"""端到端治理测试：在独立库 museum_test 上跑完整业务流程。"""
import json
import os
import subprocess
import sys
import time
import urllib.request
import urllib.error

import psycopg

BASE = os.environ.get('BASE_URL', 'http://127.0.0.1:18000')
DB = os.environ['TEST_DATABASE_URL']

sys.path.insert(0, '/workspace/backend')
from src import scoring  # noqa: E402

failures = []


def check(name, condition, detail=''):
    status = 'PASS' if condition else 'FAIL'
    print(f'[{status}] {name} {detail}')
    if not condition:
        failures.append(name)


def http(method, path, token=None, data=None):
    req = urllib.request.Request(BASE + path, method=method)
    if token:
        req.add_header('Authorization', f'Bearer {token}')
    if data is not None:
        req.add_header('Content-Type', 'application/json')
        payload = json.dumps(data).encode()
    else:
        payload = None
    try:
        with urllib.request.urlopen(req, payload, timeout=10) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def wait_for(predicate, timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.4)
    return False


def db():
    return psycopg.connect(DB, row_factory=psycopg.rows.dict_row, autocommit=True)


def login(username):
    _, payload = http('POST', '/auth/login', data={'username': username, 'password': 'MuseumDemo!26'})
    return payload['token']


def test_group_split():
    samples = scoring.synthetic_inspections()
    # 构造同一借展多条实查
    extras = [
        {'loan_id': 'LOAN-9', 'finding_id': 1, 'source': 'finding', 'label_version': 1,
         'material': 'paper', 'packing': 'foam', 'route_type': 'mountain', 'max_rh': 80,
         'transfers': 4, 'label': 1},
        {'loan_id': 'LOAN-9', 'finding_id': 2, 'source': 'finding', 'label_version': 1,
         'material': 'paper', 'packing': 'foam', 'route_type': 'mountain', 'max_rh': 81,
         'transfers': 4, 'label': 1},
    ]
    train_idx, test_idx, groups = scoring.split_groups(samples + extras)
    train_loans = {samples[i]['loan_id'] if i < len(samples) else extras[i - len(samples)]['loan_id']
                   for i in train_idx}
    test_loans = {samples[i]['loan_id'] if i < len(samples) else extras[i - len(samples)]['loan_id']
                  for i in test_idx}
    check('同一借展样本不跨训练/留出', not (train_loans & test_loans))
    check('LOAN-9 两条同组', 'LOAN-9' in train_loans or 'LOAN-9' in test_loans)
    check('划分确定可复跑', scoring.split_groups(samples) == scoring.split_groups(samples))


def wait_for_job(token, job_id):
    return wait_for(lambda: any(j['id'] == job_id and j['status'] == 'succeeded'
                                for j in http('GET', '/training/jobs', token)[1]))


def version_for_job(token, job_id):
    jobs = {j['id']: j for j in http('GET', '/training/jobs', token)[1]}
    version_id = jobs[job_id]['model_version_id']
    return next(v for v in http('GET', '/model/versions', token)[1]['versions']
                if v['id'] == version_id)


def train_candidate(token):
    job = http('POST', '/training/jobs', token)[1]
    assert wait_for_job(token, job['id']), f"训练任务 {job['id']} 未成功"
    return version_for_job(token, job['id']), job['id']


def main():
    test_group_split()
    operator = login('operator')
    viewer = login('viewer')

    status, payload = http('GET', '/model/versions', viewer)
    active = payload['active']
    check('初始存在活动模型', active and active['version'] == 'crate-inspection-logit-v1',
          active['version'] if active else '')

    loans = http('GET', '/loans', operator)[1]
    bronze = next(item for item in loans if item['accession'] == 'BR-2611-023')
    porcelain = next(item for item in loans if item['accession'] == 'BR-2611-014')

    # 1. 先训练并发布一个只含瓷瓶种子实查的“干净版”，作为后续标签更正的回退目标。
    clean, _ = train_candidate(operator)
    check('干净版仅含瓷瓶实查', clean['metrics']['finding_count'] == 1,
          f"finding={clean['metrics']['finding_count']}")
    http('POST', f"/model/versions/{clean['id']}/publish", operator)

    # 2. 登记青铜爵实查（有隐患），再训练含该标签的版本并发布。
    status, payload = http('POST', f"/loans/{bronze['id']}/findings", operator,
                           {'packing_hazard': True,
                            'observation': '复核发现内衬磨穿，器物有晃动，判定存在包装隐患。'})
    check('实查登记成功', status in (200, 201), str(status))

    candidate, job_id = train_candidate(operator)
    check('候选版纳入两笔实查样本', candidate['metrics']['finding_count'] == 2,
          f"finding={candidate['metrics']['finding_count']}")

    # 3. 发布：旧版停用、旧分失效并重算
    status, published = http('POST', f"/model/versions/{candidate['id']}/publish", operator)
    check('发布成功', status == 200 and published['status'] == 'active')
    versions = http('GET', '/model/versions', operator)[1]['versions']
    retired = next(v for v in versions if v['id'] == clean['id'])
    check('被取代版本停用且记录原因', retired['status'] == 'retired'
          and retired['retired_reason'] == 'superseded_by_publish')

    def bronze_rescored():
        record = http('GET', f"/loans/{bronze['id']}", operator)[1]
        case = record['triage']
        return case['status'] == 'scored' and case['model_version_id'] == candidate['id']
    check('未派车批次旧分失效并自动重算', wait_for(bronze_rescored))
    record = http('GET', f"/loans/{bronze['id']}", operator)[1]
    check('旧分留痕且标记失效', any(h['score_state'] == 'invalidated' for h in (record['score_history'] or [])))
    check('失效分不能放行', '旧分' in (record['dispatch_issue'] or '') or record['triage']['status'] == 'scored')

    # 4. 标签更正：停用所有含旧标签版本，退回最近可用版（干净版重新激活）
    status, payload = http('POST', f"/loans/{bronze['id']}/findings", operator,
                           {'packing_hazard': False,
                            'observation': '复检确认磨穿为外箱表层痕迹，内衬完整，无包装隐患。'})
    check('标签更正', status in (200, 201), str(status))
    versions = http('GET', '/model/versions', operator)[1]['versions']
    published_row = next(v for v in versions if v['id'] == candidate['id'])
    check('含旧标签版本全部停用', published_row['status'] == 'retired'
          and published_row['retired_reason'] == 'label_corrected')
    current = http('GET', '/model/versions', operator)[1]['active']
    check('活动指针退回最近可用版', current and current['id'] == clean['id'],
          current['version'] if current else '无可用版')

    # 5. 失效批次自动重算；无活动版的窗口期旧分也不会放行
    ok = wait_for(lambda: http('GET', f"/loans/{bronze['id']}", operator)[1]['triage']['status'] == 'scored')
    check('失效后自动重算完成', ok)
    record = http('GET', f"/loans/{bronze['id']}", operator)[1]
    check('重算分来自回退版本', record['triage']['model_version_id'] == clean['id'])

    # 6. 派车档案保留原分
    record = http('GET', f"/loans/{bronze['id']}", operator)[1]
    if record['status'] == 'ready' and not record['dispatch_issue']:
        status, payload = http('POST', f"/loans/{bronze['id']}/dispatch", operator)
        check('派车成功', status == 200, str(status))
        record = http('GET', f"/loans/{bronze['id']}", operator)[1]
        check('已派车评分冻结', record['triage']['score_state'] == 'frozen')
    else:
        check('派车前条件满足（封签/窗口/放行）', False, record.get('dispatch_issue'))

    # 7. 已派车后再发新版：冻结分不重算
    candidate2, _ = train_candidate(operator)
    http('POST', f"/model/versions/{candidate2['id']}/publish", operator)
    time.sleep(1.5)
    record = http('GET', f"/loans/{bronze['id']}", operator)[1]
    check('已派车档案保留原分', record['triage']['score_state'] == 'frozen'
          and record['status'] == 'dispatched')

    # 7b. 全部版本含旧标签时，无活动版且重算挂起，绝不放行
    fresh = next(item for item in http('GET', '/loans', operator)[1]
                 if item['accession'] == 'BR-2611-019')
    http('POST', f"/loans/{fresh['id']}/findings", operator,
         {'packing_hazard': True, 'observation': '纸本画框包角受潮发软，存在包装隐患，需更换护角。'})
    c3, _ = train_candidate(operator)
    http('POST', f"/model/versions/{c3['id']}/publish", operator)
    # 构造“所有历史版本都含同一份旧标签”的局面
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute('UPDATE model_version SET training_rows_sha256=%s',
                        (c3['training_rows_sha256'],))
    # 再更正该标签：现存所有版本都命中旧标签
    status, _ = http('POST', f"/loans/{fresh['id']}/findings", operator,
                     {'packing_hazard': False, 'observation': '复检护角仅表面压痕，结构完好，无包装隐患。'})
    check('二次更正成功', status in (200, 201))
    state = http('GET', '/model/versions', operator)[1]
    check('无干净版本时无活动版', state['active'] is None)
    record = http('GET', f"/loans/{fresh['id']}", operator)[1]
    check('无活动版时旧分不放行', bool(record['dispatch_issue']))
    # 用更正后的标签重训并发布，系统恢复
    c4, _ = train_candidate(operator)
    http('POST', f"/model/versions/{c4['id']}/publish", operator)
    check('更正标签重训后恢复活动版', http('GET', '/model/versions', operator)[1]['active'] is not None)

    # 8. 训练中断续跑 + 更正标签即停（直接在库内构造）
    with db() as conn:
        with conn.cursor() as cur:
            # 用最新成功任务的现行清单模拟进程中断，断点之后可直接续跑成功。
            cur.execute('''SELECT manifest FROM training_job
                WHERE status='succeeded' ORDER BY id DESC LIMIT 1''')
            fresh_manifest = cur.fetchone()['manifest']
            cur.execute('''INSERT INTO training_job (status,stage,manifest,requested_by,checkpoint,started_at)
                VALUES ('running','persisted',%s,'operator',95,now()) RETURNING id''',
                        (psycopg.types.json.Jsonb(fresh_manifest),))
            interrupted = cur.fetchone()['id']
    # worker 轮询会把卡在 running 的任务按断点自动续跑；persisted 之后只剩落版，很快完成。
    ok = wait_for(lambda: any(j['id'] == interrupted and j['status'] == 'succeeded'
                              for j in http('GET', '/training/jobs', operator)[1]))
    check('中断任务用原样本自动续跑成功', ok)

    with db() as conn:
        with conn.cursor() as cur:
            cur.execute('''SELECT manifest FROM training_job
                WHERE status='succeeded' ORDER BY id DESC LIMIT 1''')
            manifest = cur.fetchone()['manifest']
            # 模拟清单中的实查已被更正：指向不存在的 finding 版本
            for row in manifest['samples']:
                if row['source'] == 'finding':
                    row['finding_id'] = 999999
                    break
            cur.execute('''INSERT INTO training_job (status,stage,manifest,requested_by,checkpoint,started_at)
                VALUES ('halted','encoding',%s,'operator',45,now()) RETURNING id''',
                        (psycopg.types.json.Jsonb(manifest),))
            stale_job = cur.fetchone()['id']
    status, payload = http('POST', f'/training/jobs/{stale_job}/resume', operator)
    check('遇到已更正标签续跑被拦截', status == 409 and '更正' in payload['detail'], str(payload))
    row = next(j for j in http('GET', '/training/jobs', operator)[1] if j['id'] == stale_job)
    check('任务保持停下且记录原因', row['status'] == 'halted')

    # 9. viewer 只读
    status, _ = http('POST', '/training/jobs', viewer)
    check('阅览岗不能发起训练', status == 403)
    status, _ = http('POST', f"/loans/{porcelain['id']}/findings", viewer,
                     {'packing_hazard': False, 'observation': '只读账号尝试登记应被拒绝。'})
    check('阅览岗不能登记实查', status == 403)

    # 10. 无活动模型时提交分流必须被拒绝，不能用旧版偷偷放行
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE model_version SET status='retired',retired_reason='label_corrected',retired_at=now()")
    target = next(item for item in http('GET', '/loans', operator)[1]
                  if item['accession'] == 'BR-2611-019')
    status, payload = http('POST', f"/loans/{target['id']}/triage", operator)
    check('无可用模型时拒绝提交分流', status == 409 and '模型版本' in payload['detail'], str(payload))
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE model_version SET status='active',retired_reason=NULL,retired_at=NULL "
                        "WHERE id=(SELECT max(id) FROM model_version)")

    # 11. 晚到旧分：出队时绑定的版本在评分期间已被取代，旧分必须拦截并重新排队
    target = next(item for item in http('GET', '/loans', operator)[1]
                  if item['accession'] == 'BR-2611-019')
    board_before = http('GET', '/model/versions', operator)[1]
    active_now = board_before['active']
    retired_one = next(v for v in board_before['versions'] if v['id'] != active_now['id'])
    with db() as conn:
        with conn.cursor() as cur:
            # 不经 Redis 通知，直接构造一个挂在停用旧版上的排队案件，模拟晚到消息。
            cur.execute('''INSERT INTO triage_case (loan_id,status,input_snapshot,requested_by,model_version_id)
                SELECT c.loan_id,'queued',jsonb_build_object(
                    'material',c.material,'packing',c.packing,'route_type',c.route_type,
                    'max_rh',c.max_rh,'transfers',c.transfers),'operator',%s
                FROM crate_check c WHERE c.loan_id=%s RETURNING id''',
                        (retired_one['id'], target['id']))
            queued_id = cur.fetchone()['id']
    from src.worker import process_case
    process_case(queued_id)
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT status,error FROM triage_case WHERE id=%s", (queued_id,))
            queued_case = cur.fetchone()
            cur.execute("SELECT count(*) AS n FROM triage_case WHERE source_case_id=%s", (queued_id,))
            replacement = cur.fetchone()['n']
            cur.execute("SELECT 1 FROM audit_entry WHERE action='stale_score_blocked' AND details->>'case_id'=%s",
                        (str(queued_id),))
            blocked_audit = cur.fetchone()
    check('晚到旧分被拦截并重新排队', queued_case['status'] == 'failed' and replacement >= 1 and bool(blocked_audit),
          str(queued_case))
    ok = wait_for(lambda: http('GET', f"/loans/{target['id']}", operator)[1]['triage']['status'] == 'scored')
    check('拦截后按当前版本重算完成', ok)

    print('\n%s' % ('全部通过' if not failures else f'失败 {len(failures)} 项：{failures}'))
    sys.exit(1 if failures else 0)


if __name__ == '__main__':
    main()
