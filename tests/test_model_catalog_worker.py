from packages.core.constants.models import CATALOG, DEFAULTS


def test_worker_catalog_replaces_gpt4o_mini_with_gpt4():
    worker_ids = [item["id"] for item in CATALOG["worker"]]

    assert DEFAULTS["worker"] == "openai/gpt-4"
    assert worker_ids[0] == DEFAULTS["worker"]
    assert "openai/gpt-4" in worker_ids
    assert "openai/gpt-4o-mini" not in worker_ids
