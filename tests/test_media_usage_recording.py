"""Video jobs record their gateway usage: cost, tokens, failed requests and their run."""
from types import SimpleNamespace

import pytest

from packages.core.models.media_job import MediaJob
from packages.core.services import model_pricing, usage_service
from packages.core.tasks import media_tasks, video_adapters

OMNI = "google/gemini-omni-1.2-flash"


class _Result:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _Session:
    """Stands in for an AsyncSession that holds one media job; no database."""

    def __init__(self, job):
        self._job = job
        self.added = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def execute(self, _statement):
        return _Result(self._job)

    async def scalar(self, _statement):
        return None

    def add(self, row):
        self.added.append(row)

    async def commit(self):
        return None


def _job(**overrides) -> MediaJob:
    fields = {
        "id": "job_media_1",
        "entity_id": "entity_media",
        "kind": "video",
        "status": "processing",
        "prompt": "A clip",
        "model": OMNI,
        "duration_seconds": 10,
        "byok": False,
        "params": {"workflow_run_id": "run_wf_1"},
    }
    fields.update(overrides)
    return MediaJob(**fields)


@pytest.fixture
def finalize(monkeypatch):
    """Route _finalize_video_job at an in-memory job and record the billing it asks for."""
    import packages.core.database as database
    from packages.core.services import credit_reservations

    calls = SimpleNamespace(bill=[], unknown=[], consumed=[], released=[])

    async def bill(_job, **kwargs):
        calls.bill.append(kwargs)
        return True

    async def unknown(_job, *, pricing_source="unknown"):
        calls.unknown.append((_job.id, pricing_source))
        return True

    async def consume(job_id, credits):
        calls.consumed.append((job_id, credits))

    async def release(job_id, reason):
        calls.released.append((job_id, reason))

    async def release_by_source(_db, *, source_kind, source_id, reason):
        calls.released.append((source_id, reason))

    async def noop(*_args, **_kwargs):
        return None

    monkeypatch.setattr(media_tasks, "_bill_usage", bill)
    monkeypatch.setattr(media_tasks, "_record_unknown_video_cost", unknown)
    monkeypatch.setattr(media_tasks, "_consume_video_reservation", consume)
    monkeypatch.setattr(media_tasks, "_release_video_reservation", release)
    monkeypatch.setattr(media_tasks, "_wake_parked_task", noop)
    monkeypatch.setattr(media_tasks, "_push_notification", noop)
    monkeypatch.setattr(media_tasks, "_project_group_media_if_ready", noop)
    monkeypatch.setattr(credit_reservations, "release_reservation_by_source", release_by_source)

    def install(job):
        monkeypatch.setattr(database, "async_session", lambda: _Session(job))
        return calls

    return install


def test_gemini_omni_is_priced_per_output_token_at_each_resolution():
    # 720p is 5,792 tokens/s at $12 per 1M output tokens: $0.069504 per second.
    assert model_pricing.video_output_tokens(OMNI, 10, "720p") == 57_920
    assert model_pricing.estimate_video_cost_usd(OMNI, 10, "720p") == pytest.approx(0.69504)
    assert model_pricing.estimate_video_cost_usd(OMNI, 5, "360p") == pytest.approx(0.11586)
    assert model_pricing.estimate_video_cost_usd(OMNI, 5, "4k") == pytest.approx(1.04256)
    # A resolution without its own rate is billed at the 720p rate, as per-second models are.
    assert model_pricing.estimate_video_cost_usd(OMNI, 10, "1440p") == pytest.approx(0.69504)


@pytest.mark.parametrize(
    ("model", "duration", "resolution", "expected"),
    [
        # Per-second table entry: $0.151 per second at 720p.
        ("bytedance/seedance-2.0", 5, "720p", 0.755),
        # Per-token table entry: 5,792 output tokens/s at $12 per 1M.
        (OMNI, 10, "720p", 0.69504),
    ],
)
def test_one_table_prices_both_entry_shapes(model, duration, resolution, expected):
    assert model_pricing.priced_video_cost_usd(model, duration, resolution) == pytest.approx(expected)


def test_an_unpriced_model_has_no_price_rather_than_a_zero_one():
    assert model_pricing.video_is_priced("acme/unpriced-video") is False
    assert model_pricing.priced_video_cost_usd("acme/unpriced-video", 5, "720p") is None


def test_provider_usage_is_read_from_gateway_cost_and_token_fields():
    usage = video_adapters.provider_video_usage(
        {"usage": {"inputTokens": 12, "outputTokens": 900}, "providerMetadata": {"gateway": {"cost": "0.42"}}}
    )

    assert usage["cost_usd"] == 0.42
    assert (usage["prompt_tokens"], usage["completion_tokens"]) == (12, 900)
    assert video_adapters.provider_video_usage({"status": "completed"}) is None


# The usage object of one real Gemini Omni 1.2 Flash call: 3 s at 720p.
OMNI_CALL_USAGE = {
    "total_tokens": 18370,
    "total_input_tokens": 29,
    "input_tokens_by_modality": [{"modality": "text", "tokens": 29}],
    "total_output_tokens": 17376,
    "output_tokens_by_modality": [{"modality": "video", "tokens": 17376}],
    "total_thought_tokens": 965,
    "total_cached_tokens": 0,
    "total_tool_use_tokens": 0,
}


def test_thinking_tokens_count_as_output_and_video_is_broken_out():
    usage = video_adapters.provider_video_usage({"usage": OMNI_CALL_USAGE})

    assert usage["prompt_tokens"] == 29
    assert usage["output_tokens"] == 17_376
    assert usage["thought_tokens"] == 965
    assert usage["completion_tokens"] == 17_376 + 965
    assert usage["output_tokens_by_modality"] == {"video": 17_376}
    assert usage["cost_usd"] is None
    assert usage["raw"]["usage"] == OMNI_CALL_USAGE


def test_estimate_without_provider_usage_is_flagged_estimated():
    # 3 s at 720p: 3 × 5,792 output tokens/s, the same figure the real call returned.
    estimate = media_tasks._estimated_video_usage(OMNI, 3, "720p")

    assert estimate["completion_tokens"] == 17_376
    assert estimate["pricing_source"] == "estimated"


def test_provider_usage_leaves_the_cost_to_the_official_token_price():
    result = {"result_url": "/clip.mp4", "cost_usd": 0.5, "pricing_source": "estimated", "completion_tokens": 1}
    usage = video_adapters.provider_video_usage({"usage": OMNI_CALL_USAGE})
    updated = video_adapters.apply_provider_usage(result, usage)

    assert updated["completion_tokens"] == 18_341
    assert updated["provider_usage"] == usage
    assert updated["cost_usd"] == 0.5
    cost, source = media_tasks._official_video_cost(_job(), updated)
    assert cost == pytest.approx(18_341 * 12 / 1_000_000)
    assert source == "official_tokens"


@pytest.mark.asyncio
async def test_official_tokens_bill_the_failed_job_and_the_raw_usage_is_kept(finalize):
    job = _job(params={"generation_id": "gen_failed"})
    calls = finalize(job)
    usage = video_adapters.provider_video_usage({
        "usage": OMNI_CALL_USAGE,
        "providerMetadata": {"gateway": {"cost": "0.3"}},
    })
    result = video_adapters.apply_provider_usage({"error": "Provider failed"}, usage)

    await media_tasks._finalize_video_job(job.id, result)

    # The gateway's 0.3 is not billed: the official 18,341 tokens at $12/M are.
    assert calls.bill[0]["cost_usd"] == pytest.approx(0.220092)
    assert calls.bill[0]["completion_tokens"] == 18_341
    assert job.params["provider_usage"]["raw"]["usage"] == OMNI_CALL_USAGE


@pytest.mark.asyncio
async def test_completed_platform_video_bills_its_official_tokens(finalize):
    job = _job()
    calls = finalize(job)
    result = video_adapters.apply_provider_usage(
        {"result_url": "/clip.mp4", "cost_usd": 0.69504, "completion_tokens": 57_920, "pricing_source": "estimated"},
        {"cost_usd": 0.6, "prompt_tokens": 0, "completion_tokens": 40_000},
    )

    assert await media_tasks._finalize_video_job(job.id, result) is True

    assert len(calls.bill) == 1
    assert calls.bill[0]["cost_usd"] == pytest.approx(0.48)
    assert calls.bill[0]["completion_tokens"] == 40_000
    assert calls.bill[0]["pricing_source"] == "official_tokens"
    assert job.cost_usd == pytest.approx(0.48)


@pytest.mark.asyncio
async def test_completed_platform_video_without_provider_usage_bills_the_estimate(finalize):
    job = _job()
    calls = finalize(job)
    result = {
        "result_url": "/clip.mp4",
        "cost_usd": 0.69504,
        "credits": 1,
        "completion_tokens": 57_920,
        "pricing_source": "estimated",
    }

    assert await media_tasks._finalize_video_job(job.id, result) is True

    assert calls.bill == [{
        "cost_usd": 0.69504,
        "prompt_tokens": 0,
        "completion_tokens": 57_920,
        "pricing_source": "estimated",
    }]


@pytest.mark.parametrize("model", ["bytedance/seedance-2.0", OMNI])
def test_catalog_estimate_is_flagged_estimated_for_priced_models(model):
    estimate = media_tasks._estimated_video_usage(model, 10, "720p")

    assert estimate["pricing_source"] == "estimated"
    assert estimate["cost_usd"] is not None


def test_catalog_estimate_counts_output_tokens_only_for_token_priced_models():
    assert media_tasks._estimated_video_usage(OMNI, 10, "720p")["completion_tokens"] == 57_920
    assert media_tasks._estimated_video_usage("bytedance/seedance-2.0", 10, "720p")["completion_tokens"] == 0


@pytest.mark.asyncio
async def test_completed_unpriced_video_is_recorded_flagged_and_not_zeroed(finalize):
    job = _job(model="acme/unpriced-video", params={"workflow_run_id": "run_wf_1"})
    calls = finalize(job)
    result = media_tasks._estimated_video_usage("acme/unpriced-video", 10, "720p")
    result["result_url"] = "/clip.mp4"

    assert result["cost_usd"] is None
    assert result["pricing_source"] == "unpriced"
    assert await media_tasks._finalize_video_job(job.id, result) is True

    assert job.status == "completed"
    assert job.cost_usd is None
    assert calls.bill == []
    assert calls.unknown == [(job.id, "unpriced")]
    assert [job_id for job_id, _reason in calls.released] == [job.id]


@pytest.mark.asyncio
async def test_completed_unpriced_video_does_not_bill_the_gateway_cost(finalize):
    # An unpriced model has no token price, so the gateway's cost is audit-only.
    job = _job(model="acme/unpriced-video")
    calls = finalize(job)
    result = video_adapters.apply_provider_usage(
        {"result_url": "/clip.mp4", "cost_usd": None, "pricing_source": "unpriced"},
        {"cost_usd": 0.42, "prompt_tokens": 0, "completion_tokens": 0},
    )

    await media_tasks._finalize_video_job(job.id, result)

    assert job.cost_usd is None
    assert calls.bill == []
    assert calls.unknown == [(job.id, "unpriced")]


@pytest.mark.asyncio
async def test_failed_platform_video_with_official_tokens_is_charged_and_consumes_its_reservation(finalize):
    job = _job(params={"workflow_run_id": "run_wf_1", "generation_id": "gen_failed"})
    calls = finalize(job)
    result = video_adapters.apply_provider_usage(
        {"error": "Provider failed: content blocked"},
        {"cost_usd": 0.25, "prompt_tokens": 3, "completion_tokens": 20_000},
    )

    await media_tasks._finalize_video_job(job.id, result)

    assert job.status == "failed"
    assert job.cost_usd == pytest.approx(0.24)
    assert calls.bill == [{
        "cost_usd": pytest.approx(0.24),
        "prompt_tokens": 3,
        "completion_tokens": 20_000,
        "pricing_source": "official_tokens",
    }]
    assert calls.consumed == [(job.id, usage_service.media_cost_credits(job.cost_usd))]
    assert calls.released == []
    assert calls.unknown == []


@pytest.mark.asyncio
async def test_failed_platform_video_with_generation_id_but_no_reported_cost_logs_unknown_cost(finalize):
    job = _job(params={"workflow_run_id": "run_wf_1", "generation_id": "gen_failed"})
    calls = finalize(job)

    await media_tasks._finalize_video_job(job.id, {"error": "Provider failed"})

    assert job.status == "failed"
    assert job.cost_usd is None
    assert calls.unknown == [(job.id, "unknown")]
    assert calls.bill == []
    assert [job_id for job_id, _reason in calls.released] == [job.id]


@pytest.mark.asyncio
async def test_failed_video_that_never_reached_the_gateway_records_no_cost(finalize):
    job = _job(params={"workflow_run_id": "run_wf_1"})
    calls = finalize(job)

    await media_tasks._finalize_video_job(job.id, {"error": "Provider connection timed out."})

    assert job.status == "failed"
    assert job.cost_usd is None
    assert calls.bill == []
    assert calls.unknown == []
    assert [job_id for job_id, _reason in calls.released] == [job.id]


@pytest.mark.asyncio
async def test_failed_own_key_video_that_reached_its_provider_records_byok_tokens_only(finalize):
    job = _job(byok=True, params={"generation_id": "gen_own_key"})
    calls = finalize(job)

    await media_tasks._finalize_video_job(job.id, {"error": "Provider failed"})

    # The provider bills the user: a BYOK row with no cost and no credit.
    assert calls.bill == [{
        "cost_usd": None,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "pricing_source": "unknown",
        "byok_estimate_usd": None,
    }]
    assert calls.unknown == []
    assert calls.released == []


@pytest.fixture
def token_rows(monkeypatch):
    """Run the real BYOK recording path on a fake session, capturing the token rows and any credit rows."""
    import packages.core.database as database

    session = _Session(None)
    logged = []

    async def log_token_usage(_db, entity_id, **kwargs):
        logged.append(kwargs)

    async def no_payer(_db, _job):
        return None

    monkeypatch.setattr(database, "async_session", lambda: session)
    monkeypatch.setattr(usage_service, "log_token_usage", log_token_usage)
    monkeypatch.setattr(media_tasks, "_media_job_payer", no_payer)
    return SimpleNamespace(logged=logged, session=session)


@pytest.mark.asyncio
async def test_completed_own_key_video_records_provider_tokens_and_an_estimate(finalize):
    job = _job(byok=True)
    calls = finalize(job)
    result = video_adapters.apply_provider_usage(
        {"result_url": "/clip.mp4", "cost_usd": 0.69504, "completion_tokens": 57_920, "pricing_source": "estimated"},
        {"cost_usd": None, "prompt_tokens": 4, "completion_tokens": 900},
    )

    await media_tasks._finalize_video_job(job.id, result)

    # 900 official output tokens at $12 per 1M.
    assert calls.bill == [{
        "cost_usd": None,
        "prompt_tokens": 4,
        "completion_tokens": 900,
        "pricing_source": "byok_estimate",
        "byok_estimate_usd": pytest.approx(0.0108),
    }]
    assert calls.unknown == []
    assert calls.released == []


@pytest.mark.asyncio
async def test_own_key_thinking_tokens_are_recorded_as_provider_output(finalize):
    job = _job(byok=True)
    calls = finalize(job)
    result = video_adapters.apply_provider_usage(
        {"result_url": "/clip.mp4"},
        video_adapters.provider_video_usage({"usage": OMNI_CALL_USAGE}),
    )

    await media_tasks._finalize_video_job(job.id, result)

    assert calls.bill == [{
        "cost_usd": None,
        "prompt_tokens": 29,
        "completion_tokens": 18_341,
        "pricing_source": "byok_estimate",
        "byok_estimate_usd": pytest.approx(0.220092),
    }]


@pytest.mark.asyncio
async def test_own_key_video_without_provider_usage_is_estimated_and_flagged(finalize):
    job = _job(byok=True)
    calls = finalize(job)
    result = {"result_url": "/clip.mp4", **media_tasks._estimated_video_usage(OMNI, 3, "720p")}

    await media_tasks._finalize_video_job(job.id, result)

    # Estimated tokens are not a provider count, so the row keeps no cost.
    assert calls.bill == [{
        "cost_usd": None,
        "prompt_tokens": 0,
        "completion_tokens": 17_376,
        "pricing_source": "estimated",
        "byok_estimate_usd": None,
    }]


@pytest.mark.asyncio
async def test_own_key_omni_video_writes_an_estimated_byok_token_row_and_no_credit(token_rows):
    job = _job(byok=True)
    result = {
        "result_url": "/clip.mp4",
        "provider_usage": {"cost_usd": None, "prompt_tokens": 29, "completion_tokens": 23_988},
    }

    await media_tasks._record_own_key_video(job, result, reached_gateway=True)

    assert len(token_rows.logged) == 1
    row = token_rows.logged[0]
    # 23,988 official output tokens at $12 per 1M, the BYOK estimate.
    assert row["cost_usd"] == pytest.approx(0.287856)
    assert row["pricing_source"] == "byok_estimate"
    assert row["billing_mode"] == "byok"
    assert row["completion_tokens"] == 23_988
    # Nothing is charged: no credit row was added to the session.
    assert token_rows.session.added == []


@pytest.mark.asyncio
async def test_own_key_unpriced_video_keeps_a_null_cost(token_rows):
    job = _job(byok=True, model="acme/unpriced-video")
    result = {
        "result_url": "/clip.mp4",
        "provider_usage": {"cost_usd": None, "prompt_tokens": 4, "completion_tokens": 900},
    }

    await media_tasks._record_own_key_video(job, result, reached_gateway=True)

    assert len(token_rows.logged) == 1
    assert token_rows.logged[0]["cost_usd"] is None
    assert token_rows.logged[0]["pricing_source"] == "provider_usage"
    assert token_rows.session.added == []


@pytest.mark.asyncio
async def test_own_key_row_is_byok_with_no_charge_and_no_estimate_when_none_is_given(monkeypatch):
    import packages.core.database as database

    recorded = []

    async def record(_db, entity_id, **kwargs):
        recorded.append(kwargs)
        return False

    async def no_payer(_db, _job):
        return None

    monkeypatch.setattr(database, "async_session", lambda: _Session(None))
    monkeypatch.setattr(usage_service, "record_media_usage", record)
    monkeypatch.setattr(media_tasks, "_media_job_payer", no_payer)

    await media_tasks._bill_usage(
        _job(byok=True), cost_usd=None, prompt_tokens=29, completion_tokens=18_341, pricing_source="provider_usage",
    )

    assert recorded[-1]["byok"] is True
    assert recorded[-1]["cost_usd"] is None
    assert recorded[-1]["completion_tokens"] == 18_341
    assert recorded[-1]["pricing_source"] == "provider_usage"


@pytest.mark.asyncio
async def test_failed_own_key_video_that_never_reached_its_provider_records_nothing(finalize):
    job = _job(byok=True, params={})
    calls = finalize(job)

    await media_tasks._finalize_video_job(job.id, {"error": "Provider connection timed out."})

    assert calls.bill == []
    assert calls.unknown == []


@pytest.mark.asyncio
async def test_video_usage_is_attributed_to_the_owning_run_and_task_not_the_media_job(monkeypatch):
    import packages.core.database as database

    recorded = []

    async def record(_db, entity_id, **kwargs):
        recorded.append(kwargs)
        return True

    async def no_payer(_db, _job):
        return None

    monkeypatch.setattr(database, "async_session", lambda: _Session(None))
    monkeypatch.setattr(usage_service, "record_media_usage", record)
    monkeypatch.setattr(media_tasks, "_media_job_payer", no_payer)

    assert await media_tasks._bill_usage(
        _job(params={"run_id": "run_from_run_id"}), cost_usd=0.1, pricing_source="estimated",
    ) is True
    assert recorded[-1]["operation_id"] == "run_from_run_id"
    assert recorded[-1]["media_job_id"] == "job_media_1"
    assert recorded[-1]["byok"] is False
    assert recorded[-1]["pricing_source"] == "estimated"

    assert recorded[-1]["task_id"] is None

    await media_tasks._bill_usage(
        _job(params={"workflow_run_id": "run_wf_1", "run_id": "run_other", "task_id": "task_video"}),
        cost_usd=0.1, pricing_source="estimated",
    )
    assert recorded[-1]["operation_id"] == "run_wf_1"
    # The worker has no billing context; the job names the Task that started it.
    assert recorded[-1]["task_id"] == "task_video"


@pytest.mark.asyncio
async def test_generation_id_is_stored_on_the_media_job(monkeypatch):
    import packages.core.database as database

    job = _job(params={})

    async def no_broadcast(*_args, **_kwargs):
        return None

    monkeypatch.setattr(database, "async_session", lambda: _Session(job))
    monkeypatch.setattr(media_tasks, "_broadcast_video_progress", no_broadcast)

    await media_tasks._remember_provider_poll(
        job.id, "openrouter", "https://openrouter.ai/api/v1/videos/gen_1", "gen_1",
    )

    assert job.params["generation_id"] == "gen_1"
    assert job.params["provider_poll"]["generation_id"] == "gen_1"
