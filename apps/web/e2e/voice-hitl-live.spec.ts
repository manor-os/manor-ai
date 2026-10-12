import { expect, test, type Page, type TestInfo } from "@playwright/test";
import { readFileSync } from "node:fs";

// Real WorkspaceChat + microphone PCM + Python Native/Gateway transports +
// durable Main approvals. Only ASR/Live/TTS and external provider I/O are fake.
// Start tests.voice_hitl_browser_server on loopback; never use a live account.
test.skip(!process.env.VOICE_HITL_E2E, "Requires the isolated Voice HITL browser server");
test.setTimeout(90_000);
const speech = readFileSync(new URL("../../../tests/fixtures/voice/zh-call.wav", import.meta.url)).toString("base64");

async function observations(page: Page) {
  const response = await page.request.get("/api/v1/voice-hitl-test-observations");
  expect(response.ok()).toBe(true);
  return response.json();
}

async function setup(page: Page, mode: string, scenario = "single") {
  const reset = await page.request.post("/api/v1/voice-hitl-test-reset", { data: { mode, scenario } });
  expect(reset.ok(), await reset.text()).toBe(true);
  const errors: string[] = [];
  page.on("pageerror", error => errors.push(error.message));
  await page.addInitScript(() => {
    localStorage.setItem("manor_locale", "en");
    const state = { peak: 0, stoppedTracks: 0, startInput: async (_encoded: string) => {} };
    (window as any).hitlAudio = state;
    const connect = AudioNode.prototype.connect;
    AudioNode.prototype.connect = function (destination: any, ...args: any[]) {
      if (destination instanceof AudioDestinationNode) {
        const meter = this.context.createAnalyser();
        connect.call(this, meter);
        setInterval(() => {
          const values = new Float32Array(meter.fftSize);
          meter.getFloatTimeDomainData(values);
          state.peak = Math.max(state.peak, ...values.map(Math.abs));
        }, 20);
      }
      return (connect as any).call(this, destination, ...args);
    } as typeof connect;
    Object.defineProperty(navigator, "mediaDevices", { value: {
      getSupportedConstraints: () => ({ voiceIsolation: true }),
      getUserMedia: async () => {
        const context = new AudioContext();
        const destination = context.createMediaStreamDestination();
        state.startInput = async (encoded: string) => {
          const source = context.createBufferSource();
          const bytes = Uint8Array.from(atob(encoded), char => char.charCodeAt(0));
          source.buffer = await context.decodeAudioData(bytes.buffer);
          source.connect(destination);
          await context.resume();
          await new Promise<void>(resolve => {
            source.onended = () => resolve();
            source.start();
          });
        };
        const track = destination.stream.getAudioTracks()[0];
        const stop = track.stop.bind(track);
        track.stop = () => { state.stoppedTracks++; stop(); void context.close(); };
        return destination.stream;
      },
    } });
  });
  await page.route("**/config", route => route.fulfill({ json: {} }));
  await page.goto("/e2e/fixtures/chat-voice-workspace.html");
  await expect(page.getByRole("button", { name: "Start voice call", exact: true })).toBeVisible({ timeout: 10_000 })
    .catch(error => { throw new Error(`${error.message}\nBrowser errors: ${JSON.stringify(errors)}`); });
  await page.getByRole("button", { name: "Start voice call", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "Voice call" });
  await expect(dialog.getByRole("status")).toHaveText("Listening");
  return { dialog, errors };
}

async function say(page: Page, text: string) {
  const before = await observations(page);
  const response = await page.request.post("/api/v1/voice-hitl-test-utterance", { data: { text } });
  expect(response.ok(), await response.text()).toBe(true);
  await page.evaluate(() => { (window as any).hitlAudio.peak = 0; });
  await page.evaluate(encoded => (window as any).hitlAudio.startInput(encoded), speech);
  await expect.poll(async () => (await observations(page)).inputs.length).toBe(before.inputs.length + 1);
  await expect.poll(async () => (await observations(page)).turns.length).toBe(before.turns.length + 1);
  const dialog = page.getByRole("dialog", { name: "Voice call" });
  await expect(dialog.getByRole("log").locator(".live-chat-call__user").last()).toContainText(text);
  await expect.poll(() => page.evaluate(() => (window as any).hitlAudio.peak)).toBeGreaterThan(0.005);
  await expect(dialog.getByRole("status")).toHaveText("Listening");
  const result = await observations(page);
  expect(result.server_errors).toEqual([]);
  for (const action of result.turns.at(-1).presented_actions) {
    await expect(dialog.getByRole("log")).toContainText(`第${action.item_number}项`);
    await expect(dialog.getByRole("log")).toContainText(action.description);
  }
  if (result.pending.some((action: any) => action.requires_ui)) {
    await expect(dialog.getByRole("log")).toContainText("请在聊天界面完成");
  }
  return result;
}

async function expectHistoryCards(page: Page, result: any) {
  const message = result.messages.find((row: any) => row.hitl_requests?.length);
  expect(message).toBeTruthy();
  const cards = page.locator(`#workspace-chat-message-${message.id} .chat-hitl-card`);
  await expect(cards).toHaveCount(message.hitl_requests.length);
  for (const [index, request] of message.hitl_requests.entries()) {
    const card = cards.nth(index);
    if (request.resolved) {
      const tone = request.resolution === "reject" ? "rejected" : "approved";
      await expect(card.locator(`.chat-hitl-resolved--${tone}`)).toHaveCount(1);
      await expect(card.getByRole("button", { name: "Approve", exact: true })).toHaveCount(0);
    } else {
      await expect(card.locator(".chat-hitl-resolved")).toHaveCount(0);
      expect(await card.getByRole("button").count()).toBeGreaterThan(0);
    }
  }
}

async function finish(page: Page, info: TestInfo, errors: string[]) {
  const result = await observations(page);
  expect(result.audio_in).toBeGreaterThan(0);
  expect(result.audio_out).toBeGreaterThan(0);
  expect(result.server_errors).toEqual([]);
  expect(result.messages.filter((message: any) => message.author_kind === "user").map((message: any) => message.body))
    .toEqual(result.inputs);
  expect(errors).toEqual([]);
  const dialog = page.getByRole("dialog", { name: "Voice call" });
  await expect(dialog.getByRole("log")).not.toContainText("_fingerprint");
  await expect(dialog.getByRole("log")).not.toContainText("approval_token");
  await page.screenshot({ path: info.outputPath("voice-approval-call.png"), fullPage: true });
  await info.attach("voice-approval-observations", { body: JSON.stringify(result, null, 2), contentType: "application/json" });
  await dialog.getByRole("button", { name: "End call", exact: true }).click();
  await expect(dialog).toHaveCount(0);
  expect(await page.evaluate(() => (window as any).hitlAudio.stoppedTracks)).toBe(1);
  const lastReply = result.messages.filter((message: any) => message.author_kind === "agent").at(-1);
  const lastUser = result.messages.filter((message: any) => message.author_kind === "user").at(-1);
  await expect(page.locator(`#workspace-chat-message-${lastReply.id}`)).toContainText(lastReply.body);
  await expect(page.locator(`#workspace-chat-message-${lastUser.id}`)).toContainText(lastUser.body);
  await expectHistoryCards(page, result);
  await page.screenshot({ path: info.outputPath("voice-approval-history.png"), fullPage: true });
  const visibleHistory = await page.locator("main").innerText();
  await page.reload();
  await expect(page.getByRole("button", { name: "Start voice call", exact: true })).toBeVisible();
  await expect(page.locator(`#workspace-chat-message-${lastReply.id}`)).toContainText(lastReply.body);
  await expect(page.locator(`#workspace-chat-message-${lastUser.id}`)).toContainText(lastUser.body);
  await expectHistoryCards(page, result);
  // A reload cannot change durable decisions or repeat external execution.
  const reloaded = await observations(page);
  expect(reloaded.effects).toEqual(result.effects);
  expect(reloaded.requests).toEqual(result.requests);
  expect(visibleHistory).not.toContain("_fingerprint");
  expect(visibleHistory).not.toContain("approval_token");
}

for (const mode of ["gateway", "native"]) {
  test(`${mode}: spoken approve executes once and duplicate confirmation is blocked`, async ({ page }, info) => {
    const { errors } = await setup(page, mode);
    const prompted = await say(page, "请处理请求");
    expect(prompted.effects).toEqual([]);
    expect(prompted.pending).toHaveLength(1);
    const approved = await say(page, "确认");
    expect(approved.effects.map((effect: any) => effect.text)).toEqual(["First post"]);
    expect(approved.requests.map((request: any) => request.status)).toEqual(["consumed"]);
    const duplicate = await say(page, "确认");
    expect(duplicate.effects.map((effect: any) => effect.text)).toEqual(["First post"]);
    expect(duplicate.errors.at(-1).code).toBe("approval_not_presented");
    await finish(page, info, errors);
  });

  test(`${mode}: spoken reject cannot later be approved`, async ({ page }, info) => {
    const { errors } = await setup(page, mode);
    await say(page, "请处理请求");
    const rejected = await say(page, "拒绝");
    expect(rejected.effects).toEqual([]);
    expect(rejected.requests.map((request: any) => request.status)).toEqual(["denied"]);
    const repeated = await say(page, "确认");
    expect(repeated.effects).toEqual([]);
    expect(repeated.requests.map((request: any) => request.status)).toEqual(["denied"]);
    await finish(page, info, errors);
  });

  test(`${mode}: multiple requests require an explicit numbered decision`, async ({ page }, info) => {
    const { dialog, errors } = await setup(page, mode, "multi");
    const prompted = await say(page, "请处理请求");
    expect(prompted.pending).toHaveLength(2);
    const blocked = await say(page, "确认");
    expect(blocked.effects).toEqual([]);
    expect(blocked.errors.at(-1).code).toBe("approval_target_ambiguous");
    const approved = await say(page, "批准第二项");
    expect(approved.effects.map((effect: any) => effect.text)).toEqual(["Exact second post"]);
    expect(approved.pending).toHaveLength(1);
    expect(approved.requests.filter((request: any) => request.status === "pending")).toHaveLength(1);
    expect(approved.requests.filter((request: any) => request.status === "consumed")).toHaveLength(1);
    expect(approved.requests.find((request: any) => request.id === prompted.pending[0].id).status).toBe("pending");
    expect(approved.requests.find((request: any) => request.id === prompted.pending[1].id).status).toBe("consumed");
    await expect(dialog.getByRole("log")).toContainText("批准第二项");
    await finish(page, info, errors);
  });

  test(`${mode}: asking about approval is not consent`, async ({ page }, info) => {
    const { errors } = await setup(page, mode);
    await say(page, "请处理请求");
    const question = await say(page, "为什么要确认？");
    expect(question.effects).toEqual([]);
    expect(question.pending).toHaveLength(1);
    expect(question.requests.map((request: any) => request.status)).toEqual(["pending"]);
    await finish(page, info, errors);
  });

  test(`${mode}: login or human-only requests retain the Chat UI handoff`, async ({ page }, info) => {
    const { errors } = await setup(page, mode, "ui");
    const prompted = await say(page, "请处理请求");
    expect(prompted.pending[0].requires_ui).toBe(true);
    const blocked = await say(page, "确认");
    expect(blocked.effects).toEqual([]);
    expect(blocked.errors.at(-1).code).toBe("approval_requires_ui");
    await finish(page, info, errors);
  });

  test(`${mode}: mobile call keeps numbered approval and history usable`, async ({ page }, info) => {
    await page.setViewportSize({ width: 390, height: 844 });
    const { dialog, errors } = await setup(page, mode, "multi");
    await say(page, "请处理请求");
    const rejected = await say(page, "拒绝第2项");
    expect(rejected.effects).toEqual([]);
    expect(rejected.pending).toHaveLength(1);
    expect(rejected.requests.filter((request: any) => request.status === "denied")).toHaveLength(1);
    const box = await dialog.boundingBox();
    expect(box).not.toBeNull();
    expect(box!.x).toBeGreaterThanOrEqual(0);
    expect(box!.x + box!.width).toBeLessThanOrEqual(391);
    await finish(page, info, errors);
  });
}
