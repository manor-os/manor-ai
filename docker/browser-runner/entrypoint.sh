#!/usr/bin/env bash
#
# Browser-runner entrypoint.
#
# Multi-tenant safe: this script does NOT pre-boot a global X server.
# Each headed-login session spawns its own Xvfb + fluxbox + x11vnc +
# websockify quartet on a display number allocated from a pool by
# login_session.py. That gives us:
#
#   * concurrent sessions across tenants — display :100 for tenant A,
#     :101 for tenant B, etc., with isolated pixel buffers and ports
#   * automatic cleanup when a session ends — its 4 child processes
#     exit when ``_Session.close()`` runs
#   * no resource cost when no one is signing in — we only pay for
#     what's in use
#
# tini still wraps uvicorn so that when sessions spawn their X stack
# children, those processes get reaped when they exit (without tini,
# every aborted login leaks 4 zombies).
set -euo pipefail

exec uvicorn runner:app --host 0.0.0.0 --port 5200
