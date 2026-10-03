import json

import pytest
from sqlalchemy import select

from app.models.models import Lane, RefillOrder
from app.services import refill_service as svc
from app.services.fill_engine import (
    LINE_PENDING, LINE_VERIFIED, ORDER_ACTIVE, ORDER_COMPLETED, ORDER_VOIDED,
    RuleError,
)


def _lanes(db, ids):
    return {slot: db.get(Lane, lid) for slot, lid in ids.items()}


def _snapshot(db, ids):
    return {slot: (lane.stock, lane.in_transit) for slot, lane in _lanes(db, ids).items()}


def _line_statuses(order):
    data = json.loads(order.lines_json)
    return {l["slot_no"]: l["line_status"] for l in data["lines"]}


def _line_qtys(order):
    data = json.loads(order.lines_json)
    return {l["slot_no"]: l["fill_qty"] for l in data["lines"]}


# --------------------------------------------------------------- 开单预占在途

def test_create_order_preempts_in_transit(db, seeded):
    order, created = svc.create_or_get_active_order(db, seeded["location_id"])
    assert created is True
    lanes = _lanes(db, seeded["lane_ids"])
    qtys = _line_qtys(order)
    # gap: A1=15, B1=7, C1=10；满仓/超占行补量 0
    assert qtys == {"A1": 15, "A2": 0, "B1": 7, "B2": 0, "C1": 10, "C2": 0}
    assert lanes["A1"].in_transit == 15
    assert lanes["B1"].in_transit == 2 + 7
    assert lanes["C1"].in_transit == 10
    # 满仓行与超占行不预占
    assert lanes["A2"].in_transit == 0
    assert lanes["B2"].in_transit == 5
    assert lanes["C2"].in_transit == 2
    # 库存开单时不动
    assert lanes["B1"].stock == 3


def test_run_is_idempotent_while_active(db, seeded):
    o1, c1 = svc.create_or_get_active_order(db, seeded["location_id"])
    svc.verify_lines(db, o1.id, [seeded["lane_ids"]["B1"]])
    o2, c2 = svc.create_or_get_active_order(db, seeded["location_id"])
    assert o2.id == o1.id and c1 is True and c2 is False
    # 已核销状态与在途占用不被重置
    statuses = _line_statuses(o2)
    assert statuses["B1"] == LINE_VERIFIED
    assert _lanes(db, seeded["lane_ids"])["B1"].in_transit == 2


# --------------------------------------------------------------- 种子序列：B1 → 剩余

def test_seed_flow_b1_then_rest(db, seeded):
    ids = seeded["lane_ids"]
    order, _ = svc.create_or_get_active_order(db, seeded["location_id"])
    assert order.status == ORDER_ACTIVE

    # 第一批：只核销 B1
    svc.verify_lines(db, order.id, [ids["B1"]])
    order = db.get(RefillOrder, order.id)
    b1 = db.get(Lane, ids["B1"])
    # B1：库存按补量 +7、在途回到开单前 2
    assert (b1.stock, b1.in_transit) == (10, 2)

    statuses = _line_statuses(order)
    assert statuses["B1"] == LINE_VERIFIED
    # 其它正补量行仍待核销，继续按补量占用在途
    for slot in ("A1", "C1"):
        assert statuses[slot] == LINE_PENDING
    assert db.get(Lane, ids["A1"]).in_transit == 15
    assert db.get(Lane, ids["C1"]).in_transit == 10
    # 整单未完成
    assert order.status == ORDER_ACTIVE
    payload = svc.order_payload(db, order)
    assert payload["verified_count"] == 1 and payload["pending_count"] == 2
    assert payload["all_verified"] is False

    # 第二批：核销剩余正补量行
    svc.verify_lines(db, order.id, [ids["A1"], ids["C1"]])
    order = db.get(RefillOrder, order.id)
    assert order.status == ORDER_COMPLETED
    assert (db.get(Lane, ids["A1"]).stock, db.get(Lane, ids["A1"]).in_transit) == (20, 0)
    assert (db.get(Lane, ids["C1"]).stock, db.get(Lane, ids["C1"]).in_transit) == (10, 0)
    payload = svc.order_payload(db, order)
    assert payload["verified_count"] == 3 and payload["pending_count"] == 0
    assert payload["all_verified"] is True


def test_summary_matches_lanes_at_each_step(db, seeded):
    ids = seeded["lane_ids"]
    order, _ = svc.create_or_get_active_order(db, seeded["location_id"])
    svc.verify_lines(db, order.id, [ids["B1"]])
    payload = svc.order_payload(db, db.get(RefillOrder, order.id))
    # 行内的实时货道口径与 lanes 表一致
    by_slot = {l["slot_no"]: l for l in payload["lines"]}
    for slot, lid in ids.items():
        lane = db.get(Lane, lid)
        assert by_slot[slot]["lane_stock"] == lane.stock
        assert by_slot[slot]["lane_in_transit"] == lane.in_transit


# --------------------------------------------------------------- 非法请求拒绝

def test_completed_order_rejects_any_verify(db, seeded):
    ids = seeded["lane_ids"]
    order, _ = svc.create_or_get_active_order(db, seeded["location_id"])
    svc.verify_lines(db, order.id, [ids["A1"], ids["B1"], ids["C1"]])
    assert db.get(RefillOrder, order.id).status == ORDER_COMPLETED
    with pytest.raises(RuleError, match="整单已完成"):
        svc.verify_lines(db, order.id, [ids["B1"]])
    # 货道未被改动
    assert (db.get(Lane, ids["B1"]).stock, db.get(Lane, ids["B1"]).in_transit) == (10, 2)


def test_duplicate_verify_in_batch_rolls_back(db, seeded):
    ids = seeded["lane_ids"]
    order, _ = svc.create_or_get_active_order(db, seeded["location_id"])
    svc.verify_lines(db, order.id, [ids["B1"]])
    # 一批里同时勾选已核销 B1 与未核销 A1：B1 失败，A1 必须一起回滚
    with pytest.raises(RuleError, match="已核销"):
        svc.verify_lines(db, order.id, [ids["B1"], ids["A1"]])
    assert db.get(RefillOrder, order.id).status == ORDER_ACTIVE
    assert _line_statuses(db.get(RefillOrder, order.id))["A1"] == LINE_PENDING
    # A1 库存未加、在途仍占
    assert (db.get(Lane, ids["A1"]).stock, db.get(Lane, ids["A1"]).in_transit) == (5, 15)


def test_empty_selection_rejected(db, seeded):
    order, _ = svc.create_or_get_active_order(db, seeded["location_id"])
    with pytest.raises(RuleError, match="未勾选"):
        svc.verify_lines(db, order.id, [])


def test_zero_fill_line_rejected(db, seeded):
    order, _ = svc.create_or_get_active_order(db, seeded["location_id"])
    with pytest.raises(RuleError, match="补量为 0"):
        svc.verify_lines(db, order.id, [seeded["lane_ids"]["A2"]])


# --------------------------------------------------------------- 提交失败注入：全批回滚

class _CommitBoom(Exception):
    pass


def test_commit_failure_rolls_back_entire_batch(db, seeded, monkeypatch):
    """在提交瞬间注入失败：行状态、库存、在途、整单态全部回到批前。"""
    ids = seeded["lane_ids"]
    order, _ = svc.create_or_get_active_order(db, seeded["location_id"])

    before = _snapshot(db, ids)
    order_json_before = db.get(RefillOrder, order.id).lines_json

    real_commit = db.commit
    calls = {"n": 0}

    def boom_commit():
        calls["n"] += 1
        raise _CommitBoom("injected commit failure")

    monkeypatch.setattr(db, "commit", boom_commit)
    with pytest.raises(_CommitBoom):
        svc.verify_lines(db, order.id, [ids["A1"], ids["B1"], ids["C1"]])

    # 服务层已 rollback；恢复真实 commit 供后续断言使用
    monkeypatch.setattr(db, "commit", real_commit)
    assert calls["n"] == 1

    # 汇总与货道回到批前
    assert _snapshot(db, ids) == before
    order_after = db.get(RefillOrder, order.id)
    assert order_after.status == ORDER_ACTIVE
    assert order_after.lines_json == order_json_before
    statuses = _line_statuses(order_after)
    # 正补量行全部回到待核销；零补量行不参与核销，保持初始 pending
    assert {s for s in ("A1", "B1", "C1") if statuses[s] == LINE_PENDING} == {"A1", "B1", "C1"}
    assert {s for s, v in statuses.items() if v == LINE_VERIFIED} == set()


def test_rule_failure_mid_batch_leaves_no_trace(db, seeded):
    """校验阶段失败（在途被外部打穿）：未 commit，库里无任何痕迹。"""
    ids = seeded["lane_ids"]
    order, _ = svc.create_or_get_active_order(db, seeded["location_id"])
    before = _snapshot(db, ids)

    # 直接把 A1 在途占用抹掉，模拟口径被打穿；勾选 A1+B1 应整体拒绝
    db.get(Lane, ids["A1"]).in_transit = 0
    db.commit()
    with pytest.raises(RuleError, match="在途不足"):
        svc.verify_lines(db, order.id, [ids["A1"], ids["B1"]])

    # B1 是本批合法行，但也不得落库
    after = _snapshot(db, ids)
    assert after["B1"] == before["B1"]  # (3, 9)
    assert after["A1"] == (5, 0)        # 非法行同样没动
    assert _line_statuses(db.get(RefillOrder, order.id))["B1"] == LINE_PENDING


# --------------------------------------------------------------- 作废

def test_void_releases_pending_transit(db, seeded):
    ids = seeded["lane_ids"]
    order, _ = svc.create_or_get_active_order(db, seeded["location_id"])
    svc.verify_lines(db, order.id, [ids["B1"]])
    svc.void_order(db, order.id)
    order = db.get(RefillOrder, order.id)
    assert order.status == ORDER_VOIDED
    # 待核销行预占释放，回到开单前口径
    assert db.get(Lane, ids["A1"]).in_transit == 0
    assert db.get(Lane, ids["C1"]).in_transit == 0
    # 已核销 B1 库存/在途不受影响
    assert (db.get(Lane, ids["B1"]).stock, db.get(Lane, ids["B1"]).in_transit) == (10, 2)
    with pytest.raises(RuleError, match="已作废"):
        svc.verify_lines(db, order.id, [ids["A1"]])


def test_new_order_after_completion_preempts_again(db, seeded):
    ids = seeded["lane_ids"]
    order, _ = svc.create_or_get_active_order(db, seeded["location_id"])
    svc.verify_lines(db, order.id, [ids["A1"], ids["B1"], ids["C1"]])
    # 完成单不再作为 active 返回；但点位已满仓，重新开单应被拒绝
    with pytest.raises(RuleError, match="无需补货"):
        svc.create_or_get_active_order(db, seeded["location_id"])
    assert db.scalar(select(RefillOrder).where(RefillOrder.status == ORDER_COMPLETED)) is not None
