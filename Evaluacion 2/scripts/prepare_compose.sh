#!/usr/bin/env bash
# Genera secretos de Vault por servicio y un .env con contraseñas de BD aleatorias. Ninguno se versiona.
set -e
cd "$(dirname "$0")/.."
python3 scripts/gen_secrets.py
rm -rf .secrets/vault/provider_sandbox.json.bak
if [ ! -f .env ]; then
python3 - <<'PY'
import secrets
names = ["AUTH", "CLIENTS", "INVENTORY", "ORDERS", "PAYMENTS", "NOTIFICATIONS"]
lines = ["POSTGRES_PASSWORD=" + secrets.token_urlsafe(24), "VAULT_DEV_ROOT_TOKEN_ID=" + secrets.token_urlsafe(24)]
lines += [f"DB_PASS_{n}=" + secrets.token_urlsafe(24) for n in names]
lines += ["SEED_PASSWORD_ADMIN=Admin#Demo2026", "SEED_PASSWORD_CAJERO=Cajero#Demo2026",
          "SEED_PASSWORD_DESPACHO=Despacho#Demo2026", "SEED_PASSWORD_DUENO=Dueno#Demo2026"]
open(".env", "w").write("\n".join(lines) + "\n")
PY
chmod 600 .env
fi
echo "Listo. Ahora: docker compose up --build"
