"""clients: autoridad sobre perfil del cliente. RUN, dirección y teléfono se guardan cifrados."""
import os
import re
import uuid
from datetime import date, datetime
from typing import Optional

from fastapi import Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import Boolean, String, Text, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, Session

from cb_common.app import make_app, make_db
from cb_common.crypto import enc, dec, blind_index
from cb_common.secrets_provider import SecretProvider
from cb_common.security import make_guard, need_roles, only_from, Caller

SERVICE = "clients"
sp = SecretProvider(SERVICE)
guard = make_guard(sp)
engine, SessionLocal = make_db(os.getenv("DATABASE_URL", "sqlite:///.data/clients.db"))
app = make_app(SERVICE, "Clients")

# comuna -> (provincia, región). Fuente única; el frontend la consulta por GET /geo/comunas.
COMUNAS = {
    "Maipú": ("Santiago", "Metropolitana"), "Ñuñoa": ("Santiago", "Metropolitana"),
    "Providencia": ("Santiago", "Metropolitana"), "San Miguel": ("Santiago", "Metropolitana"),
    "Independencia": ("Santiago", "Metropolitana"), "Cerrillos": ("Santiago", "Metropolitana"),
    "Valparaíso": ("Valparaíso", "Valparaíso"), "Viña del Mar": ("Valparaíso", "Valparaíso"),
    "Quilpué": ("Valparaíso", "Valparaíso"), "Concepción": ("Concepción", "Biobío"),
    "Talcahuano": ("Concepción", "Biobío"), "Chiguayante": ("Concepción", "Biobío"),
}


class Base(DeclarativeBase):
    pass


class Client(Base):
    __tablename__ = "clients"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_enc: Mapped[str] = mapped_column(Text)
    run_idx: Mapped[str] = mapped_column(String(64), unique=True)
    nombre: Mapped[str] = mapped_column(String(120))
    direccion_enc: Mapped[str] = mapped_column(Text)
    comuna: Mapped[str] = mapped_column(String(60))
    provincia: Mapped[str] = mapped_column(String(60))
    region: Mapped[str] = mapped_column(String(60))
    fnac: Mapped[str] = mapped_column(String(10))
    sexo: Mapped[str] = mapped_column(String(1))
    email: Mapped[str] = mapped_column(String(200), unique=True)
    email_verificado: Mapped[bool] = mapped_column(Boolean, default=False)
    telefono_enc: Mapped[str] = mapped_column(Text)
    creado: Mapped[str] = mapped_column(String(30))


@app.on_event("startup")
def init():
    os.makedirs(".data", exist_ok=True)
    Base.metadata.create_all(engine)


def db():
    with SessionLocal() as s:
        yield s


def run_ok(run: str) -> bool:
    """Valida RUN chileno (módulo 11). Formato 12345678-9 o 12.345.678-9."""
    m = re.fullmatch(r"(\d{1,2}(?:\.?\d{3}){2})-([\dkK])", run.strip())
    if not m:
        return False
    body, dv = m.group(1).replace(".", ""), m.group(2).upper()
    total, mul = 0, 2
    for ch in reversed(body):
        total += int(ch) * mul
        mul = 2 if mul == 7 else mul + 1
    r = 11 - total % 11
    exp = "0" if r == 11 else "K" if r == 10 else str(r)
    return dv == exp


def norm_run(run: str) -> str:
    return run.replace(".", "").upper().strip()


class ClientIn(BaseModel):
    run: str = Field(max_length=14)
    nombre: str = Field(min_length=3, max_length=120)
    direccion: str = Field(min_length=3, max_length=200)
    comuna: str = Field(max_length=60)
    fnac: date
    sexo: str = Field(pattern="^[FMO]$")
    email: str = Field(max_length=200, pattern=r"^[^\s@]+@[^\s@]+\.[^\s@]{2,}$")
    telefono: str = Field(pattern=r"^\+?[\d\s]{8,16}$")
    email_verificado: bool = False


def validate(body: ClientIn):
    if not run_ok(body.run):
        raise HTTPException(422, "RUN inválido")
    if body.comuna not in COMUNAS:
        raise HTTPException(422, "Comuna no válida")
    today = date.today()
    age = today.year - body.fnac.year - ((today.month, today.day) < (body.fnac.month, body.fnac.day))
    if age < 18 or age > 120:  # venta de alcohol: solo mayores de edad (supuesto de negocio)
        raise HTTPException(422, "Debes ser mayor de 18 años para registrarte")


def dto_owner(c: Client):
    k = sp.get("data-enc-key")
    return {"id": c.id, "run": dec(k, c.run_enc), "nombre": c.nombre, "direccion": dec(k, c.direccion_enc),
            "comuna": c.comuna, "provincia": c.provincia, "region": c.region, "fnac": c.fnac, "sexo": c.sexo,
            "email": c.email, "email_verificado": c.email_verificado, "telefono": dec(k, c.telefono_enc)}


def create_client(s: Session, body: ClientIn) -> Client:
    validate(body)
    k = sp.get("data-enc-key")
    prov, reg = COMUNAS[body.comuna]
    c = Client(id=uuid.uuid4().hex, run_enc=enc(k, norm_run(body.run)), run_idx=blind_index(k, norm_run(body.run)),
               nombre=body.nombre.strip(), direccion_enc=enc(k, body.direccion.strip()), comuna=body.comuna,
               provincia=prov, region=reg, fnac=body.fnac.isoformat(), sexo=body.sexo, email=body.email.lower(),
               email_verificado=body.email_verificado, telefono_enc=enc(k, body.telefono.strip()),
               creado=datetime.utcnow().isoformat())
    s.add(c)
    try:
        s.commit()
    except IntegrityError:
        s.rollback()
        raise HTTPException(409, "Ya existe un cliente con ese RUN o correo")
    return c


# ---------- público ----------
@app.get("/geo/comunas")
def geo(caller: Caller = Depends(guard)):
    only_from(caller, "gateway")
    out = {}
    for com, (_, reg) in COMUNAS.items():
        out.setdefault(reg, []).append(com)
    return out


# ---------- cliente autenticado ----------
@app.get("/clients/me")
def me(caller: Caller = Depends(guard), s: Session = Depends(db)):
    only_from(caller, "gateway")
    ident = need_roles(caller, "cliente")
    c = s.get(Client, ident.client_id)
    if not c:
        raise HTTPException(404, "Cliente no encontrado")
    return dto_owner(c)


# ---------- administración ----------
@app.get("/clients")
def list_clients(caller: Caller = Depends(guard), s: Session = Depends(db)):
    only_from(caller, "gateway")
    need_roles(caller, "administrador")
    return [dto_owner(c) for c in s.scalars(select(Client).order_by(Client.creado))]


@app.get("/clients/{cid}")
def get_client(cid: str, caller: Caller = Depends(guard), s: Session = Depends(db)):
    only_from(caller, "gateway")
    need_roles(caller, "administrador")
    c = s.get(Client, cid)
    if not c:
        raise HTTPException(404, "Cliente no encontrado")
    return dto_owner(c)


class ClientUpdate(BaseModel):
    nombre: str = Field(min_length=3, max_length=120)
    direccion: str = Field(min_length=3, max_length=200)
    comuna: str = Field(max_length=60)
    telefono: str = Field(pattern=r"^\+?[\d\s]{8,16}$")


@app.put("/clients/{cid}")
def update_client(cid: str, body: ClientUpdate, caller: Caller = Depends(guard), s: Session = Depends(db)):
    """Reemplaza los campos editables. RUN, correo y fecha de nacimiento no se modifican por esta ruta."""
    only_from(caller, "gateway")
    need_roles(caller, "administrador")
    c = s.get(Client, cid)
    if not c:
        raise HTTPException(404, "Cliente no encontrado")
    if body.comuna not in COMUNAS:
        raise HTTPException(422, "Comuna no válida")
    k = sp.get("data-enc-key")
    c.nombre, c.comuna = body.nombre.strip(), body.comuna
    c.provincia, c.region = COMUNAS[body.comuna]
    c.direccion_enc, c.telefono_enc = enc(k, body.direccion.strip()), enc(k, body.telefono.strip())
    s.commit()
    return dto_owner(c)


# ---------- internas ----------
@app.post("/internal/clients", status_code=201)
def internal_create(body: ClientIn, caller: Caller = Depends(guard), s: Session = Depends(db)):
    only_from(caller, "auth")
    c = create_client(s, body)
    return {"id": c.id}


@app.delete("/internal/clients/{cid}", status_code=204)
def internal_compensate(cid: str, caller: Caller = Depends(guard), s: Session = Depends(db)):
    """Compensación del registro si auth no pudo crear la credencial (solo perfiles recién creados)."""
    only_from(caller, "auth")
    c = s.get(Client, cid)
    if c:
        s.delete(c)
        s.commit()


@app.get("/internal/clients/{cid}")
def internal_get(cid: str, caller: Caller = Depends(guard), s: Session = Depends(db)):
    """Mínimo necesario por llamador: orders (boleta/despacho) vs graphql (solo nombre y comuna)."""
    only_from(caller, "orders", "graphql")
    c = s.get(Client, cid)
    if not c:
        raise HTTPException(404, "Cliente no encontrado")
    if caller.service == "graphql":
        return {"id": c.id, "nombre": c.nombre, "comuna": c.comuna}
    k = sp.get("data-enc-key")
    return {"id": c.id, "nombre": c.nombre, "email": c.email, "direccion": dec(k, c.direccion_enc), "comuna": c.comuna}
