type MediaDurationTarget = {
  readonly duration: number;
  currentTime: number;
  readonly seekable: Pick<TimeRanges, "length" | "end">;
};

function positiveFinite(value: number): number {
  return Number.isFinite(value) && value > 0 ? value : 0;
}

export function getPlayableMediaDuration(media: MediaDurationTarget): number {
  const metadataDuration = positiveFinite(media.duration);
  if (metadataDuration > 0) return metadataDuration;

  try {
    if (media.seekable.length > 0) {
      return positiveFinite(media.seekable.end(media.seekable.length - 1));
    }
  } catch {
    // Seekable ranges can change while metadata events are being handled.
  }

  return 0;
}

export function requestMediaDurationProbe(media: MediaDurationTarget): boolean {
  if (getPlayableMediaDuration(media) > 0) return false;

  try {
    media.currentTime = Number.MAX_SAFE_INTEGER;
    return true;
  } catch {
    return false;
  }
}
