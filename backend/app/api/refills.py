from dataclasses import asdict

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.database import get_db
from app.models.models import Lane, RefillOrder
from app.services.fill_engine import RuleError, build_fill_lines
from app.services import refill_service as svc

router = APIRouter(prefix="/refills", tags=["refills"])


class VerifyIn(BaseModel):
    lane_ids: list[int] = Field(default_factory=list)


@router.post("/run")
def run_refill(location_id: int = 1, db: Session = Depends(get_db)):
    """取当前进行中的补货单；没有则按缺口开单（开单即预占在途）。幂等。"""
    try:
        order, _created = svc.create_or_get_active_order(db, location_id)
    except LookupError as e:
        raise HTTPException(404, str(e))
    except RuleError as e:
        raise HTTPException(409, str(e))
    return svc.order_payload(db, order)


@router.get("/latest")
def latest(location_id: int = 1, db: Session = Depends(get_db)):
    try:
        order = svc.latest_order(db, location_id)
        if order is None:
            order, _ = svc.create_or_get_active_order(db, location_id)
    except LookupError as e:
        raise HTTPException(404, str(e))
    except RuleError as e:
        raise HTTPException(409, str(e))
    return svc.order_payload(db, order)


# 注意：/full、/summary 必须声明在 /{order_id} 之前，否则会被路径参数吞掉。

@router.get("/full")
def full_lanes(location_id: int = 1, db: Session = Depends(get_db)):
    # 满仓视图不依赖补货单：直接按货道实时口径计算缺口为 0 的行
    lanes = db.scalars(
        select(Lane).where(Lane.location_id == location_id).order_by(Lane.slot_no)
    ).all()
    payload = [{"id": l.id, "slot_no": l.slot_no, "sku_name": l.sku_name,
                "capacity": l.capacity, "stock": l.stock, "in_transit": l.in_transit}
               for l in lanes]
    return {"location_id": location_id,
            "lanes": [asdict(l) for l in build_fill_lines(payload) if l.status == "full"]}


@router.get("/summary")
def refill_summary(location_id: int = 1, db: Session = Depends(get_db)):
    try:
        order = svc.latest_order(db, location_id)
        if order is None:
            order, _ = svc.create_or_get_active_order(db, location_id)
    except LookupError as e:
        raise HTTPException(404, str(e))
    except RuleError as e:
        raise HTTPException(409, str(e))
    data = svc.order_payload(db, order)
    return {
        "location_id": location_id,
        "order_id": data["id"],
        "status": data["status"],
        "total_fill": data["total_fill"],
        "need_fill_count": data["need_fill_count"],
        "full_count": data["full_count"],
        "overbooked_count": data["overbooked_count"],
        "pending_count": data["pending_count"],
        "verified_count": data["verified_count"],
        "voided_count": data["voided_count"],
        "pending_qty": data["pending_qty"],
        "verified_qty": data["verified_qty"],
        "all_verified": data["all_verified"],
    }


@router.get("/{order_id}")
def get_order(order_id: int, db: Session = Depends(get_db)):
    order = db.get(RefillOrder, order_id)
    if order is None:
        raise HTTPException(404, f"补货单 {order_id} 不存在")
    return svc.order_payload(db, order)


@router.post("/{order_id}/verify")
def verify(order_id: int, body: VerifyIn, db: Session = Depends(get_db)):
    """分批到货按行核销：勾选行加库存、扣在途；整批任一失败全部回滚。"""
    try:
        order = svc.verify_lines(db, order_id, body.lane_ids)
    except LookupError as e:
        raise HTTPException(404, str(e))
    except RuleError as e:
        # 规则冲突 = 本批未提交，货道/行/汇总仍是批前口径
        raise HTTPException(409, str(e))
    return svc.order_payload(db, order)


@router.post("/{order_id}/void")
def void(order_id: int, db: Session = Depends(get_db)):
    try:
        order = svc.void_order(db, order_id)
    except LookupError as e:
        raise HTTPException(404, str(e))
    except RuleError as e:
        raise HTTPException(409, str(e))
    return svc.order_payload(db, order)
