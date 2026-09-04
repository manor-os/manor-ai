import { test, expect, type Page } from "@playwright/test";
import { readFileSync } from "node:fs";

function wavClip(seconds = 1) {
  const bytes = Buffer.alloc(44 + 24000 * 2 * seconds);
  bytes.write("RIFF", 0); bytes.writeUInt32LE(bytes.length - 8, 4); bytes.write("WAVEfmt ", 8);
  bytes.writeUInt32LE(16, 16); bytes.writeUInt16LE(1, 20); bytes.writeUInt16LE(1, 22);
  bytes.writeUInt32LE(24000, 24); bytes.writeUInt32LE(48000, 28);
  bytes.writeUInt16LE(2, 32); bytes.writeUInt16LE(16, 34);
  bytes.write("data", 36); bytes.writeUInt32LE(bytes.length - 44, 40);
  for (let sample = 0; sample < (bytes.length - 44) / 2; sample++) {
    bytes.writeInt16LE(Math.round(4000 * Math.sin(sample * Math.PI * 2 * 440 / 24000)), 44 + sample * 2);
  }
  return bytes.toString("base64");
}

for (const surface of ["chat", "workspace", "public"]) {
  test(`${surface}: empty speech resumes paused audio and keeps completed replies inside the call`, async ({ page }) => {
    let wire: any;
    const received: any[] = [];
    await page.routeWebSocket(/\/api\/v1\/audio\/live$/, socket => {
      wire = socket;
      socket.onMessage(data => {
        const event = JSON.parse(String(data)); received.push(event);
        if (event.type === "start") socket.send(JSON.stringify({ type: "ready", generation: 1,
          conversation_id: surface === "public" ? "public-conversation" : "conversation-one" }));
      });
    });
    await setup(page, surface, true);
    await page.evaluate(() => {
      const start = AudioBufferSourceNode.prototype.start;
      (window as any).playedOffsets = [];
      AudioBufferSourceNode.prototype.start = function (...args) {
        (window as any).playedOffsets.push(args[1] || 0);
        return start.apply(this, args);
      };
    });
    await page.getByRole("button", { name: "Start voice call", exact: true }).click();
    const dialog = page.getByRole("dialog", { name: "Voice call" });
    await expect(dialog.getByRole("status")).toHaveText("Listening");
    const send = (event: any) => wire.send(JSON.stringify({ generation: 1, ...event }));
    send({ type: "transcript", role: "user", text: "你好" });
    send({ type: "turn", conversation_id: surface === "public" ? "public-conversation" : "conversation-one", text: "你好，我在。" });
    send({ type: "audio_clip", item_id: "pausable", audio: wavClip(3) });
    await expect(dialog.getByRole("status")).toHaveText("AI is speaking");
    // Let some real playback time pass, then pause for longer than the
    // original clip. Pausing must not acknowledge or lose its remainder.
    await expect.poll(async () => page.evaluate(() => (window as any).playedOffsets.length)).toBe(1);
    await page.waitForTimeout(250);
    send({ type: "input_started" });
    send({ type: "transcribing" });
    await expect(dialog.getByRole("status")).toHaveText("Recognizing speech…");
    await expect(dialog.getByRole("log")).toContainText("你好，我在。");
    await page.waitForTimeout(3200);
    expect(received.some(e => e.type === "clip_done")).toBeFalsy();
    send({ type: "input_empty" });
    await expect(dialog.getByRole("status")).toHaveText("AI is speaking");
    const offsets = await page.evaluate(() => (window as any).playedOffsets);
    expect(offsets).toHaveLength(2);
    expect(offsets[1]).toBeGreaterThan(0);
    expect(offsets[1]).toBeLessThan(2);
    await expect.poll(() => received.filter(e => e.type === "clip_done").length).toBe(1);
    send({ type: "interrupt", generation: 2 });
    send({ type: "transcript", generation: 2, role: "user", text: "继续" });
    await expect(dialog.getByRole("log")).toContainText("你好，我在。");
    await expect(dialog.getByRole("log")).toContainText("继续");
    await dialog.getByRole("button", { name: "End call", exact: true }).click();
  });

  test(`${surface}: call shows actual processing stages and keeps waiting between spoken chunks`, async ({ page }) => {
    let wire: any;
    const received: any[] = [];
    await page.routeWebSocket(/\/api\/v1\/audio\/live$/, socket => {
      wire = socket;
      socket.onMessage(data => {
        const event = JSON.parse(String(data)); received.push(event);
        if (event.type === "start") socket.send(JSON.stringify({ type: "ready", generation: 0,
          conversation_id: surface === "public" ? "public-conversation" : "conversation-one" }));
      });
    });
    await setup(page, surface, true);
    await page.getByRole("button", { name: "Start voice call", exact: true }).click();
    const dialog = page.getByRole("dialog", { name: "Voice call" });
    await expect(dialog.getByRole("status")).toHaveText("Listening");
    const send = (event: any) => wire.send(JSON.stringify({ generation: 0, ...event }));
    send({ type: "transcribing" });
    await expect(dialog.getByRole("status")).toHaveText("Recognizing speech…");
    send({ type: "transcript", role: "user", text: "你好" });
    send({ type: "thinking" });
    await expect(dialog.getByRole("status")).toHaveText("Thinking…");
    send({ type: "turn", conversation_id: surface === "public" ? "public-conversation" : "conversation-one", text: "你好，我在。" });
    send({ type: "synthesizing" });
    await expect(dialog.getByRole("status")).toHaveText("Preparing voice…");
    await expect(dialog.getByRole("log")).toContainText("你好，我在。");
    send({ type: "audio_clip", item_id: "first", audio: wavClip() });
    await expect(dialog.getByRole("status")).toHaveText("AI is speaking");
    send({ type: "synthesizing" });
    await expect(dialog.getByRole("status")).toHaveText("AI is speaking");
    await expect.poll(() => received.some(e => e.type === "clip_done" && e.item_id === "first")).toBeTruthy();
    await expect(dialog.getByRole("status")).toHaveText("Preparing voice…");
    send({ type: "listening" });
    await expect(dialog.getByRole("status")).toHaveText("Listening");
    send({ type: "work", status: "running" });
    await expect(dialog.getByRole("status")).toHaveText("Listening · Working");
    send({ type: "work", status: "completed" });
    await expect(dialog.getByRole("status")).toHaveText("Listening");
    send({ type: "interrupt", generation: 1 });
    send({ type: "turn", generation: 0, text: "Completed reply whose audio was interrupted" });
    send({ type: "synthesizing", generation: 0 });
    await expect(dialog.getByRole("log")).toContainText("Completed reply whose audio was interrupted");
    await expect(dialog.getByRole("log")).toContainText("你好，我在。");
    await expect(dialog.getByRole("status")).toHaveText("Listening");
    await dialog.getByRole("button", { name: "End call", exact: true }).click();
  });

  test(`${surface}: connection budget allows fallback and stops timing out after ready`, async ({ page }) => {
    let wire: any;
    let starts = 0;
    await page.routeWebSocket(/\/api\/v1\/audio\/live$/, socket => {
      wire = socket;
      socket.onMessage(data => { if (JSON.parse(String(data)).type === "start") starts++; });
    });
    await setup(page, surface, true);
    await page.clock.install();
    await page.getByRole("button", { name: "Start voice call", exact: true }).click();
    await expect.poll(() => starts).toBe(1);
    const dialog = page.getByRole("dialog", { name: "Voice call" });
    await page.clock.fastForward(44000);
    await expect(dialog.getByRole("status")).toHaveText("Connecting…");
    wire.send(JSON.stringify({ type: "ready", generation: 0,
      conversation_id: surface === "public" ? "public-conversation" : "conversation-one" }));
    await expect(dialog.getByRole("status")).toHaveText("Listening");
    await page.clock.fastForward(70000);
    await expect(dialog.getByRole("status")).toHaveText("Listening");
    await dialog.getByRole("button", { name: "End call", exact: true }).click();
    await page.getByRole("button", { name: "Start voice call", exact: true }).click();
    await expect.poll(() => starts).toBe(2);
    await page.clock.fastForward(61000);
    await expect(dialog.getByRole("alert")).toHaveText("The connection timed out. Try again.");
    expect(await page.evaluate(() => (window as any).voiceFixture.stoppedTracks)).toBe(2);
  });

  test(`${surface}: gateway call decodes audio clips and acknowledges playback`, async ({ page }) => {
    let wire: any;
    const received: any[] = [];
    await page.routeWebSocket(/\/api\/v1\/audio\/live$/, socket => {
      wire = socket;
      socket.onMessage(data => {
        const event = JSON.parse(String(data)); received.push(event);
        if (event.type === "start") socket.send(JSON.stringify({ type: "ready", duplex_mode: "push_to_interrupt",
          conversation_id: surface === "public" ? "public-conversation" : "conversation-one" }));
      });
    });
    await setup(page, surface, true);
    await page.getByRole("button", { name: "Start voice call", exact: true }).click();
    const dialog = page.getByRole("dialog", { name: "Voice call" });
    await expect(dialog.getByRole("status")).toHaveText("Listening");
    await expect(dialog).toContainText("Turn-based voice");
    await expect(dialog).not.toContainText("This mode starts processing after you finish speaking.");
    wire.send(JSON.stringify({ type: "audio_clip", item_id: "gateway-clip", audio: wavClip() }));
    await expect(dialog.getByRole("status")).toHaveText("AI is speaking");
    await expect.poll(() => received.some(e => e.type === "clip_done" && e.item_id === "gateway-clip")).toBeTruthy();
    await expect(dialog.getByRole("status")).toHaveText("Listening");
    wire.send(JSON.stringify({ type: "audio_clip", item_id: "interrupted-clip", audio: wavClip(3) }));
    await expect(dialog.getByRole("status")).toHaveText("AI is speaking");
    await dialog.getByRole("button", { name: "Interrupt", exact: true }).click();
    await expect(dialog.getByRole("status")).toHaveText("Listening");
    expect(received.some(e => e.type === "stop_reply" && e.item_id === "interrupted-clip")).toBeTruthy();
    await dialog.getByRole("button", { name: "End call", exact: true }).click();
    expect(received.filter(e => e.type === "clip_done")).toHaveLength(1);
  });

  test(`${surface}: late clips and captions from an interrupted turn are discarded`, async ({ page }) => {
    let wire: any;
    const received: any[] = [];
    await page.routeWebSocket(/\/api\/v1\/audio\/live$/, socket => {
      wire = socket;
      socket.onMessage(data => {
        const event = JSON.parse(String(data)); received.push(event);
        if (event.type === "start") socket.send(JSON.stringify({ type: "ready", generation: 0,
          conversation_id: surface === "public" ? "public-conversation" : "conversation-one" }));
      });
    });
    await setup(page, surface, true);
    await page.evaluate(() => {
      (window as any).voiceStarts = 0;
      const start = AudioBufferSourceNode.prototype.start;
      AudioBufferSourceNode.prototype.start = function (...args) {
        (window as any).voiceStarts++;
        return start.apply(this, args);
      };
    });
    await page.getByRole("button", { name: "Start voice call", exact: true }).click();
    const dialog = page.getByRole("dialog", { name: "Voice call" });
    const send = (event: any) => wire.send(JSON.stringify(event));
    await expect(dialog.getByRole("status")).toHaveText("Listening");
    send({ type: "audio_clip", item_id: "interrupted", generation: 0, audio: wavClip(3) });
    await expect(dialog.getByRole("status")).toHaveText("AI is speaking");
    send({ type: "interrupt", generation: 1 });
    send({ type: "caption", item_id: "stale", generation: 0, delta: "Obsolete reply" });
    send({ type: "audio_clip", item_id: "stale", generation: 0, audio: wavClip(3) });
    send({ type: "thinking", generation: 0 });
    send({ type: "ping" });
    await expect.poll(() => received.some(e => e.type === "pong")).toBeTruthy();
    await expect(dialog.getByRole("status")).toHaveText("Listening");
    await expect(dialog.getByRole("log")).not.toContainText("Obsolete reply");
    send({ type: "caption", item_id: "current", generation: 1, delta: "Current reply" });
    send({ type: "audio_clip", item_id: "current", generation: 1, audio: wavClip() });
    await expect(dialog.getByRole("log")).toContainText("Current reply");
    await expect.poll(() => received.some(e => e.type === "clip_done" && e.item_id === "current")).toBeTruthy();
    expect(await page.evaluate(() => (window as any).voiceStarts)).toBe(2);
    expect(received.filter(e => e.type === "clip_done").map(e => e.item_id)).toEqual(["current"]);
    await dialog.getByRole("button", { name: "End call", exact: true }).click();
  });
}

test("restricted voice preference storage does not block or disconnect calls", async ({ page }) => {
  await page.addInitScript(() => {
    const getItem = Storage.prototype.getItem;
    const setItem = Storage.prototype.setItem;
    Storage.prototype.getItem = function (key: string) {
      if (key === "manor.chat.call.voice") throw new DOMException("Storage blocked", "SecurityError");
      return getItem.call(this, key);
    };
    Storage.prototype.setItem = function (key: string, value: string) {
      if (key === "manor.chat.call.voice") throw new DOMException("Storage blocked", "SecurityError");
      return setItem.call(this, key, value);
    };
  });
  const received: any[] = [];
  await page.routeWebSocket(/\/api\/v1\/audio\/live$/, socket => {
    socket.onMessage(data => {
      const event = JSON.parse(String(data));
      received.push(event);
      if (event.type === "start") {
        socket.send(JSON.stringify({ type: "ready", conversation_id: "public-conversation", voice: event.voice }));
      } else if (event.type === "voice") {
        socket.send(JSON.stringify({ type: "voice", voice: event.voice, locked: false }));
      }
    });
  });
  await setup(page, "public", true);
  await page.getByRole("button", { name: "Start voice call", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "Voice call" });
  await expect(dialog.getByRole("status")).toHaveText("Listening");
  await dialog.getByRole("button", { name: "Voice", exact: true }).click();
  await page.getByRole("option", { name: "Deep", exact: true }).click();
  await expect.poll(() => received.some(event => event.type === "voice" && event.voice === "deep")).toBeTruthy();
  await expect(dialog.getByRole("status")).toHaveText("Listening");
  await expect(dialog.getByRole("alert")).toHaveCount(0);
  await dialog.getByRole("button", { name: "End call", exact: true }).click();
});

test("voice selector works by keyboard and Escape only closes the selector", async ({ page }) => {
  const received: any[] = [];
  await page.routeWebSocket(/\/api\/v1\/audio\/live$/, socket => {
    socket.onMessage(data => {
      const event = JSON.parse(String(data));
      received.push(event);
      if (event.type === "start") {
        socket.send(JSON.stringify({ type: "ready", conversation_id: "conversation-one", voice: event.voice }));
      } else if (event.type === "voice") {
        socket.send(JSON.stringify({ type: "voice", voice: event.voice, locked: false }));
      }
    });
  });
  await setup(page, "chat", true);
  await page.getByRole("button", { name: "Start voice call", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "Voice call" });
  const voice = dialog.getByRole("button", { name: "Voice", exact: true });
  await voice.focus();
  await voice.press("Enter");
  await expect(page.getByRole("listbox", { name: "Voice" })).toBeVisible();
  await voice.press("ArrowDown");
  await voice.press("Enter");
  await expect.poll(() => received.some(event => event.type === "voice" && event.voice === "clear")).toBeTruthy();
  await expect(voice).toContainText("Clear");

  await voice.press("Enter");
  await expect(page.getByRole("listbox", { name: "Voice" })).toBeVisible();
  await voice.press("Escape");
  await expect(page.getByRole("listbox", { name: "Voice" })).toHaveCount(0);
  await expect(dialog).toBeVisible();
  await expect(voice).toBeFocused();
  await dialog.getByRole("button", { name: "End call", exact: true }).click();
});

test("real Mandarin reaches microphone capture, selected voice and audible output", async ({ page }) => {
  let wire: any;
  let inputPeak = 0;
  const received: any[] = [];
  await page.routeWebSocket(/\/api\/v1\/audio\/live$/, socket => {
    wire = socket;
    socket.onMessage(data => {
      const event = JSON.parse(String(data)); received.push(event);
      if (event.type === "start") socket.send(JSON.stringify({ type: "ready", generation: 0, conversation_id: "conversation-one" }));
      if (event.type === "audio") {
        const bytes = Buffer.from(event.audio, "base64");
        for (let i = 0; i < bytes.length; i += 2) inputPeak = Math.max(inputPeak, Math.abs(bytes.readInt16LE(i)));
      }
    });
  });
  await setup(page, "chat", true);
  await page.evaluate(() => {
    const connect = AudioNode.prototype.connect;
    const meters: AnalyserNode[] = [];
    (window as any).voiceOutputPeak = 0;
    AudioNode.prototype.connect = function (destination: any, ...args: any[]) {
      if (destination instanceof AudioDestinationNode) {
        const meter = this.context.createAnalyser();
        meter.fftSize = 256;
        connect.call(this, meter);
        meters.push(meter);
      }
      return (connect as any).call(this, destination, ...args);
    } as typeof connect;
    setInterval(() => {
      for (const meter of meters) {
        const values = new Float32Array(meter.fftSize);
        meter.getFloatTimeDomainData(values);
        (window as any).voiceOutputPeak = Math.max((window as any).voiceOutputPeak, ...values.map(Math.abs));
      }
    }, 20);
  });
  await page.getByRole("button", { name: "Start voice call", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "Voice call" });
  await expect(dialog.getByRole("status")).toHaveText("Listening");
  expect(received.find(event => event.type === "start")?.voice).toBe("warm");
  await dialog.getByRole("button", { name: "Voice", exact: true }).click();
  await page.getByRole("option", { name: "Deep", exact: true }).click();
  await expect.poll(() => received.some(event => event.type === "voice" && event.voice === "deep")).toBeTruthy();
  expect(await page.evaluate(() => (window as any).voiceFixture.constraints.audio)).toMatchObject({
    echoCancellation: true,
    noiseSuppression: true,
    autoGainControl: false,
    voiceIsolation: true,
  });
  const speech = readFileSync(new URL("../../../tests/fixtures/voice/zh-call.wav", import.meta.url)).toString("base64");
  await page.evaluate(encoded => (window as any).voiceFixture.startInput(encoded), speech);
  await expect.poll(() => inputPeak).toBeGreaterThan(1000);
  await expect.poll(async () => Number(await dialog.getByRole("meter", { name: "Microphone level" }).getAttribute("value"))).toBeGreaterThan(0.02);
  const reply = readFileSync(new URL("../../../tests/fixtures/voice/zh-reply.mp3", import.meta.url)).toString("base64");
  wire.send(JSON.stringify({ type: "audio_clip", generation: 0, item_id: "real-speech", audio: reply }));
  await expect(dialog.getByRole("status")).toHaveText("AI is speaking");
  await expect.poll(() => page.evaluate(() => (window as any).voiceOutputPeak)).toBeGreaterThan(0.02);
  wire.send(JSON.stringify({ type: "interrupt", generation: 1 }));
  await expect(dialog.getByRole("status")).toHaveText("Listening");
  await expect(dialog.getByRole("button", { name: "Test speaker", exact: true })).toHaveCount(0);
  expect(received.filter(event => event.type === "start")).toHaveLength(1);
  await dialog.getByRole("button", { name: "Mute", exact: true }).click();
  await expect(dialog.getByRole("meter")).toHaveAttribute("value", "0");
  await dialog.getByRole("button", { name: "End call", exact: true }).click();
});

test("real WorkspaceChat keeps the call and audible reply across persisted message refetches", async ({ page }) => {
  await setup(page, "workspace", true);
  let wire: any;
  let refetches = 0;
  const received: any[] = [];
  const messages = [{ id: "welcome", conversation_id: "fixture-conversation", body: "Workspace ready.",
    message_kind: "text", author_kind: "agent", created_at: "2026-08-31T12:00:00Z", refs: [], meta: {} }];
  await page.route("**/api/v1/**", route => {
    const path = new URL(route.request().url()).pathname;
    let json: unknown = [];
    if (path.endsWith("/chat/messages/page")) {
      refetches++;
      json = { items: messages, has_more: false, next_cursor: null, open_actions_complete: true, open_action_count: 0 };
    } else if (path.endsWith("/stats/quick-view")) json = { ordered_stat_ids: [], hidden_stat_ids: [], configured: false };
    else if (path.endsWith("/connection-status")) json = { requirements: [], required_issue_count: 0 };
    else if (/\/(tasks|goals|stats)$/.test(path)) json = { items: [], total: 0 };
    return route.fulfill({ json });
  });
  await page.routeWebSocket(/\/api\/v1\/audio\/live$/, socket => {
    wire = socket;
    socket.onMessage(data => {
      const event = JSON.parse(String(data)); received.push(event);
      if (event.type === "start") socket.send(JSON.stringify({ type: "ready", generation: 1, conversation_id: "fixture-conversation" }));
    });
  });
  // This fixture mounts the complete WorkspaceChat with its real query and
  // stream stores, unlike the small composer fixture used in unit flows.
  await page.goto("/e2e/fixtures/chat-voice-workspace.html");
  await expect(page.locator("#workspace-chat-message-welcome").getByText("Workspace ready.", { exact: true })).toBeVisible();
  await page.evaluate(() => {
    const connect = AudioNode.prototype.connect;
    (window as any).replyPeak = 0;
    AudioNode.prototype.connect = function (destination: any, ...args: any[]) {
      if (destination instanceof AudioDestinationNode) {
        const meter = this.context.createAnalyser();
        connect.call(this, meter);
        setInterval(() => {
          const values = new Float32Array(meter.fftSize);
          meter.getFloatTimeDomainData(values);
          (window as any).replyPeak = Math.max((window as any).replyPeak, ...values.map(Math.abs));
        }, 20);
      }
      return (connect as any).call(this, destination, ...args);
    } as typeof connect;
  });
  await page.getByRole("button", { name: "Start voice call", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "Voice call" });
  await expect(dialog.getByRole("status")).toHaveText("Listening");
  await expect.poll(() => refetches).toBeGreaterThanOrEqual(2);
  expect(received.find(e => e.type === "start").conversation_id).toBe("fixture-conversation");
  wire.send(JSON.stringify({ type: "transcript", generation: 1, role: "user", text: "你好" }));
  messages.push({ ...messages[0], id: "saved-reply", body: "你好，我在。" });
  wire.send(JSON.stringify({ type: "turn", generation: 1, conversation_id: "fixture-conversation", text: "你好，我在。" }));
  await expect.poll(() => refetches).toBeGreaterThanOrEqual(3);
  await expect(dialog.getByRole("log")).toContainText("你好，我在。");
  expect(received.some(e => e.type === "end")).toBeFalsy();
  const reply = readFileSync(new URL("../../../tests/fixtures/voice/zh-reply.mp3", import.meta.url)).toString("base64");
  wire.send(JSON.stringify({ type: "audio_clip", generation: 1, item_id: "workspace-reply", audio: reply }));
  await expect(dialog.getByRole("status")).toHaveText("AI is speaking");
  await expect.poll(async () => page.evaluate(() => (window as any).replyPeak)).toBeGreaterThan(0.005);
  await expect.poll(() => received.some(e => e.type === "clip_done" && e.item_id === "workspace-reply")).toBeTruthy();
  await expect(dialog.getByRole("log")).toContainText("你好，我在。");
  await expect(dialog).toBeVisible();
  await dialog.getByRole("button", { name: "End call", exact: true }).click();
});

test("real browser PCM and gateway WebSocket suppress echo and resume after manual interruption", async ({ page }) => {
  test.skip(!process.env.VOICE_GATEWAY_E2E, "Requires the isolated tests.voice_browser_server fixture");
  await setup(page, "workspace", true);
  await page.unroute("**/api/v1/**");
  await page.goto("/e2e/fixtures/chat-voice-workspace.html");
  await expect(page.locator("#workspace-chat-message-welcome").getByText("Workspace ready.", { exact: true })).toBeVisible();
  await page.evaluate(() => {
    const connect = AudioNode.prototype.connect;
    (window as any).outputPeak = 0;
    AudioNode.prototype.connect = function (destination: any, ...args: any[]) {
      if (destination instanceof AudioDestinationNode) {
        const meter = this.context.createAnalyser();
        connect.call(this, meter);
        setInterval(() => {
          const values = new Float32Array(meter.fftSize);
          meter.getFloatTimeDomainData(values);
          (window as any).outputPeak = Math.max((window as any).outputPeak, ...values.map(Math.abs));
        }, 20);
      }
      return (connect as any).call(this, destination, ...args);
    } as typeof connect;
  });
  await page.getByRole("button", { name: "Start voice call", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "Voice call" });
  await expect(dialog.getByRole("status")).toHaveText("Listening");
  const speech = readFileSync(new URL("../../../tests/fixtures/voice/zh-call.wav", import.meta.url)).toString("base64");
  await page.evaluate(encoded => (window as any).voiceFixture.startInput(encoded), speech);
  await expect(dialog.getByRole("status")).toHaveText("AI is speaking");
  await expect.poll(async () => page.evaluate(() => (window as any).outputPeak)).toBeGreaterThan(0.005);
  const answer = "你好，我能听到你的声音，现在测试中文语音回复。";
  await expect(dialog.getByRole("log")).toContainText(answer);
  await expect(dialog.getByRole("button", { name: "Interrupt", exact: true })).toBeVisible();
  const beforeEcho = await (await page.request.get("/api/v1/voice-test-observations")).json();
  await page.evaluate(encoded => (window as any).voiceFixture.startInput(encoded), speech);
  await page.waitForTimeout(800);
  const afterEcho = await (await page.request.get("/api/v1/voice-test-observations")).json();
  expect(afterEcho.received.filter((type: string) => type === "audio")).toHaveLength(
    beforeEcho.received.filter((type: string) => type === "audio").length
  );
  await dialog.getByRole("button", { name: "Interrupt", exact: true }).click();
  await expect(dialog.getByRole("status")).toHaveText("Listening");
  await page.waitForTimeout(300);
  await page.evaluate(encoded => (window as any).voiceFixture.startInput(encoded), speech);
  await expect.poll(async () => {
    const result = await page.request.get("/api/v1/voice-test-observations");
    return (await result.json()).inputs;
  }).toBe(2);
  await expect(dialog.getByRole("log")).toContainText("继续");
  const result = await (await page.request.get("/api/v1/voice-test-observations")).json();
  expect(result.received.filter((type: string) => type === "stop_reply")).toHaveLength(1);
  expect(result.events.filter((type: string) => type === "turn")).toHaveLength(2);
  await expect(dialog.getByRole("log")).toContainText(answer);
  await dialog.getByRole("button", { name: "End call", exact: true }).click();
});

test("gateway audio decoded after interruption never resumes playback", async ({ page }) => {
  let wire: any;
  await page.routeWebSocket(/\/api\/v1\/audio\/live$/, socket => {
    wire = socket;
    socket.onMessage(data => {
      if (JSON.parse(String(data)).type === "start") socket.send(JSON.stringify({ type: "ready", conversation_id: "conversation-one" }));
    });
  });
  await setup(page, "chat", true);
  await page.evaluate(() => {
    const decode = AudioContext.prototype.decodeAudioData;
    AudioContext.prototype.decodeAudioData = function (buffer) {
      return new Promise(resolve => { (window as any).releaseVoiceDecode = () => decode.call(this, buffer).then(resolve); });
    } as typeof decode;
  });
  await page.getByRole("button", { name: "Start voice call", exact: true }).click();
  const status = page.getByRole("dialog").getByRole("status");
  await expect(status).toHaveText("Listening");
  wire.send(JSON.stringify({ type: "audio_clip", item_id: "delayed", audio: wavClip() }));
  await expect.poll(() => page.evaluate(() => !!(window as any).releaseVoiceDecode)).toBeTruthy();
  wire.send(JSON.stringify({ type: "interrupt" }));
  await expect(status).toHaveText("Listening");
  await page.evaluate(() => (window as any).releaseVoiceDecode());
  await expect(status).toHaveText("Listening");
  await page.getByRole("button", { name: "End call", exact: true }).click();
});

async function setup(page: Page, surface = "chat", live = false, locale = "en", empty = false) {
  await page.addInitScript((live) => {
    const state = { stoppedTracks: 0, uploads: 0, plays: 0, pauses: 0, deny: false, pending: false,
      audio: null as any, resolve: null as null | (() => void), constraints: null as any };
    (window as any).voiceFixture = state;
    Object.defineProperty(navigator, "mediaDevices", { value: {
      getSupportedConstraints: () => ({ voiceIsolation: true }),
      getUserMedia: async (constraints: MediaStreamConstraints) => {
      state.constraints = constraints;
      if (state.deny) throw new DOMException("Denied", "NotAllowedError");
      if (state.pending) await new Promise<void>((resolve) => { state.resolve = resolve; });
      if (live) {
        const context = new AudioContext();
        const destination = context.createMediaStreamDestination();
        const stream = destination.stream;
        (state as any).startInput = async (encoded: string) => {
          const bytes = Uint8Array.from(atob(encoded), char => char.charCodeAt(0));
          const source = context.createBufferSource();
          source.buffer = await context.decodeAudioData(bytes.buffer);
          source.connect(destination);
          await context.resume();
          source.start();
        };
        const track = stream.getAudioTracks()[0];
        (state as any).liveTrack = track;
        const stop = track.stop.bind(track);
        track.stop = () => { state.stoppedTracks++; stop(); void context.close(); };
        return stream;
      }
      return { getTracks: () => [{ stop: () => { state.stoppedTracks++; }, onended: null }] };
    } } });
    class Recorder {
      static isTypeSupported(type: string) { return type === "audio/mp4"; }
      state = "inactive";
      mimeType = "audio/mp4";
      ondataavailable: any;
      onstop: any;
      onerror: any;
      start() { this.state = "recording"; }
      stop() {
        this.state = "inactive";
        // The last data event happens asynchronously, before onstop.
        setTimeout(() => {
          this.ondataavailable?.({ data: new Blob(["last-audio-chunk"], { type: this.mimeType }) });
          this.onstop?.();
        }, 0);
      }
    }
    (window as any).MediaRecorder = Recorder;
    class Audio {
      constructor() { state.audio = this; }
      src = "";
      onended: any;
      onerror: any;
      async play() { state.plays++; }
      pause() { state.pauses++; }
      removeAttribute() { this.src = ""; }
      load() {}
    }
    (window as any).Audio = Audio;
  }, live);
  await page.route("**/config", (route) => route.fulfill({ json: {} }));
  await page.route("**/api/v1/**", (route) => {
    const path = new URL(route.request().url()).pathname;
    const info = { channel_name: "Voice demo", agent_name: "Assistant", language: "en", welcome_message: "Welcome", login_required: false };
    let body: unknown = [];
    if (path.endsWith("/voice-demo")) body = info;
    if (path.endsWith("/session")) body = { session_id: "voice-session", conversation_id: "public-conversation" };
    if (path.endsWith("/messages")) body = { messages: [{ id: "reply", role: "assistant", content: "Hello visitor!", created_at: "2026-01-01T12:00:00Z" }] };
    return route.fulfill({ json: body });
  });
  await page.goto(`/e2e/fixtures/chat-voice.html?surface=${surface}&locale=${locale}${empty ? "&empty=1" : ""}`);
  await expect(page.getByRole("button", { name: locale === "zh" ? "语音输入" : "Voice input", exact: true })).toBeVisible();
}

test("workspace: first voice call adopts its server-created conversation without disconnecting", async ({ page }) => {
  const received: any[] = [];
  await page.routeWebSocket(/\/api\/v1\/audio\/live$/, socket => {
    socket.onMessage(data => {
      const event = JSON.parse(String(data));
      received.push(event);
      if (event.type === "start") {
        socket.send(JSON.stringify({
          type: "ready",
          generation: 0,
          conversation_id: "conversation-one",
        }));
      }
    });
  });
  await setup(page, "workspace", true, "en", true);
  await expect(page.getByTestId("conversation")).toBeEmpty();

  await page.getByRole("button", { name: "Start voice call", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "Voice call" });
  await expect(dialog.getByRole("status")).toHaveText("Listening");
  await expect(page.getByTestId("conversation")).toHaveText("conversation-one");
  await page.waitForTimeout(100);
  await expect(dialog.getByRole("status")).toHaveText("Listening");
  expect(received.filter(event => event.type === "start")).toHaveLength(1);

  await dialog.getByRole("button", { name: "End call", exact: true }).click();
});

for (const surface of ["chat", "workspace", "public"]) {
  test(`${surface}: records final audio, appends transcript and plays AI reply`, async ({ page }) => {
    await setup(page, surface);
    const uploads: string[] = [];
    await page.route("**/audio/transcribe", async (route) => {
      uploads.push(route.request().postData() || "");
      await route.fulfill({ json: { text: "Voice transcript" } });
    });
    const editor = page.getByRole("textbox");
    if (surface === "public") await editor.fill("Draft");
    await page.getByRole("button", { name: "Voice input", exact: true }).click();
    await expect(page.getByRole("status").filter({ hasText: "Recording" })).toBeVisible();
    await editor.press("Enter");
    if (surface !== "public") await expect(page.getByTestId("sent")).toBeEmpty();
    await page.getByRole("button", { name: "Finish recording" }).click();
    await expect.poll(() => uploads.length).toBe(1);
    expect(uploads[0]).toContain('filename="voice.m4a"');
    expect(uploads[0]).toContain("last-audio-chunk");
    if (surface === "workspace") expect(uploads[0]).toContain("workspace_id");
    if (surface === "public") {
      expect(uploads[0]).toContain("voice-session");
      await expect(editor).toHaveValue("Draft Voice transcript");
    } else await expect(page.getByTestId("draft")).toHaveText("Draft Voice transcript");
    await expect.poll(() => page.evaluate(() => (window as any).voiceFixture.stoppedTracks)).toBeGreaterThan(0);
    const requests: any[] = [];
    await page.route(/\/(?:tts|audio\/speech)$/, (route) => {
      requests.push(route.request().postDataJSON());
      return route.fulfill({ contentType: "audio/mpeg", body: "audio" });
    });
    await page.getByRole("button", { name: "Read aloud · AI voice", exact: true }).first().focus();
    await page.getByRole("button", { name: "Read aloud · AI voice", exact: true }).first().click();
    await expect(page.getByRole("button", { name: "Stop AI voice" })).toBeVisible();
    if (surface === "public") expect(requests[0]).toMatchObject({ session_id: "voice-session", message_id: "reply" });
    else expect(requests[0]).toMatchObject({ conversation_id: "conversation-one" });
    await page.getByRole("button", { name: "Stop AI voice" }).click();
    await expect.poll(() => page.evaluate(() => (window as any).voiceFixture.pauses)).toBeGreaterThan(0);
  });
}

test("permission failure preserves the draft and delayed permission is cancelled", async ({ page }) => {
  await setup(page);
  await page.evaluate(() => { (window as any).voiceFixture.deny = true; });
  await page.getByRole("button", { name: "Voice input", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText("Microphone access was denied");
  await expect(page.getByTestId("draft")).toHaveText("Draft");
  await page.evaluate(() => { (window as any).voiceFixture.deny = false; (window as any).voiceFixture.pending = true; });
  await page.getByRole("button", { name: "Voice input", exact: true }).click();
  await page.getByRole("button", { name: "Cancel voice input" }).click();
  await page.evaluate(() => (window as any).voiceFixture.resolve());
  await expect.poll(() => page.evaluate(() => (window as any).voiceFixture.stoppedTracks)).toBeGreaterThan(0);
  await expect(page.getByTestId("draft")).toHaveText("Draft");
});

for (const surface of ["chat", "workspace", "public"]) {
  test(`${surface}: live call streams PCM, speaks, supports interruption, mute and hangup`, async ({ page }) => {
    const received: any[] = [];
    let wire: any;
    await page.routeWebSocket(/\/api\/v1\/audio\/live$/, socket => {
      wire = socket;
      socket.onMessage(data => {
        const event = JSON.parse(String(data));
        received.push(event);
        if (event.type === "start") socket.send(JSON.stringify({ type: "ready", conversation_id: surface === "public" ? "public-conversation" : "conversation-one" }));
      });
    });
    await setup(page, surface, true);
    await page.getByRole("button", { name: "Start voice call", exact: true }).click();
    const dialog = page.getByRole("dialog", { name: "Voice call" });
    await expect.poll(() => dialog.innerText()).toContain("Listening");
    const start = received.find(e => e.type === "start");
    if (surface === "public") expect(start).toMatchObject({ public_token: "voice-demo", session_id: "voice-session" });
    else expect(start.conversation_id).toBe("conversation-one");
    if (surface === "workspace") expect(start.workspace_id).toBe("workspace");
    await expect.poll(() => received.filter(e => e.type === "audio").length).toBeGreaterThan(1);
    const send = (event: any) => wire.send(JSON.stringify(event));
    send({ type: "transcript", role: "user", text: "Can we talk about my workspace?" });
    send({ type: "thinking" });
    await expect(dialog.getByRole("status")).toHaveText("Thinking…");
    send({ type: "audio", item_id: "spoken-one", audio: Buffer.alloc(48000).toString("base64") });
    send({ type: "caption", item_id: "spoken-one", delta: "Of course. What would you like to work on?" });
    await expect(dialog.getByRole("status")).toHaveText("AI is speaking");
    await expect(dialog.getByRole("log")).toContainText("Of course.");
    send({ type: "interrupt", item_id: "spoken-one" });
    await expect(dialog.getByRole("status")).toHaveText("Listening");
    await expect.poll(() => received.some(e => e.type === "played" && e.item_id === "spoken-one")).toBeTruthy();
    const played = received.find(e => e.type === "played");
    expect(played.audio_end_ms).toBeGreaterThanOrEqual(0);
    expect(played.audio_end_ms).toBeLessThanOrEqual(1000);
    await dialog.getByRole("button", { name: "Mute", exact: true }).click();
    await expect(dialog.getByRole("status")).toHaveText("Microphone muted");
    expect(await page.evaluate(() => (window as any).voiceFixture.liveTrack.enabled)).toBe(false);
    await dialog.getByRole("button", { name: "Unmute", exact: true }).click();
    expect(await page.evaluate(() => (window as any).voiceFixture.liveTrack.enabled)).toBe(true);
    send({ type: "audio", item_id: "spoken-two", audio: Buffer.alloc(4800).toString("base64") });
    send({ type: "audio_done", item_id: "spoken-two" });
    await expect(dialog.getByRole("status")).toHaveText("Listening");
    await dialog.getByRole("button", { name: "End call" }).click();
    await expect(dialog).toHaveCount(0);
    await expect.poll(() => received.some(e => e.type === "end")).toBeTruthy();
    expect(await page.evaluate(() => (window as any).voiceFixture.stoppedTracks)).toBe(1);
  });
}

test("live call permission cancellation and scope changes release all audio", async ({ page }) => {
  await page.routeWebSocket(/\/api\/v1\/audio\/live$/, socket => socket.onMessage(data => {
    if (JSON.parse(String(data)).type === "start") socket.send(JSON.stringify({ type: "ready", conversation_id: "conversation-one" }));
  }));
  await setup(page, "chat", true);
  await page.evaluate(() => { (window as any).voiceFixture.pending = true; });
  await page.getByRole("button", { name: "Start voice call", exact: true }).click();
  await expect.poll(() => page.evaluate(() => Boolean((window as any).voiceFixture.resolve))).toBeTruthy();
  await page.getByRole("button", { name: "End call" }).click();
  await page.evaluate(() => (window as any).voiceFixture.resolve());
  await expect.poll(() => page.evaluate(() => (window as any).voiceFixture.stoppedTracks)).toBe(1);
  await page.evaluate(() => { (window as any).voiceFixture.pending = false; });
  await page.getByRole("button", { name: "Start voice call", exact: true }).click();
  await expect(page.getByRole("dialog").getByRole("status")).toHaveText("Listening");
  await page.getByRole("button", { name: "Switch conversation" }).evaluate((button: HTMLButtonElement) => button.click());
  await expect(page.getByRole("dialog")).toHaveCount(0);
  expect(await page.evaluate(() => (window as any).voiceFixture.stoppedTracks)).toBe(2);
});

test("live call desktop and mobile panel, focus trap and disconnect recovery", async ({ page }) => {
  let wire: any;
  await page.routeWebSocket(/\/api\/v1\/audio\/live$/, socket => {
    wire = socket;
    socket.onMessage(data => {
      if (JSON.parse(String(data)).type === "start") socket.send(JSON.stringify({ type: "ready", conversation_id: "conversation-one" }));
    });
  });
  await setup(page, "workspace", true, "zh");
  for (const width of [1280, 390]) {
    await page.setViewportSize({ width, height: 850 });
    await page.getByRole("button", { name: "开始语音通话", exact: true }).click();
    const dialog = page.getByRole("dialog");
    await expect(dialog.getByRole("status")).toHaveText("正在聆听");
    wire.send(JSON.stringify({ type: "transcript", role: "user", text: "帮我看一下今天的工作安排。" }));
    wire.send(JSON.stringify({ type: "caption", item_id: "voice", delta: "好的，我们先从今天最重要的事情开始。" }));
    await expect(dialog.getByRole("log")).toContainText("今天最重要");
    await dialog.getByRole("button", { name: "挂断" }).focus();
    await page.keyboard.press("Tab");
    expect(await dialog.evaluate(node => node.contains(document.activeElement))).toBe(true);
    await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBeTruthy();
    await page.screenshot({ path: `/tmp/manor-live-call-${width}.png`, fullPage: true, animations: "disabled" });
    await page.keyboard.press("Escape");
    await expect(dialog).toHaveCount(0);
  }
  await page.getByRole("button", { name: "开始语音通话", exact: true }).click();
  await expect(page.getByRole("dialog").getByRole("status")).toHaveText("正在聆听");
  wire.send(JSON.stringify({ type: "error", message: "Workspace access revoked" }));
  await expect(page.getByRole("alert")).toHaveText("语音通话失败，请重试。");
  await expect(page.getByRole("button", { name: "重新呼叫" })).toBeVisible();
  expect(await page.evaluate(() => (window as any).voiceFixture.stoppedTracks)).toBe(3);
});

test("conversation change discards a late transcript and stops playback", async ({ page }) => {
  await setup(page);
  let finish!: () => void;
  await page.route("**/audio/transcribe", async (route) => {
    await new Promise<void>((resolve) => { finish = resolve; });
    await route.fulfill({ json: { text: "Old transcript" } }).catch(() => {});
  });
  await page.getByRole("button", { name: "Voice input", exact: true }).click();
  await page.getByRole("button", { name: "Finish recording" }).click();
  await expect.poll(() => Boolean(finish)).toBeTruthy();
  await page.getByRole("button", { name: "Switch conversation" }).click();
  finish();
  await expect(page.getByTestId("draft")).toHaveText("New draft");
  await page.route("**/chat/tts", (route) => route.fulfill({ body: "audio", contentType: "audio/mpeg" }));
  await page.getByRole("button", { name: "Read aloud · AI voice", exact: true }).first().focus();
    await page.getByRole("button", { name: "Read aloud · AI voice", exact: true }).first().click();
  await expect(page.getByRole("button", { name: "Stop AI voice" })).toHaveCount(1);
  await page.getByRole("button", { name: "Read aloud · AI voice", exact: true }).focus();
  await page.getByRole("button", { name: "Read aloud · AI voice", exact: true }).click();
  await expect(page.getByRole("button", { name: "Stop AI voice" })).toHaveCount(1);
  await page.getByRole("button", { name: "Voice input", exact: true }).click();
  await expect(page.getByRole("button", { name: "Stop AI voice" })).toHaveCount(0);
});

test("desktop and mobile voice controls fit and have keyboard focus", async ({ page }) => {
  await setup(page, "workspace");
  for (const width of [1280, 390]) {
    await page.setViewportSize({ width, height: 850 });
    const mic = page.getByRole("button", { name: "Voice input", exact: true });
    await mic.focus();
    await expect(mic).toBeFocused();
    await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBeTruthy();
    await mic.click();
    await page.screenshot({ path: `/tmp/manor-chat-voice-${width}.png`, fullPage: true });
    await page.getByRole("button", { name: "Cancel voice input" }).click();
  }
});

test("public long replies continue with Unicode-safe offsets", async ({ page }) => {
  await setup(page, "public");
  const content = "😀".repeat(3100) + " done";
  await page.route("**/messages?*", (route) => route.fulfill({ json: { messages: [{ id: "long-reply", role: "assistant", content }] } }));
  await page.reload();
  const requests: any[] = [];
  await page.route("**/audio/speech", (route) => {
    requests.push(route.request().postDataJSON());
    return route.fulfill({ body: "audio", contentType: "audio/mpeg" });
  });
  await page.getByRole("button", { name: "Read aloud · AI voice", exact: true }).click();
  await expect(page.getByRole("button", { name: "Stop AI voice" })).toBeVisible();
  expect(requests[0]).toMatchObject({ message_id: "long-reply", offset: 0, length: 3000 });
  await page.evaluate(() => (window as any).voiceFixture.audio.onended());
  await expect.poll(() => requests.length).toBe(2);
  expect(requests[1]).toMatchObject({ offset: 3000, length: 105 });
  await expect.poll(() => page.evaluate(() => (window as any).voiceFixture.plays)).toBe(2);
  await page.evaluate(() => (window as any).voiceFixture.audio.onended());
  await expect(page.getByRole("button", { name: "Stop AI voice" })).toHaveCount(0);
});

test("cancelled speech never plays late audio and page exit releases the microphone", async ({ page }) => {
  await setup(page);
  let finish!: () => void;
  await page.route("**/chat/tts", async (route) => {
    await new Promise<void>((resolve) => { finish = resolve; });
    await route.fulfill({ body: "audio", contentType: "audio/mpeg" }).catch(() => {});
  });
  const play = page.getByRole("button", { name: "Read aloud · AI voice", exact: true }).first();
  await play.focus();
  await play.click();
  await expect.poll(() => Boolean(finish)).toBeTruthy();
  await page.getByRole("button", { name: "Stop AI voice" }).click();
  finish();
  await page.getByRole("button", { name: "Voice input", exact: true }).click();
  await expect(page.getByRole("button", { name: "Finish recording" })).toBeVisible();
  await page.evaluate(() => window.dispatchEvent(new Event("pagehide")));
  await expect(page.getByRole("button", { name: "Finish recording" })).toHaveCount(0);
  expect(await page.evaluate(() => (window as any).voiceFixture.plays)).toBe(0);
  expect(await page.evaluate(() => (window as any).voiceFixture.stoppedTracks)).toBeGreaterThan(0);
});
