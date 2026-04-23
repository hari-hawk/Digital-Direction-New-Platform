import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from backend.settings import settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: detect extractions stuck from a previous server crash
    from backend.api.uploads import detect_stuck_extractions
    detect_stuck_extractions()
    logger.info("Startup complete — stuck extraction check done")
    yield


app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


from backend.api.uploads import router as uploads_router
from backend.api.review import router as review_router
from backend.api.exports import router as exports_router
from backend.api.carriers import router as carriers_router
from backend.api.dashboard import router as dashboard_router
from backend.api.analytics import router as analytics_router

app.include_router(uploads_router)
app.include_router(review_router)
app.include_router(exports_router)
app.include_router(carriers_router)
app.include_router(dashboard_router)
app.include_router(analytics_router)


@app.get("/health")
async def health():
    return {"status": "ok", "app": settings.app_name}


@app.get("/api/spend")
async def spend():
    from backend.services.spend_ledger import current_total
    total = current_total()
    cap = settings.max_spend_usd
    return {
        "total_usd": round(total, 4),
        "cap_usd": cap,
        "remaining_usd": round(max(cap - total, 0.0), 4) if cap > 0 else None,
        "pct_used": round((total / cap * 100) if cap > 0 else 0.0, 2),
        "warn_at_pct": int(settings.spend_warn_pct * 100),
    }
