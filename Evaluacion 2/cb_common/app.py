import logging
import time
import uuid
from contextvars import ContextVar

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

corr_id: ContextVar[str] = ContextVar("corr_id", default="-")


class _CorrFilter(logging.Filter):
    def filter(self, record):
        record.corr = corr_id.get()
        return True


def make_logger(name: str) -> logging.Logger:
    log = logging.getLogger(name)
    if not log.handlers:
        h = logging.StreamHandler()
        h.setFormatter(logging.Formatter("%(asctime)s %(name)s corr=%(corr)s %(message)s"))
        h.addFilter(_CorrFilter())
        log.addHandler(h)
        log.setLevel(logging.INFO)
    return log


def make_app(name: str, title: str) -> FastAPI:
    app = FastAPI(title=title, docs_url=None, redoc_url=None)  # sin Swagger público en servicios privados
    log = make_logger(name)

    @app.middleware("http")
    async def correlation(request: Request, call_next):
        cid = request.headers.get("x-correlation-id") or uuid.uuid4().hex
        corr_id.set(cid)
        t0 = time.time()
        try:
            resp = await call_next(request)
        except Exception:
            log.exception("error no controlado %s %s", request.method, request.url.path)
            resp = JSONResponse({"detail": "Error interno"}, status_code=500)
        resp.headers["X-Correlation-Id"] = cid
        log.info("%s %s -> %s %.0fms", request.method, request.url.path, resp.status_code, (time.time() - t0) * 1000)
        return resp

    @app.get("/health")
    def health():
        return {"status": "OK", "service": name}

    return app


def make_db(url: str):
    kw = {"connect_args": {"check_same_thread": False}} if url.startswith("sqlite") else {}
    engine = create_engine(url, pool_pre_ping=True, **kw)
    return engine, sessionmaker(bind=engine, expire_on_commit=False)
