import logging

from fastapi import HTTPException
from psycopg.types.json import Jsonb

from . import findings, scoring
from .models import register_candidate
from .storage import audit

STAGES = ('manifest', 'encoding', 'fitting', 'metrics', 'persisted')
CHECKPOINT = {'manifest': 15, 'encoding': 45, 'fitting': 70, 'metrics': 90, 'persisted': 100}


def create_job(db, actor):
    db.execute('SELECT pg_advisory_xact_lock(66005)')
    busy = db.execute("SELECT id FROM training_job WHERE status IN ('queued','running') LIMIT 1").fetchone()
    if busy:
        raise HTTPException(409, '已有训练任务在排队或运行中，请等待其结束')
    samples = findings.training_samples(db)
    job = db.execute('''INSERT INTO training_job (status,stage,manifest,requested_by)
        VALUES ('queued','queued',%s,%s) RETURNING *''',
                     (Jsonb({'samples': samples, 'resumed': 0}), actor)).fetchone()
    audit(db, None, actor, 'training_requested', job_id=job['id'],
          finding_count=sum(1 for row in samples if row['source'] == 'finding'),
          rows=len(samples))
    return job


def _advance(db, job_id, stage):
    db.execute('UPDATE training_job SET stage=%s,checkpoint=%s WHERE id=%s',
               (stage, CHECKPOINT[stage], job_id))


def run_job(db, job_id):
    """用任务清单中的原样本运行；每个检查点都核对标签，遇到更正立即停下。

    续跑时阶段检查点保留在台账上，拟合按原样本确定性重算（同清单结果一致），
    因此中断恢复不会跳过任何标签新鲜度校验。
    """
    job = db.execute('SELECT * FROM training_job WHERE id=%s FOR UPDATE', (job_id,)).fetchone()
    if not job or job['status'] not in ('queued', 'running'):
        return
    db.execute("UPDATE training_job SET status='running',started_at=coalesce(started_at,now()) WHERE id=%s", (job_id,))
    samples = job['manifest']['samples']
    try:
        for stage in STAGES:
            stale = findings.assert_labels_fresh(db, samples)
            if stale:
                _halt(db, job_id, stale)
                return
            if stage == 'fitting':
                parameters, metrics = scoring.train(samples, seed=scoring.SEED)
            _advance(db, job_id, stage)
        stale = findings.assert_labels_fresh(db, samples)
        if stale:
            _halt(db, job_id, stale)
            return
        latest = db.execute('SELECT version FROM model_version ORDER BY id DESC LIMIT 1').fetchone()
        version = _next_version(latest['version'] if latest else None)
        register_candidate(db, version, parameters, metrics, job_id)
        audit(db, None, job['requested_by'], 'training_succeeded', job_id=job_id,
              version=version, resumed_count=job['resumed_count'])
    except Exception as exc:
        logging.exception('Training job %s failed', job_id)
        db.execute("UPDATE training_job SET status='failed',error=%s,finished_at=now() WHERE id=%s",
                   (f'训练失败：{type(exc).__name__}', job_id))
        audit(db, None, 'worker', 'training_failed', job_id=job_id, error_type=type(exc).__name__)


def resume_job(db, job_id, actor):
    job = db.execute('SELECT * FROM training_job WHERE id=%s FOR UPDATE', (job_id,)).fetchone()
    if not job:
        raise HTTPException(404, '未找到训练任务')
    if job['status'] != 'halted':
        raise HTTPException(409, '只有因标签更正停下的训练可以用原样本续跑')
    samples = job['manifest']['samples']
    stale = findings.assert_labels_fresh(db, samples)
    if stale:
        raise HTTPException(409, f'原训练样本中的标签已更正（实查 {stale}），续跑已被拦截，请发起新训练')
    db.execute('''UPDATE training_job SET status='queued',halt_reason=NULL,error=NULL,
                  resumed_count=resumed_count+1,
                  manifest=jsonb_set(manifest,'{resumed}',to_jsonb(resumed_count+1)) WHERE id=%s''', (job_id,))
    audit(db, None, actor, 'training_resumed', job_id=job_id, resumed_count=job['resumed_count'] + 1)
    return db.execute('SELECT * FROM training_job WHERE id=%s', (job_id,)).fetchone()


def _halt(db, job_id, stale):
    reason = f"训练样本中的实查标签已更正（finding {stale}），按规程停止，未生成模型版本"
    db.execute("UPDATE training_job SET status='halted',halt_reason=%s,finished_at=now() WHERE id=%s",
               (reason, job_id))
    audit(db, None, 'worker', 'training_halted', job_id=job_id, stale_findings=stale)


def _next_version(latest):
    if not latest:
        return 'crate-inspection-logit-v2'
    if not latest.startswith('crate-inspection-logit-v'):
        return 'crate-inspection-logit-v2'
    try:
        number = int(latest.rsplit('v', 1)[1])
    except ValueError:
        number = 1
    return f'crate-inspection-logit-v{number + 1}'
