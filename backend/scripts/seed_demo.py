from sqlalchemy import func, select

from src.config.database import Base, Session, engine
from src.models.entities import Build, User
from src.services import online_index
from src.services.builds import _replay
from src.utils.security import password_hash

DEMO_ROWS = [
    {'id': 1, 'value': 'member1@qiyuan.test'},
    {'id': 2, 'value': 'member2@qiyuan.test'},
    {'id': 3, 'value': 'member3@qiyuan.test'},
    {'id': 4, 'value': 'member4@qiyuan.test'},
    {'id': 5, 'value': 'member5@qiyuan.test'},
    {'id': 6, 'value': 'member6@qiyuan.test'},
]

NODES = [
    {'name': 'harbor-primary', 'role': 'primary', 'status': 'healthy', 'position': 105, 'delay_ms': 0},
    {'name': 'north-replica', 'role': 'replica', 'status': 'healthy', 'position': 104, 'delay_ms': 150},
    {'name': 'south-replica', 'role': 'replica', 'status': 'healthy', 'position': 105, 'delay_ms': 20},
]


def _build(name, policy, events, batch_size=3):
    inputs = {'nodes': NODES, 'rows': DEMO_ROWS}
    initial = online_index.make_initial_state(
        name=name, table_name='members', index_name='uniq_member_email',
        key_column='email', policy=policy, batch_size=batch_size,
        nodes=inputs['nodes'], rows=inputs['rows'],
    )
    build = Build(
        name=name, table_name='members', index_name='uniq_member_email',
        key_column='email', policy=policy, batch_size=batch_size,
        status=initial['status'], initial_state=initial, events=[],
        state=initial, summary=online_index.summarize(initial), error=None,
    )
    return build, initial


def seed():
    Base.metadata.create_all(engine)
    with Session() as db:
        for role in ('admin', 'operator', 'viewer'):
            email = role + '@qiyuan.test'
            if not db.scalar(select(User).where(User.email == email)):
                db.add(User(email=email, password=password_hash('Demo2026!'), role=role))
        db.commit()
        if db.scalar(select(func.count()).select_from(Build)):
            print({'builds': db.scalar(select(func.count()).select_from(Build))})
            return

        accepted_events = [
            {'at_ms': 0, 'kind': 'scan', 'batch_no': 0, 'session': 'ddl'},
            {'at_ms': 50, 'kind': 'scan', 'batch_no': 1, 'session': 'ddl'},
            {'at_ms': 100, 'kind': 'write', 'row_id': 3, 'value': 'member3-new@qiyuan.test', 'session': 'lab-A'},
            {'at_ms': 120, 'kind': 'scan', 'batch_no': 0, 'session': 'ddl'},  # 同批重试
            {'at_ms': 150, 'kind': 'query', 'value': 'member3-new@qiyuan.test', 'session': 'lab-A'},
            {'at_ms': 200, 'kind': 'write', 'row_id': 4, 'value': 'member3-new@qiyuan.test', 'session': 'lab-B'},
            {'at_ms': 220, 'kind': 'scan', 'batch_no': 1, 'session': 'ddl'},  # 同批重试
            {'at_ms': 260, 'kind': 'finalize', 'session': 'ddl'},
            {'at_ms': 320, 'kind': 'delete', 'row_id': 4, 'session': 'lab-B'},
            {'at_ms': 360, 'kind': 'finalize', 'session': 'ddl'},
            {'at_ms': 400, 'kind': 'catch_up', 'node': 'south-replica', 'position': 108, 'session': 'ddl'},
            {'at_ms': 420, 'kind': 'catch_up', 'node': 'north-replica', 'position': 108, 'session': 'ddl'},
            {'at_ms': 500, 'kind': 'query', 'value': 'member3-new@qiyuan.test', 'session': 'lab-B'},
        ]
        accepted, initial = _build('演示 · 照收冲突后消除（accept）', 'accept', accepted_events)
        if not db.scalar(select(Build).where(Build.name == accepted.name)):
            accepted.events = accepted_events
            _replay(accepted, accepted_events)
            db.add(accepted)
            db.commit()

        blocked_events = [
            {'at_ms': 0, 'kind': 'scan', 'batch_no': 0, 'session': 'ddl'},
            {'at_ms': 100, 'kind': 'write', 'row_id': 9, 'value': 'member1@qiyuan.test', 'session': 'lab-A'},
            {'at_ms': 200, 'kind': 'scan', 'batch_no': 1, 'session': 'ddl'},
            {'at_ms': 260, 'kind': 'finalize', 'session': 'ddl'},
        ]
        blocked, initial = _build('演示 · 构建立即拦下重复值（block）', 'block', blocked_events)
        if not db.scalar(select(Build).where(Build.name == blocked.name)):
            blocked.events = blocked_events
            _replay(blocked, blocked_events)
            db.add(blocked)
            db.commit()
        print({cls.__tablename__: db.scalar(select(func.count()).select_from(cls)) for cls in (User, Build)})


if __name__ == '__main__':
    seed()
