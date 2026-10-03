import pytest

from app.services.fill_engine import (
    LINE_PENDING, LINE_VERIFIED, LINE_VOIDED,
    ORDER_ACTIVE, ORDER_COMPLETED,
    RuleError, order_completed, plan_batch_verify, plan_void,
)


def _lines():
    return [
        {"lane_id": 1, "slot_no": "A1", "fill_qty": 15, "status": "need_fill",
         "line_status": LINE_PENDING},
        {"lane_id": 2, "slot_no": "A2", "fill_qty": 0, "status": "full",
         "line_status": LINE_PENDING},
        {"lane_id": 3, "slot_no": "B1", "fill_qty": 7, "status": "need_fill",
         "line_status": LINE_PENDING},
    ]


def _states():
    return {1: {"in_transit": 15, "stock": 5}, 2: {"in_transit": 0, "stock": 18},
            3: {"in_transit": 9, "stock": 3}}


def test_partial_verify_only_selected_lines_move():
    lines, changes, status = plan_batch_verify(_lines(), _states(), {3})
    # 入选行：加库存、扣在途
    assert changes[3].stock_delta == 7
    assert changes[3].transit_delta == -7
    # 未选中行：原样 pending，且不产出货道变更
    assert lines[0]["line_status"] == LINE_PENDING
    assert 1 not in changes
    # 仍有正补量待核销 → 整单不能完成
    assert status == ORDER_ACTIVE
    assert not order_completed(lines)
    # 入参不被改写
    assert _lines()[2]["line_status"] == LINE_PENDING


def test_completed_only_when_all_positive_lines_verified():
    lines, _c, status = plan_batch_verify(_lines(), _states(), {3})
    assert status == ORDER_ACTIVE
    lines, changes, status = plan_batch_verify(lines, {**_states(), 3: {"in_transit": 2, "stock": 10}}, {1})
    assert changes[1].stock_delta == 15
    assert status == ORDER_COMPLETED
    assert order_completed(lines)
    # 补量为 0 的满仓行不参与完成判定
    assert lines[1]["line_status"] == LINE_PENDING


def test_batch_rejects_empty_selection():
    with pytest.raises(RuleError):
        plan_batch_verify(_lines(), _states(), set())


def test_batch_rejects_already_verified_line():
    lines, _c, _s = plan_batch_verify(_lines(), _states(), {3})
    with pytest.raises(RuleError, match="已核销"):
        plan_batch_verify(lines, _states(), {3})


def test_batch_rejects_zero_fill_line():
    with pytest.raises(RuleError, match="补量为 0"):
        plan_batch_verify(_lines(), _states(), {2})


def test_batch_rejects_unknown_lane():
    with pytest.raises(RuleError, match="不属于"):
        plan_batch_verify(_lines(), _states(), {99})


def test_batch_rejects_when_in_transit_insufficient_and_produces_no_changes():
    states = _states()
    states[3]["in_transit"] = 6  # 待核销 7，实际在途 6
    with pytest.raises(RuleError, match="在途不足"):
        plan_batch_verify(_lines(), states, {3})


def test_mixed_batch_all_rejected_if_any_line_invalid():
    """一批两行，其中一行在途不足：整批拒绝，不返回任何半成品计划。"""
    states = _states()
    states[1]["in_transit"] = 0  # A1 在途不足，B1 本身合法
    with pytest.raises(RuleError, match="A1"):
        plan_batch_verify(_lines(), states, {1, 3})


def test_plan_void_releases_only_pending_lines():
    lines, _c, _s = plan_batch_verify(_lines(), _states(), {3})  # B1 已核销
    new_lines, changes = plan_void(lines, _states())
    # 待核销正补行 A1 释放；已核销 B1 与满仓 A2 不动
    assert changes[1].transit_delta == -15
    assert changes[1].stock_delta == 0
    assert 3 not in changes and 2 not in changes
    statuses = {l["lane_id"]: l["line_status"] for l in new_lines}
    assert statuses == {1: LINE_VOIDED, 2: LINE_PENDING, 3: LINE_VERIFIED}
