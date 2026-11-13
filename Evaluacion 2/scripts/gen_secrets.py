"""Genera secretos de DESARROLLO con valores aleatorios y mínimo alcance por servicio.
Salida: .secrets/secrets.local.json (ignorado por git). En Vault se cargan igual por servicio
(secret/cb/<servicio>) con una política por servicio. Nunca se versionan valores reales.
"""
import json, os, secrets, sys
from cryptography.fernet import Fernet

# (llamador -> destino): clave por par
PAIRS = [("gateway", "auth"), ("gateway", "clients"), ("gateway", "inventory"), ("gateway", "orders"),
         ("gateway", "payments"), ("gateway", "graphql"), ("gateway", "notifications"),
         ("auth", "clients"), ("auth", "notifications"),
         ("orders", "inventory"), ("orders", "clients"), ("orders", "notifications"),
         ("payments", "orders"), ("graphql", "orders"), ("graphql", "inventory"), ("graphql", "clients")]

def r(): return secrets.token_urlsafe(32)

out = {s: {} for s in ["gateway", "auth", "clients", "inventory", "orders", "payments", "notifications", "graphql", "provider_sandbox"]}
for caller, callee in PAIRS:
    k = r()
    out[caller][f"to-{callee}"] = k
    out[callee][f"from-{caller}"] = k
ident = r()
for s in ["gateway", "auth", "clients", "inventory", "orders", "payments", "graphql", "notifications"]:
    out[s]["identity-key"] = ident
jwtk = r()
out["gateway"]["jwt-signing-key"] = jwtk
out["auth"]["jwt-signing-key"] = jwtk
out["clients"]["data-enc-key"] = Fernet.generate_key().decode()
out["orders"]["data-enc-key"] = Fernet.generate_key().decode()
wh = r()
out["payments"]["webhook-secret"] = wh
out["provider_sandbox"]["webhook-secret"] = wh
pk = r()
out["payments"]["provider-api-key"] = pk
out["provider_sandbox"]["provider-api-key"] = pk

dest = sys.argv[1] if len(sys.argv) > 1 else ".secrets/secrets.local.json"
os.makedirs(os.path.dirname(dest), exist_ok=True)
with open(dest, "w", encoding="utf-8") as f:
    json.dump(out, f, indent=2)
os.chmod(dest, 0o600)
print("secretos de desarrollo escritos en", dest)

vdir = os.path.join(os.path.dirname(dest) or ".", "vault")
os.makedirs(vdir, exist_ok=True)
for svc, vals in out.items():
    fp = os.path.join(vdir, f"{svc}.json")
    with open(fp, "w", encoding="utf-8") as f:
        json.dump(vals, f)
    os.chmod(fp, 0o600)
print("archivos por servicio para Vault en", vdir)
