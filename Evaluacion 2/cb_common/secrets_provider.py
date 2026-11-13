"""SecretProvider: cada servicio lee SOLO su propia ruta de secretos.

Modo Vault  : VAULT_ADDR + VAULT_TOKEN  -> GET {VAULT_ADDR}/v1/secret/data/cb/<servicio>
Modo archivo: CB_SECRETS_FILE (JSON {servicio: {clave: valor}}), solo para desarrollo local.
Nunca se registran valores en logs.
"""
import json
import os
import time
import httpx

_TTL = 60


class SecretProvider:
    def __init__(self, service: str):
        self.service = service
        self._cache: dict = {}
        self._loaded_at = 0.0

    def _load(self) -> dict:
        addr, token = os.getenv("VAULT_ADDR"), os.getenv("VAULT_TOKEN")
        if addr and token:
            r = httpx.get(f"{addr}/v1/secret/data/cb/{self.service}",
                          headers={"X-Vault-Token": token}, timeout=3.0)
            r.raise_for_status()
            return r.json()["data"]["data"]
        path = os.getenv("CB_SECRETS_FILE")
        if not path:
            raise RuntimeError("Sin VAULT_ADDR/VAULT_TOKEN ni CB_SECRETS_FILE")
        with open(path, encoding="utf-8") as f:
            return json.load(f)[self.service]

    def get(self, key: str) -> str:
        now = time.time()
        if not self._cache or now - self._loaded_at > _TTL:
            try:
                self._cache = self._load()
                self._loaded_at = now
            except Exception:
                if not self._cache:  # sin copia previa: fallo cerrado
                    raise
                # Vault caído: se sigue con la copia en caché hasta que vuelva
        try:
            return self._cache[key]
        except KeyError:
            raise RuntimeError(f"Secreto '{key}' no definido para {self.service}")
