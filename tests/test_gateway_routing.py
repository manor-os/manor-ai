from __future__ import annotations

import re
from pathlib import Path


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


def test_group_file_upload_nginx_limit_is_route_scoped():
    nginx_conf = (ROOT / "docker" / "nginx.conf").read_text()
    upload_route = next(
        block for block in _nginx_location_blocks(nginx_conf)
        if "^/api/v1/chat/rooms/[^/]+/files/?$" in block
    )
    assert "client_max_body_size 1025m;" in upload_route
    assert "proxy_request_buffering off;" in upload_route
    assert "proxy_pass http://$api_upstream;" in upload_route
    assert nginx_conf.index("location ~ ^/api/v1/chat/rooms/") < nginx_conf.index("location /api/")


def test_group_file_download_access_log_keeps_diagnostics_without_credentials():
    nginx_conf = (ROOT / "docker" / "nginx.conf").read_text()
    download_route = next(
        block for block in _nginx_location_blocks(nginx_conf)
        if "/file-downloads" in block
    )
    # A route override must replace the inherited combined log, which includes
    # both the ticket-bearing request line and an arbitrary Referer header.
    logs = re.findall(r"(?m)^\s*access_log\s+([^;]+);", download_route)
    assert len(logs) == 1
    destination, format_name = logs[0].split()
    assert destination != "off"
    log_format = re.search(
        rf"(?ms)^log_format\s+{re.escape(format_name)}\s+(.+?);", nginx_conf,
    )
    assert log_format is not None
    variables = set(re.findall(r"\$\{?([a-zA-Z_]\w*)", log_format[1]))
    assert {"request_method", "uri", "status", "request_time", "upstream_status"} <= variables
    assert variables <= {
        "remote_addr", "time_local", "request_method", "uri", "server_protocol",
        "status", "body_bytes_sent", "request_time", "upstream_status", "upstream_response_time",
    }, "Download access logs must not include query strings or request headers"
    # Fixed-format nginx errors append the raw signed URL. Only this location
    # drops those lines; its safe access log still records failed transfers.
    error_log = r"(?m)^\s*error_log\s+/dev/null(?:\s+\w+)?\s*;"
    assert len(re.findall(error_log, download_route)) == 1
    assert len(re.findall(error_log, nginx_conf)) == 1


def test_group_file_body_precheck_runs_after_rate_limiters():
    from fastapi import FastAPI

    from apps.api.middleware import setup_middleware
    from apps.api.middleware.rate_limit import ChatRateLimitMiddleware
    from apps.api.middleware.room_file_ingress import RoomFileIngressLimitMiddleware
    from apps.api.middleware_core import RateLimitMiddleware

    app = FastAPI()
    setup_middleware(app)
    order = [entry.cls for entry in app.user_middleware]  # outermost first
    assert order.index(ChatRateLimitMiddleware) < order.index(RoomFileIngressLimitMiddleware)
    assert order.index(RateLimitMiddleware) < order.index(RoomFileIngressLimitMiddleware)
    assert order[-1] is RoomFileIngressLimitMiddleware




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


def test_nginx_gzips_static_text_assets_without_including_api_or_sse_types():
    nginx_conf = (ROOT / "docker" / "nginx.conf").read_text()

    assert "gzip on;" in nginx_conf
    assert "gzip_vary on;" in nginx_conf
    assert "gzip_min_length 1024;" in nginx_conf
    assert (
        "gzip_types text/css application/javascript text/javascript "
        "application/wasm image/svg+xml;"
    ) in nginx_conf
    gzip_types = nginx_conf.split("gzip_types", 1)[1].split(";", 1)[0]
    assert "application/json" not in gzip_types
    assert "text/event-stream" not in gzip_types


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


def test_nginx_blueprint_alias_bypasses_public_asset_directory():
    nginx_conf = (ROOT / "docker" / "nginx.conf").read_text()
    blocks = _nginx_location_blocks(nginx_conf)

    for route in ("/blueprints", "/blueprints/"):
        block = next(block for block in blocks if block.startswith(f"location = {route} {{"))
        assert "rewrite ^ /index.html last;" in block
        assert "$uri/" not in block
    # Nested cover/sample URLs continue through the normal static-file handler.
    assert "location ^~ /blueprints/" not in nginx_conf




def test_runtime_reconnect_events_are_part_of_chat_stream_route_contract():
    reconnect_path = "/api/v1/chat/runs/run-123/events"

    assert reconnect_path in CHAT_STREAM_ROUTE_EXAMPLES


