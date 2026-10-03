"""补货建议 + 分批到货按行核销，所有写操作共用同一套原子规则。

规则（建单 / 核销 / 作废均在单事务内完成，任一不满足则整体回滚）：

1. 建单：仅正补量行（fill_qty > 0）按补量占用货道在途（in_transit += fill_qty），
   行状态待核销，整单 draft。同一点位只允许存在一张进行中的草稿单（重复建单返回原单）。
2. 核销（可分批、可只勾选部分行）：
   - 整单 completed / voided 后再核销任何行 → 拒绝。
   - 入选行：库存 += 补量、在途 -= 补量、行置 verified；未入选行一律不动，
     仍按其补量继续占用在途口径。
   - 全部正补量行均已核销 → 整单 completed；否则保持 draft。
     不可能出现「整单完成但仍有待核销行」或「行已核销但库存未加」。
   - 本批任一行失败（含 fail_after 注入失败）→ 本批所有入选行回滚，
     未入选行同样不变，汇总与货道全部回到批前。
3. 作废：释放所有仍待核销行占用的在途，整单置 voided；已核销行不动。
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.models import Lane, Location, RefillOrder

# ---- 建议层（纯函数，不触库）----------------------------------------------

@dataclass
class FillLine:
    lane_id: int
    slot_no: str
    sku_name: str
    capacity: int
    stock: int
    in_transit: int
    gap: int
    fill_qty: int
    status: str  # need_fill | full | overbooked

def compute_gap(capacity: int, stock: int, in_transit: int) -> int:
    return capacity - stock - in_transit

def build_fill_lines(lanes: list[dict], requested: dict[int, int] | None = None) -> list[FillLine]:
    """requested optional desired fill per lane_id; capped by gap; never negative."""
    lines: list[FillLine] = []
    for lane in lanes:
        gap = compute_gap(int(lane["capacity"]), int(lane["stock"]), int(lane["in_transit"]))
        if gap < 0:
            status = "overbooked"
            fill = 0
        elif gap == 0:
            status = "full"
            fill = 0
        else:
            status = "need_fill"
            desire = gap if requested is None else int(requested.get(lane["id"], gap))
            fill = max(0, min(desire, gap))
        lines.append(FillLine(
            lane_id=lane["id"], slot_no=lane["slot_no"], sku_name=lane["sku_name"],
            capacity=lane["capacity"], stock=lane["stock"], in_transit=lane["in_transit"],
            gap=gap, fill_qty=fill, status=status,
        ))
    return lines

def summarize(lines: list[FillLine]) -> dict:
    return {
        "total_fill": sum(l.fill_qty for l in lines),
        "need_fill_count": sum(1 for l in lines if l.status == "need_fill"),
        "full_count": sum(1 for l in lines if l.status == "full"),
        "overbooked_count": sum(1 for l in lines if l.status == "overbooked"),
        "lines": [asdict(l) for l in lines],
    }

# ---- 原子持久化层（行状态 / 库存 / 在途 / 整单态 / 汇总，同一事务）---------

ORDER_DRAFT = "draft"
ORDER_COMPLETED = "completed"
ORDER_VOIDED = "voided"

LINE_NONE = "none"          # 零补量行（满仓/超占），不参与核销
LINE_PENDING = "pending"    # 待核销：补量仍占用在途
LINE_VERIFIED = "verified"  # 已核销：补量已入库存
LINE_VOIDED = "voided"      # 随整单作废：占用的在途已释放


class RefillError(Exception):
    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.message = message


def _lane_payload(lanes) -> list[dict]:
    return [{"id": l.id, "slot_no": l.slot_no, "sku_name": l.sku_name,
             "capacity": l.capacity, "stock": l.stock, "in_transit": l.in_transit}
            for l in lanes]


def _build_snapshot(lanes) -> list[dict]:
    """建单瞬间的不可变建议；base_stock/base_in_transit 是一致性对账基准。"""
    snapshot: list[dict] = []
    for fl in build_fill_lines(_lane_payload(lanes)):
        row = asdict(fl)
        row["verified"] = False
        row["base_stock"] = fl.stock
        row["base_in_transit"] = fl.in_transit
        snapshot.append(row)
    return snapshot


def _line_status(row: dict, order_status: str) -> str:
    if row["verified"]:
        return LINE_VERIFIED
    if order_status == ORDER_VOIDED:
        return LINE_VOIDED
    return LINE_PENDING if int(row["fill_qty"]) > 0 else LINE_NONE


def order_to_dict(db: Session, order: RefillOrder) -> dict:
    """汇总与行视图统一从「单据快照 + 货道实时值」派生，任何地方都不另搞一套口径。"""
    snapshot = json.loads(order.lines_json or "[]")
    lane_ids = [r["lane_id"] for r in snapshot]
    lanes = {l.id: l for l in
             db.scalars(select(Lane).where(Lane.id.in_(lane_ids))).all()} if lane_ids else {}

    lines: list[dict] = []
    for r in snapshot:
        lane = lanes.get(r["lane_id"])
        line_status = _line_status(r, order.status)
        lines.append({
            "lane_id": r["lane_id"],
            "slot_no": r["slot_no"],
            "sku_name": r["sku_name"],
            "capacity": r["capacity"],
            "stock": lane.stock if lane else r["stock"],
            "in_transit": lane.in_transit if lane else r["in_transit"],
            "gap": r["gap"],
            "fill_qty": r["fill_qty"],
            "status": r["status"],          # 建议态：need_fill / full / overbooked
            "verified": bool(r["verified"]),
            "line_status": line_status,     # 核销态：pending / verified / voided / none
            "base_stock": r.get("base_stock", r["stock"]),
            "base_in_transit": r.get("base_in_transit", r["in_transit"]),
        })

    positive = [l for l in lines if l["fill_qty"] > 0]
    verified = [l for l in positive if l["line_status"] == LINE_VERIFIED]
    pending = [l for l in positive if l["line_status"] == LINE_PENDING]
    released = [l for l in positive if l["line_status"] == LINE_VOIDED]
    return {
        "id": order.id,
        "location_id": order.location_id,
        "order_status": order.status,
        "created_at": order.created_at.isoformat() if order.created_at else None,
        # 建议口径（建单时定格）
        "total_fill": sum(l["fill_qty"] for l in lines),
        "need_fill_count": sum(1 for l in lines if l["status"] == "need_fill"),
        "full_count": sum(1 for l in lines if l["status"] == "full"),
        "overbooked_count": sum(1 for l in lines if l["status"] == "overbooked"),
        # 核销口径（随原子核销推进）
        "total_line_count": len(positive),
        "verified_count": len(verified),
        "pending_count": len(pending),
        "verified_fill": sum(l["fill_qty"] for l in verified),
        "pending_fill": sum(l["fill_qty"] for l in pending),
        "released_fill": sum(l["fill_qty"] for l in released),
        "completed": order.status == ORDER_COMPLETED,
        "voided": order.status == ORDER_VOIDED,
        "lines": lines,
    }


def _check_consistency(snapshot: list[dict], lanes: dict[int, Lane], order_status: str) -> None:
    """不变量：待核销行的补量必须在在途里，已核销行的补量必须在库存里。

    库存 == 建单基准 + (已核销 ? 补量 : 0)
    在途 == 建单基准 + (待核销 ? 补量 : 0)
    """
    for r in snapshot:
        lane = lanes.get(r["lane_id"])
        if lane is None:
            raise RefillError(409, f"货道 {r['slot_no']} 已不存在，数据不一致")
        qty = int(r["fill_qty"])
        is_verified = bool(r["verified"])
        is_pending = qty > 0 and not is_verified and order_status != ORDER_VOIDED
        exp_stock = int(r["base_stock"]) + (qty if is_verified else 0)
        exp_transit = int(r["base_in_transit"]) + (qty if is_pending else 0)
        if lane.stock != exp_stock or lane.in_transit != exp_transit:
            raise RefillError(
                409,
                f"货道 {r['slot_no']} 库存/在途与单据口径不一致："
                f"库存 {lane.stock}!={exp_stock}，在途 {lane.in_transit}!={exp_transit}",
            )


def get_order(db: Session, order_id: int) -> RefillOrder:
    order = db.get(RefillOrder, order_id)
    if order is None:
        raise RefillError(404, "补货单不存在")
    return order


def create_refill_order(db: Session, location_id: int) -> dict:
    """建单即占在途。同点位已有草稿单时幂等返回该单（保证在途口径只有一套）。"""
    loc = db.get(Location, location_id)
    if loc is None:
        raise RefillError(404, "点位不存在")
    existing = db.scalars(
        select(RefillOrder)
        .where(RefillOrder.location_id == location_id, RefillOrder.status == ORDER_DRAFT)
        .order_by(RefillOrder.id.desc())
    ).first()
    if existing is not None:
        return order_to_dict(db, existing)

    lanes = list(db.scalars(
        select(Lane).where(Lane.location_id == location_id).order_by(Lane.slot_no)
    ).all())
    snapshot = _build_snapshot(lanes)
    for lane, row in zip(lanes, snapshot):
        if row["fill_qty"] > 0:
            lane.in_transit += row["fill_qty"]  # 建单即占用在途

    order = RefillOrder(location_id=location_id, created_at=datetime.utcnow(),
                        status=ORDER_DRAFT, lines_json=json.dumps(snapshot, ensure_ascii=False))
    db.add(order)
    try:
        db.commit()
    except Exception:
        db.rollback()
        raise
    db.refresh(order)
    return order_to_dict(db, order)


def verify_lines(db: Session, order_id: int,
                 lane_ids: list[int] | set[int] | None = None,
                 fail_after: int | None = None) -> dict:
    """按行分批核销，整批一个事务：任一入选行失败 → 本批全部回滚。

    lane_ids 为 None 时核销该单全部待核销的正补量行。
    fail_after 仅用于故障演练：成功应用前 N 行后、提交前立即抛错
    （0 表示首行前即失败；N ≥ 本批行数时全部行已改但仍不提交），
    调用方可据此观察批内全回滚；生产调用不要传。
    """
    order = db.scalars(
        select(RefillOrder).where(RefillOrder.id == order_id).with_for_update()
    ).first()
    if order is None:
        raise RefillError(404, "补货单不存在")
    if order.status == ORDER_COMPLETED:
        raise RefillError(409, "整单已完成，禁止再核销任何行")
    if order.status == ORDER_VOIDED:
        raise RefillError(409, "补货单已作废，禁止核销")

    snapshot = json.loads(order.lines_json or "[]")
    by_lane = {int(r["lane_id"]): r for r in snapshot}

    if lane_ids is None:
        selected = [lid for lid, r in by_lane.items()
                    if int(r["fill_qty"]) > 0 and not r["verified"]]
    else:
        selected = list(dict.fromkeys(int(x) for x in lane_ids))  # 去重保序
        if not selected:
            raise RefillError(400, "未勾选任何核销行")
        for lid in selected:
            row = by_lane.get(lid)
            if row is None:
                raise RefillError(400, f"货道 {lid} 不在本补货单中")
            if int(row["fill_qty"]) <= 0:
                raise RefillError(400, f"货道 {row['slot_no']} 补量为 0，无需核销")
            if row["verified"]:
                raise RefillError(409, f"货道 {row['slot_no']} 已核销，不能重复核销")

    if not selected:
        raise RefillError(400, "本单已无待核销行")

    lanes = {l.id: l for l in db.scalars(
        select(Lane).where(Lane.id.in_(selected)).with_for_update()
    ).all()}

    applied = 0
    try:
        if fail_after is not None and fail_after <= 0:
            raise RuntimeError("注入失败：首批入选行核销前中断")
        for lid in selected:
            lane = lanes.get(lid)
            row = by_lane[lid]
            qty = int(row["fill_qty"])
            if lane is None:
                raise RefillError(400, f"货道 {row['slot_no']} 不存在")
            if lane.in_transit < qty:
                raise RefillError(409, f"货道 {lane.slot_no} 在途不足 {qty}，核销将导致在途为负")

            lane.in_transit -= qty   # 扣在途
            lane.stock += qty        # 加库存
            row["verified"] = True   # 行置已核销
            applied += 1
            if fail_after is not None and applied >= fail_after:
                raise RuntimeError(f"注入失败：第 {applied} 行已应用、提交前中断")

        # 整单完成态与行状态同一事务落定：全部正补量行已核销才完成
        if all(r["verified"] for r in snapshot if int(r["fill_qty"]) > 0):
            order.status = ORDER_COMPLETED
        order.lines_json = json.dumps(snapshot, ensure_ascii=False)

        all_lanes = {l.id: l for l in db.scalars(
            select(Lane).where(Lane.id.in_(list(by_lane.keys())))
        ).all()}
        _check_consistency(snapshot, all_lanes, order.status)
        db.commit()
    except Exception:
        db.rollback()  # 本批入选行回滚，未入选行本就未动，汇总/货道回到批前
        raise

    db.refresh(order)
    return order_to_dict(db, order)


def void_order(db: Session, order_id: int) -> dict:
    """作废整单：释放全部仍待核销行占用的在途；已核销行保留。"""
    order = db.scalars(
        select(RefillOrder).where(RefillOrder.id == order_id).with_for_update()
    ).first()
    if order is None:
        raise RefillError(404, "补货单不存在")
    if order.status == ORDER_VOIDED:
        raise RefillError(409, "补货单已作废，不能重复作废")
    if order.status == ORDER_COMPLETED:
        raise RefillError(409, "整单已完成，禁止作废")

    snapshot = json.loads(order.lines_json or "[]")
    pending_ids = [int(r["lane_id"]) for r in snapshot
                   if int(r["fill_qty"]) > 0 and not r["verified"]]
    lanes = {l.id: l for l in db.scalars(
        select(Lane).where(Lane.id.in_(pending_ids)).with_for_update()
    ).all()} if pending_ids else {}
    try:
        for r in snapshot:
            if int(r["fill_qty"]) <= 0 or r["verified"]:
                continue
            lane = lanes.get(int(r["lane_id"]))
            if lane is None:
                raise RefillError(409, f"货道 {r['slot_no']} 不存在，无法释放在途")
            if lane.in_transit < int(r["fill_qty"]):
                raise RefillError(409, f"货道 {lane.slot_no} 在途不足，作废将导致在途为负")
            lane.in_transit -= int(r["fill_qty"])
        order.status = ORDER_VOIDED
        db.commit()
    except Exception:
        db.rollback()
        raise
    db.refresh(order)
    return order_to_dict(db, order)
