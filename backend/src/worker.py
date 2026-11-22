import logging
import time
from pathlib import Path

from psycopg import Error as DatabaseError
from psycopg.types.json import Jsonb
from redis.exceptions import RedisError

from .loans import change_status, lock_loan
from .models import TRIAGE_QUEUE, TRAINING_QUEUE, active_version, notify
from .schemas import LoanStatus
from .scoring import score
from .storage import audit, connection, initialize, queue
from .training import run_job

HEARTBEAT = Path('/tmp/museum-heartbeat')


def _block_stale(db, loan_id, case_id, expected_version):
    db.execute("UPDATE triage_case SET status='failed',attempts=attempts+1,"
               "error=%s WHERE id=%s",
               ('评分依据的模型版本已停用，旧分已拦截，任务按当前版本重新排队。', case_id))
    new_id = db.execute('''INSERT INTO triage_case
        (loan_id,status,input_snapshot,requested_by,source_case_id)
        SELECT loan_id,'queued',input_snapshot,requested_by,id FROM triage_case WHERE id=%s
        RETURNING id''', (case_id,)).fetchone()['id']
    audit(db, loan_id, 'worker', 'stale_score_blocked', case_id=case_id,
          replacement_case_id=new_id, expected_version=expected_version)
    notify(TRIAGE_QUEUE, new_id)


def process_case(case_id):
    with connection() as db:
        reference = db.execute('SELECT loan_id FROM triage_case WHERE id=%s', (case_id,)).fetchone()
        if not reference:
            return
        loan_id = reference['loan_id']
        lock_loan(db, loan_id)
        case = db.execute('SELECT * FROM triage_case WHERE id=%s', (case_id,)).fetchone()
        if case['status'] != 'queued':
            return
        # 案件入队时若已绑定版本（晚到消息场景），必须沿用该版本并核对其是否仍为活动版。
        bound = db.execute('SELECT * FROM model_version WHERE id=%s',
                           (case['model_version_id'],)).fetchone() if case['model_version_id'] else None
        if bound and bound['status'] != 'active':
            _block_stale(db, loan_id, case_id, bound['version'])
            return
        model = bound or active_version(db)
        if not model:
            db.execute("UPDATE triage_case SET status='failed',attempts=attempts+1,"
                       "error=%s WHERE id=%s", ('当前没有可用的模型版本，发布新版本后再重试分流。', case_id))
            change_status(db, loan_id, LoanStatus.PACKING)
            audit(db, loan_id, 'worker', 'triage_failed', case_id=case_id, error_type='NoActiveModel')
            return
        try:
            probability, risk, evidence = score(case['input_snapshot'], model['parameters'])
        except Exception as exc:
            logging.exception('Model failed to score case %s', case_id)
            db.execute("UPDATE triage_case SET status='failed',error=%s,attempts=attempts+1 WHERE id=%s",
                       ('模型计算失败。请检查包装快照后重试。', case_id))
            change_status(db, loan_id, LoanStatus.PACKING)
            audit(db, loan_id, 'worker', 'triage_failed', case_id=case_id, error_type=type(exc).__name__)
            return
        # 晚到旧分拦截：出队后版本若已停用，绝不写入判定、绝不放行。
        current = active_version(db)
        if not current or current['id'] != model['id']:
            _block_stale(db, loan_id, case_id, model['version'])
            return
        evidence = {**model['metrics'], **evidence, 'model_version': model['version']}
        db.execute('''UPDATE triage_case SET status='scored', probability=%s, risk=%s, evidence=%s,
                       model_version_id=%s, attempts=attempts+1,computed_at=now(),error=NULL WHERE id=%s''',
                   (probability, risk, Jsonb(evidence), model['id'], case_id))
        db.execute('''INSERT INTO triage_score_history
            (case_id,model_version_id,version_label,probability,risk,evidence,score_state,computed_at)
            VALUES (%s,%s,%s,%s,%s,%s,'current',now())''',
                   (case_id, model['id'], model['version'], probability, risk, Jsonb(evidence)))
        if risk == 'high':
            db.execute("INSERT INTO unpack_review (case_id,status) VALUES (%s,'awaiting_witness')", (case_id,))
        change_status(db, loan_id, LoanStatus.REVIEW_REQUIRED if risk == 'high' else LoanStatus.READY)
        audit(db, loan_id, 'worker', 'triage_scored', case_id=case_id, risk=risk,
              probability=probability, model_version=model['version'])
        print(f'case={case_id} loan={loan_id} risk={risk} probability={probability:.6f} model={model["version"]}',
              flush=True)


def process_training(job_id):
    with connection() as db:
        run_job(db, job_id)
        job = db.execute('SELECT status FROM training_job WHERE id=%s', (job_id,)).fetchone()
        if job:
            print(f'training_job={job_id} status={job["status"]}', flush=True)


def recover_queue(redis):
    # PostgreSQL 保留排队意图，Redis 通知丢失时由这里重新推送。
    with connection() as db:
        pending_cases = db.execute(
            "SELECT id FROM triage_case WHERE status='queued' ORDER BY id LIMIT 100").fetchall()
        # 无活动版窗口期失败的任务，在新版本发布后自动恢复重算。
        retry_cases = db.execute('''SELECT id FROM triage_case WHERE status='failed'
            AND error LIKE '%%没有可用的模型版本%%' ORDER BY id LIMIT 100''').fetchall()
        pending_jobs = db.execute(
            "SELECT id FROM training_job WHERE status='queued' ORDER BY id LIMIT 20").fetchall()
        # 仅进程中断卡在 running 的任务按断点续跑；halted 必须由人工处理，不能自动复活。
        stale_running = db.execute(
            "SELECT id FROM training_job WHERE status='running' ORDER BY id LIMIT 20").fetchall()
    if retry_cases:
        with connection() as db:
            for row in retry_cases:
                db.execute("UPDATE triage_case SET status='queued',error=NULL WHERE id=%s", (row['id'],))
        pending_cases = [*pending_cases, *retry_cases]
    if pending_cases:
        redis.lpush(TRIAGE_QUEUE, *(row['id'] for row in pending_cases))
    if pending_jobs:
        redis.lpush(TRAINING_QUEUE, *(row['id'] for row in pending_jobs))
    # 进程中断后卡在 running 的任务按断点续跑。
    if stale_running:
        redis.lpush(TRAINING_QUEUE, *(row['id'] for row in stale_running))


def main():
    initialize()
    with queue() as redis:
        while True:
            try:
                message = redis.brpop(TRIAGE_QUEUE, timeout=1)
                if message:
                    process_case(int(message[1]))
                training = redis.brpop(TRAINING_QUEUE, timeout=1)
                if training:
                    process_training(int(training[1]))
                if not message and not training:
                    recover_queue(redis)
                HEARTBEAT.touch()
            except (DatabaseError, RedisError, ValueError):
                logging.exception('Queue operation failed')
                time.sleep(2)


if __name__ == '__main__':
    main()
