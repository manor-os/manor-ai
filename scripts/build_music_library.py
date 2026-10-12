"""Build Manor's royalty-free music library from its spec.

Usage:
    GEMINI_API_KEY=... python scripts/build_music_library.py [--only ID ...] [--force]

Reads packages/core/assets/music_library/spec.json, generates each missing
track with Lyria, makes it loop seamlessly, normalizes its loudness, and
records it in catalog.json. Tracks already in the catalog are kept unless
--force is given, so an interrupted run resumes where it stopped.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.core.services.music_library import LIBRARY_ROOT  # noqa: E402

DEFAULT_MODEL = "google/lyria-3-pro-preview"
Generator = Callable[[str, float], Awaitable[bytes]]


def _probe_seconds(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    return float(out or 0)


def finish_track(raw: bytes, out_path: Path, *, seconds: float, crossfade: float, lufs: float) -> dict:
    """Cut, loop, level, and encode one generated track.

    When the source runs past the target by at least the crossfade, the
    overrun is faded into the opening, so the end flows straight back into the
    start when the track loops. A shorter source is trimmed with a fade out and
    is not marked loopable.
    """
    with tempfile.TemporaryDirectory() as tmp:
        source = Path(tmp) / "source.audio"
        source.write_bytes(raw)
        length = _probe_seconds(source)
        loopable = length >= seconds + crossfade
        level = f"loudnorm=I={lufs}:TP=-1.5:LRA=11"
        if loopable:
            graph = (
                f"[0:a]atrim=0:{crossfade},asetpts=PTS-STARTPTS,afade=t=in:st=0:d={crossfade}:curve=qsin[head];"
                f"[0:a]atrim={seconds}:{seconds + crossfade},asetpts=PTS-STARTPTS,"
                f"afade=t=out:st=0:d={crossfade}:curve=qsin[tail];"
                "[head][tail]amix=inputs=2:normalize=0[joined];"
                f"[0:a]atrim={crossfade}:{seconds},asetpts=PTS-STARTPTS,"
                f"adelay={crossfade * 1000:.0f}:all=1[rest];"
                f"[joined][rest]amix=inputs=2:duration=longest:normalize=0,{level}[out]"
            )
        else:
            kept = min(length, seconds)
            graph = (
                f"[0:a]atrim=0:{kept},asetpts=PTS-STARTPTS,"
                f"afade=t=out:st={max(0.0, kept - 1.0)}:d=1,{level}[out]"
            )
        out_path.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-i", str(source), "-filter_complex", graph,
             "-map", "[out]", "-ar", "44100", "-ac", "2", "-c:a", "libmp3lame", "-b:a", "192k",
             str(out_path)],
            check=True,
        )
    return {"duration_seconds": round(_probe_seconds(out_path), 2), "loopable": loopable}


async def build(
    generate: Generator,
    *,
    root: Path = LIBRARY_ROOT,
    model: str = DEFAULT_MODEL,
    only: set[str] | None = None,
    force: bool = False,
) -> list[str]:
    """Generate every missing spec track into ``root`` and return the built ids."""
    spec = json.loads((root / "spec.json").read_text(encoding="utf-8"))
    catalog_path = root / "catalog.json"
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    entries = {entry["id"]: entry for entry in catalog.get("tracks") or []}
    seconds = float(spec["duration_seconds"])
    crossfade = float(spec["crossfade_seconds"])
    built = []
    for track in spec["tracks"]:
        track_id = track["id"]
        if only and track_id not in only:
            continue
        existing = entries.get(track_id)
        if existing and (root / existing["file"]).is_file() and not force:
            continue
        # Ask for extra length so the loop has an overrun to crossfade.
        raw = await generate(track["prompt"], seconds + crossfade + 5)
        file_name = f"{track_id}.mp3"
        finished = finish_track(
            raw, root / file_name, seconds=seconds, crossfade=crossfade, lufs=float(spec["loudness_lufs"]),
        )
        entries[track_id] = {
            "id": track_id,
            "file": file_name,
            "moods": list(track["moods"]),
            **finished,
            "model": model,
            "prompt": track["prompt"],
            "sha256": hashlib.sha256((root / file_name).read_bytes()).hexdigest(),
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        # Spec order, written after every track so a stopped run keeps its work.
        order = [item["id"] for item in spec["tracks"]]
        catalog["tracks"] = sorted(entries.values(), key=lambda entry: order.index(entry["id"]) if entry["id"] in order else len(order))
        catalog_path.write_text(json.dumps(catalog, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        built.append(track_id)
        print(f"built {track_id}: {finished['duration_seconds']}s loopable={finished['loopable']}", flush=True)
    return built


def lyria_generator(model: str) -> Generator:
    """Generate with the same Lyria call the product uses."""
    from packages.core.ai.tools.extended_tools import _audio_prompt_for_purpose, _google_music_bytes
    from packages.core.contracts.audio_generation import AudioGenerationPurpose

    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or ""
    if not api_key:
        raise SystemExit("Set GEMINI_API_KEY (or GOOGLE_API_KEY) to generate the library with Lyria.")

    async def generate(prompt: str, seconds: float) -> bytes:
        audio, _format = await _google_music_bytes(
            api_key=api_key,
            model=model,
            prompt=_audio_prompt_for_purpose(prompt, AudioGenerationPurpose.MUSIC, seconds),
            audio_format="mp3",
        )
        return audio

    return generate


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--only", nargs="*", help="Build only these track ids.")
    parser.add_argument("--force", action="store_true", help="Rebuild tracks already in the catalog.")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    args = parser.parse_args()
    built = asyncio.run(build(
        lyria_generator(args.model), model=args.model, only=set(args.only or []) or None, force=args.force,
    ))
    print(f"done: {len(built)} track(s) built")


if __name__ == "__main__":
    main()
