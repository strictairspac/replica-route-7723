import {table} from '../components/common/table.js';
import {buildBadge, badge} from '../components/common/badge.js';
import {esc, date} from '../utils/html.js';
import {session} from '../hooks/session.js';

const KIND_LABEL = {scan: '扫描批次', write: '业务写入', delete: '删除', catch_up: '副本追赶',
  finalize: '校验生效', query: '索引查询', node_down: '节点下线', node_up: '节点恢复'};

function decisionCell(o) {
  const styles = {read: '', not_found: 'rejected', rejected: 'rejected', accepted: '', applied: '', deleted: '', ready: '', failed: 'rejected'};
  const label = {read: '命中', not_found: '未命中', rejected: '拒绝', accepted: '已写入', applied: '已执行',
    deleted: '已删除', ready: '索引生效', failed: '校验失败'}[o.decision] || o.decision;
  return `<span class="decision ${styles[o.decision] || ''}">${label}</span>`;
}

function detailLine(o) {
  const parts = [];
  if (o.position !== undefined) parts.push(`位点 ${o.position}`);
  if (o.row_id !== undefined) parts.push(`行 ${o.row_id}`);
  if (o.version !== undefined) parts.push(`版本 ${o.version}`);
  if (o.node) parts.push(`节点 ${o.node}`);
  if (o.inserted !== undefined) parts.push(`新装 ${o.inserted}`);
  if (o.suppressed !== undefined) parts.push(`重试跳过 ${o.suppressed}`);
  if (o.stale_skipped !== undefined) parts.push(`旧值跳过 ${o.stale_skipped}`);
  if (o.deleted_skipped !== undefined) parts.push(`已删跳过 ${o.deleted_skipped}`);
  if (o.conflict) parts.push(`产生冲突 ${o.conflict_row_ids?.join(',')}`);
  return parts.join(' · ');
}

function commandText(e) {
  const at = e.at_ms == null ? '' : `${e.at_ms}ms `;
  const session = e.session && e.session !== 'default' ? ` @${e.session}` : '';
  switch (e.kind) {
    case 'scan': return `${at}scan 批次${e.batch_no}${session}`;
    case 'write': return `${at}write 行${e.row_id} → ${e.value}${session}`;
    case 'delete': return `${at}delete 行${e.row_id}${session}`;
    case 'catch_up': return `${at}catch_up ${e.node} → 位点${e.position}`;
    case 'finalize': return `${at}finalize`;
    case 'query': return `${at}query ${e.value}${session}`;
    case 'node_down': return `${at}node_down ${e.node}`;
    case 'node_up': return `${at}node_up ${e.node}`;
    default: return `${at}${e.kind}`;
  }
}

function nodePanel(state) {
  return `<div class="topology">${state.nodes.map(n => {
    const stateLabel = n.status !== 'healthy' ? '节点已下线'
      : n.index_state === 'valid' ? '索引可用'
        : n.index_state === 'invalid' ? '索引无效'
          : n.index_state === 'installing' ? '索引安装中' : '索引待生效';
    const stateClass = n.status !== 'healthy' ? 'failed'
      : n.index_state === 'valid' ? 'completed'
        : n.index_state === 'invalid' ? 'failed' : 'running';
    return `
    <div class="node ${n.role}">
      <span class="eyebrow">${n.role === 'primary' ? 'PRIMARY / 主站' : 'REPLICA / 副本'}</span>
      <strong>${esc(n.name)}</strong>
      <div class="position">${n.position}<small>LOG POSITION</small></div>
      <div>${n.delay_ms}ms · ${n.status === 'healthy' ? '在线' : '下线'}</div>
      <div class="badge ${stateClass}">${stateLabel}</div>
      <small>索引位点 ${n.installed_position}</small>
    </div>`;
  }).join('')}</div>`;
}

function conflictPanel(summary) {
  const live = summary.conflicts || [];
  const history = summary.conflict_history || [];
  if (!history.length) return '<div class="notice">尚无重复值冲突。写入一个与存量相同的值即可观察策略代价。</div>';
  return table(['值 / 冲突状态', '涉及行', '发现 / 消除序号', '代价'], history.map(c => {
    const open = c.resolved_seq === null;
    return [
      `<strong>${esc(c.value)}</strong><small>${open ? '冲突未消除，索引不可生效' : '冲突已消除'}</small>`,
      c.row_ids.map(x => `#${x}`).join(' · '),
      `${c.first_seq} / ${c.resolved_seq ?? '—'}`,
      open ? '<span class="rejected">阻塞索引生效，查询被拒</span>' : '已消除',
    ];
  }));
}

function queryPanel(state) {
  if (!state.queries.length) return '<div class="empty">还没有索引查询。先让索引生效，再查询一个值，观察读到的是哪个节点。</div>';
  const rows = state.queries.map(q => [
    `#${q.seq}<small>${esc(q.session)}</small>`,
    esc(q.key_value),
    q.node ? esc(q.node) : '<span class="rejected">无可用节点</span>',
    `${q.required_position} / ${q.node ? (state.nodes.find(n => n.name === q.node)?.installed_position ?? '—') : '—'}`,
    q.decision === 'read'
      ? `命中行 ${q.entry.row_id}<small>索引版本 ${q.entry.version}</small>`
      : q.decision === 'not_found' ? '索引中不存在' : `<span class="rejected">${esc(q.reason)}</span>`,
    `<small>${q.node_checks.map(c => `${c.name}: ${c.eligible ? '合格' : [c.online ? '' : '离线', c.index_installed ? '' : '索引未装', c.position_sufficient ? '' : `位点${c.installed_position}<${q.required_position}`].filter(Boolean).join('/')}`).join('<br>')}</small>`,
  ]);
  return table(['序号 / 会话', '查询值', '路由节点', '会话位点 / 节点索引位点', '结果', '节点资格'], rows);
}

export function buildPage(build, jobs) {
  const s = build.summary || {};
  const st = build.state;
  const readOnly = session.user.role === 'viewer' ? 'disabled' : '';
  const cov = s.coverage || {};
  const cost = s.cost || {};
  const nodeOptions = st.nodes.map(n => `<option value="${esc(n.name)}">${esc(n.name)} · 位点 ${n.position}</option>`).join('');
  const batchButtons = st.scan_plan.map((rows, i) => {
    const done = !!st.batches[String(i)]?.applied;
    return `<button class="small quick-batch" data-batch="${i}" ${readOnly}>${done ? '重试' : '扫描'}批次 ${i}<small>行 ${rows.join(',')}</small></button>`;
  }).join(' ');
  const nextBatch = st.scan_plan.findIndex((_, i) => !st.batches[String(i)]?.applied);
  const jobsTable = table(['任务 / 时间', '状态', '事件数', '校验和', '证据文件'], jobs.map(j => [
    `#${j.id}<small>${date(j.created_at)}</small>`,
    badge(j.status),
    String(j.event_count),
    `<small>期望 ${esc(j.expected_checksum || '—')}<br>实际 ${esc(j.actual_checksum || '—')}</small>`,
    j.artifact ? `<small>${esc(j.artifact)}</small>` : '<small>—</small>',
  ]));
  const statusBanner = s.status === 'ready'
    ? `<div class="verdict"><div class="verdict-icon">✓</div><div><span class="eyebrow">INDEX READY</span><h2>索引已生效，可路由索引查询</h2><p>就绪位点 ${s.ready_position} · 条目 ${s.coverage.indexed_entries} · 索引生效后，写入在主库开始强制唯一</p></div></div>`
    : s.status === 'failed'
      ? `<div class="verdict"><div class="verdict-icon">!</div><div><span class="eyebrow">INDEX FAILED</span><h2>索引无法生效</h2><p>${esc(s.error)}</p></div></div>`
      : `<div class="verdict"><div class="verdict-icon">…</div><div><span class="eyebrow">BUILDING</span><h2>索引构建中</h2><p>已应用 ${(cov.applied_batches || []).length}/${cov.total_batches} 批 · 写入同步双写索引 · 查询暂被拒绝</p></div></div>`;

  return `<div class="evidence-top"><div><span class="eyebrow">BUILD EVIDENCE / #${build.id}</span><h2>${esc(build.name)} ${buildBadge(build.status)}</h2><p>${esc(build.table_name)}.${esc(build.index_name)} · ${esc(build.key_column)} · ${date(build.created_at)} · 快照位点 ${s.snapshot_position}</p></div><div style="display:flex;gap:8px"><button id="verify" ${readOnly}>独立校验</button><button id="export" class="small" data-id="${build.id}">导出证据 JSON ↓</button></div></div>
  ${statusBanner}
  <div class="policy-strip">重复值策略 <strong>${build.policy === 'accept' ? '照收（冲突消除后再生效）' : '立即拦下写入'}</strong><span>／</span>扫描批次 ${st.scan_plan.length}<span>／</span>主库位点 ${st.nodes.find(n => n.role === 'primary').position}<span>／</span>就绪位点 ${s.ready_position ?? '—'}</div>
  ${nodePanel(st)}
  <div class="split">
    <section>
      <div class="section-top"><h2>构建时间线</h2><span>全部事件均可回退重放</span></div>
      ${table(['序号 / 事件', '指令', '结果', '细节'], build.trace.map(t => [
        `#${t.seq}<small>${KIND_LABEL[t.event.kind] || t.event.kind}</small>`,
        `<span class="command">${esc(commandText(t.event))}</span>`,
        decisionCell(t.outcome),
        `${esc(t.outcome.reason)}<small>${detailLine(t.outcome)}</small>`,
      ]))}
      <div class="section-top"><h2>重复值冲突</h2><span>DUPLICATE KEY STATE</span></div>
      ${conflictPanel(s)}
      <div class="section-top"><h2>索引查询读到了什么</h2><span>INDEX-QUALIFIED ROUTING</span></div>
      ${queryPanel(st)}
      <div class="section-top"><h2>独立校验任务</h2><span>WORKER REPLAY</span></div>
      ${jobsTable}
    </section>
    <section class="form-panel"><span class="eyebrow">BUILD CONTROLS</span><h2>安排扫描与写入</h2>
      <h3>扫描批次</h3>
      <p class="help">批次计划：${st.scan_plan.map((rows, i) => `[${i}]→行${rows.join(',')}`).join(' &nbsp; ')}</p>
      <div style="display:flex;gap:6px;flex-wrap:wrap">${batchButtons}</div>
      <label>指定批次号<input id="batch-no" type="number" min="0" value="${nextBatch === -1 ? 0 : nextBatch}"></label>
      <button class="small" id="add-batch" ${readOnly}>执行该批次（可重复以演练重试）</button>
      <h3 style="margin-top:24px">业务写入</h3>
      <label>用户行号<input id="write-row" type="number" min="1" value="7"></label>
      <label>唯一键新值<input id="write-value" value="member7@qiyuan.test"></label>
      <button class="small" id="add-write" ${readOnly}>写入并双写索引</button>
      <h3 style="margin-top:24px">删除与冲突消除</h3>
      <label>删除行号<input id="delete-row" type="number" min="1" value="7"></label>
      <button class="small" id="add-delete" ${readOnly}>删除行</button>
      <h3 style="margin-top:24px">校验生效</h3>
      <button class="small" id="add-finalize" ${readOnly}>finalize：尝试让索引生效</button>
      <h3 style="margin-top:24px">索引查询</h3>
      <label>查询唯一键<input id="query-value" value="member1@qiyuan.test"></label>
      <button class="small" id="add-query" ${readOnly}>通过索引查询</button>
      <p class="help">${s.status === 'ready' ? '索引已生效，查询会选延迟最低且索引位点满足会话要求的节点。' : '索引未生效时，查询会被拒绝——这是正确结果。'}</p>
      <h3 style="margin-top:24px">副本追赶</h3>
      <label>节点<select id="catchup-node">${nodeOptions}</select></label>
      <label>追赶到位点<input id="catchup-position" type="number" min="0" value="${st.nodes.find(n => n.role === 'primary').position}"></label>
      <button class="small" id="add-catchup" ${readOnly}>应用日志到该位点</button>
      <h3 style="margin-top:24px">节点可用性</h3>
      <label>目标节点<select id="node-target">${nodeOptions}</select></label>
      <div style="display:flex;gap:6px"><button class="small" id="add-node-down" ${readOnly}>下线</button><button class="small" id="add-node-up" ${readOnly}>恢复</button></div>
      <h3 style="margin-top:24px">回退重放</h3>
      <label>保留前 N 条事件<input id="rewind-keep" type="number" min="0" value="${build.trace.length}"></label>
      <button class="small" id="rewind" ${readOnly}>回退并重放</button>
    </section>
  </div>
  <div class="evidence-notes"><section><h3>旧扫描不会盖掉新值</h3><p>扫描读到的是构建开始时的快照值。若某行在扫描期间被写入，索引条目已有更高版本，旧批次落到该行时会被标记为“旧值跳过”，绝不会回写更低版本。</p></section>
  <section><h3>同批重试不重复入索引</h3><p>条目以行号为键。重复执行同一批次时，已存在且版本一致的条目计为“重试跳过”，计数可见但索引大小不变。</p></section>
  <section><h3>确定性摘要</h3><code>${esc(s.checksum || '')}</code><p>相同快照与事件序列产生相同摘要；独立 worker 会用同样输入重放并比对。</p></section></div>`;
}
