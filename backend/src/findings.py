from fastapi import HTTPException
from psycopg.types.json import Jsonb

from . import scoring
from .loans import active_loan, dossier
from .models import retire_for_label
from .storage import audit

FEATURES = ('material', 'packing', 'route_type', 'max_rh', 'transfers')


def register(db, loan_id, data, actor):
    active_loan(db, loan_id)
    check = db.execute('SELECT * FROM crate_check WHERE loan_id=%s', (loan_id,)).fetchone()
    if not check:
        raise HTTPException(409, '请先完成包装检查，再登记开箱实查')
    current = db.execute('''SELECT * FROM inspection_finding WHERE loan_id=%s AND superseded_at IS NULL
                            FOR UPDATE''', (loan_id,)).fetchone()
    if current and bool(current['packing_hazard']) == data.packing_hazard:
        raise HTTPException(409, '实查标签与当前记录相同，无需更正')
    previous_rows_sha = _training_rows_sha(db) if current else None
    snapshot = {key: check[key] for key in FEATURES}
    label_version = (current['label_version'] + 1) if current else 1
    if current:
        # 先释放部分唯一索引占位（不触发自引用外键），再写入更正后的实查。
        db.execute('UPDATE inspection_finding SET superseded_at=now() WHERE id=%s', (current['id'],))
    row = db.execute('''INSERT INTO inspection_finding
        (loan_id,packing_hazard,observation,feature_snapshot,label_version,recorded_by)
        VALUES (%s,%s,%s,%s,%s,%s) RETURNING *''',
                     (loan_id, data.packing_hazard, data.observation, Jsonb(snapshot), label_version, actor)).fetchone()
    if current:
        db.execute('UPDATE inspection_finding SET superseded_by=%s WHERE id=%s', (row['id'], current['id']))
    audit(db, loan_id, actor, 'finding_corrected' if current else 'finding_recorded',
          finding_id=row['id'], label_version=label_version,
          packing_hazard=data.packing_hazard, previous_label=None if not current else current['packing_hazard'],
          observation=data.observation)
    retired = retire_for_label(db, {
        'loan_id': loan_id, 'new_id': row['id'], 'previous_rows_sha': previous_rows_sha,
    }, actor=actor) if current else []
    if retired:
        audit(db, loan_id, actor, 'label_governance_applied', retired_versions=retired)
    return dossier(db, loan_id)


def _finding_rows(db):
    return db.execute('''SELECT f.* FROM inspection_finding f
        WHERE f.superseded_at IS NULL ORDER BY f.id''').fetchall()


def _training_rows_sha(db):
    # 与模型版本留档的 training_rows_sha256 同口径：实查样本在前、合成垫底，全量参与哈希。
    return scoring.sha(training_samples(db))


def _sample(row):
    snapshot = row['feature_snapshot']
    return {'loan_id': f"LOAN-{row['loan_id']}", 'finding_id': row['id'], 'source': 'finding',
            'label_version': row['label_version'],
            'material': snapshot['material'], 'packing': snapshot['packing'],
            'route_type': snapshot['route_type'], 'max_rh': snapshot['max_rh'],
            'transfers': snapshot['transfers'], 'label': 1 if row['packing_hazard'] else 0}


def training_samples(db):
    """实查样本优先，合成数据仅在实查不足时垫底；每个借展的多条实查同属一个划分组。"""
    samples = [_sample(row) for row in _finding_rows(db)]
    samples.extend(scoring.synthetic_inspections())
    return samples


def assert_labels_fresh(db, manifest_samples, actor='worker'):
    """续跑前置校验：任何已更正标签都会让旧训练任务停下，绝不带着旧标签继续。"""
    current = {row['id']: row for row in _finding_rows(db)}
    stale = []
    for row in manifest_samples:
        if row['source'] != 'finding':
            continue
        finding = current.get(row['finding_id'])
        if not finding or finding['label_version'] != row['label_version'] \
                or bool(finding['packing_hazard']) != bool(row['label']):
            stale.append(row['finding_id'])
    return sorted({item for item in stale if item is not None})
