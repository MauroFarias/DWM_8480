"""notifications: validación de correo vía adaptador y outbox de avisos (boletas, bienvenida).

El outbox es la estructura de BD de la que se obtienen los avisos (RNF 4). El worker consume
PENDING y entrega mediante el adaptador (sandbox en desarrollo; SMTP/API real por configuración).
"""
import os
import re
import threading
import time
from datetime import datetime
from typing import Optional

from fastapi import Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import Integer, String, Text, select
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, Session

from cb_common.app import make_app, make_db, make_logger
from cb_common.secrets_provider import SecretProvider
from cb_common.security import make_guard, only_from, Caller

SERVICE = "notifications"
sp = SecretProvider(SERVICE)
guard = make_guard(sp)
log = make_logger(SERVICE)
engine, SessionLocal = make_db(os.getenv("DATABASE_URL", "sqlite:///.data/notifications.db"))
app = make_app(SERVICE, "Notifications")
MODE = os.getenv("EMAIL_PROVIDER", "sandbox")  # sandbox | (smtp/api real: implementar adaptador)
BLOCKED_DOMAINS = {"invalid", "example.invalid", "noexiste.cl", "mailinator.com"}


class Base(DeclarativeBase):
    pass


class Outbox(Base):
    __tablename__ = "outbox"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    dedupe_key: Mapped[Optional[str]] = mapped_column(String(80), unique=True, nullable=True)
    to_email: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(30))
    subject: Mapped[str] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(10), default="PENDING")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    created: Mapped[str] = mapped_column(String(30))
    sent: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)


def deliver(row: Outbox) -> bool:
    """Adaptador de entrega. En sandbox solo registra (sin cuerpo del mensaje) y marca como enviado."""
    if MODE == "sandbox":
        log.info("[sandbox] correo '%s' (%s) entregado al adaptador", row.kind, row.subject)
        return True
    raise NotImplementedError("Configurar adaptador SMTP/API real")


def worker():
    while True:
        try:
            with SessionLocal() as s:
                for row in s.scalars(select(Outbox).where(Outbox.status == "PENDING").limit(20)):
                    row.attempts += 1
                    try:
                        ok = deliver(row)
                    except Exception:
                        ok = False
                    if ok:
                        row.status, row.sent = "SENT", datetime.utcnow().isoformat()
                    elif row.attempts >= 5:
                        row.status = "FAILED"
                s.commit()
        except Exception:
            log.exception("worker outbox")
        time.sleep(2)


@app.on_event("startup")
def init():
    os.makedirs(".data", exist_ok=True)
    Base.metadata.create_all(engine)
    threading.Thread(target=worker, daemon=True).start()


def db():
    with SessionLocal() as s:
        yield s


class EmailIn(BaseModel):
    email: str = Field(max_length=200)


@app.post("/internal/email/validate")
def validate(body: EmailIn, caller: Caller = Depends(guard)):
    """Comprueba que el correo existe y está activo. Sandbox: sintaxis + dominio resoluble simulado.
    Modo real: reemplazar por la API del proveedor de verificación (misma firma de respuesta)."""
    only_from(caller, "auth")
    e = body.email.strip().lower()
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]{2,}", e):
        return {"valid": False, "reason": "formato"}
    dom = e.split("@")[1]
    if dom in BLOCKED_DOMAINS:
        return {"valid": False, "reason": "dominio_inexistente"}
    return {"valid": True, "reason": "ok"}


class SendIn(BaseModel):
    to: str = Field(max_length=200)
    kind: str = Field(max_length=30)
    subject: str = Field(max_length=200)
    body: str = Field(max_length=4000)
    dedupe_key: Optional[str] = Field(None, max_length=80)


@app.post("/internal/email/send", status_code=202)
def send(body: SendIn, caller: Caller = Depends(guard), s: Session = Depends(db)):
    only_from(caller, "auth", "orders")
    if body.dedupe_key:
        ex = s.scalar(select(Outbox).where(Outbox.dedupe_key == body.dedupe_key))
        if ex:
            return {"id": ex.id, "duplicate": True}
    row = Outbox(dedupe_key=body.dedupe_key, to_email=body.to.lower(), kind=body.kind, subject=body.subject,
                 body=body.body, created=datetime.utcnow().isoformat())
    s.add(row)
    s.commit()
    return {"id": row.id}


@app.get("/admin/outbox")
def outbox(caller: Caller = Depends(guard), s: Session = Depends(db)):
    """Avisos emitidos (sin cuerpo). Solo administrador, vía gateway."""
    only_from(caller, "gateway")
    if caller.identity is None:
        raise HTTPException(401, "Autenticación requerida")
    if caller.identity.role != "administrador":
        raise HTTPException(403, "Rol sin permiso para esta operación")
    return [{"id": r.id, "para": r.to_email, "tipo": r.kind, "asunto": r.subject, "estado": r.status,
             "creado": r.created} for r in s.scalars(select(Outbox).order_by(Outbox.id.desc()).limit(100))]
