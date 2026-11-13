"""Proveedor de pagos EXTERNO simulado (sandbox). Preserva el contrato de un proveedor real:
- payments crea una intención con API key (servidor a servidor).
- el cliente "paga" en la página del proveedor.
- el proveedor avisa por webhook firmado HMAC al gateway público.
No es un servicio propio del sistema: reemplaza a Servipag / banco en pruebas.
"""
import hashlib, hmac, json, os, time, uuid
import httpx
from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from cb_common.secrets_provider import SecretProvider

sp = SecretProvider("provider_sandbox")
app = FastAPI(title="Provider Sandbox", docs_url=None, redoc_url=None)
app.add_middleware(CORSMiddleware, allow_origins=os.getenv("FRONTEND_ORIGINS", "http://127.0.0.1:8000,http://localhost:8000").split(","),
                   allow_methods=["POST", "GET"], allow_headers=["Content-Type"])
GATEWAY = os.getenv("GATEWAY_URL", "http://127.0.0.1:8000")
INTENTS: dict = {}
LAST_EVENT: dict = {}


class IntentIn(BaseModel):
    payment_id: str = Field(max_length=40)
    amount: int = Field(gt=0)
    platform: str


class ConfirmIn(BaseModel):
    resultado: str = Field("approve", pattern="^(approve|reject)$")


def send(event: dict):
    raw = json.dumps(event, separators=(",", ":")).encode()
    ts = str(int(time.time()))
    sig = hmac.new(sp.get("webhook-secret").encode(), ts.encode() + b"." + raw, hashlib.sha256).hexdigest()
    r = httpx.post(f"{GATEWAY}/api/v1/webhooks/provider", content=raw, timeout=6,
                   headers={"Content-Type": "application/json", "X-Timestamp": ts, "X-Signature": sig})
    return r.status_code, r.json() if r.headers.get("content-type", "").startswith("application/json") else {}


@app.get("/health")
def health():
    return {"status": "OK", "service": "provider-sandbox"}


@app.post("/intents", status_code=201)
def intent(body: IntentIn, x_api_key: str = Header(default="")):
    if not hmac.compare_digest(x_api_key, sp.get("provider-api-key")):
        raise HTTPException(401, "API key inválida")
    INTENTS[body.payment_id] = body.model_dump()
    return {"payment_id": body.payment_id, "checkout": f"/checkout/{body.payment_id}"}


@app.post("/checkout/{payment_id}/confirm")
def confirm(payment_id: str, body: ConfirmIn):
    """El cliente aprueba o rechaza en la página del proveedor. El monto lo conoce el proveedor, no el navegador."""
    it = INTENTS.get(payment_id)
    if not it:
        raise HTTPException(404, "Intención de pago no encontrada")
    ev = {"event_id": uuid.uuid4().hex, "payment_id": payment_id,
          "status": "APPROVED" if body.resultado == "approve" else "REJECTED", "amount": it["amount"]}
    LAST_EVENT[payment_id] = ev
    code, data = send(ev)
    return {"resultado": body.resultado, "webhook_http": code, "respuesta": data}


@app.post("/sandbox/replay/{payment_id}")
def replay(payment_id: str):
    """Reenvía el último aviso tal cual (simula reintento del proveedor)."""
    ev = LAST_EVENT.get(payment_id)
    if not ev:
        raise HTTPException(404, "Sin eventos")
    code, data = send(ev)
    return {"webhook_http": code, "respuesta": data}
