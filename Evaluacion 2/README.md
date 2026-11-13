# Craft & Beer — backend seguro (Evaluación Unidad 2)

Microservicios FastAPI detrás de un API Gateway, con Vault para secretos. Informe completo: `docs/INFORME.md`.

```
Navegador ─► API Gateway :8000 ─► auth · clients · inventory · orders · payments · notifications · graphql
Proveedor de pagos (sandbox) :8010 ──webhook firmado──► Gateway
```

## Arranque rápido (sin Docker)
```bash
pip install -r requirements.txt
scripts/run_local.sh --fresh      # genera secretos aleatorios en .secrets/ y levanta todo
# abrir http://127.0.0.1:8000
pytest -v tests                   # con el sistema arriba (40 pruebas)
scripts/stop_local.sh
```
Cuentas del personal: `admin`, `cajero`, `despacho`, `dueno` — contraseñas en `.secrets/seed.env` (se crea localmente, no se versiona).
Clientes: se registran en la pantalla de ingreso (RUN válido, mayor de 18, correo con dominio real: `@noexiste.cl`, `@mailinator.com`, `@invalid` se rechazan en sandbox).

## Con Docker (NO probado)
```bash
scripts/prepare_compose.sh && docker compose up --build
```
Redes: `public` (gateway, proveedor), `private` (internal, sin puertos), `egress` (payments↔proveedor). Un token de Vault por servicio con acceso solo a su ruta.

## Seguridad en una frase por capa
JWT de 15 min + revocación · rol verificado en gateway **y** en cada servicio · identidad firmada de 30 s hacia dentro · clave distinta por par de servicios · RUN/dirección/teléfono cifrados · contraseñas Argon2id · precios, stock y pagos decididos solo por el servidor · pagos confirmados solo por webhook HMAC idempotente.

## Estructura
`cb_common/` (secretos, seguridad, app) · `services/*` · `gateway/` · `frontend/index.html` · `scripts/` · `tests/` · `docs/`

## Antes de producción
Vault en modo dev → init/unseal y AppRole · TLS/mTLS · Oracle/procedimientos almacenados si el docente los exige · prueba de carga · pasar el rate limit y la revocación a un almacén compartido (hoy en memoria / en BD de un solo gateway).
