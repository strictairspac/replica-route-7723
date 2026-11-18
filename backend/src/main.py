import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy import select, text

from src.config.database import Base, engine
from src.config.settings import SEED_DEMO
from src.controllers.auth import router as auth
from src.middlewares.errors import ErrorMiddleware
from src.middlewares.request_id import RequestIdMiddleware
from src.models import entities  # noqa: F401 - registers tables
from src.models.entities import BuildJob
from src.routes import audit, builds


@asynccontextmanager
async def lifespan(app):
    Base.metadata.create_all(engine)
    if SEED_DEMO:
        try:
            from scripts.seed_demo import seed
            seed()
        except Exception:
            logging.exception('seed failed; manual retry is available')
    yield


app = FastAPI(
    title='在线唯一索引演练',
    description='在线加唯一索引：扫描/写入并发、幂等批次、重复值策略、索引就绪路由。',
    lifespan=lifespan,
)
app.add_middleware(ErrorMiddleware)
app.add_middleware(RequestIdMiddleware)
for r in (auth, builds.router, audit.router):
    app.include_router(r)


@app.get('/health')
def health():
    with engine.connect() as conn:
        conn.execute(text('SELECT 1'))
    return {'status': 'healthy', 'db': True}


@app.get('/jobs/pending')
def pending():
    """Progress probe for polling UIs."""
    from src.config.database import Session
    with Session() as db:
        ids = list(db.scalars(select(BuildJob.id).where(BuildJob.status.in_(('queued', 'running')))))
    return {'pending': len(ids), 'ids': ids}
