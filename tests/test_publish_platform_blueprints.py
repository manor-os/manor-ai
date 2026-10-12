from __future__ import annotations

import json
from types import SimpleNamespace

import pytest


class _FakeScalarResult:
    def __init__(self, rows: list[SimpleNamespace]) -> None:
        self._rows = rows

    def scalars(self) -> _FakeScalarResult:
        return self

    def all(self) -> list[SimpleNamespace]:
        return self._rows


class _FakeSession:
    def __init__(
        self,
        rows: list[SimpleNamespace],
        *,
        events: list[str] | None = None,
        commit_error: Exception | None = None,
    ) -> None:
        self._rows = rows
        self._events = events
        self._commit_error = commit_error
        self.committed = False
        self.statements: list[object] = []

    async def execute(self, statement: object) -> _FakeScalarResult:
        self.statements.append(statement)
        if self._events is not None:
            self._events.append("execute")
        return _FakeScalarResult(self._rows)

    async def commit(self) -> None:
        if self._events is not None:
            self._events.append("commit")
        if self._commit_error is not None:
            raise self._commit_error
        self.committed = True


class _FakeSessionContext:
    def __init__(
        self,
        session: _FakeSession,
        *,
        events: list[str] | None = None,
    ) -> None:
        self._session = session
        self._events = events

    async def __aenter__(self) -> _FakeSession:
        if self._events is not None:
            self._events.append("enter")
        return self._session

    async def __aexit__(self, *_args: object) -> None:
        if self._events is not None:
            self._events.append("exit")
        return None


@pytest.fixture(autouse=True)
def _no_builtin_skill_seed(monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts import publish_platform_blueprints as publisher

    async def seed_builtin_skills(_db: object) -> list[object]:
        return []

    monkeypatch.setattr(publisher, "seed_builtin_skills", seed_builtin_skills)


@pytest.mark.asyncio
async def test_publish_platform_blueprints_commits_and_reports_fingerprints(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts import publish_platform_blueprints as publisher

    events: list[str] = []
    printed: list[str] = []
    rows = [
        SimpleNamespace(
            slug="zeta-workspace-v1",
            content_version="1.0.3",
            content_fingerprint="zeta-fingerprint",
        ),
        SimpleNamespace(
            slug="alpha-workspace-v1",
            content_version="1.2.0",
            content_fingerprint="alpha-fingerprint",
        ),
    ]
    session = _FakeSession(rows, events=events)
    session_context = _FakeSessionContext(session, events=events)

    async def seed_platform_blueprints(db: _FakeSession) -> dict[str, str]:
        assert db is session
        events.append("seed_complete")
        return {
            "alpha-workspace-v1": "1.2.0",
            "zeta-workspace-v1": "1.0.3",
        }

    def record_print(value: str) -> None:
        events.append("print")
        printed.append(value)

    async def seed_builtin_skills(db: _FakeSession) -> list[object]:
        assert db is session
        events.append("skills_seeded")
        return [object(), object()]

    monkeypatch.setattr(publisher, "async_session", lambda: session_context)
    monkeypatch.setattr(publisher, "seed_builtin_skills", seed_builtin_skills)
    monkeypatch.setattr(publisher, "seed_platform_blueprints", seed_platform_blueprints)
    monkeypatch.setattr(publisher, "print", record_print, raising=False)

    await publisher.publish_platform_blueprints()

    assert session.committed is True
    # Built-in Skills seed in the same transaction, before the Blueprints.
    assert events == [
        "enter", "skills_seeded", "seed_complete", "execute", "commit", "exit", "print",
    ]
    assert len(session.statements) == 1
    compiled = session.statements[0].compile()
    assert "workspace_blueprints.entity_id IS NULL" in str(compiled)
    slug_params = [value for key, value in compiled.params.items() if key.startswith("slug_")]
    assert slug_params == [["alpha-workspace-v1", "zeta-workspace-v1"]]
    assert json.loads(printed[0]) == {
        "builtin_skills": 2,
        "published_blueprints": [
            {
                "content_fingerprint": "alpha-fingerprint",
                "content_version": "1.2.0",
                "slug": "alpha-workspace-v1",
            },
            {
                "content_fingerprint": "zeta-fingerprint",
                "content_version": "1.0.3",
                "slug": "zeta-workspace-v1",
            },
        ],
    }


@pytest.mark.asyncio
async def test_publish_platform_blueprints_propagates_seed_failure_without_output(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from scripts import publish_platform_blueprints as publisher

    session = _FakeSession([])
    error = RuntimeError("seed failed")

    async def seed_platform_blueprints(_db: _FakeSession) -> dict[str, str]:
        raise error

    monkeypatch.setattr(publisher, "async_session", lambda: _FakeSessionContext(session))
    monkeypatch.setattr(publisher, "seed_platform_blueprints", seed_platform_blueprints)

    with pytest.raises(RuntimeError) as exc_info:
        await publisher.publish_platform_blueprints()

    assert exc_info.value is error
    assert session.committed is False
    assert capsys.readouterr().out == ""


@pytest.mark.asyncio
async def test_publish_platform_blueprints_propagates_commit_failure_without_output(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from scripts import publish_platform_blueprints as publisher

    error = RuntimeError("commit failed")
    events: list[str] = []
    session = _FakeSession([], events=events, commit_error=error)
    session_context = _FakeSessionContext(session, events=events)

    async def seed_platform_blueprints(_db: _FakeSession) -> dict[str, str]:
        events.append("seed_complete")
        return {}

    monkeypatch.setattr(publisher, "async_session", lambda: session_context)
    monkeypatch.setattr(publisher, "seed_platform_blueprints", seed_platform_blueprints)

    with pytest.raises(RuntimeError) as exc_info:
        await publisher.publish_platform_blueprints()

    assert exc_info.value is error
    assert events == ["enter", "seed_complete", "commit", "exit"]
    assert session.committed is False
    assert capsys.readouterr().out == ""


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("rows", "seeded_versions", "error_match"),
    [
        (
            [],
            {"alpha-workspace-v1": "1.2.0"},
            "rows do not match seeded slugs",
        ),
        (
            [
                SimpleNamespace(
                    slug="alpha-workspace-v1",
                    content_version="1.1.0",
                    content_fingerprint="alpha-fingerprint",
                )
            ],
            {"alpha-workspace-v1": "1.2.0"},
            "version mismatch",
        ),
        (
            [
                SimpleNamespace(
                    slug="alpha-workspace-v1",
                    content_version="1.2.0",
                    content_fingerprint="",
                )
            ],
            {"alpha-workspace-v1": "1.2.0"},
            "fingerprint missing",
        ),
    ],
)
async def test_publish_platform_blueprints_rejects_invalid_seed_evidence_before_commit(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    rows: list[SimpleNamespace],
    seeded_versions: dict[str, str],
    error_match: str,
) -> None:
    from scripts import publish_platform_blueprints as publisher

    session = _FakeSession(rows)

    async def seed_platform_blueprints(_db: _FakeSession) -> dict[str, str]:
        return seeded_versions

    monkeypatch.setattr(publisher, "async_session", lambda: _FakeSessionContext(session))
    monkeypatch.setattr(publisher, "seed_platform_blueprints", seed_platform_blueprints)

    with pytest.raises(RuntimeError, match=error_match):
        await publisher.publish_platform_blueprints()

    assert session.committed is False
    assert capsys.readouterr().out == ""
