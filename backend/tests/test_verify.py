import json

import pytest

from app.models.models import Lane, Location, RefillOrder
from app.services.fill_engine import (
    ORDER_COMPLETED, ORDER_DRAFT, RefillError, create_refill_order,
    order_to_dict, verify_lines, void_order,
)
from app.services.seed import seed_if_empty


def _lanes(db):
    return {l.slot_no: l for l in db.query(Lane).order_by(Lane.slot_no).all()}


def _seeded_location(db):
    seed_if_empty(db)
    loc_id = db.query(Lane).first().location_id
    order = db.query(RefillOrder).order_by(RefillOrder.id.desc()).first()
    return loc_id, order


# ---------- 种子场景：B1 已核销，其它正补量行仍待核销，整单未完成 ----------

def test_seed_b1_verified_rest_pending_order_not_completed(db):
    _loc_id, order = _seeded_location(db)
    assert order.status == ORDER_DRAFT
    data = order_to_dict(db, order)

    by_slot = {l["slot_no"]: l for l in data["lines"]}
    assert by_slot["B1"]["line_status"] == "verified"
    assert by_slot["A1"]["line_status"] == "pending"
    assert by_slot["C1"]["line_status"] == "pending"
    # 零补量行不参与核销
    assert by_slot["A2"]["line_status"] == "none"
    assert by_slot["B2"]["line_status"] == "none"
    assert by_slot["C2"]["line_status"] == "none"

    assert data["verified_count"] == 1
    assert data["pending_count"] == 2
    assert data["completed"] is False

    lanes = _lanes(db)
    # B1：补量 7 已入库存（3+7=10），在途回到建单前口径（2）
    assert lanes["B1"].stock == 10
    assert lanes["B1"].in_transit == 2
    # A1 补 15、C1 补 10 仍占用在途，库存未加
    assert lanes["A1"].stock == 5 and lanes["A1"].in_transit == 15
    assert lanes["C1"].stock == 0 and lanes["C1"].in_transit == 10
    # 满仓/超占行货道值未被动过
    assert lanes["A2"].stock == 18 and lanes["A2"].in_transit == 0
    assert lanes["B2"].stock == 10 and lanes["B2"].in_transit == 5
    assert lanes["C2"].stock == 24 and lanes["C2"].in_transit == 2


# ---------- 剩余行核销后整单完成，汇总与货道一致 ----------

def test_verify_remaining_lines_completes_order(db):
    loc_id, order = _seeded_location(db)

    data = verify_lines(db, order.id)  # lane_ids=None → 全部待核销行

    assert data["order_status"] == ORDER_COMPLETED
    assert data["completed"] is True
    assert data["pending_count"] == 0
    assert data["verified_count"] == 3
    # 整单完成时不存在任何待核销的正补量行
    assert all(l["line_status"] != "pending" for l in data["lines"])

    # 统一不变量对账（含上一批已核销的 B1）：
    # 已核销行 库存 == 建单基准 + 补量，在途 == 建单基准
    lanes = _lanes(db)
    for row in data["lines"]:
        lane = lanes[row["slot_no"]]
        if row["fill_qty"] > 0:
            assert lane.stock == row["base_stock"] + row["fill_qty"], row["slot_no"]
            assert lane.in_transit == row["base_in_transit"], row["slot_no"]
    assert lanes["A1"].stock == 20 and lanes["A1"].in_transit == 0
    assert lanes["B1"].stock == 10 and lanes["B1"].in_transit == 2
    assert lanes["C1"].stock == 10 and lanes["C1"].in_transit == 0


def test_partial_batch_keeps_unselected_lines_untouched(db):
    _loc_id, order = _seeded_location(db)
    lanes = _lanes(db)
    a1_id, c1_id = lanes["A1"].id, lanes["C1"].id
    a1_before = (lanes["A1"].stock, lanes["A1"].in_transit)
    c1_before = (lanes["C1"].stock, lanes["C1"].in_transit)

    # 只勾选 A1；C1 不入选，必须保持待核销并继续占用在途
    data = verify_lines(db, order.id, lane_ids=[a1_id])
    assert data["order_status"] == ORDER_DRAFT
    by_slot = {l["slot_no"]: l for l in data["lines"]}
    assert by_slot["A1"]["line_status"] == "verified"
    assert by_slot["C1"]["line_status"] == "pending"

    db.expire_all()
    lanes = _lanes(db)
    assert (lanes["A1"].stock, lanes["A1"].in_transit) == (a1_before[0] + 15, a1_before[1] - 15)
    assert (lanes["C1"].stock, lanes["C1"].in_transit) == c1_before


# ---------- 注入失败：批内全回滚，未选中行不动，汇总与货道回到批前 ----------

def test_injected_failure_rolls_back_whole_batch(db):
    _loc_id, order = _seeded_location(db)
    lanes = _lanes(db)
    a1_id, c1_id = lanes["A1"].id, lanes["C1"].id
    snapshot_before = {s: (l.stock, l.in_transit) for s, l in lanes.items()}

    # 勾选 A1+C1，应用 1 行后注入失败：A1 已改也要回滚
    with pytest.raises(RuntimeError):
        verify_lines(db, order.id, lane_ids=[a1_id, c1_id], fail_after=1)

    db.expire_all()
    order = db.get(RefillOrder, order.id)
    assert order.status == ORDER_DRAFT
    # 货道全部回到批前
    for slot, (stock, transit) in snapshot_before.items():
        lane = _lanes(db)[slot]
        assert lane.stock == stock and lane.in_transit == transit, slot
    # 单据行状态回到批前（A1/C1 仍待核销；此前已核销的 B1 不变）
    rows = {r["slot_no"]: r for r in json.loads(order.lines_json)}
    assert rows["A1"]["verified"] is False
    assert rows["C1"]["verified"] is False
    assert rows["B1"]["verified"] is True
    # 汇总口径一致
    data = order_to_dict(db, order)
    assert data["verified_count"] == 1 and data["pending_count"] == 2

    # fail_after=0：首行前失败，什么都不发生
    with pytest.raises(RuntimeError):
        verify_lines(db, order.id, lane_ids=[a1_id], fail_after=0)
    db.expire_all()
    for slot, (stock, transit) in snapshot_before.items():
        lane = _lanes(db)[slot]
        assert lane.stock == stock and lane.in_transit == transit, slot

    # 回滚后可正常核销剩余行并完成整单
    data = verify_lines(db, order.id)
    assert data["order_status"] == ORDER_COMPLETED
    assert data["pending_count"] == 0


# ---------- 非法核销一律拒绝 ----------

def test_completed_order_rejects_any_verify(db):
    _loc_id, order = _seeded_location(db)
    verify_lines(db, order.id)  # 全部核销 → 完成
    with pytest.raises(RefillError) as ei:
        verify_lines(db, order.id, lane_ids=[])
    assert ei.value.status_code == 409
    with pytest.raises(RefillError) as ei2:
        verify_lines(db, order.id)
    assert ei2.value.status_code == 409


def test_verify_unknown_or_zero_or_duplicate_line_rejected(db):
    _loc_id, order = _seeded_location(db)
    lanes = _lanes(db)
    # 补量 0 的行不能核销
    with pytest.raises(RefillError):
        verify_lines(db, order.id, lane_ids=[lanes["A2"].id])
    # 不属于本单的货道
    other_loc = Location(code="VM-02", name="别处")
    db.add(other_loc); db.flush()
    other = Lane(location_id=other_loc.id, slot_no="Z9", sku_name="x",
                 capacity=5, stock=0, in_transit=0)
    db.add(other); db.commit()
    with pytest.raises(RefillError):
        verify_lines(db, order.id, lane_ids=[other.id])
    # 重复核销 B1
    with pytest.raises(RefillError):
        verify_lines(db, order.id, lane_ids=[lanes["B1"].id])
    # 失败后整单仍未完成、货道未变
    db.refresh(order)
    assert order.status == ORDER_DRAFT


def test_empty_selection_rejected(db):
    _loc_id, order = _seeded_location(db)
    with pytest.raises(RefillError) as ei:
        verify_lines(db, order.id, lane_ids=[])
    assert ei.value.status_code == 400


# ---------- 作废：释放待核销行在途，已核销行不动 ----------

def test_void_releases_pending_in_transit(db):
    _loc_id, order = _seeded_location(db)
    data = void_order(db, order.id)
    assert data["voided"] is True
    by_slot = {l["slot_no"]: l for l in data["lines"]}
    assert by_slot["A1"]["line_status"] == "voided"
    assert by_slot["C1"]["line_status"] == "voided"
    assert by_slot["B1"]["line_status"] == "verified"
    lanes = _lanes(db)
    assert lanes["A1"].in_transit == 0      # 15 已释放
    assert lanes["C1"].in_transit == 0      # 10 已释放
    assert lanes["B1"].stock == 10          # 已核销不动
    with pytest.raises(RefillError):
        verify_lines(db, order.id)


# ---------- 建单幂等：草稿期重复建单不重复占用在途 ----------

def test_run_is_idempotent_while_draft(db):
    loc_id, first = _seeded_location(db)
    transit_before = {s: l.in_transit for s, l in _lanes(db).items()}
    again = create_refill_order(db, loc_id)
    assert again["id"] == first.id
    assert {s: l.in_transit for s, l in _lanes(db).items()} == transit_before
