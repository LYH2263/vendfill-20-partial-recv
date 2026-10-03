"""验收叙述的端到端版本：走真实 seed_if_empty + HTTP，B1 先核销、剩余后核销、注入失败回滚。"""
import json

from sqlalchemy import select

from app.models.models import Lane, RefillOrder
from app.services.seed import seed_if_empty
from app.services import refill_service as svc
from app.services.fill_engine import RuleError


def _lane_map(db):
    return {l.slot_no: l for l in db.scalars(select(Lane)).all()}


def test_acceptance_narrative_with_real_seed(db):
    seed_if_empty(db)
    lanes = _lane_map(db)
    loc_id = lanes["B1"].location_id

    # 开单预占在途
    order, created = svc.create_or_get_active_order(db, loc_id)
    assert created is True
    assert lanes["B1"].in_transit == 2 + 7
    assert lanes["A1"].in_transit == 15 and lanes["C1"].in_transit == 10
    assert lanes["A2"].in_transit == 0 and lanes["B2"].in_transit == 5  # 满仓行不预占
    assert lanes["C2"].in_transit == 2                                  # 超占行不预占
    assert order.status == "active"

    # 第一批：只核销 B1
    svc.verify_lines(db, order.id, [lanes["B1"].id])
    order = db.get(RefillOrder, order.id)
    assert (lanes["B1"].stock, lanes["B1"].in_transit) == (10, 2)
    assert order.status == "active"
    statuses = {l["slot_no"]: l["line_status"]
                for l in json.loads(order.lines_json)["lines"]}
    assert statuses["B1"] == "verified"
    assert statuses["A1"] == "pending" and statuses["C1"] == "pending"

    # 第二批：剩余正补量行
    svc.verify_lines(db, order.id, [lanes["A1"].id, lanes["C1"].id])
    order = db.get(RefillOrder, order.id)
    assert order.status == "completed"
    assert (lanes["A1"].stock, lanes["A1"].in_transit) == (20, 0)
    assert (lanes["C1"].stock, lanes["C1"].in_transit) == (10, 0)
    # 汇总口径
    payload = svc.order_payload(db, order)
    assert payload["all_verified"] and payload["verified_count"] == 3
    assert payload["pending_count"] == 0

    # 已完成整单再核任何行必须拒绝
    try:
        svc.verify_lines(db, order.id, [lanes["B1"].id])
        assert False, "应当拒绝"
    except RuleError as e:
        assert "整单已完成" in str(e)


def test_acceptance_narrative_injected_failure_full_rollback(db, monkeypatch):
    seed_if_empty(db)
    lanes = _lane_map(db)
    order, _ = svc.create_or_get_active_order(db, lanes["B1"].location_id)
    svc.verify_lines(db, order.id, [lanes["B1"].id])

    # 批前快照
    before = {s: (l.stock, l.in_transit) for s, l in lanes.items()}
    json_before = db.get(RefillOrder, order.id).lines_json

    def boom():
        raise RuntimeError("注入：提交失败")
    monkeypatch.setattr(db, "commit", boom)
    try:
        svc.verify_lines(db, order.id, [lanes["A1"].id, lanes["C1"].id])
        assert False, "应当抛出注入错误"
    except RuntimeError:
        pass

    # 汇总与货道回到批前
    for slot, lane in lanes.items():
        assert (lane.stock, lane.in_transit) == before[slot], slot
    order_after = db.get(RefillOrder, order.id)
    assert order_after.status == "active"
    assert order_after.lines_json == json_before
