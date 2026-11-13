"""API Gateway: ÚNICA entrada pública. Lista explícita de rutas/verbos, validación de JWT,
revocación, autorización por rol, límite de solicitudes y reenvío de identidad firmada.
Lo que no está en ROUTES no existe (404). Las rutas /internal/* nunca se publican."""
import os
import re
import time
from collections import defaultdict, deque

import httpx
import jwt
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from cb_common.app import make_logger, corr_id
from cb_common.secrets_provider import SecretProvider
from cb_common.security import Identity, sign_identity

import uuid

sp = SecretProvider("gateway")
log = make_logger("gateway")
app = FastAPI(title="Craft & Beer — API Gateway", docs_url=None, redoc_url=None)
V1 = "/api/v1"
URLS = {"auth": os.getenv("AUTH_URL", "http://127.0.0.1:8001"), "clients": os.getenv("CLIENTS_URL", "http://127.0.0.1:8002"),
        "inventory": os.getenv("INVENTORY_URL", "http://127.0.0.1:8003"), "orders": os.getenv("ORDERS_URL", "http://127.0.0.1:8004"),
        "payments": os.getenv("PAYMENTS_URL", "http://127.0.0.1:8005"), "notifications": os.getenv("NOTIFICATIONS_URL", "http://127.0.0.1:8006"),
        "graphql": os.getenv("GRAPHQL_URL", "http://127.0.0.1:8007")}
RL_DEFAULT = int(os.getenv("RL_DEFAULT_PER_MIN", "300"))
RL_AUTH = int(os.getenv("RL_AUTH_PER_MIN", "40"))
MAX_BODY = 64 * 1024
ANY = "*"  # cualquier usuario autenticado

# (id, método, ruta externa, roles, servicio, ruta interna, query permitidos, clase de límite)
R = [
    ("S01", "GET", "/products", None, "inventory", "/products", {"available", "style"}, "d"),
    ("S02", "GET", "/products/{id}", None, "inventory", "/products/{id}", set(), "d"),
    ("S03", "GET", "/geo/comunas", None, "clients", "/geo/comunas", set(), "d"),
    ("S04", "POST", "/auth/register", None, "auth", "/auth/register", set(), "a"),
    ("S05", "POST", "/auth/login", None, "auth", "/auth/login", set(), "a"),
    ("S06", "POST", "/auth/logout", {ANY}, None, None, set(), "d"),
    ("S07", "POST", "/auth/password", {ANY}, "auth", "/auth/password", set(), "a"),
    ("S08", "GET", "/me", {"cliente"}, "clients", "/clients/me", set(), "d"),
    ("S09", "GET", "/admin/products", {"administrador", "dueno"}, "inventory", "/admin/products", set(), "d"),
    ("S10", "POST", "/products", {"administrador", "dueno"}, "inventory", "/products", set(), "d"),
    ("S11", "PUT", "/products/{id}", {"administrador", "dueno"}, "inventory", "/products/{id}", set(), "d"),
    ("S12", "DELETE", "/products/{id}", {"administrador"}, "inventory", "/products/{id}", set(), "d"),
    ("S13", "GET", "/clients", {"administrador"}, "clients", "/clients", set(), "d"),
    ("S14", "GET", "/clients/{id}", {"administrador"}, "clients", "/clients/{id}", set(), "d"),
    ("S15", "PUT", "/clients/{id}", {"administrador"}, "clients", "/clients/{id}", set(), "d"),
    ("S16", "POST", "/admin/clients", {"administrador"}, "auth", "/auth/register-by-admin", set(), "d"),
    ("S17", "GET", "/users", {"administrador"}, "auth", "/users", set(), "d"),
    ("S18", "POST", "/users", {"administrador"}, "auth", "/users", set(), "d"),
    ("S19", "PUT", "/users/{id}", {"administrador"}, "auth", "/users/{id}", set(), "d"),
    ("S20", "POST", "/orders", {"cliente"}, "orders", "/orders", set(), "d"),
    ("S21", "GET", "/orders", {"cliente", "administrador", "cajero_virtual", "dueno"}, "orders", "/orders", {"estado", "limit", "offset"}, "d"),
    ("S22", "GET", "/orders/{id}", {"cliente", "administrador", "cajero_virtual", "dueno"}, "orders", "/orders/{id}", set(), "d"),
    ("S23", "POST", "/orders/{id}/cancel", {"cliente", "administrador"}, "orders", "/orders/{id}/cancel", set(), "d"),
    ("S24", "POST", "/payments", {"cliente"}, "payments", "/payments", set(), "d"),
    ("S25", "GET", "/payments/{id}", {"cliente"}, "payments", "/payments/{id}", set(), "d"),
    ("S26", "POST", "/webhooks/provider", None, "payments", "/webhooks/provider", set(), "d"),   # anónima + firma HMAC
    ("S27", "GET", "/dispatch/orders", {"despacho"}, "orders", "/dispatch/orders", set(), "d"),
    ("S28", "POST", "/dispatch/orders/{id}/start", {"despacho"}, "orders", "/dispatch/orders/{id}/start", set(), "d"),
    ("S29", "POST", "/dispatch/orders/{id}/deliver", {"despacho"}, "orders", "/dispatch/orders/{id}/deliver", set(), "d"),
    ("S30", "GET", "/dispatch/orders/{id}/print", {"despacho"}, "orders", "/dispatch/orders/{id}/print", set(), "d"),
    ("S31", "GET", "/reports/sales", {"dueno", "administrador"}, "orders", "/reports/sales", {"desde", "hasta"}, "d"),
    ("S32", "GET", "/cashier/sales", {"cajero_virtual", "administrador"}, "orders", "/cashier/sales", set(), "d"),
    ("S33", "GET", "/admin/outbox", {"administrador"}, "notifications", "/admin/outbox", set(), "d"),
    ("S34", "POST", "/graphql", {ANY}, "graphql", "/graphql", set(), "d"),
]


def compile_route(path: str):
    return re.compile("^" + V1 + re.sub(r"\{id\}", r"(?P<id>[A-Za-z0-9_-]{1,40})", path) + "$")


ROUTES = [(i, m, compile_route(p), roles, svc, ip, q, rl) for (i, m, p, roles, svc, ip, q, rl) in R]
HITS: dict = defaultdict(deque)


def limited(key: str, limit: int) -> bool:
    now = time.time()
    dq = HITS[key]
    while dq and dq[0] < now - 60:
        dq.popleft()
    if len(dq) >= limit:
        return True
    dq.append(now)
    return False


def err(status: int, msg: str, cid: str):
    return JSONResponse({"detail": msg}, status_code=status, headers={"X-Correlation-Id": cid})


origins = [o for o in os.getenv("FRONTEND_ORIGINS", "").split(",") if o]
if origins:
    app.add_middleware(CORSMiddleware, allow_origins=origins, allow_methods=["GET", "POST", "PUT", "DELETE"],
                       allow_headers=["Authorization", "Content-Type"])


@app.get("/health")
def health():
    return {"status": "OK", "service": "gateway"}


async def authenticate(request: Request):
    h = request.headers.get("authorization", "")
    if not h.lower().startswith("bearer "):
        return None, "Bearer token requerido"
    try:
        c = jwt.decode(h[7:].strip(), sp.get("jwt-signing-key"), algorithms=["HS256"], issuer="cb-auth",
                       options={"require": ["exp", "jti", "sub", "role"]})
    except jwt.PyJWTError:
        return None, "Token inválido o expirado"
    try:
        async with httpx.AsyncClient(timeout=3.0) as cl:
            r = await cl.get(f"{URLS['auth']}/internal/introspect/{c['jti']}",
                             headers={"X-Service-Name": "gateway", "X-Service-Key": sp.get("to-auth")})
        if r.status_code != 200:
            return None, "unavailable"
        if not r.json()["active"]:
            return None, "Sesión cerrada"
    except httpx.HTTPError:
        return None, "unavailable"                # falla cerrado: sin poder verificar revocación no se deja pasar
    return c, None


@app.api_route(V1 + "/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"])
async def route(path: str, request: Request):
    cid = request.headers.get("x-correlation-id") or uuid.uuid4().hex
    cid = re.sub(r"[^A-Za-z0-9-]", "", cid)[:40] or uuid.uuid4().hex
    corr_id.set(cid)
    full = request.url.path
    ip = request.client.host if request.client else "?"
    path_match = None
    for rid, method, rx, roles, svc, ipath, qallow, rl in ROUTES:
        m = rx.match(full)
        if m:
            path_match = True
            if method == request.method:
                break
    else:
        return err(405 if path_match else 404, "Método no permitido" if path_match else "Ruta no encontrada", cid)
    if limited(f"{rl}:{ip}", RL_AUTH if rl == "a" else RL_DEFAULT):
        return err(429, "Demasiadas solicitudes", cid)
    body = await request.body()
    if len(body) > MAX_BODY:
        return err(413, "Cuerpo demasiado grande", cid)
    claims = None
    if roles is not None:
        claims, why = await authenticate(request)
        if why == "unavailable":
            return err(503, "Servicio de autenticación no disponible", cid)
        if why:
            return err(401, why, cid)
        if ANY not in roles and claims["role"] not in roles:
            log.info("DENEGADO %s %s rol=%s", request.method, full, claims["role"])
            return err(403, "Rol sin permiso para esta operación", cid)
    if rid == "S06":                                   # logout: se revoca el jti hasta su expiración
        try:
            async with httpx.AsyncClient(timeout=3.0) as cl:
                await cl.post(f"{URLS['auth']}/internal/revoke", params={"jti": claims["jti"], "exp": claims["exp"]},
                              headers={"X-Service-Name": "gateway", "X-Service-Key": sp.get("to-auth")})
        except httpx.HTTPError:
            return err(503, "No se pudo cerrar la sesión; reintenta", cid)
        return Response(status_code=204, headers={"X-Correlation-Id": cid})
    # cabeceras hacia el interior: se construyen desde cero; las del navegador (X-Identity, X-Service-*) se descartan
    h = {"X-Service-Name": "gateway", "X-Service-Key": sp.get(f"to-{svc}"), "X-Correlation-Id": cid}
    if claims:
        h["X-Identity"] = sign_identity(sp, Identity(claims["sub"], claims["role"], claims.get("cid"), claims.get("name", "")))
    for name in ("content-type",):
        if request.headers.get(name):
            h[name] = request.headers[name]
    if rid == "S26":
        for name in ("x-signature", "x-timestamp"):
            if request.headers.get(name):
                h[name] = request.headers[name]
    target = URLS[svc] + ipath.replace("{id}", m.groupdict().get("id", ""))
    params = {k: v for k, v in request.query_params.items() if k in qallow}
    try:
        async with httpx.AsyncClient(timeout=8.0) as cl:
            up = await cl.request(request.method, target, params=params, content=body, headers=h)
    except httpx.TimeoutException:
        return err(504, "El servicio tardó demasiado en responder", cid)
    except httpx.RequestError:
        return err(502, "Servicio no disponible", cid)
    out = {"X-Correlation-Id": cid, "X-Content-Type-Options": "nosniff", "Cache-Control": "no-store"}
    if up.headers.get("content-type"):
        out["content-type"] = up.headers["content-type"]
    return Response(content=up.content, status_code=up.status_code, headers=out)


FRONTEND_DIR = os.getenv("FRONTEND_DIR")
if FRONTEND_DIR and os.path.isdir(FRONTEND_DIR):
    app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
