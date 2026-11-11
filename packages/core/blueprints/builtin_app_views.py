"""The curated App page of every built-in Blueprint, in one place.

Each entry is a portable ``app_view`` (workflow slugs, scheduled job ids and
service keys, never runtime ids). A Blueprint whose App declares its own data views
(``data_sources``/``components``, such as the music release studio) keeps its
whole App in its config instead. The Blueprint JSON configs carry exactly the
normalised form of these, the generator scripts write them on export, and a
test keeps the two equal. Entries a page does not name in ``order`` or
``hidden`` follow the App's default, which already leaves out services a flow
runs and plumbing such as the metric assessment.
"""
from __future__ import annotations

from typing import Any

from packages.core.services.workspace_app_view import normalize_app_view_config


def _studio(eyebrow: str, title: str, description: str, flow: str, label: str,
            line: str | None = None) -> dict[str, Any]:
    """A one-flow studio: its flow is the main function, under a short action name.

    ``line`` sits under that name; the Flow's own description is written for
    builders and runs long, so the page never shows it.
    """
    return {
        "eyebrow": eyebrow, "title": title, "description": description,
        "hero": f"workflow:{flow}", "labels": {f"workflow:{flow}": label},
        **({"descriptions": {f"workflow:{flow}": line}} if line else {}),
    }


def _with_variants(view: dict[str, Any], flow: str, field: str, variants: tuple[tuple[str, str, str, str], ...],
                   also: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    """Offer one Flow as several entries, each opening it with ``field`` prefilled.

    ``variants`` are (id, title, description, prefilled text); ``also`` gives a
    variant more prefilled inputs by its id (e.g. the style a deck type suits).
    The Flow itself stays the main function for anything that fits no variant.
    """
    keys = [f"action:{variant_id}" for variant_id, *_ in variants]
    return {
        **view,
        "order": [f"workflow:{flow}", *keys],
        "entries": {key: {"title": title} for key, (_, title, _, _) in zip(keys, variants)},
        "descriptions": {**view.get("descriptions", {}),
                         **{key: description for key, (_, _, description, _) in zip(keys, variants)}},
        "targets": {key: f"workflow:{flow}" for key in keys},
        "presets": {key: {field: text, **(also or {}).get(variant_id, {})}
                    for key, (variant_id, _, _, text) in zip(keys, variants)},
    }


def _agent_studio(eyebrow: str, title: str, description: str, service: str, agent: str, label: str, line: str,
                  prompt: str, kinds: tuple[tuple[str, str, str, str], ...],
                  form: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """A studio run by one Agent: its service is the main function, each kind an entry to that Agent.

    ``kinds`` are (id, title, description, request): every entry asks only what
    the work is about; the Agent assumes sensible defaults for the rest. ``form``
    is what the page asks before it opens the Agent; by default a topic and a
    source file.
    """
    hero = f"service:{service}"
    keys = [f"action:{kind_id}" for kind_id, *_ in kinds]
    form = form or [
        {"key": "topic", "label": "What is it about?", "type": "long_text", "required": True,
         "placeholder": "Subject, audience, language and style. Anything you leave out gets a sensible default."},
        {"key": "source", "label": "Source file (optional)", "type": "file", "required": False},
    ]
    return {
        "eyebrow": eyebrow, "title": title, "description": description,
        "hero": hero, "order": [hero, *keys], "labels": {hero: label},
        "prompts": {hero: prompt, **{key: text for key, (_, _, _, text) in zip(keys, kinds)}},
        "forms": {key: form for key in (hero, *keys)},
        "entries": {key: {"title": name} for key, (_, name, _, _) in zip(keys, kinds)},
        "descriptions": {hero: line, **{key: text for key, (_, _, text, _) in zip(keys, kinds)}},
        "targets": {key: f"agent:{agent}" for key in keys},
    }


# Each made or started episode opens the series' Remake with itself chosen;
# the entry lives on the episode, never as a tile of its own.
_SERIES_REMAKE = ("action:series-remake", "episode_id", "Remake")
# A planned episode opens Make this episode with its series and code chosen;
# the entry is never a tile of its own.
_SERIES_MAKE = ("action:series-make", "Make this episode", {"series_id": "parent", "episode_code": "code"})
# Next episode asks which series first, Remake which episode, Make which
# series and planned episode; other entries never ask for either.
_SERIES_ASKS = {"action:series-next": ["series_id"], "action:series-remake": ["episode_id"],
                "action:series-make": ["series_id", "episode_code"]}


def project_tree_view(flow: str, *, parent: tuple[str, str], child: tuple[str, str],
                      about: tuple[tuple[str, str], ...], child_fields: tuple[tuple[str, str], ...],
                      stages: dict[str, str], done: str,
                      references: tuple[tuple[str, str], ...] = (("voices", "Voices"),),
                      remake: tuple[str, str, str] | None = None,
                      make: tuple[str, str, dict[str, str]] | None = None,
                      plan_path: str = "episodes", child_flow: str | None = None) -> dict[str, Any]:
    """One App section for a project and the projects inside it.

    Pick a project (a series, a campaign) to see what it is about, how many of
    its child projects are done, each child's latest results as one pack (a
    picture beside its recording, its script scrolling under it; working files
    never show) and the references every child reuses (its voices, its named
    images such as a key visual, one block per kind in ``references``). Any Blueprint whose Flow makes parent and child projects
    with ``folder`` reuses it with its own project types and wording.

    The children follow the parent's plan (its ``plan_path`` list), a planned
    child a card before its run starts. ``remake`` (action, input, label) puts
    that App action on each started child, opened with the child chosen;
    ``make`` (action, label, inputs) puts one on each planned child, its Flow
    inputs filled from the card's ``parent`` or plan ``code``. ``child_flow``
    is the Flow that makes the children when it is not ``flow``, which makes
    the parents. The App designer offers the same layout to Workspaces it
    designs.
    """
    parent_type, parent_label = parent
    child_type, child_label = child
    producer = [f"workflow_definition:{flow}"]
    child_producer = [f"workflow_definition:{child_flow or flow}"]
    return {
        "data_sources": {
            "parents": {"type": "workflow_projects", "project_type": parent_type, "producer_workflows": producer,
                        "fields": [["title"], *([key] for key, _ in about), ["project_root"]],
                        "label_fields": [["title"]]},
            "children": {"type": "related_projects", "parent": "parents", "project_type": child_type,
                         "producer_workflows": child_producer,
                         "fields": [["code"], ["title"], *([key] for key, _ in child_fields)],
                         "label_fields": [["code"], ["title"]]},
            # The whole plan, made or not: a child project exists only once its run starts.
            "plan": {"type": "project_records", "parent": "parents", "record_path": [plan_path]},
            **{f"{kind}_references": {"type": "project_references", "parent": "parents", "reference_kind": kind}
               for kind, _ in references},
            "results": {"type": "project_files", "parent": "parents", "results_only": True},
        },
        "components": [{
            "type": "data_view", "schema_version": 1, "key": "project_tree", "title": parent_label,
            # Each parent is a card (its key visual, how many children are done); one opens it.
            "context": {"kind": "project", "source": "parents", "label": parent_label, "display": "cards",
                        "progress": "child_rollup",
                        **({"cover": "images_references"} if any(kind == "images" for kind, _ in references) else {})},
            "blocks": [
                {"key": "about", "type": "summary", "source": "parents", "title": "About", "fields": [
                    {"label": label, "field": [key], "hide_when_empty": True} for key, label in about]},
                {"key": "child_rollup", "type": "project_rollup", "source": "children", "title": "Progress",
                 "count_by_stage": True, "plan": "plan",
                 "completion": {"stages": [done], "label": stages[done]}, "stage_labels": stages},
                # Each child of the plan, in its order, with its current results as
                # one pack; working files and earlier takes never show.
                {"key": "results", "type": "packs", "source": "results", "title": child_label,
                 "limit": 100, "stage_labels": {"planned": "Planned", **stages}, "plan": "plan",
                 **({"item_action": {"action": remake[0], "input": remake[1], "label": remake[2],
                                     "stages": list(stages)}} if remake else {}),
                 **({"plan_action": {"action": make[0], "label": make[1], "inputs": dict(make[2])}}
                    if make else {})},
                *({"key": f"{kind}_references", "type": "references", "source": f"{kind}_references", "title": label}
                  for kind, label in references),
            ],
        }],
    }


def content_calendar_view(*, key: str, title: str, period_label: str, collection_title: str,
                          period: tuple[str, dict[str, Any]], slots: tuple[str, dict[str, Any]],
                          stage_labels: dict[str, str], attention_stages: list[str],
                          rollup: dict[str, Any], calendar: dict[str, Any], summary: dict[str, Any],
                          group_titles: dict[str, str], documents: tuple[dict[str, Any], ...] = (),
                          issues: dict[str, Any] | None = None, upcoming: dict[str, Any] | None = None,
                          published: dict[str, Any] | None = None, automations: dict[str, Any] | None = None,
                          sources: dict[str, dict[str, Any]] | None = None,
                          description: str | None = None) -> dict[str, Any]:
    """One App section for a plan of dated content: a campaign, a week.

    Pick a period (a project) to see its progress and plan documents in its
    header, every planned item on a calendar on its date (a time, or a whole
    day), what was published with its results, and the period's details.
    Groups, in order: ``overview`` (the rollup, the ``documents`` placed there
    and the ``issues`` list of items at an attention stage), ``timeline`` (the
    ``upcoming`` list and the calendar), ``results`` (``published``) and
    ``reference`` (the period ``summary`` the header reads, the documents
    placed there and the period's ``automations``). A group with no block is
    left out.

    ``period`` and ``slots`` are (source name, source): the period's
    projects and the dated items inside them, ordered by the calendar's date
    field. ``issues`` and ``upcoming`` list the same items at some stages (an
    issue at the attention stages), each with its own source fields.
    ``calendar`` may carry an ``item_action`` (script this slot, remake this
    one). ``sources`` adds what documents, published records and automations
    read. The music release campaign and the Instagram weekly plan are both
    this layout.
    """
    period_name, period_source = period
    slots_name, slots_source = slots
    date_field = slots_source["order"]["field"]
    data_sources: dict[str, Any] = {period_name: period_source, slots_name: slots_source, **(sources or {})}

    def listing(spec: dict[str, Any], stages: list[str]) -> dict[str, Any]:
        # The same items as the calendar, at some stages only.
        data_sources[spec["source"]] = {**slots_source, "fields": spec["source_fields"], "stages": list(stages)}
        return {"key": spec["key"], "type": "progress", "source": spec["source"], "title": spec["title"],
                "fields": spec["fields"], "limit": spec.get("limit", 5), "stage_labels": stage_labels}

    def document(spec: dict[str, Any]) -> dict[str, Any]:
        return {"key": spec["key"], "type": "documents", "source": spec["source"], "title": spec["title"],
                "limit": spec.get("limit", 1), **({"empty_message": spec["empty_message"]} if spec.get("empty_message") else {})}

    rollup_block = {"key": rollup["key"], "type": "project_rollup", "source": slots_name, "title": rollup["title"],
                    "count_by_stage": True, "completion": {"label": rollup["done_label"], "stages": rollup["done_stages"]},
                    "first": {"stages": rollup["working_stages"]}, "stage_labels": rollup.get("stage_labels") or stage_labels,
                    **({"empty_message": rollup["empty_message"]} if rollup.get("empty_message") else {})}
    calendar_block = {"key": calendar["key"], "type": "progress", "source": slots_name, "title": calendar["title"],
                      "fields": calendar["fields"], "limit": calendar.get("limit", 100), "stage_labels": stage_labels,
                      **({"item_action": calendar["item_action"]} if calendar.get("item_action") else {})}
    summary_block = {"key": summary["key"], "type": "summary", "source": period_name, "title": summary["title"],
                     "fields": summary["fields"], "limit": 1}
    groups = {
        "overview": [rollup_block, *(document(spec) for spec in documents if spec.get("group", "overview") == "overview"),
                     *([listing(issues, attention_stages)] if issues else [])],
        "timeline": [*([listing(upcoming, upcoming["stages"])] if upcoming else []), calendar_block],
        "results": [published] if published else [],
        "reference": [summary_block, *(document(spec) for spec in documents if spec.get("group") == "reference"),
                      *([{"key": automations["key"], "type": "progress", "source": automations["source"],
                          "title": automations["title"]}] if automations else [])],
    }
    return {
        "data_sources": data_sources,
        "components": [{
            "type": "data_view", "schema_version": 1, "key": key, "title": title, "description": description,
            "context": {"kind": "project", "source": period_name, "label": period_label},
            "blocks": [block for blocks in groups.values() for block in blocks],
            "display": {
                "layout": "stack", "hidden": False,
                "groups": [{"key": name, "title": group_titles[name], "blocks": [block["key"] for block in blocks]}
                           for name, blocks in groups.items() if blocks],
                # The calendar draws the items; an automation list is for settings only.
                "hidden_blocks": [*([automations["key"]] if automations else []), calendar["key"]],
                "calendar": {"block": calendar["key"], "date_field": list(date_field), "rollup_block": rollup["key"],
                             "context_block": summary["key"], "attention_stages": list(attention_stages),
                             "collection_title": collection_title},
            },
        }],
    }


# Carousel kinds the Carousel Producer offers, each with its own page structure.
_CAROUSEL_AGENT_KINDS: tuple[tuple[str, str, str, str], ...] = (
    ("carousel-howto", "How-to steps", "One step per page, in order.",
     "Make a how-to carousel: a cover with the outcome, one step per page with a short instruction, "
     "and a final page with the result."),
    ("carousel-tips", "Tips list", "A handful of tips, one per page.",
     "Make a tips carousel: a cover page, one tip per page with a short explanation, and a closing page."),
    ("carousel-myths", "Myth vs fact", "Common mistakes and what is actually true.",
     "Make a myth vs fact carousel: a cover, one myth and the fact on each page, and a closing takeaway."),
    ("carousel-product", "Product highlights", "What a product does, one feature per page.",
     "Make a product highlights carousel: a cover with the main benefit, one feature per page with what it "
     "does for the reader, and a page with where to get it."),
    ("carousel-event", "Event announcement", "What, when, where and how to join.",
     "Make an event announcement carousel: the event and date, what happens, who it is for, and how to sign up."),
    ("carousel-takeaways", "Article takeaways", "The key points of an article or report.",
     "Make a takeaways carousel from an article or report: the title, one key point per page, and what to do "
     "with it. Use the uploaded or named source."),
)


# Deck types the presentation studio offers. Each fixes the page structure; the
# bracketed parts are for the person to fill in. Each also suggests the
# designed styles that suit it (DECK_TYPE_STYLES); the person can pick another.
_DECK_TYPES: tuple[tuple[str, str, str, str], ...] = (
    ("deck-product", "Product or service intro", "Who it is for, what it solves, how it works, what to do next.",
     "Product introduction for [product or service], for [audience], in [language]. 8–10 slides: who it is for, "
     "the problem, how it works, key features, proof or examples, pricing or packages, next step."),
    ("deck-pitch", "Pitch deck", "Problem, solution, market, traction, team and the ask.",
     "Pitch deck for [company], for [investors or partners], in [language]. 10–12 slides: problem, solution, "
     "product, market size, business model, traction, competition, team, the ask and use of funds."),
    ("deck-report", "Work report", "Goals, results, numbers, issues and next steps.",
     "Work report on [project or period], for [team or management], in [language]. 6–10 slides: goals, what we "
     "did, results with numbers, what did not work, lessons, next steps and asks."),
    ("deck-training", "Training deck", "Learning goals, chapters, examples and a recap.",
     "Training deck on [topic], for [learners], in [language]. 10–14 slides: learning goals, agenda, one slide per "
     "concept with an example, a practice exercise, common mistakes, recap."),
    ("deck-event", "Event or workshop intro", "Who should come, what happens, when and how to join.",
     "Introduction to [event or workshop], for [audience], in [language]. 4–6 slides: what it is, who it is for, "
     "what happens, schedule and place, what to bring, how to sign up."),
    ("deck-proposal", "Proposal", "Background, options, recommendation and timeline.",
     "Proposal for [decision or project], for [decision makers], in [language]. 6–8 slides: background, the problem, "
     "options compared, recommendation, plan and timeline, cost and risks, the decision needed."),
)


# Each deck type: the kind the flow is told (deck_type) and the designed styles
# that suit it, best first. The first is preselected; the style picker shows
# these first, with sample slides of that kind, and the others under "More".
DECK_TYPE_STYLES: dict[str, tuple[str, tuple[str, ...]]] = {
    "deck-product": ("product", ("stripe", "cobalt", "photo", "pop", "gradient")),
    "deck-pitch": ("pitch", ("velvet", "keynote", "stripe", "gradient", "neon", "aurora")),
    "deck-report": ("report", ("consulting", "swiss", "mono", "editorial", "neon")),
    "deck-training": ("training", ("pop", "periwinkle", "cobalt", "editorial", "gradient")),
    "deck-event": ("event", ("starlit", "velvet", "photo", "neon", "pop")),
    "deck-proposal": ("proposal", ("mono", "bloom", "consulting", "editorial", "starlit")),
}
_DECK_TYPE_STYLES: dict[str, dict[str, str]] = {
    variant: {"style": styles[0], "deck_type": kind} for variant, (kind, styles) in DECK_TYPE_STYLES.items()
}


# Video series kinds: each entry asks only what the work is about.
_VIDEO_TYPES: tuple[tuple[str, str, str, str], ...] = (
    ("series-new", "New series", "Plan every episode of a video series, then make episode 1.",
     "Video series about [subject], for [viewers], in [language]. [8] episodes of about [60] seconds, with [a host / "
     "no host]: plan them all, then make episode 1."),
    ("series-next", "Next episode", "Make the next episode of a series, with the same voice, host and look.",
     "Next episode. Add what it should cover, or ask for more episodes (say how many in all), if you like."),
    ("series-remake", "Remake an episode", "Make an episode again, in its series' voice, host and look.",
     "Make this episode again. Say what should change, if you like."),
    ("series-make", "Make this episode", "Make one planned episode of a series, with the same voice, host and look.",
     "Make this episode. Say what it should cover, if you like."),
    ("video-explainer", "Explainer video", "One idea explained in about a minute.",
     "Explainer video about [topic], for [viewers], in [language]. About 60 seconds: a hook, the idea in three "
     "steps, one takeaway."),
    ("video-story", "Short story video", "A short narrated story told over pictures.",
     "Short story video about [subject], for [viewers], in [language]. About 90 seconds: setting, turning point, "
     "ending."),
)


# Podcast episode types: one narrator, 20 seconds to 10 minutes, no music.
_PODCAST_TYPES: tuple[tuple[str, str, str, str], ...] = (
    ("series-new", "New series", "Plan every episode of a series, then make episode 1.",
     "Series about [subject], for [listeners], in [language]. [12] episodes of about [4] minutes, in order: "
     "plan them all, then make episode 1."),
    ("series-next", "Next episode", "Make the next episode of a series, in the same voice.",
     "Next episode. Add what it should cover, or ask for more episodes (say how many in all), if you like."),
    ("series-remake", "Remake an episode", "Make an episode again, in its series' voice.",
     "Make this episode again. Say what should change, if you like."),
    ("series-make", "Make this episode", "Make one planned episode of a series, in the same voice.",
     "Make this episode. Say what it should cover, if you like."),
    ("episode-explainer", "Explainer episode", "One topic explained clearly from start to finish.",
     "Explainer episode about [topic], for [listeners], in [language]. About 4 minutes: a hook, what it is, why it "
     "matters, how it works with one example, a short recap."),
    ("episode-roundup", "News roundup", "The week's few stories that matter, briefly.",
     "News roundup on [field] for [period], for [listeners], in [language]. About 3 minutes: a one-line intro, "
     "[3–5] stories with what happened and why it matters, a closing line."),
    ("episode-digest", "Book or article digest", "The ideas of a book or article in one listen.",
     "Digest of [book or article], for [listeners], in [language]. About 5 minutes: what it is about, the [3] key "
     "ideas with one example each, what to take away. Use the uploaded source if provided."),
    ("episode-story", "Story episode", "A short narrated story with a beginning, turn and end.",
     "Story episode about [subject or event], for [listeners], in [language]. About 5 minutes: set the scene, the "
     "turning point, how it ends, one line on what it means."),
    ("episode-update", "Announcement", "News from your team or product, in two minutes.",
     "Announcement episode for [company or team] about [news], for [customers or members], in [language]. "
     "About 2 minutes: what is new, who it helps, what changes for them, what to do next."),
    ("episode-tip", "Daily tip", "One practical tip in about a minute.",
     "Daily tip on [topic], for [listeners], in [language]. About 60–90 seconds: the tip in one sentence, why it "
     "works, how to try it today."),
)

# GIF styles the page offers as pictures: (key, label, picture on the page, how the Agent renders it).
GIF_STYLES: tuple[tuple[str, str, str, str], ...] = (
    ("illustration", "Illustration", "/blueprints/styles/gif/illustration.webp",
     "clean outlined illustration with flat colour and soft shading"),
    ("pixel-art", "Pixel art", "/blueprints/styles/gif/pixel-art.webp",
     "16-bit pixel art: chunky square pixels, a limited palette, no anti-aliasing"),
    ("clay", "Clay", "/blueprints/styles/gif/clay.webp",
     "soft 3D claymation: matte surfaces, gentle highlights, fingerprint-like texture"),
    ("minimal", "Minimal", "/blueprints/styles/gif/minimal.webp",
     "flat shapes in a few colours, no outlines or texture"),
    ("watercolor", "Watercolor", "/blueprints/styles/gif/watercolor.webp",
     "soft watercolour washes on paper, with loose edges and visible grain"),
    ("anime", "Anime", "/blueprints/styles/gif/anime.webp",
     "anime cel style: bold dark outlines, two-tone cel shading and large expressive eyes"),
    ("sticker", "Sticker", "/blueprints/styles/gif/sticker.webp",
     "die-cut sticker: bold flat shapes with a thick white border and a soft drop shadow"),
    ("halftone", "Halftone", "/blueprints/styles/gif/halftone.webp",
     "vintage print: flat colours with halftone dot shading, like a retro poster"),
    ("papercut", "Paper cut", "/blueprints/styles/gif/papercut.webp",
     "layered paper-cut collage with soft shadows between the paper edges"),
)

# GIF kinds the GIF Animation Director starts from; each names one subject and one loop.
_GIF_KINDS: tuple[tuple[str, str, str, str], ...] = (
    ("gif-reaction", "Character reaction", "A wave, a nod or a little celebration.",
     "A cheerful small cat waves one paw, then returns to its starting pose. A clean repeating loop."),
    ("gif-brand", "Brand moment", "A small animated scene in your colors.",
     "A coffee cup releases a gentle curl of steam that loops smoothly against a quiet background."),
    ("gif-scene", "Looping scene", "A calm moment that keeps moving.",
     "A glowing paper lantern sways gently in a quiet night scene, returning to its starting position."),
)

BUILTIN_APP_VIEWS: dict[str, dict[str, Any]] = {
    # The GIF Studio is run by its Agent, which generates the GIF itself in the
    # conversation; each kind is a starting point that opens that Agent.
    "creative-gif-v1": _agent_studio(
        "GIF Studio", "Make a little motion",
        "Animated GIFs from a description; every version is kept.",
        "creative.gif", "creative-gif-v1-director", "Make a GIF",
        "One subject and one small movement that loops.",
        "Make an animated GIF.", _GIF_KINDS,
        form=[{"key": "brief", "label": "What should move?", "type": "long_text", "required": True,
               "placeholder": "Subject, one small movement, and any colour notes. "
                              "Anything you leave out gets a sensible default."},
              {"key": "style", "label": "Style", "type": "picture_choice", "required": False,
               "options": [key for key, *_ in GIF_STYLES],
               "option_labels": [label for _, label, *_ in GIF_STYLES],
               "option_images": [picture for _, _, picture, _ in GIF_STYLES]},
              {"key": "character", "label": "Character picture (optional)", "type": "file", "required": False},
              {"key": "style_reference", "label": "Your own style picture (optional)", "type": "file", "required": False}]),
    "creative-presentation-v1": _with_variants(_studio(
        "Presentation studio", "Make a presentation",
        "An editable deck in your style, with PDF and speaker notes.",
        "produce-presentation-v1", "Make a deck", "From a brief and your files to a finished deck."), "produce-presentation-v1", "brief", _DECK_TYPES,
        _DECK_TYPE_STYLES),
    # The Carousel studio is run by its Agent with a production Skill; the Flow
    # version (creative-carousel-v1) is retired from the catalogue.
    "creative-carousel-agent-v1": _agent_studio(
        "Carousel studio", "Make an illustrated carousel",
        "Illustrated pages with captions, in one consistent style.",
        "creative.carousel", "creative-carousel-agent-v1-producer", "Make a carousel",
        "Describe it, then ask for changes in plain words.",
        "Make an illustrated carousel.", _CAROUSEL_AGENT_KINDS),
    "creative-dubbing-v1": _studio(
        "Dubbing studio", "Dub a video into another language",
        "Your video with a new-language voiceover and subtitles.",
        "produce-dubbing-v1", "Dub a video", "Upload a narrated video and pick the language."),
    "creative-handbook-v1": _studio(
        "Handbook studio", "Make an illustrated guide",
        "An illustrated PDF guide with contents and sections.",
        "produce-handbook-v1", "Make a guide", "Turn your source material into a guide."),
    "creative-podcast-v1": {**_with_variants(_studio(
        "Podcast studio", "Make a podcast episode",
        "A narrated, mastered MP3 episode with an editable script.",
        "produce-podcast-v1", "Make an episode", "Start from a topic or your notes."), "produce-podcast-v1", "brief", _PODCAST_TYPES,
        {"series-new": {"series": "new"}, "series-next": {"series": "next"}, "series-remake": {"series": "next"},
         "series-make": {"series": "next"}}),
        "asks": _SERIES_ASKS, "hidden": [_SERIES_MAKE[0], _SERIES_REMAKE[0]],
        **project_tree_view(
            "produce-podcast-v1", parent=("podcast_series", "Series"), child=("podcast_episode", "Episodes"),
            about=(("premise", "Premise"),),
            child_fields=(("synopsis", "Synopsis"),),
            stages={"in_production": "Making", "made": "Ready"}, done="made",
            references=(("voices", "Voices"), ("images", "Visuals")), remake=_SERIES_REMAKE, make=_SERIES_MAKE)},
    "creative-video-v1": {**_with_variants(_studio(
        "Video series studio", "Make a video series",
        "Short narrated vertical videos, one episode at a time.",
        "produce-video-v1", "Make a video", "Same voice, host and look in every episode."), "produce-video-v1", "brief", _VIDEO_TYPES,
        {"series-new": {"series": "new"}, "series-next": {"series": "next"}, "series-remake": {"series": "next"},
         "series-make": {"series": "next"}}),
        "asks": _SERIES_ASKS, "hidden": [_SERIES_MAKE[0], _SERIES_REMAKE[0]],
        **project_tree_view(
            "produce-video-v1", parent=("video_series", "Series"), child=("video_episode", "Episodes"),
            about=(("premise", "Premise"),), child_fields=(("synopsis", "Synopsis"),),
            stages={"in_production": "Making", "made": "Ready"}, done="made",
            references=(("voices", "Voices"), ("images", "Visuals")), remake=_SERIES_REMAKE, make=_SERIES_MAKE)},
    "recording-shorts-v1": _studio(
        "Shorts studio", "Turn a long video into shorts",
        "One to five subtitled portrait clips from a long video.",
        "produce-recording-shorts-v1", "Make shorts", "Upload a narrated video; its audio stays as it is."),
    "recording-tutorial-v1": _studio(
        "Tutorial studio", "Turn a screen recording into a tutorial",
        "A trimmed, subtitled widescreen tutorial with chapters.",
        "produce-recording-tutorial-v1", "Make a tutorial", "Upload a narrated screen recording."),
}


# Carousel types: 3–6 portrait pages, one idea per page.
_CAROUSEL_TYPES: tuple[tuple[str, str, str, str], ...] = (
    ("carousel-howto", "How-to steps", "One step per page, in order.",
     "How-to carousel on [task], for [audience], in [language]. [5] pages: a cover with the outcome, one step per "
     "page with a short instruction, a final page with the result. Style: [style]."),
    ("carousel-tips", "Tips list", "A handful of tips, one per page.",
     "Tips carousel: [number] tips for [goal], for [audience], in [language]. A cover page, one tip per page with "
     "a short explanation, a closing page. Style: [style]."),
    ("carousel-myths", "Myth vs fact", "Common mistakes and what is actually true.",
     "Myth vs fact carousel about [topic], for [audience], in [language]. [4] pages: a cover, then one myth and the "
     "fact on each page, a closing takeaway. Style: [style]."),
    ("carousel-product", "Product highlights", "What a product does, one feature per page.",
     "Product highlights carousel for [product], for [audience], in [language]. [5] pages: a cover with the main "
     "benefit, one feature per page with what it does for the reader, a page with where to get it. Style: [style]."),
    ("carousel-event", "Event announcement", "What, when, where and how to join.",
     "Event announcement carousel for [event], for [audience], in [language]. [4] pages: the event and date, what "
     "happens, who it is for, how to sign up. Style: [style]."),
    ("carousel-takeaways", "Article takeaways", "The key points of an article or report.",
     "Takeaways carousel from [article or report], for [audience], in [language]. [5] pages: the title, one key "
     "point per page, what to do with it. Use the uploaded source if provided. Style: [style]."),
)


# Pages of retired Blueprints whose configs stay as source examples and keep
# serving existing installs. They are not listed, so they are not in
# BUILTIN_APP_VIEWS; their generators still write the page they shipped with.
RETAINED_APP_VIEWS: dict[str, dict[str, Any]] = {
    "creative-carousel-v1": _with_variants(_studio(
        "Carousel studio", "Make an illustrated carousel",
        "Turn a brief into a series of finished illustrated pages with caption copy, in one consistent style.",
        "produce-carousel-v1", "Make a carousel"), "produce-carousel-v1", "brief", _CAROUSEL_TYPES),
}


# Each entry's glyph (a name from APP_ENTRY_ICONS), so a page does not depend on
# what its entry names happen to suggest. Series entries read the same everywhere.
_SERIES_ICONS = {"action:series-new": "sparkles", "action:series-next": "refresh",
                 "action:series-remake": "refresh", "action:series-make": "play"}
_GIF_ICONS = {
    "service:creative.gif": "sparkles", "action:gif-reaction": "heart", "action:gif-brand": "star",
    "action:gif-scene": "refresh",
}
_CAROUSEL_ICONS = {
    "action:carousel-howto": "checklist", "action:carousel-tips": "bolt", "action:carousel-myths": "scale",
    "action:carousel-product": "box", "action:carousel-event": "calendar", "action:carousel-takeaways": "document",
}
_APP_ICONS: dict[str, dict[str, str]] = {
    "creative-gif-v1": _GIF_ICONS,
    "creative-presentation-v1": {
        "workflow:produce-presentation-v1": "presentation", "action:deck-product": "box", "action:deck-pitch": "rocket",
        "action:deck-report": "report", "action:deck-training": "academic", "action:deck-event": "calendar",
        "action:deck-proposal": "clipboard",
    },
    "creative-carousel-agent-v1": {"service:creative.carousel": "palette", **_CAROUSEL_ICONS},
    "creative-carousel-v1": {"workflow:produce-carousel-v1": "palette", **_CAROUSEL_ICONS},
    "creative-dubbing-v1": {"workflow:produce-dubbing-v1": "microphone"},
    "creative-handbook-v1": {"workflow:produce-handbook-v1": "bookmark"},
    "creative-podcast-v1": {
        "workflow:produce-podcast-v1": "audio", **_SERIES_ICONS, "action:episode-explainer": "presentation",
        "action:episode-roundup": "report", "action:episode-digest": "document", "action:episode-story": "bookmark",
        "action:episode-update": "megaphone", "action:episode-tip": "bolt",
    },
    "creative-video-v1": {
        "workflow:produce-video-v1": "play", **_SERIES_ICONS, "action:video-explainer": "presentation",
        "action:video-story": "bookmark",
    },
    "recording-shorts-v1": {"workflow:produce-recording-shorts-v1": "play"},
    "recording-tutorial-v1": {"workflow:produce-recording-tutorial-v1": "academic"},
}


def _outputs(*shelves: tuple[str, str, str | None, str]) -> list[dict[str, Any]]:
    """The default page with its outputs split into named shelves.

    Each shelf is (id, title, flow slug or None for the whole Workspace, view):
    ``gallery`` for what is looked at or played (each tile takes its file's
    shape), ``list`` for what is read.
    """
    return [
        {"id": "connections", "block": "connections"}, {"id": "queue", "block": "queue"},
        {"id": "actions", "block": "actions"},
        *({"id": shelf_id, "block": "outputs", "title": title, "view": view,
           "source": f"workflow:{flow}" if flow else "workspace"} for shelf_id, title, flow, view in shelves),
    ]


# What each page keeps its made work under, so decks, episodes and articles do
# not mix in one recent-files strip.
_APP_OUTPUTS: dict[str, list[dict[str, Any]]] = {
    "creative-gif-v1": _outputs(("animations", "Your GIFs", None, "gallery")),
    "creative-presentation-v1": _outputs(("decks", "Decks", "produce-presentation-v1", "gallery")),
    "creative-carousel-agent-v1": _outputs(("carousels", "Carousels", None, "gallery")),
    "creative-carousel-v1": _outputs(("carousels", "Carousels", "produce-carousel-v1", "gallery")),
    "creative-dubbing-v1": _outputs(("dubbed", "Dubbed videos", "produce-dubbing-v1", "gallery")),
    "creative-handbook-v1": _outputs(("guides", "Guides", "produce-handbook-v1", "gallery")),
    "creative-podcast-v1": _outputs(("episodes", "Episodes", "produce-podcast-v1", "gallery")),
    "creative-video-v1": _outputs(("videos", "Videos", "produce-video-v1", "gallery")),
    "recording-shorts-v1": _outputs(("shorts", "Shorts", "produce-recording-shorts-v1", "gallery")),
    "recording-tutorial-v1": _outputs(("tutorials", "Tutorials", "produce-recording-tutorial-v1", "gallery")),
}
for _slug in {*_APP_ICONS, *_APP_OUTPUTS}:
    _views = BUILTIN_APP_VIEWS if _slug in BUILTIN_APP_VIEWS else RETAINED_APP_VIEWS
    _views[_slug] = {**_views[_slug], **({"icons": _APP_ICONS[_slug]} if _slug in _APP_ICONS else {}),
                     **({"sections": _APP_OUTPUTS[_slug]} if _slug in _APP_OUTPUTS else {})}


def builtin_app_view(slug: str) -> dict[str, Any] | None:
    """The normalised App page a built-in Blueprint ships, or None if it has none."""
    value = BUILTIN_APP_VIEWS.get(slug, RETAINED_APP_VIEWS.get(slug))
    return normalize_app_view_config(value) if value is not None else None
