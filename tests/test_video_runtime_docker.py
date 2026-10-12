"""Offline Linux regression; opt in with MANOR_VIDEO_DOCKER_TEST=1.

Uses an already-built sandbox-skill:latest image and only an owned disposable
container. No running Manor sandbox, network, or user media is involved.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
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
RUNTIME = ROOT / "packages/core/ai/skills/video-edit/scripts/video_runtime.py"


def _docker(*args: str, timeout: int = 30) -> str:
    result = subprocess.run(
        ["docker", *args], capture_output=True, text=True, timeout=timeout,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout.strip()


@pytest.fixture(scope="module")
def video_container(tmp_path_factory):
    project = tmp_path_factory.mktemp("video-runtime-docker")
    (project / "assets").mkdir()
    (project / "video.json").write_text(json.dumps({
        "paths": {"blocks": "compositions", "components": "compositions/components", "assets": "assets"},
        "media": {"autoProxy": True}, "authoringSkill": "video-edit",
    }))
    (project / "package.json").write_text(json.dumps({
        "name": "video-runtime-regression", "private": True, "type": "module",
    }))
    (project / "index.html").write_text(textwrap.dedent("""\
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
    _docker("image", "inspect", "sandbox-skill:latest")
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
        "--mount", f"type=bind,src={RUNTIME},dst=/opt/video_runtime.py,readonly",
        "--env", "DO_NOT_TRACK=1", "sandbox-skill:latest", "sleep", "infinity",
    )
    try:
        _docker("start", name)
        _docker(
            "exec", name, "ffmpeg", "-v", "error", "-f", "lavfi", "-i",
            "testsrc2=size=640x360:rate=30:duration=4", "-c:v", "libx264",
            "-threads", "2", "-pix_fmt", "yuv420p", "assets/source.mp4",
        )
        yield name, project
    finally:
        # The UUID name is created above, never discovered from live containers.
        _docker("rm", "-f", name)


def test_linux_timeout_reaps_detached_children(video_container):
    name, _project = video_container
    script = textwrap.dedent("""\
        import importlib.util, json, signal, subprocess, sys, time
        from pathlib import Path
        spec = importlib.util.spec_from_file_location('runtime', '/opt/video_runtime.py')
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
    script = textwrap.dedent("""\
        import json, subprocess, threading
        from pathlib import Path
        cgroup = Path('/sys/fs/cgroup')
        before = (cgroup/'pids.events').read_text()
        samples = []
        done = threading.Event()
        def monitor():
            while not done.is_set():
                samples.append(int((cgroup/'pids.current').read_text()))
                done.wait(0.05)
        watcher = threading.Thread(target=monitor)
        watcher.start()
        try:
            result = subprocess.run(
                ['python3', '/opt/video_runtime.py', 'render', '--quality', 'high',
                 '--workers', '4', '--output', 'renders/regression.mp4'],
                capture_output=True, text=True, timeout=150,
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
        """)
    report = json.loads(_docker("exec", name, "python3", "-c", script, timeout=180))
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
