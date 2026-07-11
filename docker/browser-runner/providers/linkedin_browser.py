"""LinkedIn (browser) — Playwright provider for the browser-runner sidecar.

Driven from packages/core/ai/mcp/linkedin_browser.py. The runner
imports this module at request time and calls ``perform(page, action,
params)`` with a fresh authenticated context.

Selectors here target LinkedIn's web app as of April 2026. LinkedIn
ships A/B test cohorts continuously and ships full layout changes 1-2x
per year, so every selector is wrapped in fallbacks. When all fallbacks
miss, the provider returns a structured ``error`` field rather than
crashing — the wrapper surfaces that to the agent which can retry
later.

Defensive moves:
  * ``_ensure_logged_in``    — bail early with a clear message if the
                               session is unauthenticated
  * multi-selector fallbacks — try a list of selectors before giving up
  * scroll-to-load          — LinkedIn lazy-loads everything; we scroll
                               the search results list before reading
  * polite delays           — a small ``asyncio.sleep`` between scrolls
                               and clicks to keep load below the
                               (very low) automation-detection threshold
"""
from __future__ import annotations

import asyncio
import re
from typing import Any, Dict, List, Optional
from urllib.parse import quote_plus, urlparse


_BASE = "https://www.linkedin.com"


# Skip the playwright-stealth pass for this provider. LinkedIn detects
# the stealth patches (navigator.webdriver shim, plugin spoof, languages,
# WebGL fingerprint) and renders pages blank — verified end-to-end:
# every tool returns "did not render" with stealth ON, all tools work
# with stealth OFF. The runner (docker/browser-runner/runner.py)
# checks this attribute before calling _apply_stealth.
USE_STEALTH = False


# ── Dispatch ────────────────────────────────────────────────────────────────

async def perform(page, action: str, params: Dict[str, Any]) -> Dict[str, Any]:
    if action == "search_people":
        return await _search_people(page, params)
    if action == "view_profile":
        return await _view_profile(page, params)
    if action == "search_companies":
        return await _search_companies(page, params)
    if action == "view_company":
        return await _view_company(page, params)
    # Jobs (PR3)
    if action == "search_jobs":
        return await _search_jobs(page, params)
    if action == "view_job":
        return await _view_job(page, params)
    if action == "easy_apply":
        return await _easy_apply(page, params)
    if action == "list_my_applications":
        return await _list_my_applications(page, params)
    # Messaging (PR4)
    if action == "list_conversations":
        return await _list_conversations(page, params)
    if action == "view_conversation":
        return await _view_conversation(page, params)
    if action == "send_message":
        return await _send_message(page, params)
    if action == "send_invitation":
        return await _send_invitation(page, params)
    # Feed + posts (PR4)
    if action == "browse_feed":
        return await _browse_feed(page, params)
    if action == "search_posts":
        return await _search_posts(page, params)
    if action == "view_post":
        return await _view_post(page, params)
    return {"error": f"unknown linkedin_browser action: {action!r}"}


# ── search_people ───────────────────────────────────────────────────────────

async def _search_people(page, params: Dict[str, Any]) -> Dict[str, Any]:
    keywords = (params.get("keywords") or "").strip()
    if not keywords:
        return {"error": "keywords required"}
    count = max(1, min(25, int(params.get("count") or 10)))

    url = f"{_BASE}/search/results/people/?keywords={quote_plus(keywords)}"
    await page.goto(url, wait_until="domcontentloaded")
    await _ensure_logged_in(page)

    # Wait on the LINK selector, not container class names. LinkedIn
    # ships content-hashed CSS classes (e.g. `_20be700a _893ca234`)
    # that change per build, so container-class matching breaks every
    # rollout. The link selectors are stable. Scroll once to coax
    # lazy-load.
    try:
        await page.wait_for_selector("a[href*='/in/']", timeout=15_000)
    except Exception:
        return {"error": "search results did not render — no profile links found"}
    await _scroll(page, distance=600, times=3, pause=0.7)

    # Pull the result rows. We extract via JS for resilience: read all
    # links to /in/ profiles and the surrounding card text.
    raw_items = await page.evaluate(
        """(maxCount) => {
            const out = [];
            const seen = new Set();
            const links = document.querySelectorAll("a[href*='/in/']");
            for (const a of links) {
                if (out.length >= maxCount) break;
                const href = a.href.split('?')[0];
                if (!/\\/in\\/[^/]+\\/?$/.test(href)) continue;
                if (seen.has(href)) continue;
                seen.add(href);

                // Walk up to the result card.
                let card = a.closest('li') || a.closest('div.entity-result') || a.closest('div');
                if (!card) continue;
                const cardText = (card.innerText || '').trim();
                if (!cardText) continue;

                // The card text typically reads:
                //   {name}\\n{headline}\\n{location}\\n...
                const lines = cardText.split('\\n').map(s => s.trim()).filter(Boolean);
                // Skip "Status is online", connection-degree chips, etc.
                const noise = /^(Status is|·|Member|LinkedIn Member|\\d+(st|nd|rd|th) degree connection|View .*'s profile)$/i;
                const cleaned = lines.filter(l => !noise.test(l));

                out.push({
                    profile_url: href,
                    raw_lines: cleaned.slice(0, 6),
                });
            }
            return out;
        }""",
        count,
    )

    items: List[Dict[str, Any]] = []
    for it in raw_items:
        lines = it.get("raw_lines") or []
        items.append({
            "profile_url": it.get("profile_url"),
            "name": lines[0] if lines else None,
            "headline": lines[1] if len(lines) > 1 else None,
            "location": lines[2] if len(lines) > 2 else None,
        })

    return {
        "count": len(items),
        "query": keywords,
        "results": items,
    }


# ── view_profile ────────────────────────────────────────────────────────────

async def _view_profile(page, params: Dict[str, Any]) -> Dict[str, Any]:
    target = (params.get("profile") or "").strip()
    if not target:
        return {"error": "profile required"}

    url = _normalize_profile_url(target)
    if not url:
        return {"error": f"could not resolve profile target: {target!r}"}

    await page.goto(url, wait_until="domcontentloaded")
    await _ensure_logged_in(page)

    # Profile page lazy-loads sections. Wait for the header h1, then
    # scroll once to coax About / Experience into the DOM.
    try:
        await page.wait_for_selector("h1", timeout=10_000)
    except Exception:
        return {"error": "profile did not load — bad handle, blocked, or session expired"}
    await _scroll(page, distance=900, times=2, pause=0.6)

    # Extract through evaluate() so we can use a single batch read.
    data = await page.evaluate(
        """() => {
            const text = (sel) => {
                const el = document.querySelector(sel);
                return el ? (el.textContent || '').trim() : null;
            };

            const sectionByHeader = (label) => {
                const h2s = Array.from(document.querySelectorAll('h2, h3'));
                const h = h2s.find(el => (el.textContent || '').trim().toLowerCase().startsWith(label.toLowerCase()));
                if (!h) return null;
                const sec = h.closest('section') || h.parentElement;
                return sec ? (sec.innerText || '').trim() : null;
            };

            // Top card — name lives in h1. Headline is the next prominent
            // text-body-medium block. Location is text-body-small.
            const name = text('h1');
            const headline = text('.text-body-medium.break-words')
                          || text('div.pv-text-details__left-panel .text-body-medium');
            const location = text('.text-body-small.inline.t-black--light.break-words')
                          || text('span.text-body-small.inline.t-black--light');

            // About section — usually <section> with <h2>About</h2>.
            let about = sectionByHeader('About');
            if (about) {
                // strip the leading "About" header echo
                about = about.replace(/^About\\s*/i, '').trim();
            }

            // Experience: pull <li> roles inside the Experience section.
            const expLis = (() => {
                const h2s = Array.from(document.querySelectorAll('h2, h3'));
                const h = h2s.find(el => (el.textContent || '').trim().toLowerCase().startsWith('experience'));
                if (!h) return [];
                const sec = h.closest('section');
                if (!sec) return [];
                return Array.from(sec.querySelectorAll('li'));
            })();
            const experience = expLis.slice(0, 8).map(li => {
                const lines = (li.innerText || '').split('\\n').map(s => s.trim()).filter(Boolean);
                // Dedupe consecutive duplicates LinkedIn emits for a11y.
                const seen = new Set();
                const dedup = [];
                for (const l of lines) {
                    if (seen.has(l)) continue;
                    seen.add(l);
                    dedup.push(l);
                }
                return {
                    title: dedup[0] || null,
                    company: dedup[1] || null,
                    duration: dedup[2] || null,
                    location: dedup[3] || null,
                };
            });

            return { name, headline, location, about, experience };
        }"""
    )

    about = data.get("about") or None
    if about and len(about) > 4000:
        about = about[:4000] + "…"

    return {
        "url": url,
        "name": data.get("name"),
        "headline": data.get("headline"),
        "location": data.get("location"),
        "about": about,
        "current_position": (
            data.get("experience")[0] if data.get("experience") else None
        ),
        "experience": data.get("experience") or [],
    }


# ── search_companies ────────────────────────────────────────────────────────

async def _search_companies(page, params: Dict[str, Any]) -> Dict[str, Any]:
    keywords = (params.get("keywords") or "").strip()
    if not keywords:
        return {"error": "keywords required"}
    count = max(1, min(25, int(params.get("count") or 10)))

    url = f"{_BASE}/search/results/companies/?keywords={quote_plus(keywords)}"
    await page.goto(url, wait_until="domcontentloaded")
    await _ensure_logged_in(page)

    try:
        await page.wait_for_selector("a[href*='/company/']", timeout=15_000)
    except Exception:
        return {"error": "company search did not render — no company links found"}
    await _scroll(page, distance=600, times=3, pause=0.7)

    raw_items = await page.evaluate(
        """(maxCount) => {
            const out = [];
            const seen = new Set();
            const links = document.querySelectorAll("a[href*='/company/']");
            for (const a of links) {
                if (out.length >= maxCount) break;
                const href = a.href.split('?')[0];
                const m = href.match(/\\/company\\/([^/]+)\\/?$/);
                if (!m) continue;
                if (seen.has(href)) continue;
                seen.add(href);

                let card = a.closest('li') || a.closest('div.entity-result') || a.closest('div');
                if (!card) continue;
                const lines = (card.innerText || '').split('\\n').map(s => s.trim()).filter(Boolean);

                out.push({
                    page_url: href,
                    handle: m[1],
                    raw_lines: lines.slice(0, 6),
                });
            }
            return out;
        }""",
        count,
    )

    items: List[Dict[str, Any]] = []
    for it in raw_items:
        lines = it.get("raw_lines") or []
        # First non-noise line is usually the company name; the
        # subsequent lines are industry / headcount / followers.
        name = next((l for l in lines if l), None)
        industry = lines[1] if len(lines) > 1 else None
        size_or_followers = lines[2] if len(lines) > 2 else None
        items.append({
            "name": name,
            "handle": it.get("handle"),
            "page_url": it.get("page_url"),
            "industry_or_tagline": industry,
            "size_or_followers": size_or_followers,
        })

    return {
        "count": len(items),
        "query": keywords,
        "results": items,
    }


# ── view_company ────────────────────────────────────────────────────────────

async def _view_company(page, params: Dict[str, Any]) -> Dict[str, Any]:
    target = (params.get("company") or "").strip()
    if not target:
        return {"error": "company required"}

    url = _normalize_company_url(target)
    if not url:
        return {"error": f"could not resolve company target: {target!r}"}

    # Hit the /about/ subpath where the structured card lives.
    about_url = url.rstrip("/") + "/about/"
    await page.goto(about_url, wait_until="domcontentloaded")
    await _ensure_logged_in(page)

    try:
        await page.wait_for_selector("h1", timeout=10_000)
    except Exception:
        return {"error": "company page did not load — bad handle or blocked"}
    await _scroll(page, distance=600, times=2, pause=0.5)

    data = await page.evaluate(
        """() => {
            const text = (sel) => {
                const el = document.querySelector(sel);
                return el ? (el.textContent || '').trim() : null;
            };

            const name = text('h1');

            // Top card has a tagline immediately under the name.
            const tagline = text('p.org-top-card-summary__tagline')
                         || text('div.org-top-card-summary p');

            // Follower count appears in a small block near the top.
            let followers = null;
            const fm = (document.body.innerText || '').match(/([\\d,\\.]+\\s*(?:M|K)?\\s+followers)/i);
            if (fm) followers = fm[1];

            // Structured "About" dl pairs (Industry / Company size /
            // Headquarters / Founded / Specialties / Website).
            const card = {};
            const dts = document.querySelectorAll('dt.text-heading-medium, dt');
            dts.forEach(dt => {
                const key = (dt.textContent || '').trim().toLowerCase().replace(/\\s+/g, '_');
                if (!key) return;
                const dd = dt.nextElementSibling;
                if (!dd) return;
                const val = (dd.innerText || '').trim();
                if (val) card[key] = val;
            });

            // About long-form description.
            const aboutHeader = Array.from(document.querySelectorAll('h2'))
                .find(h => (h.textContent || '').trim().toLowerCase().startsWith('about'));
            let about = null;
            if (aboutHeader) {
                const sec = aboutHeader.closest('section');
                if (sec) about = (sec.innerText || '').replace(/^About\\s*/i, '').trim();
            }

            return { name, tagline, followers, about, card };
        }"""
    )

    about = data.get("about")
    if about and len(about) > 4000:
        about = about[:4000] + "…"

    card = data.get("card") or {}
    return {
        "url": url,
        "name": data.get("name"),
        "tagline": data.get("tagline"),
        "follower_count": data.get("followers"),
        "industry": card.get("industry"),
        "headcount": card.get("company_size") or card.get("size"),
        "headquarters": card.get("headquarters"),
        "founded": card.get("founded"),
        "specialties": card.get("specialties"),
        "website": card.get("website"),
        "about": about,
    }


# ── search_jobs ─────────────────────────────────────────────────────────────

async def _search_jobs(page, params: Dict[str, Any]) -> Dict[str, Any]:
    keywords = (params.get("keywords") or "").strip()
    if not keywords:
        return {"error": "keywords required"}
    count = max(1, min(25, int(params.get("count") or 10)))

    qs = [f"keywords={quote_plus(keywords)}"]
    location = (params.get("location") or "").strip()
    if location:
        qs.append(f"location={quote_plus(location)}")
    if params.get("remote"):
        # f_WT=2 = remote only. (1=on-site, 3=hybrid)
        qs.append("f_WT=2")
    days = params.get("posted_within_days")
    if days:
        # LinkedIn buckets: r86400 (24h), r604800 (week), r2592000 (month).
        bucket = 86400 if int(days) <= 1 else 604800 if int(days) <= 7 else 2592000
        qs.append(f"f_TPR=r{bucket}")
    if params.get("easy_apply_only"):
        qs.append("f_AL=true")

    url = f"{_BASE}/jobs/search/?" + "&".join(qs)
    await page.goto(url, wait_until="domcontentloaded")
    await _ensure_logged_in(page)

    try:
        await page.wait_for_selector("a[href*='/jobs/view/']", timeout=15_000)
    except Exception:
        return {"error": "job search did not render — no job links found"}

    # Trigger lazy-load by scrolling the result column.
    await _scroll(page, distance=900, times=3, pause=0.6)

    raw = await page.evaluate(
        """(maxCount) => {
            const out = [];
            const seen = new Set();
            const links = document.querySelectorAll("a[href*='/jobs/view/']");
            for (const a of links) {
                if (out.length >= maxCount) break;
                const href = a.href.split('?')[0];
                const m = href.match(/\\/jobs\\/view\\/(\\d+)/);
                if (!m) continue;
                if (seen.has(m[1])) continue;
                seen.add(m[1]);

                let card = a.closest('li') || a.closest('div.job-card-container') || a.closest('div');
                if (!card) continue;
                const text = (card.innerText || '').trim();
                const lines = text.split('\\n').map(s => s.trim()).filter(Boolean);

                const easyApply = /easy apply/i.test(text);
                const promoted = /promoted/i.test(text);

                out.push({
                    job_id: m[1],
                    job_url: 'https://www.linkedin.com/jobs/view/' + m[1] + '/',
                    raw_lines: lines.slice(0, 8),
                    easy_apply: easyApply,
                    promoted,
                });
            }
            return out;
        }""",
        count,
    )

    items: List[Dict[str, Any]] = []
    noise = re.compile(
        r"^(promoted|easy apply|viewed|applied|actively reviewing|with verification)$",
        re.I,
    )
    for it in raw:
        lines = [l for l in (it.get("raw_lines") or []) if not noise.match(l)]
        items.append({
            "job_id": it.get("job_id"),
            "job_url": it.get("job_url"),
            "title": lines[0] if lines else None,
            "company": lines[1] if len(lines) > 1 else None,
            "location": lines[2] if len(lines) > 2 else None,
            "posted_age": lines[3] if len(lines) > 3 else None,
            "easy_apply": it.get("easy_apply"),
            "promoted": it.get("promoted"),
        })

    return {
        "count": len(items),
        "query": keywords,
        "results": items,
    }


# ── view_job ────────────────────────────────────────────────────────────────

async def _view_job(page, params: Dict[str, Any]) -> Dict[str, Any]:
    target = (params.get("job") or "").strip()
    if not target:
        return {"error": "job required"}
    job_id = _job_id_from(target)
    if not job_id:
        return {"error": f"could not resolve job id from {target!r}"}

    url = f"{_BASE}/jobs/view/{job_id}/"
    await page.goto(url, wait_until="domcontentloaded")
    await _ensure_logged_in(page)

    try:
        await page.wait_for_selector("h1", timeout=10_000)
    except Exception:
        return {"error": "job page did not load — bad id, expired, or blocked"}

    # Click the "show more" toggle on the description to expand it.
    for sel in (
        "button.show-more-less-html__button",
        "button[aria-label*='Show more']",
    ):
        try:
            await page.click(sel, timeout=2000)
            break
        except Exception:
            continue
    await _scroll(page, distance=400, times=1, pause=0.4)

    data = await page.evaluate(
        """() => {
            const text = (sel) => {
                const el = document.querySelector(sel);
                return el ? (el.textContent || '').trim() : null;
            };

            const title = text('h1');

            // Top card holds company + location + posted age + applicants.
            const top = text('.job-details-jobs-unified-top-card__primary-description-container')
                     || text('.jobs-unified-top-card__primary-description')
                     || text('.topcard__flavor-row');
            // Each item separated by middle dots in LinkedIn's UI.
            const topParts = (top || '').split(/[·•|]/).map(s => s.trim()).filter(Boolean);

            // Description body.
            const desc = text('.jobs-description-content__text')
                      || text('.jobs-description__container')
                      || text('article.jobs-description');

            // "About the job" criteria (Seniority / Employment type / etc.)
            const insights = Array.from(
                document.querySelectorAll('.job-details-jobs-unified-top-card__job-insight, .description__job-criteria-item, li.job-criteria__item')
            ).map(el => (el.innerText || '').trim()).filter(Boolean);

            // Easy Apply availability.
            const easyApplyBtn = document.querySelector(
                "button.jobs-apply-button, button[aria-label*='Easy Apply']"
            );
            const easyApply = easyApplyBtn
              ? /easy apply/i.test(easyApplyBtn.innerText || '')
              : false;

            return { title, topParts, desc, insights, easyApply };
        }"""
    )

    desc = data.get("desc") or None
    if desc and len(desc) > 8000:
        desc = desc[:8000] + "…"

    top_parts = data.get("topParts") or []
    return {
        "url": url,
        "job_id": job_id,
        "title": data.get("title"),
        "company": top_parts[0] if top_parts else None,
        "location": top_parts[1] if len(top_parts) > 1 else None,
        "posted_age": top_parts[2] if len(top_parts) > 2 else None,
        "applicant_count": top_parts[3] if len(top_parts) > 3 else None,
        "criteria": data.get("insights") or [],
        "description": desc,
        "easy_apply_available": bool(data.get("easyApply")),
    }


# ── easy_apply ──────────────────────────────────────────────────────────────

# Maximum number of Easy Apply form pages to walk in one call. LinkedIn's
# longest forms are typically 4–5 pages (contact info, resume,
# screening questions, voluntary disclosures, review). 8 is a generous
# upper bound that bails on infinite-loop bugs without truncating real
# applications.
_MAX_FORM_PAGES = 8


async def _inspect_easy_apply_form(page) -> Optional[Dict[str, Any]]:
    """Read the current Easy Apply modal page. Returns
    ``{labels, blocking_fields, has_submit, has_next, has_review}``
    where each ``blocking_fields`` entry is
    ``{label, type, options}`` so the caller can render a typed form
    in the chat (and so the answer-filling code knows whether to
    fill / select / check)."""
    return await page.evaluate(
        """() => {
            const modal = document.querySelector(
                "div[role='dialog'][aria-labelledby], div.jobs-easy-apply-modal"
            );
            if (!modal) return null;

            const labels = [];
            modal.querySelectorAll('label, legend').forEach(l => {
                const txt = (l.innerText || '').trim();
                if (txt) labels.push(txt);
            });

            const fields = [];

            // Required text / number / select / textarea — empty-valued
            // entries are blockers we need user answers for.
            modal.querySelectorAll(
                'input[required], select[required], textarea[required]'
            ).forEach(el => {
                if (el.value) return;  // already filled, skip

                const labEl = el.closest('div')
                    ? el.closest('div').querySelector('label')
                    : null;
                const label = ((labEl && labEl.innerText) || el.name || '').trim()
                    || '<unnamed>';

                let type = 'text';
                let options = null;
                const tag = el.tagName;
                if (tag === 'SELECT') {
                    type = 'select';
                    options = Array.from(el.querySelectorAll('option'))
                        .map(o => (o.innerText || o.value || '').trim())
                        .filter(s => s && s.toLowerCase() !== 'select an option');
                } else if (tag === 'INPUT') {
                    const t = (el.type || 'text').toLowerCase();
                    if (t === 'number' || t === 'tel') type = 'number';
                    else if (t === 'checkbox') type = 'checkbox';
                    else if (t === 'radio') type = 'radio';
                    else type = 'text';
                } else if (tag === 'TEXTAREA') {
                    type = 'text';
                }
                fields.push({ label, type, options });
            });

            // Radio groups — required-looking when no option is checked.
            modal.querySelectorAll('fieldset[role="radiogroup"]').forEach(fs => {
                const lab = fs.querySelector('legend');
                if (!lab) return;
                const label = (lab.innerText || '').trim();
                if (!label) return;
                const radios = Array.from(fs.querySelectorAll('input[type="radio"]'));
                if (!radios.length) return;
                if (radios.some(r => r.checked)) return;
                const options = radios.map(r => {
                    const wrap = r.closest('label') || r.closest('div');
                    const t = (wrap && wrap.innerText) || r.value || '';
                    return t.trim();
                }).filter(Boolean);
                fields.push({ label, type: 'radio', options });
            });

            const hasSubmit = !!modal.querySelector(
                "button[aria-label*='Submit application'], "
                + "button[data-easy-apply-submit-button]"
            );
            const hasReview = !!modal.querySelector(
                "button[aria-label*='Review your application']"
            );
            const hasNext   = !!modal.querySelector(
                "button[aria-label*='Continue to next step']"
            );

            return {
                labels: labels.slice(0, 30),
                blocking_fields: fields,
                has_submit: hasSubmit,
                has_review: hasReview,
                has_next: hasNext,
            };
        }"""
    )


async def _fill_known_answers(
    page,
    blocking_fields: List[Dict[str, Any]],
    answers: Dict[str, str],
) -> List[str]:
    """For each blocking field whose label has an entry in ``answers``,
    set the value via the type-appropriate Playwright API. Returns the
    list of labels we successfully filled (so the caller can re-inspect
    + know what was just answered).

    Filling is best-effort: failures (selector miss, invalid option for
    a select) are logged and skipped rather than raised. The next
    inspect pass picks them up as still-blocking and the user's next
    answer attempt can fix the wrong-format value."""
    filled: List[str] = []
    for field in blocking_fields:
        label = (field or {}).get("label") or ""
        if not label or label not in answers:
            continue
        value = answers[label]
        if value is None or value == "":
            continue
        ftype = (field.get("type") or "text").lower()
        try:
            ok = await _fill_one_field(page, label, value, ftype)
        except Exception:
            ok = False
        if ok:
            filled.append(label)
    return filled


async def _fill_one_field(
    page, label: str, value: str, ftype: str,
) -> bool:
    """Set ``value`` into the input that ``label`` annotates. Returns
    True on success. The ``ftype`` is one of ``text`` / ``number`` /
    ``select`` / ``radio`` / ``checkbox``."""
    if ftype in ("text", "number"):
        try:
            await page.get_by_label(label, exact=False).first.fill(str(value))
            return True
        except Exception:
            return False
    if ftype == "select":
        try:
            loc = page.get_by_label(label, exact=False).first
            await loc.select_option(label=str(value))
            return True
        except Exception:
            try:
                await loc.select_option(value=str(value))
                return True
            except Exception:
                return False
    if ftype == "radio":
        # LinkedIn radio groups: click the option whose label text matches.
        try:
            await page.get_by_role("radio", name=str(value), exact=False).first.check()
            return True
        except Exception:
            return False
    if ftype == "checkbox":
        # Boolean: truthy → check, falsy → uncheck.
        try:
            loc = page.get_by_label(label, exact=False).first
            should_check = str(value).lower() in ("true", "yes", "1", "on", "y")
            if should_check:
                await loc.check()
            else:
                await loc.uncheck()
            return True
        except Exception:
            return False
    return False


async def _advance_easy_apply_page(page) -> bool:
    """Click Next or Review on the current modal page. Returns True if
    a click landed; False if all selectors missed (caller treats as a
    layout-drift error)."""
    for sel in (
        "button[aria-label*='Continue to next step']",
        "button[aria-label*='Review your application']",
        "button:has-text('Next')",
        "button:has-text('Review')",
    ):
        try:
            await page.click(sel, timeout=2500)
            return True
        except Exception:
            continue
    return False


async def _easy_apply(page, params: Dict[str, Any]) -> Dict[str, Any]:
    """Submit (or dry-run) an Easy Apply application.

    Strategy — keep it conservative:

      1. Open the job, click Easy Apply.
      2. If the modal is a single-page form and every field is
         pre-filled (typical happy path), we can submit safely.
      3. If there are multi-page wizards or any unanswered required
         questions, return ``status='blocked'`` with the questions
         extracted so the caller knows what to ask the user.
      4. ``confirm`` MUST be True to actually click Submit. Default
         dry-run reports the captured state, then closes the modal.
    """
    target = (params.get("job") or "").strip()
    if not target:
        return {"error": "job required"}
    job_id = _job_id_from(target)
    if not job_id:
        return {"error": f"could not resolve job id from {target!r}"}
    confirm = bool(params.get("confirm", False))

    url = f"{_BASE}/jobs/view/{job_id}/"
    await page.goto(url, wait_until="domcontentloaded")
    await _ensure_logged_in(page)

    # Click the Easy Apply button. There are several layouts; try a list.
    opened = False
    for sel in (
        "button.jobs-apply-button",
        "button[aria-label*='Easy Apply']",
        "button:has-text('Easy Apply')",
    ):
        try:
            await page.click(sel, timeout=4000)
            opened = True
            break
        except Exception:
            continue
    if not opened:
        return {
            "status": "error",
            "reason": "Easy Apply button not found — this job may use an external apply flow",
            "job_id": job_id,
        }

    # Wait for the modal to appear.
    try:
        await page.wait_for_selector(
            "div[role='dialog'][aria-labelledby], div.jobs-easy-apply-modal",
            timeout=8000,
        )
    except Exception:
        return {
            "status": "error",
            "reason": "Easy Apply modal did not open",
            "job_id": job_id,
        }

    # Answer-aware multi-page walk. ``answers`` is a dict
    # ``{label: value}`` populated by the resolve endpoint after a
    # prior dry-run returned status='blocked'. We loop:
    #   inspect → fill from answers → advance Next/Review → repeat
    # bounded by ``_MAX_FORM_PAGES`` so a corrupt form can never run
    # away with the agent.
    answers = params.get("answers") or {}
    if not isinstance(answers, dict):
        answers = {}

    visited_pages = 0
    all_labels: List[str] = []
    answered_in_session: List[str] = []

    while visited_pages < _MAX_FORM_PAGES:
        visited_pages += 1
        inspect = await _inspect_easy_apply_form(page)
        if not inspect:
            return {"status": "error", "reason": "modal disappeared", "job_id": job_id}

        # Accumulate labels seen across pages — surfaced in preview /
        # blocked returns so the caller can sanity-check what we walked.
        all_labels.extend(inspect.get("labels") or [])

        blocking_fields: List[Dict[str, Any]] = inspect.get("blocking_fields") or []

        # Fill what we know on this page.
        if blocking_fields and answers:
            filled = await _fill_known_answers(page, blocking_fields, answers)
            answered_in_session.extend(filled)
            if filled:
                # Re-inspect — filled fields may unblock or expose new ones.
                inspect = await _inspect_easy_apply_form(page) or inspect
                blocking_fields = inspect.get("blocking_fields") or []

        # Still blocking on this page — surface the questions to caller.
        if blocking_fields:
            await _close_easy_apply_modal(page, save_draft=False)
            return {
                "status": "blocked",
                "job_id": job_id,
                "reason": (
                    "form has questions we don't have answers for; "
                    "fill them via the resolve endpoint and retry"
                ),
                "blocking_questions": blocking_fields,
                "form_labels": all_labels[:30],
                "pages_walked": visited_pages,
                "answered_this_session": answered_in_session,
            }

        # Page is clear. Decide what's next.
        if inspect.get("has_next") or inspect.get("has_review"):
            advanced = await _advance_easy_apply_page(page)
            if not advanced:
                # Couldn't advance — defensive return so we don't loop.
                await _close_easy_apply_modal(page, save_draft=False)
                return {
                    "status": "error",
                    "reason": "next/review button visible but click failed",
                    "job_id": job_id,
                    "form_labels": all_labels[:30],
                }
            # Loop to inspect the next page.
            await asyncio.sleep(0.4)
            continue

        # No more pages; we should be at submit.
        if not inspect.get("has_submit"):
            await _close_easy_apply_modal(page, save_draft=False)
            return {
                "status": "error",
                "reason": "no Submit button after walking the form",
                "job_id": job_id,
                "form_labels": all_labels[:30],
                "pages_walked": visited_pages,
            }

        # Honor the confirm gate.
        if not confirm:
            await _close_easy_apply_modal(page, save_draft=False)
            return {
                "status": "preview",
                "job_id": job_id,
                "can_submit": True,
                "form_labels": all_labels[:30],
                "pages_walked": visited_pages,
                "answered_this_session": answered_in_session,
                "note": "Pass confirm=true to actually submit.",
            }

        # Click Submit.
        submitted = False
        for sel in (
            "button[aria-label*='Submit application']",
            "button[data-easy-apply-submit-button]",
        ):
            try:
                await page.click(sel, timeout=4000)
                submitted = True
                break
            except Exception:
                continue
        if not submitted:
            return {"status": "error", "reason": "submit click failed", "job_id": job_id}

        await asyncio.sleep(2.0)
        return {
            "status": "submitted",
            "job_id": job_id,
            "pages_walked": visited_pages,
            "answered_this_session": answered_in_session,
            "confirmation": "Easy Apply submitted (verify in My Jobs)",
        }

    # Hit the page-walk depth limit — bail with what we have.
    await _close_easy_apply_modal(page, save_draft=False)
    return {
        "status": "error",
        "reason": (
            f"easy_apply page-walk exceeded {_MAX_FORM_PAGES} pages; "
            "this form is unusual — fall back to the LinkedIn UI"
        ),
        "job_id": job_id,
        "form_labels": all_labels[:30],
    }


async def _close_easy_apply_modal(page, *, save_draft: bool) -> None:
    """Dismiss the Easy Apply modal without submitting. LinkedIn pops a
    'Save this application?' confirm — we Discard so we don't pollute
    the user's drafts."""
    try:
        await page.click(
            "button[aria-label*='Dismiss'], button.artdeco-modal__dismiss",
            timeout=2000,
        )
    except Exception:
        return
    # Confirm dialog: 'Save' or 'Discard'.
    try:
        if save_draft:
            await page.click("button:has-text('Save')", timeout=2000)
        else:
            await page.click("button:has-text('Discard')", timeout=2000)
    except Exception:
        pass


# ── list_my_applications ────────────────────────────────────────────────────

async def _list_my_applications(page, params: Dict[str, Any]) -> Dict[str, Any]:
    count = max(1, min(50, int(params.get("count") or 20)))
    url = f"{_BASE}/my-items/saved-jobs/?cardType=APPLIED"
    await page.goto(url, wait_until="domcontentloaded")
    await _ensure_logged_in(page)

    if not await _wait_any(
        page,
        [
            "ul.reusable-search__entity-result-list",
            "div.workflow-results-container",
            "ul.scaffold-layout__list-container",
            "main",
        ],
        timeout=12_000,
    ):
        return {"error": "applied-jobs page did not render"}

    await _scroll(page, distance=900, times=3, pause=0.5)

    raw = await page.evaluate(
        """(maxCount) => {
            const out = [];
            const seen = new Set();
            const links = document.querySelectorAll("a[href*='/jobs/view/']");
            for (const a of links) {
                if (out.length >= maxCount) break;
                const href = a.href.split('?')[0];
                const m = href.match(/\\/jobs\\/view\\/(\\d+)/);
                if (!m) continue;
                if (seen.has(m[1])) continue;
                seen.add(m[1]);
                let card = a.closest('li') || a.closest('div.entity-result') || a.closest('div');
                if (!card) continue;
                const lines = (card.innerText || '').split('\\n').map(s => s.trim()).filter(Boolean);
                out.push({
                    job_id: m[1],
                    job_url: 'https://www.linkedin.com/jobs/view/' + m[1] + '/',
                    raw_lines: lines.slice(0, 6),
                });
            }
            return out;
        }""",
        count,
    )

    items: List[Dict[str, Any]] = []
    for it in raw:
        lines = it.get("raw_lines") or []
        # Look for an "Applied {time ago}" line.
        applied_line = next(
            (l for l in lines if re.search(r"applied", l, re.I)),
            None,
        )
        items.append({
            "job_id": it.get("job_id"),
            "job_url": it.get("job_url"),
            "title": lines[0] if lines else None,
            "company": lines[1] if len(lines) > 1 else None,
            "location": lines[2] if len(lines) > 2 else None,
            "applied": applied_line,
        })

    return {"count": len(items), "results": items}


# ── list_conversations ──────────────────────────────────────────────────────

async def _list_conversations(page, params: Dict[str, Any]) -> Dict[str, Any]:
    count = max(1, min(50, int(params.get("count") or 20)))
    await page.goto(f"{_BASE}/messaging/", wait_until="domcontentloaded")
    await _ensure_logged_in(page)

    if not await _wait_any(
        page,
        [
            "ul.msg-conversations-container__conversations-list",
            "section.msg-overlay-list-bubble",
            "div.scaffold-finite-scroll__content",
            "div.msg-conversations-container",
        ],
        timeout=12_000,
    ):
        return {"error": "messaging inbox did not render"}

    await _scroll(page, distance=600, times=2, pause=0.4)

    raw = await page.evaluate(
        """(maxCount) => {
            const out = [];
            const seen = new Set();
            // Conversation list items each carry a link to the thread.
            const links = document.querySelectorAll("a[href*='/messaging/thread/']");
            for (const a of links) {
                if (out.length >= maxCount) break;
                const href = a.href.split('?')[0];
                const m = href.match(/\\/messaging\\/thread\\/([^/]+)/);
                if (!m) continue;
                if (seen.has(m[1])) continue;
                seen.add(m[1]);

                let card = a.closest('li') || a.closest('div.msg-conversation-card') || a.closest('div');
                if (!card) continue;

                const lines = (card.innerText || '').split('\\n').map(s => s.trim()).filter(Boolean);
                // Detect unread badge / bold styling.
                const unread = !!card.querySelector('.notification-badge--show, .msg-conversation-card__unread-count');

                out.push({
                    thread_id: m[1],
                    thread_url: 'https://www.linkedin.com' + (new URL(href)).pathname,
                    raw_lines: lines.slice(0, 8),
                    unread,
                });
            }
            return out;
        }""",
        count,
    )

    items: List[Dict[str, Any]] = []
    for it in raw:
        lines = it.get("raw_lines") or []
        # Card layout is roughly: participant name → timestamp → preview
        # → "You: ..." sometimes. Take first three for the structured fields.
        items.append({
            "thread_id": it.get("thread_id"),
            "thread_url": it.get("thread_url"),
            "participants": lines[0] if lines else None,
            "last_updated": lines[1] if len(lines) > 1 else None,
            "last_message_preview": lines[2] if len(lines) > 2 else None,
            "unread": it.get("unread"),
        })
    return {"count": len(items), "results": items}


# ── view_conversation ───────────────────────────────────────────────────────

async def _view_conversation(page, params: Dict[str, Any]) -> Dict[str, Any]:
    target = (params.get("thread") or "").strip()
    if not target:
        return {"error": "thread required"}
    thread_id = _thread_id_from(target)
    if not thread_id:
        return {"error": f"could not resolve thread id from {target!r}"}
    message_count = max(1, min(50, int(params.get("message_count") or 20)))

    url = f"{_BASE}/messaging/thread/{thread_id}/"
    await page.goto(url, wait_until="domcontentloaded")
    await _ensure_logged_in(page)

    try:
        await page.wait_for_selector(
            "div.msg-s-message-list, ul.msg-s-message-list-content, "
            "[data-test-message-list]",
            timeout=10_000,
        )
    except Exception:
        return {"error": "thread did not load — bad id, expired, or blocked"}

    # Scroll up a bit to surface older messages.
    try:
        await page.evaluate(
            "() => { const el = document.querySelector('.msg-s-message-list, .msg-s-message-list-content'); "
            "if (el) el.scrollTop = 0; }"
        )
    except Exception:
        pass
    await asyncio.sleep(0.6)

    data = await page.evaluate(
        """(maxMsgs) => {
            const text = (sel) => {
                const el = document.querySelector(sel);
                return el ? (el.textContent || '').trim() : null;
            };
            const participants = text('h2.msg-thread__top-bar-title')
                              || text('header h2');

            const blocks = Array.from(
                document.querySelectorAll('li.msg-s-message-list__event, div.msg-s-event-listitem')
            );
            const msgs = blocks.slice(-maxMsgs).map(li => {
                const sender = (li.querySelector('.msg-s-message-group__name, .msg-s-event-listitem__name') || {}).textContent || null;
                const ts     = (li.querySelector('time, .msg-s-message-group__timestamp, .msg-s-event-listitem__timestamp') || {}).textContent || null;
                const body   = (li.querySelector('.msg-s-event-listitem__body, p.msg-s-event-listitem__body') || {}).textContent || (li.innerText || '');
                return {
                    sender: sender ? sender.trim() : null,
                    timestamp: ts ? ts.trim() : null,
                    body: (body || '').trim().slice(0, 4000),
                };
            });
            return { participants, msgs };
        }""",
        message_count,
    )

    return {
        "thread_id": thread_id,
        "thread_url": url,
        "participants": data.get("participants"),
        "messages": data.get("msgs") or [],
    }


# ── send_message ────────────────────────────────────────────────────────────

async def _send_message(page, params: Dict[str, Any]) -> Dict[str, Any]:
    """Compose (and optionally send) a LinkedIn DM.

    Same conservative pattern as ``easy_apply``:
      * default ``confirm=False`` is a dry-run — types the message
        into the composer, captures the recipient and a screenshot
        of the prepared state, then closes the panel without clicking
        Send. Returns ``status='preview'``.
      * ``confirm=True`` clicks Send. Returns ``status='sent'``.

    The recipient may be:
      * a thread_id (existing conversation — append to it), or
      * a profile URL / vanity handle (open a new conversation via
        the profile's "Message" button).
    """
    recipient = (params.get("recipient") or "").strip()
    text = (params.get("text") or "").strip()
    if not recipient or not text:
        return {"error": "recipient and text required"}
    confirm = bool(params.get("confirm", False))

    # Decide which mode: existing thread vs new conversation.
    thread_id = _thread_id_from(recipient)
    profile_url = None if thread_id else _normalize_profile_url(recipient)

    if not thread_id and not profile_url:
        return {
            "error": (
                "recipient must be a thread_id, a /messaging/thread URL, "
                "or a /in/ profile URL / vanity handle"
            ),
        }

    if thread_id:
        await page.goto(
            f"{_BASE}/messaging/thread/{thread_id}/",
            wait_until="domcontentloaded",
        )
        await _ensure_logged_in(page)
        try:
            await page.wait_for_selector(
                "div.msg-form__contenteditable, [role='textbox']",
                timeout=10_000,
            )
        except Exception:
            return {"status": "error", "reason": "compose box did not appear"}
    else:
        # Open the profile, click "Message".
        await page.goto(profile_url, wait_until="domcontentloaded")
        await _ensure_logged_in(page)
        clicked = False
        for sel in (
            "button[aria-label*='Message'][aria-expanded]",
            "a[aria-label*='Message']",
            "button:has-text('Message')",
        ):
            try:
                await page.click(sel, timeout=4000)
                clicked = True
                break
            except Exception:
                continue
        if not clicked:
            return {
                "status": "error",
                "reason": (
                    "Message button not found — recipient may not allow "
                    "DMs from non-connections"
                ),
            }
        try:
            await page.wait_for_selector(
                "div.msg-form__contenteditable, [role='textbox']",
                timeout=8000,
            )
        except Exception:
            return {"status": "error", "reason": "compose box did not appear"}

    # Type the message into the contenteditable / textarea.
    typed = False
    for sel in (
        "div.msg-form__contenteditable",
        "[role='textbox']",
        "textarea[name='message']",
    ):
        try:
            box = await page.wait_for_selector(sel, timeout=3000)
            if not box:
                continue
            await box.click()
            await page.keyboard.type(text, delay=8)
            typed = True
            break
        except Exception:
            continue
    if not typed:
        return {"status": "error", "reason": "could not type into compose box"}

    if not confirm:
        # Dry-run — back out without sending. LinkedIn keeps a draft
        # on the thread, which is usually fine — the user can wipe it
        # next time they open the conversation.
        return {
            "status": "preview",
            "recipient": recipient,
            "thread_id": thread_id,
            "profile_url": profile_url,
            "text_preview": text[:300],
            "note": "Pass confirm=true to actually send.",
        }

    # Click Send.
    sent = False
    for sel in (
        "button.msg-form__send-button",
        "button[aria-label*='Send']",
        "button[type='submit']:has-text('Send')",
    ):
        try:
            await page.click(sel, timeout=4000)
            sent = True
            break
        except Exception:
            continue
    if not sent:
        return {"status": "error", "reason": "Send button click failed"}

    await asyncio.sleep(1.5)
    return {
        "status": "sent",
        "recipient": recipient,
        "thread_id": thread_id,
        "profile_url": profile_url,
    }


# ── send_invitation ─────────────────────────────────────────────────────────

# LinkedIn caps the personalized note at 300 characters. Anything longer
# either truncates silently (lossy) or trips the Send button into a disabled
# state — neither failure mode is friendly. We refuse on the wrapper side.
_INVITE_NOTE_MAX = 300


async def _send_invitation(page, params: Dict[str, Any]) -> Dict[str, Any]:
    """Send (or dry-run) a LinkedIn connection request.

    Same conservative shape as ``easy_apply`` / ``send_message``:

      * default ``confirm=False`` opens the profile, primes the Connect
        dialog, types the note (if given), captures the prepared state,
        then dismisses the dialog WITHOUT clicking Send. Returns
        ``status='preview'``.
      * ``confirm=True`` clicks Send. Returns ``status='sent'``.

    LinkedIn surfaces several non-error end states that callers should
    branch on rather than treat as failures:
      * already_connected — 1st-degree, nothing to do.
      * pending           — a prior invitation is still outstanding.
      * blocked           — profile disables Connect entirely (Follow-only,
                            out-of-network without InMail, etc.).
    """
    target = (params.get("profile") or "").strip()
    if not target:
        return {"error": "profile required"}
    profile_url = _normalize_profile_url(target)
    if not profile_url:
        return {"error": f"could not resolve profile from {target!r}"}

    note = (params.get("note") or "").strip()
    if len(note) > _INVITE_NOTE_MAX:
        return {
            "error": (
                f"note exceeds LinkedIn's {_INVITE_NOTE_MAX}-char "
                f"limit ({len(note)} chars)"
            ),
        }
    confirm = bool(params.get("confirm", False))

    await page.goto(profile_url, wait_until="domcontentloaded")
    await _ensure_logged_in(page)

    # Wait for the top profile card so the action buttons are present.
    if not await _wait_any(
        page,
        [
            "main section.pv-top-card",
            "main div.ph5",
            "main",
        ],
        timeout=12_000,
    ):
        return {"status": "error", "reason": "profile did not render", "profile_url": profile_url}

    # Inspect what action buttons LinkedIn rendered. We need to know
    # whether Connect is direct, behind a More menu, or absent (already
    # connected / pending / unavailable).
    state = await page.evaluate(
        """() => {
            const top = document.querySelector("main section.pv-top-card") || document.querySelector("main");
            if (!top) return null;
            const buttons = Array.from(top.querySelectorAll("button, a[role='button']"));
            const visible = buttons.filter(b => {
                const r = b.getBoundingClientRect();
                return r.width > 0 && r.height > 0;
            });
            const labelOf = b => ((b.getAttribute('aria-label') || b.innerText || '') + '').trim();

            const findByLabel = re => visible.find(b => re.test(labelOf(b)));

            const connect = findByLabel(/^connect$|^invite\\b|^connect with\\b/i);
            const pending = findByLabel(/^pending$|withdraw invitation/i);
            const message = findByLabel(/^message$/i);
            const more    = findByLabel(/^more$|more actions/i);

            return {
                has_connect: !!connect,
                has_pending: !!pending,
                has_message_only: !!message && !connect && !pending && !more,
                has_more: !!more,
            };
        }"""
    )

    if not state:
        return {"status": "error", "reason": "could not read profile actions", "profile_url": profile_url}

    if state.get("has_pending"):
        return {
            "status": "pending",
            "profile_url": profile_url,
            "reason": "a previous invitation is still outstanding",
        }
    if state.get("has_message_only") and not state.get("has_connect") and not state.get("has_more"):
        # 1st-degree connections lose Connect, keep Message — best signal we have.
        return {
            "status": "already_connected",
            "profile_url": profile_url,
        }

    # Click Connect — direct, then fall back to "More → Connect".
    opened = False
    if state.get("has_connect"):
        for sel in (
            "main section.pv-top-card button[aria-label^='Invite'][aria-label*='to connect']",
            "main button[aria-label^='Invite'][aria-label*='to connect']",
            "main section.pv-top-card button:has-text('Connect')",
            "main button:has-text('Connect')",
        ):
            try:
                await page.click(sel, timeout=3000)
                opened = True
                break
            except Exception:
                continue

    if not opened and state.get("has_more"):
        # Open the More overflow, then click Connect inside the dropdown.
        try:
            await page.click(
                "main button[aria-label='More actions'], main button:has-text('More')",
                timeout=3000,
            )
            await asyncio.sleep(0.4)
            for sel in (
                "div[role='menu'] [aria-label^='Invite'][aria-label*='to connect']",
                "div[role='menu'] div:has-text('Connect')",
                "div.artdeco-dropdown__content [aria-label*='to connect']",
            ):
                try:
                    await page.click(sel, timeout=3000)
                    opened = True
                    break
                except Exception:
                    continue
        except Exception:
            pass

    if not opened:
        return {
            "status": "blocked",
            "profile_url": profile_url,
            "reason": (
                "Connect button not found — profile may be Follow-only, "
                "out-of-network, or LinkedIn changed its layout"
            ),
        }

    # Connect dialog is open. Path A: noteless quick-send (single "Send"
    # button). Path B: choose "Add a note" → textarea → "Send".
    try:
        await page.wait_for_selector(
            "div[role='dialog'][aria-labelledby], div.send-invite",
            timeout=6000,
        )
    except Exception:
        return {
            "status": "error",
            "reason": "connect dialog did not open",
            "profile_url": profile_url,
        }

    if note:
        # Click "Add a note" if it's there. On the noteless dialog this
        # button is absent and we type directly.
        for sel in (
            "button[aria-label='Add a note']",
            "button:has-text('Add a note')",
        ):
            try:
                await page.click(sel, timeout=2000)
                break
            except Exception:
                continue
        # Type the note.
        try:
            box = await page.wait_for_selector(
                "textarea[name='message'], textarea#custom-message",
                timeout=4000,
            )
            await box.click()
            # Use Playwright's fill where possible — typing each
            # character at LinkedIn's compose box can race with their
            # debounce and corrupt the length counter.
            await box.fill(note)
        except Exception:
            return {
                "status": "error",
                "reason": "note textarea did not appear",
                "profile_url": profile_url,
            }

    if not confirm:
        # Dry-run — close the dialog without sending.
        try:
            await page.click(
                "button[aria-label='Dismiss'], button.artdeco-modal__dismiss",
                timeout=2000,
            )
        except Exception:
            pass
        return {
            "status": "preview",
            "profile_url": profile_url,
            "note_preview": note[:_INVITE_NOTE_MAX] if note else None,
            "would_include_note": bool(note),
            "note": "Pass confirm=true to actually send.",
        }

    # Click Send.
    sent = False
    for sel in (
        "button[aria-label='Send invitation']",
        "button[aria-label='Send now']",
        "button[aria-label='Send without a note']",
        "button:has-text('Send')",
    ):
        try:
            await page.click(sel, timeout=4000)
            sent = True
            break
        except Exception:
            continue
    if not sent:
        return {
            "status": "error",
            "reason": "Send button click failed",
            "profile_url": profile_url,
        }

    await asyncio.sleep(1.5)
    return {
        "status": "sent",
        "profile_url": profile_url,
        "with_note": bool(note),
    }


# ── browse_feed ─────────────────────────────────────────────────────────────

async def _browse_feed(page, params: Dict[str, Any]) -> Dict[str, Any]:
    count = max(1, min(25, int(params.get("count") or 10)))
    await page.goto(f"{_BASE}/feed/", wait_until="domcontentloaded")
    await _ensure_logged_in(page)

    # Wait on activity URN attributes — LinkedIn keeps these stable
    # across DOM rollouts (they're load-bearing for tracking).
    try:
        await page.wait_for_selector(
            "[data-id^='urn:li:activity:'], [data-urn^='urn:li:activity:'], [data-urn^='urn:li:share:'], main",
            timeout=12_000,
        )
    except Exception:
        return {"error": "feed did not render"}

    # Scroll a few times to load more posts.
    await _scroll(page, distance=1200, times=4, pause=0.7)

    raw = await page.evaluate(
        """(maxCount) => _extractPosts(document, maxCount);
        function _extractPosts(root, maxCount) {
            const out = [];
            const seen = new Set();
            const cards = root.querySelectorAll(
                "div[data-id^='urn:li:activity:'], div[data-urn^='urn:li:activity:'], div[data-urn^='urn:li:share:']"
            );
            for (const c of cards) {
                if (out.length >= maxCount) break;
                const urn = c.getAttribute('data-id')
                         || c.getAttribute('data-urn')
                         || '';
                if (!urn || seen.has(urn)) continue;
                seen.add(urn);

                const text = (c.innerText || '').trim();
                if (!text) continue;
                const lines = text.split('\\n').map(s => s.trim()).filter(Boolean);

                // Author is generally the first line, body starts after
                // the author/headline/timestamp meta.
                const author = lines[0] || null;
                const body = lines.slice(2).join('\\n').slice(0, 1500);

                // Engagement counts when LinkedIn renders them.
                const reactionsEl = c.querySelector("[aria-label*='reaction'], button.social-details-social-counts__reactions-count");
                const reactions = reactionsEl ? (reactionsEl.textContent || '').trim() : null;
                const commentsEl = c.querySelector("[aria-label*='comment'], button.social-details-social-counts__comments");
                const comments = commentsEl ? (commentsEl.textContent || '').trim() : null;

                const permalinkEl = c.querySelector("a[href*='/feed/update/']");
                const permalink = permalinkEl ? permalinkEl.href.split('?')[0] : null;

                out.push({ urn, author, body, reactions, comments, url: permalink });
            }
            return out;
        }""",
        count,
    )

    return {"count": len(raw), "results": raw}


# ── search_posts ────────────────────────────────────────────────────────────

async def _search_posts(page, params: Dict[str, Any]) -> Dict[str, Any]:
    keywords = (params.get("keywords") or "").strip()
    if not keywords:
        return {"error": "keywords required"}
    count = max(1, min(25, int(params.get("count") or 10)))

    url = f"{_BASE}/search/results/content/?keywords={quote_plus(keywords)}"
    await page.goto(url, wait_until="domcontentloaded")
    await _ensure_logged_in(page)

    try:
        await page.wait_for_selector(
            "[data-id^='urn:li:activity:'], [data-urn^='urn:li:activity:'], [data-urn^='urn:li:share:'], main",
            timeout=15_000,
        )
    except Exception:
        return {"error": "post search did not render — no activity URNs found"}

    await _scroll(page, distance=900, times=3, pause=0.6)

    raw = await page.evaluate(
        """(maxCount) => {
            const out = [];
            const seen = new Set();
            const cards = document.querySelectorAll(
                "div[data-urn^='urn:li:activity:'], div[data-id^='urn:li:activity:'], div[data-urn^='urn:li:share:']"
            );
            for (const c of cards) {
                if (out.length >= maxCount) break;
                const urn = c.getAttribute('data-urn') || c.getAttribute('data-id') || '';
                if (!urn || seen.has(urn)) continue;
                seen.add(urn);

                const lines = (c.innerText || '').split('\\n').map(s => s.trim()).filter(Boolean);
                const author = lines[0] || null;
                const body = lines.slice(2).join('\\n').slice(0, 1200);
                const permalinkEl = c.querySelector("a[href*='/feed/update/']");
                const permalink = permalinkEl ? permalinkEl.href.split('?')[0] : null;

                out.push({ urn, author, body, url: permalink });
            }
            return out;
        }""",
        count,
    )

    return {"count": len(raw), "query": keywords, "results": raw}


# ── view_post ───────────────────────────────────────────────────────────────

async def _view_post(page, params: Dict[str, Any]) -> Dict[str, Any]:
    target = (params.get("post") or "").strip()
    if not target:
        return {"error": "post required"}
    urn = _post_urn_from(target)
    if not urn:
        return {"error": f"could not resolve post URN from {target!r}"}

    url = f"{_BASE}/feed/update/{urn}/"
    await page.goto(url, wait_until="domcontentloaded")
    await _ensure_logged_in(page)

    try:
        await page.wait_for_selector(
            "div[data-urn^='urn:li:activity:'], main",
            timeout=10_000,
        )
    except Exception:
        return {"error": "post did not load — may have been deleted"}

    await _scroll(page, distance=400, times=1, pause=0.4)

    data = await page.evaluate(
        """(targetUrn) => {
            // Find the activity card matching the URN; fall back to first.
            let card = document.querySelector(`div[data-urn='${targetUrn}']`)
                    || document.querySelector(`div[data-id='${targetUrn}']`)
                    || document.querySelector("div[data-urn^='urn:li:activity:']");
            if (!card) return null;

            const text = (card.innerText || '').trim();
            const lines = text.split('\\n').map(s => s.trim()).filter(Boolean);

            const author = lines[0] || null;
            const headline = lines[1] || null;
            const posted = lines[2] || null;
            const body = lines.slice(3).join('\\n');

            const reactionsEl = card.querySelector("[aria-label*='reaction'], .social-details-social-counts__reactions-count");
            const commentsEl  = card.querySelector("[aria-label*='comment'], .social-details-social-counts__comments");
            return {
                author,
                headline,
                posted,
                body,
                reactions: reactionsEl ? (reactionsEl.textContent || '').trim() : null,
                comments:  commentsEl  ? (commentsEl.textContent  || '').trim() : null,
            };
        }""",
        urn,
    )

    if not data:
        return {"error": "post body not found in DOM"}

    body = data.get("body") or None
    if body and len(body) > 8000:
        body = body[:8000] + "…"

    return {
        "urn": urn,
        "url": url,
        "author": data.get("author"),
        "author_headline": data.get("headline"),
        "posted": data.get("posted"),
        "body": body,
        "reactions": data.get("reactions"),
        "comments": data.get("comments"),
    }


# ── helpers ─────────────────────────────────────────────────────────────────

async def _ensure_logged_in(page) -> None:
    """Hard fail if we got bounced to the login wall. The wrapper turns
    this into a clear message asking the user to re-export cookies."""
    cur = page.url or ""
    if any(p in cur for p in ("/login", "/uas/login", "/checkpoint", "/authwall")):
        raise RuntimeError(
            "LinkedIn session is not authenticated. Re-export your "
            "li_at + JSESSIONID cookies (Cookie-Editor → Export → JSON) "
            "and paste into Integrations → LinkedIn (Search & Messaging)."
        )


async def _wait_any(page, selectors: List[str], *, timeout: int) -> bool:
    """Return True as soon as any of the given selectors becomes
    visible. Splits ``timeout`` evenly across selectors."""
    per = max(1000, timeout // max(1, len(selectors)))
    for sel in selectors:
        try:
            await page.wait_for_selector(sel, timeout=per)
            return True
        except Exception:
            continue
    return False


async def _scroll(page, *, distance: int, times: int, pause: float) -> None:
    """Polite scroll — LinkedIn lazy-loads sections only after they
    enter the viewport."""
    for _ in range(times):
        try:
            await page.evaluate(f"window.scrollBy(0, {int(distance)})")
        except Exception:
            return
        await asyncio.sleep(pause)


_VANITY_RE = re.compile(r"[A-Za-z0-9一-鿿_\-\.%]+")


def _normalize_profile_url(target: str) -> Optional[str]:
    """Accept full URL or vanity handle; return canonical
    https://www.linkedin.com/in/{handle}/."""
    target = target.strip().strip("@")
    if target.startswith("http://") or target.startswith("https://"):
        try:
            parsed = urlparse(target)
        except Exception:
            return None
        netloc = (parsed.netloc or "").lower()
        if netloc != "linkedin.com" and not netloc.endswith(".linkedin.com"):
            return None
        path = parsed.path.rstrip("/")
        m = re.search(r"/in/([^/]+)$", path)
        if not m:
            return None
        handle = m.group(1)
    else:
        handle = target.strip("/")
    if not handle or not _VANITY_RE.fullmatch(handle):
        return None
    return f"{_BASE}/in/{handle}/"


def _job_id_from(target: str) -> Optional[str]:
    """Accept a numeric job id, a /jobs/view/{id} URL, or a /jobs/...
    search URL with currentJobId param. Return the id or None."""
    target = target.strip()
    if not target:
        return None
    if target.isdigit():
        return target
    if target.startswith("http://") or target.startswith("https://"):
        try:
            parsed = urlparse(target)
        except Exception:
            return None
        netloc = (parsed.netloc or "").lower()
        if netloc != "linkedin.com" and not netloc.endswith(".linkedin.com"):
            return None
        m = re.search(r"/jobs/view/(\d+)", parsed.path or "")
        if m:
            return m.group(1)
        # Search URLs may carry currentJobId=... ; parsed.query has no
        # leading "?", so anchor on start-or-ampersand.
        m = re.search(r"(?:^|&)currentJobId=(\d+)", parsed.query or "")
        if m:
            return m.group(1)
        return None
    return None


_THREAD_ID_RE = re.compile(r"[A-Za-z0-9_\-]+")


def _thread_id_from(target: str) -> Optional[str]:
    """Accept a raw thread id (URN-shaped or numeric) or a
    /messaging/thread/{id}/ URL. Returns the id or None."""
    target = target.strip()
    if not target:
        return None
    if target.startswith("http://") or target.startswith("https://"):
        try:
            parsed = urlparse(target)
        except Exception:
            return None
        netloc = (parsed.netloc or "").lower()
        if netloc != "linkedin.com" and not netloc.endswith(".linkedin.com"):
            return None
        m = re.search(r"/messaging/thread/([^/]+)", parsed.path or "")
        if not m:
            return None
        candidate = m.group(1)
    else:
        # LinkedIn thread URNs (e.g. urn:li:fsd_messagingThread:2-XXXX) or
        # short numeric ids both come through as "the part you'd put in
        # the URL". Don't try to be clever — if it looks like a token,
        # accept it.
        candidate = target.strip("/")
    if not candidate or not _THREAD_ID_RE.fullmatch(candidate.replace(":", "_").replace(".", "_")):
        return None
    return candidate


_ACTIVITY_URN_RE = re.compile(r"urn:li:(?:activity|share|ugcPost):\d+", re.IGNORECASE)


def _post_urn_from(target: str) -> Optional[str]:
    """Accept a /feed/update/{urn}/ URL, a /posts/{slug}-{numeric-id}-foo
    permalink, or a raw urn:li:activity:{id} / urn:li:share:{id} /
    urn:li:ugcPost:{id} URN. Returns a normalized urn:li:activity:{id}
    string when possible, or the raw URN when already in a known form.

    Order matters: URLs are validated by netloc BEFORE we extract URN,
    so a URN embedded in a spoofed URL (notlinkedin.com/...urn:...)
    cannot slip through."""
    target = target.strip()
    if not target:
        return None

    # URL form: validate domain first, then extract.
    if target.startswith("http://") or target.startswith("https://"):
        try:
            parsed = urlparse(target)
        except Exception:
            return None
        netloc = (parsed.netloc or "").lower()
        if netloc != "linkedin.com" and not netloc.endswith(".linkedin.com"):
            return None
        path = parsed.path or ""
        m = re.search(r"/feed/update/(urn:li:[^/]+:\d+)", path)
        if m:
            return m.group(1)
        # /posts/{slug}-{numeric-id}-{tracking}/  → reconstruct activity URN
        m = re.search(r"/posts/[^/]*-(\d{15,})-", path)
        if m:
            return f"urn:li:activity:{m.group(1)}"
        return None

    # Bare token form — only accept if the WHOLE input is a URN.
    m = _ACTIVITY_URN_RE.fullmatch(target)
    if m:
        return m.group(0)
    return None


def _normalize_company_url(target: str) -> Optional[str]:
    target = target.strip().strip("@")
    if target.startswith("http://") or target.startswith("https://"):
        try:
            parsed = urlparse(target)
        except Exception:
            return None
        netloc = (parsed.netloc or "").lower()
        if netloc != "linkedin.com" and not netloc.endswith(".linkedin.com"):
            return None
        path = parsed.path.rstrip("/")
        m = re.search(r"/(?:company|school)/([^/]+)$", path)
        if not m:
            # Allow /company/{handle}/about/ etc.
            m = re.search(r"/(?:company|school)/([^/]+)", path)
        if not m:
            return None
        handle = m.group(1)
    else:
        handle = target.strip("/")
    if not handle or not _VANITY_RE.fullmatch(handle):
        return None
    return f"{_BASE}/company/{handle}/"
