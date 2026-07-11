import asyncio

from packages.core.contracts.envelope import Success, Failure
from packages.core.workers.internal import enforce_with_repair


async def _fake_reshaper(shape_name, raw, errors):
    # pretend the model fixed the key name
    return {"files": [{"name": "a.md", "fs_path": "Workspaces/Demo/a.md"}]}


def test_repair_recovers_after_one_pass():
    raw = {"files": [{"name": "a.md", "fs_path": None}]}
    result = asyncio.run(enforce_with_repair("ArtifactResult", raw, reshaper=_fake_reshaper))
    assert isinstance(result, Success)


def test_repair_gives_up_to_failure():
    async def _bad_reshaper(shape_name, raw, errors):
        return {"files": [{"name": "a.md"}]}  # still no fs_path

    raw = {"files": [{"name": "a.md"}]}
    result = asyncio.run(enforce_with_repair("ArtifactResult", raw, reshaper=_bad_reshaper))
    assert isinstance(result, Failure)
