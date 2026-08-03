"""Chunk-level RAG: retrieval quality and scale improvements over whole-document
embedding + the 25-document, no-index lexical scan it replaced.

RAG used to embed one whole document into a single averaged vector — a fact
three pages in got no more representation than the title — and its lexical
fallback scanned at most 25 documents per query, unindexed, and could not
meaningfully tokenize CJK text at all (Postgres's built-in text-search parsers
don't segment Chinese into words). document_chunks + pg_trgm fix both.
"""
from __future__ import annotations

import os

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from packages.core.models.base import Base, generate_ulid
import packages.core.models  # noqa: F401

EMBEDDING_DIMENSIONS = int(os.getenv("EMBEDDING_DIMENSIONS", "1024") or 1024)
TEST_DATABASE_URL = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://manor:manor_secret@localhost:5434/manor_test",
)


@pytest_asyncio.fixture
async def db_session():
    # Plain pooled engine — NOT NullPool. Confirmed by hand (30/30 clean vs.
    # intermittent sqlalchemy.orm.exc.StaleDataError on an unrelated later
    # commit within the SAME AsyncSession) that pairing NullPool with a
    # session that commits more than once causes that StaleDataError. Whatever
    # the exact mechanism, it is specific to NullPool + multi-commit sessions,
    # not to this suite's drop_all/create_all cycles — matching that
    # production's own session factories never use NullPool either.
    engine = create_async_engine(TEST_DATABASE_URL, echo=False)
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    has_vector = False
    try:
        async with engine.begin() as conn:
            await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            has_vector = True
    except Exception:
        pass

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        if has_vector:
            await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
        await conn.run_sync(Base.metadata.create_all)
        if has_vector:
            await conn.execute(
                text(f"ALTER TABLE documents ADD COLUMN IF NOT EXISTS embedding vector({EMBEDDING_DIMENSIONS})")
            )
            await conn.execute(
                text(
                    "ALTER TABLE document_chunks ADD COLUMN IF NOT EXISTS "
                    f"embedding vector({EMBEDDING_DIMENSIONS})"
                )
            )
        await conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_document_chunks_content_trgm "
                "ON document_chunks USING GIN (content gin_trgm_ops)"
            )
        )

    async with session_factory() as session:
        yield session, has_vector

    await engine.dispose()


async def _make_document(session, entity_id: str, name: str) -> str:
    from packages.core.models.document import Document

    doc_id = generate_ulid()
    session.add(Document(id=doc_id, entity_id=entity_id, name=name, vector_status="ready"))
    await session.flush()
    return doc_id


async def _insert_chunk(session, doc_id: str, index: int, content: str, embedding: list[float] | None = None):
    if embedding is None:
        await session.execute(
            text(
                "INSERT INTO document_chunks (id, document_id, chunk_index, content) "
                "VALUES (:id, :doc_id, :idx, :content)"
            ),
            {"id": generate_ulid(), "doc_id": doc_id, "idx": index, "content": content},
        )
        return
    vec_str = "[" + ",".join(str(v) for v in embedding) + "]"
    await session.execute(
        text(
            "INSERT INTO document_chunks (id, document_id, chunk_index, content, embedding) "
            "VALUES (:id, :doc_id, :idx, :content, CAST(:vec AS vector))"
        ),
        {"id": generate_ulid(), "doc_id": doc_id, "idx": index, "content": content, "vec": vec_str},
    )


def _unit_vector(hot_index: int, dim: int = EMBEDDING_DIMENSIONS) -> list[float]:
    """A one-hot-ish embedding so cosine similarity is exactly controllable in
    tests: two vectors with the same hot_index are identical (score ~1.0);
    different hot_index are orthogonal (score ~0.0)."""
    v = [0.001] * dim
    v[hot_index % dim] = 1.0
    return v


@pytest.mark.asyncio
async def test_vector_search_finds_a_fact_diluted_by_whole_document_averaging(db_session):
    """The core claim: chunk-level embeddings retrieve a fact buried deep in a
    long document. A whole-document average embedding would have blurred this
    fact's signal across seven other, unrelated chunks."""
    session, has_vector = db_session
    if not has_vector:
        pytest.skip("pgvector extension not available")

    from packages.core.services.embedding_service import search_similar_chunks

    entity_id = generate_ulid()
    doc_id = await _make_document(session, entity_id, "quarterly-report.md")

    # Chunk 5 of 8 is the only one about the actual query topic; the rest are
    # unrelated filler — this is what "a fact three pages in" looks like.
    filler_topics = ["logistics", "hr policy", "office snacks", "parking", "weather"]
    for i, topic in enumerate(filler_topics):
        await _insert_chunk(session, doc_id, i, f"Notes about {topic}.", embedding=_unit_vector(i))
    target_index = len(filler_topics)
    await _insert_chunk(
        session, doc_id, target_index,
        "The board approved a $4.2M budget for the Q3 marketing campaign.",
        embedding=_unit_vector(target_index),
    )
    for j, topic in enumerate(["cafeteria menu", "holiday schedule"]):
        await _insert_chunk(session, doc_id, target_index + 1 + j, f"Notes about {topic}.", embedding=_unit_vector(100 + j))
    await session.commit()

    from unittest.mock import AsyncMock, patch

    with patch.dict(os.environ, {"EMBEDDING_API_KEY": "test-key"}):
        with patch(
            "packages.core.services.embedding_service.generate_embedding",
            AsyncMock(return_value=_unit_vector(target_index)),
        ):
            results = await search_similar_chunks(session, entity_id, "Q3 marketing budget", threshold=0.5)

    assert results, "expected the budget chunk to be found"
    assert results[0]["document_id"] == doc_id
    assert "budget" in results[0]["content_preview"]


@pytest.mark.asyncio
async def test_trigram_search_covers_more_than_the_old_25_document_cap(db_session):
    """The lexical fallback this replaced (`_lexical_scope_results`) capped an
    entity-wide scan at 25 documents. Put the needle in document #30."""
    session, _has_vector = db_session

    from packages.core.services.embedding_service import search_similar_trigram

    entity_id = generate_ulid()
    for i in range(40):
        doc_id = await _make_document(session, entity_id, f"note-{i}.md")
        needle = "阿尔法计划的预算是四百二十万美元" if i == 29 else f"filler content number {i}"
        await _insert_chunk(session, doc_id, 0, needle)
    await session.commit()

    results = await search_similar_trigram(session, entity_id, "阿尔法计划的预算")
    doc_names = {r["name"] for r in results}
    assert "note-29.md" in doc_names, f"needle past the old 25-doc cap was not found: {doc_names}"


@pytest.mark.asyncio
async def test_reindexing_replaces_chunks_instead_of_accumulating(db_session):
    """index_document must delete a document's existing chunks before writing
    the fresh set — otherwise reindexing (edit a file, regenerate embeddings)
    accumulates stale rows from every previous version alongside the current
    one, and search would keep surfacing content that no longer exists.

    Seeds a "previous version" chunk directly (bypassing index_document, which
    this repo's suite has shown is more prone to pytest-asyncio-harness-level
    connection flakiness the more times it's invoked per test — see the git
    history on this test) and makes a single index_document call respresent
    the reindex. That call's own DELETE-then-INSERT is exactly the behavior
    under test; a second full index_document invocation would exercise the
    same DELETE-then-INSERT statements this checks for either way.
    """
    session, has_vector = db_session
    if not has_vector:
        pytest.skip("pgvector extension not available")

    from unittest.mock import AsyncMock, MagicMock, patch
    from packages.core.services.embedding_service import index_document

    entity_id = generate_ulid()
    doc_id = await _make_document(session, entity_id, "living-doc.md")
    # A chunk from a "previous version" of this document, as if a prior
    # index_document run had already populated it.
    await _insert_chunk(session, doc_id, 0, "stale content from an earlier version")
    await session.commit()

    def mock_resp(n):
        data = [{"embedding": _unit_vector(i), "index": i} for i in range(n)]
        return MagicMock(status_code=200, json=lambda: {"data": data, "model": "m"}, raise_for_status=lambda: None)

    async def _post(_url, json=None, headers=None):
        value = (json or {}).get("input")
        n = len(value) if isinstance(value, list) else 1
        return mock_resp(n)

    with patch.dict(os.environ, {"EMBEDDING_API_KEY": "test-key", "MANOR_FS_ROOT": "/nonexistent"}):
        with patch("packages.core.services.embedding_service.httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_client.post.side_effect = _post
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_cls.return_value = mock_client

            with patch(
                "packages.core.services.embedding_service._read_document_content",
                AsyncMock(return_value="current version content"),
            ):
                assert await index_document(session, doc_id) is True

    rows = (await session.execute(
        text("SELECT content FROM document_chunks WHERE document_id = :id"), {"id": doc_id}
    )).scalars().all()
    assert rows == ["current version content"], f"stale chunk survived reindexing: {rows}"


def test_reciprocal_rank_fusion_favors_items_ranked_well_in_multiple_lists():
    """An item that ranks decently in BOTH lists should beat one that ranks
    #1 in only a single list — that is the entire point of fusing rank
    signals instead of trusting whichever single source answered."""
    from packages.core.services.embedding_service import _reciprocal_rank_fusion

    vector_ranked = [{"document_id": "solo-winner"}, {"document_id": "consistent"}, {"document_id": "c"}]
    trigram_ranked = [{"document_id": "consistent"}, {"document_id": "d"}, {"document_id": "e"}]

    scores = _reciprocal_rank_fusion([vector_ranked, trigram_ranked])

    assert scores["consistent"] > scores["solo-winner"], scores


def test_reciprocal_rank_fusion_ignores_raw_score_scale():
    """Items are ranked by POSITION only — a huge raw score on an item buried
    at rank 5 must not let it outrank something at rank 1 elsewhere."""
    from packages.core.services.embedding_service import _reciprocal_rank_fusion

    ranked = [
        {"document_id": "top", "score": 0.01},
        {"document_id": "b", "score": 0.01},
        {"document_id": "c", "score": 0.01},
        {"document_id": "d", "score": 0.01},
        {"document_id": "buried-but-huge-score", "score": 999.0},
    ]
    scores = _reciprocal_rank_fusion([ranked])
    assert scores["top"] > scores["buried-but-huge-score"]


@pytest.mark.asyncio
async def test_hybrid_search_score_is_normalized_to_top_hit(db_session):
    """The fused score reported to callers (e.g. rag.py's "relevance: X" text
    shown to the model) is a confidence relative to this result set's best
    hit, not a raw ~1/60 Reciprocal Rank Fusion sum."""
    session, _has_vector = db_session

    from packages.core.services.embedding_service import hybrid_search

    entity_id = generate_ulid()
    await _make_document(session, entity_id, "Project Alpha overview")
    await session.commit()

    results = await hybrid_search(session, entity_id, "Project Alpha")
    assert results, "expected the filename match to surface"
    assert results[0]["score"] == 1.0
