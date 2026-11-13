#!/bin/sh
# Carga secrets por servicio en Vault y crea UNA política y UN token por servicio (lectura de su ruta solamente).
# Vault en modo dev es solo para el laboratorio: en producción usar init/unseal, AppRole y auditoría.
set -e
export VAULT_ADDR=http://vault:8200
until vault status >/dev/null 2>&1; do sleep 1; done
vault login -no-print "$VAULT_DEV_ROOT_TOKEN_ID"
for f in /seed/*.json; do
  svc=$(basename "$f" .json)
  vault kv put -mount=secret "cb/$svc" @"$f" >/dev/null
  printf 'path "secret/data/cb/%s" { capabilities = ["read"] }\n' "$svc" | vault policy write "cb-$svc" - >/dev/null
  vault token create -policy="cb-$svc" -period=24h -field=token > "/tokens/$svc"
  chmod 644 "/tokens/$svc"
done
echo "Vault inicializado"
