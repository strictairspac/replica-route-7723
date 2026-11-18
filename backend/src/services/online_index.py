"""Deterministic online unique-index build model.

The model replays a fixed event sequence against an initial snapshot of the
table and replica positions.  It is intentionally small, but every rule has a
direct counterpart in an online DDL deployment:

* a scan batch reads the *snapshot* value of each row (frozen at build start);
* concurrent writes are dual-written into the index with the newer log version;
* an index entry is only installed when the scan version is not older than the
  entry already present, so a stale scan can never overwrite a newer value;
* retrying a batch is idempotent: the entry is keyed by row id, and an equal
  version/source is reported as suppressed instead of inserted twice;
* duplicate key values either block the write immediately or are accepted and
  recorded as a conflict, which keeps the index invalid until resolved;
* finalize validates full coverage and uniqueness; duplicates mean the index
  cannot become valid;
* queries may only use nodes whose index copy is valid and whose applied
  position satisfies the session's required position.

All state is JSON-serialisable so the latest state and every event outcome can
be persisted as evidence.
"""

from __future__ import annotations

import copy
import hashlib
import json

POLICIES = ('block', 'accept')


def _key(row_id):
    return str(int(row_id))


def make_initial_state(name, table_name, index_name, key_column, policy, batch_size, nodes, rows):
    if policy not in POLICIES:
        raise ValueError('重复值策略只能是 block 或 accept')
    if not 1 <= int(batch_size) <= 100:
        raise ValueError('扫描批次大小需在 1–100 之间')
    if len(nodes) < 1:
        raise ValueError('至少需要一个集群节点')
    primaries = [n for n in nodes if n['role'] == 'primary']
    if len(primaries) != 1:
        raise ValueError('必须且只能有一个主节点')
    ids = set()
    clean_rows = {}
    for item in rows:
        rid = int(item['id'])
        if rid in ids:
            raise ValueError(f'行号重复：{rid}')
        ids.add(rid)
        value = str(item['value']).strip()
        if not value:
            raise ValueError(f'行 {rid} 的唯一键值不能为空')
        clean_rows[_key(rid)] = {
            'id': rid,
            'value': value,
            'version': int(primaries[0]['position']),
            'deleted': False,
            'updated_seq': 0,
        }
    snapshot_position = int(primaries[0]['position'])
    ordered_ids = sorted(int(k) for k in clean_rows)
    plan = [ordered_ids[i:i + int(batch_size)] for i in range(0, len(ordered_ids), int(batch_size))]
    clean_nodes = copy.deepcopy(nodes)
    for node in clean_nodes:
        node['index_state'] = 'building'
        node['installed_position'] = 0
    return {
        'name': name,
        'table_name': table_name,
        'index_name': index_name,
        'key_column': key_column,
        'policy': policy,
        'batch_size': int(batch_size),
        'snapshot_position': snapshot_position,
        'status': 'building',
        'ready_position': None,
        'error': None,
        'nodes': clean_nodes,
        'rows': clean_rows,
        'snapshot_rows': {k: {'value': v['value'], 'version': v['version']} for k, v in clean_rows.items()},
        'history': {k: [{'version': v['version'], 'value': v['value']}] for k, v in clean_rows.items()},
        'scan_plan': plan,
        'entries': {},
        'batches': {},
        'sessions': {},
        'conflicts': {},
        'queries': [],
        'counters': {
            'scan_inserted': 0,
            'scan_suppressed': 0,
            'scan_stale_skipped': 0,
            'scan_deleted_skipped': 0,
            'writes_accepted': 0,
            'writes_rejected': 0,
            'accepted_conflicting_writes': 0,
            'deletes': 0,
            'conflicts_detected': 0,
            'conflicts_resolved': 0,
            'finalize_attempts': 0,
            'finalize_failures': 0,
            'queries_routed': 0,
            'queries_not_found': 0,
            'queries_rejected': 0,
            'catch_ups': 0,
        },
        'last_seq': 0,
    }


def primary_of(state):
    return next(n for n in state['nodes'] if n['role'] == 'primary')


def _active_rows(state):
    return {k: r for k, r in state['rows'].items() if not r['deleted']}


def _duplicate_groups(state):
    groups = {}
    for rid, row in _active_rows(state).items():
        groups.setdefault(row['value'], []).append(int(rid))
    return {v: sorted(ids) for v, ids in groups.items() if len(ids) > 1}


def _refresh_conflicts(state, seq):
    """Derive active duplicate groups from live rows and keep conflict history."""
    live = _duplicate_groups(state)
    for value, group in live.items():
        record = state['conflicts'].get(value)
        if not record:
            record = {
                'value': value,
                'row_ids': group,
                'first_seq': seq,
                'last_seq': seq,
                'resolved_seq': None,
            }
            state['conflicts'][value] = record
            state['counters']['conflicts_detected'] += 1
        else:
            record['row_ids'] = group
            record['last_seq'] = seq
            record['resolved_seq'] = None
    for value, record in state['conflicts'].items():
        if value not in live and record['resolved_seq'] is None:
            record['resolved_seq'] = seq
            state['counters']['conflicts_resolved'] += 1
    # Entry state mirrors the currently active duplicate groups.
    for rid, entry in state['entries'].items():
        row = state['rows'].get(rid)
        if row is None or row['deleted']:
            continue
        entry['state'] = 'conflict' if entry['key_value'] in live else 'installed'
    return live


def _entry_for(state, row_id, value, version, source, seq, batch_no=None):
    state['entries'][_key(row_id)] = {
        'row_id': int(row_id),
        'key_value': value,
        'version': int(version),
        'source': source,
        'batch_no': batch_no,
        'seq': seq,
        'state': 'installed',
    }


def _apply_scan(state, event, seq):
    batch_no = event.get('batch_no')
    if batch_no is None:
        raise ValueError('扫描事件需要 batch_no')
    batch_no = int(batch_no)
    plan = state['scan_plan']
    if not 0 <= batch_no < len(plan):
        raise ValueError(f'扫描批次 {batch_no} 不存在，本构建共 {len(plan)} 批')
    record = state['batches'].setdefault(str(batch_no), {
        'batch_no': batch_no,
        'row_ids': plan[batch_no],
        'attempts': 0,
        'applied': False,
        'inserted': 0,
        'suppressed': 0,
        'stale': 0,
        'deleted': 0,
        'first_seq': seq,
    })
    record['attempts'] += 1
    inserted = suppressed = stale = deleted = 0
    reasons = []
    for rid in plan[batch_no]:
        key = _key(rid)
        snapshot = state['snapshot_rows'][key]
        row = state['rows'].get(key)
        if row is None or row['deleted']:
            deleted += 1
            continue
        existing = state['entries'].get(key)
        if existing and existing['version'] > snapshot['version']:
            # A concurrent write installed a newer value; the snapshot scan
            # must not roll it back.
            stale += 1
            reasons.append({'row_id': rid, 'reason': '索引已有更新版本，保留新值',
                            'existing_version': existing['version'], 'scan_version': snapshot['version']})
            continue
        if (existing and existing['version'] == snapshot['version']
                and existing['source'] == 'scan' and existing.get('batch_no') == batch_no):
            # Retry of the same batch: the entry already exists, so nothing is
            # inserted twice.  This is the idempotency proof.
            suppressed += 1
            reasons.append({'row_id': rid, 'reason': '同批重试，条目已存在，跳过'})
            continue
        if existing and existing['version'] == snapshot['version'] and existing['key_value'] == snapshot['value']:
            suppressed += 1
            reasons.append({'row_id': rid, 'reason': '条目已存在且版本一致，跳过'})
            continue
        _entry_for(state, rid, snapshot['value'], snapshot['version'], 'scan', seq, batch_no)
        inserted += 1
    record['applied'] = True
    record['inserted'] += inserted
    record['suppressed'] += suppressed
    record['stale'] += stale
    record['deleted'] += deleted
    record['last_seq'] = seq
    state['counters']['scan_inserted'] += inserted
    state['counters']['scan_suppressed'] += suppressed
    state['counters']['scan_stale_skipped'] += stale
    state['counters']['scan_deleted_skipped'] += deleted
    live = _refresh_conflicts(state, seq)
    outcome = {
        'decision': 'applied',
        'reason': f'批次 {batch_no} 第 {record["attempts"]} 次执行',
        'inserted': inserted,
        'suppressed': suppressed,
        'stale_skipped': stale,
        'deleted_skipped': deleted,
        'details': reasons[:20],
        'batch_attempts': record['attempts'],
        'conflicts': {v: g for v, g in live.items()},
    }
    return outcome


def _write(state, event, seq):
    primary = primary_of(state)
    rid = event.get('row_id')
    value = event.get('value')
    session = event.get('session') or 'default'
    if rid is None or value is None or str(value).strip() == '':
        raise ValueError('写入事件需要 row_id 和非空 value')
    rid = int(rid)
    value = str(value).strip()
    if primary['status'] != 'healthy':
        state['counters']['writes_rejected'] += 1
        return {'decision': 'rejected', 'reason': '主库不可用，写入被拒绝'}
    key = _key(rid)
    current = state['rows'].get(key)
    duplicate_ids = sorted(int(k) for k, r in _active_rows(state).items()
                           if r['value'] == value and int(k) != rid)
    index_ready = state['status'] == 'ready'
    enforced = index_ready or state['policy'] == 'block'
    if duplicate_ids and enforced:
        state['counters']['writes_rejected'] += 1
        mode = '唯一索引已生效' if index_ready else '拦下策略：构建期即拦截重复值'
        return {
            'decision': 'rejected',
            'reason': f'{mode}，行 {rid} 的值与行 {duplicate_ids} 冲突',
            'duplicate_row_ids': duplicate_ids,
            'position_advanced': False,
        }
    position = int(primary['position']) + 1
    primary['position'] = position
    if current is None or current['deleted']:
        state['rows'][key] = {
            'id': rid,
            'value': value,
            'version': position,
            'deleted': False,
            'updated_seq': seq,
        }
    else:
        current['value'] = value
        current['version'] = position
        current['updated_seq'] = seq
    state['history'].setdefault(key, []).append({'version': position, 'value': value,
                                                 'deleted': False})
    state['sessions'][session] = {'required_position': position}
    _entry_for(state, rid, value, position, 'write', seq)
    live = _refresh_conflicts(state, seq)
    state['counters']['writes_accepted'] += 1
    if index_ready:
        primary['index_state'] = 'valid'
        primary['installed_position'] = position
    conflict = value in live
    if conflict:
        state['counters']['accepted_conflicting_writes'] += 1
    return {
        'decision': 'accepted',
        'reason': '写入成功并双写索引条目',
        'position': position,
        'session_required_position': position,
        'new_value': value,
        'conflict': conflict,
        'conflict_row_ids': live.get(value),
        'index_enforced': index_ready,
    }


def _delete(state, event, seq):
    primary = primary_of(state)
    rid = event.get('row_id')
    session = event.get('session')
    if rid is None:
        raise ValueError('删除事件需要 row_id')
    rid = int(rid)
    if primary['status'] != 'healthy':
        return {'decision': 'rejected', 'reason': '主库不可用，删除被拒绝'}
    key = _key(rid)
    row = state['rows'].get(key)
    if row is None or row['deleted']:
        return {'decision': 'rejected', 'reason': '行不存在或已删除'}
    position = int(primary['position']) + 1
    primary['position'] = position
    row['deleted'] = True
    row['version'] = position
    row['updated_seq'] = seq
    state['history'].setdefault(key, []).append({'version': position, 'value': row['value'],
                                                 'deleted': True})
    state['entries'].pop(key, None)
    if session:
        state['sessions'][session] = {'required_position': position}
    if state['status'] == 'ready':
        primary['index_state'] = 'valid'
        primary['installed_position'] = position
    live = _refresh_conflicts(state, seq)
    state['counters']['deletes'] += 1
    return {
        'decision': 'deleted',
        'reason': '删除行并移除索引条目',
        'position': position,
        'conflicts_remaining': live,
    }


def _catch_up(state, event, seq):
    node_name = event.get('node')
    position = event.get('position')
    if not node_name or position is None:
        raise ValueError('副本追赶需要 node 和 position')
    node = next((n for n in state['nodes'] if n['name'] == node_name), None)
    if not node:
        raise ValueError(f'未知节点：{node_name}')
    if node['role'] != 'replica':
        raise ValueError('只有副本可以执行追赶')
    if node['status'] != 'healthy':
        return {'decision': 'rejected', 'reason': '副本不在线，不能追赶'}
    position = int(position)
    primary = primary_of(state)
    if position < int(node['position']):
        raise ValueError('副本位点不能回退')
    if position > int(primary['position']):
        raise ValueError('追赶位点不能超过主库已提交位点')
    node['position'] = position
    state['counters']['catch_ups'] += 1
    if state['status'] == 'ready':
        ready_position = int(state['ready_position'])
        if position >= ready_position:
            node['index_state'] = 'valid'
            node['installed_position'] = position
            note = '副本已应用至索引就绪点，索引可查询'
        else:
            node['index_state'] = 'installing'
            node['installed_position'] = 0
            note = '副本位点尚未达到索引就绪点，索引仍在安装'
    else:
        node['index_state'] = 'pending'
        node['installed_position'] = 0
        note = '索引尚未就绪，副本只应用日志'
    return {
        'decision': 'applied',
        'reason': note,
        'node': node_name,
        'position': position,
        'index_state': node['index_state'],
        'installed_position': node['installed_position'],
    }


def _finalize(state, event, seq):
    state['counters']['finalize_attempts'] += 1
    applied = {int(k) for k, b in state['batches'].items() if b['applied']}
    missing = [i for i in range(len(state['scan_plan'])) if i not in applied]
    if missing:
        return {
            'decision': 'rejected',
            'reason': f'扫描未覆盖全部批次，缺少批次 {missing}，索引保持构建中',
            'coverage': {'total_batches': len(state['scan_plan']), 'applied_batches': sorted(applied)},
            'status_after': state['status'],
        }
    missing_entries = []
    for key, row in _active_rows(state).items():
        entry = state['entries'].get(key)
        if not entry or entry['version'] < row['version']:
            missing_entries.append(int(key))
    if missing_entries:
        state['status'] = 'failed'
        state['error'] = f'索引条目不完整，缺少行 {sorted(missing_entries)}'
        state['counters']['finalize_failures'] += 1
        return {'decision': 'failed', 'reason': state['error'], 'status_after': 'failed'}
    live = _refresh_conflicts(state, seq)
    if live:
        state['status'] = 'failed'
        state['error'] = '存在重复索引值：' + '；'.join(
            f'{value} → 行 {ids}' for value, ids in sorted(live.items()))
        state['counters']['finalize_failures'] += 1
        for node in state['nodes']:
            node['index_state'] = 'invalid'
            node['installed_position'] = 0
        return {
            'decision': 'failed',
            'reason': state['error'],
            'duplicates': live,
            'status_after': 'failed',
            'queries_blocked': True,
        }
    state['status'] = 'ready'
    state['error'] = None
    state['ready_position'] = int(primary_of(state)['position'])
    primary = primary_of(state)
    primary['index_state'] = 'valid'
    primary['installed_position'] = state['ready_position']
    for node in state['nodes']:
        if node['role'] == 'replica':
            node['index_state'] = 'installing'
            node['installed_position'] = 0
    return {
        'decision': 'ready',
        'reason': '扫描覆盖完整且无重复值，索引在主库生效',
        'ready_position': state['ready_position'],
        'entries': len(state['entries']),
        'replica_install_required': True,
    }


def _query(state, event, seq):
    key_value = event.get('value')
    session = event.get('session') or 'default'
    if key_value is None or str(key_value).strip() == '':
        raise ValueError('查询需要 value')
    key_value = str(key_value).strip()
    required = int(state['sessions'].get(session, {}).get('required_position', 0))
    checks = [{
        'name': n['name'],
        'role': n['role'],
        'online': n['status'] == 'healthy',
        'index_state': n['index_state'],
        'index_installed': n['index_state'] == 'valid',
        'installed_position': n['installed_position'],
        'position_sufficient': int(n['installed_position']) >= required,
        'eligible': (n['status'] == 'healthy' and n['index_state'] == 'valid'
                     and int(n['installed_position']) >= required),
    } for n in state['nodes']]
    if state['status'] != 'ready':
        state['counters']['queries_rejected'] += 1
        result = {
            'decision': 'rejected',
            'reason': f'索引状态为 {state["status"]}，没有已验证的索引副本可用',
            'session': session,
            'key_value': key_value,
            'required_position': required,
            'candidates': [],
            'node_checks': checks,
        }
        state['queries'].append({'seq': seq, **result})
        return result
    candidates = []
    for node in state['nodes']:
        if node['status'] != 'healthy':
            continue
        if node['index_state'] != 'valid':
            continue
        if int(node['installed_position']) < required:
            continue
        candidates.append(node)
    candidates.sort(key=lambda n: (0 if n['role'] == 'replica' else 1,
                                   int(n['delay_ms']), -int(n['position']), n['name']))
    if not candidates:
        state['counters']['queries_rejected'] += 1
        result = {
            'decision': 'rejected',
            'reason': '没有已装好索引且位点满足会话要求的节点，拒绝索引查询',
            'session': session,
            'key_value': key_value,
            'required_position': required,
            'candidates': [],
            'node_checks': checks,
        }
        state['queries'].append({'seq': seq, **result})
        return result
    selected = candidates[0]
    matches = []
    for key, versions in state['history'].items():
        visible = [h for h in versions if h['version'] <= int(selected['installed_position'])]
        if not visible:
            continue
        latest = visible[-1]
        if latest.get('deleted'):
            continue
        if latest['value'] == key_value:
            matches.append({'row_id': int(key), 'key_value': latest['value'],
                            'version': latest['version']})
    matches.sort(key=lambda e: e['version'], reverse=True)
    state['queries'].append({
        'seq': seq,
        'decision': 'read' if matches else 'not_found',
        'reason': '命中已安装索引' if matches else '索引中不存在该值',
        'session': session,
        'key_value': key_value,
        'required_position': required,
        'node': selected['name'],
        'candidates': [n['name'] for n in candidates],
        'node_checks': checks,
        'entry': None if not matches else {
            'row_id': matches[0]['row_id'],
            'key_value': matches[0]['key_value'],
            'version': matches[0]['version'],
        },
    })
    if not matches:
        state['counters']['queries_not_found'] += 1
        return {
            'decision': 'not_found',
            'reason': '索引中不存在该值',
            'node': selected['name'],
            'session': session,
            'key_value': key_value,
            'required_position': required,
            'candidates': [n['name'] for n in candidates],
            'node_checks': checks,
        }
    state['counters']['queries_routed'] += 1
    entry = matches[0]
    return {
        'decision': 'read',
        'reason': f'命中 {selected["name"]} 的已安装索引',
        'node': selected['name'],
        'session': session,
        'key_value': key_value,
        'required_position': required,
        'row_id': entry['row_id'],
        'version': entry['version'],
        'entry': entry,
        'candidates': [n['name'] for n in candidates],
    }


def _node_status(state, event, seq):
    node_name = event.get('node')
    if not node_name:
        raise ValueError('节点事件需要 node')
    node = next((n for n in state['nodes'] if n['name'] == node_name), None)
    if not node:
        raise ValueError(f'未知节点：{node_name}')
    healthy = event['kind'] == 'node_up'
    node['status'] = 'healthy' if healthy else 'down'
    # The installed index copy stays on disk while a node is down; routing
    # eligibility is decided by the node's status at query time.
    return {
        'decision': 'applied',
        'reason': f'{node_name} 已{"恢复" if healthy else "下线"}',
        'node': node_name,
        'status': node['status'],
    }


def apply_event(state, event, seq):
    """Apply one event in place; returns a JSON outcome or raises ValueError."""
    kind = event.get('kind')
    state['last_seq'] = seq
    if kind == 'scan':
        return _apply_scan(state, event, seq)
    if kind == 'write':
        return _write(state, event, seq)
    if kind == 'delete':
        return _delete(state, event, seq)
    if kind == 'catch_up':
        return _catch_up(state, event, seq)
    if kind == 'finalize':
        return _finalize(state, event, seq)
    if kind == 'query':
        return _query(state, event, seq)
    if kind in ('node_down', 'node_up'):
        return _node_status(state, event, seq)
    raise ValueError(f'未知事件类型：{kind}')


def _normalize(state):
    """Fill fields introduced after a build was frozen, so old snapshots replay."""
    state.setdefault('history', {})
    state.setdefault('conflicts', {})
    state.setdefault('queries', [])
    state.setdefault('batches', {})
    state.setdefault('sessions', {})
    for key, row in state['rows'].items():
        state['history'].setdefault(key, [{'version': row['version'], 'value': row['value']}])
    for node in state['nodes']:
        node.setdefault('index_state', 'building' if state.get('status') == 'building' else 'pending')
        node.setdefault('installed_position', 0)
    return state


def replay(initial_state, events):
    state = copy.deepcopy(initial_state)
    _normalize(state)
    results = []
    for seq, event in enumerate(events, start=1):
        outcome = apply_event(state, event, seq)
        results.append({'seq': seq, 'event': event, 'outcome': outcome})
    return state, results


def summarize(state):
    coverage = {
        'total_batches': len(state['scan_plan']),
        'applied_batches': sorted(int(k) for k, b in state['batches'].items() if b['applied']),
        'planned_rows': sum(len(batch) for batch in state['scan_plan']),
        'indexed_entries': len(state['entries']),
        'active_rows': len(_active_rows(state)),
    }
    live_conflicts = {v: r for v, r in state['conflicts'].items() if r['resolved_seq'] is None}
    counters = state['counters']
    return {
        'status': state['status'],
        'policy': state['policy'],
        'snapshot_position': state['snapshot_position'],
        'ready_position': state['ready_position'],
        'error': state['error'],
        'coverage': coverage,
        'conflicts': sorted(live_conflicts.values(), key=lambda r: r['value']),
        'conflict_history': sorted(state['conflicts'].values(), key=lambda r: r['first_seq']),
        'counters': counters,
        'cost': {
            'blocked_writes': counters['writes_rejected'],
            'accepted_conflicting_writes': counters['accepted_conflicting_writes'],
            'failed_validations': counters['finalize_failures'],
            'rejected_index_queries': counters['queries_rejected'],
            'conflicts_detected': counters['conflicts_detected'],
            'conflicts_resolved': counters['conflicts_resolved'],
        },
        'checksum': hashlib.sha256(
            json.dumps({'rows': state['rows'], 'entries': state['entries'],
                        'status': state['status'], 'ready': state['ready_position']},
                       sort_keys=True, ensure_ascii=False).encode()).hexdigest(),
    }
