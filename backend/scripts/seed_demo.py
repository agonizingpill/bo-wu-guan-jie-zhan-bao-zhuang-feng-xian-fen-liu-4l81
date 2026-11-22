import json
from datetime import datetime

from psycopg.types.json import Jsonb

from src import findings, scoring
from src.models import active_version
from src.security import hash_password
from src.storage import audit, connection, initialize

ACCOUNTS = (('operator', '林遥', 'operator'), ('supervisor', '周岚', 'supervisor'), ('viewer', '程雨', 'viewer'))
PASSWORD = 'MuseumDemo!26'

LOANS = (
    ('BR-2611-014', '青花缠枝莲纹瓷瓶', '澄江博物馆', '一号临展厅', '17:30',
     'BX-014', 'SEAL-2611-014', 'porcelain', 'foam', 'mountain', 79, 4, '13:20', '14:10', '沪A·藏02'),
    ('BR-2611-019', '《秋江帆影》纸本设色', '临浦书画院', '纸本展厅', '16:40',
     'BX-019', None, 'paper', 'double_wall', 'transfer', 60, 1, '12:30', '13:15', '沪A·藏09'),
    ('BR-2611-023', '饕餮纹青铜爵', '鹭湾历史馆', '二号临展厅', '18:00',
     'BX-023', 'SEAL-2611-023', 'bronze', 'suspension', 'direct', 43, 0, '13:45', '14:25', '沪A·藏06'),
)

FINDINGS = {
    'BR-2611-014': (True, '开箱见外箱角部泡棉塌陷，山地转运致内衬松动，存在包装隐患，需悬托复核。'),
}


def clock(value):
    return datetime.fromisoformat(f'2026-11-26T{value}:00+08:00')


def ensure_baseline_model(db):
    if active_version(db):
        return active_version(db)
    samples = findings.training_samples(db)
    parameters, metrics = scoring.train(samples, seed=scoring.SEED)
    job = db.execute('''INSERT INTO training_job (status,stage,manifest,requested_by,checkpoint,started_at,finished_at)
        VALUES ('succeeded','done',%s,'operator',100,now(),now()) RETURNING id''',
                     (Jsonb({'samples': samples, 'resumed': 0}),)).fetchone()
    version = db.execute('''INSERT INTO model_version
        (version,status,metrics,parameters,training_rows_sha256,training_job_id,published_at)
        VALUES (%s,'active',%s,%s,%s,%s,now()) RETURNING *''',
                         ('crate-inspection-logit-v1', Jsonb(metrics), Jsonb(parameters),
                          metrics['training_rows_sha256'], job['id'])).fetchone()
    db.execute('UPDATE training_job SET model_version_id=%s WHERE id=%s', (version['id'], job['id']))
    audit(db, None, 'operator', 'model_published', version=version['version'], reason='baseline_seed')
    return version


def seed():
    with connection() as db:
        db.execute('SELECT pg_advisory_xact_lock(66003)')
        for username, name, role in ACCOUNTS:
            if not db.execute('SELECT 1 FROM staff WHERE username=%s', (username,)).fetchone():
                db.execute('INSERT INTO staff (username,display_name,password_hash,role) VALUES (%s,%s,%s,%s)',
                           (username, name, hash_password(PASSWORD), role))
        setup_inserted = db.execute('''INSERT INTO transit_window (gallery,label,begins_at,ends_at,status,recorded_by)
          VALUES ('纸本展厅','轨道灯标定',%s,%s,'setup','supervisor') ON CONFLICT DO NOTHING RETURNING id''',
                                    (clock('12:00'), clock('13:30'))).fetchone()
        if setup_inserted:
            audit(db, None, 'supervisor', 'gallery_setup', window_id=setup_inserted['id'],
                  gallery='纸本展厅', label='轨道灯标定')
        model = ensure_baseline_model(db)
        inserted = 0
        for (accession, name, lender, gallery, due, crate, seal, material, packing, route, rh,
             transfers, start, end, vehicle) in LOANS:
            is_sample = accession == 'BR-2611-014'
            is_blocked = accession == 'BR-2611-019'
            seal_status = 'sampling' if is_sample else 'packing' if is_blocked else 'ready'
            row = db.execute('''INSERT INTO exhibit_loan (accession,object_name,lender,gallery,must_arrive,status)
                         VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (accession) DO NOTHING RETURNING id''',
                             (accession, name, lender, gallery, clock(due), seal_status)).fetchone()
            if not row:
                continue
            inserted += 1
            loan_id = row['id']
            audit(db, loan_id, 'operator', 'loan_registered', accession=accession)
            db.execute('''INSERT INTO crate_check (loan_id,crate_code,seal_code,material,packing,route_type,max_rh,transfers,noted_by)
                          VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'operator')''',
                       (loan_id, crate, seal, material, packing, route, rh, transfers))
            audit(db, loan_id, 'operator', 'crate_checked', crate_code=crate, seal_code=seal,
                  issue='missing_seal' if not seal else None)
            reason = '纸本展厅正在布置（轨道灯标定），请改期' if is_blocked else None
            window_id = db.execute('''INSERT INTO transit_window (loan_id,gallery,vehicle,label,begins_at,ends_at,status,conflict_reason,recorded_by)
                          VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'operator') RETURNING id''',
                                   (loan_id, gallery, vehicle, accession, clock(start), clock(end),
                                    'conflict' if is_blocked else 'booked', reason)).fetchone()['id']
            audit(db, loan_id, 'operator', 'window_conflict' if is_blocked else 'window_reserved',
                  window_id=window_id, reason=reason)
            if is_blocked:
                continue
            snapshot = dict(material=material, packing=packing, route_type=route, max_rh=rh, transfers=transfers)
            probability, risk, evidence = scoring.score(snapshot, model['parameters'])
            assert risk == ('high' if is_sample else 'low'), (accession, probability)
            case_id = db.execute('''INSERT INTO triage_case
                (loan_id,status,input_snapshot,probability,risk,evidence,attempts,requested_by,computed_at,model_version_id)
                VALUES (%s,'scored',%s,%s,%s,%s,1,'operator',now(),%s) RETURNING id''',
                                 (loan_id, Jsonb(snapshot), probability, risk,
                                  Jsonb({**model['metrics'], **evidence, 'model_version': model['version']}),
                                  model['id'])).fetchone()['id']
            db.execute('''INSERT INTO triage_score_history
                (case_id,model_version_id,version_label,probability,risk,evidence,score_state,computed_at)
                VALUES (%s,%s,%s,%s,%s,%s,'current',now())''',
                       (case_id, model['id'], model['version'], probability, risk,
                        Jsonb({**model['metrics'], **evidence, 'model_version': model['version']})))
            audit(db, loan_id, 'worker', 'triage_scored', case_id=case_id, risk=risk,
                  probability=probability, model_version=model['version'])
            if accession in FINDINGS:
                hazard, observation = FINDINGS[accession]
                db.execute('''INSERT INTO inspection_finding
                    (loan_id,packing_hazard,observation,feature_snapshot,label_version,recorded_by)
                    VALUES (%s,%s,%s,%s,1,'operator')''',
                           (loan_id, hazard, observation, Jsonb(snapshot)))
                audit(db, loan_id, 'operator', 'finding_recorded', label_version=1,
                      packing_hazard=hazard, observation=observation)
            if is_sample:
                witness = '两侧内衬贴合，器口与肩部无新裂纹。'
                reason = '开箱确认器物由独立悬托固定，震动痕迹仅见外箱。按规程复核封签。'
                db.execute('''INSERT INTO unpack_review (case_id,status,witness_by,witness_note,witnessed_at,
                           supervisor_by,decision_reason,decided_at)
                           VALUES (%s,'sampling','operator',%s,%s,'supervisor',%s,%s)''',
                           (case_id, witness, clock('09:15'), reason, clock('09:28')))
                audit(db, loan_id, 'operator', 'unpack_witnessed', observation=witness)
                audit(db, loan_id, 'supervisor', 'downgrade_sample_required', reason=reason)
        return {'inserted_loans': inserted,
                'total_loans': db.execute('SELECT count(*) AS n FROM exhibit_loan').fetchone()['n'],
                'active_model': active_version(db)['version']}


if __name__ == '__main__':
    initialize()
    print(json.dumps(seed(), ensure_ascii=False))
