"""payments: inicia pagos con el proveedor externo y es la ÚNICA autoridad que confirma un pago.

Confirmación: webhook firmado (HMAC-SHA256 sobre timestamp + cuerpo), con ventana de 5 min,
deduplicado por event_id y verificado contra el monto que este servicio registró.
El navegador nunca informa que algo está pagado.
"""
import hashlib
import hmac
import json
import os
import time
import uuid
from datetime import datetime
from typing import Optional

import httpx
from fastapi import Depends, HTTPException, Request
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import Integer, String, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, Session

from cb_common.app import make_app, make_db, make_logger
from cb_common.secrets_provider import SecretProvider
from cb_common.security import make_guard, need_roles, only_from, Caller, InternalClient

SERVICE = "payments"
sp = SecretProvider(SERVICE)
guard = make_guard(sp)
log = make_logger(SERVICE)
engine, SessionLocal = make_db(os.getenv("DATABASE_URL", "sqlite:///.data/payments.db"))
app = make_app(SERVICE, "Payments")
orders_api = InternalClient(sp, SERVICE, "orders", os.getenv("ORDERS_URL", "http://127.0.0.1:8004"))
PROVIDER_URL = os.getenv("PROVIDER_URL", "http://127.0.0.1:8010")                 # servidor a servidor
PROVIDER_PUBLIC_URL = os.getenv("PROVIDER_PUBLIC_URL", "http://127.0.0.1:8010")   # al que va el navegador
PLATFORMS = {"Servipag", "Deposito bancario"}


class Base(DeclarativeBase):
    pass


class Payment(Base):
    __tablename__ = "payments"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    order_id: Mapped[int] = mapped_column(Integer, index=True)
    client_id: Mapped[str] = mapped_column(String(36))
    amount: Mapped[int] = mapped_column(Integer)
    platform: Mapped[str] = mapped_column(String(30))
    status: Mapped[str] = mapped_column(String(24))  # PENDIENTE|CONFIRMADO|RECHAZADO|FALLIDO|REEMBOLSO_PENDIENTE
    created: Mapped[str] = mapped_column(String(30))
    confirmed_at: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)


class WebhookEvent(Base):
    __tablename__ = "webhook_events"
    event_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    payment_id: Mapped[str] = mapped_column(String(40))
    received: Mapped[str] = mapped_column(String(30))
    outcome: Mapped[str] = mapped_column(String(30))


@app.on_event("startup")
def init():
    os.makedirs(".data", exist_ok=True)
    Base.metadata.create_all(engine)


def db():
    with SessionLocal() as s:
        yield s


def dto(p: Payment):
    return {"id": p.id, "pedido": p.order_id, "monto": p.amount, "plataforma": p.platform, "estado": p.status,
            "checkout_url": f"{PROVIDER_PUBLIC_URL}/checkout/{p.id}" if p.status == "PENDIENTE" else None}


class PayIn(BaseModel):
    order_id: int = Field(gt=0)
    plataforma: str


@app.post("/payments", status_code=201)
def start_payment(body: PayIn, caller: Caller = Depends(guard), s: Session = Depends(db)):
    only_from(caller, "gateway")
    ident = need_roles(caller, "cliente")
    if body.plataforma not in PLATFORMS:
        raise HTTPException(422, "Plataforma de pago no válida")
    try:
        r = httpx.get(f"{orders_api.base}/internal/orders/{body.order_id}", headers=orders_api.headers(None, None), timeout=3)
    except httpx.HTTPError:
        raise HTTPException(503, "Pedidos no disponible. Intenta nuevamente.")
    if r.status_code != 200 or r.json()["client_id"] != ident.client_id:
        raise HTTPException(404, "Pedido no encontrado")           # el monto sale del pedido, no del navegador
    order = r.json()
    if order["status"] != "PENDIENTE_PAGO":
        raise HTTPException(409, f"El pedido está {order['status']}; no admite pago")
    prev = s.scalar(select(Payment).where(Payment.order_id == order["id"], Payment.status == "PENDIENTE"))
    if prev:
        return dto(prev)                                           # idempotente: un solo pago activo por pedido
    p = Payment(id=uuid.uuid4().hex, order_id=order["id"], client_id=ident.client_id, amount=order["total"],
                platform=body.plataforma, status="PENDIENTE", created=datetime.utcnow().isoformat())
    s.add(p)
    s.commit()
    try:
        pr = httpx.post(f"{PROVIDER_URL}/intents", timeout=4, headers={"X-Api-Key": sp.get("provider-api-key")},
                        json={"payment_id": p.id, "amount": p.amount, "platform": p.platform})
        pr.raise_for_status()
    except httpx.HTTPError:
        p.status = "FALLIDO"
        s.commit()
        raise HTTPException(502, "El proveedor de pagos no respondió. Intenta nuevamente.")
    return dto(p)


@app.get("/payments/{pid}")
def get_payment(pid: str, caller: Caller = Depends(guard), s: Session = Depends(db)):
    only_from(caller, "gateway")
    ident = need_roles(caller, "cliente")
    p = s.get(Payment, pid)
    if not p or p.client_id != ident.client_id:
        raise HTTPException(404, "Pago no encontrado")
    return dto(p)


class HookIn(BaseModel):
    event_id: str = Field(min_length=8, max_length=80)
    payment_id: str = Field(max_length=40)
    status: str = Field(pattern="^(APPROVED|REJECTED)$")
    amount: int


def sign(secret: str, ts: str, raw: bytes) -> str:
    return hmac.new(secret.encode(), ts.encode() + b"." + raw, hashlib.sha256).hexdigest()


@app.post("/webhooks/provider")
async def webhook(request: Request, caller: Caller = Depends(guard), s: Session = Depends(db)):
    only_from(caller, "gateway")
    raw = await request.body()
    ts, sig = request.headers.get("x-timestamp", ""), request.headers.get("x-signature", "")
    try:
        fresh = abs(time.time() - int(ts)) <= 300
    except ValueError:
        fresh = False
    if not fresh or not hmac.compare_digest(sig, sign(sp.get("webhook-secret"), ts, raw)):
        log.warning("webhook rechazado: firma o timestamp inválidos")
        raise HTTPException(401, "Firma inválida")
    try:
        ev = HookIn(**json.loads(raw))
    except (ValueError, ValidationError):
        raise HTTPException(422, "Evento mal formado")
    pay = s.get(Payment, ev.payment_id)
    if not pay:
        raise HTTPException(404, "Pago desconocido")
    s.add(WebhookEvent(event_id=ev.event_id, payment_id=pay.id, received=datetime.utcnow().isoformat(), outcome="EN_PROCESO"))
    try:
        s.flush()
    except IntegrityError:
        s.rollback()
        return {"duplicate": True}                                  # mismo aviso dos veces: no se procesa de nuevo
    outcome = "OK"
    if ev.amount != pay.amount:
        outcome = "MONTO_DISTINTO"
    elif pay.status == "CONFIRMADO":
        outcome = "YA_CONFIRMADO"
    elif ev.status == "REJECTED":
        pay.status, outcome = "RECHAZADO", "RECHAZADO"
    else:
        try:
            r = httpx.post(f"{orders_api.base}/internal/orders/{pay.order_id}/mark-paid", timeout=4,
                           headers=orders_api.headers(None, None),
                           json={"payment_id": pay.id, "amount": pay.amount, "provider_ref": ev.event_id})
        except httpx.HTTPError:
            s.rollback()                                            # no se guarda el evento: el proveedor reintentará
            raise HTTPException(503, "Pedidos no disponible; reintentar")
        if r.status_code == 200:
            pay.status, pay.confirmed_at = "CONFIRMADO", datetime.utcnow().isoformat()
        elif r.status_code == 409:                                  # pago llegó tarde (pedido anulado/expirado)
            pay.status, outcome = "REEMBOLSO_PENDIENTE", "PEDIDO_NO_PAGABLE"
        else:
            s.rollback()
            raise HTTPException(502, "Pedidos rechazó la confirmación")
    s.get(WebhookEvent, ev.event_id).outcome = outcome
    s.commit()
    if outcome == "MONTO_DISTINTO":
        raise HTTPException(422, "El monto del aviso no coincide con el pago registrado")
    return {"ok": True, "estado": pay.status}
