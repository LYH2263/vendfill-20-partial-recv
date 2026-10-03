from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.database import get_db
from app.models.models import RefillOrder
from app.services.fill_engine import (
    RefillError, create_refill_order, get_order, order_to_dict,
    verify_lines, void_order,
)

router = APIRouter(prefix="/refills", tags=["refills"])


class VerifyRequest(BaseModel):
    lane_ids: list[int] | None = None      # 勾选核销的行；None=核销全部待核销行
    fail_after: int | None = Field(default=None)  # 故障演练：应用 N 行后提交前失败


def _raise(e: RefillError):
    raise HTTPException(e.status_code, e.message)


def _latest_order_id(db: Session, location_id: int) -> int | None:
    order = db.scalars(select(RefillOrder).where(RefillOrder.location_id == location_id)
                       .order_by(RefillOrder.id.desc())).first()
    return order.id if order else None


@router.post("/run")
def run_refill(location_id: int = 1, db: Session = Depends(get_db)):
    try:
        return create_refill_order(db, location_id)
    except RefillError as e:
        _raise(e)


@router.get("/latest")
def latest(location_id: int = 1, db: Session = Depends(get_db)):
    order_id = _latest_order_id(db, location_id)
    if order_id is None:
        try:
            return create_refill_order(db, location_id)
        except RefillError as e:
            _raise(e)
    return order_to_dict(db, get_order(db, order_id))


@router.get("/full")
def full_lanes(location_id: int = 1, db: Session = Depends(get_db)):
    data = latest(location_id=location_id, db=db)
    return {"location_id": location_id, "order_status": data["order_status"],
            "lanes": [l for l in data["lines"] if l["status"] == "full"]}


@router.get("/summary")
def refill_summary(location_id: int = 1, db: Session = Depends(get_db)):
    data = latest(location_id=location_id, db=db)
    return {
        "location_id": location_id,
        "order_id": data["id"],
        "order_status": data["order_status"],
        "completed": data["completed"],
        "voided": data["voided"],
        "total_fill": data["total_fill"],
        "need_fill_count": data["need_fill_count"],
        "full_count": data["full_count"],
        "overbooked_count": data["overbooked_count"],
        "total_line_count": data["total_line_count"],
        "verified_count": data["verified_count"],
        "pending_count": data["pending_count"],
        "verified_fill": data["verified_fill"],
        "pending_fill": data["pending_fill"],
        "released_fill": data["released_fill"],
    }


@router.get("/{order_id}")
def get_refill(order_id: int, db: Session = Depends(get_db)):
    try:
        return order_to_dict(db, get_order(db, order_id))
    except RefillError as e:
        _raise(e)


@router.post("/{order_id}/verify")
def verify_refill(order_id: int, body: VerifyRequest, db: Session = Depends(get_db)):
    """按勾选行分批核销；任一入选行失败则整批回滚。"""
    try:
        return verify_lines(db, order_id, lane_ids=body.lane_ids, fail_after=body.fail_after)
    except RefillError as e:
        _raise(e)


@router.post("/{order_id}/void")
def void_refill(order_id: int, db: Session = Depends(get_db)):
    try:
        return void_order(db, order_id)
    except RefillError as e:
        _raise(e)
