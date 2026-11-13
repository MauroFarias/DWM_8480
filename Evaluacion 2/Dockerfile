FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt "psycopg[binary]==3.2.3"
COPY cb_common ./cb_common
COPY services ./services
COPY gateway ./gateway
COPY frontend ./frontend
RUN useradd -r -u 10001 app && mkdir -p /app/.data && chown -R app /app/.data
USER app
ENV PYTHONPATH=/app PYTHONUNBUFFERED=1
# Cada servicio lee su token de Vault (solo su ruta) desde /tokens/<SVC> y arranca uvicorn
COPY scripts/entrypoint.sh /entrypoint.sh
ENTRYPOINT ["sh", "/entrypoint.sh"]
