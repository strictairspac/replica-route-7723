import {table} from '../components/common/table.js';
import {buildBadge} from '../components/common/badge.js';
import {esc, date} from '../utils/html.js';
import {session} from '../hooks/session.js';

const rowsJson = [
  {id: 1, value: 'member1@qiyuan.test'},
  {id: 2, value: 'member2@qiyuan.test'},
  {id: 3, value: 'member3@qiyuan.test'},
  {id: 4, value: 'member4@qiyuan.test'},
  {id: 5, value: 'member5@qiyuan.test'},
  {id: 6, value: 'member6@qiyuan.test'},
];
const nodesJson = [
  {name: 'harbor-primary', role: 'primary', status: 'healthy', position: 105, delay_ms: 0},
  {name: 'north-replica', role: 'replica', status: 'healthy', position: 104, delay_ms: 150},
  {name: 'south-replica', role: 'replica', status: 'healthy', position: 105, delay_ms: 20},
];

export function buildsPage(items) {
  const readOnly = session.user.role === 'viewer' ? 'disabled' : '';
  return `<div class="split"><section>
    <div class="section-top"><h2>索引构建台账</h2><span>${items.length} BUILDS / ONLINE UNIQUE INDEX</span></div>
    ${table(['构建 / 时间', '策略', '扫描进度', '状态 / 代价'], items.map(b => {
      const s = b.summary || {};
      const cov = s.coverage || {};
      const cost = s.cost || {};
      return [
        `<strong>${esc(b.name)}</strong><small>#${b.id} · ${esc(b.table_name)}.${esc(b.index_name)}</small><small>${date(b.created_at)}</small>`,
        `<span class="badge">${b.policy === 'accept' ? '照收冲突' : '立即拦下'}</span>`,
        `${(cov.applied_batches || []).length} / ${cov.total_batches || 0} 批<small>索引条目 ${cov.indexed_entries || 0} / 活跃行 ${cov.active_rows || 0}</small>`,
        `${buildBadge(b.status)}<small>拦下写 ${cost.blocked_writes ?? 0} · 冲突 ${cost.conflicts_detected ?? 0} · 失败校验 ${cost.failed_validations ?? 0}</small>`,
      ];
    }))}
    <div class="protocol"><h3>演练顺序</h3><ol>
      <li>冻结存量行与主/副节点位点，按批次大小切分扫描计划</li>
      <li>扫描批次与业务写入并发；写入同时把新版本装进索引</li>
      <li>同一批次可重试，条目按行号与版本幂等去重</li>
      <li>重复值策略二选一：构建立即拦下，或照收并记录冲突</li>
      <li>finalize 必须扫描全覆盖且没有重复值，索引才能生效</li>
      <li>查询只路由到索引已装好且位点满足会话要求的节点</li>
    </ol></div>
  </section>
  <section class="form-panel"><span class="eyebrow">ONLINE INDEX BUILD</span><h2>新建在线唯一索引演练</h2>
    <form id="build-form">
      <label>演练名称<input name="name" value="online-unique-20261118" required minlength="2"></label>
      <div class="fields">
        <label>表名<input name="table_name" value="members" required></label>
        <label>索引名<input name="index_name" value="uniq_member_email" required></label>
      </div>
      <div class="fields">
        <label>唯一列<input name="key_column" value="email" required></label>
        <label>扫描批次大小<input name="batch_size" type="number" min="1" max="100" value="3" required></label>
      </div>
      <label>重复值策略<select name="policy">
        <option value="block">block · 写入撞值立即拦下</option>
        <option value="accept">accept · 照收，等冲突消除再生效</option>
      </select><small>block 期间写入被拒绝，业务可见错误；accept 期间写入成功但索引保持无效，查询被拒绝，直到重复消除。</small></label>
      <label>节点快照 JSON<textarea name="nodes" rows="5" required>${esc(JSON.stringify(nodesJson, null, 2))}</textarea></label>
      <label>存量行 JSON<textarea name="rows" rows="8" required>${esc(JSON.stringify(rowsJson, null, 2))}</textarea></label>
      <button class="primary" ${readOnly}>冻结快照并开始构建 +</button>
    </form>
    <p class="help">所有位点为命名日志谱系中的单调整数。查询只在已装好索引且位点 ≥ 会话写位点的节点上执行；模型不连接真实集群。</p>
  </section></div>`;
}
