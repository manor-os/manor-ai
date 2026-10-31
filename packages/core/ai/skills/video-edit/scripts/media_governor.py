#!/usr/bin/env python3
"""Admit HyperFrames' FFmpeg processes into the render sandbox's budget.

HyperFrames starts one FFmpeg per video layer at once, and each FFmpeg sizes
its decoder, filter and encoder thread pools from the CPUs it may run on, not
from the sandbox's CPU quota. On a 14-CPU host one frame extraction peaks at
86 threads, so 14 layers ask for ~1,200 tasks inside a 512-PID sandbox.

``video_runtime.py`` points ``HYPERFRAMES_FFMPEG_PATH`` at a launcher for this
file and passes the plan it derived from the sandbox's limits:

* ``MANOR_MEDIA_SLOTS`` -- FFmpeg processes that may run at once. The rest
  wait here for a slot (an exclusive ``flock`` that the exec'd FFmpeg keeps
  holding until it exits, however it exits).
* ``MANOR_MEDIA_CPUS`` -- CPUs each FFmpeg may run on, which is what its
  automatic thread counts are derived from.

A process reading frames from a pipe (the streaming encoder, or the second
half of a two-process pipeline) is fed by a process that may hold a slot, so it
never waits for one; its threads are budgeted with the slot of its producer.
The governor only ever narrows what FFmpeg would do anyway: on any problem it
executes FFmpeg unchanged rather than fail the render on its own account.
"""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
import time

PIPE_INPUTS = {"-", "pipe:", "pipe:0", "fd:", "fd:0", "/dev/stdin"}
POLL_SECONDS = 0.05


def _int_env(name: str) -> int:
    try:
        return max(0, int(os.environ.get(name, "") or 0))
    except ValueError:
        return 0


def reads_pipe(argv: list[str]) -> bool:
    return any(
        argv[index] == "-i" and argv[index + 1] in PIPE_INPUTS
        for index in range(len(argv) - 1)
    )


def acquire_slot(slot_dir: str, slots: int) -> tuple[int, int]:
    """Block until one of ``slots`` lock files is free; return (fd, index)."""
    os.makedirs(slot_dir, exist_ok=True)
    while True:
        for index in range(slots):
            fd = os.open(os.path.join(slot_dir, f"slot-{index}"), os.O_RDWR | os.O_CREAT, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                os.close(fd)
                continue
            # The lock lives as long as this open file: FFmpeg inherits it
            # across exec and the kernel drops it when FFmpeg exits or dies.
            os.set_inheritable(fd, True)
            return fd, index
        time.sleep(POLL_SECONDS)


def cpu_window(allowed: list[int], cpus: int, index: int, base: int) -> set[int]:
    """``cpus`` CPUs of ``allowed``, rotated so concurrent slots spread out."""
    if not allowed or cpus <= 0:
        return set(allowed)
    width = min(cpus, len(allowed))
    start = base + index * width
    return {allowed[(start + offset) % len(allowed)] for offset in range(width)}


def pin(cpus: int, index: int) -> None:
    if cpus <= 0 or not hasattr(os, "sched_setaffinity"):
        return
    allowed = sorted(os.sched_getaffinity(0))
    os.sched_setaffinity(0, cpu_window(allowed, cpus, index, _int_env("MANOR_MEDIA_CPU_BASE")))


def _last_frame_seconds(src: str, ffprobe: str) -> float | None:
    """Timestamp of the first video stream's last frame, from its duration and rate."""
    probed = subprocess.run(
        [ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=duration,avg_frame_rate:format=duration", "-of", "json", src],
        capture_output=True, text=True, timeout=10,
    )
    data = json.loads(probed.stdout or "{}")
    stream = (data.get("streams") or [{}])[0]
    duration = float(stream.get("duration") or (data.get("format") or {}).get("duration") or 0)
    if duration <= 0:
        return None
    num, _, den = str(stream.get("avg_frame_rate") or "0/1").partition("/")
    fps = float(num or 0) / float(den or 1) if float(den or 1) else 0.0
    return max(0.0, duration - (1.0 / fps if fps > 0 else 0.04))


def hold_last_frame(argv: list[str], ffprobe: str | None) -> list[str]:
    """Clamp a single-frame extraction that seeks past a video's last frame.

    Playback and render hold a clip's last frame once its footage ends, but
    a frame extraction seeking past it (``-ss T -i clip -frames:v 1``, how
    HyperFrames' snapshot copies <video> frames into review frames) gets no
    frame, so the review frame shows the clip black. A slot filled exactly
    to a file's container duration ends a frame or two after its last video
    frame, so this happened on ordinary compositions. Seek to the last frame
    instead, which is what the viewer sees.
    """
    try:
        frames = argv.index("-frames:v")
        first_input = argv.index("-i")
        seek = max(i for i in range(first_input) if argv[i] == "-ss")
    except ValueError:
        return argv
    src = argv[first_input + 1] if first_input + 1 < len(argv) else ""
    if (
        not ffprobe or argv[frames + 1:frames + 2] != ["1"] or src in PIPE_INPUTS
        or "://" in src or not os.path.isfile(src)
    ):
        return argv
    try:
        wanted = float(argv[seek + 1])
        last = _last_frame_seconds(src, ffprobe)
    except (OSError, ValueError, subprocess.SubprocessError):
        return argv
    if last is None or wanted <= last:
        return argv
    return [*argv[:seek + 1], f"{last:.3f}", *argv[seek + 2:]]


def main(argv: list[str]) -> None:
    real = os.environ.get("MANOR_MEDIA_FFMPEG") or "/usr/bin/ffmpeg"
    argv = hold_last_frame(argv, os.environ.get("MANOR_MEDIA_FFPROBE") or None)
    slots = _int_env("MANOR_MEDIA_SLOTS")
    slot_dir = os.environ.get("MANOR_MEDIA_SLOT_DIR", "")
    try:
        index = slots
        if slots and slot_dir and not reads_pipe(argv):
            _fd, index = acquire_slot(slot_dir, slots)
        pin(_int_env("MANOR_MEDIA_CPUS"), index)
    except OSError as exc:
        print(f"[manor-media-governor] running ffmpeg ungoverned: {exc}", file=sys.stderr)
    os.execv(real, [real, *argv])


if __name__ == "__main__":
    main(sys.argv[1:])
