"""Creative QA must decode real pixels and certify only the inspected bytes."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import hashlib
import io
import json
from pathlib import Path
import shutil
import threading
from types import SimpleNamespace

from PIL import Image
import pytest

from packages.core.ai.tools import creative_studio_tools as studio


def _image_bytes(image_format: str, color: str = "red") -> bytes:
    encoded = io.BytesIO()
    Image.new("RGB", (512, 640), color).save(encoded, format=image_format)
    return encoded.getvalue()


def _truncated_jpeg() -> bytes:
    # JPEG.verify() checks the container without decoding the pixel stream.
    # Removing the end marker leaves a recognizable image that load() rejects.
    truncated = _image_bytes("JPEG")[:-2]
    with Image.open(io.BytesIO(truncated)) as image:
        image.verify()
    with pytest.raises(OSError):
        with Image.open(io.BytesIO(truncated)) as image:
            image.load()
    return truncated


@pytest.fixture
def verification_run(tmp_path, monkeypatch):
    """Exercise the real verifier without a live database or Workspace writes."""
    from packages.core.config import get_settings

    monkeypatch.setattr(get_settings(), "MANOR_FS_ROOT", str(tmp_path))
    monkeypatch.setattr(get_settings(), "MANOR_FS_ENABLED", True)
    entity_root = tmp_path / "entity"
    directory = "production/run"
    (entity_root / directory).mkdir(parents=True)
    saved = []
    inspected_paths = []
    inspect = studio._inspect_file

    def inspect_snapshot(path):
        inspected_paths.append(Path(path))
        return inspect(path)

    async def source(reference, _context):
        return reference, str(entity_root / reference)

    async def scope(*_args):
        return str(entity_root), "production/review"

    class Session:
        async def scalar(self, _query):
            return SimpleNamespace(id="current-clean-document")

    @asynccontextmanager
    async def session():
        yield Session()

    async def save(_context, _runtime, path, data):
        saved.append((path, data))
        return {"document_id": "verification-receipt", "fs_path": path}

    marked = []

    async def mark(_entity_id, output_directory, final_paths):
        marked.append((output_directory, set(final_paths)))

    monkeypatch.setattr(studio, "_source_path", source)
    monkeypatch.setattr(studio, "_scope", scope)
    monkeypatch.setattr(studio, "_save", save)
    monkeypatch.setattr(studio, "mark_production_results", mark)
    monkeypatch.setattr(studio, "_inspect_file", inspect_snapshot)
    monkeypatch.setattr("packages.core.database.async_session", session)

    async def verify(mode, outputs):
        snapshot = json.dumps({
            "workspace_id": "workspace", "mode": mode, "sources": [],
            "output_directory": directory,
        }).encode()
        (entity_root / directory / "brief.json").write_bytes(snapshot)
        return json.loads(await studio._verify_creative_studio_handler(
            entity_id="entity", workspace_id="workspace",
            snapshot_path=directory + "/brief.json",
            snapshot_sha256=hashlib.sha256(snapshot).hexdigest(),
            files=[directory + "/" + name for name in outputs],
            output_directory="production/review",
        ))

    def write(name, data):
        path = entity_root / directory / name
        path.write_bytes(data)
        return path

    return SimpleNamespace(verify=verify, write=write, saved=saved, directory=directory,
                           entity_root=entity_root, inspected_paths=inspected_paths, marked=marked)


def _assert_private_snapshots_cleaned(run):
    assert run.inspected_paths, "The verifier must inspect at least one real file"
    for path in run.inspected_paths:
        assert not path.is_relative_to(run.entity_root)
        assert not path.exists(), "Private QA copies must be cleaned on success and failure"


@pytest.mark.parametrize("image_format, extension", [
    ("PNG", ".png"), ("JPEG", ".jpg"), ("WEBP", ".webp"),
])
def test_valid_images_remain_decodable(tmp_path, image_format, extension):
    image = tmp_path / ("page" + extension)
    image.write_bytes(_image_bytes(image_format))
    assert studio._inspect_file(str(image)) == {"width": 512, "height": 640}


def test_truncated_jpeg_is_not_a_readable_delivery(tmp_path):
    image = tmp_path / "truncated.jpg"
    image.write_bytes(_truncated_jpeg())
    with pytest.raises(OSError):
        studio._inspect_file(str(image))


async def test_truncated_jpeg_cannot_get_a_passing_receipt(verification_run):
    run = verification_run
    run.write("page-01.jpg", _truncated_jpeg())
    run.write("page-02.png", _image_bytes("PNG", "green"))
    run.write("page-03.webp", _image_bytes("WEBP", "blue"))
    run.write("captions.md", b"Synthetic test captions, not real output evidence.")

    result = await run.verify("carousel", ["page-01.jpg", "page-02.png", "page-03.webp", "captions.md"])

    assert result["status"] == "failed", result
    assert not result.get("technical_checks_passed")
    assert not run.saved
    _assert_private_snapshots_cleaned(run)


async def test_replaced_source_after_qa_cannot_get_a_passing_receipt(verification_run, monkeypatch):
    run = verification_run
    original = run.write("page-01.png", _image_bytes("PNG"))
    run.write("page-02.jpg", _image_bytes("JPEG", "green"))
    run.write("page-03.webp", _image_bytes("WEBP", "blue"))
    run.write("captions.md", b"Synthetic test captions, not real output evidence.")
    inspect = studio._inspect_file
    replaced = []

    def inspect_then_replace(path):
        qa = inspect(path)
        if not replaced:
            # Mutate the original, not the path supplied to the inspector:
            # correct production code inspects a private immutable snapshot.
            assert Path(path) != original
            original.write_bytes(b"This is no longer a PNG after concurrent editing.")
            replaced.append(True)
        return qa

    monkeypatch.setattr(studio, "_inspect_file", inspect_then_replace)
    result = await run.verify("carousel", ["page-01.png", "page-02.jpg", "page-03.webp", "captions.md"])

    assert replaced, "The test must change a source after successful real QA"
    assert result["status"] == "failed", result
    assert not result.get("technical_checks_passed")
    assert not run.saved
    _assert_private_snapshots_cleaned(run)


async def test_valid_mixed_image_package_keeps_original_paths_and_hashes(verification_run):
    run = verification_run
    outputs = {
        "page-01.png": _image_bytes("PNG"),
        "page-02.jpg": _image_bytes("JPEG", "green"),
        "page-03.webp": _image_bytes("WEBP", "blue"),
        "captions.md": b"Synthetic test captions, not real output evidence.",
    }
    for name, data in outputs.items():
        run.write(name, data)

    result = await run.verify("carousel", list(outputs))

    assert result["status"] == "completed", result
    assert result["technical_checks_passed"] is True
    assert len(result["final_files"]) == len(outputs)
    for item in result["final_files"]:
        assert item["fs_path"] == run.directory + "/" + item["name"]
        assert item["sha256"] == hashlib.sha256(outputs[item["name"]]).hexdigest()
        assert item["file_size"] == len(outputs[item["name"]])
        assert item["document_id"] == "current-clean-document"
        if item["kind"] == "image":
            assert item["qa"] == {"width": 512, "height": 640}
    assert len(run.saved) == 1
    receipt = json.loads(run.saved[0][1])
    assert receipt["files"] == result["final_files"]
    # The verified files are the production's results; the rest of its folder is how they were made.
    assert run.marked == [(run.directory, {run.directory + "/" + name for name in outputs})]
    assert receipt["technical_checks_passed"] is True
    _assert_private_snapshots_cleaned(run)


@pytest.mark.media_integration
@pytest.mark.oss_regression
async def test_valid_mp3_uses_real_probe_and_full_decode(verification_run, monkeypatch):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("The actual media verification requires FFmpeg and ffprobe")
    run = verification_run
    fixture = Path(__file__).resolve().parents[1] / "apps/web/public/blueprints/creative/podcast-episode.mp3"
    audio = fixture.read_bytes()
    run.write("episode.mp3", audio)
    run.write("script.md", b"Synthetic test script, not a quality or speech-alignment claim.")
    probe = studio.media._probe_media_report
    process = studio.media._run_process
    probed, decoded = [], []

    async def probe_snapshot(ffprobe, path):
        assert Path(path) in run.inspected_paths
        probed.append(Path(path))
        return await probe(ffprobe, path)

    async def decode_snapshot(args, *, timeout_seconds):
        if args[0] == shutil.which("ffmpeg") and "-i" in args:
            path = Path(args[args.index("-i") + 1])
            assert path in probed
            decoded.append(path)
        return await process(args, timeout_seconds=timeout_seconds)

    monkeypatch.setattr(studio.media, "_probe_media_report", probe_snapshot)
    monkeypatch.setattr(studio.media, "_run_process", decode_snapshot)

    result = await run.verify("podcast", ["episode.mp3", "script.md"])

    assert result["status"] == "completed", result
    assert result["technical_checks_passed"] is True
    item = result["final_files"][0]
    assert item["fs_path"] == run.directory + "/episode.mp3"
    assert item["sha256"] == hashlib.sha256(audio).hexdigest()
    assert item["qa"]["has_audio"] is True
    assert 20 <= item["qa"]["duration_seconds"] <= 600
    assert len(probed) == len(decoded) == 1
    assert len(run.saved) == 1
    _assert_private_snapshots_cleaned(run)


@pytest.mark.parametrize("outside_media", [False, True])
@pytest.mark.media_integration
@pytest.mark.oss_regression
async def test_dubbing_validates_frozen_srt_against_probed_video(
    verification_run, monkeypatch, outside_media,
):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("The actual dubbing verification requires FFmpeg and ffprobe")
    run = verification_run
    fixture = Path(__file__).resolve().parents[1] / "apps/web/public/blueprints/creative/cedar-cup-english.mp4"
    run.write("dubbed.mp4", fixture.read_bytes())
    ending = "00:01:39,000" if outside_media else "00:00:10,000"
    subtitles = f"1\n00:00:00,000 --> {ending}\nSynthetic translated subtitle.\n"
    run.write("subtitles.srt", subtitles.encode())
    run.write("translation.md", b"Fictional test translation notes; no translation quality claim.")
    probe = studio.media._probe_media_report
    validate = studio.media._validate_subtitle_content
    durations, checked = [], []

    async def probe_snapshot(ffprobe, path):
        assert Path(path) in run.inspected_paths
        report = await probe(ffprobe, path)
        durations.append(report["duration_seconds"])
        return report

    def validate_snapshot_content(content, subtitle_format, duration, **kwargs):
        paths = [path for path in run.inspected_paths if path.suffix == ".srt"]
        assert len(paths) == 1 and paths[0].read_text() == content == subtitles
        assert subtitle_format == "srt"
        assert durations == [duration]
        checked.append(True)
        return validate(content, subtitle_format, duration, **kwargs)

    async def unexpected_entity_handler(**_kwargs):
        pytest.fail("Frozen subtitle QA must not reopen mutable Entity files")

    monkeypatch.setattr(studio.media, "_probe_media_report", probe_snapshot)
    monkeypatch.setattr(studio.media, "_validate_subtitle_content", validate_snapshot_content)
    monkeypatch.setattr(studio.media, "_validate_subtitles_handler", unexpected_entity_handler)
    result = await run.verify("dubbing", ["dubbed.mp4", "subtitles.srt", "translation.md"])

    assert checked == [True]
    assert 10 <= durations[0] < 99
    if outside_media:
        assert result["status"] == "failed", result
        assert "cue_outside_media" in result["error"]
        assert not result.get("technical_checks_passed")
        assert not run.saved
    else:
        assert result["status"] == "completed", result
        assert result["technical_checks_passed"] is True
        assert len(run.saved) == 1
        assert result["final_files"][0]["qa"]["duration_seconds"] == durations[0]
    _assert_private_snapshots_cleaned(run)


@pytest.mark.parametrize("data", [b"", b"123456789"])
def test_snapshot_rejects_empty_or_oversized_file_before_copy(verification_run, tmp_path, data):
    run = verification_run
    source = run.write("source.bin", data)
    destination = tmp_path / "captured.bin"

    with pytest.raises(ValueError, match="size limit"):
        studio._capture_verification_file(
            "entity", run.directory + "/source.bin", str(source), str(destination), 8,
        )

    assert not destination.exists()
    assert source.read_bytes() == data


def test_snapshot_exact_size_limit_copies_and_hashes_same_bytes(verification_run, tmp_path):
    run = verification_run
    data = b"12345678"
    source = run.write("source.bin", data)
    destination = tmp_path / "captured.bin"

    digest, size = studio._capture_verification_file(
        "entity", run.directory + "/source.bin", str(source), str(destination), len(data),
    )

    assert (digest, size) == (hashlib.sha256(data).hexdigest(), len(data))
    assert source.read_bytes() == destination.read_bytes() == data
    assert studio._capture_verification_file(
        "entity", run.directory + "/source.bin", str(source), None, len(data),
    ) == (digest, size)


def test_snapshot_rejects_symlink_source(verification_run, tmp_path):
    from packages.core.services.entity_fs import EntityFilesystemError

    run = verification_run
    outside = tmp_path / "outside-source.bin"
    outside.write_bytes(b"outside")
    link = run.entity_root / run.directory / "source.bin"
    link.symlink_to(outside)
    destination = tmp_path / "captured.bin"

    with pytest.raises(EntityFilesystemError):
        studio._capture_verification_file(
            "entity", run.directory + "/source.bin", str(link), str(destination), 8,
        )

    assert not destination.exists()
    assert outside.read_bytes() == b"outside"


@pytest.mark.parametrize("cancel_count", [1, 2])
async def test_cancel_waits_for_snapshot_copy_before_temporary_cleanup(
    verification_run, monkeypatch, cancel_count,
):
    run = verification_run
    run.write("page-01.png", _image_bytes("PNG"))
    started = asyncio.Event()
    release = threading.Event()
    finished = threading.Event()
    loop = asyncio.get_running_loop()
    capture = studio._capture_verification_file
    destinations = []

    def capture_after_release(*args):
        destinations.append(Path(args[3]))
        loop.call_soon_threadsafe(started.set)
        try:
            if not release.wait(timeout=5):
                raise AssertionError("The test did not release the bounded copy")
            return capture(*args)
        finally:
            finished.set()

    monkeypatch.setattr(studio, "_capture_verification_file", capture_after_release)
    task = asyncio.create_task(run.verify("carousel", ["page-01.png"]))
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        assert destinations[0].parent.exists()
        for _ in range(cancel_count):
            task.cancel()
            await asyncio.sleep(0.01)
        assert not task.done(), "Cancellation must not abandon the still-running copy thread"
        assert destinations[0].parent.exists(), "Cleanup must wait until the copy stops"
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert await asyncio.to_thread(finished.wait, 5)

    assert all(not path.parent.exists() for path in destinations)
    assert not run.saved
