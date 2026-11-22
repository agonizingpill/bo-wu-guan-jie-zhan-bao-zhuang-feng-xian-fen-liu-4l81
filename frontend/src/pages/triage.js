import {activity, fileIndex, heading, note, recordHeader, stamp} from '../components.js';
import {hazardLabels, jobStates, materials, modelStates, packings, retiredReasons, routes} from '../constants.js';
import {dateTime, escape as e, percent} from '../format.js';

function versionBadge(board) {
  const active = board?.active;
  if (!active) return '<p class="alert">当前没有可用模型版本，旧分不能放行；请用更正后的实查重新训练并发布。</p>';
  return `<div class="model-current"><div><strong>${e(active.version)}</strong>
    <small>训练 ${active.metrics.train_count} 行 · 留出 ${active.metrics.test_count} 行
      · 实查样本 ${active.metrics.finding_count} 条 · 发布于 ${dateTime(active.published_at)}</small></div>
    <p>ROC AUC ${active.metrics.roc_auc ?? '—'} · Brier ${active.metrics.brier_score ?? '—'}
      · 阈值 ${percent(active.parameters.threshold)}</p></div>`;
}

function versionBoard(board, staff, jobs) {
  const running = jobs?.some((job) => ['queued', 'running'].includes(job.status));
  return `<section class="governance"><div class="section-line"><h3>模型版本与停用原因</h3>
    ${staff.role === 'operator'
      ? `<form data-action="training"><button ${running ? 'disabled' : ''}>${running ? '训练进行中' : '发起训练'}</button></form>`
      : ''}</div>
    ${versionBadge(board)}
    <div class="version-list">${(board?.versions ?? []).map((version) => `
      <article class="version-row" data-state="${e(version.status)}">
        <div><strong>${e(version.version)}</strong> ${stamp(version.status, modelStates)}
          <small>${e(version.metrics.training_source === 'inspection_findings' ? '实查样本训练' : '合成样本训练')}
            · 实查 ${version.metrics.finding_count} 条 · 生成于 ${dateTime(version.created_at)}</small></div>
        ${version.status === 'retired'
          ? `<p class="retired-reason">停用原因：${e(retiredReasons[version.retired_reason] ?? version.retired_reason)} · ${dateTime(version.retired_at)}</p>
             ${version.invalidated_cases ? `<small>该版已失效评分 ${version.invalidated_cases} 笔，已自动重算或排队</small>` : ''}`
          : ''}
        ${version.status === 'candidate' && staff.role === 'operator'
          ? `<form data-action="publish"><input type="hidden" name="version_id" value="${version.id}"><button>发布为当前版</button></form>`
          : ''}
      </article>`).join('')}</div></section>`;
}

function jobBoard(jobs) {
  if (!jobs?.length) return '';
  return `<section class="jobs"><h3>训练任务</h3><ol class="job-list">
    ${jobs.slice(0, 6).map((job) => `<li><div><strong>#${job.id}</strong> ${stamp(job.status, jobStates)}
      <small>${e(job.stage)} · 进度 ${job.checkpoint}% · 续跑 ${job.resumed_count} 次</small></div>
      ${job.halt_reason ? `<p class="alert">${e(job.halt_reason)}</p>` : ''}
      ${job.error ? `<p class="alert">${e(job.error)}</p>` : ''}
      ${job.status === 'halted' ? `<form data-action="resume-training">
        <input type="hidden" name="job_id" value="${job.id}"><button class="subtle">用原样本续跑</button></form>` : ''}
    </li>`).join('')}</ol></section>`;
}

function findingPanel(record, staff) {
  const finding = record.finding;
  if (staff.role !== 'operator' || record.status === 'dispatched') {
    if (!finding) return '<p class="empty">尚未登记开箱实查。</p>';
    return findingReadout(finding);
  }
  return `<div class="finding-box">${finding ? findingReadout(finding) : '<p class="empty">开箱后登记是否发现包装隐患，作为训练标签。</p>'}
    <form data-action="finding" class="finding-form">
      <label>实查结论<select name="packing_hazard" required>
        <option value="true" ${finding?.packing_hazard === true ? 'selected' : ''}>发现包装隐患（正例）</option>
        <option value="false" ${finding?.packing_hazard === false ? 'selected' : ''}>未发现包装隐患（负例）</option>
      </select></label>
      ${note('开箱实查记录', 'observation', '记录包装结构、内衬、封签与隐患的实见情况')}
      <div class="form-actions"><button>${finding ? '更正实查标签' : '登记实查'}</button></div>
      <p class="muted">更正标签会停用所有含旧标签的版本；同一借展的样本只进入训练或留出其中一边。</p>
    </form></div>`;
}

function findingReadout(finding) {
  return `<div class="finding-readout"><h4>当前实查标签（第 ${finding.label_version} 版）</h4>
    ${stamp(String(finding.packing_hazard), {'true': '发现隐患', 'false': '未发现隐患'})}
    <p>${e(finding.observation)}</p><small>${e(finding.recorded_by)} · ${dateTime(finding.created_at)}</small></div>`;
}

function riskResult(result) {
  const evidence = result.evidence;
  const snapshot = result.input_snapshot;
  const p = result.probability;
  const threshold = evidence.risk_threshold;
  const stale = result.score_state === 'invalidated';
  return `<div class="scoring-sheet"><section class="scoring-slip">
    <h3>模型判定 ${stamp(result.risk, {low: '低风险', high: '高风险'})}
      ${stale ? stamp('hold', {'hold': '该分已失效'}) : ''}</h3>
    <div class="probability">${percent(p)}<small>模型风险概率</small></div>
    <div class="risk-bar" role="img" aria-label="风险 ${percent(p)}，阈值 ${percent(threshold)}"><i style="width:${p * 100}%"></i>
      <b style="left:${threshold * 100}%"></b></div><div class="risk-labels"><span>0%</span><span>复核阈值 ${percent(threshold)}</span><span>100%</span></div>
    <p>${result.risk === 'high' ? '已生成开箱复核单。' : '风险环节已放行。派车仍需确认封签与窗口。'}</p></section>
    <section class="feature-sheet"><h3>包装输入快照</h3><dl class="facts">
      <dt>器物材质</dt><dd>${e(materials[snapshot.material])}</dd><dt>包装结构</dt><dd>${e(packings[snapshot.packing])}</dd>
      <dt>路线类别</dt><dd>${e(routes[snapshot.route_type])}</dd><dt>最高相对湿度</dt><dd>${snapshot.max_rh}% RH</dd>
      <dt>转运次数</dt><dd>${snapshot.transfers} 次</dd></dl></section></div>
    <section class="evidence"><div><h3>模型留档</h3>
      <p>${e(evidence.model_version ?? 'legacy')} · ${dateTime(result.computed_at)} ·
        ${e(evidence.training_source === 'inspection_findings' ? '实查样本训练' : '合成样本')}</p>
      <dl class="facts"><dt>训练 / 留出</dt><dd>${evidence.train_count ?? '—'} / ${evidence.test_count ?? '—'}</dd>
        <dt>ROC AUC</dt><dd>${evidence.roc_auc?.toFixed?.(3) ?? '—'}</dd>
        <dt>Brier 分数</dt><dd>${evidence.brier_score?.toFixed?.(3) ?? '—'}</dd>
        <dt>评分状态</dt><dd>${result.score_state === 'frozen' ? '已派车冻结，保留原分' : result.score_state === 'invalidated' ? '已失效，等待重算' : '当前有效'}</dd></dl></div>
      <details><summary>查看编码与输入指纹</summary><dl class="facts">${Object.entries(evidence.encoded_features ?? {}).map(([label, value]) =>
        `<dt>${e(label)}</dt><dd>${Number(value).toFixed(4)}</dd>`).join('')}</dl>
        <p class="fingerprint mono">${e(evidence.input_sha256 ?? '')}</p></details></section>`;
}

function scoreHistory(record) {
  const history = record.score_history;
  if (!history?.length) return '';
  return `<section class="score-history"><h3>评分留痕（旧分失效与冻结记录）</h3><ol>
    ${history.map((item) => `<li><strong>${e(item.version_label)}</strong>
      ${stamp(item.score_state, {current: '当前有效', invalidated: '已失效', frozen: '派车冻结'})}
      <small>${percent(item.probability)} · ${e(item.risk === 'high' ? '高风险' : '低风险')} · ${dateTime(item.computed_at)}</small>
      ${item.invalidated_reason ? `<p class="muted">失效原因：${e(retiredReasons[item.invalidated_reason] ?? item.invalidated_reason)}</p>` : ''}</li>`).join('')}
  </ol></section>`;
}

export function triagePage({loans, record, staff, modelBoard, jobs}) {
  const result = record?.triage;
  const busy = jobs?.some((job) => ['queued', 'running'].includes(job.status));
  return `${heading('风险分流', '实查标签训练、版本治理与包装快照判定')}
    <div class="workbench">${fileIndex(loans, record, 'triage')}${record ? `<article class="dossier">${recordHeader(record)}
      ${record.invalidated_scores ? `<p class="alert">该批次有 ${record.invalidated_scores} 笔旧分已随版本停用失效，正按当前版本重算；晚到的旧分不会放行。</p>` : ''}
      ${result?.status === 'scored' ? riskResult(result) : `<section class="triage-intake"><h3>${result?.status === 'queued' ? '风险分流排队中' : '提交包装记录'}</h3>
        <p>${e(result?.error ?? (result?.status === 'queued' ? '模型完成后，本页会更新判定与复核任务。' : '先完成包装检查，再将材质、包装、路线、湿度与转运次数归档计算。'))}</p>
        ${staff.role === 'operator' && record.crate && result?.status !== 'queued' && !busy
          ? `<form data-action="triage"><button>${result?.status === 'failed' ? '重试分流' : '提交风险分流'}</button></form>` : ''}</section>`}
      <section class="finding-panel"><h3>开箱实查标签</h3>${findingPanel(record, staff)}</section>
      ${scoreHistory(record)}
      <section class="audit-column"><h3>分流留痕</h3>${activity(record.history)}</section>
    </article>` : ''}</div>
    ${versionBoard(modelBoard, staff, jobs)}${jobBoard(jobs)}`;
}
