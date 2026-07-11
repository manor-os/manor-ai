# Browser-runner sidecar — Playwright + Chromium + a tiny FastAPI
# server that exposes "perform action against this provider with these
# cookies" RPC for Manor's main api container to call.
#
# Two operating modes share the same container:
#
#   1. Headless `/perform` calls — short-lived, one Chromium per
#      request; never visible. Used by every browser-driven MCP tool
#      (notebooklm.ask, linkedin_browser.search_people, etc.).
#
#   2. Headed `/login_session` calls — long-lived, one Chromium per
#      session that the user signs into. Each session spawns its OWN
#      Xvfb + fluxbox + x11vnc + websockify quartet on a display
#      number allocated from a pool (default 50 slots). This is the
#      multi-tenant story — concurrent tenants signing into different
#      providers never share pixel buffers or input streams.
#
# Replaces the old CDP-screencast JPEG-over-WebSocket flow, which
# dropped frames on dynamic pages and forced users to "click to
# focus" before keyboard input would register.
#
# Why a separate container:
#   - Chromium adds ~600MB to an image; keeping it out of api/worker
#     keeps those images lean.
#   - Headless browser sessions can hang or leak processes; isolating
#     them means a OOM in browser-runner can't kill core Manor services.
#   - Lets us scale browser concurrency independently (one runner
#     instance ≈ 4-8 concurrent sessions; horizontal scale-out via
#     compose ``deploy.replicas``).
#
# The runner is stateless w.r.t. user data — every request supplies
# its own cookies. No filesystem state, no DB connection. Sessions
# live entirely in Manor's CredentialService (Vault); this container
# just executes.

# Bumped to noble (Ubuntu 24.04) for Python 3.12 — the previous
# jammy variant shipped Python 3.10 which can't satisfy
# browser-use's >=3.11 requirement (used by the L2 generic_browser
# provider). Playwright pip pin (below) is bumped to match the base
# image's bundled Chromium.
FROM mcr.microsoft.com/playwright/python:v1.55.0-noble

# ── X11 stack for headed (visible) sessions ──────────────────────────────
#
# Installs:
#   - xvfb        — virtual X server; the "screen" Chromium draws onto
#   - fluxbox     — minimal window manager (no decorations, just enough
#                   to make Chromium happy about WM_PROTOCOLS)
#   - x11vnc      — exports the Xvfb display over the VNC protocol
#   - websockify  — bridges raw VNC TCP (5900) to WebSocket (6080)
#   - dbus-x11    — Chromium pulls dbus-launch in some startup paths;
#                   without it you get "Failed to connect to bus" spam
#   - tini        — reaps zombie X processes (Xvfb, fluxbox, x11vnc,
#                   websockify) when entrypoint.sh exits
USER root
# Non-interactive tells debconf NOT to prompt. tzdata is pulled in by
# the X11 stack and can otherwise ask the user to pick a timezone,
# which hangs CI/deploy builds forever. TZ pins UTC and the RUN below
# also pre-seeds /etc/timezone + /etc/localtime before apt configures
# tzdata; relying on ARG alone was not enough on Ubuntu package
# upgrade paths.
#
# ARG (not ENV) so the noninteractive flag only applies during the
# image build — won't leak into the running container, where any
# future `apt-get install` should be interactive by default.
ARG DEBIAN_FRONTEND=noninteractive
ARG TZ=Etc/UTC
RUN set -eux; \
    ln -snf "/usr/share/zoneinfo/${TZ}" /etc/localtime; \
    echo "${TZ}" > /etc/timezone; \
    apt-get update; \
    DEBIAN_FRONTEND="${DEBIAN_FRONTEND}" TZ="${TZ}" apt-get install -y --no-install-recommends \
        xvfb \
        fluxbox \
        x11vnc \
        websockify \
        dbus-x11 \
        ca-certificates \
        tini \
        tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Slim runtime deps. The Microsoft base image already ships Chromium +
# all OS-level deps for it, so we only need the Python web layer.
#
# --ignore-installed: the noble base image has apt-managed
# python3-typing-extensions 4.10.0 from the X11 stack install (pulled
# in by python3-jwcrypto). Pydantic 2.11+ needs >=4.12.2; without
# --ignore-installed pip tries to uninstall the apt package and fails
# ("no RECORD file"). The flag makes pip skip the uninstall step and
# put the new wheel in /usr/local/lib/python3.12/dist-packages, which
# has higher sys.path priority than apt's /usr/lib/python3/...
RUN pip install --no-cache-dir --ignore-installed \
    fastapi==0.115.4 \
    uvicorn[standard]==0.32.0 \
    pydantic==2.11.5 \
    httpx==0.28.1 \
    websockets==15.0.1 \
    playwright==1.55.0 \
    playwright-stealth==1.0.6 \
    "browser-use==0.7.12"

# browser-use is the L2 generic browser agent (LLM-driven, any-site
# fallback) used by providers/generic_browser.py. It pulls in its own
# Anthropic / OpenAI client deps; we pin a specific version because
# browser-use is pre-1.0 and breaks the Agent API every few months.
# Bump deliberately, never with a floating range.

COPY runner.py /app/runner.py
COPY login_session.py /app/login_session.py
COPY artifact_store.py /app/artifact_store.py
COPY entrypoint.sh /app/entrypoint.sh
COPY providers /app/providers

RUN chmod +x /app/entrypoint.sh

# Fluxbox config: hide the toolbar so the user sees a pristine browser.
RUN mkdir -p /home/pwuser/.fluxbox \
    && echo "session.screen0.toolbar.visible: false" > /home/pwuser/.fluxbox/init \
    && chown -R pwuser:pwuser /home/pwuser

# Non-root for safety (Microsoft's base image creates pwuser).
USER pwuser

# DISPLAY is NOT set image-wide — each headed session passes its own
# display number (allocated from the pool in login_session.py) as an
# explicit env on the Playwright launch. Headless `/perform` calls
# don't need a display at all. Setting it globally would cause
# Chromium to try to connect to a non-existent :99 if any future
# code path forgot to override.
ENV HOME=/home/pwuser

EXPOSE 5200

# tini reaps zombie X processes when entrypoint.sh exits. Without it
# the container leaks PIDs across soft restarts.
ENTRYPOINT ["/usr/bin/tini", "--", "/app/entrypoint.sh"]
