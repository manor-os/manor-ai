"""Static-site publish pipeline.

Publishable: any folder whose root contains ``index.html``, or a single
``.html`` file — judged by content, never by folder name or a marker
attribute. The published snapshot contains whitelisted static files only;
exclusions are reported, never silent; a page that references an excluded or
missing file fails the publish (spec: keep the live site complete, keep
sources and secrets off the public internet).
"""
from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import TYPE_CHECKING, Iterable

from packages.core.services.entity_fs import get_entity_root

if TYPE_CHECKING:
    from packages.core.models import Site

MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_TOTAL_BYTES = 20 * 1024 * 1024
MAX_FILES = 200

ALLOWED_EXTENSIONS = frozenset({
    ".html", ".htm", ".css", ".js", ".mjs", ".map", ".json", ".webmanifest",
    ".txt", ".xml", ".svg", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif",
    ".ico", ".woff", ".woff2", ".ttf", ".otf", ".eot",
    ".mp3", ".mp4", ".webm", ".pdf",
})
_SENSITIVE_RE = re.compile(r"(^\.env(\..*)?$|\.pem$|\.key$|credential|secret)", re.I)
_JUNK_BASENAMES = frozenset({".DS_Store", "Thumbs.db"})

_SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


class SitePublishError(ValueError):
    """Publish rejected — message is agent-readable and actionable."""


@dataclass
class PublishTarget:
    kind: str      # "folder" | "file"
    root_rel: str  # entity-relative path of the folder (or the single file)
    entry: str     # entry file relative to root


@dataclass
class SiteBridgeFeatures:
    """Declarative Manor capabilities discovered in publishable HTML."""

    lead_forms: int = 0
    subscription_forms: int = 0
    tracked_interactions: int = 0

    def as_dict(self) -> dict[str, int | bool]:
        return {
            "customer_service": True,
            "lead_forms": self.lead_forms,
            "subscription_forms": self.subscription_forms,
            "tracked_interactions": self.tracked_interactions,
            "analytics": True,
        }


@dataclass
class SnapshotFile:
    rel: str
    size: int
    sensitive: bool = False


@dataclass
class Snapshot:
    included: list[SnapshotFile] = field(default_factory=list)
    excluded: list[SnapshotFile] = field(default_factory=list)


def _entity_abs(entity_id: str, rel: str) -> str:
    root = os.path.realpath(get_entity_root(entity_id))
    full = os.path.realpath(os.path.join(root, rel.strip("/")))
    if not (full == root or full.startswith(root + os.sep)):
        raise SitePublishError(f"path escapes entity filesystem: {rel!r}")
    return full


def resolve_publish_target(entity_id: str, rel_path: str) -> PublishTarget | None:
    """Judge publishability by content: folder with root index.html, or .html file."""
    rel = (rel_path or "").strip().strip("/")
    if not rel:
        return None
    full = _entity_abs(entity_id, rel)
    if os.path.isdir(full):
        if os.path.isfile(os.path.join(full, "index.html")):
            return PublishTarget(kind="folder", root_rel=rel, entry="index.html")
        return None
    if not os.path.isfile(full) or not rel.lower().endswith((".html", ".htm")):
        return None
    parent_rel = os.path.dirname(rel)
    if parent_rel and os.path.isfile(os.path.join(os.path.dirname(full), "index.html")):
        return PublishTarget(kind="folder", root_rel=parent_rel, entry="index.html")
    return PublishTarget(kind="file", root_rel=rel, entry="index.html")


def collect_snapshot(root_abs: str) -> Snapshot:
    snap = Snapshot()
    total = 0
    count = 0
    for dirpath, dirnames, filenames in os.walk(root_abs):
        for d in list(dirnames):
            if os.path.islink(os.path.join(dirpath, d)):
                raise SitePublishError(
                    f"symlink not allowed: {os.path.relpath(os.path.join(dirpath, d), root_abs)}"
                )
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for name in filenames:
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root_abs)
            if os.path.islink(full):
                raise SitePublishError(f"symlink not allowed: {rel}")
            if name.startswith(".") or name in _JUNK_BASENAMES:
                if _SENSITIVE_RE.search(name):
                    snap.excluded.append(SnapshotFile(rel=rel, size=0, sensitive=True))
                continue
            size = os.path.getsize(full)
            if _SENSITIVE_RE.search(name):
                snap.excluded.append(SnapshotFile(rel=rel, size=size, sensitive=True))
                continue
            ext = os.path.splitext(name)[1].lower()
            if ext not in ALLOWED_EXTENSIONS:
                snap.excluded.append(SnapshotFile(rel=rel, size=size))
                continue
            if size > MAX_FILE_BYTES:
                raise SitePublishError(
                    f"file too large ({size} bytes > {MAX_FILE_BYTES} limit): {rel}"
                )
            count += 1
            total += size
            if count > MAX_FILES:
                raise SitePublishError(f"file count exceeds limit of {MAX_FILES}")
            if total > MAX_TOTAL_BYTES:
                raise SitePublishError(f"total size exceeds limit of {MAX_TOTAL_BYTES} bytes")
            snap.included.append(SnapshotFile(rel=rel, size=size))
    return snap


def generate_site_slug(entity_id: str) -> str:
    """System-generated subdomain label: entity id + random suffix.

    Slugs are deliberately NOT user-chosen (no vanity-name collisions, no
    reserved-word policing, no squatting). The platform subdomain is a
    machine address; the human-facing address is the user's own custom
    domain. Entity participation in the label keeps it traceable in logs.
    """
    import secrets

    base = re.sub(r"[^a-z0-9]", "", (entity_id or "").lower())[:20] or "site"
    return f"{base}-{secrets.token_hex(4)}"


_EXTERNAL_PREFIXES = (
    "http://", "https://", "//", "mailto:", "tel:", "data:", "blob:",
)
_PLACEHOLDER_HREFS = frozenset({"", "#", "javascript:", "javascript:void(0)", "javascript:void(0);"})
_MANOR_FORM_ACTIONS = frozenset({"lead", "subscription"})
_CSS_REF_RE = re.compile(
    r"url\(\s*['\"]?([^'\")]+)|@import\s+(?:url\(\s*)?['\"]([^'\"]+)",
    re.I,
)
_CSS_QUOTED_DATA_URL_RE = re.compile(
    r"url\(\s*(?P<quote>['\"])data:.*?(?P=quote)\s*\)",
    re.I | re.S,
)
_JS_CALL_REF_RE = re.compile(r"\b(?:fetch|import)\s*\(\s*['\"]([^'\"]+)['\"]", re.I)
_JS_IMPORT_REF_RE = re.compile(
    r"\bimport\s+(?:[^;\n]*?\s+from\s+)?['\"]([^'\"]+)['\"]",
    re.I,
)


@dataclass
class _HtmlControl:
    tag: str
    attrs: dict[str, str]
    line: int
    form_index: int | None = None


class _SiteHTMLParser(HTMLParser):
    """Collect browser references and controls without executing the page."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.refs: list[tuple[str, str, int]] = []
        self.anchors: list[_HtmlControl] = []
        self.buttons: list[_HtmlControl] = []
        self.forms: list[_HtmlControl] = []
        self.targets: set[str] = set()
        self.inline_scripts: list[str] = []
        self.inline_styles: list[str] = []
        self.tracked_interactions = 0
        self._form_stack: list[int] = []
        self._capture_script = False
        self._capture_style = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._handle_tag(tag, attrs)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._handle_tag(tag, attrs)

    def _handle_tag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        values = {key.lower(): value or "" for key, value in attrs}
        if values.get("data-manor-event"):
            self.tracked_interactions += 1
        line = self.getpos()[0]
        for target_attr in ("id", "name"):
            if values.get(target_attr):
                self.targets.add(values[target_attr])
        for attr in ("href", "src", "poster", "data", "action", "formaction"):
            if attr in values:
                self.refs.append((attr, values[attr], line))
        if "srcset" in values:
            for ref in _srcset_refs(values["srcset"]):
                self.refs.append(("srcset", ref, line))
        if "style" in values:
            self.inline_styles.append(values["style"])
        if tag == "a":
            self.anchors.append(_HtmlControl(tag, values, line))
        elif tag == "form":
            self.forms.append(_HtmlControl(tag, values, line))
            self._form_stack.append(len(self.forms) - 1)
        elif tag == "button":
            form_index = self._form_stack[-1] if self._form_stack else None
            self.buttons.append(_HtmlControl(tag, values, line, form_index))
        if tag == "script" and "src" not in values:
            self._capture_script = True
        elif tag == "style":
            self._capture_style = True

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "form" and self._form_stack:
            self._form_stack.pop()
        elif tag == "script":
            self._capture_script = False
        elif tag == "style":
            self._capture_style = False

    def handle_data(self, data: str) -> None:
        if self._capture_script:
            self.inline_scripts.append(data)
        elif self._capture_style:
            self.inline_styles.append(data)


def _srcset_refs(value: str) -> Iterable[str]:
    if value.lstrip().lower().startswith("data:"):
        return ()
    return tuple(part.strip().split()[0] for part in value.split(",") if part.strip())


def inspect_site_bridge(entity_id: str, target: PublishTarget) -> SiteBridgeFeatures:
    """Inspect publishable HTML without executing it.

    The result powers the pre-publish confirmation and determines which
    Workspace resources need to be provisioned. Only explicit
    ``data-manor-*`` declarations are treated as application actions.
    """
    source_abs = _entity_abs(entity_id, target.root_rel)
    sources: list[str]
    if target.kind == "folder":
        snapshot = collect_snapshot(source_abs)
        sources = [
            os.path.join(source_abs, item.rel)
            for item in snapshot.included
            if item.rel.lower().endswith((".html", ".htm"))
        ]
    else:
        sources = [source_abs]

    features = SiteBridgeFeatures()
    for source in sources:
        with open(source, encoding="utf-8", errors="replace") as html_file:
            parser = _SiteHTMLParser()
            parser.feed(html_file.read())
        for form in parser.forms:
            action = form.attrs.get("data-manor-action", "").strip().lower()
            if action == "lead":
                features.lead_forms += 1
            elif action == "subscription":
                features.subscription_forms += 1
        features.tracked_interactions += parser.tracked_interactions
    return features


def _css_refs(value: str) -> Iterable[str]:
    sanitized = _CSS_QUOTED_DATA_URL_RE.sub("", value)
    for match in _CSS_REF_RE.finditer(sanitized):
        ref = (match.group(1) or match.group(2) or "").strip()
        if ref:
            yield ref


def _script_refs(value: str) -> Iterable[str]:
    for pattern in (_JS_CALL_REF_RE, _JS_IMPORT_REF_RE):
        for match in pattern.finditer(value):
            yield match.group(1).strip()


def _control_has_script_hook(control: _HtmlControl, script_source: str) -> bool:
    attrs = control.attrs
    if attrs.get("data-manor-event"):
        return True
    if any(attrs.get(name) for name in ("onclick", "onchange", "oninput", "onpointerdown")):
        return True
    if attrs.get("popovertarget") or attrs.get("commandfor"):
        return True
    control_id = attrs.get("id", "")
    if control_id and (
        f"#{control_id}" in script_source
        or re.search(rf"getElementById\(\s*['\"]{re.escape(control_id)}['\"]", script_source)
        or f"'{control_id}'" in script_source
        or f'"{control_id}"' in script_source
    ):
        return True
    for class_name in attrs.get("class", "").split():
        if f".{class_name}" in script_source:
            return True
    for name, value in attrs.items():
        if not name.startswith("data-"):
            continue
        dataset_name = re.sub(r"-([a-z])", lambda m: m.group(1).upper(), name[5:])
        if f"[{name}" in script_source or f"dataset.{dataset_name}" in script_source:
            return True
        if value and (f"'{value}'" in script_source or f'"{value}"' in script_source):
            return True
    return False


def _add_reference_error(
    *,
    source_rel: str,
    ref: str,
    line: int | None,
    included: set[str],
    excluded: set[str],
    errors: list[str],
) -> None:
    value = ref.strip()
    location = f"{source_rel}:{line}" if line else source_rel
    lower = value.lower()
    if not value or lower.startswith(_EXTERNAL_PREFIXES):
        return
    if lower.startswith("javascript:"):
        errors.append(f"{location}: uses JavaScript URL {value!r}; use a real link or button handler")
        return
    if value.startswith("#"):
        return
    if lower.startswith("/api/"):
        errors.append(
            f"{location}: references platform-internal path {value!r}; use a bundled relative path instead"
        )
        return
    path_only = value.split("#", 1)[0].split("?", 1)[0]
    if not path_only:
        return
    base = "" if path_only.startswith("/") else os.path.dirname(source_rel)
    resolved = os.path.normpath(os.path.join(base, path_only.lstrip("/")))
    if resolved.startswith(".."):
        errors.append(f"{location}: reference escapes site root: {value!r}")
        return
    candidates = (resolved, os.path.join(resolved, "index.html"))
    if any(candidate in included for candidate in candidates):
        return
    if any(candidate in excluded for candidate in candidates):
        errors.append(
            f"{location}: references {value!r} which is excluded from publishing "
            f"(not a browser-ready static asset)"
        )
    else:
        errors.append(f"{location}: broken internal reference {value!r} (file not found)")


def check_links(
    root_abs: str,
    snap: Snapshot,
    *,
    source_overrides: dict[str, str] | None = None,
) -> list[str]:
    """Validate static references, fragment links, forms, and detectable controls."""
    included = {f.rel for f in snap.included}
    excluded = {f.rel for f in snap.excluded}
    errors: list[str] = []
    inspections: dict[str, _SiteHTMLParser] = {}
    script_chunks: list[str] = []
    source_overrides = source_overrides or {}

    for f in snap.included:
        source_path = source_overrides.get(f.rel, os.path.join(root_abs, f.rel))
        suffix = os.path.splitext(f.rel)[1].lower()
        if suffix not in {".html", ".htm", ".css", ".js", ".mjs"}:
            continue
        with open(source_path, encoding="utf-8", errors="replace") as fh:
            content = fh.read()
        if suffix in {".js", ".mjs"}:
            script_chunks.append(content)
            for ref in _script_refs(content):
                _add_reference_error(
                    source_rel=f.rel, ref=ref, line=None, included=included,
                    excluded=excluded, errors=errors,
                )
        elif suffix == ".css":
            for ref in _css_refs(content):
                _add_reference_error(
                    source_rel=f.rel, ref=ref, line=None, included=included,
                    excluded=excluded, errors=errors,
                )
        else:
            parser = _SiteHTMLParser()
            parser.feed(content)
            inspections[f.rel] = parser
            script_chunks.extend(parser.inline_scripts)
            for attr, ref, line in parser.refs:
                _add_reference_error(
                    source_rel=f.rel, ref=ref, line=line, included=included,
                    excluded=excluded, errors=errors,
                )
            for style in parser.inline_styles:
                for ref in _css_refs(style):
                    _add_reference_error(
                        source_rel=f.rel, ref=ref, line=None, included=included,
                        excluded=excluded, errors=errors,
                    )

    script_source = "\n".join(script_chunks)
    for source_rel, parser in inspections.items():
        for anchor in parser.anchors:
            href = anchor.attrs.get("href")
            normalized = (href or "").strip().lower()
            if href is None or normalized in _PLACEHOLDER_HREFS:
                errors.append(
                    f"{source_rel}:{anchor.line}: anchor has no real destination; "
                    f"use a valid href or a button with a handler"
                )
                continue
            if href.startswith("#") and href[1:] not in parser.targets:
                errors.append(
                    f"{source_rel}:{anchor.line}: fragment target {href!r} does not exist"
                )

        form_valid: dict[int, bool] = {}
        for index, form in enumerate(parser.forms):
            action = form.attrs.get("action", "").strip()
            manor_action = form.attrs.get("data-manor-action", "").strip().lower()
            valid = (
                bool(action and action != "#")
                or manor_action in _MANOR_FORM_ACTIONS
                or _control_has_script_hook(form, script_source)
            )
            form_valid[index] = valid
            if not valid:
                errors.append(
                    f"{source_rel}:{form.line}: form has no action or detectable submit handler"
                )

        for button in parser.buttons:
            if "disabled" in button.attrs:
                continue
            button_type = button.attrs.get("type", "submit" if button.form_index is not None else "button")
            native_form_control = button.form_index is not None and button_type in {"submit", "reset"}
            if native_form_control and form_valid.get(button.form_index, False):
                continue
            if _control_has_script_hook(button, script_source):
                continue
            errors.append(
                f"{source_rel}:{button.line}: button has no detectable behavior or event hook"
            )
    return errors


KEEP_REVISIONS = 3


@dataclass
class PublishResult:
    site: "Site"
    included: list[SnapshotFile]
    excluded: list[SnapshotFile]


async def _unique_slug(session, base: str) -> str:
    from sqlalchemy import select

    from packages.core.models import Site

    slug = base
    n = 2
    while True:
        existing = await session.scalar(select(Site.id).where(Site.slug == slug))
        if existing is None:
            return slug
        slug = f"{base[:60]}-{n}"
        n += 1


async def publish(
    session,
    *,
    entity_id: str,
    rel_path: str,
    name: str,
    workspace_id: str | None = None,
    connections: dict | None = None,
) -> PublishResult:
    """Snapshot-publish a folder or single HTML file. Creates or updates the
    Site row for (entity_id, source_path); each publish bumps the revision and
    prunes snapshots older than the last KEEP_REVISIONS. The subdomain slug is
    system-generated (entity id + random) and never user-chosen."""
    from datetime import datetime, timezone

    from sqlalchemy import select

    from packages.core.models import Site

    target = resolve_publish_target(entity_id, rel_path)
    if target is None:
        raise SitePublishError(
            f"{rel_path!r} is not publishable: publish a folder whose root contains "
            f"index.html, or a single .html file"
        )

    entity_root = get_entity_root(entity_id)
    src_abs = os.path.join(entity_root, target.root_rel)
    if target.kind == "folder":
        snap = collect_snapshot(src_abs)
        if not any(f.rel == "index.html" for f in snap.included):
            raise SitePublishError("index.html missing from folder root")
        errors = check_links(src_abs, snap)
        if errors:
            raise SitePublishError("link check failed:\n" + "\n".join(errors[:20]))
    else:
        size = os.path.getsize(src_abs)
        if size > MAX_FILE_BYTES:
            raise SitePublishError(f"file too large ({size} bytes > {MAX_FILE_BYTES} limit)")
        snap = Snapshot(included=[SnapshotFile(rel="index.html", size=size)])
        errors = check_links(
            os.path.dirname(src_abs),
            snap,
            source_overrides={"index.html": src_abs},
        )
        if errors:
            raise SitePublishError("link check failed:\n" + "\n".join(errors[:20]))

    site = await session.scalar(
        select(Site).where(Site.entity_id == entity_id, Site.source_path == target.root_rel)
    )
    if site is None:
        chosen = await _unique_slug(session, generate_site_slug(entity_id))
        site = Site(
            entity_id=entity_id, name=name, slug=chosen,
            source_path=target.root_rel, entry=target.entry,
        )
        session.add(site)
        await session.flush()

    next_rev = site.revision + 1
    site_dir = os.path.join(entity_root, ".sites", site.id)
    rev_dir = os.path.join(site_dir, f"rev{next_rev}")
    tmp_dir = rev_dir + ".tmp"
    shutil.rmtree(tmp_dir, ignore_errors=True)
    shutil.rmtree(rev_dir, ignore_errors=True)
    os.makedirs(tmp_dir, exist_ok=True)
    if target.kind == "folder":
        for f in snap.included:
            dst = os.path.join(tmp_dir, f.rel)
            os.makedirs(os.path.dirname(dst) or tmp_dir, exist_ok=True)
            shutil.copy2(os.path.join(src_abs, f.rel), dst)
    else:
        shutil.copy2(src_abs, os.path.join(tmp_dir, "index.html"))
    os.replace(tmp_dir, rev_dir)

    site.revision = next_rev
    site.status = "active"
    site.published_at = datetime.now(timezone.utc)
    if connections is not None:
        site.workspace_id = workspace_id
        site.connections = dict(connections)
    await session.commit()
    await session.refresh(site)

    for old in os.listdir(site_dir):
        if not old.startswith("rev") or old == f"rev{next_rev}":
            continue
        try:
            old_n = int(old[3:])
        except ValueError:
            continue
        if old_n <= next_rev - KEEP_REVISIONS:
            shutil.rmtree(os.path.join(site_dir, old), ignore_errors=True)

    return PublishResult(site=site, included=snap.included, excluded=snap.excluded)


def site_serving_root(entity_id: str, site) -> str:
    """Absolute path of the currently-live snapshot for a site."""
    return os.path.join(get_entity_root(entity_id), ".sites", site.id, f"rev{site.revision}")


async def _resolve_ips(host: str) -> set[str]:
    import asyncio
    import socket

    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
        return {info[4][0] for info in infos}
    except socket.gaierror:
        return set()


async def dns_points_at_us(domain: str, expected_target: str) -> bool:
    """True if `domain` resolves to the same IPs as the sites domain, or is
    Cloudflare-proxied (orange cloud + SSL Full also reaches our origin)."""
    import asyncio

    if not domain or not expected_target:
        return False
    ours, theirs = await asyncio.gather(
        _resolve_ips(expected_target), _resolve_ips(domain)
    )
    if theirs and ours & theirs:
        return True
    if theirs:
        from packages.core.services.cloudflare_ips import is_cloudflare_ip

        return all(is_cloudflare_ip(ip) for ip in theirs)
    return False
