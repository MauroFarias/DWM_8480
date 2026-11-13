"""Pruebas de ejecución contra el sistema levantado (scripts/run_local.sh). Ejecutar: pytest -v tests"""
import hashlib, hmac, json, os, random, time, uuid
import httpx
import pytest

GW = os.getenv("GW", "http://127.0.0.1:8000")
API = GW + "/api/v1"
SECRETS = json.load(open(os.getenv("CB_SECRETS_FILE", ".secrets/secrets.local.json")))
STAFF = {"admin": "Admin#Demo2026", "cajero": "Cajero#Demo2026", "despacho": "Despacho#Demo2026", "dueno": "Dueno#Demo2026"}
CPASS = "Cliente#Demo2026"
c = httpx.Client(timeout=10)


def valid_run():
    body = random.randint(10_000_000, 24_999_999)
    total, mul = 0, 2
    for ch in reversed(str(body)):
        total += int(ch) * mul
        mul = 2 if mul == 7 else mul + 1
    r = 11 - total % 11
    return f"{body}-{'0' if r == 11 else 'K' if r == 10 else r}"


def login(user, pw):
    r = c.post(f"{API}/auth/login", json={"username": user, "password": pw})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["access_token"]}


def register(tag):
    email = f"{tag}{uuid.uuid4().hex[:6]}@demo.cl"
    r = c.post(f"{API}/auth/register", json={
        "run": valid_run(), "nombre": f"Cliente {tag}", "direccion": "Los Aromos 234", "comuna": "Maipú",
        "fnac": "1994-03-12", "sexo": "F", "email": email, "telefono": "+56 9 8123 4455", "password": CPASS})
    assert r.status_code == 201, r.text
    return email


@pytest.fixture(scope="session")
def H():
    return {u: login(u, p) for u, p in STAFF.items()}


@pytest.fixture(scope="session")
def ana():
    return _mk("ana")


@pytest.fixture(scope="session")
def beto():
    return _mk("beto")


def _mk(tag):
    email = register(tag)
    return login(email, CPASS)


def stock(H, pid):
    return next(p for p in c.get(f"{API}/admin/products", headers=H["admin"]).json() if p["id"] == pid)["stock"]


def webhook(payment_id, amount, status="APPROVED", event_id=None, secret=None, ts=None):
    raw = json.dumps({"event_id": event_id or uuid.uuid4().hex, "payment_id": payment_id, "status": status, "amount": amount}).encode()
    ts = ts or str(int(time.time()))
    sig = hmac.new((secret or SECRETS["payments"]["webhook-secret"]).encode(), ts.encode() + b"." + raw, hashlib.sha256).hexdigest()
    return c.post(f"{API}/webhooks/provider", content=raw, headers={"Content-Type": "application/json", "X-Timestamp": ts, "X-Signature": sig})


def new_order(h, items):
    r = c.post(f"{API}/orders", headers=h, json={"items": items})
    return r


def pay_start(h, oid):
    return c.post(f"{API}/payments", headers=h, json={"order_id": oid, "plataforma": "Servipag"})


# ---------- V1: acceso anónimo al catálogo ----------
def test_v1_catalogo_anonimo_sin_stock_exacto():
    r = c.get(f"{API}/products?available=true")
    assert r.status_code == 200 and len(r.json()) >= 5
    for p in r.json():
        assert "stock" not in p and "activo" not in p
        assert p["precio"] > 0


# ---------- V2: operación protegida sin sesión ----------
@pytest.mark.parametrize("m,p", [("GET", "/orders"), ("POST", "/orders"), ("GET", "/me"), ("POST", "/payments"), ("GET", "/users")])
def test_v2_sin_sesion_401(m, p):
    assert c.request(m, API + p).status_code == 401


def test_v2_token_falso_o_manipulado_401():
    assert c.get(f"{API}/orders", headers={"Authorization": "Bearer abc.def.ghi"}).status_code == 401


# ---------- V3: administración desde rol sin permiso ----------
def test_v3_cliente_no_administra(ana):
    assert c.get(f"{API}/admin/products", headers=ana).status_code == 403
    assert c.delete(f"{API}/products/P1", headers=ana).status_code == 403
    assert c.get(f"{API}/clients", headers=ana).status_code == 403
    assert c.post(f"{API}/users", headers=ana, json={}).status_code == 403


def test_v3_roles_staff_cruzados(H):
    assert c.get(f"{API}/dispatch/orders", headers=H["cajero"]).status_code == 403
    assert c.get(f"{API}/reports/sales", headers=H["despacho"]).status_code == 403
    assert c.delete(f"{API}/products/P1", headers=H["dueno"]).status_code == 403  # dueño edita, no descontinúa
    assert c.get(f"{API}/cashier/sales", headers=H["cajero"]).status_code == 200


def test_v3_cabecera_identidad_falsa_se_ignora(ana):
    h = dict(ana, **{"X-Identity": "eyJhbGciOiJIUzI1NiJ9.e30.x", "X-Service-Name": "gateway", "X-Service-Key": "x"})
    assert c.get(f"{API}/admin/products", headers=h).status_code == 403


# ---------- V5: acceso directo al backend ----------
@pytest.mark.parametrize("port,path", [(8001, "/users"), (8002, "/clients"), (8003, "/admin/products"), (8004, "/orders"),
                                       (8005, "/payments/x"), (8006, "/admin/outbox"), (8007, "/graphql")])
def test_v5_acceso_directo_sin_credencial_403(port, path):
    assert c.get(f"http://127.0.0.1:{port}{path}").status_code == 403


def test_v5_credencial_de_otro_par_no_sirve_para_mark_paid():
    # el gateway conoce 'to-orders', pero mark-paid solo admite al llamador 'payments'
    h = {"X-Service-Name": "gateway", "X-Service-Key": SECRETS["gateway"]["to-orders"]}
    assert c.post("http://127.0.0.1:8004/internal/orders/1/mark-paid", headers=h,
                  json={"payment_id": "x", "amount": 1}).status_code == 403


def test_v5_rutas_internas_no_se_publican(ana):
    for p in ["/internal/orders/1/mark-paid", "/internal/introspect/x", "/internal/products/lookup"]:
        assert c.post(API + p, headers=ana, json={}).status_code == 404


# ---------- usuarios, registro y sesiones ----------
def test_registro_valida_run_edad_correo_y_duplicados():
    base = {"nombre": "Pedro Pérez", "direccion": "Calle 1 123", "comuna": "Maipú", "fnac": "1990-01-01", "sexo": "M",
            "telefono": "+56 9 1111 2222", "password": CPASS}
    assert c.post(f"{API}/auth/register", json=dict(base, run="12345678-9", email="a@demo.cl")).status_code == 422   # DV malo
    assert c.post(f"{API}/auth/register", json=dict(base, run=valid_run(), email="a@noexiste.cl")).status_code == 422  # correo inexistente
    assert c.post(f"{API}/auth/register", json=dict(base, run=valid_run(), email="m@demo.cl", fnac="2015-01-01")).status_code == 422  # menor
    assert c.post(f"{API}/auth/register", json=dict(base, run=valid_run(), email="b@demo.cl", password="corta")).status_code == 422
    email = register("dup")
    assert c.post(f"{API}/auth/register", json=dict(base, run=valid_run(), email=email)).status_code == 409


def test_login_mensaje_uniforme_y_bloqueo():
    r1 = c.post(f"{API}/auth/login", json={"username": "noexiste@demo.cl", "password": "xxxxxxxxxxx"})
    email = register("lock")
    r2 = c.post(f"{API}/auth/login", json={"username": email, "password": "incorrecta1A"})
    assert r1.status_code == r2.status_code == 401 and r1.json() == r2.json()
    for _ in range(5):
        c.post(f"{API}/auth/login", json={"username": email, "password": "incorrecta1A"})
    assert c.post(f"{API}/auth/login", json={"username": email, "password": CPASS}).status_code == 429


def test_logout_revoca_token():
    h = _mk("out")
    assert c.get(f"{API}/me", headers=h).status_code == 200
    assert c.post(f"{API}/auth/logout", headers=h).status_code == 204
    assert c.get(f"{API}/me", headers=h).status_code == 401


def test_perfil_propio_devuelve_datos_y_no_expone_hash(ana):
    d = c.get(f"{API}/me", headers=ana).json()
    assert d["comuna"] == "Maipú" and d["run"] and "password" not in json.dumps(d).lower()


def test_datos_personales_cifrados_en_reposo():
    import sqlite3
    row = sqlite3.connect(".data/clients.db").execute("select run_enc, direccion_enc, telefono_enc from clients limit 1").fetchone()
    assert all(v.startswith("gAAAA") for v in row)  # Fernet
    hashes = sqlite3.connect(".data/auth.db").execute("select password_hash from credentials").fetchall()
    assert all(h[0].startswith("$argon2id$") for h in hashes)


# ---------- pedidos, propiedad y precios ----------
def test_pedido_precio_lo_decide_el_servidor_y_propiedad(ana, beto):
    r = c.post(f"{API}/orders", headers=ana, json={"items": [{"product_id": "P1", "cantidad": 2}], "total": 1, "precio": 1})
    assert r.status_code == 201, r.text
    o = r.json()
    assert o["total"] == 2 * 2990 and o["estado"] == "PENDIENTE_PAGO" and o["items"][0]["precio_unit"] == 2990
    assert c.get(f"{API}/orders/{o['id']}", headers=ana).status_code == 200
    r = c.get(f"{API}/orders/{o['id']}", headers=beto)           # V4: pedido ajeno
    assert r.status_code == 404 and "Aromos" not in r.text
    assert o["id"] not in [x["id"] for x in c.get(f"{API}/orders", headers=beto).json()]
    assert c.post(f"{API}/orders/{o['id']}/cancel", headers=beto, json={"motivo": "intento"}).status_code == 404
    assert pay_start(beto, o["id"]).status_code == 404            # tampoco puede pagar un pedido ajeno


def test_validaciones_de_pedido(ana):
    assert new_order(ana, []).status_code == 422
    assert new_order(ana, [{"product_id": "P1", "cantidad": 0}]).status_code == 422
    assert new_order(ana, [{"product_id": "P1", "cantidad": 99}]).status_code == 422
    assert new_order(ana, [{"product_id": "NOPE", "cantidad": 1}]).status_code == 422


def test_stock_reserva_sobreventa_y_recuperacion(H, ana, beto):
    before = stock(H, "P5")                                          # P5 tiene 8
    r = new_order(ana, [{"product_id": "P5", "cantidad": 5}])
    assert r.status_code == 201 and stock(H, "P5") == before - 5
    r2 = new_order(beto, [{"product_id": "P5", "cantidad": 5}])      # solo quedan 3
    assert r2.status_code == 409 and stock(H, "P5") == before - 5
    r3 = c.post(f"{API}/orders/{r.json()['id']}/cancel", headers=ana, json={"motivo": "me arrepentí"})
    assert r3.status_code == 200 and r3.json()["estado"] == "ANULADO" and stock(H, "P5") == before
    assert c.post(f"{API}/orders/{r.json()['id']}/cancel", headers=ana, json={"motivo": "otra vez"}).status_code == 409
    assert c.post(f"{API}/orders/{r.json()['id']}/cancel", headers=ana, json={}).status_code == 422


def test_pedido_con_producto_sin_stock(ana):
    assert new_order(ana, [{"product_id": "P7", "cantidad": 1}]).status_code == 409


# ---------- pagos ----------
def test_v6_pago_firma_monto_duplicado_y_boleta(H, ana):
    o = new_order(ana, [{"product_id": "P2", "cantidad": 1}, {"product_id": "P3", "cantidad": 2}]).json()
    total = 3490 + 2 * 3990
    p = pay_start(ana, o["id"])
    assert p.status_code == 201 and p.json()["monto"] == total
    pid = p.json()["id"]
    assert pay_start(ana, o["id"]).json()["id"] == pid                 # idempotente
    # el navegador intenta "pagar" por su cuenta
    assert c.post(f"{API}/payments/{pid}/confirm", headers=ana, json={"estado": "PAGADO"}).status_code == 404
    assert c.post(f"{API}/orders/{o['id']}/pay", headers=ana, json={}).status_code == 404
    # aviso falso: firma inválida / timestamp viejo
    assert webhook(pid, total, secret="otra-clave").status_code == 401
    assert webhook(pid, total, ts=str(int(time.time()) - 4000)).status_code == 401
    assert c.post(f"{API}/webhooks/provider", json={"event_id": "e" * 10, "payment_id": pid, "status": "APPROVED", "amount": total}).status_code == 401
    # firma válida pero monto alterado
    assert webhook(pid, 1).status_code == 422
    assert c.get(f"{API}/orders/{o['id']}", headers=ana).json()["estado"] == "PENDIENTE_PAGO"
    # el proveedor real confirma (vía sandbox, servidor a servidor)
    r = httpx.post(f"http://127.0.0.1:8010/checkout/{pid}/confirm", json={"resultado": "approve"})
    assert r.status_code == 200 and r.json()["webhook_http"] == 200
    ped = c.get(f"{API}/orders/{o['id']}", headers=ana).json()
    assert ped["estado"] == "PAGADO" and ped["boleta"].startswith("B-")
    # el mismo aviso dos veces: no se procesa de nuevo
    rp = httpx.post(f"http://127.0.0.1:8010/sandbox/replay/{pid}").json()
    assert rp["respuesta"].get("duplicate") is True
    out = c.get(f"{API}/admin/outbox", headers=H["admin"]).json()
    assert len([x for x in out if x["tipo"] == "BOLETA" and f"{ped['boleta']}" in x["asunto"]]) == 1
    # el pedido pagado ya no admite otro pago
    assert pay_start(ana, o["id"]).status_code == 409
    # caja virtual y despacho lo ven
    assert any(s["boleta"] == ped["boleta"] for s in c.get(f"{API}/cashier/sales", headers=H["cajero"]).json())
    return o["id"]


def test_pago_rechazado_y_pago_tardio(H, ana):
    o = new_order(ana, [{"product_id": "P4", "cantidad": 1}]).json()
    pid = pay_start(ana, o["id"]).json()["id"]
    assert httpx.post(f"http://127.0.0.1:8010/checkout/{pid}/confirm", json={"resultado": "reject"}).json()["webhook_http"] == 200
    assert c.get(f"{API}/orders/{o['id']}", headers=ana).json()["estado"] == "PENDIENTE_PAGO"
    # pedido anulado y luego llega una aprobación tardía: no se marca pagado, queda reembolso pendiente
    o2 = new_order(ana, [{"product_id": "P4", "cantidad": 1}]).json()
    pid2 = pay_start(ana, o2["id"]).json()["id"]
    assert c.post(f"{API}/orders/{o2['id']}/cancel", headers=ana, json={"motivo": "tarde"}).status_code == 200
    r = httpx.post(f"http://127.0.0.1:8010/checkout/{pid2}/confirm", json={"resultado": "approve"}).json()
    assert r["respuesta"]["estado"] == "REEMBOLSO_PENDIENTE"
    assert c.get(f"{API}/orders/{o2['id']}", headers=ana).json()["estado"] == "ANULADO"


# ---------- despacho y reportes ----------
def test_despacho_flujo_y_reporte(H, ana):
    o = new_order(ana, [{"product_id": "P6", "cantidad": 1}]).json()
    pid = pay_start(ana, o["id"]).json()["id"]
    httpx.post(f"http://127.0.0.1:8010/checkout/{pid}/confirm", json={"resultado": "approve"})
    q = c.get(f"{API}/dispatch/orders", headers=H["despacho"]).json()
    mine = next(x for x in q if x["id"] == o["id"])
    assert "Los Aromos 234" in mine["direccion_despacho"] and mine["fuera_de_radio"] is False
    assert c.post(f"{API}/dispatch/orders/{o['id']}/deliver", headers=H["despacho"]).status_code == 409   # salta estado
    assert "ORDEN DE DESPACHO" in c.get(f"{API}/dispatch/orders/{o['id']}/print", headers=H["despacho"]).text
    assert c.post(f"{API}/dispatch/orders/{o['id']}/start", headers=H["despacho"]).json()["estado"] == "EN_DESPACHO"
    assert c.post(f"{API}/orders/{o['id']}/cancel", headers=ana, json={"motivo": "ya en ruta"}).status_code == 409  # cliente no
    assert c.post(f"{API}/dispatch/orders/{o['id']}/deliver", headers=H["despacho"]).json()["estado"] == "ENTREGADO"
    rep = c.get(f"{API}/reports/sales", headers=H["dueno"]).json()
    assert rep["ventas"] >= 2 and rep["ingresos"] >= 6990 and any("Growler" in x["producto"] for x in rep["por_producto"])
    assert c.get(f"{API}/reports/sales?desde=2026-12-01&hasta=2026-01-01", headers=H["dueno"]).status_code == 422
    assert c.get(f"{API}/reports/sales?desde=basura", headers=H["dueno"]).status_code == 422
    # el dueño no ve direcciones en la lista de pedidos
    assert all("direccion_despacho" not in x for x in c.get(f"{API}/orders", headers=H["dueno"]).json())


def test_admin_cancela_en_despacho_y_devuelve_stock(H, ana):
    b = stock(H, "P8")
    o = new_order(ana, [{"product_id": "P8", "cantidad": 1}]).json()
    pid = pay_start(ana, o["id"]).json()["id"]
    httpx.post(f"http://127.0.0.1:8010/checkout/{pid}/confirm", json={"resultado": "approve"})
    c.post(f"{API}/dispatch/orders/{o['id']}/start", headers=H["despacho"])
    r = c.post(f"{API}/orders/{o['id']}/cancel", headers=H["admin"], json={"motivo": "cliente no atiende"})
    assert r.status_code == 200 and r.json()["reembolso_pendiente"] is True and stock(H, "P8") == b


# ---------- mantenedores ----------
def test_mantenedor_productos_soft_delete_conserva_historial(H, ana):
    r = c.post(f"{API}/products", headers=H["dueno"], json={"nombre": "Test Stout", "estilo": "Stout", "formato": "Botella 330ml",
                                                           "abv": 6.0, "precio": 3500, "stock": 5})
    assert r.status_code == 201
    pid = r.json()["id"]
    assert c.post(f"{API}/products", headers=H["dueno"], json={"nombre": "x", "estilo": "x", "formato": "x", "abv": 5, "precio": -1, "stock": 1}).status_code == 422
    o = new_order(ana, [{"product_id": pid, "cantidad": 1}]).json()
    assert c.delete(f"{API}/products/{pid}", headers=H["admin"]).status_code == 204
    assert pid not in [p["id"] for p in c.get(f"{API}/products").json()]
    assert c.get(f"{API}/products/{pid}").status_code == 404
    assert c.get(f"{API}/orders/{o['id']}", headers=ana).json()["items"][0]["nombre"] == "Test Stout"


def test_mantenedor_usuarios(H):
    u = f"op{uuid.uuid4().hex[:5]}"
    r = c.post(f"{API}/users", headers=H["admin"], json={"username": u, "password": "Operador#2026x", "nombre": "Operador Test", "rol": "despacho"})
    assert r.status_code == 201 and "password" not in r.text
    assert c.post(f"{API}/users", headers=H["admin"], json={"username": u, "password": "Operador#2026x", "nombre": "Dup Dup", "rol": "despacho"}).status_code == 409
    assert c.post(f"{API}/users", headers=H["admin"], json={"username": "zz9", "password": "debil", "nombre": "Weak Pw", "rol": "despacho"}).status_code == 422
    assert c.post(f"{API}/users", headers=H["admin"], json={"username": "zz8", "password": "Operador#2026x", "nombre": "Bad Role", "rol": "cliente"}).status_code == 422
    assert login(u, "Operador#2026x")


def test_admin_registra_cliente_y_lista(H):
    r = c.post(f"{API}/admin/clients", headers=H["admin"], json={
        "run": valid_run(), "nombre": "Cliente Local", "direccion": "Av. X 1", "comuna": "Ñuñoa", "fnac": "1985-05-05", "sexo": "M",
        "email": f"local{uuid.uuid4().hex[:5]}@demo.cl", "telefono": "+56 9 5555 4444", "password": CPASS})
    assert r.status_code == 201
    assert any(x["id"] == r.json()["client_id"] for x in c.get(f"{API}/clients", headers=H["admin"]).json())


# ---------- GraphQL ----------
def gql(h, q):
    return c.post(f"{API}/graphql", headers=h, json={"query": q})


def test_v8_graphql_agrega_orders_e_inventory_y_respeta_propiedad(H, ana, beto):
    o = new_order(ana, [{"product_id": "P1", "cantidad": 1}, {"product_id": "P2", "cantidad": 3}]).json()
    q = "{ resumenPedido(id:%d){ id estado total items{ nombre cantidad estilo abv disponibleAhora } } }" % o["id"]
    d = gql(ana, q).json()
    assert "errors" not in d, d
    items = d["data"]["resumenPedido"]["items"]
    assert {i["estilo"] for i in items} == {"Golden Ale", "India Pale Ale"} and d["data"]["resumenPedido"]["total"] == 2990 + 3 * 3490
    # pedido ajeno: error y SIN datos
    d2 = gql(beto, q).json()
    assert d2.get("data") is None or d2["data"]["resumenPedido"] is None
    assert d2["errors"] and "Aromos" not in json.dumps(d2) and "estilo" not in json.dumps(d2.get("data"))
    # campo restringido por rol
    d3 = gql(ana, "{ resumenPedido(id:%d){ id cliente{ nombre } } }" % o["id"]).json()
    assert d3["errors"] and "no autorizado" in d3["errors"][0]["message"].lower()
    d4 = gql(H["admin"], "{ resumenPedido(id:%d){ id cliente{ nombre comuna } } }" % o["id"]).json()
    assert d4["data"]["resumenPedido"]["cliente"]["comuna"] == "Maipú"
    # el esquema no expone dirección ni RUN
    assert gql(ana, "{ resumenPedido(id:%d){ direccionDespacho } }" % o["id"]).json()["errors"]
    # sin sesión
    assert c.post(f"{API}/graphql", json={"query": "{ misPedidos{ id } }"}).status_code == 401
    # misPedidos solo devuelve los del cliente y respeta la paginación
    mp = gql(ana, "{ misPedidos(limit:2){ id } }").json()["data"]["misPedidos"]
    assert 1 <= len(mp) <= 2
    mine = {x["id"] for x in c.get(f"{API}/orders", headers=ana).json()}
    assert {x["id"] for x in mp} <= mine


def test_v8_graphql_limite_de_profundidad():
    """El esquema no es recursivo (profundidad máx. real = 2 < 5), así que se verifica el MISMO limitador
    sobre el MISMO esquema con un límite menor y que el servicio lo declara con valor 5."""
    import asyncio, sys
    sys.path.insert(0, ".")
    os.environ.setdefault("CB_SECRETS_FILE", ".secrets/secrets.local.json")
    import strawberry
    from strawberry.extensions import QueryDepthLimiter
    from services.graphql import main as gm
    strict = strawberry.Schema(query=gm.Query, extensions=[lambda: QueryDepthLimiter(max_depth=1)])
    res = asyncio.run(strict.execute("{ misPedidos{ items{ nombre } } }"))
    assert res.errors and "depth" in res.errors[0].message.lower()
    assert "max_depth=5" in open("services/graphql/main.py").read()
    h = _mk("deep")
    assert gql(h, "{ misPedidos{ items{ nombre } cliente{ nombre } } }").status_code == 200   # dentro del límite


# ---------- robustez del gateway ----------
def test_gateway_ruta_inexistente_metodo_y_tamano(ana):
    assert c.get(f"{API}/nada").status_code == 404
    assert c.patch(f"{API}/products").status_code == 405
    assert c.post(f"{API}/orders", headers=ana, content=b"x" * 70000).status_code == 413
    assert c.post(f"{API}/orders", headers=ana, content=b"{no json").status_code == 422


def test_correlation_id_y_logs_sin_secretos(ana):
    cid = "test-corr-" + uuid.uuid4().hex[:8]
    r = c.get(f"{API}/orders", headers=dict(ana, **{"X-Correlation-Id": cid}))
    assert r.headers["X-Correlation-Id"] == cid
    time.sleep(0.3)
    log = open(".logs/orders.log").read()
    assert cid in log
    for sec in [ana["Authorization"][7:], SECRETS["gateway"]["to-orders"], SECRETS["gateway"]["identity-key"]]:
        assert sec not in log


def test_rate_limit_en_rutas_de_autenticacion():
    codes = [c.post(f"{API}/auth/login", json={"username": f"x{i}@demo.cl", "password": "nopenopenope"}).status_code for i in range(60)]
    assert 429 in codes
