"""Manor's own royalty-free music library.

Instrumental beds generated once by Manor (see ``catalog.json``) and shipped
with the product, so a video always has music even when live music generation
is unavailable for an account. The tracks are Manor-generated and owned, so
they carry no third-party license or attribution.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

LIBRARY_ROOT = Path(__file__).resolve().parents[1] / "assets" / "music_library"
LIBRARY_MODEL = "manor/music-library"

# Words a prompt may use for each catalog mood, in English and Chinese.
# Chinese has no spaces, so its words are matched as substrings.
MOOD_WORDS: dict[str, tuple[str, ...]] = {
    "calm": ("calm", "chill", "relaxed", "soft", "gentle", "peaceful", "平静", "舒缓", "放松", "轻柔"),
    "upbeat": ("upbeat", "happy", "bright", "cheerful", "playful", "fun", "轻快", "欢快", "愉快", "活泼"),
    "cinematic": ("cinematic", "epic", "dramatic", "film", "trailer", "电影", "史诗", "大气"),
    "tech": ("tech", "technology", "digital", "electronic", "futuristic", "saas", "科技", "电子", "未来"),
    "warm": ("warm", "cozy", "heartfelt", "acoustic", "friendly", "温暖", "温馨", "治愈"),
    "inspiring": ("inspiring", "uplifting", "hopeful", "motivational", "励志", "鼓舞", "希望"),
    "minimal": ("minimal", "minimalist", "ambient", "clean", "subtle", "simple", "极简", "简约", "氛围"),
    "energetic": ("energetic", "powerful", "driving", "dynamic", "hype", "sport", "动感", "激昂", "有力", "活力"),
}


def prompt_moods(prompt: str) -> set[str]:
    """Catalog moods a prompt asks for, from English words or Chinese phrases."""
    text = str(prompt or "").lower()
    words = set(re.findall(r"[a-z]+", text))
    return {
        mood
        for mood, synonyms in MOOD_WORDS.items()
        if any(word in words if word.isascii() else word in text for word in synonyms)
    }


@dataclass(frozen=True)
class LibraryTrack:
    id: str
    path: Path
    moods: tuple[str, ...]
    duration_seconds: float
    loopable: bool
    license: str


def library_tracks(root: Path | None = None) -> list[LibraryTrack]:
    """Every track in the catalog whose audio file is present."""
    base = Path(root or LIBRARY_ROOT)
    try:
        catalog = json.loads((base / "catalog.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    default_license = str(catalog.get("license") or "")
    tracks = []
    for entry in catalog.get("tracks") or []:
        path = base / str(entry.get("file") or "")
        if not entry.get("id") or not path.is_file():
            continue
        tracks.append(LibraryTrack(
            id=str(entry["id"]),
            path=path,
            moods=tuple(str(mood).lower() for mood in entry.get("moods") or []),
            duration_seconds=float(entry.get("duration_seconds") or 0),
            loopable=bool(entry.get("loopable")),
            license=str(entry.get("license") or default_license),
        ))
    return tracks


def pick_library_track(
    prompt: str,
    duration_seconds: float | None = None,
    root: Path | None = None,
) -> LibraryTrack | None:
    """The track whose moods best match the prompt.

    Ties go to a track long enough for the video (or loopable), then to the
    catalog order, so the same request always gets the same track.
    """
    tracks = library_tracks(root)
    if not tracks:
        return None
    moods = prompt_moods(prompt)
    wanted = float(duration_seconds or 0)

    def score(item: tuple[int, LibraryTrack]) -> tuple[int, int, int]:
        index, track = item
        matches = sum(1 for mood in track.moods if mood in moods)
        fits = int(track.loopable or track.duration_seconds >= wanted)
        return (matches, fits, -index)

    return max(enumerate(tracks), key=score)[1]
