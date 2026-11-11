from __future__ import annotations

from packages.core.services.workflow_review_files import _step_files

POSTER, COPY, DRAFT, NOTES, OLD_NOTES = (
    "01M6Z6Z62NYY14S07SXPTBDJXY", "01M6Z6R4KMM0YE9Z527X4XGSGH",
    "01M6Z6QTKFSS0HQ7M2QC0TDXK6", "01M6Z70NVAY567SGQ7WAT5S74J", "01M6Z6S05XN0FPWK732383XJHF",
)
SAVED = [
    {"document_id": DRAFT, "name": "poster.svg"},
    {"document_id": COPY, "name": "promotional-copy.md"},
    {"document_id": OLD_NOTES, "name": "review-notes.md"},
    {"document_id": POSTER},
    {"document_id": NOTES, "name": "creative-review-notes.md"},
]


def test_a_review_shows_the_files_a_step_hands_over_not_everything_it_saved():
    # Staging promotion run: the step saved a draft SVG and an earlier notes file
    # next to the poster, copy and reviewer notes its result names.
    result = {"saved_files": SAVED, "output": {
        "summary": "Saved the finished poster and copy.",
        "copy_document_id": COPY, "poster_document_id": POSTER, "review_notes_document_id": NOTES,
    }}

    shown = {item["document_id"] for item in _step_files(result, entity_id="entity")}

    assert shown == {POSTER, COPY, NOTES}


def test_a_step_that_names_no_files_shows_what_it_saved():
    result = {"saved_files": SAVED, "output": {"summary": "Poster ready"}}

    assert [item["document_id"] for item in _step_files(result, entity_id="entity")] == [
        NOTES, POSTER, OLD_NOTES, COPY, DRAFT,
    ]


async def test_a_handoff_package_brings_the_images_it_links_to_the_review(db_session):
    # Staging weekly post: the step named only a package .md and reviewer notes;
    # the finished PNGs it links were missing from the approval card.
    from types import SimpleNamespace

    from packages.core.models.base import generate_ulid
    from packages.core.models.document import Document
    from packages.core.services.workflow_review_files import workflow_wait_review_files

    entity_id = generate_ulid()
    instagram = Document(entity_id=entity_id, name="instagram.png")
    draft = Document(entity_id=entity_id, name="instagram.svg")
    unrelated = Document(entity_id=entity_id, name="private-draft.png", visibility="private")
    elsewhere = Document(entity_id=generate_ulid(), name="their.png")
    db_session.add_all([instagram, draft, unrelated, elsewhere])
    await db_session.flush()
    package = Document(entity_id=entity_id, name="mapo-tofu-social-package.md", metadata_={"content": (
        f"# Mapo Tofu\n\n![Instagram](/viewer/{instagram.id})\n\n[Instagram — PNG](/viewer/{instagram.id})\n\n"
        f"![Unrelated](/viewer/{unrelated.id})\n\n"
        f"![Theirs](/viewer/{elsewhere.id})\n")})
    notes = Document(entity_id=entity_id, name="review-notes.md", metadata_={"content": "Checked."})
    db_session.add_all([package, notes])
    await db_session.commit()
    run = SimpleNamespace(entity_id=entity_id, definition_snapshot={"nodes": []}, step_results={"produce": {
        "status": "completed", "completed_at": "2026-11-09T08:36:00Z",
        "saved_files": [{"document_id": draft.id}, {"document_id": instagram.id},
                        {"document_id": package.id}, {"document_id": notes.id}],
        "output": {"deliverable_document_id": package.id, "review_notes_document_id": notes.id},
    }})

    files = await workflow_wait_review_files(db_session, run, step_id="approve")

    assert [item["document_id"] for item in files] == [package.id, instagram.id, notes.id]
