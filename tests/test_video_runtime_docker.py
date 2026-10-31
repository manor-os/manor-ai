"""Offline Linux regression; opt in with MANOR_VIDEO_DOCKER_TEST=1.

Uses an already-built sandbox-skill image (MANOR_VIDEO_DOCKER_IMAGE, default
sandbox-skill:latest) and only owned disposable containers. No running Manor
sandbox, network, or user media is involved.
"""
from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
import subprocess
import sys
import textwrap
import uuid

import pytest


pytestmark = [
    pytest.mark.docker,
    pytest.mark.skipif(
        os.getenv("MANOR_VIDEO_DOCKER_TEST") != "1",
        reason="Set MANOR_VIDEO_DOCKER_TEST=1 to use the local sandbox image",
    ),
]
ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "packages/core/ai/skills/video-edit/scripts"
IMAGE = os.getenv("MANOR_VIDEO_DOCKER_IMAGE", "sandbox-skill:latest")
# The bundle ships the runtime and its FFmpeg governor side by side.
RUNTIME = "/skill/scripts/video_runtime.py"


def _docker(*args: str, timeout: int = 30) -> str:
    result = subprocess.run(
        ["docker", *args], capture_output=True, text=True, timeout=timeout,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout.strip()


def _write_project(project: Path, html: str) -> None:
    (project / "assets").mkdir()
    (project / "video.json").write_text(json.dumps({
        "paths": {"blocks": "compositions", "components": "compositions/components", "assets": "assets"},
        "media": {"autoProxy": True}, "authoringSkill": "video-edit",
    }))
    (project / "package.json").write_text(json.dumps({
        "name": "video-runtime-regression", "private": True, "type": "module",
    }))
    (project / "index.html").write_text(html)


@contextlib.contextmanager
def _sandbox(project: Path):
    """A container with the production render sandbox's limits."""
    _docker("image", "inspect", IMAGE)
    name = f"manor-video-regression-{uuid.uuid4().hex[:12]}"
    _docker(
        "create", "--pull=never", "--name", name, "--init",
        "--pids-limit", "512", "--cpus", "4", "--memory", "4g",
        "--network", "none", "--workdir", "/skill/project",
        "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
        "--tmpfs", "/tmp:exec,size=1g", "--tmpfs", "/var/tmp:exec,size=1g",
        "--tmpfs", "/root:exec,size=128m",
        "--tmpfs", "/home/sandbox:exec,size=512m,uid=1000,gid=1000,mode=0755",
        "--mount", f"type=bind,src={project},dst=/skill/project",
        "--mount", f"type=bind,src={SCRIPTS},dst=/skill/scripts,readonly",
        "--env", "DO_NOT_TRACK=1", IMAGE, "sleep", "infinity",
    )
    try:
        _docker("start", name)
        yield name
    finally:
        # The UUID name is created above, never discovered from live containers.
        _docker("rm", "-f", name)


# Runs the runtime inside the container while sampling the cgroup's PIDs.
_MEASURED_RENDER = textwrap.dedent("""\
    import json, subprocess, sys, threading
    from pathlib import Path
    cgroup = Path('/sys/fs/cgroup')
    before = (cgroup/'pids.events').read_text()
    samples = []
    done = threading.Event()
    def monitor():
        while not done.is_set():
            samples.append(int((cgroup/'pids.current').read_text()))
            done.wait(0.02)
    watcher = threading.Thread(target=monitor)
    watcher.start()
    try:
        result = subprocess.run(
            ['python3', '%s', 'render', '--quality', 'high',
             '--workers', '4', '--output', 'renders/regression.mp4'],
            capture_output=True, text=True, timeout=int(sys.argv[1]),
        )
    finally:
        done.set()
        watcher.join()
    peak = cgroup/'pids.peak'
    zombies = []
    for path in Path('/proc').glob('[0-9]*/stat'):
        try:
            if path.read_text().rpartition(')')[2].split()[0] == 'Z':
                zombies.append(path.parent.name)
        except FileNotFoundError:
            pass
    print(json.dumps({
        'exit_code': result.returncode, 'stdout': result.stdout, 'stderr': result.stderr,
        'pids_sampled_peak': max(samples),
        'pids_peak': int(peak.read_text()) if peak.exists() else None,
        'pids_events_before': before, 'pids_events_after': (cgroup/'pids.events').read_text(),
        'zombie_pids': zombies,
    }))
    """) % RUNTIME


@pytest.fixture(scope="module")
def video_container(tmp_path_factory):
    project = tmp_path_factory.mktemp("video-runtime-docker")
    _write_project(project, textwrap.dedent("""\
        <!doctype html>
        <html lang="en"><head><meta charset="utf-8">
        <meta name="viewport" content="width=640, height=360">
        <title>Video runtime regression</title>
        <style>
        body { margin: 0; background: #000; color: #fff; font-family: sans-serif; }
        #root { position: relative; width: 640px; height: 360px; overflow: hidden; }
        #source { position: absolute; inset: 0; width: 640px; height: 360px; }
        .caption { position: absolute; left: 80px; top: 280px; width: 480px;
          height: 48px; margin: 0; display: grid; place-items: center;
          background: #000; color: #fff; font-size: 24px; }
        </style></head><body>
        <div id="root" data-composition-id="regression" data-start="0"
          data-width="640" data-height="360" data-duration="4" data-fps="30">
          <video id="source" class="clip" src="assets/source.mp4" muted playsinline
            data-start="0" data-duration="4" data-track-index="0"></video>
          <p id="caption-a" class="clip caption" data-start="0" data-duration="2"
            data-track-index="1">First subtitle</p>
          <p id="caption-b" class="clip caption" data-start="2" data-duration="2"
            data-track-index="1">Second subtitle</p>
        </div>
        <script src="./assets/vendor/gsap.min.js"></script>
        <script>
        const tl = gsap.timeline({ paused: true });
        tl.to({}, { duration: 4 });
        window.__timelines = window.__timelines || {};
        window.__timelines.regression = tl;
        </script></body></html>
        """))
    with _sandbox(project) as name:
        _docker(
            "exec", name, "ffmpeg", "-v", "error", "-f", "lavfi", "-i",
            "testsrc2=size=640x360:rate=30:duration=4", "-c:v", "libx264",
            "-threads", "2", "-pix_fmt", "yuv420p", "assets/source.mp4",
        )
        yield name, project


def test_linux_timeout_reaps_detached_children(video_container):
    name, _project = video_container
    script = textwrap.dedent("""\
        import importlib.util, json, signal, subprocess, sys, time
        from pathlib import Path
        spec = importlib.util.spec_from_file_location('runtime', '/skill/scripts/video_runtime.py')
        runtime = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(runtime)
        runtime.TERMINATE_GRACE_SECONDS = 0.2
        child = 'import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(30)'
        parent = (
            'import signal,subprocess,sys,time; '
            'signal.signal(signal.SIGTERM,signal.SIG_IGN); '
            f'child=subprocess.Popen([sys.executable,"-c",{child!r}],start_new_session=True); '
            'print(child.pid,flush=True); '
            'print("capture stalled",file=sys.stderr,flush=True); time.sleep(30)'
        )
        handler = signal.getsignal(signal.SIGTERM)
        started = time.monotonic()
        try:
            runtime._run([sys.executable, '-c', parent], timeout=0.5)
            raise AssertionError('The fixture must time out')
        except subprocess.TimeoutExpired as error:
            assert 'capture stalled' in error.stderr
            pid = int(error.stdout.strip())
        assert signal.getsignal(signal.SIGTERM) == handler
        path = Path(f'/proc/{pid}')
        deadline = time.monotonic() + 3
        while path.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not path.exists(), 'Docker init did not reap the detached child'
        print(json.dumps({'detached_child_reaped': True, 'seconds': time.monotonic()-started}))
        """)
    report = json.loads(_docker("exec", name, "python3", "-c", script))
    print("Linux cleanup:", json.dumps(report))
    assert report["detached_child_reaped"] is True


def test_single_worker_renders_source_video_with_subtitles_under_pid_limit(video_container):
    name, project = video_container
    version = _docker("exec", name, "hyperframes", "--version")
    assert "0.7.105" in version, version
    report = json.loads(_docker("exec", name, "python3", "-c", _MEASURED_RENDER, "150", timeout=180))
    (project / "regression-report.json").write_text(json.dumps(report, indent=2))
    assert report["exit_code"] == 0, report
    result = json.loads(report["stdout"])
    assert result["status"] == "ok", result
    assert result["resources"]["workers_requested"] == 4
    assert result["resources"]["workers"] == 1
    assert result["resources"]["pids_limit"] == 512
    assert report["pids_events_before"] == report["pids_events_after"]
    assert report["pids_sampled_peak"] <= 448, "Keep at least 64 PIDs for cancellation"
    assert report["zombie_pids"] == []
    probe = json.loads(_docker(
        "exec", name, "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-count_frames", "-show_entries", "stream=width,height,nb_read_frames:format=duration",
        "-of", "json", "/skill/project/renders/regression.mp4",
    ))
    assert probe["streams"][0] == {"width": 640, "height": 360, "nb_read_frames": "120"}
    assert float(probe["format"]["duration"]) == pytest.approx(4, abs=0.05)
    assert (project / "renders/regression.mp4").stat().st_size > 0
    print("Docker render:", json.dumps({
        "version": version, "resources": result["resources"], "probe": probe,
        "pids_sampled_peak": report["pids_sampled_peak"], "pids_peak": report["pids_peak"],
        "pids_events": report["pids_events_after"], "zombies": report["zombie_pids"],
        "report": str(project / "regression-report.json"),
    }))


SLOW_PAGE_SECONDS = 15


@pytest.fixture(scope="module")
def slow_page_container(tmp_path_factory):
    """A page that reaches DOMContentLoaded only after 15 s, as a starved host did.

    Incident 01M50AHSWQEPZ7YTC4BQH6FDC0 (load ~274): every review failed with
    "Navigation timeout of 10000 ms exceeded" from HyperFrames' fixed 10 s
    diagnostic page.goto budget.
    """
    project = tmp_path_factory.mktemp("video-runtime-slow-page")
    _write_project(project, textwrap.dedent("""\
        <!doctype html>
        <html lang="en"><head><meta charset="utf-8">
        <meta name="viewport" content="width=640, height=360">
        <title>Slow page regression</title>
        <script>
        // Parser-blocking work stands in for a CPU-starved Chromium. The
        // wall clock is reached by index so the determinism lint (which
        // rightly rejects clocks in compositions) does not stop the review
        // before the browser runs; the cap keeps it finite if it is frozen.
        (function () {
          var clock = Date["now"], until = clock() + %(slow_ms)d, spins = 0;
          while (clock() < until && spins++ < 4e9) {}
        })();
        </script>
        <style>
        body { margin: 0; background: #000; color: #fff; font-family: sans-serif; }
        #root { position: relative; width: 640px; height: 360px; overflow: hidden; }
        .title { position: absolute; left: 80px; top: 150px; width: 480px; height: 60px;
          margin: 0; display: grid; place-items: center; font-size: 32px; }
        </style></head><body>
        <div id="root" data-composition-id="slow" data-start="0"
          data-width="640" data-height="360" data-duration="2" data-fps="30">
          <p id="title" class="clip title" data-start="0" data-duration="2"
            data-track-index="0">Slow page</p>
        </div>
        <script src="./assets/vendor/gsap.min.js"></script>
        <script>
        const tl = gsap.timeline({ paused: true });
        tl.to({}, { duration: 2 });
        window.__timelines = window.__timelines || {};
        window.__timelines.slow = tl;
        </script></body></html>
        """) % {"slow_ms": SLOW_PAGE_SECONDS * 1000})
    with _sandbox(project) as name:
        yield name, project


def test_review_survives_a_page_slower_than_the_cli_navigation_default(slow_page_container):
    name, _project = slow_page_container
    result = subprocess.run(
        ["docker", "exec", name, "python3", RUNTIME, "review", "--snapshot-times", "0.5,1.5"],
        capture_output=True, text=True, timeout=900,
    )
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    # Before the fix: snapshot_failed, "Navigation timeout of 10000 ms exceeded".
    assert "Navigation timeout" not in result.stdout, payload
    assert result.returncode == 0, payload
    assert payload["status"] == "ok" and payload["review_passed"] is True, payload
    frames = [path for path in payload["files"] if "frame-" in path]
    assert len(frames) == 2, payload
    print("Docker slow-page review:", json.dumps({"files": payload["files"]}))


MONTAGE_LAYERS = 14


@pytest.fixture(scope="module")
def montage_container(tmp_path_factory):
    """The CITY PULSE incident shape: 14 video layers, each a separate source.

    HyperFrames extracts all of them at once; ungoverned, each FFmpeg sized
    its thread pools from every host CPU and the 512-PID sandbox ran out.
    """
    project = tmp_path_factory.mktemp("video-runtime-montage")
    layers = "\n".join(
        f'  <video id="shot-{index}" class="clip shot" src="assets/shot-{index}.mp4" muted playsinline'
        f' data-start="{index * 0.5:g}" data-duration="0.5" data-track-index="0"></video>'
        for index in range(MONTAGE_LAYERS)
    )
    duration = MONTAGE_LAYERS * 0.5
    _write_project(project, textwrap.dedent("""\
        <!doctype html>
        <html lang="en"><head><meta charset="utf-8">
        <meta name="viewport" content="width=360, height=640">
        <title>Montage regression</title>
        <style>
        body { margin: 0; background: #000; }
        #root { position: relative; width: 360px; height: 640px; overflow: hidden; }
        .shot { position: absolute; inset: 0; width: 360px; height: 640px; }
        </style></head><body>
        <div id="root" data-composition-id="montage" data-start="0"
          data-width="360" data-height="640" data-duration="%(duration)g" data-fps="30">
        %(layers)s
        </div>
        <script src="./assets/vendor/gsap.min.js"></script>
        <script>
        const tl = gsap.timeline({ paused: true });
        tl.to({}, { duration: %(duration)g });
        window.__timelines = window.__timelines || {};
        window.__timelines.montage = tl;
        </script></body></html>
        """) % {"duration": duration, "layers": layers})
    with _sandbox(project) as name:
        for index in range(MONTAGE_LAYERS):
            _docker(
                "exec", name, "ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                "testsrc2=size=720x1280:rate=30:duration=1", "-c:v", "libx264",
                "-threads", "2", "-pix_fmt", "yuv420p", f"assets/shot-{index}.mp4",
            )
        yield name, project


def test_montage_extraction_stays_inside_the_sandbox_pid_budget(montage_container):
    name, project = montage_container
    report = json.loads(_docker("exec", name, "python3", "-c", _MEASURED_RENDER, "600", timeout=660))
    (project / "regression-report.json").write_text(json.dumps(report, indent=2))
    assert report["exit_code"] == 0, report
    result = json.loads(report["stdout"])
    assert result["status"] == "ok", result
    # Governed from the first attempt: no PID denial, no reduced retry needed.
    assert "resource_retry" not in result
    assert report["pids_events_before"] == report["pids_events_after"]
    assert report["pids_sampled_peak"] <= 448, "Keep at least 64 PIDs for cancellation"
    media = result["resources"]["media"]
    assert 1 <= media["slots"] <= result["resources"]["cpu_limit"]
    assert media["media_elements"] == MONTAGE_LAYERS
    probe = json.loads(_docker(
        "exec", name, "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-count_frames", "-show_entries", "stream=nb_read_frames",
        "-of", "json", "/skill/project/renders/regression.mp4",
    ))
    assert probe["streams"][0]["nb_read_frames"] == str(MONTAGE_LAYERS * 15)
    print("Docker montage render:", json.dumps({
        "media": media, "pids_sampled_peak": report["pids_sampled_peak"],
        "pids_peak": report["pids_peak"], "pids_events": report["pids_events_after"],
    }))


NARRATION_STARTS = [0, 1.5, 3, 4.5, 6, 7.5]


@pytest.fixture(scope="module")
def narrated_container(tmp_path_factory):
    """Picture book 01M58KZRHR9WJG0GMFZJEFPTTY's audio shape: a music bed plus six
    48 kHz stereo narration clips whose elements a script creates."""
    project = tmp_path_factory.mktemp("video-runtime-narration")
    _write_project(project, textwrap.dedent("""\
        <!doctype html>
        <html lang="zh-CN"><head><meta charset="utf-8">
        <title>Narration regression</title>
        <style>
        body { margin: 0; background: #000; }
        #root { position: relative; width: 640px; height: 360px; overflow: hidden; background: #433327; }
        .title { position: absolute; left: 80px; top: 150px; width: 480px; height: 60px;
          margin: 0; color: #fff; font-size: 32px; }
        </style></head><body>
        <div id="root" data-composition-id="narrated" data-start="0"
          data-width="640" data-height="360" data-duration="9" data-fps="30">
          <p id="title" class="clip title" data-start="0" data-duration="9"
            data-track-index="0">Postcard</p>
          <audio id="music" class="clip" data-start="0" data-duration="9" data-track-index="10"
            data-volume="0.4" src="assets/references/music.wav"></audio>
        </div>
        <script src="./assets/vendor/gsap.min.js"></script>
        <script>
        const root = document.querySelector('#root');
        const starts = %(starts)s;
        for (let i = 0; i < starts.length; i++) {
          const clip = document.createElement('audio');
          clip.id = 'vo' + i;
          clip.className = 'clip';
          clip.dataset.start = starts[i];
          clip.dataset.duration = 1.2;
          clip.dataset.trackIndex = 11;
          clip.src = 'assets/references/vo-s0' + (i + 1) + '.wav';
          root.appendChild(clip);
        }
        const tl = gsap.timeline({ paused: true });
        tl.to('#music', { volume: 0, duration: 1 }, 8);
        window.__timelines = window.__timelines || {};
        window.__timelines.narrated = tl;
        </script></body></html>
        """) % {"starts": json.dumps(NARRATION_STARTS)})
    with _sandbox(project) as name:
        _docker("exec", name, "mkdir", "-p", "assets/references")
        _docker(
            "exec", name, "ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=f=220:d=20:r=48000",
            "-ac", "2", "-c:a", "pcm_s16le", "assets/references/music.wav",
        )
        yield name, project


def _render_narrated(name: str) -> dict:
    result = subprocess.run(
        ["docker", "exec", name, "python3", RUNTIME, "render", "--quality", "standard",
         "--workers", "2", "--reviewed", "--output", "renders/narrated.mp4"],
        capture_output=True, text=True, timeout=600,
    )
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_narration_mix_renders_and_a_missing_clip_is_a_structured_audio_failure(narrated_container):
    name, _project = narrated_container
    # The page's narration files are absent, as in the render that left them out.
    missing = _render_narrated(name)
    assert missing["code"] == "render_audio_failed", missing
    assert (missing["reason"], missing["step"], missing["retryable"]) == (
        "source_not_found", "audio_process", False,
    )
    assert missing["missing_media"], missing
    assert all(path.startswith("assets/references/vo-s0") for path in missing["missing_media"])

    for index in range(len(NARRATION_STARTS)):
        _docker(
            "exec", name, "ffmpeg", "-v", "error", "-f", "lavfi",
            "-i", f"sine=f={440 + index * 40}:d=1.2:r=48000", "-ac", "2", "-c:a", "pcm_s16le",
            f"assets/references/vo-s0{index + 1}.wav",
        )
    rendered = _render_narrated(name)
    assert rendered["status"] == "ok", rendered
    probe = json.loads(_docker(
        "exec", name, "ffprobe", "-v", "error", "-show_entries", "stream=codec_type:format=duration",
        "-of", "json", "/skill/project/renders/narrated.mp4",
    ))
    assert sorted(stream["codec_type"] for stream in probe["streams"]) == ["audio", "video"]
    assert float(probe["format"]["duration"]) == pytest.approx(9, abs=0.05)
    print("Docker narration render:", json.dumps({"missing": missing["missing_media"], "probe": probe}))


def test_strict_rule_list_is_generated_from_the_image_cli():
    """The committed rule list is what the image's pinned CLI can report."""
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/hyperframes_check_rules.py"), "--image", IMAGE, "--check"],
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr


_LAYERED_PICTURE = """\
<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=640, height=360">
<title>Layered picture</title>
<style>
body { margin: 0; background: #000; }
#root { position: relative; width: 640px; height: 360px; overflow: hidden; }
.layer { position: absolute; left: 0; top: 0; width: 640px; height: 360px; }
#backdrop { background: url(assets/picture.png) center / cover; filter: blur(8px); }
#subject { left: 200px; top: 90px; width: 240px; height: 180px; }
</style></head><body>
<div id="root" data-composition-id="layered" data-start="0"
  data-width="640" data-height="360" data-duration="2" data-fps="30">
  %(backdrop)s
  <img id="subject" class="clip layer" src="assets/picture.png" alt=""
    data-start="0" data-duration="2" data-track-index="1">
</div>
<script src="./assets/vendor/gsap.min.js"></script>
<script>
const tl = gsap.timeline({ paused: true });
tl.to("#subject", { x: 12, duration: 2, ease: "none" }, 0);
window.__timelines = window.__timelines || {};
window.__timelines.layered = tl;
</script></body></html>
"""


@pytest.fixture(scope="module")
def layered_picture_container(tmp_path_factory):
    """One illustration used as both a backdrop and the subject (picture_book)."""
    project = tmp_path_factory.mktemp("video-runtime-layered-picture")
    _write_project(project, _LAYERED_PICTURE % {"backdrop": (
        '<img id="backdrop" class="clip layer" src="assets/picture.png" alt=""\n'
        '    data-start="0" data-duration="2" data-track-index="0">'
    )})
    with _sandbox(project) as name:
        _docker(
            "exec", name, "ffmpeg", "-v", "error", "-f", "lavfi", "-i",
            "testsrc2=size=640x360", "-frames:v", "1", "assets/picture.png",
        )
        yield name, project


def test_duplicate_media_review_carries_its_fix_and_the_fix_passes(layered_picture_container):
    from packages.core.ai.tools.video_edit_tools import _review_findings

    name, project = layered_picture_container

    def review() -> dict:
        result = subprocess.run(
            ["docker", "exec", name, "python3", RUNTIME, "review", "--snapshot-times", "1"],
            capture_output=True, text=True, timeout=600,
        )
        return json.loads(result.stdout.strip().splitlines()[-1])

    failed = review()
    assert failed["status"] == "ok" and failed["review_passed"] is False, failed
    diagnostics, _advisories = _review_findings(failed["check_report"])
    duplicate = next(item for item in diagnostics if item["code"] == "duplicate_media_discovery_risk")
    assert duplicate["file"] == "index.html" and duplicate["severity"] == "warning"
    assert duplicate["fix_hint"] and "background-image" in duplicate["manor_hint"]

    # The guidance the review returned: paint the extra layer as a CSS background.
    (project / "index.html").write_text(_LAYERED_PICTURE % {"backdrop": (
        '<div id="backdrop" class="clip layer" data-start="0" data-duration="2" data-track-index="0"></div>'
    )})
    passed = review()
    assert passed["status"] == "ok" and passed["review_passed"] is True, passed
    print("Docker duplicate-media review:", json.dumps({"failed": diagnostics}))


# The sub-composition contract the video-edit skill teaches: the host and the
# file's root share one data-composition-id; the file's script (inside the
# <template>) registers a paused timeline under that id, or the host says
# data-no-timeline. Otherwise export waits for the timeline and times out.
_SCENE_WITH_TIMELINE = """\
<template id="s01-template">
  <div id="s01-root" data-composition-id="s01" data-width="640" data-height="360">
    <h2 id="s01-title" class="title">Scene one</h2>
    <style>#s01-root .title { position: absolute; left: 80px; top: 140px;
      margin: 0; color: #fff; font: 48px sans-serif; }</style>
    <script>
      window.__timelines = window.__timelines || {};
      const tl = gsap.timeline({ paused: true });
      tl.fromTo("#s01-title", { x: -40 }, { x: 0, duration: 2, ease: "none" }, 0);
      window.__timelines["s01"] = tl;
    </script>
  </div>
</template>
"""
_SCENE_WITHOUT_TIMELINE = """\
<template id="s02-template">
  <div id="s02-root" data-composition-id="s02" data-width="640" data-height="360">
    <h2 id="s02-title" class="title">Scene two</h2>
    <style>#s02-root .title { position: absolute; left: 80px; top: 140px;
      margin: 0; color: #fff; font: 48px sans-serif; }</style>
  </div>
</template>
"""


@pytest.fixture(scope="module")
def sub_composition_container(tmp_path_factory):
    project = tmp_path_factory.mktemp("video-runtime-sub-compositions")
    _write_project(project, textwrap.dedent("""\
        <!doctype html>
        <html lang="en"><head><meta charset="utf-8">
        <meta name="viewport" content="width=640, height=360">
        <title>Sub-compositions</title>
        <style>
        body { margin: 0; background: #000; }
        #root { position: relative; width: 640px; height: 360px; overflow: hidden; }
        #bg { position: absolute; inset: 0; background: #123; }
        </style></head><body>
        <div id="root" data-composition-id="scenes" data-start="0"
          data-width="640" data-height="360" data-duration="4" data-fps="30">
          <div id="bg" class="clip" data-start="0" data-duration="4" data-track-index="0"></div>
          <div id="s01-host" data-composition-id="s01" data-composition-src="compositions/s01.html"
            data-start="0" data-duration="2" data-track-index="1"></div>
          <div id="s02-host" data-composition-id="s02" data-composition-src="compositions/s02.html"
            data-no-timeline data-start="2" data-duration="2" data-track-index="1"></div>
        </div>
        <script src="./assets/vendor/gsap.min.js"></script>
        <script>
        window.__timelines = window.__timelines || {};
        const tl = gsap.timeline({ paused: true });
        tl.to("#bg", { opacity: 0.8, duration: 4, ease: "none" }, 0);
        window.__timelines.scenes = tl;
        </script></body></html>
        """))
    (project / "compositions").mkdir()
    (project / "compositions/s01.html").write_text(_SCENE_WITH_TIMELINE)
    (project / "compositions/s02.html").write_text(_SCENE_WITHOUT_TIMELINE)
    with _sandbox(project) as name:
        yield name, project


def test_sub_composition_contract_reviews_and_exports(sub_composition_container):
    from packages.core.ai.tools.video_edit_tools import _review_findings

    name, _project = sub_composition_container
    reviewed = subprocess.run(
        ["docker", "exec", name, "python3", RUNTIME, "review", "--snapshot-times", "1,3"],
        capture_output=True, text=True, timeout=600,
    )
    review = json.loads(reviewed.stdout.strip().splitlines()[-1])
    assert review["status"] == "ok", review
    assert review["review_passed"] is True, _review_findings(review["check_report"])
    rendered = subprocess.run(
        ["docker", "exec", name, "python3", RUNTIME, "render", "--quality", "standard",
         "--workers", "1", "--reviewed", "--output", "renders/scenes.mp4"],
        capture_output=True, text=True, timeout=600,
    )
    render = json.loads(rendered.stdout.strip().splitlines()[-1])
    assert render["status"] == "ok", render
    logs = rendered.stdout + rendered.stderr + json.dumps(render)
    assert "timelines not registered" not in logs and "sub_timeline" not in logs, render
