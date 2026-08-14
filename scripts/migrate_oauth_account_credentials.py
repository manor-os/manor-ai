#!/usr/bin/env python3
"""Backfill OAuthAccount plaintext tokens into CredentialService storage."""
from __future__ import annotations

import argparse
import asyncio
import os

from sqlalchemy import func, or_, select

from packages.core.credentials import get_credential_service
from packages.core.database import async_session
from packages.core.models.user import OAuthAccount


async def _run(*, apply: bool, batch_size: int) -> int:
    service = None
    backend = os.getenv("CREDENTIAL_BACKEND", "vault").strip().lower()
    if apply:
        service = get_credential_service()
        backend = service.backend
        if backend in {"legacy", "legacy_jsonb", "legacy_columns"}:
            raise RuntimeError("Refusing backfill with a plaintext credential backend")

    async with async_session() as db:
        before = int((await db.execute(
            select(func.count()).select_from(OAuthAccount).where(
                or_(
                    OAuthAccount.access_token.is_not(None),
                    OAuthAccount.refresh_token.is_not(None),
                )
            )
        )).scalar_one())
        print(f"plaintext_rows_before={before} backend={backend}")
        if not apply:
            print("dry_run=true; rerun with --apply after verifying Vault readiness")
            return 0

        migrated = 0
        while True:
            rows = list((await db.execute(
                select(OAuthAccount).where(
                    or_(
                        OAuthAccount.access_token.is_not(None),
                        OAuthAccount.refresh_token.is_not(None),
                    )
                ).limit(batch_size)
            )).scalars().all())
            if not rows:
                break
            for row in rows:
                if service is None:  # Defensive: --apply always initialises it.
                    raise RuntimeError("Credential service was not initialised")
                service.store_oauth_account(
                    row,
                    {
                        key: value
                        for key, value in {
                            "access_token": row.access_token,
                            "refresh_token": row.refresh_token,
                        }.items()
                        if value
                    },
                )
                migrated += 1
            await db.commit()

        after = int((await db.execute(
            select(func.count()).select_from(OAuthAccount).where(
                or_(
                    OAuthAccount.access_token.is_not(None),
                    OAuthAccount.refresh_token.is_not(None),
                )
            )
        )).scalar_one())
        encrypted = int((await db.execute(
            select(func.count()).select_from(OAuthAccount).where(
                OAuthAccount.credential_ref.is_not(None)
            )
        )).scalar_one())
        print(
            f"migrated={migrated} plaintext_rows_after={after} "
            f"encrypted_rows={encrypted}"
        )
        return 0 if after == 0 else 2


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--batch-size", type=int, default=100)
    args = parser.parse_args()
    return asyncio.run(_run(apply=args.apply, batch_size=max(1, args.batch_size)))


if __name__ == "__main__":
    raise SystemExit(main())
