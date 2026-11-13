#!/usr/bin/env bash
# Arranca todo el sistema como procesos locales (sin Docker). Uso: scripts/run_local.sh [--fresh]
set -e
cd "$(dirname "$0")/.."
[ "$1" = "--fresh" ] && rm -rf .data
mkdir -p .data .logs
[ -f .secrets/secrets.local.json ] || python3 scripts/gen_secrets.py
export CB_SECRETS_FILE="$PWD/.secrets/secrets.local.json"
export PYTHONPATH="$PWD"
# Contraseñas de demostración del personal: se leen de .secrets/seed.env (no versionado)
if [ ! -f .secrets/seed.env ]; then
  cat > .secrets/seed.env <<ENV
SEED_PASSWORD_ADMIN=Admin#Demo2026
SEED_PASSWORD_CAJERO=Cajero#Demo2026
SEED_PASSWORD_DESPACHO=Despacho#Demo2026
SEED_PASSWORD_DUENO=Dueno#Demo2026
ENV
fi
set -a; . .secrets/seed.env; set +a
export FRONTEND_DIR="$PWD/frontend" RL_AUTH_PER_MIN="${RL_AUTH_PER_MIN:-40}"
start() { # nombre módulo puerto
  nohup python3 -m uvicorn "$2" --host 127.0.0.1 --port "$3" --log-level warning > ".logs/$1.log" 2>&1 &
  echo $! > ".logs/$1.pid"
}
start auth services.auth.main:app 8001
start clients services.clients.main:app 8002
start inventory services.inventory.main:app 8003
start orders services.orders.main:app 8004
start payments services.payments.main:app 8005
start notifications services.notifications.main:app 8006
start graphql services.graphql.main:app 8007
start provider services.provider_sandbox.main:app 8010
start gateway gateway.main:app 8000
for p in 8001 8002 8003 8004 8005 8006 8007 8010 8000; do
  for i in $(seq 1 40); do
    curl -s -o /dev/null "http://127.0.0.1:$p/health" && break; sleep 0.5
  done
done
echo "Sistema arriba. Gateway: http://127.0.0.1:8000  (logs en .logs/)"
