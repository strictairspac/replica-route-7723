"""Background verifier for online-index builds.

The API replays a build synchronously so the UI is immediate.  This worker
independently replays the frozen snapshot + event log from MySQL, compares the
resulting checksum with the checksum the API persisted, and writes a JSON
artifact.  A mismatch marks the job failed and is surfaced in the UI; it never
mutates the build itself.
"""

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select

from src.config.database import Base, Session, engine
from src.models.entities import Build, BuildJob
from src.services import online_index

ARTIFACT_DIR = Path(os.getenv('ARTIFACT_DIR', '/artifacts'))
HEARTBEAT = Path(os.getenv('WORKER_HEARTBEAT', '/tmp/worker-heartbeat'))


def _claim(db):
    """Oldest queued job, flipped to running inside a transaction."""
    job = db.scalar(select(BuildJob).where(BuildJob.status == 'queued').order_by(BuildJob.id))
    if not job:
        return None
    job.status = 'running'
    db.commit()
    return job


def run_job(job_id):
    with Session() as db:
        job = db.get(BuildJob, job_id)
        if not job or job.status not in ('queued', 'running'):
            return
        build = db.get(Build, job.build_id)
        if not build:
            job.status = 'failed'
            job.error = '构建记录不存在'
            job.finished_at = datetime.now(timezone.utc)
            db.commit()
            return
        events = list(build.events or [])
        state, trace = online_index.replay(build.initial_state, events)
        checksum = online_index.summarize(state)['checksum']
        job.actual_checksum = checksum
        job.event_count = len(events)
        if checksum == job.expected_checksum:
            job.status = 'completed'
            job.error = None
        else:
            job.status = 'failed'
            job.error = '独立重放校验和不一致，构建证据不可信'
        job.finished_at = datetime.now(timezone.utc)
        db.commit()
        try:
            ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
            path = ARTIFACT_DIR / f'index-build-{build.id}-job-{job.id}.json'
            path.write_text(json.dumps({
                'build_id': build.id,
                'job_id': job.id,
                'checksum': checksum,
                'expected_checksum': job.expected_checksum,
                'status': job.status,
                'events': events,
                'trace': trace,
                'summary': online_index.summarize(state),
            }, ensure_ascii=False, indent=2), encoding='utf-8')
            job.artifact = str(path)
            db.commit()
        except Exception as exc:  # artifact is evidence, not correctness
            job.error = (job.error + '；' if job.error else '') + f'证据文件写入失败：{exc}'
            db.commit()


def main():
    Base.metadata.create_all(engine)
    while True:
        HEARTBEAT.touch()
        job_id = None
        try:
            with Session() as db:
                job = _claim(db)
                job_id = job.id if job else None
            if job_id is not None:
                run_job(job_id)
                continue
        except Exception as exc:  # keep the worker alive and retry
            print('verify worker error:', exc, flush=True)
        time.sleep(2)


if __name__ == '__main__':
    main()
