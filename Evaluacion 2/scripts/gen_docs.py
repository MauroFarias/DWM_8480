"""Genera docs/INFORME.md. Las matrices de rutas y permisos se derivan de gateway/main.py (R) para que
la documentación describa exactamente lo implementado."""
import os, sys
sys.path.insert(0, ".")
os.environ.setdefault("CB_SECRETS_FILE", ".secrets/secrets.local.json")
from gateway.main import R

ROLE = {"cliente": "Cliente", "administrador": "Administrador", "cajero_virtual": "Cajero virtual", "despacho": "Encargado de despacho", "dueno": "Dueño"}
META = {  # id: (requisito, pantalla, propósito, datos)
 "S01": ("RF1, RF7", "P01 Catálogo", "Listar productos activos", "Público"),
 "S02": ("RF1", "P01 Catálogo", "Detalle de un producto", "Público"),
 "S03": ("RF5", "P05 Registro", "Comunas/regiones válidas", "Público"),
 "S04": ("RF5, RF6, RF11", "P05 Registro", "Registro de cliente con validación de correo", "Personal (RUN, dirección, teléfono) + credencial"),
 "S05": ("RF12 / CU1", "P05 Ingreso", "Autenticación; entrega JWT de 15 min", "Credencial"),
 "S06": ("RF12", "Todas · Cerrar sesión", "Revocar el token", "—"),
 "S07": ("RNF7", "P04 Mi cuenta", "Cambiar contraseña", "Credencial"),
 "S08": ("RF5", "P04 Mi cuenta", "Perfil propio", "Personal"),
 "S09": ("RF7, RF10 / CU4", "P06 Productos", "Catálogo completo con stock", "Interno de negocio"),
 "S10": ("RF10 / CU4", "P06 Productos", "Crear producto", "Interno de negocio"),
 "S11": ("RF7, RF10 / CU4", "P06 Productos", "Reemplazar precio/stock/estado", "Interno de negocio"),
 "S12": ("RF10 / CU4", "P06 Productos", "Descontinuar (baja lógica)", "Interno de negocio"),
 "S13": ("RF10 / CU3", "P07 Clientes", "Listar clientes", "Personal"),
 "S14": ("RF10 / CU3", "P07 Clientes", "Ver un cliente", "Personal"),
 "S15": ("RF10 / CU3", "P07 Clientes (API)", "Actualizar datos de contacto", "Personal"),
 "S16": ("RF6 / CU3", "P07 Clientes", "Registro desde el local", "Personal + credencial"),
 "S17": ("RF10 / CU2", "P08 Usuarios", "Listar usuarios internos", "Interno"),
 "S18": ("RF10 / CU2", "P08 Usuarios", "Crear usuario interno", "Credencial"),
 "S19": ("RF10 / CU2", "P08 Usuarios (API)", "Cambiar rol/estado/clave", "Credencial"),
 "S20": ("RF1 / CU5", "P02 Mi pedido", "Crear pedido (precio y stock decididos en servidor)", "Personal (dirección)"),
 "S21": ("RF3, RF4", "P03, P09", "Listar pedidos (propios o según rol)", "Personal según rol"),
 "S22": ("RF3, RF4", "P03", "Ver un pedido", "Personal según rol"),
 "S23": ("RF3 / CU7", "P03, P09", "Anular con motivo; devuelve stock", "Interno"),
 "S24": ("RF2, RF4", "P02 Mi pedido", "Iniciar pago con proveedor externo", "Interno"),
 "S25": ("RF2", "P02", "Consultar estado de un pago propio", "Interno"),
 "S26": ("RF2, RF4", "(proveedor externo)", "Webhook firmado: confirma o rechaza el pago", "Interno"),
 "S27": ("RF13 / CU9", "P11 Despacho", "Cola de despacho en orden de llegada", "Personal (dirección)"),
 "S28": ("RF13", "P11 Despacho", "PAGADO → EN_DESPACHO", "Interno"),
 "S29": ("RF13", "P11 Despacho", "EN_DESPACHO → ENTREGADO", "Interno"),
 "S30": ("RF13 / CU9", "P11 Despacho", "Orden imprimible", "Personal (dirección)"),
 "S31": ("RF9 / CU8", "P12 Reporte", "Ventas por período", "Interno agregado"),
 "S32": ("RF4, RF8 / CU6", "P10 Caja virtual", "Ventas y boletas de la caja virtual", "Interno"),
 "S33": ("RF8, RNF4", "(evidencia)", "Avisos emitidos por el outbox (sin cuerpo)", "Interno"),
 "S34": ("RF3, RF4", "P03 Mis compras", "Agregación GraphQL orders+inventory+clients", "Según campo"),
}
routes = ["| ID | Req. | Pantalla | Verbo y ruta externa | Propósito | Acceso | Destino interno | Datos |", "|---|---|---|---|---|---|---|---|"]
perms = ["| ID | Ruta | GET | POST | PUT | DELETE | Quién | Condición / dónde se verifica |", "|---|---|---|---|---|---|---|---|"]
for (i, m, p, roles, svc, ip, q, rl) in R:
    req, scr, purp, data = META[i]
    acc = "anónimo" + (" + firma HMAC" if i == "S26" else "") if roles is None else ("cualquier sesión válida" if "*" in roles else ", ".join(ROLE[r] for r in sorted(roles)))
    dest = "gateway (revoca jti)" if svc is None else f"{svc}: {ip}"
    routes.append(f"| {i} | {req} | {scr} | `{m} /api/v1{p}` | {purp} | {acc} | {dest} | {data} |")
    cond = {"S08": "solo su propio perfil (client_id del JWT)", "S21": "cliente: solo los suyos; staff: todos sin dirección salvo administrador",
            "S22": "cliente: solo si le pertenece (404 si no); lo verifica orders", "S23": "cliente dueño en PENDIENTE/PAGADO; administrador también EN_DESPACHO; lo verifica orders",
            "S25": "solo pagos propios; lo verifica payments", "S24": "solo pedidos propios y PENDIENTE_PAGO; monto lo toma payments desde orders",
            "S26": "firma HMAC + ventana 5 min + event_id único + monto igual al registrado; lo verifica payments",
            "S34": "identidad se reenvía a orders; campo `cliente` solo administrador/cajero", "S12": "solo administrador (el dueño edita pero no descontinúa)"}.get(i, "rol exigido; se verifica en gateway y de nuevo en el servicio destino")
    cell = lambda v: "permitido" if m == v else "—"
    perms.append(f"| {i} | `{p}` | {cell('GET')} | {cell('POST')} | {cell('PUT')} | {cell('DELETE')} | {acc} | {cond} |")

doc = open("docs/INFORME.template.md", encoding="utf-8").read()
doc = doc.replace("{{ROUTES}}", "\n".join(routes)).replace("{{PERMS}}", "\n".join(perms))
open("docs/INFORME.md", "w", encoding="utf-8").write(doc)
print("docs/INFORME.md generado,", len(doc), "caracteres")
