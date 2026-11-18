import {session} from '../../hooks/session.js';
import {ROLES} from '../../constants/enums.js';
import {esc} from '../../utils/html.js';

export function frame(page, title, subtitle, content) {
  const nav = [['builds', '01', '索引构建台账'], ['audit', '02', '审计日志']];
  return `<aside><div class="brand"><span class="brand-symbol">QY</span><div>栖原站点<small>ONLINE INDEX CHART</small></div></div>
  <div class="nav-label">值班航图 / WATCH 018</div>
  <nav>${nav.filter(([key]) => key !== 'audit' || session.user?.role === 'admin').map(([key, n, label]) =>
    `<a class="${page === key ? 'active' : ''}" href="#${key}"><b>${n}</b>${label}</a>`).join('')}</nav>
  <div class="aside-foot">在线加唯一索引<br><small>扫描 / 并发写入 / 重复值 / 会话位点<br>受控模型，非真实 DDL 集群</small></div></aside>
  <main><header><div>QIYUAN STATIONS / ONLINE UNIQUE INDEX</div><div>${esc(session.user?.email)} · ${ROLES[session.user?.role]} <button id="logout" class="link">退出</button></div></header>
  <section class="heading"><div><span class="eyebrow">交接班航图 · 2026 / 11 / 18</span><h1>${title}</h1><p>${subtitle}</p></div><div class="stamp">栖原<small>UNIQUE<br>KEYS</small></div></section>
  <div id="feedback" class="feedback" role="status"></div>${content}
  <footer>栖原站点运维组 · 11 人协作 · 每次索引读取都有位点依据<span>CONTROLLED ONLINE INDEX MODEL / v2.0</span></footer></main>`;
}
