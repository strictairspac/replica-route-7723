import {api, download} from './api/client.js';
import {session} from './hooks/session.js';
import {feedback} from './hooks/feedback.js';
import {esc, date} from './utils/html.js';
import {frame} from './components/common/frame.js';
import {table} from './components/common/table.js';
import {loginPage} from './pages/login.js';
import {buildsPage} from './pages/builds.js';
import {buildPage} from './pages/build.js';
import {titles} from './router/routes.js';

let timer = null;
let pollCount = 0;
const app = document.querySelector('#app');

async function bindCreate() {
  document.querySelector('#build-form')?.addEventListener('submit', async e => {
    e.preventDefault();
    const button = e.target.querySelector('button');
    button.disabled = true;
    try {
      const raw = new FormData(e.target);
      const nodes = JSON.parse(raw.get('nodes'));
      const rows = JSON.parse(raw.get('rows'));
      const data = {
        name: raw.get('name'),
        table_name: raw.get('table_name'),
        index_name: raw.get('index_name'),
        key_column: raw.get('key_column'),
        policy: raw.get('policy'),
        batch_size: Number(raw.get('batch_size')),
        nodes,
        rows,
      };
      const created = await api('/builds', 'POST', data);
      feedback('构建已创建，开始编排扫描与写入。');
      location.hash = 'build/' + created.id;
    } catch (err) {
      feedback(err.message.includes('JSON') ? '节点或存量数据 JSON 格式错误' : err.message, true);
    } finally {
      button.disabled = false;
    }
  });
}

async function bindCommands(id) {
  const post = async (path, body, message) => {
    let result;
    try {
      result = await api(path, 'POST', body);
    } catch (err) {
      await render();
      feedback(err.message, true);
      return null;
    }
    await render();
    feedback(message || '指令已执行。');
    return result;
  };
  document.querySelector('#add-batch')?.addEventListener('click', async () => {
    const result = await post(`/builds/${id}/events`, {events: [{kind: 'scan', batch_no: Number(document.querySelector('#batch-no').value)}]}, '扫描批次已执行。');
    if (result) document.querySelector('#batch-no').value = String(Number(document.querySelector('#batch-no').value) + 1);
  });
  document.querySelector('#add-write')?.addEventListener('click', () => {
    const row = Number(document.querySelector('#write-row').value);
    const value = document.querySelector('#write-value').value;
    post(`/builds/${id}/events`, {events: [{kind: 'write', row_id: row, value, session: 'lab-A'}]}, '写入指令已执行。');
  });
  document.querySelector('#add-delete')?.addEventListener('click', () => {
    const row = Number(document.querySelector('#delete-row').value);
    post(`/builds/${id}/events`, {events: [{kind: 'delete', row_id: row, session: 'lab-A'}]}, '删除指令已执行。');
  });
  document.querySelector('#add-finalize')?.addEventListener('click', () =>
    post(`/builds/${id}/events`, {events: [{kind: 'finalize'}]}, '校验指令已执行。'));
  document.querySelector('#add-query')?.addEventListener('click', () => {
    const value = document.querySelector('#query-value').value;
    post(`/builds/${id}/events`, {events: [{kind: 'query', value, session: 'lab-A'}]}, '索引查询已执行。');
  });
  document.querySelector('#add-catchup')?.addEventListener('click', () => {
    const node = document.querySelector('#catchup-node').value;
    const position = Number(document.querySelector('#catchup-position').value);
    post(`/builds/${id}/events`, {events: [{kind: 'catch_up', node, position, session: 'ddl'}]}, '副本追赶指令已执行。');
  });
  document.querySelector('#add-node-down')?.addEventListener('click', () => {
    const node = document.querySelector('#node-target').value;
    post(`/builds/${id}/events`, {events: [{kind: 'node_down', node}]}, '节点已下线。');
  });
  document.querySelector('#add-node-up')?.addEventListener('click', () => {
    const node = document.querySelector('#node-target').value;
    post(`/builds/${id}/events`, {events: [{kind: 'node_up', node}]}, '节点已恢复。');
  });
  document.querySelector('#rewind')?.addEventListener('click', () => {
    const keep = Number(document.querySelector('#rewind-keep').value);
    post(`/builds/${id}/rewind`, {keep}, '已回退并重放。');
  });
  document.querySelector('#verify')?.addEventListener('click', () =>
    post(`/builds/${id}/verify`, {}, '独立校验任务已入队。'));
  document.querySelector('#export')?.addEventListener('click', async () => {
    try { await download(id); feedback('构建证据 JSON 已下载'); }
    catch (err) { feedback(err.message, true); }
  });
  document.querySelectorAll('.quick-batch').forEach(b => b.addEventListener('click', () =>
    post(`/builds/${id}/events`, {events: [{kind: 'scan', batch_no: Number(b.dataset.batch)}]}, '扫描批次已执行。')));
}

const TRANSIENT_FIELDS = new Set(['batch-no', 'catchup-position', 'rewind-keep']);

function snapshotInputs() {
  const values = {};
  document.querySelectorAll('.form-panel input[id], .form-panel select[id]').forEach(el => {
    if (!TRANSIENT_FIELDS.has(el.id)) values[el.id] = el.value;
  });
  return values;
}

function restoreInputs(values) {
  for (const [id, value] of Object.entries(values)) {
    const el = document.getElementById(id);
    if (el && value !== '') el.value = value;
  }
}

async function render() {
  clearTimeout(timer);
  const typed = snapshotInputs();
  if (!session.token) {
    app.innerHTML = loginPage();
    document.querySelector('#login')?.addEventListener('submit', async e => {
      e.preventDefault();
      const raw = Object.fromEntries(new FormData(e.target));
      try {
        const result = await api('/auth/login', 'POST', raw);
        session.save(result);
        location.hash = 'builds';
        await render();
      } catch (err) { feedback(err.message, true); }
    });
    return;
  }
  try {
    await api('/auth/me');
    const [page = 'builds', id] = location.hash.slice(1).split('/');    let content, title, subtitle, poll = false, bindPage = null;
    if (page === 'audit') {
      const rows = await api('/audit');
      [title, subtitle] = titles.audit;
      content = table(['时间', '操作人', '动作'], rows.map(x => [date(x.created_at), esc(x.actor), esc(x.action)]));
      bindPage = async () => {};
    } else if (page === 'build' && id) {
      const build = await api('/builds/' + id);
      const jobs = await api('/jobs?build_id=' + id);
      [title, subtitle] = titles.build;
      content = buildPage(build, jobs);
      bindPage = () => bindCommands(Number(id));
      poll = jobs.some(j => ['queued', 'running'].includes(j.status));
    } else {
      const builds = await api('/builds');
      [title, subtitle] = titles[page] || titles.builds;
      content = buildsPage(builds);
      bindPage = bindCreate;
    }
    app.innerHTML = frame(page === 'build' ? 'builds' : page, title, subtitle, content);
    await bindPage();
    restoreInputs(typed);
    document.querySelector('#logout').onclick = () => { session.clear(); render(); };
    if (poll && pollCount < 8) {
      pollCount += 1;
      timer = setTimeout(render, 1500);
    } else {
      pollCount = 0;
    }
  } catch (err) {
    if (err.message.includes('登录') || err.message.includes('Not authenticated') || err.message.includes('401')) {
      session.clear();
      await render();
    } else {
      if (!document.querySelector('#feedback')) app.innerHTML = '<main><div id="feedback"></div><button onclick="location.reload()">重新加载</button></main>';
      feedback(err.message, true);
    }
  }
}

window.addEventListener('hashchange', render);
render();
