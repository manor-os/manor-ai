from __future__ import annotations

import pytest

from tests.redis_test_resource import RedisTestResource, require_test_cache_namespace

pytestmark = pytest.mark.oss_smoke


def test_test_redis_resource_ignores_inherited_runtime_url(monkeypatch):
    monkeypatch.setenv("REDIS_URL", "redis://developer-cache:6379/0")
    monkeypatch.delenv("TEST_REDIS_URL", raising=False)

    resource = RedisTestResource.from_environment()

    assert resource.directory is not None
    assert resource.url.startswith("unix://")
    assert "developer-cache" not in resource.url
    resource.cleanup(None)


def test_test_redis_resource_uses_only_explicit_test_url(monkeypatch):
    monkeypatch.setenv("REDIS_URL", "redis://developer-cache:6379/0")
    monkeypatch.setenv("TEST_REDIS_URL", "redis://ci-cache:6379/0")

    resource = RedisTestResource.from_environment()

    assert resource.directory is None
    assert resource.url == "redis://ci-cache:6379/0"


def test_test_redis_resource_allocates_distinct_child_cache_namespaces(monkeypatch):
    monkeypatch.setenv("TEST_REDIS_URL", "redis://ci-cache:6379/0")

    resource = RedisTestResource.from_environment()

    first = resource.cache_prefix_for_test()
    second = resource.cache_prefix_for_test()
    assert first.startswith(resource.cache_prefix)
    assert second.startswith(resource.cache_prefix)
    assert first != second


def test_redis_cleanup_rejects_a_non_test_cache_prefix():
    try:
        require_test_cache_namespace("manor:", "manor:test:worker:")
    except RuntimeError as exc:
        assert "test cache namespace" in str(exc)
    else:
        raise AssertionError("expected cleanup guard to reject a shared prefix")
