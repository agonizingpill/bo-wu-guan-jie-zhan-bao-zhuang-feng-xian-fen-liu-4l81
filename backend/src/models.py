from fastapi import HTTPException
from psycopg.types.json import Jsonb

from .storage import audit, queue

TRAINING_QUEUE = 'museum:training'
TRIAGE_QUEUE = 'museum:triage'


def notify(redis_queue, value):
    try:
        with queue() as redis:
            redis.lpush(redis_queue, value)
    except Exception:
        # Redis 通知丢失时由 worker 的轮询恢复兜底，提交结果不受影响。
        pass


def active_version(db, lock=False):
    clause = 'FOR UPDATE' if lock else ''
    return db.execute(f'SELECT * FROM model_version WHERE status=%s {clause}', ('active',)).fetchone()


def list_versions(db):
    return db.execute('''SELECT v.*, j.status AS job_status, j.id AS job_id,
           (SELECT count(*) FROM triage_case c WHERE c.model_version_id=v.id
              AND c.score_state='invalidated') AS invalidated_cases
           FROM model_version v LEFT JOIN training_job j ON j.id=v.training_job_id
           ORDER BY v.id DESC''').fetchall()


def list_jobs(db):
    return db.execute('SELECT * FROM training_job ORDER BY id DESC LIMIT 20').fetchall()


def register_candidate(db, version, parameters, metrics, job_id):
    row = db.execute('''INSERT INTO model_version
        (version,status,metrics,parameters,training_rows_sha256,training_job_id)
        VALUES (%s,'candidate',%s,%s,%s,%s) RETURNING *''',
                     (version, Jsonb(metrics), Jsonb(parameters),
                      metrics['training_rows_sha256'], job_id)).fetchone()
    db.execute('UPDATE training_job SET model_version_id=%s,stage=%s,status=%s,finished_at=now() WHERE id=%s',
               (row['id'], 'done', 'succeeded', job_id))
    return row


def publish(db, version_id, actor):
    db.execute('SELECT pg_advisory_xact_lock(66004)')
    target = db.execute("SELECT * FROM model_version WHERE id=%s FOR UPDATE", (version_id,)).fetchone()
    if not target:
        raise HTTPException(404, '未找到该模型版本')
    if target['status'] == 'retired':
        raise HTTPException(409, '该版本已停用，不能发布')
    previous = active_version(db, lock=True)
    if previous and previous['id'] != target['id']:
        db.execute('''UPDATE model_version SET status='retired',retired_reason='superseded_by_publish',retired_at=now()
                    WHERE id=%s''', (previous['id'],))
        invalidate_scores(db, previous['id'], 'superseded_by_publish')
        audit(db, None, actor, 'model_retired', version=previous['version'],
              reason='superseded_by_publish', successor=target['version'])
    if target['status'] != 'active':
        db.execute("UPDATE model_version SET status='active',published_at=now(),retired_reason=NULL,retired_at=NULL WHERE id=%s",
                   (version_id,))
    audit(db, None, actor, 'model_published', version=target['version'])
    return db.execute('SELECT * FROM model_version WHERE id=%s', (version_id,)).fetchone()


def retire_for_label(db, finding, actor='system'):
    """标签更正后停用所有含旧标签的版本，并把活动指针退回最近一个可用版。"""
    old_sha = finding['previous_rows_sha']
    if not old_sha:
        return []
    affected = db.execute('''SELECT * FROM model_version
        WHERE training_rows_sha256=%s AND status <> 'retired' ORDER BY id''', (old_sha,)).fetchall()
    if not affected:
        return []
    db.execute('SELECT pg_advisory_xact_lock(66004)')
    retired = []
    for version in affected:
        db.execute('''UPDATE model_version SET status='retired',retired_reason='label_corrected',retired_at=now()
                    WHERE id=%s''', (version['id'],))
        invalidate_scores(db, version['id'], 'label_corrected')
        retired.append(version['version'])
        audit(db, None, actor, 'model_retired', version=version['version'],
              reason='label_corrected', related_loan_id=finding['loan_id'], finding_id=finding['new_id'])
    fallback = db.execute('''SELECT * FROM model_version WHERE status <> 'retired'
        ORDER BY published_at DESC NULLS LAST, id DESC LIMIT 1''').fetchone()
    if fallback and fallback['status'] == 'candidate':
        db.execute("UPDATE model_version SET status='active',published_at=now() WHERE id=%s", (fallback['id'],))
        audit(db, None, actor, 'model_published', version=fallback['version'], reason='fallback_after_label_correction')
    elif not fallback:
        # 没有候选版时，退回最近一个仅因发布取代而停用、且不含旧标签的版本。
        safe = db.execute('''SELECT * FROM model_version
            WHERE retired_reason='superseded_by_publish' AND training_rows_sha256 <> %s
            ORDER BY id DESC LIMIT 1''', (old_sha,)).fetchone()
        if safe:
            db.execute('''UPDATE model_version SET status='active',retired_reason=NULL,retired_at=NULL
                WHERE id=%s''', (safe['id'],))
            audit(db, None, actor, 'model_reactivated', version=safe['version'],
                  reason='fallback_after_label_correction')
        else:
            audit(db, None, actor, 'model_no_fallback', retired=retired)
    return retired


def invalidate_scores(db, version_id, reason):
    """停用版本的分对未派车批次失效：归档旧分、作废复核单并重新入队；已派车档案保留原分。"""
    cases = db.execute('''SELECT c.*, l.status AS loan_status FROM triage_case c
        JOIN exhibit_loan l ON l.id=c.loan_id
        WHERE c.model_version_id=%s AND c.score_state='current'
          AND l.status <> 'dispatched' FOR UPDATE OF c''', (version_id,)).fetchall()
    for case in cases:
        db.execute("UPDATE exhibit_loan SET status='triage_pending' WHERE id=%s", (case['loan_id'],))
        db.execute("UPDATE unpack_review SET status='voided' WHERE case_id=%s AND status <> 'voided'",
                   (case['id'],))
        if case['status'] == 'scored':
            # 已出分：旧分归档留痕，案件作废并生成新的排队任务。
            db.execute('''UPDATE triage_case SET status='voided',score_state='invalidated',
                invalidated_reason=%s,invalidated_at=now() WHERE id=%s''', (reason, case['id']))
            db.execute('''INSERT INTO triage_score_history
                (case_id,model_version_id,version_label,probability,risk,evidence,score_state,invalidated_reason,computed_at)
                SELECT id,model_version_id,%s,probability,risk,evidence,'invalidated',%s,coalesce(computed_at,now())
                FROM triage_case WHERE id=%s''',
                       (_version_label(db, case['model_version_id']), reason, case['id']))
            new_id = db.execute('''INSERT INTO triage_case
                (loan_id,status,input_snapshot,requested_by,source_case_id)
                SELECT loan_id,'queued',input_snapshot,requested_by,id FROM triage_case WHERE id=%s
                RETURNING id''', (case['id'],)).fetchone()['id']
            notify(TRIAGE_QUEUE, new_id)
            audit(db, case['loan_id'], 'system', 'score_invalidated',
                  case_id=case['id'], replacement_case_id=new_id, reason=reason)
        else:
            # 排队/失败中的案件没有旧分：剥离停用版本，worker 出队时按当前活动版计算。
            db.execute('UPDATE triage_case SET status=%s,model_version_id=NULL,error=NULL WHERE id=%s',
                       ('queued' if case['status'] != 'queued' else 'queued', case['id']))
            notify(TRIAGE_QUEUE, case['id'])
    frozen = db.execute('''SELECT count(*) AS n FROM triage_case c
        JOIN exhibit_loan l ON l.id=c.loan_id
        WHERE c.model_version_id=%s AND c.score_state='current' AND l.status='dispatched' ''',
                        (version_id,)).fetchone()['n']
    if frozen:
        db.execute('''UPDATE triage_case SET score_state='frozen' WHERE id IN (
            SELECT c.id FROM triage_case c JOIN exhibit_loan l ON l.id=c.loan_id
            WHERE c.model_version_id=%s AND l.status='dispatched')''', (version_id,))


def _version_label(db, version_id):
    if not version_id:
        return 'crate-target-logit-v1'
    row = db.execute('SELECT version FROM model_version WHERE id=%s', (version_id,)).fetchone()
    return row['version'] if row else 'unknown-version'


def freeze_case(db, case_id):
    db.execute("UPDATE triage_case SET score_state='frozen' WHERE id=%s", (case_id,))
    db.execute('''INSERT INTO triage_score_history
        (case_id,model_version_id,version_label,probability,risk,evidence,score_state,computed_at)
        SELECT id,model_version_id,coalesce((SELECT version FROM model_version m WHERE m.id=model_version_id),'legacy'),
        probability,risk,evidence,'frozen',coalesce(computed_at,now()) FROM triage_case WHERE id=%s''', (case_id,))
