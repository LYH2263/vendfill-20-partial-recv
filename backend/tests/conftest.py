import os

# 必须在导入 app.* 之前：避免 lifespan 去连默认的 Postgres / 自动种子
os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("SEED_ON_EMPTY", "false")

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.main import app
from app.models.models import Lane, Location


SEED_LANES = [
    # slot, sku, capacity, stock, in_transit  -> gap
    ("A1", "矿泉水", 20, 5, 0),    # gap 15  正补量
    ("A2", "可乐", 18, 18, 0),     # gap 0   full
    ("B1", "薯片", 12, 3, 2),      # gap 7   正补量
    ("B2", "巧克力", 15, 10, 5),   # gap 0   full
    ("C1", "能量棒", 10, 0, 0),    # gap 10  正补量
    ("C2", "口香糖", 24, 24, 2),   # gap -2  overbooked
]


@pytest.fixture()
def engine():
    eng = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def SessionFactory(engine):
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)


@pytest.fixture()
def db(SessionFactory):
    s = SessionFactory()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture()
def client(SessionFactory):
    def _get_db():
        s = SessionFactory()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = _get_db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture()
def seeded(SessionFactory):
    """建点位+货道，返回 {slot_no: lane_id} 与初始货道快照。"""
    s = SessionFactory()
    loc = Location(code="VM-01", name="地铁口 A 点位", address="测试")
    s.add(loc)
    s.flush()
    location_id = loc.id
    ids = {}
    for slot, sku, cap, stock, transit in SEED_LANES:
        lane = Lane(location_id=loc.id, slot_no=slot, sku_name=sku,
                    capacity=cap, stock=stock, in_transit=transit)
        s.add(lane)
        s.flush()
        ids[slot] = lane.id
    s.commit()
    s.close()
    baseline = {slot: {"stock": stock, "in_transit": transit}
                for slot, _sku, _cap, stock, transit in SEED_LANES}
    return {"location_id": location_id, "lane_ids": ids, "baseline": baseline}
