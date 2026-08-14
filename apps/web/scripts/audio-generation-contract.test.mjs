#!/usr/bin/env node
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";
import { transform } from "esbuild";

const contractSource = await readFile(
  new URL("../src/lib/audioGeneration.ts", import.meta.url),
  "utf8",
);
const toolCallListSource = await readFile(
  new URL("../src/components/ui/ToolCallList.tsx", import.meta.url),
  "utf8",
);
const compiledContract = await transform(contractSource, {
  format: "esm",
  loader: "ts",
  target: "es2022",
});
const contract = await import(
  `data:text/javascript;base64,${Buffer.from(compiledContract.code).toString("base64")}`
);

test("audio generation exposes enum-backed request and result contracts", () => {
  for (const enumName of [
    "GenerateFileKind",
    "AudioGenerationPurpose",
    "AudioGenerationFormat",
    "AudioGenerationStatus",
    "AudioGenerationErrorCode",
    "AudioGenerationProvider",
    "AudioGenerationRole",
  ]) {
    assert.match(contractSource, new RegExp(`export enum ${enumName}\\b`));
  }
  assert.match(contractSource, /export type AudioGenerationResult\s*=/);
  assert.match(contractSource, /parseAudioGenerationResult/);
});

test("chat media preview validates the typed audio result before consuming it", () => {
  assert.match(toolCallListSource, /parseAudioGenerationResult\(parsed\)/);
  assert.match(toolCallListSource, /AudioGenerationStatus\.Completed/);
});

test("audio result guard accepts enum-backed completed and error payloads", () => {
  const completed = {
    kind: contract.GenerateFileKind.Audio,
    status: contract.AudioGenerationStatus.Completed,
    provider: contract.AudioGenerationProvider.OpenRouter,
    result_url: "/api/v1/fs/entity/audio/voice.mp3",
    audio_url: "/api/v1/fs/entity/audio/voice.mp3",
    fs_path: "audio/voice.mp3",
    prompt: "Hello",
    purpose: contract.AudioGenerationPurpose.Narration,
    model: "google/gemini-3.1-flash-tts-preview",
    voice: "Zephyr",
    voice_instructions: null,
    format: contract.AudioGenerationFormat.Mp3,
    provider_response_format: contract.AudioGenerationFormat.Pcm,
    duration_seconds: 1.2,
    requested_duration_seconds: null,
    file_size: 1024,
  };
  const error = {
    kind: contract.GenerateFileKind.Audio,
    status: contract.AudioGenerationStatus.Error,
    code: contract.AudioGenerationErrorCode.AudioProviderUnavailable,
    error: "provider unavailable",
    purpose: contract.AudioGenerationPurpose.Narration,
    provider: contract.AudioGenerationProvider.OpenRouter,
    retryable: true,
    audio_generated: false,
    provider_status: 500,
  };

  assert.deepEqual(contract.parseAudioGenerationResult(completed), completed);
  assert.deepEqual(contract.parseAudioGenerationResult(error), error);
});

test("audio result guard rejects string drift and incomplete payloads", () => {
  assert.equal(
    contract.parseAudioGenerationResult({
      kind: "audio",
      status: "finished",
      provider: "openrouter",
      purpose: "narration",
    }),
    null,
  );
  assert.equal(
    contract.parseAudioGenerationResult({
      kind: "audio",
      status: "error",
      code: "made_up_error",
      error: "bad",
      purpose: "narration",
      provider: "openrouter",
      retryable: false,
      audio_generated: false,
    }),
    null,
  );
});
