"""Local live API + SMTP inbox for pdf-signature-live.spec.ts.

Run from the repository root with PYTHONPATH=. .venv/bin/python <this file>.
Uses a dedicated database and filesystem; never sends mail outside loopback.
"""
from __future__ import annotations

import asyncio
from email import policy
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import socketserver
import threading
import tempfile
from urllib.parse import urlsplit

DATABASE_NAME = "manor_signature_e2e_test"
ROOT = Path(os.getenv("E2E_SIGNATURE_ROOT", str(Path(tempfile.gettempdir()) / "manor-signature-e2e")))
API_PORT = int(os.getenv("E2E_SIGNATURE_API_PORT", "8000"))
SMTP_PORT = int(os.getenv("E2E_SIGNATURE_SMTP_PORT", "1026"))
INBOX_PORT = int(os.getenv("E2E_SIGNATURE_INBOX_PORT", "8026"))
MESSAGES: list[dict] = []
DELIVERY = {"enabled": True}
MAIL_LOCK = threading.Lock()


class InboxSMTP(socketserver.StreamRequestHandler):
    def handle(self):
        self.wfile.write(b"220 signature-e2e SMTP inbox\r\n")
        recipients = []
        while line := self.rfile.readline():
            command = line.decode("utf-8", errors="replace").strip()
            verb = command.split(" ", 1)[0].upper()
            if verb in {"EHLO", "HELO"}:
                self.wfile.write(b"250-signature-e2e\r\n250 AUTH PLAIN\r\n")
            elif verb == "AUTH" and command.upper().startswith("AUTH PLAIN "):
                self.wfile.write(b"235 Test authentication accepted\r\n")
            elif verb == "MAIL":
                recipients = []
                self.wfile.write(b"250 OK\r\n")
            elif verb == "RCPT":
                recipients.append(command.partition(":")[2].strip().strip("<>"))
                self.wfile.write(b"250 OK\r\n")
            elif verb == "DATA":
                with MAIL_LOCK:
                    rejected = not DELIVERY["enabled"]
                if rejected:
                    self.wfile.write(b"451 Temporary test delivery failure\r\n")
                    continue
                self.wfile.write(b"354 End data with a dot\r\n")
                chunks = []
                while chunk := self.rfile.readline():
                    if chunk == b".\r\n":
                        break
                    chunks.append(chunk[1:] if chunk.startswith(b"..") else chunk)
                message = BytesParser(policy=policy.default).parsebytes(b"".join(chunks))
                content = "\n".join(
                    part.get_content()
                    for part in message.walk()
                    if part.get_content_type() in {"text/plain", "text/html"}
                )
                with MAIL_LOCK:
                    MESSAGES.append({
                        "id": len(MESSAGES) + 1,
                        "from": str(message["From"] or ""),
                        "to": recipients[:],
                        "subject": str(message["Subject"] or ""),
                        "content": content,
                    })
                self.wfile.write(b"250 Message captured locally\r\n")
            elif verb == "QUIT":
                self.wfile.write(b"221 Bye\r\n")
                break
            elif verb in {"RSET", "NOOP"}:
                self.wfile.write(b"250 OK\r\n")
            else:
                self.wfile.write(b"502 Unsupported command\r\n")


class InboxHTTP(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def reply(self, value, status=200):
        payload = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        if self.path == "/messages":
            with MAIL_LOCK:
                self.reply(MESSAGES[:])
        elif self.path == "/health":
            self.reply({"status": "ok", "smtp_delivery": DELIVERY["enabled"]})
        else:
            self.reply({"error": "not found"}, 404)

    def do_POST(self):
        if self.path != "/delivery-mode":
            return self.reply({"error": "not found"}, 404)
        length = int(self.headers.get("Content-Length", 0))
        if length < 1 or length > 4096:
            return self.reply({"error": "invalid test control payload"}, 400)
        try:
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict):
                raise ValueError("invalid payload")
        except (ValueError, TypeError):
            return self.reply({"error": "invalid test control payload"}, 400)
        with MAIL_LOCK:
            DELIVERY["enabled"] = bool(data.get("enabled"))
            self.reply(dict(DELIVERY))


async def initialize_database():
    import asyncpg
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    import packages.core.models  # noqa: F401
    from packages.core.models.base import Base

    admin_url = os.getenv(
        "E2E_DATABASE_ADMIN_URL",
        "postgresql://manor:manor_secret@127.0.0.1:5434/manor",
    )
    parts = urlsplit(admin_url)
    if parts.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise RuntimeError("Signature E2E requires a loopback PostgreSQL server")
    connection = await asyncpg.connect(admin_url)
    try:
        if not await connection.fetchval("SELECT 1 FROM pg_database WHERE datname=$1", DATABASE_NAME):
            await connection.execute(f'CREATE DATABASE "{DATABASE_NAME}"')
    finally:
        await connection.close()
    database_url = parts._replace(path=f"/{DATABASE_NAME}").geturl()
    os.environ["DATABASE_URL"] = database_url.replace("postgresql://", "postgresql+asyncpg://")
    os.environ["DATABASE_URL_SYNC"] = database_url
    engine = create_async_engine(os.environ["DATABASE_URL"])
    try:
        async with engine.begin() as connection:
            await connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            await connection.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
            await connection.run_sync(Base.metadata.create_all)
            await connection.execute(text("ALTER TABLE documents ADD COLUMN IF NOT EXISTS embedding vector(1024)"))
            await connection.execute(text("ALTER TABLE document_chunks ADD COLUMN IF NOT EXISTS embedding vector(1024)"))
        from packages.core.services.demo_account import ensure_demo_account

        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            await ensure_demo_account(session)
            await session.commit()
    finally:
        await engine.dispose()


if __name__ == "__main__":
    (ROOT / "files").mkdir(parents=True, exist_ok=True)
    os.environ.update({
        "DEPLOYMENT_MODE": "oss",
        "MANOR_FS_ENABLED": "true",
        "MANOR_FS_ROOT": str(ROOT / "files"),
        "EMAIL_ENABLED": "true",
        "SMTP_HOST": "127.0.0.1",
        "SMTP_PORT": str(SMTP_PORT),
        "SMTP_STARTTLS": "false",
        "SMTP_SSL": "false",
        "SMTP_USER": "",
        "SMTP_USERNAME": "",
        "SMTP_PASSWORD": "",
        "SMTP_EGRESS_MODE": "direct",
        "APP_URL": os.getenv("E2E_SIGNATURE_APP_URL", "http://127.0.0.1:5185"),
        "JWT_SECRET_KEY": secrets.token_urlsafe(48),
        "CELERY_BROKER_URL": "memory://",
        "CELERY_RESULT_BACKEND": "cache+memory://",
        "REDIS_URL": "redis://127.0.0.1:6389/15",
        "EMBEDDING_BASE_URL": "http://127.0.0.1:11435/v1",
    })
    # Set the dedicated URL before model imports initialize Settings.
    admin = urlsplit(os.getenv("E2E_DATABASE_ADMIN_URL", "postgresql://manor:manor_secret@127.0.0.1:5434/manor"))
    database = admin._replace(path=f"/{DATABASE_NAME}").geturl()
    os.environ["DATABASE_URL"] = database.replace("postgresql://", "postgresql+asyncpg://")
    os.environ["DATABASE_URL_SYNC"] = database
    asyncio.run(initialize_database())
    socketserver.ThreadingTCPServer.allow_reuse_address = True
    for server in (
        socketserver.ThreadingTCPServer(("127.0.0.1", SMTP_PORT), InboxSMTP),
        ThreadingHTTPServer(("127.0.0.1", INBOX_PORT), InboxHTTP),
    ):
        threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"Signature E2E: API :{API_PORT}, SMTP :{SMTP_PORT}, inbox :{INBOX_PORT}; all loopback", flush=True)
    import uvicorn

    # Real routers/DB/filesystem/SMTP; omit unrelated startup jobs and workers.
    uvicorn.run("apps.api.main:app", host="127.0.0.1", port=API_PORT, lifespan="off")
