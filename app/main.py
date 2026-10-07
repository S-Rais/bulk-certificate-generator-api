import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy import text

from .config import Settings, get_settings
from .database import Base, make_engine, make_session_factory
from .routers import jobs
from .services.processor import JobRunner

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

DESCRIPTION = """
Submit a list of recipients in **one request**, get a job id back immediately (`202`), poll for
progress, then download individual PDFs or everything as a ZIP.

* Invalid rows are reported per-recipient and never block valid ones.
* Generation failures are isolated per certificate and can be retried.
* Jobs survive server restarts (state lives in the database).
"""


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    engine = make_engine(settings.database_url)
    session_factory = make_session_factory(engine)
    runner = JobRunner(session_factory, settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        Base.metadata.create_all(engine)
        settings.storage_dir.mkdir(parents=True, exist_ok=True)
        n = runner.recover()
        if n:
            logging.getLogger("certgen").info("re-queued %d unfinished job(s)", n)
        yield
        runner.shutdown()
        engine.dispose()

    app = FastAPI(title="Bulk Certificate Generator API", version="1.0.0",
                  description=DESCRIPTION, lifespan=lifespan)
    app.state.settings = settings
    app.state.session_factory = session_factory
    app.state.runner = runner
    app.include_router(jobs.router)

    @app.get("/health", tags=["ops"])
    def health():
        with session_factory() as db:
            db.execute(text("SELECT 1"))
        return {"status": "ok"}

    return app


app = create_app()
