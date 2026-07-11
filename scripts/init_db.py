"""
Seed helper — runs on every boot after schema is ready.

Responsibilities (idempotent data seeds that change with code):
  1. System tool_definitions  — derived from ALWAYS_LOADED at runtime
  2. MCP catalog rows         — derived from built-in MCP server list
  3. pgvector embedding column dimension (configurable via EMBEDDING_DIMENSIONS)
  4. Default staff roles (viewer, member, admin, owner)

All seeds use ON CONFLICT DO NOTHING or equivalent, safe to call any
number of times.

Usage:
    python scripts/init_db.py
"""
from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

# Ensure project root is on sys.path
project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from packages.core.config import get_settings
from packages.core.models.base import generate_ulid
from packages.core.ai.tool_pool import ALWAYS_LOADED

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


async def seed_system_tool_definitions(engine) -> None:
    """Upsert system tool_definitions derived from ALWAYS_LOADED + runtime tool pool."""
    from packages.core.ai.tool_pool import tool_pool

    # Ensure runtime tools are loaded so we can seed a complete practical baseline.
    if not tool_pool._tools:
        tool_pool.initialize()

    tool_names = set(ALWAYS_LOADED) | set(tool_pool._tools.keys())

    async with engine.begin() as conn:
        for tool_name in sorted(tool_names):
            await conn.execute(
                text(
                    """
                    INSERT INTO tool_definitions (id, name, display_name, description, category, status)
                    VALUES (:id, :name, :display_name, :description, :category, 'active')
                    ON CONFLICT (name) DO NOTHING
                    """
                ),
                {
                    "id": generate_ulid(),
                    "name": tool_name,
                    "display_name": tool_name.replace("_", " ").title(),
                    "description": f"System tool: {tool_name}",
                    "category": "system" if tool_name in ALWAYS_LOADED else "builtin",
                },
            )
    logger.info("Seeded %d system tool definitions.", len(tool_names))


async def retire_legacy_tool_definitions(engine) -> None:
    """Remove retired aliases, preserving old bindings via current equivalents."""
    async with engine.begin() as conn:
        await conn.execute(
            text(
                """
                INSERT INTO agent_tool_bindings (agent_id, tool_id)
                SELECT b.agent_id, dst.id
                FROM agent_tool_bindings b
                JOIN tool_definitions src ON src.id = b.tool_id
                JOIN tool_definitions dst ON dst.name = 'generate_document_file'
                WHERE src.name = 'upload_text_document'
                ON CONFLICT DO NOTHING
                """
            )
        )
        await conn.execute(
            text(
                """
                DELETE FROM agent_tool_bindings b
                USING tool_definitions t
                WHERE b.tool_id = t.id
                  AND t.name = 'upload_text_document'
                """
            )
        )
        await conn.execute(
            text(
                """
                DELETE FROM tool_definitions
                WHERE name = 'upload_text_document'
                """
            )
        )
    logger.info("Retired legacy tool definitions.")


async def ensure_embedding_column(engine) -> None:
    """Add embedding column to documents (dimension is runtime-configurable)."""
    from packages.core.services.embedding_service import get_embedding_dimensions
    dim = get_embedding_dimensions()
    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.execute(
            text(f"ALTER TABLE documents ADD COLUMN IF NOT EXISTS embedding vector({dim})")
        )
        await conn.execute(text("""
            CREATE INDEX IF NOT EXISTS ix_documents_embedding
            ON documents USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100)
        """))
    logger.info("pgvector ready — embedding column: vector(%d).", dim)




async def seed_default_roles(engine) -> None:
    """Seed default staff role templates."""
    import json

    viewer = ["entity.read", "tasks.read", "docs.read", "agents.read",
              "chat.use", "workspaces.read", "integrations.read"]
    member = viewer + ["tasks.create", "tasks.update", "tasks.assign",
                       "docs.upload", "agents.create",
                       "integrations.connect", "mcp.use_personal"]
    admin = member + ["entity.update", "users.read", "users.invite",
                      "tasks.delete", "docs.delete", "agents.update", "agents.delete",
                      "workspaces.create", "workspaces.update", "workspaces.delete",
                      "admin.settings", "admin.audit", "chat.view_all",
                      "integrations.manage", "mcp.quickbooks.use", "mcp.stripe.use"]
    owner = admin + ["users.manage", "admin.api_keys", "admin.webhooks", "admin.billing"]

    roles = [("viewer", viewer, False), ("member", member, True),
             ("admin", admin, False), ("owner", owner, False)]

    async with engine.begin() as conn:
        for name, perms, is_default in roles:
            role_id = f"role_tmpl_{name}".ljust(26, "_")[:26]
            await conn.execute(
                text(
                    "INSERT INTO staff_roles (id, entity_id, name, permissions, is_default, status) "
                    "VALUES (:id, :eid, :name, CAST(:perms AS jsonb), :is_def, 'active') "
                    "ON CONFLICT (id) DO NOTHING"
                ),
                {"id": role_id, "eid": "0" * 26, "name": name,
                 "perms": json.dumps(perms), "is_def": is_default},
            )
    logger.info("Seeded default staff roles.")




async def main() -> None:
    settings = get_settings()
    logger.info("Connecting to: %s", settings.DATABASE_URL.split("@")[-1])

    engine = create_async_engine(settings.DATABASE_URL, echo=False)

    try:
        try:
            await ensure_embedding_column(engine)
        except Exception as e:
            logger.warning("pgvector setup failed (non-fatal): %s", e)

        try:
            await seed_system_tool_definitions(engine)
        except Exception as e:
            logger.warning("Could not seed system tool definitions: %s", e)

        try:
            await retire_legacy_tool_definitions(engine)
        except Exception as e:
            logger.warning("Could not retire legacy tool definitions: %s", e)

        try:
            from packages.core.services.mcp_seed import seed_mcp_catalog
            await seed_mcp_catalog(engine)
        except Exception as e:
            logger.warning("Could not seed MCP catalog: %s", e)


        try:
            await seed_default_roles(engine)
        except Exception as e:
            logger.warning("Could not seed default roles: %s", e)


        logger.info("init_db complete.")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
