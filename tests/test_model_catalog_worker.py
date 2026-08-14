from packages.core.constants.models import CATALOG, DEFAULTS


def test_worker_default_is_deepseek_v4_flash():
    """The worker tier runs tool-using agent tasks in bulk, so the factory
    default is the cheapest model we trust with tools. Legacy ``openai/gpt-4``
    remains available for continuity even though it is not the default.
    """
    worker_ids = [item["id"] for item in CATALOG["worker"]]

    assert DEFAULTS["worker"] == "deepseek/deepseek-v4-flash"
    assert worker_ids[0] == DEFAULTS["worker"]
    assert "openai/gpt-4" in worker_ids
    # gpt-4o-mini stays out: it was dropped for unreliable tool-calling.
    assert "openai/gpt-4o-mini" not in worker_ids
