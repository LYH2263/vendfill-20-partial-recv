"""Vending refill core rules.

gap = capacity - stock - in_transit; fills capped by gap; no negative fills.

核销原子规则（唯一事实来源，纯函数、不依赖 DB）：
- 行态：pending（待核销，补量继续占用在途）→ verified（已核销，补量已加库存、已扣在途）；
  作废单的待核销行为 voided。
- 整单完成态只能由行态推导：存在正补量行且全部 verified 才是 completed，否则 active。
- 分批核销：仅勾选行变动；任一入选行不合法（已核销/补量 0/货道在途不足）则整批拒绝，
  调用方据此回滚事务，未选中行与货道不动。
"""
from __future__ import annotations
from dataclasses import asdict, dataclass

# ---- 行核销态 ----
LINE_PENDING = "pending"
LINE_VERIFIED = "verified"
LINE_VOIDED = "voided"

# ---- 整单生命周期 ----
ORDER_ACTIVE = "active"
ORDER_COMPLETED = "completed"
ORDER_VOIDED = "voided"


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
    line_status: str = LINE_PENDING  # pending | verified | voided


@dataclass
class LaneChange:
    lane_id: int
    stock_delta: int
    transit_delta: int


class RuleError(ValueError):
    """领域规则冲突；调用方必须放弃本批（回滚事务）。"""


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


# ---------------------------------------------------------------- 核销纯规则

def positive_lines(lines: list[dict]) -> list[dict]:
    """正补量行：占用在途、需要到货核销、参与整单完成态判定的行。"""
    return [l for l in lines if int(l.get("fill_qty", 0)) > 0]


def order_completed(lines: list[dict]) -> bool:
    """整单完成态的唯一推导：至少一行正补量，且全部已核销。"""
    pending = positive_lines(lines)
    return bool(pending) and all(l.get("line_status", LINE_PENDING) == LINE_VERIFIED for l in pending)


def line_open(line: dict) -> bool:
    """该行是否仍可核销（正补量且待核销）。"""
    return int(line.get("fill_qty", 0)) > 0 and line.get("line_status", LINE_PENDING) == LINE_PENDING


def plan_batch_verify(
    lines: list[dict],
    lane_states: dict[int, dict],
    selected_ids: set[int] | list[int],
) -> tuple[list[dict], dict[int, LaneChange], str]:
    """对一批勾选行做核销规划（纯函数，不改入参）。

    入选行：补量加库存、扣在途、行态置 verified；未选中行原样保留。
    任一行不合法即抛 RuleError —— 不产出任何半成品计划，调用方必须整批回滚。

    lane_states: {lane_id: {"in_transit": int, ...}}，以数据库实时口径为准。
    返回 (新行列表, {lane_id: LaneChange}, 新整单状态)。
    """
    selected = {int(x) for x in selected_ids}
    if not selected:
        raise RuleError("未勾选任何核销行")

    by_id = {int(l["lane_id"]): l for l in lines}
    new_lines = [dict(l) for l in lines]
    new_by_id = {int(l["lane_id"]): l for l in new_lines}
    changes: dict[int, LaneChange] = {}

    # 先全量校验，再产出变更 —— 校验阶段不允许任何副作用外逃
    for lane_id in sorted(selected):
        line = by_id.get(lane_id)
        if line is None:
            raise RuleError(f"货道 {lane_id} 不属于本补货单")
        qty = int(line["fill_qty"])
        ls = line.get("line_status", LINE_PENDING)
        if qty <= 0:
            raise RuleError(f"货道 {line.get('slot_no', lane_id)} 补量为 0，无需核销")
        if ls == LINE_VERIFIED:
            raise RuleError(f"货道 {line.get('slot_no', lane_id)} 已核销，禁止重复核销")
        if ls == LINE_VOIDED:
            raise RuleError(f"货道 {line.get('slot_no', lane_id)} 已随单作废")
        state = lane_states.get(lane_id)
        if state is None:
            raise RuleError(f"货道 {lane_id} 不存在或不在本点位")
        in_transit = int(state["in_transit"])
        if in_transit < qty:
            raise RuleError(
                f"货道 {line.get('slot_no', lane_id)} 在途不足：待核销 {qty}，实际在途 {in_transit}"
            )

    for lane_id in selected:
        qty = int(by_id[lane_id]["fill_qty"])
        changes[lane_id] = LaneChange(lane_id=lane_id, stock_delta=qty, transit_delta=-qty)
        new_by_id[lane_id]["line_status"] = LINE_VERIFIED

    new_status = ORDER_COMPLETED if order_completed(new_lines) else ORDER_ACTIVE
    return new_lines, changes, new_status


def plan_void(
    lines: list[dict],
    lane_states: dict[int, dict],
) -> tuple[list[dict], dict[int, LaneChange]]:
    """作废整单：所有仍待核销的正补量行释放在途占用并置 voided；已核销行不动。"""
    new_lines = [dict(l) for l in lines]
    changes: dict[int, LaneChange] = {}
    for line in new_lines:
        if not line_open(line):
            continue
        lane_id = int(line["lane_id"])
        qty = int(line["fill_qty"])
        state = lane_states.get(lane_id)
        if state is None:
            raise RuleError(f"货道 {lane_id} 不存在或不在本点位")
        if int(state["in_transit"]) < qty:
            raise RuleError(
                f"货道 {line.get('slot_no', lane_id)} 在途占用异常：待释放 {qty}，实际 {state['in_transit']}"
            )
        changes[lane_id] = LaneChange(lane_id=lane_id, stock_delta=0, transit_delta=-qty)
        line["line_status"] = LINE_VOIDED
    return new_lines, changes


def summarize_stored(lines: list[dict]) -> dict:
    """基于持久化行（dict，含 line_status）出汇总；行态/整单态同一套口径。"""
    pos = positive_lines(lines)
    return {
        "total_fill": sum(int(l.get("fill_qty", 0)) for l in lines),
        "need_fill_count": sum(1 for l in lines if l.get("status") == "need_fill"),
        "full_count": sum(1 for l in lines if l.get("status") == "full"),
        "overbooked_count": sum(1 for l in lines if l.get("status") == "overbooked"),
        "pending_count": sum(1 for l in pos if l.get("line_status", LINE_PENDING) == LINE_PENDING),
        "verified_count": sum(1 for l in pos if l.get("line_status") == LINE_VERIFIED),
        "voided_count": sum(1 for l in pos if l.get("line_status") == LINE_VOIDED),
        "pending_qty": sum(int(l["fill_qty"]) for l in pos
                           if l.get("line_status", LINE_PENDING) == LINE_PENDING),
        "verified_qty": sum(int(l["fill_qty"]) for l in pos
                            if l.get("line_status") == LINE_VERIFIED),
        "all_verified": order_completed(lines),
        "lines": lines,
    }
