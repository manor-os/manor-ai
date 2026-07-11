"""
Sandbox Service — FastAPI application entry point.

Provides a REST API for creating sandboxed Docker containers to
execute skill scripts safely. Designed for integration with chat
applications that need to run LLM-directed code in isolation.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.routes import router, set_runner
from config import config
from sandbox.skill_runner import SkillRunner

logging.basicConfig(
    level=logging.DEBUG if config.DEBUG else logging.INFO,
    format="%(asctime)s %(levelname)-8s [%(name)s] %(message)s",
)
logger = logging.getLogger("sandbox-service")

runner = SkillRunner()


@asynccontextmanager
async def lifespan(app: FastAPI):
    set_runner(runner)
    await runner.startup()
    logger.info("Sandbox Service started on %s:%s", config.HOST, config.PORT)
    yield
    await runner.shutdown()
    logger.info("Sandbox Service stopped")


app = FastAPI(
    title="Sandbox Service",
    description=(
        "A sandboxed execution service for AI skill scripts. "
        "Creates isolated Docker containers, injects skill files and dependencies, "
        "and provides exec/file APIs for LLM-directed execution."
    ),
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router)


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "active_sandboxes": len(runner.list_sandboxes()),
        **runner.runtime_status(),
    }


if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host=config.HOST,
        port=config.PORT,
        reload=config.DEBUG,
    )
