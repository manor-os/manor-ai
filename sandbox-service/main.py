"""
Sandbox Service — FastAPI application entry point.

Provides a REST API for creating sandboxed Docker containers to
execute skill scripts safely. Designed for integration with chat
applications that need to run LLM-directed code in isolation.
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from api.errors import error_body, install_error_handlers
from api.routes import router, set_runner
from sandbox_config import config
from sandbox.drain import SandboxServiceDraining
from sandbox.skill_runner import SkillRunner

BUILD_VERSION = os.getenv("MANOR_BUILD_VERSION", "dev")

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
    version="0.1.1",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def require_sandbox_api_token(request: Request, call_next):
    token = config.API_TOKEN.strip()
    if request.url.path.startswith("/api/v1/"):
        if not token:
            return JSONResponse(
                error_body("Sandbox API token is not configured", "token_not_configured"),
                status_code=503,
            )
        supplied = request.headers.get("X-Manor-Sandbox-Token", "")
        if not secrets.compare_digest(supplied, token):
            return JSONResponse(
                error_body("Sandbox API token required", "unauthorized"),
                status_code=401,
            )
        # Count API work so a restart drains it instead of cutting it off.
        with runner.track_request():
            return await call_next(request)
    return await call_next(request)

install_error_handlers(app)
app.include_router(router)

# Seconds a draining service asks a refused create to wait before retrying.
_DRAINING_RETRY_AFTER_SECONDS = 15


@app.exception_handler(SandboxServiceDraining)
async def sandbox_service_draining(_request: Request, exc: SandboxServiceDraining):
    return JSONResponse(
        error_body(str(exc), "sandbox_service_draining"),
        status_code=503,
        headers={"Retry-After": str(_DRAINING_RETRY_AFTER_SECONDS)},
    )


@app.get("/health")
async def health():
    """Liveness: answered from memory only.

    It must never wait on Docker, the filesystem or a lock — a liveness probe
    that queues behind a slow Docker CLI under host load gets a busy but
    healthy service killed, destroying every sandbox it owns. Image state is
    a cache that startup fills and a background loop refreshes off the event
    loop (see ``SkillRunner.refresh_image_status``).
    """
    # With a cached answer (always, once startup ran) this only schedules a
    # refresh when stale and returns at once; only a process that never
    # started waits for one bounded off-loop probe.
    await runner.refresh_image_status()
    return {
        "status": "ok",
        "build_version": BUILD_VERSION,
        # Skill env is handed to each docker exec and replaced by load_skill;
        # the core only reuses a container for a credentialed skill when the
        # runner reports this mode.
        "env_injection": "exec",
        # A synchronous exec accepts its own env (layered over the skill env
        # for that command only); the core hands the in-sandbox manor CLI its
        # short-lived token this way.
        "exec_env": True,
        # ... a started (possibly queued) exec does too, for that command only;
        # and the core's own bookkeeping scripts run on the side channel,
        # next to the command slots, never queued behind them.
        "exec_start_env": True,
        "exec_side_channel": True,
        # Capabilities the control plane once inferred from older reports
        # (packages/core/services/sandbox_sdk/capabilities.py): a started
        # command (/exec/start, /executions), per-exec Skill env, /files/tail,
        # exec timeouts up to 3600 s and the /workspace/c work areas. Every
        # new runner behaviour adds its flag here and its gate there
        # (tests/test_sandbox_runner_compat.py holds them together).
        "exec_start": True,
        "env_per_exec": True,
        "files_tail": True,
        "long_exec_timeout": True,
        "work_areas": True,
        **runner.runtime_status(),
    }


@app.get("/ready")
async def ready():
    """Readiness: started and the cached Docker probe can reach the daemon.

    Stays ready while draining so callers can finish with their sandboxes.
    """
    ok, detail = runner.readiness()
    return JSONResponse(
        {
            "status": "ready" if ok else "not_ready",
            "detail": detail,
            "draining": runner.draining,
        },
        status_code=200 if ok else 503,
    )


# After a drain, uvicorn waits this long for straggling requests before
# cancelling them and destroying the remaining sandboxes.
_POST_DRAIN_GRACEFUL_SHUTDOWN_SECONDS = 15


class DrainingServer(uvicorn.Server):
    """uvicorn server that drains the SkillRunner before it stops listening.

    uvicorn closes its listening socket as soon as SIGTERM arrives, so a caller
    that just finished an exec could no longer read the output, and lifespan
    shutdown then destroys every sandbox. Here the first SIGTERM/SIGINT only
    starts a drain (new sandboxes are refused, existing ones keep working);
    the normal uvicorn shutdown begins once in-flight work settles or the
    drain timeout passes. A second signal stops immediately.
    """

    def __init__(
        self,
        config: uvicorn.Config,
        *,
        skill_runner: SkillRunner,
        drain_timeout: float,
        quiet_seconds: float,
    ) -> None:
        super().__init__(config)
        self._skill_runner = skill_runner
        self._drain_timeout = drain_timeout
        self._quiet_seconds = quiet_seconds
        self._loop: asyncio.AbstractEventLoop | None = None
        self._drain_requested = False
        self._drain_task: asyncio.Task | None = None

    async def serve(self, sockets=None) -> None:
        self._loop = asyncio.get_running_loop()
        await super().serve(sockets=sockets)

    def handle_exit(self, sig, frame) -> None:
        if (
            self._drain_requested
            or self.should_exit
            or self._loop is None
            or self._drain_timeout <= 0
        ):
            super().handle_exit(sig, frame)
            return
        self._drain_requested = True
        logger.info("Received signal %s; draining before shutdown", sig)
        self._loop.call_soon_threadsafe(self._start_drain, sig)

    def _start_drain(self, sig) -> None:
        self._drain_task = asyncio.ensure_future(self._drain_then_exit(sig))

    async def _drain_then_exit(self, sig) -> None:
        try:
            await self._skill_runner.drain(
                timeout=self._drain_timeout,
                quiet_seconds=self._quiet_seconds,
            )
        except Exception:
            logger.exception("Sandbox drain failed; shutting down")
        finally:
            super().handle_exit(sig, None)


def serve() -> None:
    if config.DEBUG:
        # Auto-reload needs an import string; development has no work to drain.
        uvicorn.run("main:app", host=config.HOST, port=config.PORT, reload=True)
        return
    server = DrainingServer(
        uvicorn.Config(
            app,
            host=config.HOST,
            port=config.PORT,
            timeout_graceful_shutdown=_POST_DRAIN_GRACEFUL_SHUTDOWN_SECONDS,
        ),
        skill_runner=runner,
        drain_timeout=config.DRAIN_TIMEOUT_SECONDS,
        quiet_seconds=config.DRAIN_QUIET_SECONDS,
    )
    server.run()


if __name__ == "__main__":
    serve()
