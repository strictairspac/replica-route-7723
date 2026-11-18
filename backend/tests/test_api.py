import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

_tmp = tempfile.mkdtemp()
os.environ['DATABASE_URL'] = 'sqlite:///' + os.path.join(_tmp, 'api.db')
os.environ['SEED_DEMO'] = 'true'
os.environ.setdefault('JWT_SECRET', 'test-secret')

import pytest
from fastapi.testclient import TestClient

from src.main import app


@pytest.fixture(scope='module')
def client():
    with TestClient(app) as c:
        yield c


def login(client, role='operator'):
    r = client.post('/auth/login', json={'email': f'{role}@qiyuan.test', 'password': 'Demo2026!'})
    assert r.status_code == 200, r.text
    return {'Authorization': 'Bearer ' + r.json()['token']}


def new_build(client, headers, policy='block', batch_size=2):
    payload = {
        'name': 'api-test-' + policy,
        'table_name': 'members',
        'index_name': 'uniq_email',
        'key_column': 'email',
        'policy': policy,
        'batch_size': batch_size,
        'nodes': [
            {'name': 'm', 'role': 'primary', 'position': 10, 'delay_ms': 0},
            {'name': 'r1', 'role': 'replica', 'position': 10, 'delay_ms': 20},
        ],
        'rows': [{'id': i, 'value': f'v{i}@x'} for i in range(1, 6)],
    }
    r = client.post('/builds', json=payload, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()['id']


def test_health_and_login(client):
    assert client.get('/health').json()['status'] == 'healthy'
    assert client.post('/auth/login', json={'email': 'operator@qiyuan.test', 'password': 'bad'}).status_code == 401


def test_seeded_demo_builds_exist(client):
    rows = client.get('/builds', headers=login(client)).json()
    assert {r['policy'] for r in rows} == {'block', 'accept'}
    assert all(r['status'] in ('ready', 'failed', 'building') for r in rows)


def test_viewer_cannot_write(client):
    headers = login(client, 'viewer')
    assert client.get('/builds', headers=headers).status_code == 200
    r = client.post('/builds', json={}, headers=headers)
    assert r.status_code == 403


def test_unauthenticated_rejected(client):
    assert client.get('/builds').status_code in (401, 403)


def test_full_build_flow_and_query_routing(client):
    headers = login(client)
    bid = new_build(client, headers, policy='accept')
    trace = client.post(f'/builds/{bid}/events', json={'events': [{'kind': 'scan', 'batch_no': 0}]}, headers=headers)
    assert trace.status_code == 201
    r = client.post(f'/builds/{bid}/events', json={'events': [
        {'kind': 'write', 'row_id': 2, 'value': 'v2new@x', 'session': 'lab-A'},
        {'kind': 'scan', 'batch_no': 0},  # retry + stale
        {'kind': 'scan', 'batch_no': 1},
        {'kind': 'scan', 'batch_no': 2},
    ]}, headers=headers)
    assert r.status_code == 201, r.text
    state = r.json()['state']
    assert state['entries']['2']['key_value'] == 'v2new@x'
    assert r.json()['summary']['counters']['scan_stale_skipped'] == 1
    assert r.json()['summary']['counters']['scan_suppressed'] == 1
    r = client.post(f'/builds/{bid}/events', json={'events': [{'kind': 'finalize'}]}, headers=headers)
    assert r.json()['summary']['status'] == 'ready'
    r = client.post(f'/builds/{bid}/events', json={'events': [
        {'kind': 'catch_up', 'node': 'r1', 'position': 11},
        {'kind': 'query', 'value': 'v2new@x', 'session': 'ddl'},
    ]}, headers=headers)
    q = r.json()['trace'][-1]['outcome']
    assert q['decision'] == 'read' and q['node'] == 'r1'


def test_invalid_events_return_422_and_do_not_persist(client):
    headers = login(client)
    bid = new_build(client, headers)
    before = client.get(f'/builds/{bid}', headers=headers).json()['events']
    bad = client.post(f'/builds/{bid}/events', json={'events': [{'kind': 'write'}]}, headers=headers)
    assert bad.status_code == 422
    bad2 = client.post(f'/builds/{bid}/events', json={'events': [
        {'kind': 'catch_up', 'node': 'nope', 'position': 5}]}, headers=headers)
    assert bad2.status_code == 422
    after = client.get(f'/builds/{bid}', headers=headers).json()['events']
    assert before == after


def test_rewind_and_verify_job(client):
    headers = login(client)
    bid = new_build(client, headers, policy='accept')
    client.post(f'/builds/{bid}/events', json={'events': [
        {'kind': 'scan', 'batch_no': 0}, {'kind': 'scan', 'batch_no': 1},
        {'kind': 'scan', 'batch_no': 2}]}, headers=headers)
    r = client.post(f'/builds/{bid}/rewind', json={'keep': 1}, headers=headers)
    assert r.status_code == 200
    assert len(r.json()['events']) == 1
    job = client.post(f'/builds/{bid}/verify', headers=headers)
    assert job.status_code == 201
    assert job.json()['status'] == 'queued'
    jobs = client.get(f'/jobs?build_id={bid}', headers=headers).json()
    assert jobs[0]['expected_checksum']


def test_invalid_build_input(client):
    headers = login(client)
    payload = {
        'name': 'bad', 'policy': 'block', 'batch_size': 2,
        'nodes': [
            {'name': 'm1', 'role': 'primary', 'position': 1},
            {'name': 'm2', 'role': 'primary', 'position': 1},
        ],
        'rows': [{'id': 1, 'value': 'a'}],
    }
    assert client.post('/builds', json=payload, headers=headers).status_code == 422
    payload['nodes'] = [{'name': 'm1', 'role': 'primary', 'position': 1}]
    payload['rows'] = [{'id': 1, 'value': 'a'}, {'id': 1, 'value': 'b'}]
    assert client.post('/builds', json=payload, headers=headers).status_code == 422


def test_missing_at_ms_gets_monotonic_and_decreasing_rejected(client):
    headers = login(client)
    bid = new_build(client, headers)
    r = client.post(f'/builds/{bid}/events', json={'events': [
        {'kind': 'scan', 'batch_no': 0}, {'kind': 'scan', 'batch_no': 1}]}, headers=headers)
    assert r.status_code == 201, r.text
    times = [e['at_ms'] for e in r.json()['build']['events']]
    assert times == sorted(times) and len(set(times)) == len(times)
    bad = client.post(f'/builds/{bid}/events', json={'events': [
        {'kind': 'scan', 'batch_no': 2, 'at_ms': 0}]}, headers=headers)
    assert bad.status_code == 422
