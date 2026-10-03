"""补货单 DB 服务：下单预占在途、分批按行核销、作废 —— 全部单事务提交。

事务边界即原子边界：行状态、货道库存/在途、整单完成态、行内汇总在同一事务里
一起落库；任一行校验失败或提交失败，本批全部回滚（未选中行与货道保持批前口径）。
纯规则见 fill_engine.plan_batch_verify / plan_void / order_completed。
"""
from __future__ import annotations
import json
from datetime import datetime

from dataclasses import asdict

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.models import (
    Lane, Location, RefillOrder,
    ORDER_STATUS_ACTIVE, ORDER_STATUS_COMPLETED, ORDER_STATUS_VOIDED,
)
from app.services.fill_engine import (
    RuleError, build_fill_lines, plan_batch_verify, plan_void,
    positive_lines, summarize_stored,
)


def _load_order(db: Session, order_id: int) -> RefillOrder:
    # 行级锁：并发核销/作废同一单时在 Postgres 上串行化（SQLite 自动忽略 FOR UPDATE）
    order = db.scalars(
        select(RefillOrder).where(RefillOrder.id == order_id).with_for_update()
    ).first()
    if order is None:
        raise LookupError(f"补货单 {order_id} 不存在")
    return order


def _lock_lanes(db: Session, lane_ids: list[int]) -> dict[int, Lane]:
    if not lane_ids:
        return {}
    rows = db.scalars(
        select(Lane).where(Lane.id.in_(lane_ids)).with_for_update()
    ).all()
    return {l.id: l for l in rows}


def _assert_active(order: RefillOrder) -> None:
    """已完成整单再核任何行必须拒绝；作废单同样终态拒绝。"""
    if order.status == ORDER_STATUS_COMPLETED:
        raise RuleError("整单已完成，禁止再核销任何行")
    if order.status == ORDER_STATUS_VOIDED:
        raise RuleError("补货单已作废，禁止核销")


def create_or_get_active_order(db: Session, location_id: int) -> tuple[RefillOrder, bool]:
    """返回当前点位进行中的补货单；没有则按缺口开单并预占在途。

    幂等：存在 active 单直接返回（created=False），不重复预占、不重复建行。
    """
    # 锁点位行：并发开单在 Postgres 上串行化，后到者会看到前者已提交的 active 单
    loc = db.scalars(select(Location).where(Location.id == location_id).with_for_update()).first()
    if loc is None:
        raise LookupError(f"点位 {location_id} 不存在")

    existing = db.scalars(
        select(RefillOrder)
        .where(RefillOrder.location_id == location_id,
               RefillOrder.status == ORDER_STATUS_ACTIVE)
        .order_by(RefillOrder.id.desc())
    ).first()
    if existing is not None:
        return existing, False

    lanes = db.scalars(
        select(Lane).where(Lane.location_id == location_id)
        .order_by(Lane.slot_no).with_for_update()
    ).all()
    payload = [{"id": l.id, "slot_no": l.slot_no, "sku_name": l.sku_name,
                "capacity": l.capacity, "stock": l.stock, "in_transit": l.in_transit}
               for l in lanes]
    summary = summarize_stored([asdict(l) for l in build_fill_lines(payload)])
    if not positive_lines(summary["lines"]):
        raise RuleError("该点位所有货道均无需补货，无法开单")

    # 开单即占用在途口径：未核销行的补量一直挂在货道在途上，直到核销或作废。
    by_id = {l.id: l for l in lanes}
    for line in summary["lines"]:
        qty = int(line["fill_qty"])
        if qty > 0:
            by_id[line["lane_id"]].in_transit += qty

    order = RefillOrder(location_id=location_id, created_at=datetime.utcnow(),
                        lines_json=json.dumps(summary, ensure_ascii=False),
                        status=ORDER_STATUS_ACTIVE)
    db.add(order)
    db.commit()
    db.refresh(order)
    return order, True


def latest_order(db: Session, location_id: int) -> RefillOrder | None:
    return db.scalars(
        select(RefillOrder).where(RefillOrder.location_id == location_id)
        .order_by(RefillOrder.id.desc())
    ).first()


def order_payload(db: Session, order: RefillOrder) -> dict:
    """行内汇总 + 货道实时库存/在途，全部取自同一套行态口径。"""
    stored = json.loads(order.lines_json)
    lines = stored.get("lines", [])
    lane_ids = [int(l["lane_id"]) for l in lines]
    lanes = {l.id: l for l in
             db.scalars(select(Lane).where(Lane.id.in_(lane_ids))).all()} if lane_ids else {}
    for line in lines:
        lane = lanes.get(int(line["lane_id"]))
        line["lane_stock"] = lane.stock if lane else None
        line["lane_in_transit"] = lane.in_transit if lane else None
    summary = summarize_stored(lines)
    return {
        "id": order.id,
        "location_id": order.location_id,
        "status": order.status,
        "created_at": order.created_at.isoformat() if order.created_at else None,
        **summary,
    }


def verify_lines(db: Session, order_id: int, lane_ids: list[int] | set[int]) -> RefillOrder:
    """分批到货按行核销（单事务原子提交）。

    入选行：按补量加库存、扣在途、行态置 verified；未选中行不动且继续占用在途。
    整单仅在全部正补量行已核销后置 completed。任一行失败 → 整批回滚。
    """
    order = _load_order(db, order_id)
    _assert_active(order)

    stored = json.loads(order.lines_json)
    lines = stored.get("lines", [])
    lane_ids_in_order = [int(l["lane_id"]) for l in lines]
    lanes = _lock_lanes(db, lane_ids_in_order)

    # 实时货道口径参与规划；校验不过直接抛 RuleError，此时尚未写任何东西。
    try:
        lane_states = {lid: {"in_transit": lane.in_transit, "stock": lane.stock}
                       for lid, lane in lanes.items()}
        new_lines, changes, new_status = plan_batch_verify(lines, lane_states, set(lane_ids))

        for lane_id, ch in changes.items():
            lane = lanes[lane_id]
            lane.stock += ch.stock_delta
            lane.in_transit += ch.transit_delta
            # 纵深防御：绝不允许“行已核销但库存/在途口径被打穿”。
            if lane.in_transit < 0 or lane.stock < 0 or lane.stock > lane.capacity:
                raise RuleError(
                    f"货道 {lane.slot_no} 核销后口径非法：库存 {lane.stock}、在途 {lane.in_transit}"
                )

        order.lines_json = json.dumps(summarize_stored(new_lines), ensure_ascii=False)
        order.status = new_status  # 只能是 active / completed，且由行态推导
        db.commit()
    except RuleError:
        db.rollback()  # 丢弃会话内任何半成品改动
        raise
    except Exception:
        db.rollback()
        raise
    db.refresh(order)
    return order


def void_order(db: Session, order_id: int) -> RefillOrder:
    """作废整单：释放全部仍待核销行的在途占用；已核销行与库存不动。"""
    order = _load_order(db, order_id)
    _assert_active(order)

    stored = json.loads(order.lines_json)
    lines = stored.get("lines", [])
    lane_ids_in_order = [int(l["lane_id"]) for l in lines]
    lanes = _lock_lanes(db, lane_ids_in_order)
    lane_states = {lid: {"in_transit": lane.in_transit} for lid, lane in lanes.items()}

    try:
        new_lines, changes = plan_void(lines, lane_states)
        for lane_id, ch in changes.items():
            lanes[lane_id].in_transit += ch.transit_delta

        order.lines_json = json.dumps(summarize_stored(new_lines), ensure_ascii=False)
        order.status = ORDER_STATUS_VOIDED
        db.commit()
    except Exception:
        db.rollback()
        raise
    db.refresh(order)
    return order
