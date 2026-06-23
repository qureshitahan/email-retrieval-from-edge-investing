from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import get_settings
from app.database import init_db
from app.routers import ai, auth, contacts, export, messages, outreach, sync

settings = get_settings()
app = FastAPI(title="Relationship Intelligence CRM", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def on_startup() -> None:
    init_db()


@app.get("/api/v1/health")
def health():
    return {"status": "ok"}


app.include_router(auth.router, prefix="/api/v1")
app.include_router(messages.router, prefix="/api/v1")
app.include_router(ai.router, prefix="/api/v1")
app.include_router(sync.router, prefix="/api/v1")
app.include_router(contacts.router, prefix="/api/v1")
app.include_router(outreach.router, prefix="/api/v1")
app.include_router(export.router, prefix="/api/v1")
