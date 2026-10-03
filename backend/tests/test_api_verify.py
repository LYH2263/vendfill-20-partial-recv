import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.main as mainmod
from app.database import Base, get_db
from app.models.models import Lane, RefillOrder  # noqa: F401
from app.services.seed import seed_if_empty


@pytest.fixture()
def client():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    # lifespan 与 seed 都走 main 模块级 engine/SessionLocal，替换为内存库
    orig_engine, orig_session = mainmod.engine, mainmod.SessionLocal
    mainmod.engine = engine
    mainmod.SessionLocal = Session
    seed_db = Session()
    seed_if_empty(seed_db)
    seed_db.close()

    def override_get_db():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    mainmod.app.dependency_overrides[get_db] = override_get_db
    try:
        with TestClient(mainmod.app, raise_server_exceptions=False) as c:
            yield c, Session
    finally:
        mainmod.app.dependency_overrides.clear()
        mainmod.engine = orig_engine
        mainmod.SessionLocal = orig_session
        Base.metadata.drop_all(bind=engine)


def _latest(session_factory):
    db = session_factory()
    try:
        return db.query(RefillOrder).order_by(RefillOrder.id.desc()).first()
    finally:
        db.close()


def _lane_map(session_factory):
    db = session_factory()
    try:
        return {l.slot_no: l for l in db.query(Lane).all()}
    finally:
        db.close()


def test_seed_via_api_b1_verified_order_draft(client):
    c, Session = client
    r = c.get("/api/refills/latest?location_id=1")
    assert r.status_code == 200
    data = r.json()
    assert data["order_status"] == "draft"
    by_slot = {l["slot_no"]: l for l in data["lines"]}
    assert by_slot["B1"]["line_status"] == "verified"
    assert by_slot["A1"]["line_status"] == "pending"


def test_partial_verify_then_complete_via_api(client):
    c, Session = client
    order = _latest(Session)
    lanes = _lane_map(Session)

    # 只核销 A1
    r = c.post(f"/api/refills/{order.id}/verify", json={"lane_ids": [lanes["A1"].id]})
    assert r.status_code == 200, r.text
    assert r.json()["order_status"] == "draft"

    # 注入失败：勾选 C1，fail_after=0，批前状态不变
    before = (lanes["C1"].stock, lanes["C1"].in_transit)
    r = c.post(f"/api/refills/{order.id}/verify",
               json={"lane_ids": [lanes["C1"].id], "fail_after": 0})
    assert r.status_code == 500
    lanes2 = _lane_map(Session)
    assert (lanes2["C1"].stock, lanes2["C1"].in_transit) == before
    assert lanes2["A1"].stock == 20  # 已提交的 A1 不受失败批影响

    # 核销剩余 → 整单完成
    r = c.post(f"/api/refills/{order.id}/verify", json={"lane_ids": [lanes["C1"].id]})
    assert r.status_code == 200
    assert r.json()["order_status"] == "completed"

    # 完成单再核销 → 409
    r = c.post(f"/api/refills/{order.id}/verify", json={})
    assert r.status_code == 409

    # 汇总与货道一致
    s = c.get("/api/refills/summary?location_id=1").json()
    assert s["completed"] is True and s["pending_count"] == 0
    assert s["verified_fill"] == 32 and s["verified_count"] == 3
    lanes3 = _lane_map(Session)
    assert (lanes3["A1"].stock, lanes3["A1"].in_transit) == (20, 0)
    assert (lanes3["B1"].stock, lanes3["B1"].in_transit) == (10, 2)
    assert (lanes3["C1"].stock, lanes3["C1"].in_transit) == (10, 0)


def test_batch_injected_failure_midway_rolls_back_all(client):
    c, Session = client
    order = _latest(Session)
    lanes = _lane_map(Session)
    a1_before = (lanes["A1"].stock, lanes["A1"].in_transit)
    c1_before = (lanes["C1"].stock, lanes["C1"].in_transit)

    # A1+C1 同批，应用第 1 行后崩溃 → 两行都回到批前
    r = c.post(f"/api/refills/{order.id}/verify",
               json={"lane_ids": [lanes["A1"].id, lanes["C1"].id], "fail_after": 1})
    assert r.status_code == 500
    after = _lane_map(Session)
    assert (after["A1"].stock, after["A1"].in_transit) == a1_before
    assert (after["C1"].stock, after["C1"].in_transit) == c1_before
    assert _latest(Session).status == "draft"
