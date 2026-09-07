from __future__ import annotations

import re
from pathlib import Path

import yaml

from apps.api.chat_stream_routes import CHAT_STREAM_ROUTE_EXAMPLES


ROOT = Path(__file__).resolve().parents[1]
APP_SECURITY_HEADERS_INCLUDE = (
    "include /etc/nginx/snippets/manor-app-security-headers.conf;"
)
FORWARDED_SCHEME_HEADER = (
    "proxy_set_header X-Forwarded-Proto $manor_forwarded_scheme;"
)
SCHEME_AGNOSTIC_PROXY_LOCATIONS = frozenset({"location /config", "location /health"})


def _nginx_location_blocks(nginx_conf: str) -> list[str]:
    return re.findall(r"(?ms)^    (location [^{\n]+ \{\n.*?^    \})", nginx_conf)




def test_nginx_routes_streams_to_manor_chat_before_generic_api():
    nginx_conf = (ROOT / "docker" / "nginx.conf").read_text()

    assert "set $chat_upstream ${MANOR_CHAT_UPSTREAM};" in nginx_conf
    assert "proxy_pass http://$chat_upstream;" in nginx_conf
    assert nginx_conf.index("proxy_pass http://$chat_upstream;") < nginx_conf.index("location /api/")
    assert "set $api_upstream ${MANOR_API_UPSTREAM};" in nginx_conf


def test_nginx_preserves_ingress_forwarded_scheme_for_upstream_services():
    nginx_conf = (ROOT / "docker" / "nginx.conf").read_text()

    assert "map $http_x_forwarded_proto $manor_forwarded_scheme" in nginx_conf
    assert "default $http_x_forwarded_proto;" in nginx_conf
    assert "''      $scheme;" in nginx_conf
    assert "proxy_set_header X-Forwarded-Proto $scheme;" not in nginx_conf
    proxied_locations = [
        block
        for block in _nginx_location_blocks(nginx_conf)
        if "proxy_pass " in block
        and block.split(" {", 1)[0] not in SCHEME_AGNOSTIC_PROXY_LOCATIONS
    ]
    assert proxied_locations
    for block in proxied_locations:
        assert FORWARDED_SCHEME_HEADER in block, block.splitlines()[0]


def test_nginx_marks_static_web_responses_for_gateway_smoke():
    nginx_conf = (ROOT / "docker" / "nginx.conf").read_text()

    assert 'add_header X-Manor-Service-Role "web" always;' in nginx_conf
    assert nginx_conf.index('location = /index.html') < nginx_conf.index(
        'add_header X-Manor-Service-Role "web" always;'
    )


def test_nginx_custom_static_locations_preserve_app_security_headers():
    nginx_conf = (ROOT / "docker" / "nginx.conf").read_text()
    dockerfile = (ROOT / "docker" / "Dockerfile.web").read_text()
    security_headers = (
        ROOT / "docker" / "nginx-app-security-headers.conf"
    ).read_text()

    assert "X-Content-Type-Options" in security_headers
    assert "Content-Security-Policy" in security_headers
    assert "X-Frame-Options" in security_headers
    assert APP_SECURITY_HEADERS_INCLUDE in nginx_conf.split("    location ", 1)[0]
    assert (
        "COPY docker/nginx-app-security-headers.conf "
        "/etc/nginx/snippets/manor-app-security-headers.conf"
    ) in dockerfile

    for location in (
        "location = /robots.txt",
        "location = /index.html",
        "location = /admin.html",
        "location = /version.json",
        "location /assets/",
        r"location ~* \.mjs$",
    ):
        block = nginx_conf.split(location, 1)[1].split("}", 1)[0]
        assert APP_SECURITY_HEADERS_INCLUDE in block, location


def test_nginx_domain_root_sitemap_redirects_to_public_marketplace_sitemap():
    nginx_conf = (ROOT / "docker" / "nginx.conf").read_text()
    robots = (ROOT / "apps" / "web" / "public" / "robots.txt").read_text()

    root_sitemap = nginx_conf.split("location = /sitemap.xml", 1)[1].split("}", 1)[0]
    assert "return 308 /marketplace/sitemap.xml;" in root_sitemap
    assert nginx_conf.index("location = /sitemap.xml") < nginx_conf.index("location / {")
    assert "Allow: /sitemap.xml" in robots
    assert "Sitemap: https://app.manorai.xyz/marketplace/sitemap.xml" in robots




def test_runtime_reconnect_events_are_part_of_chat_stream_route_contract():
    reconnect_path = "/api/v1/chat/runs/run-123/events"

    assert reconnect_path in CHAT_STREAM_ROUTE_EXAMPLES


def _caddy_stream_matcher_covers(caddyfile: str, path: str) -> bool:
    if path.startswith("/api/v1/chat/runs/") and path.endswith("/events"):
        return "/api/v1/chat/runs/*/events" in caddyfile
    if path.startswith("/api/v1/public/chat/"):
        return "/api/v1/public/chat/*/message/stream" in caddyfile
    if path.startswith("/api/v1/workspace-drafts/") and path.endswith("/messages/stream"):
        return "/api/v1/workspace-drafts/*/messages/stream" in caddyfile
    if path.startswith("/api/v1/workspace-drafts/") and path.endswith("/finalize/stream"):
        return "/api/v1/workspace-drafts/*/finalize/stream" in caddyfile
    return path in caddyfile


def _nginx_stream_location_covers(nginx_conf: str, path: str) -> bool:
    if path.startswith("/api/v1/chat/runs/") and path.endswith("/events"):
        return r"^/api/v1/chat/runs/[^/]+/events$" in nginx_conf
    if path.startswith("/api/v1/public/chat/"):
        return r"^/api/v1/public/chat/[^/]+/message/stream$" in nginx_conf
    if path.startswith("/api/v1/workspace-drafts/") and (
        path.endswith("/messages/stream") or path.endswith("/finalize/stream")
    ):
        return r"^/api/v1/workspace-drafts/[^/]+/(messages|finalize)/stream$" in nginx_conf
    if path in {"/api/v1/agents/generate-stream", "/api/v1/agents/generate-draft-stream", "/api/v1/skills/generate-stream"}:
        return r"^/api/v1/(agents/(generate-stream|generate-draft-stream)|skills/generate-stream)$" in nginx_conf
    return f"location = {path}" in nginx_conf


def _k8s_stream_ingress_covers(ingress_paths: list[str], path: str) -> bool:
    if path.startswith("/api/v1/chat/runs/") and path.endswith("/events"):
        return "/api/v1/chat/runs/[^/]+/events" in ingress_paths
    if path.startswith("/api/v1/public/chat/"):
        return "/api/v1/public/chat/[^/]+/message/stream" in ingress_paths
    if path.startswith("/api/v1/workspace-drafts/") and path.endswith("/messages/stream"):
        return "/api/v1/workspace-drafts/[^/]+/messages/stream" in ingress_paths
    if path.startswith("/api/v1/workspace-drafts/") and path.endswith("/finalize/stream"):
        return "/api/v1/workspace-drafts/[^/]+/finalize/stream" in ingress_paths
    return path in ingress_paths
