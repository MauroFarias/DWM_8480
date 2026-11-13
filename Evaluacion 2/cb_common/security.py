"""Identidad entre componentes.

* Servicio -> servicio: cabeceras X-Service-Name + X-Service-Key. La clave es POR PAR
  (el llamador guarda 'to-<destino>', el destino guarda 'from-<llamador>').
* Usuario: el gateway valida el JWT del navegador y reenvía una identidad firmada de
  vida corta en X-Identity (HS256, clave 'identity-key'). Los servicios la verifican.
"""
import hmac
import time
import uuid
from dataclasses import dataclass
from typing import Optional

import jwt
from fastapi import HTTPException, Request

from .secrets_provider import SecretProvider


@dataclass
class Identity:
    sub: str
    role: str
    client_id: Optional[str]
    name: str = ""


@dataclass
class Caller:
    service: str
    identity: Optional[Identity]


def sign_identity(sp: SecretProvider, ident: Identity, jti: str = "") -> str:
    now = int(time.time())
    return jwt.encode({"sub": ident.sub, "role": ident.role, "cid": ident.client_id,
                       "name": ident.name, "jti": jti, "iat": now, "exp": now + 30,
                       "iss": "cb-gateway"}, sp.get("identity-key"), algorithm="HS256")


def make_guard(sp: SecretProvider):
    """Devuelve dependencia: valida llamador (X-Service-*) e identidad opcional."""

    def guard(request: Request) -> Caller:
        name = request.headers.get("x-service-name", "")
        key = request.headers.get("x-service-key", "")
        try:
            expected = sp.get(f"from-{name}") if name else None
        except RuntimeError:
            expected = None
        if not expected or not hmac.compare_digest(key.encode(), expected.encode()):
            raise HTTPException(403, "Llamada interna no autorizada")
        ident = None
        raw = request.headers.get("x-identity")
        if raw:
            try:
                c = jwt.decode(raw, sp.get("identity-key"), algorithms=["HS256"], issuer="cb-gateway")
                ident = Identity(c["sub"], c["role"], c.get("cid"), c.get("name", ""))
            except jwt.PyJWTError:
                raise HTTPException(401, "Identidad inválida o expirada")
        return Caller(name, ident)

    return guard


def only_from(caller: Caller, *services: str):
    if caller.service not in services:
        raise HTTPException(403, "Este llamador no puede usar esta operación")


def need_roles(caller: Caller, *roles: str) -> Identity:
    """Exige identidad de usuario con uno de los roles. 401 si no hay identidad, 403 si no tiene rol."""
    if caller.identity is None:
        raise HTTPException(401, "Autenticación requerida")
    if roles and caller.identity.role not in roles:
        raise HTTPException(403, "Rol sin permiso para esta operación")
    return caller.identity


class InternalClient:
    """Cliente HTTP hacia otro servicio privado: plazo corto, credencial por par, sin reintentos ciegos."""

    def __init__(self, sp: SecretProvider, me: str, target: str, base_url: str, timeout: float = 3.0):
        self.sp, self.me, self.target, self.base, self.timeout = sp, me, target, base_url.rstrip("/"), timeout

    def headers(self, identity_token: Optional[str], corr: Optional[str]):
        h = {"X-Service-Name": self.me, "X-Service-Key": self.sp.get(f"to-{self.target}"),
             "X-Correlation-Id": corr or uuid.uuid4().hex}
        if identity_token:
            h["X-Identity"] = identity_token
        return h
