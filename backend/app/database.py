import json

from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import settings

engine = create_engine(settings.database_url, pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


def ensure_schema() -> None:
    """建表 + 对已存在的旧库做幂等小迁移（create_all 不会给旧表加列）。"""
    Base.metadata.create_all(bind=engine)
    inspector = inspect(engine)
    if "refill_orders" not in inspector.get_table_names():
        return
    columns = {c["name"] for c in inspector.get_columns("refill_orders")}
    if "status" not in columns:
        # 旧补货单没有整单状态列：历史单默认按“进行中”对待，行缺省 pending，
        # 并按其正补量补占货道在途，使“待核销行占用在途”的口径对旧数据同样成立。
        with engine.begin() as conn:
            conn.execute(text(
                "ALTER TABLE refill_orders "
                "ADD COLUMN status VARCHAR(16) NOT NULL DEFAULT 'active'"
            ))
        _backfill_pending_transit()


def _backfill_pending_transit() -> None:
    # 延迟导入，避免模型/服务层与本模块的初始化环
    from app.models.models import Lane, RefillOrder

    # 按调用时的模块级 engine 临时建会话（便于测试替换 engine）
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = session_factory()
    try:
        orders = db.scalars(select(RefillOrder).where(RefillOrder.status == "active")).all()
        lanes = {l.id: l for l in db.scalars(select(Lane)).all()}
        changed = False
        for order in orders:
            data = json.loads(order.lines_json or "{}")
            for line in data.get("lines", []):
                qty = int(line.get("fill_qty", 0))
                if qty <= 0 or line.get("line_status", "pending") != "pending":
                    continue
                lane = lanes.get(int(line["lane_id"]))
                if lane is not None:
                    lane.in_transit += qty
                    changed = True
        if changed:
            db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
