from fastapi import HTTPException
from sqlalchemy import select

from src.models.entities import Build, BuildJob
from src.services import online_index
from src.services.common import save
from src.utils.serialization import record


def _snapshot_inputs(data):
    return {
        'nodes': [n.model_dump() for n in data.nodes],
        'rows': [r.model_dump() for r in data.rows],
    }


def _initial(data, inputs):
    return online_index.make_initial_state(
        name=data.name,
        table_name=data.table_name,
        index_name=data.index_name,
        key_column=data.key_column,
        policy=data.policy,
        batch_size=data.batch_size,
        nodes=inputs['nodes'],
        rows=inputs['rows'],
    )


def _replay(build, events):
    state, trace = online_index.replay(build.initial_state, events)
    build.state = state
    build.summary = online_index.summarize(state)
    build.status = state['status']
    build.error = state['error']
    return state, trace


def _detail(build):
    state, trace = online_index.replay(build.initial_state, build.events)
    return {
        **record(build),
        'state': state,
        'summary': online_index.summarize(state),
        'trace': trace,
        'scan_plan': state['scan_plan'],
    }


def listing(db):
    rows = db.scalars(select(Build).order_by(Build.id.desc())).all()
    result = []
    for row in rows:
        item = record(row)
        item.pop('state', None)
        item.pop('initial_state', None)
        item.pop('events', None)
        item['events_count'] = len(row.events or [])
        item['summary'] = row.summary
        result.append(item)
    return result


def detail(db, ident):
    build = db.get(Build, ident)
    if not build:
        raise HTTPException(404, '索引构建不存在')
    return _detail(build)


def create(db, data, user):
    inputs = _snapshot_inputs(data)
    try:
        initial = _initial(data, inputs)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    build = Build(
        name=data.name,
        table_name=data.table_name,
        index_name=data.index_name,
        key_column=data.key_column,
        policy=data.policy,
        batch_size=data.batch_size,
        status=initial['status'],
        initial_state=initial,
        events=[],
        state=initial,
        summary=online_index.summarize(initial),
        error=None,
    )
    return save(db, build, user, f'创建在线唯一索引构建 {data.name}')


def append_events(db, ident, events, user):
    build = db.get(Build, ident)
    if not build:
        raise HTTPException(404, '索引构建不存在')
    payload = [e.model_dump() for e in events]
    candidate = list(build.events or []) + payload
    if len(candidate) > 200:
        raise HTTPException(422, '单个构建最多 200 条编排事件')
    # Missing at_ms values get a deterministic monotonic slot; explicit values
    # must stay non-decreasing so the evidence log reads chronologically.
    last_at = max((int(e.get('at_ms') or 0) for e in build.events or []), default=0)
    for index, event in enumerate(payload):
        if event.get('at_ms') is None:
            last_at += 10
            event['at_ms'] = last_at
        elif int(event['at_ms']) < last_at:
            raise HTTPException(422, '事件时间必须非递减，请把 at_ms 调到不小于上一条')
        else:
            last_at = int(event['at_ms'])
    try:
        state, trace = _replay(build, candidate)
    except ValueError as exc:
        db.rollback()
        raise HTTPException(422, str(exc))
    build.events = candidate
    build.state = state
    build.summary = online_index.summarize(state)
    build.status = state['status']
    build.error = state['error']
    save(db, build, user, f'追加 {len(payload)} 条构建/查询事件')
    return {
        'build': record(build),
        'state': state,
        'summary': build.summary,
        'trace': trace,
        'scan_plan': state['scan_plan'],
    }


def rewind(db, ident, keep, user):
    build = db.get(Build, ident)
    if not build:
        raise HTTPException(404, '索引构建不存在')
    if keep > len(build.events or []):
        raise HTTPException(422, '保留事件数超过现有事件数')
    try:
        state, trace = _replay(build, list(build.events or [])[:keep])
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    build.events = list(build.events or [])[:keep]
    build.state = state
    build.summary = online_index.summarize(state)
    build.status = state['status']
    build.error = state['error']
    save(db, build, user, f'回退到前 {keep} 条事件')
    return _detail(build)


def enqueue_verify(db, ident, user):
    build = db.get(Build, ident)
    if not build:
        raise HTTPException(404, '索引构建不存在')
    if not build.events:
        raise HTTPException(422, '尚未编排任何事件，无法验证')
    expected = (build.summary or {}).get('checksum')
    job = BuildJob(build_id=ident, status='queued', expected_checksum=expected,
                   event_count=len(build.events))
    db.add(job)
    db.commit()
    db.refresh(job)
    return record(job)


def jobs(db, build_id=None):
    query = select(BuildJob).order_by(BuildJob.id.desc()).limit(50)
    if build_id:
        query = query.where(BuildJob.build_id == build_id)
    return [record(x) for x in db.scalars(query)]


def artifact(db, job_id):
    import json
    from pathlib import Path
    job = db.get(BuildJob, job_id)
    if not job:
        raise HTTPException(404, '校验任务不存在')
    if not job.artifact:
        raise HTTPException(404, '校验任务还没有证据文件')
    path = Path(job.artifact)
    if not path.is_file():
        raise HTTPException(404, '证据文件不存在或已被清理')
    return json.loads(path.read_text(encoding='utf-8'))
