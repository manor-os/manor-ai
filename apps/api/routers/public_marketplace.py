"""Public, indexable Workspace Marketplace pages and metadata APIs.

Only platform-reviewed ``published`` Blueprint rows are visible here.  The
installable payload, source Workspace id, share token, private entity fields,
credentials, and purchase entitlements never cross this boundary.
"""
from __future__ import annotations

import html
import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.routers.blueprints import (
    BlueprintSetupPreview,
    _author_avatars,
    _marketplace_signals,
    _payload_setup_preview,
    _showcase_assets,
)
from packages.core.constants.blueprints import BlueprintStatus
from packages.core.database import get_db
from packages.core.models.blueprint import WorkspaceBlueprint
from packages.core.services.entity_fs import resolve_path

api_router = APIRouter(
    prefix="/api/v1/public/marketplace",
    tags=["public-marketplace"],
)
page_router = APIRouter(prefix="/marketplace", tags=["public-marketplace-pages"])

_DEFAULT_PUBLIC_ORIGIN = "https://app.manorai.xyz"
_PLATFORM_CREATOR_ID = "manor"
_MAX_CATALOG_ITEMS = 200
_SAFE_SLUG_RE = re.compile(r"[^a-z0-9]+")


class PublicShowcaseAsset(BaseModel):
    id: str
    kind: str
    url: str
    caption: str | None = None
    alt_text: str | None = None


class PublicBlueprint(BaseModel):
    id: str
    slug: str
    public_url: str
    title: str
    summary: str | None = None
    description: str | None = None
    tags: list[str] = Field(default_factory=list)
    cover_image_url: str | None = None
    showcase_assets: list[PublicShowcaseAsset] = Field(default_factory=list)
    creator_id: str
    creator_url: str
    author_handle: str | None = None
    author_display_name: str
    author_avatar_url: str | None = None
    install_count: int = 0
    purchase_count: int = 0
    favorite_count: int = 0
    remix_count: int = 0
    price_cents: int = 0
    list_price_cents: int | None = None
    currency: str = "usd"
    setup_preview: BlueprintSetupPreview = Field(default_factory=BlueprintSetupPreview)
    published_at: datetime | None = None
    updated_at: datetime | None = None


class PublicCreator(BaseModel):
    id: str
    public_url: str
    handle: str | None = None
    display_name: str
    avatar_url: str | None = None
    blueprint_count: int
    total_installs: int
    total_purchases: int
    blueprints: list[PublicBlueprint]


def _public_origin() -> str:
    configured = (os.getenv("APP_URL") or _DEFAULT_PUBLIC_ORIGIN).strip().rstrip("/")
    parsed = urlsplit(configured)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return _DEFAULT_PUBLIC_ORIGIN
    return f"{parsed.scheme}://{parsed.netloc}"


def _slug(value: str, fallback: str = "workspace") -> str:
    normalized = _SAFE_SLUG_RE.sub("-", value.strip().lower()).strip("-")
    return normalized[:100] or fallback


def _creator_id(row: WorkspaceBlueprint) -> str:
    return _PLATFORM_CREATOR_ID if row.entity_id is None else row.entity_id


def _blueprint_path(row: WorkspaceBlueprint) -> str:
    return (
        f"/marketplace/blueprints/{quote(row.id, safe='')}/"
        f"{_slug(row.slug or row.title)}"
    )


def _creator_path(row: WorkspaceBlueprint) -> str:
    return f"/marketplace/creators/{quote(_creator_id(row), safe='')}"


def _external_media_url(value: str | None) -> str | None:
    if not value:
        return None
    parsed = urlsplit(value)
    if parsed.scheme == "https" and parsed.netloc:
        return value
    if value.startswith("/assets/blueprints/"):
        return value
    return None


def _asset_public_path(row: WorkspaceBlueprint, asset_id: str) -> str:
    return (
        f"/marketplace/assets/{quote(row.id, safe='')}/"
        f"{quote(asset_id, safe='')}"
    )


def _public_assets(row: WorkspaceBlueprint) -> list[PublicShowcaseAsset]:
    result: list[PublicShowcaseAsset] = []
    for asset in _showcase_assets(row.showcase_assets):
        public_url = _external_media_url(asset.url)
        if public_url is None and row.entity_id and asset.url.startswith(
            f"/api/v1/fs/{row.entity_id}/marketplace/blueprints/{row.id}/"
        ):
            public_url = _asset_public_path(row, asset.id)
        if public_url is None:
            continue
        result.append(PublicShowcaseAsset(
            id=asset.id,
            kind=asset.kind,
            url=public_url,
            caption=asset.caption,
            alt_text=asset.alt_text,
        ))
    return result


def _cover_url(
    row: WorkspaceBlueprint,
    assets: list[PublicShowcaseAsset],
) -> str | None:
    external = _external_media_url(row.cover_image_url)
    if external:
        return external
    if row.cover_image_url:
        raw_match = next(
            (
                raw
                for raw in _showcase_assets(row.showcase_assets)
                if raw.url == row.cover_image_url
            ),
            None,
        )
        if raw_match is not None:
            public_match = next(
                (public for public in assets if public.id == raw_match.id),
                None,
            )
            if public_match is not None:
                return public_match.url
    return next((asset.url for asset in assets if asset.kind == "image"), None)


def _absolute_url(value: str | None) -> str | None:
    if not value:
        return None
    if value.startswith("/"):
        return f"{_public_origin()}{value}"
    return value


def _author_identity(row: WorkspaceBlueprint) -> tuple[str | None, str]:
    manifest = row.payload.get("manifest") if isinstance(row.payload, dict) else {}
    manifest = manifest if isinstance(manifest, dict) else {}
    author = manifest.get("author") if isinstance(manifest.get("author"), dict) else {}
    handle = row.author_handle or author.get("handle")
    handle = handle if isinstance(handle, str) and handle.strip() else None
    display_name = row.author_display_name or author.get("display_name")
    if not isinstance(display_name, str) or not display_name.strip():
        display_name = "Manor AI" if row.entity_id is None else "Manor creator"
    return handle, display_name.strip()


def _public_blueprint(
    row: WorkspaceBlueprint,
    *,
    author_avatar_url: str | None = None,
    favorite_count: int = 0,
    remix_count: int = 0,
) -> PublicBlueprint:
    assets = _public_assets(row)
    handle, display_name = _author_identity(row)
    return PublicBlueprint(
        id=row.id,
        slug=row.slug,
        public_url=f"{_public_origin()}{_blueprint_path(row)}",
        title=row.title,
        summary=row.summary,
        description=row.description,
        tags=[tag for tag in (row.tags or []) if isinstance(tag, str) and tag],
        cover_image_url=_absolute_url(_cover_url(row, assets)),
        showcase_assets=[
            asset.model_copy(update={"url": _absolute_url(asset.url) or asset.url})
            for asset in assets
        ],
        creator_id=_creator_id(row),
        creator_url=f"{_public_origin()}{_creator_path(row)}",
        author_handle=handle,
        author_display_name=display_name,
        author_avatar_url=_external_media_url(author_avatar_url),
        install_count=max(0, int(row.install_count or 0)),
        purchase_count=max(0, int(row.purchase_count or 0)),
        favorite_count=max(0, int(favorite_count or 0)),
        remix_count=max(0, int(remix_count or 0)),
        price_cents=max(0, int(row.price_cents or 0)),
        list_price_cents=(
            max(0, int(row.list_price_cents))
            if row.list_price_cents is not None
            else None
        ),
        currency=(row.currency or "usd").lower(),
        setup_preview=_payload_setup_preview(row.payload),
        published_at=row.published_at,
        updated_at=row.updated_at,
    )


async def _published_rows(
    db: AsyncSession,
    *,
    limit: int | None = None,
    offset: int = 0,
) -> list[WorkspaceBlueprint]:
    stmt = (
        select(WorkspaceBlueprint)
        .where(WorkspaceBlueprint.status == BlueprintStatus.PUBLISHED.value)
        .order_by(
            WorkspaceBlueprint.purchase_count.desc(),
            WorkspaceBlueprint.install_count.desc(),
            WorkspaceBlueprint.published_at.desc().nulls_last(),
            WorkspaceBlueprint.created_at.desc(),
        )
        .offset(offset)
    )
    if limit is not None:
        stmt = stmt.limit(limit)
    return list((await db.execute(stmt)).scalars().all())


async def _published_blueprint(
    db: AsyncSession,
    blueprint_id: str,
) -> WorkspaceBlueprint | None:
    return (await db.execute(
        select(WorkspaceBlueprint).where(
            WorkspaceBlueprint.id == blueprint_id,
            WorkspaceBlueprint.status == BlueprintStatus.PUBLISHED.value,
        )
    )).scalar_one_or_none()


async def _serialize_rows(
    db: AsyncSession,
    rows: list[WorkspaceBlueprint],
) -> list[PublicBlueprint]:
    if not rows:
        return []
    avatars = await _author_avatars(db, rows)
    signals = await _marketplace_signals(db, rows)
    return [
        _public_blueprint(
            row,
            author_avatar_url=avatars.get(row.author_user_id or ""),
            favorite_count=int(signals.get(row.id, {}).get("favorite_count", 0)),
            remix_count=int(signals.get(row.id, {}).get("remix_count", 0)),
        )
        for row in rows
    ]


def _creator_from_blueprints(
    creator_id: str,
    blueprints: list[PublicBlueprint],
) -> PublicCreator:
    first = blueprints[0]
    return PublicCreator(
        id=creator_id,
        public_url=first.creator_url,
        handle=first.author_handle,
        display_name=first.author_display_name,
        avatar_url=first.author_avatar_url,
        blueprint_count=len(blueprints),
        total_installs=sum(item.install_count for item in blueprints),
        total_purchases=sum(item.purchase_count for item in blueprints),
        blueprints=blueprints,
    )


async def _creator_blueprints(
    db: AsyncSession,
    creator_id: str,
) -> list[PublicBlueprint]:
    creator_filter = (
        WorkspaceBlueprint.entity_id.is_(None)
        if creator_id == _PLATFORM_CREATOR_ID
        else WorkspaceBlueprint.entity_id == creator_id
    )
    rows = list((await db.execute(
        select(WorkspaceBlueprint)
        .where(
            WorkspaceBlueprint.status == BlueprintStatus.PUBLISHED.value,
            creator_filter,
        )
        .order_by(
            WorkspaceBlueprint.purchase_count.desc(),
            WorkspaceBlueprint.install_count.desc(),
            WorkspaceBlueprint.published_at.desc().nulls_last(),
        )
    )).scalars().all())
    return await _serialize_rows(db, rows)


@api_router.get("/blueprints", response_model=list[PublicBlueprint])
async def list_public_blueprints(
    limit: int = Query(60, ge=1, le=_MAX_CATALOG_ITEMS),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
):
    return await _serialize_rows(
        db,
        await _published_rows(db, limit=limit, offset=offset),
    )


@api_router.get("/blueprints/{blueprint_id}", response_model=PublicBlueprint)
async def get_public_blueprint(
    blueprint_id: str,
    db: AsyncSession = Depends(get_db),
):
    row = await _published_blueprint(db, blueprint_id)
    if row is None:
        raise HTTPException(404, "Published Workspace Blueprint not found")
    return (await _serialize_rows(db, [row]))[0]


@api_router.get("/creators/{creator_id}", response_model=PublicCreator)
async def get_public_creator(
    creator_id: str,
    db: AsyncSession = Depends(get_db),
):
    blueprints = await _creator_blueprints(db, creator_id)
    if not blueprints:
        raise HTTPException(404, "Published Workspace creator not found")
    return _creator_from_blueprints(creator_id, blueprints)


def _e(value: Any) -> str:
    return html.escape(str(value or ""), quote=True)


def _json_ld(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")


def _price(item: PublicBlueprint) -> str:
    if item.price_cents <= 0:
        return "Free"
    return f"${item.price_cents / 100:,.2f} {item.currency.upper()}"


def _stats(item: PublicBlueprint) -> str:
    parts = [f"{item.install_count:,} installs"]
    if item.purchase_count:
        parts.append(f"{item.purchase_count:,} purchases")
    if item.remix_count:
        parts.append(f"{item.remix_count:,} remixes")
    return " · ".join(parts)


def _card(item: PublicBlueprint) -> str:
    image = (
        f'<img src="{_e(item.cover_image_url)}" alt="{_e(item.title)} workspace cover" loading="lazy">'
        if item.cover_image_url
        else '<div class="cover-mark" aria-hidden="true">M</div>'
    )
    tags = "".join(f"<span>{_e(tag)}</span>" for tag in item.tags[:4])
    return f"""
      <article class="card">
        <a class="cover" href="{_e(urlsplit(item.public_url).path)}">{image}</a>
        <div class="card-body">
          <div class="eyebrow"><a href="{_e(urlsplit(item.creator_url).path)}">{_e(item.author_display_name)}</a><b>{_e(_price(item))}</b></div>
          <h2><a href="{_e(urlsplit(item.public_url).path)}">{_e(item.title)}</a></h2>
          <p>{_e(item.summary or 'A long-running AI business workspace Blueprint.')}</p>
          <div class="tags">{tags}</div>
          <small>{_e(_stats(item))}</small>
        </div>
      </article>
    """


_STYLE = """
:root{--ink:#17140f;--muted:#6f675d;--line:#ded7cb;--paper:#fbf8f2;--wash:#f1ece3;--green:#1f7655;--gold:#9a6b20}*{box-sizing:border-box}body{margin:0;background:var(--paper);color:var(--ink);font:16px/1.6 Inter,ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}a{color:inherit}.nav{height:72px;border-bottom:1px solid var(--line);display:flex;align-items:center;justify-content:space-between;padding:0 max(24px,calc((100vw - 1180px)/2));background:rgba(251,248,242,.96)}.brand{font-weight:800;text-decoration:none;letter-spacing:-.02em}.nav-links{display:flex;gap:22px;align-items:center}.nav-links a{text-decoration:none;color:var(--muted);font-size:14px}.nav-links .button,.button{display:inline-flex;align-items:center;justify-content:center;border-radius:999px;background:var(--green);color:white;text-decoration:none;font-weight:750;padding:11px 18px}.shell{width:min(1180px,calc(100% - 40px));margin:auto}.hero{padding:72px 0 48px;display:grid;grid-template-columns:minmax(0,1.35fr) minmax(280px,.65fr);gap:48px;align-items:end}.kicker,.eyebrow{font-size:12px;letter-spacing:.12em;text-transform:uppercase;color:var(--gold);font-weight:800}.hero h1{font-size:clamp(42px,7vw,78px);line-height:.98;letter-spacing:-.055em;margin:14px 0 20px;max-width:900px}.hero p{font-size:20px;color:var(--muted);max-width:760px;margin:0}.hero-aside{border-left:1px solid var(--line);padding-left:28px}.hero-aside strong{display:block;font-size:28px}.hero-aside span{color:var(--muted)}.grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:20px;padding:18px 0 80px}.card{border:1px solid var(--line);border-radius:22px;overflow:hidden;background:#fff;min-width:0}.cover{height:190px;background:linear-gradient(145deg,#dfe9df,#e8dcc5);display:grid;place-items:center;overflow:hidden}.cover img{width:100%;height:100%;object-fit:cover}.cover-mark{font:800 64px/1 Georgia,serif;color:rgba(23,20,15,.35)}.card-body{padding:20px}.card .eyebrow{display:flex;justify-content:space-between;gap:12px;letter-spacing:.02em;text-transform:none}.card .eyebrow a{color:var(--green)}.card h2{font-size:22px;line-height:1.18;letter-spacing:-.025em;margin:10px 0}.card h2 a{text-decoration:none}.card p{color:var(--muted);margin:0 0 15px}.tags{display:flex;flex-wrap:wrap;gap:7px;margin-bottom:13px}.tags span{background:var(--wash);border-radius:999px;padding:3px 9px;font-size:12px}.card small{color:var(--muted)}.detail{padding:64px 0 90px}.breadcrumb{font-size:14px;color:var(--muted);margin-bottom:34px}.breadcrumb a{color:var(--green)}.detail-head{display:grid;grid-template-columns:minmax(0,1fr) 420px;gap:54px;align-items:start}.detail h1{font-size:clamp(42px,6vw,72px);line-height:1;letter-spacing:-.05em;margin:12px 0 20px}.lead{font-size:20px;color:var(--muted);max-width:720px}.creator-line{margin:24px 0}.creator-line a{color:var(--green);font-weight:750}.price{font-size:28px;font-weight:850;margin:18px 0}.visual{border-radius:24px;overflow:hidden;min-height:360px;background:linear-gradient(145deg,#dfe9df,#e8dcc5);display:grid;place-items:center}.visual img{width:100%;height:100%;min-height:360px;object-fit:cover}.sections{display:grid;grid-template-columns:1fr 1fr;gap:20px;margin-top:54px}.panel{border:1px solid var(--line);border-radius:20px;padding:26px;background:#fff}.panel h2{margin:0 0 12px;font-size:22px}.panel p{white-space:pre-line;color:var(--muted)}.panel ul{padding-left:20px}.panel li+li{margin-top:8px}.cta{margin-top:50px;padding:30px;border-radius:22px;background:var(--ink);color:white;display:flex;justify-content:space-between;gap:30px;align-items:center}.cta h2{margin:0 0 7px}.cta p{margin:0;color:#d6d0c8}.empty{padding:100px 0;text-align:center}.footer{border-top:1px solid var(--line);padding:30px 0 50px;color:var(--muted);font-size:14px}.footer a{margin-right:18px}@media(max-width:900px){.hero,.detail-head{grid-template-columns:1fr}.grid{grid-template-columns:repeat(2,minmax(0,1fr))}.hero-aside{border-left:0;padding-left:0}.visual{min-height:280px}.sections{grid-template-columns:1fr}}@media(max-width:620px){.nav-links a:not(.button){display:none}.shell{width:min(100% - 28px,1180px)}.hero{padding-top:48px}.grid{grid-template-columns:1fr}.cover{height:210px}.detail{padding-top:42px}.detail h1{font-size:42px}.cta{align-items:flex-start;flex-direction:column}.button{width:100%}}
"""


def _layout(
    *,
    title: str,
    description: str,
    canonical: str,
    body: str,
    schema: dict[str, Any],
    image: str | None = None,
) -> str:
    image_meta = ""
    if image:
        image_meta = f"""
    <meta property="og:image" content="{_e(image)}">
    <meta name="twitter:image" content="{_e(image)}">"""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_e(title)}</title><meta name="description" content="{_e(description)}">
<meta name="robots" content="index,follow,max-image-preview:large"><link rel="canonical" href="{_e(canonical)}">
<meta property="og:type" content="website"><meta property="og:site_name" content="Manor AI">
<meta property="og:title" content="{_e(title)}"><meta property="og:description" content="{_e(description)}"><meta property="og:url" content="{_e(canonical)}">
<meta name="twitter:card" content="summary_large_image"><meta name="twitter:title" content="{_e(title)}"><meta name="twitter:description" content="{_e(description)}">{image_meta}
<script type="application/ld+json">{_json_ld(schema)}</script><style>{_STYLE}</style></head>
<body><nav class="nav"><a class="brand" href="https://manorai.xyz/">Manor AI</a><div class="nav-links"><a href="/marketplace/">Marketplace</a><a href="https://manorai.xyz/creators">Creators</a><a class="button" href="/login">Sign in</a></div></nav>{body}
<footer class="footer"><div class="shell"><a href="https://manorai.xyz/creators">Create and sell a Workspace</a><a href="https://manorai.xyz/privacy">Privacy</a><a href="https://manorai.xyz/user_agreement">Terms</a><span>&copy; 2026 Manor AI LLC</span></div></footer></body></html>"""


def _html_response(content: str, *, status_code: int = 200) -> HTMLResponse:
    return HTMLResponse(
        content,
        status_code=status_code,
        headers={
            "Cache-Control": "public, max-age=60, s-maxage=300",
            "X-Robots-Tag": (
                "index, follow, max-image-preview:large"
                if status_code == 200
                else "noindex, nofollow"
            ),
        },
    )


def _not_found(kind: str) -> HTMLResponse:
    canonical = f"{_public_origin()}/marketplace/"
    body = f'<main class="shell empty"><p class="kicker">Marketplace</p><h1>{_e(kind)} not found</h1><p>This public listing is unavailable or no longer published.</p><p><a class="button" href="/marketplace/">Browse published Workspaces</a></p></main>'
    return _html_response(
        _layout(
            title=f"{kind} Not Found | Manor AI",
            description="This public Marketplace listing is unavailable.",
            canonical=canonical,
            body=body,
            schema={"@context": "https://schema.org", "@type": "WebPage", "name": f"{kind} not found"},
        ),
        status_code=404,
    )


@page_router.get("", include_in_schema=False)
async def marketplace_no_slash():
    return RedirectResponse("/marketplace/", status_code=308)


@page_router.get("/", response_class=HTMLResponse, include_in_schema=False)
async def public_marketplace_page(db: AsyncSession = Depends(get_db)):
    items = await _serialize_rows(
        db,
        await _published_rows(db, limit=_MAX_CATALOG_ITEMS),
    )
    cards = "".join(_card(item) for item in items)
    if not cards:
        cards = '<div class="panel"><h2>Published Workspaces are coming soon</h2><p>Creators are preparing and testing the first long-running Marketplace Blueprints.</p></div>'
    canonical = f"{_public_origin()}/marketplace/"
    body = f"""
<header class="shell hero"><div><p class="kicker">Workspace Creator Marketplace</p><h1>AI workspaces that keep your business running.</h1><p>Discover long-running AI Workspace Blueprints created by operators, educators, consultants, automation experts, and other creators. Purchase once, install an editable copy, and adapt it to your business.</p></div><aside class="hero-aside"><strong>{len(items):,} published</strong><span>reviewed Workspace Blueprints</span></aside></header>
<main class="shell"><section class="grid" aria-label="Published Workspace Blueprints">{cards}</section></main>"""
    schema = {
        "@context": "https://schema.org",
        "@type": "CollectionPage",
        "name": "Manor Workspace Creator Marketplace",
        "url": canonical,
        "description": "Discover, purchase, and install long-running AI Workspace Blueprints created by experts.",
        "mainEntity": {
            "@type": "ItemList",
            "numberOfItems": len(items),
            "itemListElement": [
                {"@type": "ListItem", "position": index, "url": item.public_url, "name": item.title}
                for index, item in enumerate(items, start=1)
            ],
        },
    }
    return _html_response(_layout(
        title="AI Workspace Marketplace for Business | Manor AI",
        description="Discover and install long-running AI Workspace Blueprints created by experts. Browse editable business systems for agents, workflows, knowledge, schedules, and approvals.",
        canonical=canonical,
        body=body,
        schema=schema,
    ))


@page_router.get("/blueprints/{blueprint_id}", include_in_schema=False)
async def public_blueprint_short_url(
    blueprint_id: str,
    db: AsyncSession = Depends(get_db),
):
    row = await _published_blueprint(db, blueprint_id)
    if row is None:
        return _not_found("Workspace Blueprint")
    return RedirectResponse(_blueprint_path(row), status_code=301)


@page_router.get(
    "/blueprints/{blueprint_id}/{display_slug}",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def public_blueprint_page(
    blueprint_id: str,
    display_slug: str,
    db: AsyncSession = Depends(get_db),
):
    row = await _published_blueprint(db, blueprint_id)
    if row is None:
        return _not_found("Workspace Blueprint")
    item = (await _serialize_rows(db, [row]))[0]
    canonical_path = _blueprint_path(row)
    if display_slug != _slug(row.slug or row.title):
        return RedirectResponse(canonical_path, status_code=301)

    preview = item.setup_preview
    outputs = "".join(f"<li>{_e(value)}</li>" for value in preview.first_week_outputs[:8]) or "<li>Creator-defined first-run outputs are documented inside the Workspace.</li>"
    requirements = [
        *(entry.label for entry in preview.required_integrations),
        *(entry.label for entry in preview.required_channels),
        *(entry.label for entry in preview.required_sessions),
        *(entry.label for entry in preview.required_variables),
    ]
    requirement_items = "".join(f"<li>{_e(value)}</li>" for value in requirements[:10]) or "<li>No additional required connections are listed.</li>"
    image = f'<img src="{_e(item.cover_image_url)}" alt="{_e(item.title)} Workspace Blueprint">' if item.cover_image_url else '<div class="cover-mark" aria-hidden="true">M</div>'
    install_url = f"/blueprints/{quote(item.id, safe='')}"
    canonical = item.public_url
    body = f"""
<main class="shell detail"><div class="breadcrumb"><a href="/marketplace/">Marketplace</a> / {_e(item.title)}</div>
<div class="detail-head"><div><p class="kicker">Long-running AI Workspace Blueprint</p><h1>{_e(item.title)}</h1><p class="lead">{_e(item.summary or 'A reusable AI business workspace with agents, knowledge, workflows, schedules, and approvals.')}</p><p class="creator-line">Created by <a href="{_e(urlsplit(item.creator_url).path)}">{_e(item.author_display_name)}</a></p><div class="tags">{''.join(f'<span>{_e(tag)}</span>' for tag in item.tags)}</div><p class="price">{_e(_price(item))}</p><p>{_e(_stats(item))}</p><a class="button" href="{_e(install_url)}">Sign in to purchase or install &rarr;</a></div><div class="visual">{image}</div></div>
<div class="sections"><section class="panel"><h2>What this Workspace does</h2><p>{_e(item.description or item.summary or 'The creator has packaged a reusable operating method as an editable Workspace Blueprint.')}</p></section><section class="panel"><h2>First-run outputs</h2><ul>{outputs}</ul></section><section class="panel"><h2>Required setup</h2><ul>{requirement_items}</ul></section><section class="panel"><h2>How installation works</h2><p>Purchase or install this Blueprint in Manor Cloud. Manor creates an editable copy in your account; it does not give you access to the creator's private Workspace, data, or credentials.</p></section></div>
<section class="cta"><div><h2>Want to sell your own Workspace?</h2><p>Turn a tested operating method into an installable Blueprint, publish it, set a price, and earn when it sells.</p></div><a class="button" href="https://manorai.xyz/creators">Become a Workspace creator</a></section></main>"""
    offer = {
        "@type": "Offer",
        "url": canonical,
        "priceCurrency": item.currency.upper(),
        "price": f"{item.price_cents / 100:.2f}",
        "availability": "https://schema.org/InStock",
    }
    schema = {
        "@context": "https://schema.org",
        "@type": "SoftwareApplication",
        "name": item.title,
        "description": item.summary or item.description,
        "url": canonical,
        "applicationCategory": "BusinessApplication",
        "operatingSystem": "Web",
        "author": {"@type": "Person", "name": item.author_display_name, "url": item.creator_url},
        "offers": offer,
        "keywords": item.tags,
        "datePublished": item.published_at.isoformat() if item.published_at else None,
        "interactionStatistic": {
            "@type": "InteractionCounter",
            "interactionType": "https://schema.org/UseAction",
            "userInteractionCount": item.install_count,
        },
    }
    if item.cover_image_url:
        schema["image"] = item.cover_image_url
    return _html_response(_layout(
        title=f"{item.title}: AI Workspace Blueprint | Manor AI",
        description=(item.summary or item.description or f"Install {item.title}, a long-running AI Workspace Blueprint by {item.author_display_name}.")[:160],
        canonical=canonical,
        body=body,
        schema=schema,
        image=item.cover_image_url,
    ))


@page_router.get(
    "/creators/{creator_id}",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def public_creator_page(
    creator_id: str,
    db: AsyncSession = Depends(get_db),
):
    items = await _creator_blueprints(db, creator_id)
    if not items:
        return _not_found("Creator")
    creator = _creator_from_blueprints(creator_id, items)
    canonical = creator.public_url
    cards = "".join(_card(item) for item in items)
    body = f"""
<header class="shell hero"><div><p class="kicker">Workspace Creator</p><h1>{_e(creator.display_name)}'s AI Workspace Blueprints</h1><p>Explore published AI Workspace Blueprints by {_e(creator.display_name)}. Install an editable copy and keep the creator's operating method running in your own Manor account.</p></div><aside class="hero-aside"><strong>{creator.blueprint_count:,} Blueprints</strong><span>{creator.total_installs:,} installs · {creator.total_purchases:,} purchases</span></aside></header>
<main class="shell"><section class="grid" aria-label="Workspaces by {_e(creator.display_name)}">{cards}</section><section class="cta"><div><h2>Publish your own long-running Workspace products</h2><p>Creators can package expertise, publish paid Blueprints, and earn seller proceeds from Marketplace purchases.</p></div><a class="button" href="https://manorai.xyz/creators">Creator Marketplace guide</a></section></main>"""
    schema = {
        "@context": "https://schema.org",
        "@type": "ProfilePage",
        "name": f"{creator.display_name} — Manor Workspace Creator",
        "url": canonical,
        "mainEntity": {
            "@type": "Person",
            "name": creator.display_name,
            "url": canonical,
        },
        "hasPart": [
            {"@type": "SoftwareApplication", "name": item.title, "url": item.public_url}
            for item in items
        ],
    }
    if creator.avatar_url:
        schema["mainEntity"]["image"] = creator.avatar_url
    return _html_response(_layout(
        title=f"{creator.display_name}: AI Workspace Creator | Manor AI",
        description=f"Browse {creator.blueprint_count} published AI Workspace Blueprints by {creator.display_name}. Install editable business systems for long-running work.",
        canonical=canonical,
        body=body,
        schema=schema,
        image=creator.avatar_url,
    ))


@page_router.get(
    "/assets/{blueprint_id}/{asset_id}",
    response_class=FileResponse,
    include_in_schema=False,
)
async def public_marketplace_asset(
    blueprint_id: str,
    asset_id: str,
    db: AsyncSession = Depends(get_db),
):
    row = await _published_blueprint(db, blueprint_id)
    if row is None or row.entity_id is None:
        return Response(status_code=404, headers={"X-Robots-Tag": "noindex"})
    asset = next(
        (item for item in _showcase_assets(row.showcase_assets) if item.id == asset_id),
        None,
    )
    if asset is None:
        return Response(status_code=404, headers={"X-Robots-Tag": "noindex"})
    prefix = f"/api/v1/fs/{row.entity_id}/"
    if not asset.url.startswith(prefix):
        return Response(status_code=404, headers={"X-Robots-Tag": "noindex"})
    relative_path = asset.url.removeprefix(prefix)
    expected_prefix = f"marketplace/blueprints/{row.id}/"
    if not relative_path.startswith(expected_prefix):
        return Response(status_code=404, headers={"X-Robots-Tag": "noindex"})
    full_path = resolve_path(row.entity_id, relative_path)
    if not full_path or not Path(full_path).is_file():
        return Response(status_code=404, headers={"X-Robots-Tag": "noindex"})
    return FileResponse(
        full_path,
        media_type=asset.mime_type or "application/octet-stream",
        headers={
            "Cache-Control": "public, max-age=86400, immutable",
            "X-Content-Type-Options": "nosniff",
        },
    )


@page_router.get("/sitemap.xml", include_in_schema=False)
async def public_marketplace_sitemap(db: AsyncSession = Depends(get_db)):
    rows = await _published_rows(db)
    origin = _public_origin()
    creator_ids = sorted({_creator_id(row) for row in rows})
    urls: list[tuple[str, datetime | None]] = [(f"{origin}/marketplace/", None)]
    urls.extend((f"{origin}{_blueprint_path(row)}", row.updated_at or row.published_at) for row in rows)
    latest_by_creator: dict[str, datetime | None] = {}
    for row in rows:
        creator_id = _creator_id(row)
        candidate = row.updated_at or row.published_at
        current = latest_by_creator.get(creator_id)
        if candidate is not None and (current is None or candidate > current):
            latest_by_creator[creator_id] = candidate
    urls.extend(
        (f"{origin}/marketplace/creators/{quote(creator_id, safe='')}", latest_by_creator.get(creator_id))
        for creator_id in creator_ids
    )
    entries = []
    for location, modified in urls:
        lastmod = f"<lastmod>{modified.date().isoformat()}</lastmod>" if modified else ""
        entries.append(f"<url><loc>{_e(location)}</loc>{lastmod}</url>")
    xml = '<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">' + "".join(entries) + "</urlset>"
    return Response(
        xml,
        media_type="application/xml",
        headers={
            "Cache-Control": "public, max-age=300, s-maxage=900",
            "X-Robots-Tag": "noindex, follow",
        },
    )
