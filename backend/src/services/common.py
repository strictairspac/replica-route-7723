from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError

from src.models.entities import Audit
from src.utils.serialization import record


def save(db, obj, user, action):
    db.add(obj)
    db.add(Audit(actor=user.email, action=action))
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, '记录编号冲突或引用对象无效，请调整后重试')
    db.refresh(obj)
    return record(obj)
