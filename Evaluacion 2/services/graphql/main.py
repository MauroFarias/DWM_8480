"""GraphQL Gateway PRIVADO. Agrega orders + inventory + clients sin acceder a sus bases de datos.

- La identidad verificada llega desde el API Gateway (X-Identity) y se REENVÍA a orders, que sigue
  siendo la autoridad de propiedad: un cliente nunca obtiene pedidos ajenos aunque cambie el id.
- Autorización por campo: 'cliente' solo para administrador/cajero_virtual.
- Límites: profundidad máxima 5, paginación máx. 50, plazo 3 s por llamada, batching de productos (DataLoader).
- Errores parciales: si inventory no responde, los campos de producto vienen null + error, el pedido sigue.
"""
import os
from typing import List, Optional

import httpx
import strawberry
from fastapi import Depends, Request
from strawberry.dataloader import DataLoader
from strawberry.extensions import QueryDepthLimiter
from strawberry.fastapi import GraphQLRouter
from strawberry.types import Info

from cb_common.app import make_app
from cb_common.secrets_provider import SecretProvider
from cb_common.security import make_guard, only_from, Caller, InternalClient

SERVICE = "graphql"
sp = SecretProvider(SERVICE)
guard = make_guard(sp)
app = make_app(SERVICE, "GraphQL Gateway")
orders_api = InternalClient(sp, SERVICE, "orders", os.getenv("ORDERS_URL", "http://127.0.0.1:8004"))
inv_api = InternalClient(sp, SERVICE, "inventory", os.getenv("INVENTORY_URL", "http://127.0.0.1:8003"))
cli_api = InternalClient(sp, SERVICE, "clients", os.getenv("CLIENTS_URL", "http://127.0.0.1:8002"))
STAFF_CLIENT_FIELD = {"administrador", "cajero_virtual"}


async def load_products(keys: List[str]):
    async with httpx.AsyncClient(timeout=3.0) as c:
        r = await c.post(f"{inv_api.base}/internal/products/lookup", json={"ids": list(set(keys))},
                         headers=inv_api.headers(None, None))
        r.raise_for_status()
        data = r.json()
    return [data.get(k) for k in keys]


@strawberry.type
class ClienteResumen:
    nombre: str
    comuna: str


@strawberry.type
class ItemPedido:
    producto_id: str
    nombre: str
    formato: str
    cantidad: int
    precio_unit: int
    subtotal: int

    @strawberry.field
    async def estilo(self, info: Info) -> Optional[str]:
        p = await info.context["products"].load(self.producto_id)
        return p["estilo"] if p else None

    @strawberry.field
    async def abv(self, info: Info) -> Optional[float]:
        p = await info.context["products"].load(self.producto_id)
        return p["abv"] if p else None

    @strawberry.field
    async def disponible_ahora(self, info: Info) -> Optional[bool]:
        p = await info.context["products"].load(self.producto_id)
        return p["disponible"] if p else None


@strawberry.type
class PedidoResumen:
    id: int
    estado: str
    total: int
    boleta: Optional[str]
    fecha: str
    plataforma_pago: Optional[str]
    items: List[ItemPedido]
    cliente_id: strawberry.Private[Optional[str]] = None

    @strawberry.field
    async def cliente(self, info: Info) -> Optional[ClienteResumen]:
        ident = info.context["caller"].identity
        if ident.role not in STAFF_CLIENT_FIELD or not self.cliente_id:
            raise PermissionError("Campo 'cliente' no autorizado para tu rol")
        async with httpx.AsyncClient(timeout=3.0) as c:
            r = await c.get(f"{cli_api.base}/internal/clients/{self.cliente_id}", headers=cli_api.headers(None, None))
            r.raise_for_status()
        d = r.json()
        return ClienteResumen(nombre=d["nombre"], comuna=d["comuna"])


def to_type(d: dict) -> PedidoResumen:
    return PedidoResumen(id=d["id"], estado=d["estado"], total=d["total"], boleta=d.get("boleta"), fecha=d["fecha"],
                         plataforma_pago=d.get("plataforma_pago"), cliente_id=d.get("cliente_id"),
                         items=[ItemPedido(producto_id=i["producto_id"], nombre=i["nombre"], formato=i["formato"],
                                           cantidad=i["cantidad"], precio_unit=i["precio_unit"], subtotal=i["subtotal"])
                                for i in d["items"]])


async def call_orders(info: Info, path: str, params=None):
    ctx = info.context
    async with httpx.AsyncClient(timeout=3.0) as c:
        r = await c.get(f"{orders_api.base}{path}", params=params, headers=orders_api.headers(ctx["identity_token"], ctx["corr"]))
    if r.status_code == 404:
        raise ValueError("Pedido no encontrado")
    if r.status_code in (401, 403):
        raise PermissionError("No autorizado")
    r.raise_for_status()
    return r.json()


@strawberry.type
class Query:
    @strawberry.field
    async def resumen_pedido(self, info: Info, id: int) -> PedidoResumen:
        return to_type(await call_orders(info, f"/orders/{id}"))

    @strawberry.field
    async def mis_pedidos(self, info: Info, limit: int = 10, offset: int = 0) -> List[PedidoResumen]:
        limit = max(1, min(limit, 50))
        offset = max(0, offset)
        return [to_type(d) for d in await call_orders(info, "/orders", {"limit": limit, "offset": offset})]


schema = strawberry.Schema(query=Query, extensions=[lambda: QueryDepthLimiter(max_depth=5)])


async def get_context(request: Request, caller: Caller = Depends(guard)):
    only_from(caller, "gateway")
    if caller.identity is None:
        from fastapi import HTTPException
        raise HTTPException(401, "Autenticación requerida")
    return {"caller": caller, "identity_token": request.headers.get("x-identity"),
            "corr": request.headers.get("x-correlation-id"), "products": DataLoader(load_fn=load_products)}


app.include_router(GraphQLRouter(schema, context_getter=get_context, graphql_ide=None), prefix="/graphql")
