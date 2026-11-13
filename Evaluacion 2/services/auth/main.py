"""auth: credenciales, login (JWT corto), logout con revocación, registro de clientes y usuarios internos."""
import os
import time
import uuid
from datetime import datetime
from typing import Optional

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from fastapi import Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import Boolean, Integer, String, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, Session
import httpx

from cb_common.app import make_app, make_db, make_logger
from cb_common.secrets_provider import SecretProvider
from cb_common.security import make_guard, need_roles, only_from, Caller, InternalClient

SERVICE = "auth"
sp = SecretProvider(SERVICE)
guard = make_guard(sp)
log = make_logger(SERVICE)
engine, SessionLocal = make_db(os.getenv("DATABASE_URL", "sqlite:///.data/auth.db"))
app = make_app(SERVICE, "Auth")
ph = PasswordHasher()  # Argon2id: hash adaptativo con sal
TOKEN_TTL = int(os.getenv("TOKEN_TTL_SECONDS", "900"))
ROLES = {"administrador", "cajero_virtual", "despacho", "dueno", "cliente"}
STAFF_ROLES = ROLES - {"cliente"}
DUMMY_HASH = ph.hash("x" * 12)
clients_api = InternalClient(sp, SERVICE, "clients", os.getenv("CLIENTS_URL", "http://127.0.0.1:8002"))
notif_api = InternalClient(sp, SERVICE, "notifications", os.getenv("NOTIFICATIONS_URL", "http://127.0.0.1:8006"))


class Base(DeclarativeBase):
    pass


class Credential(Base):
    __tablename__ = "credentials"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    username: Mapped[str] = mapped_column(String(200), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(20))
    client_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    display_name: Mapped[str] = mapped_column(String(120))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    failed: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[int] = mapped_column(Integer, default=0)


class RevokedToken(Base):
    __tablename__ = "revoked_tokens"
    jti: Mapped[str] = mapped_column(String(40), primary_key=True)
    exp: Mapped[int] = mapped_column(Integer)


def seed_staff(s: Session):
    """Usuarios de demostración. Las contraseñas vienen de variables de entorno (SEED_PASSWORD_*), no del código."""
    base = [("admin", "administrador", "Administrador del sistema"), ("cajero", "cajero_virtual", "Cajero Virtual — turno web"),
            ("despacho", "despacho", "Encargado de Bodega y Despacho"), ("dueno", "dueno", "Dueño de Craft and Beer")]
    for user, role, name in base:
        pw = os.getenv(f"SEED_PASSWORD_{user.upper()}")
        if pw and not s.scalar(select(Credential).where(Credential.username == user)):
            s.add(Credential(id=uuid.uuid4().hex, username=user, password_hash=ph.hash(pw), role=role, display_name=name))
    s.commit()


@app.on_event("startup")
def init():
    os.makedirs(".data", exist_ok=True)
    Base.metadata.create_all(engine)
    with SessionLocal() as s:
        seed_staff(s)


def db():
    with SessionLocal() as s:
        yield s


def check_password_policy(pw: str):
    if len(pw) < 10 or pw.lower() == pw or not any(c.isdigit() for c in pw):
        raise HTTPException(422, "La contraseña debe tener 10+ caracteres, mayúsculas y números")


class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=200)
    password: str = Field(min_length=1, max_length=200)


@app.post("/auth/login")
def login(body: LoginIn, caller: Caller = Depends(guard), s: Session = Depends(db)):
    only_from(caller, "gateway")
    u = s.scalar(select(Credential).where(Credential.username == body.username.strip().lower()))
    now = int(time.time())
    if u and u.locked_until > now:
        raise HTTPException(429, "Cuenta bloqueada temporalmente por intentos fallidos")
    try:
        ph.verify(u.password_hash if u else DUMMY_HASH, body.password)
        ok = u is not None and u.active
    except VerifyMismatchError:
        ok = False
    if not ok:
        if u:
            u.failed += 1
            if u.failed >= 5:
                u.locked_until, u.failed = now + 300, 0
            s.commit()
        raise HTTPException(401, "Usuario o contraseña incorrectos")  # mismo mensaje exista o no el usuario
    u.failed = 0
    s.commit()
    jti = uuid.uuid4().hex
    token = jwt.encode({"sub": u.id, "role": u.role, "cid": u.client_id, "name": u.display_name, "jti": jti,
                        "iat": now, "exp": now + TOKEN_TTL, "iss": "cb-auth"}, sp.get("jwt-signing-key"), algorithm="HS256")
    return {"access_token": token, "token_type": "Bearer", "expires_in": TOKEN_TTL, "rol": u.role,
            "nombre": u.display_name, "client_id": u.client_id}


@app.post("/internal/revoke", status_code=204)
def revoke(jti: str, exp: int, caller: Caller = Depends(guard), s: Session = Depends(db)):
    only_from(caller, "gateway")
    if not s.get(RevokedToken, jti):
        s.add(RevokedToken(jti=jti, exp=exp))
        s.commit()


@app.get("/internal/introspect/{jti}")
def introspect(jti: str, caller: Caller = Depends(guard), s: Session = Depends(db)):
    only_from(caller, "gateway")
    s.query(RevokedToken).filter(RevokedToken.exp < int(time.time())).delete()
    s.commit()
    return {"active": s.get(RevokedToken, jti) is None}


class RegisterIn(BaseModel):
    run: str = Field(max_length=14)
    nombre: str = Field(min_length=3, max_length=120)
    direccion: str = Field(min_length=3, max_length=200)
    comuna: str = Field(max_length=60)
    fnac: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    sexo: str = Field(pattern="^[FMO]$")
    email: str = Field(max_length=200, pattern=r"^[^\s@]+@[^\s@]+\.[^\s@]{2,}$")
    telefono: str = Field(pattern=r"^\+?[\d\s]{8,16}$")
    password: str = Field(max_length=128)


def do_register(body: RegisterIn, s: Session, origin: str):
    check_password_policy(body.password)
    email = body.email.strip().lower()
    if s.scalar(select(Credential).where(Credential.username == email)):
        raise HTTPException(409, "Ya existe una cuenta con ese correo")
    try:
        v = httpx.post(f"{notif_api.base}/internal/email/validate", json={"email": email},
                       headers=notif_api.headers(None, None), timeout=notif_api.timeout)
        v.raise_for_status()
    except httpx.HTTPError:
        raise HTTPException(503, "No fue posible validar el correo ahora. Intenta más tarde.")
    if not v.json().get("valid"):
        raise HTTPException(422, "No pudimos verificar que ese correo exista. Revisa que esté bien escrito.")
    payload = body.model_dump(exclude={"password"})
    payload.update(email=email, email_verificado=True)
    try:
        r = httpx.post(f"{clients_api.base}/internal/clients", json=payload,
                       headers=clients_api.headers(None, None), timeout=clients_api.timeout)
    except httpx.HTTPError:
        raise HTTPException(503, "Servicio de clientes no disponible")
    if r.status_code != 201:
        raise HTTPException(r.status_code if r.status_code in (409, 422) else 502, r.json().get("detail", "Error al registrar"))
    cid = r.json()["id"]
    try:
        s.add(Credential(id=uuid.uuid4().hex, username=email, password_hash=ph.hash(body.password), role="cliente",
                         client_id=cid, display_name=body.nombre.strip()))
        s.commit()
    except IntegrityError:
        s.rollback()
        httpx.delete(f"{clients_api.base}/internal/clients/{cid}", headers=clients_api.headers(None, None), timeout=3)
        raise HTTPException(409, "Ya existe una cuenta con ese correo")
    try:  # aviso de bienvenida; si falla no se revierte el registro
        httpx.post(f"{notif_api.base}/internal/email/send", timeout=3, headers=notif_api.headers(None, None),
                   json={"to": email, "kind": "BIENVENIDA", "subject": "Tu cuenta en Craft and Beer está activa",
                         "body": f"Hola {body.nombre.strip()}, tu correo fue verificado y tu cuenta ({origin}) está activa.",
                         "dedupe_key": f"welcome-{cid}"})
    except httpx.HTTPError:
        log.warning("no se pudo encolar el aviso de bienvenida")
    return {"client_id": cid, "email": email, "email_verificado": True}


@app.post("/auth/register", status_code=201)
def register(body: RegisterIn, caller: Caller = Depends(guard), s: Session = Depends(db)):
    only_from(caller, "gateway")
    return do_register(body, s, "web")


@app.post("/auth/register-by-admin", status_code=201)
def register_by_admin(body: RegisterIn, caller: Caller = Depends(guard), s: Session = Depends(db)):
    only_from(caller, "gateway")
    need_roles(caller, "administrador")
    return do_register(body, s, "local")


class PasswordIn(BaseModel):
    actual: str = Field(max_length=128)
    nueva: str = Field(max_length=128)


@app.post("/auth/password", status_code=204)
def change_password(body: PasswordIn, caller: Caller = Depends(guard), s: Session = Depends(db)):
    only_from(caller, "gateway")
    ident = need_roles(caller)
    u = s.get(Credential, ident.sub)
    try:
        ph.verify(u.password_hash, body.actual)
    except VerifyMismatchError:
        raise HTTPException(403, "Contraseña actual incorrecta")
    check_password_policy(body.nueva)
    u.password_hash = ph.hash(body.nueva)
    s.commit()


# ---------- mantenedor de usuarios internos (administrador) ----------
class UserIn(BaseModel):
    username: str = Field(min_length=3, max_length=60, pattern=r"^[a-z0-9._-]+$")
    password: str = Field(max_length=128)
    nombre: str = Field(min_length=3, max_length=120)
    rol: str


class UserUpdate(BaseModel):
    nombre: str = Field(min_length=3, max_length=120)
    rol: str
    activo: bool
    password: Optional[str] = Field(None, max_length=128)


def user_dto(u: Credential):
    return {"id": u.id, "usuario": u.username, "nombre": u.display_name, "rol": u.role, "activo": u.active}


@app.get("/users")
def list_users(caller: Caller = Depends(guard), s: Session = Depends(db)):
    only_from(caller, "gateway")
    need_roles(caller, "administrador")
    return [user_dto(u) for u in s.scalars(select(Credential).where(Credential.role != "cliente"))]


@app.post("/users", status_code=201)
def create_user(body: UserIn, caller: Caller = Depends(guard), s: Session = Depends(db)):
    only_from(caller, "gateway")
    need_roles(caller, "administrador")
    if body.rol not in STAFF_ROLES:
        raise HTTPException(422, "Rol no válido")
    check_password_policy(body.password)
    u = Credential(id=uuid.uuid4().hex, username=body.username, password_hash=ph.hash(body.password), role=body.rol,
                   display_name=body.nombre.strip())
    s.add(u)
    try:
        s.commit()
    except IntegrityError:
        s.rollback()
        raise HTTPException(409, "Ese nombre de usuario ya existe")
    return user_dto(u)


@app.put("/users/{uid}")
def update_user(uid: str, body: UserUpdate, caller: Caller = Depends(guard), s: Session = Depends(db)):
    only_from(caller, "gateway")
    ident = need_roles(caller, "administrador")
    u = s.get(Credential, uid)
    if not u or u.role == "cliente":
        raise HTTPException(404, "Usuario no encontrado")
    if body.rol not in STAFF_ROLES:
        raise HTTPException(422, "Rol no válido")
    if u.id == ident.sub and (not body.activo or body.rol != "administrador"):
        raise HTTPException(409, "No puedes quitarte tu propio acceso de administrador")
    u.display_name, u.role, u.active = body.nombre.strip(), body.rol, body.activo
    if body.password:
        check_password_policy(body.password)
        u.password_hash = ph.hash(body.password)
    s.commit()
    return user_dto(u)
