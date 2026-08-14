export enum GenerateFileKind {
  Search = "search",
  Diagram = "diagram",
  Code = "code",
  Document = "document",
  WordDocument = "word_document",
  Pdf = "pdf",
  Presentation = "presentation",
  Spreadsheet = "spreadsheet",
  Image = "image",
  Video = "video",
  Audio = "audio",
}

export enum AudioGenerationPurpose {
  Speech = "speech",
  Dialogue = "dialogue",
  Narration = "narration",
  Music = "music",
  Ambience = "ambience",
  Soundscape = "soundscape",
  Sfx = "sfx",
  Transition = "transition",
}

export enum AudioGenerationFormat {
  Mp3 = "mp3",
  Wav = "wav",
  Flac = "flac",
  Opus = "opus",
  Pcm = "pcm",
  Pcm16 = "pcm16",
}

export enum AudioGenerationStatus {
  Completed = "completed",
  Error = "error",
}

export enum AudioGenerationErrorCode {
  InvalidRequest = "invalid_request",
  UnsupportedNonvoiceAudioModel = "unsupported_nonvoice_audio_model",
  ProviderKeyRequired = "provider_key_required",
  NativeMusicKeyRequired = "native_music_key_required",
  AudioProviderUnavailable = "audio_provider_unavailable",
  ProviderBlocker = "provider_blocker",
  AudioGenerationFailed = "audio_generation_failed",
}

export enum AudioGenerationProvider {
  OpenRouter = "openrouter",
  Google = "google",
  OpenAI = "openai",
  Zyphra = "zyphra",
  Unknown = "unknown",
}

export enum AudioGenerationRole {
  Voice = "voice",
  Audio = "audio",
  Sfx = "sfx",
}

export interface AudioGenerationCompletedResult {
  kind: GenerateFileKind.Audio;
  status: AudioGenerationStatus.Completed;
  provider: AudioGenerationProvider;
  result_url: string;
  audio_url: string;
  fs_path: string | null;
  prompt: string;
  purpose: AudioGenerationPurpose;
  model: string;
  voice: string | null;
  voice_instructions: string | null;
  format: AudioGenerationFormat;
  provider_response_format: AudioGenerationFormat;
  duration_seconds: number | null;
  requested_duration_seconds: number | null;
  file_size: number;
}

export interface AudioGenerationErrorResult {
  kind: GenerateFileKind.Audio;
  status: AudioGenerationStatus.Error;
  code: AudioGenerationErrorCode;
  error: string;
  purpose: AudioGenerationPurpose;
  provider: AudioGenerationProvider;
  retryable: boolean;
  audio_generated: false;
  model?: string;
  provider_status?: number | null;
  attempts?: number;
  format_related?: boolean;
  retry_advice?: string;
  role?: AudioGenerationRole;
}

export type AudioGenerationResult =
  | AudioGenerationCompletedResult
  | AudioGenerationErrorResult;

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function enumContains<T extends Record<string, string>>(
  enumObject: T,
  value: unknown,
): value is T[keyof T] {
  return typeof value === "string" && Object.values(enumObject).includes(value);
}

function isNullableString(value: unknown): value is string | null {
  return value === null || typeof value === "string";
}

function isNullableNumber(value: unknown): value is number | null {
  return value === null || typeof value === "number";
}

export function parseAudioGenerationResult(
  value: unknown,
): AudioGenerationResult | null {
  if (!isRecord(value) || value.kind !== GenerateFileKind.Audio) return null;
  if (!enumContains(AudioGenerationStatus, value.status)) return null;
  if (!enumContains(AudioGenerationPurpose, value.purpose)) return null;
  if (!enumContains(AudioGenerationProvider, value.provider)) return null;

  if (value.status === AudioGenerationStatus.Completed) {
    if (
      typeof value.result_url !== "string" ||
      typeof value.audio_url !== "string" ||
      !isNullableString(value.fs_path) ||
      typeof value.prompt !== "string" ||
      typeof value.model !== "string" ||
      !isNullableString(value.voice) ||
      !isNullableString(value.voice_instructions) ||
      !enumContains(AudioGenerationFormat, value.format) ||
      !enumContains(AudioGenerationFormat, value.provider_response_format) ||
      !isNullableNumber(value.duration_seconds) ||
      !isNullableNumber(value.requested_duration_seconds) ||
      typeof value.file_size !== "number"
    ) {
      return null;
    }
  } else if (
    !enumContains(AudioGenerationErrorCode, value.code) ||
    typeof value.error !== "string" ||
    typeof value.retryable !== "boolean" ||
    value.audio_generated !== false ||
    (value.model !== undefined && typeof value.model !== "string") ||
    (value.provider_status !== undefined && !isNullableNumber(value.provider_status)) ||
    (value.attempts !== undefined && typeof value.attempts !== "number") ||
    (value.format_related !== undefined && typeof value.format_related !== "boolean") ||
    (value.retry_advice !== undefined && typeof value.retry_advice !== "string") ||
    (value.role !== undefined && !enumContains(AudioGenerationRole, value.role))
  ) {
    return null;
  }

  return value as unknown as AudioGenerationResult;
}
