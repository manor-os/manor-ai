import json

import pytest

from packages.core.services import email_service, ops_alerts, ops_config


class _DigestRedis:
    def __init__(self) -> None:
        self.deleted: list[str] = []

    async def lrange(self, _key: str, _start: int, _end: int) -> list[str]:
        return [
            json.dumps(
                {
                    "severity": "warning",
                    "rule_id": "queue_lag",
                    "target": "queue:control",
                }
            )
        ]

    async def delete(self, key: str) -> None:
        self.deleted.append(key)


@pytest.mark.asyncio
async def test_send_digest_uses_configured_ops_recipients(monkeypatch) -> None:
    redis = _DigestRedis()
    sent: list[tuple[str, str]] = []

    async def load_config() -> dict:
        return {"recipients": ["ops@example.com"], "rules": {}}

    async def send_email(address: str, subject: str, _html: str, **_kwargs) -> bool:
        sent.append((address, subject))
        return True

    async def close(_client) -> None:
        return None

    async def get_redis() -> _DigestRedis:
        return redis

    monkeypatch.setattr(ops_alerts, "_redis", get_redis)
    monkeypatch.setattr(ops_alerts, "_close", close)
    monkeypatch.setattr(ops_config, "load_config", load_config)
    monkeypatch.setattr(email_service, "send_email", send_email)

    result = await ops_alerts.send_digest()

    assert result == {"ok": True, "events": 1, "emailed": True}
    assert redis.deleted == [ops_alerts._REDIS_DIGEST_QUEUE]
    assert sent == [("ops@example.com", "[Manor Ops] Daily Digest — 1 suppressed events")]
