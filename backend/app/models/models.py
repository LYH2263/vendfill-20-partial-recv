from datetime import datetime
from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from app.database import Base

class Location(Base):
    __tablename__ = "locations"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True)
    name: Mapped[str] = mapped_column(String(128))
    address: Mapped[str] = mapped_column(String(256), default="")

class Lane(Base):
    __tablename__ = "lanes"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    location_id: Mapped[int] = mapped_column(ForeignKey("locations.id"))
    slot_no: Mapped[str] = mapped_column(String(16))
    sku_name: Mapped[str] = mapped_column(String(64))
    capacity: Mapped[int] = mapped_column(Integer)
    stock: Mapped[int] = mapped_column(Integer, default=0)
    in_transit: Mapped[int] = mapped_column(Integer, default=0)

class Sale(Base):
    __tablename__ = "sales"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    lane_id: Mapped[int] = mapped_column(ForeignKey("lanes.id"))
    qty: Mapped[int] = mapped_column(Integer)
    sold_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

# 整单生命周期：active（未完成，可分批核销）→ completed（全部正补量行已核销，终态）
# 另设终态 voided（作废：释放全部在途占用，行不再可核销）
ORDER_STATUS_ACTIVE = "active"
ORDER_STATUS_COMPLETED = "completed"
ORDER_STATUS_VOIDED = "voided"

# 行核销态：pending（待核销，补量继续占用在途）→ verified（已核销，补量已入库存）
LINE_STATUS_PENDING = "pending"
LINE_STATUS_VERIFIED = "verified"

class RefillOrder(Base):
    __tablename__ = "refill_orders"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    location_id: Mapped[int] = mapped_column(ForeignKey("locations.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    lines_json: Mapped[str] = mapped_column(Text, default="[]")
    status: Mapped[str] = mapped_column(String(16), default=ORDER_STATUS_ACTIVE,
                                        server_default=ORDER_STATUS_ACTIVE, nullable=False)
