import json

import pytest
from sqlalchemy import create_engine, inspect, text

from app import database
from app.models.models import Lane, Location, RefillOrder


def test_ensure_schema_migrates_old_db_and_backfills_transit(tmp_path, monkeypatch):
    db_file = tmp_path / "old.db"
    eng = create_engine(f"sqlite:///{db_file}")

    # 造一个“旧版库”：只有老列，没有 status，行 JSON 也没有 line_status
    with eng.begin() as conn:
        conn.execute(text(
            "CREATE TABLE locations (id INTEGER PRIMARY KEY, code VARCHAR(32), "
            "name VARCHAR(128), address VARCHAR(256))"
        ))
        conn.execute(text(
            "CREATE TABLE lanes (id INTEGER PRIMARY KEY, location_id INTEGER, "
            "slot_no VARCHAR(16), sku_name VARCHAR(64), capacity INTEGER, "
            "stock INTEGER, in_transit INTEGER)"
        ))
        conn.execute(text(
            "CREATE TABLE refill_orders (id INTEGER PRIMARY KEY, location_id INTEGER, "
            "created_at DATETIME, lines_json TEXT)"
        ))
        conn.execute(text("INSERT INTO locations VALUES (1, 'VM-01', 'old', '')"))
        conn.execute(text(
            "INSERT INTO lanes VALUES (1, 1, 'B1', '薯片', 12, 3, 2),"
            "(2, 1, 'A1', '矿泉水', 20, 5, 0)"
        ))
        old_lines = {"lines": [
            {"lane_id": 1, "slot_no": "B1", "fill_qty": 7, "status": "need_fill"},
            {"lane_id": 2, "slot_no": "A1", "fill_qty": 15, "status": "need_fill"},
        ]}
        conn.execute(text("INSERT INTO refill_orders (id, location_id, lines_json) "
                          "VALUES (1, 1, :j)"), {"j": json.dumps(old_lines)})

    monkeypatch.setattr(database, "engine", eng)

    # 第一次：补列 + 按待核销行补占在途
    database.ensure_schema()
    cols = {c["name"] for c in inspect(eng).get_columns("refill_orders")}
    assert "status" in cols
    with eng.begin() as conn:
        status = conn.execute(text("SELECT status FROM refill_orders WHERE id=1")).scalar()
        b1 = conn.execute(text("SELECT in_transit FROM lanes WHERE id=1")).scalar()
        a1 = conn.execute(text("SELECT in_transit FROM lanes WHERE id=2")).scalar()
    assert status == "active"
    assert b1 == 2 + 7
    assert a1 == 15

    # 第二次：幂等，不重复补占
    database.ensure_schema()
    with eng.begin() as conn:
        b1 = conn.execute(text("SELECT in_transit FROM lanes WHERE id=1")).scalar()
        a1 = conn.execute(text("SELECT in_transit FROM lanes WHERE id=2")).scalar()
    assert (b1, a1) == (9, 15)
    eng.dispose()
