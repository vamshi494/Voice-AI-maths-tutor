# app/api/server.py
"""FastAPI HTTP service for LiveKit tokens and board persistence.

- POST /token
- GET /boards
- GET /boards/{id}
"""
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes_boards import router as boards_router
from app.api.routes_token import router as token_router
from app.api.routes_ocr import router as ocr_router
from app.observability import setup_observability, instrument_fastapi_app
from app.persistence.db import init_db

setup_observability()


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    yield


app = FastAPI(
    title="AI Math Tutor API",
    version="1.0.0",
    lifespan=lifespan,
)
instrument_fastapi_app(app)

# CORS configuration for frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(token_router)
app.include_router(boards_router)
app.include_router(ocr_router)


@app.get("/health")
async def health_check() -> dict[str, str]:
    return {"status": "ok"}
