from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from packages.core.services.local_worker_targeting import (
    conversation_local_worker_target,
    infer_local_worker_name_matches,
    local_worker_display_name,
    normalize_local_worker_name,
    resolve_local_worker_target,
)


def _worker(
    worker_id: str,
    name: str,
    *,
    online: bool = True,
    supports: bool = True,
    heartbeat_age_seconds: int = 5,
):
    return SimpleNamespace(
        id=worker_id,
        display_name=name,
        status="active",
        last_heartbeat_at=datetime.now(timezone.utc)
        - (
            timedelta(seconds=heartbeat_age_seconds)
            if online
            else timedelta(minutes=5)
        ),
        capabilities={"supported": supports, "daemon": {"running": True}},
    )


def test_local_worker_names_hide_legacy_prefix_and_normalize() -> None:
    worker = _worker("worker-a", "  CLI · Office   MacBook ")

    assert local_worker_display_name(worker) == "Office   MacBook"
    assert normalize_local_worker_name(worker) == "office macbook"


def test_explicit_name_selects_the_matching_online_machine() -> None:
    office = _worker("worker-a", "Office MacBook")
    home = _worker("worker-b", "Home PC")

    resolution = resolve_local_worker_target(
        [office, home],
        requested_name="office macbook",
        supports=lambda worker: worker.capabilities["supported"],
    )

    assert resolution.worker is office
    assert resolution.explicit is True
    assert resolution.error is None


def test_two_online_machines_automatically_use_the_most_recent() -> None:
    office = _worker("worker-a", "Office MacBook", heartbeat_age_seconds=20)
    home = _worker("worker-b", "Home PC", heartbeat_age_seconds=3)
    resolution = resolve_local_worker_target(
        [office, home],
        supports=lambda worker: worker.capabilities["supported"],
    )

    assert resolution.worker is home
    assert resolution.error is None


def test_message_mention_routes_one_machine_and_rejects_two_mentions() -> None:
    office = _worker("worker-a", "Office MacBook")
    home = _worker("worker-b", "Home PC")

    assert infer_local_worker_name_matches(
        [office, home], "Run this on Office MacBook"
    ) == [office]
    ambiguous = resolve_local_worker_target(
        [office, home],
        requested_message="Compare Office MacBook with Home PC",
    )
    assert ambiguous.error is not None
    assert ambiguous.error["error"] == "machine_name_ambiguous"


def test_short_ascii_machine_name_does_not_match_inside_another_word() -> None:
    pc = _worker("worker-a", "PC")

    assert infer_local_worker_name_matches([pc], "Run a special check") == []
    assert infer_local_worker_name_matches([pc], "Run it on PC") == [pc]


def test_remembered_offline_machine_automatically_falls_back() -> None:
    offline = _worker("worker-a", "Office MacBook", online=False)
    online = _worker("worker-b", "Home PC")

    resolution = resolve_local_worker_target(
        [offline, online],
        remembered_worker_id=offline.id,
    )

    assert resolution.worker is online
    assert resolution.error is None


def test_remembered_incompatible_machine_automatically_falls_back() -> None:
    unsupported = _worker("worker-a", "Office MacBook", supports=False)
    compatible = _worker("worker-b", "Home PC")

    resolution = resolve_local_worker_target(
        [unsupported, compatible],
        remembered_worker_id=unsupported.id,
        supports=lambda worker: worker.capabilities["supported"],
    )

    assert resolution.worker is compatible
    assert resolution.error is None


def test_conversation_machine_target_is_scoped_per_user() -> None:
    meta = {
        "local_worker_targets": {
            "user-a": {"worker_id": "worker-a", "display_name": "Office MacBook"},
            "user-b": {"worker_id": "worker-b", "display_name": "Home PC"},
        }
    }

    assert conversation_local_worker_target(meta, "user-a")["worker_id"] == "worker-a"
    assert conversation_local_worker_target(meta, "user-b")["worker_id"] == "worker-b"
