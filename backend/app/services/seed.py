from datetime import datetime, timedelta
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from app.models.models import Lane, Location, Sale
from app.services.fill_engine import create_refill_order, verify_lines


def seed_if_empty(db: Session) -> None:
    if (db.scalar(select(func.count()).select_from(Location)) or 0) > 0:
        return
    loc = Location(code="VM-01", name="地铁口 A 点位", address="城东地铁 1 号口")
    db.add(loc); db.flush()
    lanes = [
        ("A1", "矿泉水", 20, 5, 0),
        ("A2", "可乐", 18, 18, 0),
        ("B1", "薯片", 12, 3, 2),
        ("B2", "巧克力", 15, 10, 5),
        ("C1", "能量棒", 10, 0, 0),
        ("C2", "口香糖", 24, 24, 2),
    ]
    lane_by_slot: dict[str, Lane] = {}
    for slot, sku, cap, stock, transit in lanes:
        lane = Lane(location_id=loc.id, slot_no=slot, sku_name=sku,
                    capacity=cap, stock=stock, in_transit=transit)
        db.add(lane); db.flush()
        lane_by_slot[slot] = lane
    now = datetime(2026, 9, 16, 12, 0, 0)
    for i, lane in enumerate(lane_by_slot.values()):
        db.add(Sale(lane_id=lane.id, qty=2 + i, sold_at=now - timedelta(hours=i)))
    db.commit()

    # 分批到货种子：建单占用在途 → 先核销 B1 一行（库存已加、在途已扣），
    # A1/C1 仍待核销继续占用在途，整单保持 draft 未完成。
    order = create_refill_order(db, loc.id)
    verify_lines(db, order["id"], lane_ids=[lane_by_slot["B1"].id])
