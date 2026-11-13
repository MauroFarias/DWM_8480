"""inventory: autoridad sobre catálogo, precios, stock y reservas."""
import os
from typing import List, Optional

from fastapi import Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import Boolean, Integer, String, Float, ForeignKey, select, update, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, Session

from cb_common.app import make_app, make_db
from cb_common.secrets_provider import SecretProvider
from cb_common.security import make_guard, need_roles, only_from, Caller

SERVICE = "inventory"
sp = SecretProvider(SERVICE)
guard = make_guard(sp)
engine, SessionLocal = make_db(os.getenv("DATABASE_URL", "sqlite:///.data/inventory.db"))
app = make_app(SERVICE, "Inventory")


class Base(DeclarativeBase):
    pass


class Product(Base):
    __tablename__ = "products"
    id: Mapped[str] = mapped_column(String(20), primary_key=True)
    name: Mapped[str] = mapped_column(String(80))
    style: Mapped[str] = mapped_column(String(60))
    format: Mapped[str] = mapped_column(String(40))
    abv: Mapped[float] = mapped_column(Float)
    price: Mapped[int] = mapped_column(Integer)
    stock: Mapped[int] = mapped_column(Integer)
    swatch: Mapped[str] = mapped_column(String(9), default="#c9791f")
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class Reservation(Base):
    __tablename__ = "reservations"
    order_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    status: Mapped[str] = mapped_column(String(12))  # RESERVED | RELEASED


class ReservationItem(Base):
    __tablename__ = "reservation_items"
    order_id: Mapped[str] = mapped_column(ForeignKey("reservations.order_id"), primary_key=True)
    product_id: Mapped[str] = mapped_column(String(20), primary_key=True)
    qty: Mapped[int] = mapped_column(Integer)


SEED = [
    ("P1", "Chasqui Golden Ale", "Golden Ale", "Botella 330ml", 4.8, 2990, 120, "#d9a441"),
    ("P2", "Andina IPA", "India Pale Ale", "Botella 330ml", 6.2, 3490, 80, "#c9791f"),
    ("P3", "Volcán Porter", "Porter", "Botella 500ml", 5.9, 3990, 45, "#4a3324"),
    ("P4", "Niebla Weizen", "Hefeweizen", "Botella 500ml", 5.0, 3690, 60, "#e8c873"),
    ("P5", "Cordillera Red Ale", "Red Ale", "Botella 330ml", 5.4, 3290, 8, "#a5451f"),
    ("P6", "Chasqui Golden Ale", "Golden Ale", "Growler 1L", 4.8, 6990, 25, "#d9a441"),
    ("P7", "Andina IPA", "India Pale Ale", "Growler 1L", 6.2, 7990, 0, "#c9791f"),
    ("P8", "Volcán Porter", "Porter", "Growler 2L", 5.9, 13990, 10, "#4a3324"),
]


@app.on_event("startup")
def init():
    os.makedirs(".data", exist_ok=True)
    Base.metadata.create_all(engine)
    with SessionLocal() as s:
        if not s.scalar(select(Product.id).limit(1)):
            for p in SEED:
                s.add(Product(id=p[0], name=p[1], style=p[2], format=p[3], abv=p[4], price=p[5], stock=p[6], swatch=p[7]))
            s.commit()


def db():
    with SessionLocal() as s:
        yield s


def public_dto(p: Product):
    return {"id": p.id, "nombre": p.name, "estilo": p.style, "formato": p.format, "abv": p.abv,
            "precio": p.price, "color": p.swatch, "disponible": p.active and p.stock > 0,
            "ultimas_unidades": p.stock if 0 < p.stock <= 10 and p.active else None}


def admin_dto(p: Product):
    d = public_dto(p)
    d.update({"stock": p.stock, "activo": p.active})
    return d


# ---------- S01/S02 catálogo público ----------
@app.get("/products")
def list_products(available: Optional[bool] = None, style: Optional[str] = Query(None, max_length=60),
                  caller: Caller = Depends(guard), s: Session = Depends(db)):
    only_from(caller, "gateway")
    q = select(Product).where(Product.active.is_(True))
    if available:
        q = q.where(Product.stock > 0)
    if style:
        q = q.where(Product.style == style)
    return [public_dto(p) for p in s.scalars(q.order_by(Product.id))]


@app.get("/products/{pid}")
def get_product(pid: str, caller: Caller = Depends(guard), s: Session = Depends(db)):
    only_from(caller, "gateway")
    p = s.get(Product, pid)
    if not p or not p.active:
        raise HTTPException(404, "Producto no encontrado")
    return public_dto(p)


# ---------- mantenedor (administrador, dueño) ----------
class ProductIn(BaseModel):
    nombre: str = Field(min_length=2, max_length=80)
    estilo: str = Field(min_length=2, max_length=60)
    formato: str = Field(min_length=2, max_length=40)
    abv: float = Field(ge=0, le=20)
    precio: int = Field(gt=0, le=1_000_000)
    stock: int = Field(ge=0, le=100_000)
    activo: bool = True


@app.get("/admin/products")
def admin_list(caller: Caller = Depends(guard), s: Session = Depends(db)):
    only_from(caller, "gateway")
    need_roles(caller, "administrador", "dueno")
    return [admin_dto(p) for p in s.scalars(select(Product).order_by(Product.id))]


@app.post("/products", status_code=201)
def create_product(body: ProductIn, caller: Caller = Depends(guard), s: Session = Depends(db)):
    only_from(caller, "gateway")
    need_roles(caller, "administrador", "dueno")
    ids = [int(x[1:]) for x in s.scalars(select(Product.id)) if x[1:].isdigit()]
    new_id = "P" + str((max(ids) if ids else 0) + 1)
    pal = ["#c9791f", "#d9a441", "#4a3324", "#a5451f", "#e8c873", "#7a8f4a"]
    p = Product(id=new_id, name=body.nombre, style=body.estilo, format=body.formato, abv=body.abv, price=body.precio,
                stock=body.stock, active=body.activo, swatch=pal[int(new_id[1:]) % len(pal)])
    s.add(p)
    s.commit()
    return admin_dto(p)


@app.put("/products/{pid}")
def replace_product(pid: str, body: ProductIn, caller: Caller = Depends(guard), s: Session = Depends(db)):
    only_from(caller, "gateway")
    need_roles(caller, "administrador", "dueno")
    p = s.get(Product, pid)
    if not p:
        raise HTTPException(404, "Producto no encontrado")
    p.name, p.style, p.format, p.abv = body.nombre, body.estilo, body.formato, body.abv
    p.price, p.stock, p.active = body.precio, body.stock, body.activo
    s.commit()
    return admin_dto(p)


@app.delete("/products/{pid}", status_code=204)
def discontinue_product(pid: str, caller: Caller = Depends(guard), s: Session = Depends(db)):
    """Descontinuar = baja lógica: los pedidos históricos conservan su snapshot y el producto sale del catálogo."""
    only_from(caller, "gateway")
    need_roles(caller, "administrador")
    p = s.get(Product, pid)
    if not p:
        raise HTTPException(404, "Producto no encontrado")
    p.active = False
    s.commit()


# ---------- internas ----------
class LookupIn(BaseModel):
    ids: List[str] = Field(max_length=50)


@app.post("/internal/products/lookup")
def lookup(body: LookupIn, caller: Caller = Depends(guard), s: Session = Depends(db)):
    only_from(caller, "orders", "graphql")
    out = {}
    for p in s.scalars(select(Product).where(Product.id.in_(body.ids))):
        out[p.id] = {"id": p.id, "nombre": p.name, "estilo": p.style, "formato": p.format, "abv": p.abv,
                     "precio": p.price, "activo": p.active, "disponible": p.active and p.stock > 0}
    return out


class ItemQty(BaseModel):
    product_id: str
    qty: int = Field(gt=0, le=100)


class ReserveIn(BaseModel):
    items: List[ItemQty] = Field(min_length=1, max_length=50)


@app.post("/internal/reservations/{order_id}")
def reserve(order_id: str, body: ReserveIn, caller: Caller = Depends(guard), s: Session = Depends(db)):
    """Reserva atómica e idempotente por order_id: descuenta stock solo si alcanza para TODOS los ítems."""
    only_from(caller, "orders")
    existing = s.get(Reservation, order_id)
    if existing:
        if existing.status == "RESERVED":
            return {"status": "RESERVED", "idempotent": True}
        raise HTTPException(409, "La reserva ya fue liberada")
    missing = []
    try:
        for it in body.items:
            r = s.execute(update(Product).where(Product.id == it.product_id, Product.active.is_(True),
                                                Product.stock >= it.qty).values(stock=Product.stock - it.qty))
            if r.rowcount != 1:
                missing.append(it.product_id)
        if missing:
            s.rollback()
            raise HTTPException(409, {"mensaje": "Stock insuficiente", "productos": missing})
        s.add(Reservation(order_id=order_id, status="RESERVED"))
        s.flush()
        for it in body.items:
            s.add(ReservationItem(order_id=order_id, product_id=it.product_id, qty=it.qty))
        s.commit()
    except HTTPException:
        raise
    except Exception:
        s.rollback()
        raise
    return {"status": "RESERVED"}


@app.post("/internal/reservations/{order_id}/release")
def release(order_id: str, caller: Caller = Depends(guard), s: Session = Depends(db)):
    only_from(caller, "orders")
    r = s.get(Reservation, order_id)
    if not r:
        return {"status": "NONE"}
    if r.status == "RELEASED":
        return {"status": "RELEASED", "idempotent": True}
    for it in s.scalars(select(ReservationItem).where(ReservationItem.order_id == order_id)):
        s.execute(update(Product).where(Product.id == it.product_id).values(stock=Product.stock + it.qty))
    r.status = "RELEASED"
    s.commit()
    return {"status": "RELEASED"}
