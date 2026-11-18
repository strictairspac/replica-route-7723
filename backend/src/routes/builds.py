from fastapi import APIRouter, Depends
from fastapi.responses import Response

from src.config.database import db_session
from src.schemas.build import BuildInput, EventBatchInput, RewindInput
from src.services import builds as service
from src.utils.security import current, writer

import json

router = APIRouter()


@router.get('/builds')
def listing(db=Depends(db_session), user=Depends(current)):
    return service.listing(db)


@router.post('/builds', status_code=201)
def create(data: BuildInput, db=Depends(db_session), user=Depends(writer)):
    return service.create(db, data, user)


@router.get('/builds/{ident}')
def detail(ident: int, db=Depends(db_session), user=Depends(current)):
    return service.detail(db, ident)


@router.post('/builds/{ident}/events', status_code=201)
def append(ident: int, data: EventBatchInput, db=Depends(db_session), user=Depends(writer)):
    return service.append_events(db, ident, data.events, user)


@router.post('/builds/{ident}/rewind')
def rewind(ident: int, data: RewindInput, db=Depends(db_session), user=Depends(writer)):
    return service.rewind(db, ident, data.keep, user)


@router.post('/builds/{ident}/verify', status_code=201)
def verify(ident: int, db=Depends(db_session), user=Depends(writer)):
    return service.enqueue_verify(db, ident, user)


@router.get('/builds/{ident}/export')
def export(ident: int, db=Depends(db_session), user=Depends(current)):
    payload = service.detail(db, ident)
    return Response(json.dumps(payload, ensure_ascii=False, indent=2),
                    media_type='application/json',
                    headers={'Content-Disposition': f'attachment; filename="index-build-{ident}.json"'})


@router.get('/jobs')
def jobs(build_id: int | None = None, db=Depends(db_session), user=Depends(current)):
    return service.jobs(db, build_id)


@router.get('/jobs/{ident}/artifact')
def artifact(ident: int, db=Depends(db_session), user=Depends(current)):
    return service.artifact(db, ident)
