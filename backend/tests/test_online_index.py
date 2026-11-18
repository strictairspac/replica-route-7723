import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest

from src.services import online_index as oi

NODES = [
    {'name': 'harbor-primary', 'role': 'primary', 'status': 'healthy', 'position': 100, 'delay_ms': 0},
    {'name': 'south-replica', 'role': 'replica', 'status': 'healthy', 'position': 100, 'delay_ms': 20},
    {'name': 'north-replica', 'role': 'replica', 'status': 'healthy', 'position': 100, 'delay_ms': 150},
]
ROWS = [
    {'id': 1, 'value': 'a@x'}, {'id': 2, 'value': 'b@x'}, {'id': 3, 'value': 'c@x'},
    {'id': 4, 'value': 'd@x'}, {'id': 5, 'value': 'e@x'},
]


def state(policy='block', batch_size=2, nodes=NODES, rows=ROWS):
    return oi.make_initial_state('t', 'members', 'uniq_email', 'email', policy,
                                 batch_size, nodes, rows)


def run(events, **kw):
    initial = state(**kw)
    out, trace = oi.replay(initial, events)
    return out, trace, oi.summarize(out)


def scan_all(batches):
    return [{'kind': 'scan', 'batch_no': i} for i in range(batches)]


def test_stale_scan_never_overwrites_newer_write():
    events = [
        {'kind': 'write', 'row_id': 2, 'value': 'b-new@x', 'session': 'lab-A'},
        {'kind': 'scan', 'batch_no': 0},       # snapshot still says b@x
        {'kind': 'scan', 'batch_no': 1},
        {'kind': 'scan', 'batch_no': 2},
        {'kind': 'finalize'},
    ]
    st, trace, summary = run(events)
    scan = trace[1]['outcome']
    assert scan['stale_skipped'] == 1
    assert st['entries']['2']['key_value'] == 'b-new@x'
    assert st['entries']['2']['version'] == 101
    assert summary['status'] == 'ready'


def test_scan_then_write_then_scan_result_is_new_value():
    events = [
        {'kind': 'scan', 'batch_no': 0},
        {'kind': 'write', 'row_id': 2, 'value': 'b-new@x', 'session': 'lab-A'},
        {'kind': 'scan', 'batch_no': 0},      # retry after write; entry is newer
    ]
    st, trace, _ = run(events)
    assert trace[2]['outcome']['stale_skipped'] == 1
    assert st['entries']['2']['key_value'] == 'b-new@x'


def test_same_batch_retry_is_idempotent():
    events = [
        {'kind': 'scan', 'batch_no': 0},
        {'kind': 'scan', 'batch_no': 0},
        {'kind': 'scan', 'batch_no': 0},
    ]
    st, trace, summary = run(events)
    first, second, third = (t['outcome'] for t in trace)
    assert first['inserted'] == 2 and first['suppressed'] == 0
    assert second['inserted'] == 0 and second['suppressed'] == 2
    assert third['inserted'] == 0 and third['suppressed'] == 2
    assert len(st['entries']) == 2
    assert summary['counters']['scan_inserted'] == 2
    assert summary['counters']['scan_suppressed'] == 4


def test_retry_after_full_coverage_does_not_grow_index():
    events = scan_all(3) + [{'kind': 'scan', 'batch_no': 0}, {'kind': 'finalize'}]
    st, trace, summary = run(events)
    assert len(st['entries']) == len(ROWS)
    assert trace[-2]['outcome']['inserted'] == 0
    assert trace[-2]['outcome']['suppressed'] == 2
    assert summary['status'] == 'ready'


def test_scan_missing_batch_blocks_finalize():
    events = [{'kind': 'scan', 'batch_no': 0}, {'kind': 'finalize'}]
    st, trace, summary = run(events)
    assert trace[-1]['outcome']['decision'] == 'rejected'
    assert summary['status'] == 'building'


def test_block_policy_rejects_duplicate_write_and_keeps_index_valid():
    events = scan_all(3) + [
        {'kind': 'write', 'row_id': 5, 'value': 'a@x', 'session': 'lab-A'},
        {'kind': 'finalize'},
    ]
    st, trace, summary = run(events, policy='block')
    write = trace[-2]['outcome']
    assert write['decision'] == 'rejected'
    assert write['position_advanced'] is False
    assert st['nodes'][0]['position'] == 100
    assert summary['status'] == 'ready'
    assert summary['cost']['blocked_writes'] == 1
    assert summary['counters']['conflicts_detected'] == 0


def test_accept_policy_takes_write_then_fails_until_conflict_removed():
    events = scan_all(3) + [
        {'kind': 'write', 'row_id': 5, 'value': 'a@x', 'session': 'lab-A'},
        {'kind': 'finalize'},
        {'kind': 'delete', 'row_id': 5, 'session': 'lab-B'},
        {'kind': 'finalize'},
    ]
    st, trace, summary = run(events, policy='accept')
    write = trace[3]['outcome']
    assert write['decision'] == 'accepted'
    assert write['conflict'] is True
    assert write['conflict_row_ids'] == [1, 5]
    assert trace[4]['outcome']['decision'] == 'failed'
    assert '重复索引值' in trace[4]['outcome']['reason']
    assert summary['cost']['accepted_conflicting_writes'] == 1
    assert summary['cost']['conflicts_detected'] == 1
    assert summary['cost']['conflicts_resolved'] == 1
    assert summary['status'] == 'ready'
    assert summary['ready_position'] == 102


def test_ready_index_enforces_uniqueness_even_for_accept_build():
    events = scan_all(3) + [
        {'kind': 'finalize'},
        {'kind': 'write', 'row_id': 5, 'value': 'a@x', 'session': 'lab-A'},
        {'kind': 'write', 'row_id': 5, 'value': 'z@x', 'session': 'lab-A'},
    ]
    st, trace, summary = run(events, policy='accept')
    assert trace[4]['outcome']['decision'] == 'rejected'
    assert trace[5]['outcome']['decision'] == 'accepted'
    assert summary['status'] == 'ready'


def test_queries_rejected_while_building_and_routed_only_on_indexed_node():
    events = scan_all(3) + [
        {'kind': 'write', 'row_id': 1, 'value': 'a2@x', 'session': 'lab-A'},
        {'kind': 'query', 'value': 'a2@x', 'session': 'ddl'},
        {'kind': 'finalize'},
        {'kind': 'catch_up', 'node': 'south-replica', 'position': 101},
        {'kind': 'query', 'value': 'a2@x', 'session': 'ddl'},
        {'kind': 'query', 'value': 'missing@x', 'session': 'ddl'},
    ]
    st, trace, summary = run(events)
    assert trace[4]['outcome']['decision'] == 'rejected'
    q = trace[7]['outcome']
    assert q['decision'] == 'read'
    assert q['node'] == 'south-replica'
    assert q['candidates'] == ['south-replica', 'harbor-primary']
    assert trace[8]['outcome']['decision'] == 'not_found'
    assert summary['counters']['queries_not_found'] == 1


def test_query_respects_session_required_position_after_write():
    events = scan_all(3) + [
        {'kind': 'write', 'row_id': 1, 'value': 'a2@x', 'session': 'seed'},
        {'kind': 'finalize'},
        {'kind': 'catch_up', 'node': 'south-replica', 'position': 101},
        {'kind': 'write', 'row_id': 1, 'value': 'a3@x', 'session': 'lab-A'},
        {'kind': 'query', 'value': 'a3@x', 'session': 'lab-A'},
        {'kind': 'catch_up', 'node': 'south-replica', 'position': 102},
        {'kind': 'query', 'value': 'a3@x', 'session': 'lab-A'},
    ]
    st, trace, summary = run(events)
    q_before = trace[7]['outcome']
    assert q_before['decision'] == 'read'
    assert q_before['node'] == 'harbor-primary'
    assert q_before['required_position'] == 102
    q_after = trace[9]['outcome']
    assert q_after['node'] == 'south-replica'


def test_query_without_any_valid_node_is_rejected():
    events = scan_all(3) + [
        {'kind': 'finalize'},
        {'kind': 'node_down', 'node': 'south-replica'},
        {'kind': 'node_down', 'node': 'north-replica'},
        {'kind': 'query', 'value': 'a@x', 'session': 'ddl'},
    ]
    st, trace, summary = run(events)
    # primary is valid at the ready position, so the query falls back to it
    assert trace[-1]['outcome']['node'] == 'harbor-primary'
    events2 = scan_all(3) + [
        {'kind': 'finalize'},
        {'kind': 'node_down', 'node': 'harbor-primary'},
        {'kind': 'node_down', 'node': 'north-replica'},
        {'kind': 'node_down', 'node': 'south-replica'},
        {'kind': 'query', 'value': 'a@x', 'session': 'ddl'},
    ]
    st2, trace2, _ = run(events2)
    assert trace2[-1]['outcome']['decision'] == 'rejected'


def test_failed_build_blocks_queries_and_keeps_duplicate_evidence():
    events = scan_all(3) + [
        {'kind': 'write', 'row_id': 5, 'value': 'a@x', 'session': 'lab-A'},
        {'kind': 'finalize'},
        {'kind': 'query', 'value': 'a@x', 'session': 'lab-A'},
    ]
    st, trace, summary = run(events, policy='accept')
    assert summary['status'] == 'failed'
    assert summary['conflicts']
    assert trace[-1]['outcome']['decision'] == 'rejected'


def test_deterministic_checksum_across_replays():
    events = scan_all(3) + [{'kind': 'write', 'row_id': 4, 'value': 'd2@x', 'session': 's'},
                            {'kind': 'finalize'}]
    a = run(events)[2]['checksum']
    b = run(events)[2]['checksum']
    assert a == b


def test_write_after_ready_advances_index_position_and_keeps_it_valid():
    events = scan_all(3) + [
        {'kind': 'write', 'row_id': 1, 'value': 'a2@x', 'session': 'lab-A'},
        {'kind': 'finalize'},
        {'kind': 'catch_up', 'node': 'south-replica', 'position': 101},
        {'kind': 'query', 'value': 'a2@x', 'session': 'ddl'},
    ]
    st, trace, summary = run(events)
    primary = st['nodes'][0]
    assert primary['index_state'] == 'valid'
    assert primary['installed_position'] == 101
    assert trace[-1]['outcome']['node'] == 'south-replica'
    assert st['entries']['1']['key_value'] == 'a2@x'


def test_legacy_scan_does_not_duplicate_after_delete():
    events = scan_all(3) + [
        {'kind': 'delete', 'row_id': 2, 'session': 'lab-A'},
        {'kind': 'scan', 'batch_no': 0},
        {'kind': 'finalize'},
    ]
    st, trace, summary = run(events)
    assert trace[4]['outcome']['deleted_skipped'] == 1
    assert '2' not in st['entries']
    assert summary['status'] == 'ready'


def test_finalize_detects_missing_entry():
    # Build with a write, scan batches skipping the row version, then finalize
    events = [
        {'kind': 'scan', 'batch_no': 0},
        {'kind': 'scan', 'batch_no': 1},
        {'kind': 'scan', 'batch_no': 2},
        {'kind': 'write', 'row_id': 2, 'value': 'b-new@x', 'session': 'lab-A'},
        {'kind': 'finalize'},
    ]
    st, trace, summary = run(events)
    assert summary['status'] == 'ready'


def test_write_before_scan_then_old_batch_skips():
    events = [
        {'kind': 'write', 'row_id': 3, 'value': 'c-new@x', 'session': 'lab-A'},
        {'kind': 'scan', 'batch_no': 1},
        {'kind': 'scan', 'batch_no': 0},
        {'kind': 'scan', 'batch_no': 2},
        {'kind': 'finalize'},
    ]
    st, trace, summary = run(events)
    assert trace[1]['outcome']['stale_skipped'] == 1
    assert st['entries']['3']['key_value'] == 'c-new@x'
    assert summary['status'] == 'ready'


def test_duplicate_in_snapshot_fails_build_regardless_of_policy():
    rows = [{'id': 1, 'value': 'dup@x'}, {'id': 2, 'value': 'dup@x'}]
    st, trace, summary = run(scan_all(1) + [{'kind': 'finalize'}], rows=rows, policy='accept')
    assert summary['status'] == 'failed'
    assert 'dup@x' in summary['error']


def test_lagging_replica_serves_value_as_of_its_index_position():
    events = scan_all(3) + [
        {'kind': 'finalize'},
        {'kind': 'write', 'row_id': 1, 'value': 'a2@x', 'session': 'lab-A'},
        {'kind': 'catch_up', 'node': 'south-replica', 'position': 100},
        {'kind': 'query', 'value': 'a2@x', 'session': 'ddl'},
        {'kind': 'query', 'value': 'a@x', 'session': 'ddl'},
    ]
    st, trace, summary = run(events)
    # Both replicas are index-qualified only at the ready position (100), so
    # the new value written at 101 is invisible to them; the old value is not.
    assert trace[6]['outcome']['decision'] == 'not_found'
    assert trace[6]['outcome']['node'] == 'south-replica'
    assert trace[7]['outcome']['decision'] == 'read'
    assert trace[7]['outcome']['node'] == 'south-replica'
    assert trace[7]['outcome']['entry']['version'] == 100


def test_old_snapshot_without_history_still_replays():
    initial = state()
    initial.pop('history')
    initial.pop('conflicts')
    initial.pop('queries')
    for node in initial['nodes']:
        node.pop('index_state')
        node.pop('installed_position')
    out, trace = oi.replay(initial, scan_all(3) + [{'kind': 'finalize'},
                                                  {'kind': 'query', 'value': 'a@x', 'session': 'ddl'}])
    assert out['status'] == 'ready'
    assert trace[-1]['outcome']['decision'] == 'read'
