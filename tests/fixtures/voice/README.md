`zh-call.wav` is a synthetic Mandarin test utterance generated with the macOS
Eddy (Chinese, China mainland) voice. It contains no microphone recording or
user data. PCM16, mono, 24kHz.

Text: 你好，请用中文回答。今天我们测试实时语音通话。

Used to exercise speech detection with a real speech spectrum and natural pauses,
rather than treating a test tone as spoken language.

`zh-reply.mp3` is a synthetic reply generated through the configured speech
gateway and the shared Chat speech handler. Text:
你好，我能听到你的声音。现在测试中文语音回复。

It verifies the real provider's compressed output in addition to PCM/WAV playback.

To test the real browser capture/WebSocket/VAD/playback chain with synthetic
providers and isolated Workspace messages, start this fixture from the repo root:

```sh
PYTHONPATH=. .venv/bin/uvicorn tests.voice_browser_server:app --host 127.0.0.1 --port 3199
```

Then, from `apps/web`:

```sh
E2E_PORT=3197 E2E_API=http://127.0.0.1:3199 VOICE_GATEWAY_E2E=1 npx playwright test e2e/chat-voice.spec.ts -g 'real browser PCM'
```

Restart the fixture between runs. It never accesses user records, microphones,
credentials, or paid providers. `chat-voice-audio.html` also provides a synthetic
capture/pause/resume check for browsers outside Playwright.
