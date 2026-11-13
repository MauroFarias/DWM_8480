#!/bin/sh
# SVC=nombre MODULE=paquete.modulo:app PORT=puerto
i=0; while [ ! -s "/tokens/$SVC" ] && [ $i -lt 60 ]; do i=$((i+1)); sleep 1; done
[ -s "/tokens/$SVC" ] || { echo "sin token de Vault para $SVC"; exit 1; }
export VAULT_TOKEN="$(cat /tokens/$SVC)"
exec python -m uvicorn "$MODULE" --host 0.0.0.0 --port "$PORT" --log-level warning
