"""orders: autoridad sobre pedidos y su ciclo de vida.

PENDIENTE_PAGO -> PAGADO -> EN_DESPACHO -> ENTREGADO ; ANULADO desde PENDIENTE_PAGO/PAGADO (cliente)
o también EN_DESPACHO (administrador). Solo 'payments' puede ejecutar mark-paid.
Los precios y totales los calcula este servicio con datos de inventory; nunca los del navegador.
"""
import os
import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta, date
from typing import List, Optional

import httpx
from fastapi import Depends, HTTPException, Query
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field
from sqlalchemy import Boolean, Integer, String, Text, select, delete
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, Session

from cb_common.app import make_app, make_db, make_logger
from cb_common.crypto import enc, dec
from cb_common.secrets_provider import SecretProvider
from cb_common.security import make_guard, need_roles, only_from, Caller, InternalClient

SERVICE = "orders"
sp = SecretProvider(SERVICE)
guard = make_guard(sp)
log = make_logger(SERVICE)
engine, SessionLocal = make_db(os.getenv("DATABASE_URL", "sqlite:///.data/orders.db"))
app = make_app(SERVICE, "Orders")
inv = InternalClient(sp, SERVICE, "inventory", os.getenv("INVENTORY_URL", "http://127.0.0.1:8003"))
cli = InternalClient(sp, SERVICE, "clients", os.getenv("CLIENTS_URL", "http://127.0.0.1:8002"))
notif = InternalClient(sp, SERVICE, "notifications", os.getenv("NOTIFICATIONS_URL", "http://127.0.0.1:8006"))
PENDING_TTL = int(os.getenv("PENDING_TTL_SECONDS", "1800"))
CASHIER_ID = os.getenv("VIRTUAL_CASHIER_ID", "cajero-virtual-web")
# distancia aproximada (km) de cada comuna al local de Maipú; >3 km => costo adicional a confirmar con el chofer
COMUNA_KM = {"Maipú": 1.5, "Cerrillos": 2.9}
FREE_RADIUS_KM = 3.0
STAFF_VIEW = ("administrador", "cajero_virtual", "dueno")


class Base(DeclarativeBase):
    pass


class Order(Base):
    __tablename__ = "orders"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    client_id: Mapped[str] = mapped_column(String(36), index=True)
    status: Mapped[str] = mapped_column(String(16), index=True)
    total: Mapped[int] = mapped_column(Integer)
    platform: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    address_enc: Mapped[str] = mapped_column(Text)
    comuna: Mapped[str] = mapped_column(String(60))
    out_of_radius: Mapped[bool] = mapped_column(Boolean, default=False)
    created: Mapped[str] = mapped_column(String(30))
    paid_at: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    boleta: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    payment_id: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    cashier_id: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    cancel_reason: Mapped[Optional[str]] = mapped_column(String(300), nullable=True)
    cancelled_by: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    refund_pending: Mapped[bool] = mapped_column(Boolean, default=False)


class OrderItem(Base):
    __tablename__ = "order_items"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(Integer, index=True)
    product_id: Mapped[str] = mapped_column(String(20))
    name: Mapped[str] = mapped_column(String(80))      # snapshot: el historial no cambia si el producto cambia o se descontinúa
    format: Mapped[str] = mapped_column(String(40))
    qty: Mapped[int] = mapped_column(Integer)
    unit_price: Mapped[int] = mapped_column(Integer)


class OrderEvent(Base):
    __tablename__ = "order_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(Integer, index=True)
    at: Mapped[str] = mapped_column(String(30))
    event: Mapped[str] = mapped_column(String(60))
    actor: Mapped[str] = mapped_column(String(40))


def now() -> str:
    return datetime.utcnow().isoformat()


def event(s: Session, oid: int, ev: str, actor: str):
    s.add(OrderEvent(order_id=oid, at=now(), event=ev, actor=actor))


def expirer():
    while True:
        time.sleep(30)
        try:
            limit = (datetime.utcnow() - timedelta(seconds=PENDING_TTL)).isoformat()
            with SessionLocal() as s:
                for o in s.scalars(select(Order).where(Order.status == "PENDIENTE_PAGO", Order.created < limit)):
                    try:
                        r = httpx.post(f"{inv.base}/internal/reservations/{o.id}/release", headers=inv.headers(None, None), timeout=3)
                        r.raise_for_status()
                    except httpx.HTTPError:
                        continue  # se reintenta en el siguiente ciclo
                    o.status, o.cancel_reason, o.cancelled_by = "ANULADO", "Pago no recibido a tiempo", "sistema"
                    event(s, o.id, "EXPIRADO", "sistema")
                s.commit()
        except Exception:
            log.exception("expirer")


@app.on_event("startup")
def init():
    os.makedirs(".data", exist_ok=True)
    Base.metadata.create_all(engine)
    threading.Thread(target=expirer, daemon=True).start()


def db():
    with SessionLocal() as s:
        yield s


def items_of(s: Session, oid: int):
    return list(s.scalars(select(OrderItem).where(OrderItem.order_id == oid).order_by(OrderItem.id)))


def dto(s: Session, o: Order, role: str):
    d = {"id": o.id, "numero": o.id, "boleta": o.boleta, "estado": o.status, "total": o.total, "fecha": o.created,
         "plataforma_pago": o.platform, "fuera_de_radio": o.out_of_radius, "motivo_anulacion": o.cancel_reason,
         "items": [{"producto_id": i.product_id, "nombre": i.name, "formato": i.format, "cantidad": i.qty,
                    "precio_unit": i.unit_price, "subtotal": i.qty * i.unit_price} for i in items_of(s, o.id)]}
    if role in ("cliente", "administrador", "despacho"):
        d["direccion_despacho"] = dec(sp.get("data-enc-key"), o.address_enc) + ", " + o.comuna
    if role in ("administrador", "cajero_virtual"):
        d["cliente_id"] = o.client_id
        d["cajero_id"] = o.cashier_id
    if role == "administrador":
        d["reembolso_pendiente"] = o.refund_pending
    return d


def own_or_404(o: Optional[Order], ident):
    if o is None or (ident.role == "cliente" and o.client_id != ident.client_id):
        raise HTTPException(404, "Pedido no encontrado")  # 404 y no 403: no revela si el pedido ajeno existe


def mail(to: str, kind: str, subject: str, body: str, key: str):
    try:
        httpx.post(f"{notif.base}/internal/email/send", headers=notif.headers(None, None), timeout=3,
                   json={"to": to, "kind": kind, "subject": subject, "body": body, "dedupe_key": key})
    except httpx.HTTPError:
        log.warning("aviso %s no encolado (se registra y se continúa)", kind)


def client_info(cid: str) -> Optional[dict]:
    try:
        r = httpx.get(f"{cli.base}/internal/clients/{cid}", headers=cli.headers(None, None), timeout=3)
        return r.json() if r.status_code == 200 else None
    except httpx.HTTPError:
        return None


# ---------------- crear pedido ----------------
class ItemIn(BaseModel):
    product_id: str = Field(max_length=20)
    cantidad: int = Field(gt=0, le=24)


class OrderIn(BaseModel):
    items: List[ItemIn] = Field(min_length=1, max_length=20)
    direccion_despacho: Optional[str] = Field(None, min_length=3, max_length=200)
    comuna_despacho: Optional[str] = Field(None, max_length=60)
    plataforma_pago: Optional[str] = Field(None, pattern="^(Servipag|Deposito bancario)$")


@app.post("/orders", status_code=201)
def create_order(body: OrderIn, caller: Caller = Depends(guard), s: Session = Depends(db)):
    only_from(caller, "gateway")
    ident = need_roles(caller, "cliente")
    c = client_info(ident.client_id)
    if not c:
        raise HTTPException(503, "No se pudo obtener tu perfil. Intenta nuevamente.")
    qty = defaultdict(int)
    for it in body.items:
        qty[it.product_id] += it.cantidad
    try:
        r = httpx.post(f"{inv.base}/internal/products/lookup", json={"ids": list(qty)}, headers=inv.headers(None, None), timeout=3)
        r.raise_for_status()
    except httpx.HTTPError:
        raise HTTPException(503, "Inventario no disponible. No se creó el pedido.")
    prods = r.json()
    for pid, q in qty.items():
        p = prods.get(pid)
        if not p or not p["activo"]:
            raise HTTPException(422, f"Producto {pid} no disponible")
        if q > 24:
            raise HTTPException(422, "Máximo 24 unidades por producto")
    total = sum(prods[pid]["precio"] * q for pid, q in qty.items())  # precio SIEMPRE desde inventory
    address = body.direccion_despacho or c["direccion"]
    comuna = body.comuna_despacho or c["comuna"]
    o = Order(client_id=ident.client_id, status="PENDIENTE_PAGO", total=total, platform=body.plataforma_pago,
              address_enc=enc(sp.get("data-enc-key"), address), comuna=comuna,
              out_of_radius=COMUNA_KM.get(comuna, 99) > FREE_RADIUS_KM, created=now())
    s.add(o)
    s.flush()
    for pid, q in qty.items():
        p = prods[pid]
        s.add(OrderItem(order_id=o.id, product_id=pid, name=p["nombre"], format=p["formato"], qty=q, unit_price=p["precio"]))
    event(s, o.id, "CREADO", ident.sub)
    s.commit()
    try:
        rr = httpx.post(f"{inv.base}/internal/reservations/{o.id}", headers=inv.headers(None, None), timeout=3,
                        json={"items": [{"product_id": pid, "qty": q} for pid, q in qty.items()]})
    except httpx.HTTPError:
        rr = None
    if rr is None or rr.status_code != 200:
        s.execute(delete(OrderItem).where(OrderItem.order_id == o.id))
        s.execute(delete(OrderEvent).where(OrderEvent.order_id == o.id))
        s.execute(delete(Order).where(Order.id == o.id))
        s.commit()
        if rr is not None and rr.status_code == 409:
            raise HTTPException(409, {"mensaje": "Stock insuficiente", "productos": rr.json().get("detail", {}).get("productos", [])})
        raise HTTPException(503, "No fue posible reservar stock. No se creó el pedido.")
    return dto(s, o, "cliente")


# ---------------- consultar ----------------
@app.get("/orders")
def list_orders(estado: Optional[str] = Query(None, pattern="^(PENDIENTE_PAGO|PAGADO|EN_DESPACHO|ENTREGADO|ANULADO)$"),
                limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0, le=100000),
                caller: Caller = Depends(guard), s: Session = Depends(db)):
    only_from(caller, "gateway", "graphql")
    ident = need_roles(caller, "cliente", *STAFF_VIEW)
    q = select(Order)
    if ident.role == "cliente":
        q = q.where(Order.client_id == ident.client_id)
    if estado:
        q = q.where(Order.status == estado)
    rows = s.scalars(q.order_by(Order.id.desc()).limit(limit).offset(offset))
    return [dto(s, o, ident.role) for o in rows]


@app.get("/orders/{oid}")
def get_order(oid: int, caller: Caller = Depends(guard), s: Session = Depends(db)):
    only_from(caller, "gateway", "graphql")
    ident = need_roles(caller, "cliente", *STAFF_VIEW)
    o = s.get(Order, oid)
    own_or_404(o, ident)
    return dto(s, o, ident.role)


# ---------------- anular ----------------
class CancelIn(BaseModel):
    motivo: str = Field(min_length=3, max_length=300)


@app.post("/orders/{oid}/cancel")
def cancel(oid: int, body: CancelIn, caller: Caller = Depends(guard), s: Session = Depends(db)):
    only_from(caller, "gateway")
    ident = need_roles(caller, "cliente", "administrador")
    o = s.get(Order, oid)
    own_or_404(o, ident)
    allowed = {"PENDIENTE_PAGO", "PAGADO"} if ident.role == "cliente" else {"PENDIENTE_PAGO", "PAGADO", "EN_DESPACHO"}
    if o.status not in allowed:
        raise HTTPException(409, f"Un pedido en estado {o.status} no puede anularse")
    try:  # primero se libera stock (idempotente); si falla, el pedido sigue igual y se puede reintentar
        r = httpx.post(f"{inv.base}/internal/reservations/{o.id}/release", headers=inv.headers(None, None), timeout=3)
        r.raise_for_status()
    except httpx.HTTPError:
        raise HTTPException(503, "No se pudo devolver el stock. Reintenta en unos segundos.")
    was_paid = o.status in ("PAGADO", "EN_DESPACHO")
    o.status, o.cancel_reason, o.cancelled_by, o.refund_pending = "ANULADO", body.motivo.strip(), ident.sub, was_paid
    event(s, o.id, "ANULADO", ident.sub)
    s.commit()
    c = client_info(o.client_id)
    if c:
        mail(c["email"], "ANULACION", f"Compra #{o.id} anulada",
             f"Tu compra #{o.id} fue anulada. Motivo: {o.cancel_reason}." + (" El reembolso queda en proceso." if was_paid else ""),
             f"anulacion-{o.id}")
    return dto(s, o, ident.role)


# ---------------- interno: confirmación de pago (solo payments) ----------------
class PaidIn(BaseModel):
    payment_id: str = Field(max_length=40)
    amount: int
    provider_ref: Optional[str] = Field(None, max_length=80)


@app.get("/internal/orders/{oid}")
def internal_get(oid: int, caller: Caller = Depends(guard), s: Session = Depends(db)):
    only_from(caller, "payments")
    o = s.get(Order, oid)
    if not o:
        raise HTTPException(404, "Pedido no encontrado")
    return {"id": o.id, "client_id": o.client_id, "total": o.total, "status": o.status}


@app.post("/internal/orders/{oid}/mark-paid")
def mark_paid(oid: int, body: PaidIn, caller: Caller = Depends(guard), s: Session = Depends(db)):
    only_from(caller, "payments")
    o = s.get(Order, oid)
    if not o:
        raise HTTPException(404, "Pedido no encontrado")
    if o.status == "PAGADO" and o.payment_id == body.payment_id:
        return {"status": "PAGADO", "boleta": o.boleta, "idempotent": True}
    if o.status != "PENDIENTE_PAGO":
        raise HTTPException(409, f"El pedido está {o.status}; no admite pago")
    if body.amount != o.total:
        raise HTTPException(422, "El monto pagado no coincide con el total del pedido")
    o.status, o.paid_at, o.payment_id = "PAGADO", now(), body.payment_id
    o.boleta, o.cashier_id = f"B-{1000 + o.id}", CASHIER_ID
    event(s, o.id, "PAGADO", "payments")
    s.commit()
    c = client_info(o.client_id)
    if c:
        lines = "\n".join(f"{i.qty} x {i.name} ({i.format}) - ${i.qty * i.unit_price:,}".replace(",", ".") for i in items_of(s, o.id))
        mail(c["email"], "BOLETA", f"Boleta digital {o.boleta} — Craft and Beer",
             f"Boleta digital {o.boleta}\n{lines}\nTotal: ${o.total:,}\nPago vía: {o.platform or 'plataforma externa'}".replace(",", "."),
             f"boleta-{o.id}")
    return {"status": "PAGADO", "boleta": o.boleta}


# ---------------- despacho ----------------
@app.get("/dispatch/orders")
def dispatch_list(caller: Caller = Depends(guard), s: Session = Depends(db)):
    only_from(caller, "gateway")
    need_roles(caller, "despacho")
    rows = s.scalars(select(Order).where(Order.status.in_(["PAGADO", "EN_DESPACHO"])).order_by(Order.paid_at, Order.id))
    out = []
    for o in rows:
        d = dto(s, o, "despacho")
        c = client_info(o.client_id)
        d["cliente_nombre"] = c["nombre"] if c else None
        out.append(d)
    return out


def transition(s: Session, oid: int, src: str, dst: str, actor: str):
    o = s.get(Order, oid)
    if not o:
        raise HTTPException(404, "Pedido no encontrado")
    if o.status != src:
        raise HTTPException(409, f"El pedido está {o.status}; se esperaba {src}")
    o.status = dst
    event(s, o.id, dst, actor)
    s.commit()
    return o


@app.post("/dispatch/orders/{oid}/start")
def dispatch_start(oid: int, caller: Caller = Depends(guard), s: Session = Depends(db)):
    only_from(caller, "gateway")
    ident = need_roles(caller, "despacho")
    return dto(s, transition(s, oid, "PAGADO", "EN_DESPACHO", ident.sub), "despacho")


@app.post("/dispatch/orders/{oid}/deliver")
def dispatch_deliver(oid: int, caller: Caller = Depends(guard), s: Session = Depends(db)):
    only_from(caller, "gateway")
    ident = need_roles(caller, "despacho")
    return dto(s, transition(s, oid, "EN_DESPACHO", "ENTREGADO", ident.sub), "despacho")


@app.get("/dispatch/orders/{oid}/print", response_class=PlainTextResponse)
def dispatch_print(oid: int, caller: Caller = Depends(guard), s: Session = Depends(db)):
    only_from(caller, "gateway")
    need_roles(caller, "despacho")
    o = s.get(Order, oid)
    if not o or o.status not in ("PAGADO", "EN_DESPACHO"):
        raise HTTPException(404, "Pedido no encontrado")
    d, c = dto(s, o, "despacho"), client_info(o.client_id)
    lines = "\n".join(f"{i['cantidad']} x {i['nombre']} ({i['formato']})" for i in d["items"])
    extra = "\nATENCION: fuera del radio gratuito; confirmar costo adicional con el cliente." if o.out_of_radius else ""
    return (f"ORDEN DE DESPACHO\nBoleta: {o.boleta}\nCliente: {c['nombre'] if c else '-'}\n"
            f"Direccion: {d['direccion_despacho']}\n{'-' * 30}\n{lines}\n{'-' * 30}{extra}\n")


# ---------------- reportes y caja ----------------
def parse_day(v: Optional[str], default: date) -> date:
    try:
        return date.fromisoformat(v) if v else default
    except ValueError:
        raise HTTPException(422, "Fecha inválida (use AAAA-MM-DD)")


@app.get("/reports/sales")
def report(desde: Optional[str] = None, hasta: Optional[str] = None, caller: Caller = Depends(guard), s: Session = Depends(db)):
    only_from(caller, "gateway")
    need_roles(caller, "dueno", "administrador")
    d1 = parse_day(desde, date.today() - timedelta(days=30))
    d2 = parse_day(hasta, date.today())
    if d1 > d2:
        raise HTTPException(422, "'desde' no puede ser posterior a 'hasta'")
    lo, hi = d1.isoformat(), (d2 + timedelta(days=1)).isoformat()
    rows = list(s.scalars(select(Order).where(Order.status.in_(["PAGADO", "EN_DESPACHO", "ENTREGADO"]),
                                              Order.paid_at >= lo, Order.paid_at < hi)))
    by = defaultdict(lambda: [0, 0])
    units = 0
    for o in rows:
        for i in items_of(s, o.id):
            by[i.name + " (" + i.format + ")"][0] += i.qty
            by[i.name + " (" + i.format + ")"][1] += i.qty * i.unit_price
            units += i.qty
    return {"desde": lo[:10], "hasta": d2.isoformat(), "ventas": len(rows), "unidades": units,
            "ingresos": sum(o.total for o in rows),
            "por_producto": [{"producto": k, "unidades": v[0], "ingresos": v[1]} for k, v in sorted(by.items(), key=lambda kv: -kv[1][0])]}


@app.get("/cashier/sales")
def cashier(caller: Caller = Depends(guard), s: Session = Depends(db)):
    only_from(caller, "gateway")
    need_roles(caller, "cajero_virtual", "administrador")
    rows = s.scalars(select(Order).where(Order.boleta.is_not(None)).order_by(Order.id.desc()).limit(200))
    return [{"boleta": o.boleta, "pedido": o.id, "fecha_pago": o.paid_at, "total": o.total, "plataforma_pago": o.platform,
             "estado": o.status, "cajero_id": o.cashier_id} for o in rows]
