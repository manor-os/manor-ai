from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from packages.core.services import relationship_ledger


@pytest.fixture
def relationship_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = SimpleNamespace(storage_path="relationship-ledger", display_path="relationship-ledger")

    async def resolve_location(**kwargs):
        root = tmp_path / kwargs["storage"].directory
        root.mkdir(parents=True, exist_ok=True)
        return directory, str(root), str(tmp_path)

    async def sync_projection(**_kwargs):
        return SimpleNamespace(synced=True, document_id="relationship-document-1")

    monkeypatch.setattr(relationship_ledger, "ensure_ledger_location", resolve_location)
    monkeypatch.setattr(relationship_ledger, "runtime_sync_entity_file_to_knowledge", sync_projection)
    return tmp_path


@pytest.mark.asyncio
async def test_relationship_event_is_idempotent_and_projects_current_row(relationship_runtime: Path) -> None:
    recorded = await relationship_ledger.record_relationship_event(
        entity_id="entity-1",
        workspace_id="workspace-1",
        identity_key="investor:ada@example.com",
        subject_type="investor",
        display_name="Ada Investor",
        relationship_type="prospective_investor",
        status="active",
        stage="intro",
        event="intro_received",
        idempotency_key="intro-1",
        payload={"channel": "email"},
    )
    assert recorded["ok"] is True
    assert recorded["document_id"] == "relationship-document-1"

    repeated = await relationship_ledger.record_relationship_event(
        entity_id="entity-1",
        workspace_id="workspace-1",
        identity_key="investor:ada@example.com",
        subject_type="investor",
        display_name="Ada Investor",
        relationship_type="prospective_investor",
        status="active",
        stage="intro",
        event="intro_received",
        idempotency_key="intro-1",
        payload={"channel": "email"},
    )
    assert repeated["idempotent"] is True
    assert repeated["event_id"] == recorded["event_id"]

    updated = await relationship_ledger.record_relationship_event(
        entity_id="entity-1",
        workspace_id="workspace-1",
        identity_key="investor:ada@example.com",
        subject_type="investor",
        display_name="Ada Investor",
        relationship_type="prospective_investor",
        status="qualified",
        stage="diligence",
        event="diligence_requested",
        idempotency_key="diligence-1",
        payload={"questions": 3},
    )
    assert updated["idempotent"] is False

    ledger = await relationship_ledger.read_relationship_ledger(
        entity_id="entity-1",
        workspace_id="workspace-1",
    )
    assert ledger["entry_count"] == 2
    assert ledger["relationship_count"] == 1
    assert ledger["rows"][0]["status"] == "qualified"
    assert ledger["rows"][0]["payload"] == {"channel": "email", "questions": 3}
    assert ledger["schema_version"] == 2
    assert all(entry["schema_version"] == 1 for entry in ledger["entries"])
    json.dumps(ledger)


@pytest.mark.asyncio
async def test_relationship_subject_type_is_enum(relationship_runtime: Path) -> None:
    with pytest.raises(relationship_ledger.RelationshipLedgerError, match="not a valid"):
        await relationship_ledger.record_relationship_event(
            entity_id="entity-1",
            workspace_id="workspace-1",
            identity_key="contact-1",
            subject_type="unknown-contact",
            event="note",
            idempotency_key="note-1",
        )


@pytest.mark.asyncio
async def test_relationship_identity_aliases_are_deduplicated_and_accumulated(
    relationship_runtime: Path,
) -> None:
    await relationship_ledger.record_relationship_event(
        entity_id="entity-1",
        workspace_id="workspace-1",
        identity_key="lead:C-346",
        identity_aliases=["email:Ali@example.com", "skool:planet-ai"],
        subject_type="lead",
        event="first_contact_sent",
        idempotency_key="contact-1",
    )
    await relationship_ledger.record_relationship_event(
        entity_id="entity-1",
        workspace_id="workspace-1",
        identity_key="lead:C-346",
        identity_aliases=["EMAIL:ali@example.com", "youtube:@planet-ai"],
        subject_type="lead",
        event="reply_received",
        idempotency_key="reply-1",
    )

    ledger = await relationship_ledger.read_relationship_ledger(
        entity_id="entity-1",
        workspace_id="workspace-1",
    )

    assert ledger["rows"][0]["identity_aliases"] == [
        "email:Ali@example.com",
        "skool:planet-ai",
        "youtube:@planet-ai",
    ]
    assert ledger["entries"][0]["identity_aliases"] == [
        "email:Ali@example.com",
        "skool:planet-ai",
    ]


@pytest.mark.asyncio
async def test_relationship_aliases_preserve_punctuation_and_support_removal(
    relationship_runtime: Path,
) -> None:
    await relationship_ledger.record_relationship_event(
        entity_id="entity-1",
        workspace_id="workspace-1",
        identity_key="lead:punctuation",
        identity_aliases=["email:a-b@example.com", "email:a_b@example.com"],
        event="aliases_added",
        idempotency_key="aliases-added",
    )
    await relationship_ledger.record_relationship_event(
        entity_id="entity-1",
        workspace_id="workspace-1",
        identity_key="lead:punctuation",
        identity_aliases_removed=["EMAIL:A-B@EXAMPLE.COM"],
        event="alias_removed",
        idempotency_key="alias-removed",
    )

    ledger = await relationship_ledger.read_relationship_ledger(
        entity_id="entity-1",
        workspace_id="workspace-1",
    )

    assert ledger["rows"][0]["identity_aliases"] == ["email:a_b@example.com"]


@pytest.mark.asyncio
async def test_relationship_projects_typed_contact_points_and_lifecycle(
    relationship_runtime: Path,
) -> None:
    await relationship_ledger.record_relationship_event(
        entity_id="entity-1",
        workspace_id="workspace-1",
        identity_key="person:calvin",
        contact_points=[
            {
                "kind": "email",
                "value": "Calvin@ManorAI.xyz",
                "is_primary": True,
                "status": "verified",
            },
            {"kind": "phone", "value": "+1 (415) 555-0100", "is_primary": True},
            {"kind": "linkedin", "value": "https://linkedin.com/in/calvin-lin/"},
            {"kind": "website", "value": "HTTPS://ManorAI.xyz/Creators/"},
            {"kind": "tiktok", "value": "@manorai"},
            {"kind": "instagram", "value": "@manorai"},
            {"kind": "facebook", "value": "manorai"},
            {"kind": "whatsapp", "value": "+1 415 555 0100"},
        ],
        event="contacts_added",
        idempotency_key="contacts-added",
    )
    await relationship_ledger.record_relationship_event(
        entity_id="entity-1",
        workspace_id="workspace-1",
        identity_key="person:calvin",
        contact_points=[
            {
                "kind": "email",
                "value": "founder@manorai.xyz",
                "is_primary": True,
                "status": "verified",
            },
        ],
        contact_points_removed=[
            {"kind": "phone", "value": "+1 415 555 0100"},
        ],
        event="contacts_updated",
        idempotency_key="contacts-updated",
    )

    ledger = await relationship_ledger.read_relationship_ledger(
        entity_id="entity-1",
        workspace_id="workspace-1",
    )
    row = ledger["rows"][0]

    assert "phone:+14155550100" not in row["contact_keys"]
    assert "email:calvin@manorai.xyz" in row["contact_keys"]
    assert "email:founder@manorai.xyz" in row["contact_keys"]
    assert "website:manorai.xyz/Creators" in row["contact_keys"]
    assert all(point["validation_status"] == "valid" for point in row["contact_points"])
    email_points = [point for point in row["contact_points"] if point["kind"] == "email"]
    assert [point["is_primary"] for point in email_points] == [False, True]
    assert all(point["verification_status"] == "verified" for point in email_points)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("example.com:443", "example.com"),
        ("https://example.com:443", "example.com"),
        ("example.com:80", "example.com"),
        ("http://example.com:80", "example.com"),
        ("example.com:8443", "example.com:8443"),
    ],
)
def test_relationship_normalizes_schemeless_website_default_ports(
    value: str,
    expected: str,
) -> None:
    assert relationship_ledger._normalized_contact_value("website", value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("abc", "abc"),
        ("Creator_Name", "creator_name"),
        ("汉", "汉"),
        ("汉" * 10, "汉" * 10),
        ("あ" * 2, "あ" * 2),
        ("あ" * 20, "あ" * 20),
        ("first.last", "first.last"),
        ("مرحبا123", "مرحبا123"),
    ],
)
def test_relationship_accepts_youtube_handle_script_boundaries(
    value: str,
    expected: str,
) -> None:
    assert relationship_ledger._normalized_contact_value("youtube", value) == expected


@pytest.mark.asyncio
async def test_relationship_contact_updates_patch_only_explicit_fields(
    relationship_runtime: Path,
) -> None:
    await relationship_ledger.record_relationship_event(
        entity_id="entity-1",
        workspace_id="workspace-1",
        identity_key="person:patch",
        contact_points=[
            {
                "kind": "email",
                "value": "person@example.com",
                "label": "work",
                "is_primary": True,
                "verification_status": "verified",
                "deliverability_status": "active",
                "consent_status": "opted_in",
                "verified_at": "2026-08-25T12:00:00Z",
                "source_url": "https://example.com/contact",
                "metadata": {"source": "public"},
            }
        ],
        event="contact_added",
        idempotency_key="contact-patch-added",
    )
    await relationship_ledger.record_relationship_event(
        entity_id="entity-1",
        workspace_id="workspace-1",
        identity_key="person:patch",
        contact_points=[
            {
                "kind": "email",
                "value": "PERSON@example.com",
                "deliverability_status": "bounced",
            }
        ],
        event="contact_bounced",
        idempotency_key="contact-patch-bounced",
    )

    ledger = await relationship_ledger.read_relationship_ledger(
        entity_id="entity-1",
        workspace_id="workspace-1",
    )
    point = ledger["rows"][0]["contact_points"][0]

    assert point["label"] == "work"
    assert point["is_primary"] is True
    assert point["verification_status"] == "verified"
    assert point["deliverability_status"] == "bounced"
    assert point["consent_status"] == "opted_in"
    assert point["verified_at"] == "2026-08-25T12:00:00Z"
    assert point["source_url"] == "https://example.com/contact"
    assert point["metadata"] == {"source": "public"}
    assert point["status"] == "bounced"


@pytest.mark.asyncio
async def test_relationship_contact_patch_preserves_existing_subject_type(
    relationship_runtime: Path,
) -> None:
    await relationship_ledger.record_relationship_event(
        entity_id="entity-1",
        workspace_id="workspace-1",
        identity_key="lead:subject",
        subject_type="lead",
        event="created",
        idempotency_key="subject-created",
    )
    await relationship_ledger.record_relationship_event(
        entity_id="entity-1",
        workspace_id="workspace-1",
        identity_key="lead:subject",
        contact_points=[{"kind": "email", "value": "lead@example.com"}],
        event="contact_added",
        idempotency_key="subject-contact-added",
    )

    ledger = await relationship_ledger.read_relationship_ledger(
        entity_id="entity-1",
        workspace_id="workspace-1",
    )

    assert ledger["rows"][0]["subject_type"] == "lead"


@pytest.mark.asyncio
async def test_relationship_contact_patch_can_clear_optional_fields(
    relationship_runtime: Path,
) -> None:
    await relationship_ledger.record_relationship_event(
        entity_id="entity-1",
        workspace_id="workspace-1",
        identity_key="person:clear",
        contact_points=[
            {
                "kind": "linkedin",
                "value": "https://linkedin.com/in/example/",
                "label": "personal",
                "source_url": "https://example.com/source",
                "metadata": {"source": "public"},
            }
        ],
        event="contact_added",
        idempotency_key="contact-clear-added",
    )
    await relationship_ledger.record_relationship_event(
        entity_id="entity-1",
        workspace_id="workspace-1",
        identity_key="person:clear",
        contact_points=[
            {
                "kind": "linkedin",
                "value": "example",
                "clear_fields": ["label", "source_url", "metadata"],
            }
        ],
        event="contact_cleared",
        idempotency_key="contact-clear-updated",
    )

    ledger = await relationship_ledger.read_relationship_ledger(
        entity_id="entity-1",
        workspace_id="workspace-1",
    )
    point = ledger["rows"][0]["contact_points"][0]

    assert point["contact_key"] == "linkedin:in/example"
    assert point["label"] is None
    assert point["source_url"] is None
    assert point["metadata"] == {}


@pytest.mark.asyncio
async def test_relationship_merges_duplicate_contact_patches_in_one_event(
    relationship_runtime: Path,
) -> None:
    await relationship_ledger.record_relationship_event(
        entity_id="entity-1",
        workspace_id="workspace-1",
        identity_key="person:duplicate-patch",
        contact_points=[
            {"kind": "email", "value": "Person@example.com", "label": "work"},
            {
                "kind": "email",
                "value": "person@example.com",
                "is_primary": True,
                "verification_status": "verified",
            },
        ],
        event="contacts_added",
        idempotency_key="duplicate-contact-patches",
    )

    ledger = await relationship_ledger.read_relationship_ledger(
        entity_id="entity-1",
        workspace_id="workspace-1",
    )
    points = ledger["rows"][0]["contact_points"]

    assert len(points) == 1
    assert points[0]["label"] == "work"
    assert points[0]["is_primary"] is True
    assert points[0]["verification_status"] == "verified"


@pytest.mark.asyncio
async def test_relationship_canonicalizes_social_profile_variants_for_conflicts(
    relationship_runtime: Path,
) -> None:
    contacts = (
        [
            {"kind": "linkedin", "value": "https://www.linkedin.com/in/Example/"},
            {"kind": "instagram", "value": "@Example"},
        ],
        [
            {"kind": "linkedin", "value": "example"},
            {"kind": "instagram", "value": "https://instagram.com/example/"},
        ],
    )
    for index, points in enumerate(contacts, start=1):
        await relationship_ledger.record_relationship_event(
            entity_id="entity-1",
            workspace_id="workspace-1",
            identity_key=f"lead:{index}",
            contact_points=points,
            event="contact_added",
            idempotency_key=f"canonical-contact-{index}",
        )

    ledger = await relationship_ledger.read_relationship_ledger(
        entity_id="entity-1",
        workspace_id="workspace-1",
    )

    assert ledger["identity_conflict_count"] == 2
    assert {conflict["identifier_key"] for conflict in ledger["identity_conflicts"]} == {
        "instagram:example",
        "linkedin:in/example",
    }


@pytest.mark.asyncio
async def test_relationship_accepts_safe_dotted_handles_and_default_web_ports(
    relationship_runtime: Path,
) -> None:
    await relationship_ledger.record_relationship_event(
        entity_id="entity-1",
        workspace_id="workspace-1",
        identity_key="lead:safe-social-routes",
        contact_points=[
            {"kind": "bluesky", "value": "creator.bsky.social"},
            {"kind": "signal", "value": "creator.12"},
            {"kind": "instagram", "value": "https://instagram.com:443/creator"},
            {"kind": "whatsapp", "value": "https://wa.me:443/14155550100"},
            {"kind": "slack", "value": "https://creator-team.slack.com"},
            {"kind": "wechat", "value": "https://u.wechat.com/AbC123"},
            {"kind": "line", "value": "https://lin.ee/AbC123"},
            {"kind": "x", "value": "_creator"},
            {"kind": "github", "value": "creator-name"},
            {"kind": "telegram", "value": "creator_name"},
            {"kind": "telegram", "value": "nft"},
            {"kind": "telegram", "value": "https://t.me/+14155550102"},
            {"kind": "tiktok", "value": "@creator.name"},
            {"kind": "facebook", "value": "https://facebook.com/creator.name"},
            {"kind": "youtube", "value": "https://youtube.com/@Creator_Name"},
            {
                "kind": "youtube",
                "value": "https://youtube.com/channel/UCK8sQmJBp8GCxrOtXWBpyEA",
            },
            {"kind": "youtube", "value": f"https://youtube.com/c/{'A' * 40}"},
            {"kind": "signal", "value": "https://signal.me/#eu/AbC_123-xyz"},
            {"kind": "signal", "value": f"{'a' * 32}.12"},
            {"kind": "signal", "value": "abc.01"},
            {"kind": "signal", "value": "abc.999999999"},
        ],
        event="contacts_added",
        idempotency_key="safe-social-routes",
    )

    ledger = await relationship_ledger.read_relationship_ledger(
        entity_id="entity-1",
        workspace_id="workspace-1",
    )

    assert set(ledger["rows"][0]["contact_keys"]) == {
        "bluesky:creator.bsky.social",
        "facebook:creator.name",
        "github:creator-name",
        "instagram:creator",
        "line:url/lin.ee/AbC123",
        "signal:creator.12",
        f"signal:{'a' * 32}.12",
        "signal:abc.01",
        "signal:abc.999999999",
        "signal:url/signal.me#eu/AbC_123-xyz",
        "slack:creator-team",
        "telegram:nft",
        "telegram:phone/14155550102",
        "telegram:creator_name",
        "tiktok:creator.name",
        "whatsapp:14155550100",
        "wechat:url/u.wechat.com/AbC123",
        "x:_creator",
        "youtube:channel/UCK8sQmJBp8GCxrOtXWBpyEA",
        f"youtube:c/{'a' * 40}",
        "youtube:creator_name",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "value"),
    [
        ("youtube", "https://youtube.com/watch?v=video-1"),
        ("instagram", "https://instagram.com/p/post-1"),
        ("tiktok", "https://tiktok.com/@creator/video/123"),
        ("x", "https://x.com/creator/status/123"),
        ("wechat", "https://mp.weixin.qq.com/s/article-id"),
        ("line", "https://line.me/en/"),
        ("signal", "https://signal.me/blog"),
        ("signal", "https://profile.signal.me/#p/+14155550100"),
        ("signal", "javascript://signal.me#p/+14155550100"),
        ("wechat", "ftp://u.wechat.com/example"),
        ("line", "javascript://line.me/ti/p/@manorai"),
        ("instagram", "javascript:alert(1)"),
        ("instagram", "javascript:/alert(1)"),
        ("instagram", "//evil.example"),
        ("instagram", "/not-a-handle"),
        ("instagram", "https:/instagram.com/creator"),
        ("linkedin", "https://linkedin.com:bad/in/calvin"),
        ("instagram", "https://instagram.com:8443/creator"),
        ("whatsapp", "https://wa.me:8443/14155550100"),
        ("instagram", "https://instagram.com/%3Cscript%3E"),
        ("linkedin", "https://linkedin.com/in/%3Cscript%3E"),
        ("wechat", "https://u.wechat.com/%3Cscript%3E"),
        ("instagram", "https://instagram.com/@explore"),
        ("x", "https://x.com/@home"),
        ("wechat", "https://u.wechat.com/token%3Fadmin=true"),
        ("wechat", "https://u.wechat.com/token%3Badmin=true"),
        ("line", "https://lin.ee/token%23fragment"),
        ("line", "https://lin.ee/token%3Aadmin"),
        ("line", "https://line.me/ti/p/token%3Aadmin"),
        ("signal", "https://signal.me/#eu/%3Cscript%3E"),
        ("signal", "https://signal.me/#eu/token?admin=true"),
        ("x", "creator.name"),
        ("x", "creator-name"),
        ("x", "x" * 100),
        ("instagram", "creator-name"),
        ("github", "creator_name"),
        ("telegram", "creator-name"),
        ("signal", "a.12"),
        ("signal", "1abc.12"),
        ("signal", "abc.00"),
        ("signal", "abc.012"),
        ("signal", "abc.1234567890"),
        ("youtube", "a"),
        ("youtube", "a" * 31),
        ("youtube", "a" * 256),
        ("youtube", "123"),
        ("youtube", "123-456"),
        ("youtube", "creator.com"),
        ("youtube", "creator.ai"),
        ("youtube", "creator-site.com"),
        ("youtube", "creator.photography"),
        ("youtube", "creator.consulting"),
        ("youtube", "creator.news"),
        ("youtube", "creator.finance"),
        ("youtube", "creator.travel"),
        ("youtube", "www.creator"),
        ("youtube", "abc.technology"),
        ("youtube", "汉" * 11),
        ("youtube", "汉" * 11 + "a"),
        ("youtube", "あ" * 21),
        ("youtube", "あ" * 21 + "a"),
        ("youtube", "abcאבג"),
        ("youtube", "123مرحبا"),
        ("youtube", "مر123حبا"),
        ("youtube", "https://youtube.com/channel/uck8sQmJBp8GCxrOtXWBpyEA"),
        ("youtube", "https://youtube.com/@creator?tracking=" + "a" * 4096),
        ("tiktok", "@@creator"),
        ("facebook", "https://facebook.com/profile.php?id=admin"),
        ("instagram", "https://.instagram.com/creator"),
        ("instagram", "https://evil..instagram.com/creator"),
        ("slack", "https://.slack.com"),
        ("slack", "https://evil..slack.com"),
        ("instagram", "."),
        ("instagram", "-"),
        ("instagram", "..."),
        ("bluesky", "creator..bsky.social"),
        ("bluesky", "-creator.bsky.social"),
        ("instagram", "https://instagram.com/%65xplore"),
        ("instagram", "https://instagram.com/%2565xplore"),
        ("instagram", "https://instagram.com/calvin%2Fposts"),
        ("instagram", "https://instagram.com/%2e%2e"),
        ("instagram", "https://instagram.com/%EF%BD%85xplore"),
        ("x", "https://x.com/%68ome"),
        ("telegram", "https://t.me/%73hare"),
        ("github", "https://github.com/%73ettings"),
    ],
)
async def test_relationship_rejects_social_content_urls_as_contact_profiles(
    relationship_runtime: Path,
    kind: str,
    value: str,
) -> None:
    with pytest.raises(relationship_ledger.RelationshipLedgerError) as exc_info:
        await relationship_ledger.record_relationship_event(
            entity_id="entity-1",
            workspace_id="workspace-1",
            identity_key=f"person:{kind}",
            contact_points=[{"kind": kind, "value": value}],
            event="contact_added",
            idempotency_key=f"invalid-social-{kind}",
        )

    assert exc_info.value.code == "invalid_input"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "value"),
    [
        ("email", "not-an-email"),
        ("website", "hello"),
        ("phone", "123"),
        ("whatsapp", "call-me"),
        ("website", "http://127.0.0.1/admin"),
        ("website", "http://10.0.0.1"),
        ("website", "http://[::1]"),
        ("website", "localhost.localdomain"),
        ("website", "http://127.1/admin"),
        ("website", "http://0177.0.0.1/admin"),
        ("website", "http://0x7f.0.0.1/admin"),
        ("website", "http://127.0.0.1./admin"),
        ("website", "http://999.999.999.999/path"),
        ("website", "http://foo.123/path"),
        ("website", "http://0xzz.0.0.1/path"),
    ],
)
async def test_relationship_validates_typed_contact_values(
    relationship_runtime: Path,
    kind: str,
    value: str,
) -> None:
    with pytest.raises(relationship_ledger.RelationshipLedgerError) as exc_info:
        await relationship_ledger.record_relationship_event(
            entity_id="entity-1",
            workspace_id="workspace-1",
            identity_key=f"person:{kind}",
            contact_points=[{"kind": kind, "value": value}],
            event="contact_added",
            idempotency_key=f"invalid-contact-{kind}",
        )

    assert exc_info.value.code == "invalid_input"


@pytest.mark.asyncio
async def test_relationship_phone_keys_ignore_optional_plus_prefix(
    relationship_runtime: Path,
) -> None:
    for index, value in enumerate(("+1 415 555 0100", "1 (415) 555-0100"), start=1):
        await relationship_ledger.record_relationship_event(
            entity_id="entity-1",
            workspace_id="workspace-1",
            identity_key=f"lead:{index}",
            contact_points=[{"kind": "phone", "value": value}],
            event="contact_added",
            idempotency_key=f"phone-contact-{index}",
        )

    ledger = await relationship_ledger.read_relationship_ledger(
        entity_id="entity-1",
        workspace_id="workspace-1",
    )

    assert ledger["identity_conflicts"][0]["identifier_key"] == "phone:14155550100"


@pytest.mark.asyncio
async def test_relationship_accepts_public_messaging_links_and_regional_social_hosts(
    relationship_runtime: Path,
) -> None:
    contacts = [
        ("whatsapp", "https://wa.me/14155550100", "whatsapp:14155550100"),
        ("signal", "https://signal.me/#p/+14155550101", "signal:14155550101"),
        ("line", "https://line.me/ti/p/@manorai", "line:url/line.me/ti/p/@manorai"),
        ("wechat", "https://u.wechat.com/example", "wechat:url/u.wechat.com/example"),
        ("linkedin", "https://uk.linkedin.com/in/Example", "linkedin:in/example"),
    ]

    for index, (kind, value, _expected_key) in enumerate(contacts, start=1):
        await relationship_ledger.record_relationship_event(
            entity_id="entity-1",
            workspace_id="workspace-1",
            identity_key=f"contact:{index}",
            contact_points=[{"kind": kind, "value": value}],
            event="contact_added",
            idempotency_key=f"messaging-contact-{index}",
        )

    ledger = await relationship_ledger.read_relationship_ledger(
        entity_id="entity-1",
        workspace_id="workspace-1",
    )

    keys = {key for row in ledger["rows"] for key in row["contact_keys"]}
    assert keys == {expected_key for _kind, _value, expected_key in contacts}


@pytest.mark.asyncio
async def test_relationship_identity_keys_preserve_significant_punctuation(
    relationship_runtime: Path,
) -> None:
    for index, identity_key in enumerate(("lead:C-346", "lead:C_346"), start=1):
        await relationship_ledger.record_relationship_event(
            entity_id="entity-1",
            workspace_id="workspace-1",
            identity_key=identity_key,
            event="created",
            idempotency_key=f"distinct-identity-{index}",
        )

    ledger = await relationship_ledger.read_relationship_ledger(
        entity_id="entity-1",
        workspace_id="workspace-1",
    )

    assert ledger["relationship_count"] == 2
    assert {row["identity_key"] for row in ledger["rows"]} == {
        "lead:C-346",
        "lead:C_346",
    }


@pytest.mark.asyncio
async def test_relationship_reports_cross_identity_contact_conflicts(
    relationship_runtime: Path,
) -> None:
    for identity_key in ("lead:1", "lead:2"):
        await relationship_ledger.record_relationship_event(
            entity_id="entity-1",
            workspace_id="workspace-1",
            identity_key=identity_key,
            contact_points=[{"kind": "email", "value": "shared@example.com"}],
            event="contact_added",
            idempotency_key=f"contact-{identity_key}",
        )

    ledger = await relationship_ledger.read_relationship_ledger(
        entity_id="entity-1",
        workspace_id="workspace-1",
    )

    assert ledger["identity_conflict_count"] == 1
    assert ledger["identity_conflicts"] == [
        {
            "identifier_type": "contact",
            "identifier_key": "email:shared@example.com",
            "identity_keys": ["lead:1", "lead:2"],
        }
    ]
    assert all(row["identity_conflicts"] for row in ledger["rows"])


@pytest.mark.asyncio
async def test_relationship_legacy_event_without_aliases_remains_readable(
    relationship_runtime: Path,
) -> None:
    event_root = relationship_runtime / "relationship-ledger" / "events"
    event_root.mkdir(parents=True, exist_ok=True)
    (event_root / "legacy.json").write_text(
        json.dumps(
            {
                "contract_id": "manor.relationship_ledger/v1",
                "schema_version": 1,
                "event_id": "legacy-event",
                "workspace_id": "workspace-1",
                "entity_id": "entity-1",
                "entry_type": "event",
                "identity_key": "lead:legacy",
                "identity_fingerprint": relationship_ledger.ledger_key_fingerprint("lead:legacy"),
                "subject_type": "lead",
                "event": "imported",
                "recorded_at": "2026-08-26T08:00:00Z",
                "idempotency_key": "legacy-import",
                "payload": {},
                "evidence_refs": [],
            }
        ),
        encoding="utf-8",
    )

    ledger = await relationship_ledger.read_relationship_ledger(
        entity_id="entity-1",
        workspace_id="workspace-1",
    )

    assert ledger["rows"][0]["identity_aliases"] == []
    assert ledger["entries"][0]["identity_aliases"] == []

    repeated = await relationship_ledger.record_relationship_event(
        entity_id="entity-1",
        workspace_id="workspace-1",
        identity_key="lead:legacy",
        subject_type="lead",
        event="imported",
        idempotency_key="legacy-import",
    )

    assert repeated["idempotent"] is True
    assert repeated["event_id"] == "legacy-event"


@pytest.mark.asyncio
async def test_relationship_legacy_invalid_contacts_remain_readable_and_removable(
    relationship_runtime: Path,
) -> None:
    event_root = relationship_runtime / "relationship-ledger" / "events"
    event_root.mkdir(parents=True, exist_ok=True)
    for index in (1, 2):
        (event_root / f"legacy-invalid-contact-{index}.json").write_text(
            json.dumps(
                {
                    "contract_id": "manor.relationship_ledger/v1",
                    "schema_version": 1,
                    "event_id": f"legacy-invalid-contact-{index}",
                    "workspace_id": "workspace-1",
                    "entity_id": "entity-1",
                    "entry_type": "event",
                    "identity_key": f"lead:legacy-contact-{index}",
                    "identity_fingerprint": f"legacy-fingerprint-{index}",
                    "contact_points": [
                        {
                            "kind": "website",
                            "value": "http://[bad",
                            "is_primary": True,
                        }
                    ],
                    "event": "imported",
                    "recorded_at": f"2026-08-26T08:00:0{index}Z",
                    "idempotency_key": f"legacy-invalid-import-{index}",
                    "payload": {},
                    "evidence_refs": [],
                }
            ),
            encoding="utf-8",
        )

    ledger = await relationship_ledger.read_relationship_ledger(
        entity_id="entity-1",
        workspace_id="workspace-1",
    )
    assert ledger["identity_conflict_count"] == 0
    assert all(row["contact_keys"] == [] for row in ledger["rows"])
    for row in ledger["rows"]:
        point = row["contact_points"][0]
        assert point["contact_key"] == "website:http://[bad"
        assert point["validation_status"] == "legacy_invalid"
        assert point["is_primary"] is False
        assert point["verification_status"] == "unverified"
        assert point["deliverability_status"] == "invalid"
        assert point["status"] == "invalid"

    await relationship_ledger.record_relationship_event(
        entity_id="entity-1",
        workspace_id="workspace-1",
        identity_key="lead:legacy-contact-1",
        contact_points_removed=[{"kind": "website", "value": "http://[bad"}],
        event="legacy_contact_removed",
        idempotency_key="legacy-invalid-removed",
    )

    ledger = await relationship_ledger.read_relationship_ledger(
        entity_id="entity-1",
        workspace_id="workspace-1",
    )
    rows_by_identity = {row["identity_key"]: row for row in ledger["rows"]}
    assert rows_by_identity["lead:legacy-contact-1"]["contact_points"] == []
    assert len(rows_by_identity["lead:legacy-contact-2"]["contact_points"]) == 1


@pytest.mark.asyncio
async def test_relationship_idempotency_distinguishes_omitted_and_explicit_person_type(
    relationship_runtime: Path,
) -> None:
    await relationship_ledger.record_relationship_event(
        entity_id="entity-1",
        workspace_id="workspace-1",
        identity_key="lead:subject-type-idempotency",
        event="updated",
        idempotency_key="subject-type-idempotency",
    )

    with pytest.raises(relationship_ledger.RelationshipLedgerError) as exc_info:
        await relationship_ledger.record_relationship_event(
            entity_id="entity-1",
            workspace_id="workspace-1",
            identity_key="lead:subject-type-idempotency",
            subject_type="person",
            event="updated",
            idempotency_key="subject-type-idempotency",
        )

    assert exc_info.value.code == "idempotency_conflict"


@pytest.mark.asyncio
async def test_relationship_legacy_person_idempotency_replay_remains_compatible(
    relationship_runtime: Path,
) -> None:
    event_root = relationship_runtime / "relationship-ledger" / "events"
    event_root.mkdir(parents=True, exist_ok=True)
    (event_root / "legacy-person.json").write_text(
        json.dumps(
            {
                "contract_id": "manor.relationship_ledger/v1",
                "schema_version": 1,
                "event_id": "legacy-person",
                "workspace_id": "workspace-1",
                "entity_id": "entity-1",
                "entry_type": "event",
                "identity_key": "person:legacy",
                "identity_fingerprint": "legacy-fingerprint",
                "subject_type": "person",
                "event": "created",
                "recorded_at": "2026-08-26T08:00:00Z",
                "idempotency_key": "legacy-person-created",
                "payload": {},
                "evidence_refs": [],
            }
        ),
        encoding="utf-8",
    )

    replay = await relationship_ledger.record_relationship_event(
        entity_id="entity-1",
        workspace_id="workspace-1",
        identity_key="person:legacy",
        event="created",
        idempotency_key="legacy-person-created",
    )

    assert replay["idempotent"] is True
    assert replay["event_id"] == "legacy-person"


def test_relationship_event_model_reads_v1_and_v2_but_writes_v1_during_rollout() -> None:
    common = {
        "event_id": "event-versioned",
        "workspace_id": "workspace-1",
        "entity_id": "entity-1",
        "identity_key": "person:versioned",
        "identity_fingerprint": "fingerprint",
        "event": "created",
        "recorded_at": "2026-08-26T08:00:00Z",
        "idempotency_key": "event-versioned",
    }

    writer_default = relationship_ledger.RelationshipLedgerEvent.model_validate(common)
    current = relationship_ledger.RelationshipLedgerEvent.model_validate(
        {
            **common,
            "schema_version": 2,
        }
    )

    assert writer_default.schema_version == 1
    assert current.schema_version == 2


def test_relationship_idempotency_ignores_version_during_reader_first_rollout() -> None:
    common = {
        "event_id": "event-versioned",
        "workspace_id": "workspace-1",
        "entity_id": "entity-1",
        "identity_key": "person:versioned",
        "identity_fingerprint": "fingerprint",
        "event": "created",
        "recorded_at": "2026-08-26T08:00:00Z",
        "idempotency_key": "event-versioned",
        "subject_type_explicit": False,
    }
    prior = relationship_ledger.RelationshipLedgerEvent.model_validate(
        {
            **common,
            "schema_version": 2,
        }
    )
    current = relationship_ledger.RelationshipLedgerEvent.model_validate(common)

    assert relationship_ledger._idempotency_payloads_match(prior, current) is True


@pytest.mark.asyncio
async def test_relationship_detects_concurrent_idempotency_payload_conflict(
    relationship_runtime: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    existing_entry = relationship_ledger.RelationshipLedgerEvent(
        event_id="existing-event",
        workspace_id="workspace-1",
        entity_id="entity-1",
        identity_key="lead:concurrent",
        identity_fingerprint="legacy-fingerprint",
        event="contact_added",
        recorded_at="2026-08-26T08:00:00Z",
        idempotency_key="concurrent-key",
        payload={"version": 1},
    )

    async def empty_read(**_kwargs):
        return {"entries": []}

    monkeypatch.setattr(relationship_ledger, "read_relationship_ledger", empty_read)
    monkeypatch.setattr(relationship_ledger, "write_immutable_json", lambda *_args: False)
    monkeypatch.setattr(relationship_ledger, "_parse_event", lambda *_args, **_kwargs: existing_entry)

    with pytest.raises(relationship_ledger.RelationshipLedgerError) as exc_info:
        await relationship_ledger.record_relationship_event(
            entity_id="entity-1",
            workspace_id="workspace-1",
            identity_key="lead:concurrent",
            event="contact_added",
            idempotency_key="concurrent-key",
            payload={"version": 2},
        )

    assert exc_info.value.code == "idempotency_conflict"
