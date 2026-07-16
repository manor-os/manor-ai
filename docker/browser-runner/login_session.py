"""Headed-login session — interactive browser exposed to the user via VNC.

Multi-tenant design
───────────────────
Each session gets its own X11 stack so concurrent tenants never share
pixel buffers or input streams. On ``POST /login_session`` we:

  1. Allocate a free display number from ``_DisplayPool`` (e.g. :100)
  2. Spawn ``Xvfb :100`` (the virtual screen)
  3. Spawn ``fluxbox`` against ``DISPLAY=:100`` (window manager)
  4. Spawn ``x11vnc`` exporting :100 over TCP ``5900 + N``
  5. Spawn ``websockify`` bridging that VNC port → WebSocket on
     ``6080 + N``
  6. Launch Playwright Chromium with ``DISPLAY=:100`` and the
     headed flag so it renders into Xvfb
  7. Return the session id; the api proxies the WS through to
     ``ws://browser-runner:5200/login_session/{sid}/stream`` which
     internally reaches the per-session websockify port

On close (capture / cancel / GC), each session terminates its own
4-process quartet and releases the display number back to the pool.
The pool default is 50 concurrent slots (displays :100 .. :149); raise
``BROWSER_RUNNER_DISPLAY_CAPACITY`` to grow it.

Replaces the previous CDP-screencast flow (which sent JPEG frames over
a custom WebSocket and dispatched mouse/keyboard via
``Input.dispatchMouseEvent`` / ``Input.dispatchKeyEvent``). That flow
had two structural problems:

  * CDP only emits ``Page.screencastFrame`` when the page repaints.
    Pages with intermittent visual changes (typing into an input,
    dropdowns, animated chrome) skipped frames; users reported "the
    page doesn't update."
  * Keyboard input was silently dropped unless the canvas had focus,
    which broke as soon as the user dragged or selected text.

API surface (unchanged from the api's perspective):

  POST /login_session                 → start, returns { session_id, viewport }
  WS   /login_session/{sid}/stream    → bidirectional VNC frames
                                        (proxied to per-session websockify)
  POST /login_session/{sid}/capture   → snapshot storage_state, close browser
  POST /login_session/{sid}/cancel    → close without capturing
  GET  /login_session/{sid}/state     → debug — current page URL + title

Sessions are in-process and ephemeral. They're GC'd after 10 minutes
of inactivity (the user closing their tab leaves an orphan otherwise).
"""
from __future__ import annotations

import asyncio
import logging
import os
import secrets
import shutil
import signal
import time
from typing import Any, Dict, List, Optional

import websockets
from fastapi import APIRouter, Header, HTTPException, WebSocket, WebSocketDisconnect
from playwright.async_api import async_playwright
from pydantic import BaseModel

logger = logging.getLogger("browser-runner.login_session")

router = APIRouter(prefix="/login_session", tags=["login_session"])


_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)

_SESSION_IDLE_SEC = 600  # GC after 10 min inactivity
_GC_INTERVAL_SEC = 60

# Display pool: which X display numbers are allocatable to sessions.
# Each display N is paired with VNC port (5900+N) and WS port (6080+N).
# Default 50 slots is plenty for typical tenant overlap; tune if you
# see HTTP 503 from the start endpoint.
_DISPLAY_BASE = int(os.environ.get("BROWSER_RUNNER_DISPLAY_BASE", "100"))
_DISPLAY_CAPACITY = int(os.environ.get("BROWSER_RUNNER_DISPLAY_CAPACITY", "50"))

# Internal websockify host (always localhost — sessions spawn their
# own websockify processes bound to 127.0.0.1 + the per-session port).
_VNC_WS_HOST = os.environ.get("BROWSER_RUNNER_VNC_HOST", "localhost")
_VNC_WS_PATH = os.environ.get("BROWSER_RUNNER_VNC_PATH", "websockify")


# ── Display pool ───────────────────────────────────────────────────────────

class _DisplayPool:
    """Allocates X display numbers (and the matching VNC + WS ports)
    from a fixed range. Thread-safe via an asyncio lock."""

    def __init__(self, base: int, capacity: int) -> None:
        self._base = base
        self._capacity = capacity
        self._in_use: set[int] = set()
        self._lock = asyncio.Lock()

    @property
    def capacity(self) -> int:
        return self._capacity

    @property
    def in_use_count(self) -> int:
        return len(self._in_use)

    async def acquire(self) -> int:
        async with self._lock:
            for n in range(self._base, self._base + self._capacity):
                if n not in self._in_use:
                    self._in_use.add(n)
                    return n
        raise HTTPException(
            503,
            f"all {self._capacity} display slots in use; raise "
            "BROWSER_RUNNER_DISPLAY_CAPACITY or scale browser-runner replicas",
        )

    async def release(self, n: int) -> None:
        async with self._lock:
            self._in_use.discard(n)


_pool = _DisplayPool(_DISPLAY_BASE, _DISPLAY_CAPACITY)


def vnc_port_for(display: int) -> int:
    """VNC server bound on TCP for the given display number."""
    return 5900 + display


def ws_port_for(display: int) -> int:
    """websockify port bridging the VNC TCP socket → noVNC WS."""
    return 6080 + display


# ── X11 stack lifecycle ────────────────────────────────────────────────────

class _XStack:
    """The four child processes that back one display: Xvfb,
    fluxbox, x11vnc, websockify. Started in dependency order, torn
    down in reverse."""

    def __init__(
        self,
        display: int,
        vnc_port: int,
        ws_port: int,
        viewport: Dict[str, int],
    ) -> None:
        self.display = display
        self.vnc_port = vnc_port
        self.ws_port = ws_port
        self.viewport = viewport
        self.procs: List[asyncio.subprocess.Process] = []

    async def start(self) -> None:
        env = {**os.environ, "DISPLAY": f":{self.display}"}
        screen = f"{self.viewport['width']}x{self.viewport['height']}x24"

        # Belt-and-suspenders: clear any stale X lock + socket from a
        # previous SIGKILL. Without this, the new Xvfb refuses to start
        # ("server already active") and the display slot is permanently
        # poisoned.
        self._purge_stale_lock_files()

        # 1. Xvfb — virtual screen.
        xvfb = await asyncio.create_subprocess_exec(
            "Xvfb",
            f":{self.display}",
            "-screen", "0", screen,
            "-ac",
            "+extension", "RANDR",
            "+render",
            "-noreset",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        self.procs.append(xvfb)
        # Wait for the X lock file before launching anything that
        # talks to it — otherwise fluxbox / x11vnc race and may fail
        # to connect.
        await self._wait_for_x_lock()

        # 2. fluxbox — Chromium needs SOMETHING handling WM_PROTOCOLS
        # or it spams "no compositor" warnings and some popups misbehave.
        fluxbox = await asyncio.create_subprocess_exec(
            "fluxbox",
            env=env,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        self.procs.append(fluxbox)

        # 3. x11vnc — exports the X display over RFB on a localhost-only
        # TCP socket. -shared lets a noVNC client reconnect mid-flow.
        x11vnc = await asyncio.create_subprocess_exec(
            "x11vnc",
            "-display", f":{self.display}",
            "-rfbport", str(self.vnc_port),
            "-localhost",
            "-nopw",
            "-shared",
            "-forever",
            "-noxdamage",
            "-quiet",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        self.procs.append(x11vnc)

        # 4. websockify — bridges TCP RFB → WS for the noVNC client.
        # Bound to all interfaces so the FastAPI proxy on the same
        # container can reach it on localhost; not exposed externally
        # (the docker-compose network only publishes port 5200).
        websockify = await asyncio.create_subprocess_exec(
            "websockify",
            "--heartbeat", "30",
            f"0.0.0.0:{self.ws_port}",
            f"localhost:{self.vnc_port}",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        self.procs.append(websockify)

        # Tiny grace period so the very first VNC connect doesn't race
        # the x11vnc socket binding.
        await asyncio.sleep(0.4)

    async def _wait_for_x_lock(self, timeout: float = 5.0) -> None:
        lock_path = f"/tmp/.X{self.display}-lock"
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            if os.path.exists(lock_path):
                return
            await asyncio.sleep(0.1)

    async def stop(self) -> None:
        # Reverse-order termination — websockify first so noVNC clients
        # see a clean disconnect, then x11vnc (releases display lock),
        # then fluxbox, then Xvfb (the screen itself).
        for proc in reversed(self.procs):
            if proc.returncode is None:
                try:
                    proc.terminate()
                except ProcessLookupError:
                    continue
        # Give them a moment to exit gracefully.
        await asyncio.sleep(0.2)
        for proc in reversed(self.procs):
            if proc.returncode is None:
                try:
                    proc.kill()
                except ProcessLookupError:
                    continue
            try:
                await asyncio.wait_for(proc.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                pass
            except ProcessLookupError:
                pass
        self.procs.clear()
        # Final cleanup — even after SIGKILL, the X server may have
        # left its lock + socket behind. The next session that gets
        # this display number would otherwise fail to boot Xvfb.
        self._purge_stale_lock_files()

    def _purge_stale_lock_files(self) -> None:
        """Remove ``/tmp/.X{N}-lock`` and ``/tmp/.X11-unix/X{N}`` so a
        fresh Xvfb can claim the display. Best-effort — owned by
        the previous Xvfb process so deletion may fail in non-root
        containers; we log and move on."""
        for path in (
            f"/tmp/.X{self.display}-lock",
            f"/tmp/.X11-unix/X{self.display}",
        ):
            try:
                if os.path.exists(path):
                    os.remove(path)
            except OSError as exc:
                logger.debug(
                    "could not remove stale lock %s: %s — Xvfb may "
                    "refuse to bind display :%d on next acquire",
                    path, exc, self.display,
                )


def _x_binaries_present() -> bool:
    """Cheap probe so we can return a useful error if the Dockerfile
    refactor accidentally drops one of the X binaries."""
    return all(shutil.which(b) for b in ("Xvfb", "fluxbox", "x11vnc", "websockify"))


# ── Session ────────────────────────────────────────────────────────────────

class _Session:
    """One headed login attempt — owns its own Playwright context AND
    its own X11 stack. Tearing down the session terminates both."""

    def __init__(
        self,
        sid: str,
        pw,
        browser,
        context,
        page,
        viewport: Dict[str, int],
        provider: str,
        x_stack: _XStack,
    ) -> None:
        self.sid = sid
        self.pw = pw
        self.browser = browser
        self.context = context
        self.page = page
        self.viewport = viewport
        self.provider = provider
        self.x_stack = x_stack
        self.last_activity = time.monotonic()
        self.closed = False
        self.lock = asyncio.Lock()

    @property
    def display(self) -> int:
        return self.x_stack.display

    @property
    def ws_port(self) -> int:
        return self.x_stack.ws_port

    def touch(self) -> None:
        self.last_activity = time.monotonic()

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        # 1. Close Playwright (triggers Chromium shutdown).
        try:
            await self.browser.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            await self.pw.stop()
        except Exception:  # noqa: BLE001
            pass
        # 2. Tear down the X stack for this display.
        try:
            await self.x_stack.stop()
        except Exception:  # noqa: BLE001
            pass
        # 3. Release the display number.
        try:
            await _pool.release(self.x_stack.display)
        except Exception:  # noqa: BLE001
            pass


_sessions: Dict[str, _Session] = {}
_gc_task: Optional[asyncio.Task] = None


# ── Auth (shared with the rest of the runner) ──────────────────────────────

def _check_auth(authorization: Optional[str], runner_token: str) -> None:
    if not runner_token:
        return
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Missing Bearer token")
    if authorization.removeprefix("Bearer ").strip() != runner_token:
        raise HTTPException(403, "Bad token")


# ── Schemas ────────────────────────────────────────────────────────────────

class StartRequest(BaseModel):
    provider: str
    url: str
    viewport: Optional[Dict[str, int]] = None


class StartResponse(BaseModel):
    session_id: str
    viewport: Dict[str, int]


class CaptureResponse(BaseModel):
    storage_state: Dict[str, Any]
    final_url: str


class PoolStatusResponse(BaseModel):
    capacity: int
    in_use: int


# ── Endpoints ──────────────────────────────────────────────────────────────

def install(app, runner_token: str) -> None:
    """Bind the login_session router onto the main FastAPI app and
    schedule the GC task. Called from runner.py at startup."""

    @router.post("", response_model=StartResponse)
    async def start(
        req: StartRequest,
        authorization: Optional[str] = Header(default=None),
    ) -> StartResponse:
        _check_auth(authorization, runner_token)

        if not _x_binaries_present():
            raise HTTPException(
                500,
                "X11 stack missing — Dockerfile must install xvfb, "
                "fluxbox, x11vnc, websockify",
            )

        viewport = req.viewport or {"width": 1440, "height": 900}

        # Allocate a display from the pool. Each session gets its own
        # X server + VNC + WS ports — concurrent tenants never collide.
        display = await _pool.acquire()
        x_stack = _XStack(
            display=display,
            vnc_port=vnc_port_for(display),
            ws_port=ws_port_for(display),
            viewport=viewport,
        )

        try:
            await x_stack.start()
        except Exception as exc:
            await x_stack.stop()
            await _pool.release(display)
            raise HTTPException(500, f"failed to start X stack on :{display}: {exc}")

        try:
            pw = await async_playwright().start()
            browser = await pw.chromium.launch(
                headless=False,
                # Pin Chromium to THIS session's Xvfb display.
                env={**os.environ, "DISPLAY": f":{display}"},
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    f"--window-size={viewport['width']},{viewport['height']}",
                    "--window-position=0,0",
                    "--no-first-run",
                    "--no-default-browser-check",
                    "--disable-infobars",
                ],
            )
            try:
                context = await browser.new_context(
                    user_agent=_USER_AGENT,
                    viewport=viewport,
                    locale="en-US",
                    timezone_id="America/Los_Angeles",
                )
                page = await context.new_page()

                # Skip playwright-stealth in the headed login flow.
                # LinkedIn's signin page (and Xiaohongshu's creator SPA)
                # detect stealth patches reliably enough that the page
                # renders blank — we lose the entire login UX. The user
                # is physically interacting with this browser via VNC, so
                # stealth's value (hiding "this is automation" from
                # heuristic anti-bot) is minimal here. Keep stealth ON
                # in /perform (headless tool calls), where the value is
                # real and providers can opt out via USE_STEALTH=False.

                sid = secrets.token_urlsafe(16)
                sess = _Session(
                    sid=sid,
                    pw=pw,
                    browser=browser,
                    context=context,
                    page=page,
                    viewport=viewport,
                    provider=req.provider,
                    x_stack=x_stack,
                )
                _sessions[sid] = sess

                # Don't block on goto — let the page stream into the
                # user's view via VNC.
                asyncio.create_task(_safe_goto(page, req.url))
                _ensure_gc()

                logger.info(
                    "login_session start sid=%s provider=%s display=:%d "
                    "ws_port=%d url=%s",
                    sid, req.provider, display, x_stack.ws_port, req.url,
                )
                return StartResponse(session_id=sid, viewport=viewport)
            except Exception:
                await browser.close()
                await pw.stop()
                raise
        except Exception:
            # Playwright launch failed — tear down the X stack we
            # already started and release the display before reraising.
            await x_stack.stop()
            await _pool.release(display)
            raise

    @router.websocket("/{sid}/stream")
    async def stream(ws: WebSocket, sid: str) -> None:
        """Proxy bidirectional WebSocket frames between the api's WS
        client and THIS session's websockify port."""
        sess = _sessions.get(sid)
        if not sess or sess.closed:
            await ws.close(code=4404)
            return
        # Echo the upstream-requested WebSocket subprotocol back so
        # the api proxy (which negotiates 'binary' with noVNC) sees a
        # matching subprotocol from us. Without this echo the api's
        # `websockets.connect(subprotocols=['binary'])` to us either
        # gets None back or rejects the upstream entirely, breaking
        # the proxy chain.
        requested_subprotocols = ws.scope.get("subprotocols") or []
        chosen_subprotocol = "binary" if "binary" in requested_subprotocols else None
        await ws.accept(subprotocol=chosen_subprotocol)
        sess.touch()

        upstream_url = (
            f"ws://{_VNC_WS_HOST}:{sess.ws_port}/{_VNC_WS_PATH.lstrip('/')}"
        )

        try:
            async with websockets.connect(
                upstream_url,
                subprotocols=["binary"],
                max_size=16 * 1024 * 1024,
                ping_interval=None,
                ping_timeout=None,
                close_timeout=5,
                open_timeout=10,
            ) as upstream:
                await _bridge(ws, upstream, sess)
        except WebSocketDisconnect:
            pass
        except Exception as exc:  # noqa: BLE001
            logger.warning("VNC proxy ended sid=%s: %s", sid, exc)
            try:
                await ws.close(code=1011, reason=f"vnc proxy error: {exc}")
            except Exception:  # noqa: BLE001
                pass

    @router.post("/{sid}/capture", response_model=CaptureResponse)
    async def capture(
        sid: str,
        authorization: Optional[str] = Header(default=None),
    ) -> CaptureResponse:
        _check_auth(authorization, runner_token)
        sess = _sessions.get(sid)
        if not sess or sess.closed:
            raise HTTPException(404, "session not found")
        async with sess.lock:
            try:
                storage_state = await sess.context.storage_state()
                final_url = sess.page.url
            finally:
                await sess.close()
                _sessions.pop(sid, None)
        return CaptureResponse(storage_state=storage_state, final_url=final_url)

    @router.post("/{sid}/cancel")
    async def cancel(
        sid: str,
        authorization: Optional[str] = Header(default=None),
    ) -> Dict[str, bool]:
        _check_auth(authorization, runner_token)
        sess = _sessions.pop(sid, None)
        if sess:
            await sess.close()
        return {"ok": True}

    @router.get("/{sid}/state")
    async def state(
        sid: str,
        authorization: Optional[str] = Header(default=None),
    ) -> Dict[str, Any]:
        _check_auth(authorization, runner_token)
        sess = _sessions.get(sid)
        if not sess or sess.closed:
            raise HTTPException(404, "session not found")
        return {
            "url": sess.page.url,
            "title": await sess.page.title(),
            "provider": sess.provider,
            "display": sess.display,
        }

    @router.get("/_pool", response_model=PoolStatusResponse)
    async def pool_status(
        authorization: Optional[str] = Header(default=None),
    ) -> PoolStatusResponse:
        """Operator visibility — how saturated is the display pool?
        Useful for compose autoscaling alerts."""
        _check_auth(authorization, runner_token)
        return PoolStatusResponse(
            capacity=_pool.capacity,
            in_use=_pool.in_use_count,
        )

    app.include_router(router)


# ── Internals ──────────────────────────────────────────────────────────────

async def _safe_goto(page, url: str) -> None:
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
    except Exception as exc:  # noqa: BLE001
        logger.warning("login_session goto failed url=%s: %s", url, exc)


async def _bridge(client: WebSocket, upstream, sess: _Session) -> None:
    """Pump RFB frames upstream→client and mouse/keyboard upstream
    direction until either side disconnects.

    Both halves are now binary (RFB is a binary protocol; the noVNC
    client encodes its input events as RFB messages and the websockify
    bridge passes them through verbatim). Either side closing tears
    down the other half. Errors are swallowed so a transient drop
    can't take down the proxy.
    """
    stop = asyncio.Event()

    async def up_to_down() -> None:
        try:
            async for msg in upstream:
                if stop.is_set():
                    break
                sess.touch()
                if isinstance(msg, bytes):
                    await client.send_bytes(msg)
                else:
                    await client.send_bytes(
                        msg.encode("utf-8") if isinstance(msg, str) else bytes(msg)
                    )
        except (asyncio.CancelledError, WebSocketDisconnect):
            pass
        except Exception as exc:  # noqa: BLE001
            logger.debug("up_to_down ended: %s", exc)
        finally:
            stop.set()

    async def down_to_up() -> None:
        try:
            while not stop.is_set():
                msg = await client.receive()
                if msg["type"] == "websocket.disconnect":
                    return
                sess.touch()
                if msg.get("bytes") is not None:
                    await upstream.send(msg["bytes"])
                elif msg.get("text") is not None:
                    await upstream.send(msg["text"])
        except (asyncio.CancelledError, WebSocketDisconnect):
            pass
        except Exception as exc:  # noqa: BLE001
            logger.debug("down_to_up ended: %s", exc)
        finally:
            stop.set()

    up = asyncio.create_task(up_to_down())
    down = asyncio.create_task(down_to_up())
    await stop.wait()
    for t in (up, down):
        if not t.done():
            t.cancel()
    for t in (up, down):
        try:
            await t
        except BaseException:  # noqa: BLE001
            pass


def _ensure_gc() -> None:
    global _gc_task
    if _gc_task and not _gc_task.done():
        return
    _gc_task = asyncio.create_task(_gc_loop())


async def _gc_loop() -> None:
    while True:
        await asyncio.sleep(_GC_INTERVAL_SEC)
        now = time.monotonic()
        stale = [
            sid for sid, s in _sessions.items()
            if not s.closed and (now - s.last_activity) > _SESSION_IDLE_SEC
        ]
        for sid in stale:
            sess = _sessions.pop(sid, None)
            if sess:
                logger.info("gc'ing idle login_session sid=%s display=:%d",
                            sid, sess.display)
                await sess.close()
