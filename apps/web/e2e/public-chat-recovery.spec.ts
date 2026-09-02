import { expect, test, type Page } from "@playwright/test";

const token = "public-recovery";
const sessionId = "visitor-session";
const apiBase = `/api/v1/public/chat/${token}`;
const placeholder = "The assistant started this response and is still working. If this remains after a reload, the stream was interrupted before it could finish.";

async function prepareChat(page: Page) {
  await page.addInitScript(({ token, sessionId }) => {
    localStorage.setItem(`manor_public_chat_session:${token}`, sessionId);
    localStorage.removeItem("manor_token");
  }, { token, sessionId });
  await page.route("**/config", (route) => route.fulfill({ json: {} }));
  await page.route(`**${apiBase}`, (route) => route.fulfill({ json: {
    channel_name: "Recovery chat", agent_name: "Support", language: "en", login_required: false,
  } }));
}

async function openChat(page: Page) {
  await prepareChat(page);
  await page.route(`**${apiBase}/session`, (route) => route.fulfill({ json: {
    session_id: sessionId, conversation_id: "conversation", channel_config_id: "channel",
  } }));
  await page.goto(`/chat/public/${token}`);
  await expect(page.locator("textarea")).toBeVisible();
}

test("a restored running message completes even after the cursor passes it", async ({ page }) => {
  let completed = false;
  const refreshedCursors: string[] = [];
  const first = { id: "user-1", role: "user", content: "First question", created_at: "2026-08-31T00:00:00Z" };
  const later = { id: "user-2", role: "user", content: "Later question", created_at: "2026-08-31T00:00:02Z" };
  const assistant = () => ({
    id: "assistant-1", role: "assistant", created_at: "2026-08-31T00:00:01Z",
    content: completed ? "Recovered complete answer" : placeholder,
    stream_status: completed ? "completed" : "running",
  });
  await page.route(`**${apiBase}/messages?**`, (route) => {
    const params = new URL(route.request().url()).searchParams;
    const refresh = params.getAll("refresh").includes("assistant-1");
    if (refresh) refreshedCursors.push(params.get("after") || "");
    return route.fulfill({ json: {
      messages: params.get("after") ? [] : [first, assistant(), later],
      updates: refresh ? [assistant()] : [],
    } });
  });
  await openChat(page);
  await expect(page.getByText("Later question", { exact: true })).toBeVisible();
  await expect.poll(() => refreshedCursors).toContain("user-2");
  completed = true;
  await expect(page.getByText("Recovered complete answer", { exact: true })).toHaveCount(1);
  await expect(page.getByText(placeholder, { exact: true })).toHaveCount(0);
  expect(refreshedCursors).toContain("user-2");
});

test("poll replaces a partial SSE response without duplicating the assistant bubble", async ({ page }) => {
  let sent = false;
  let completed = false;
  await page.route(`**${apiBase}/message/stream`, (route) => {
    sent = true;
    return route.fulfill({ contentType: "text/event-stream", body:
      'event: stream_start\ndata: {"message_id":"assistant-1"}\n\n' +
      'event: text_delta\ndata: {"content":"Partial answer"}\n\n',
    });
  });
  await page.route(`**${apiBase}/messages?**`, (route) => {
    const params = new URL(route.request().url()).searchParams;
    const assistant = {
      id: "assistant-1", role: "assistant", created_at: "2026-08-31T00:00:01Z",
      content: completed ? "Complete answer after interruption" : "Partial answer",
      stream_status: completed ? "completed" : "streaming",
    };
    return route.fulfill({ json: {
      messages: sent && !params.get("after") ? [
        { id: "user-1", role: "user", content: "Question", created_at: "2026-08-31T00:00:00Z" },
        assistant,
      ] : [],
      updates: sent && params.getAll("refresh").includes(assistant.id) ? [assistant] : [],
    } });
  });
  await openChat(page);
  await page.locator("textarea").fill("Question");
  await page.locator("textarea").press("Enter");
  await expect(page.getByText("Partial answer", { exact: true })).toBeVisible();
  completed = true;
  await expect(page.getByText("Complete answer after interruption", { exact: true })).toHaveCount(1);
  await expect(page.getByText("Partial answer", { exact: true })).toHaveCount(0);
});

test("poll preserves replies postponed while another message is sending", async ({ page }) => {
  let sent = false;
  let pollsDuringSend = 0;
  let releaseStream!: () => void;
  const streamReady = new Promise<void>((resolve) => { releaseStream = resolve; });
  const history = [
    { id: "other-answer", role: "assistant", content: "Reply from another tab", stream_status: "completed", created_at: "2026-08-31T00:00:00Z" },
    { id: "user-1", role: "user", content: "New question", created_at: "2026-08-31T00:00:01Z" },
  ];
  await page.route(`**${apiBase}/message/stream`, async (route) => {
    sent = true;
    await streamReady;
    await route.fulfill({ contentType: "text/event-stream", body:
      'event: stream_start\ndata: {"message_id":"assistant-1"}\n\n' +
      'event: text_delta\ndata: {"content":"Current answer"}\n\n' +
      'event: stream_end\ndata: {}\n\n',
    });
  });
  await page.route(`**${apiBase}/messages?**`, (route) => {
    const params = new URL(route.request().url()).searchParams;
    if (sent) pollsDuringSend += 1;
    const cursorIndex = history.findIndex((message) => message.id === params.get("after"));
    return route.fulfill({ json: { messages: sent ? history.slice(cursorIndex + 1) : [], updates: [] } });
  });
  await openChat(page);
  await page.locator("textarea").fill("New question");
  await page.locator("textarea").press("Enter");
  try {
    await expect.poll(() => pollsDuringSend).toBeGreaterThanOrEqual(2);
  } finally {
    releaseStream();
  }
  await expect(page.getByText("Current answer", { exact: true })).toBeVisible();
  await expect(page.getByText("Reply from another tab", { exact: true })).toHaveCount(1);
  await expect(page.getByText("New question", { exact: true })).toHaveCount(1);
});

for (const endpoint of ["session", "messages", "message/stream"]) {
  test(`a block from ${endpoint} keeps the visitor session and stops chatting`, async ({ page }) => {
    if (endpoint === "session") await page.setViewportSize({ width: 390, height: 844 });
    await prepareChat(page);
    let sessionRequests = 0;
    let blockPoll = false;
    const blocked = { status: 403, json: { detail: { code: "visitor_blocked", message: "This chat visitor is blocked." } } };
    await page.route(`**${apiBase}/session`, (route) => {
      sessionRequests += 1;
      return route.fulfill(endpoint === "session" ? blocked : { json: {
        session_id: sessionId, conversation_id: "conversation", channel_config_id: "channel",
      } });
    });
    await page.route(`**${apiBase}/messages?**`, (route) => route.fulfill(
      endpoint === "messages" && blockPoll ? blocked : { json: { messages: [] } },
    ));
    await page.route(`**${apiBase}/message/stream`, (route) => route.fulfill(blocked));
    await page.goto(`/chat/public/${token}`);
    if (endpoint !== "session") {
      await expect(page.locator("textarea")).toBeVisible();
      if (endpoint === "messages") blockPoll = true;
      else {
        await page.locator("textarea").fill("Hello");
        await page.locator("textarea").press("Enter");
      }
    }
    await expect(page.getByText("Your access to this chat has been blocked.", { exact: true })).toBeVisible();
    await expect(page.getByRole("heading", { name: "Chat access restricted" })).toBeVisible();
    await expect(page.getByText("Ask the person who shared this chat to send you a fresh link.")).toHaveCount(0);
    expect(sessionRequests).toBe(1);
    expect(await page.evaluate((key) => localStorage.getItem(key), `manor_public_chat_session:${token}`)).toBe(sessionId);
    await expect(page.locator("textarea")).toHaveCount(0);
    await page.screenshot({ path: test.info().outputPath("blocked.png"), fullPage: true });
  });
}

test("a different visitor can replace a mismatched stored session", async ({ page }) => {
  await prepareChat(page);
  const requestedSessions: (string | undefined)[] = [];
  await page.route(`**${apiBase}/session`, (route) => {
    const requested = route.request().postDataJSON().session_id;
    requestedSessions.push(requested);
    return route.fulfill(requested ? {
      status: 403, json: { detail: { code: "session_mismatch", message: "This chat session belongs to another signed-in visitor." } },
    } : { json: { session_id: "new-visitor-session", conversation_id: "new-conversation", channel_config_id: "channel" } });
  });
  await page.route(`**${apiBase}/messages?**`, (route) => route.fulfill({ json: { messages: [] } }));
  await page.goto(`/chat/public/${token}`);
  await expect(page.locator("textarea")).toBeVisible();
  expect(requestedSessions).toEqual([sessionId, undefined]);
  expect(await page.evaluate((key) => localStorage.getItem(key), `manor_public_chat_session:${token}`)).toBe("new-visitor-session");
});

test("refreshing an older answer preserves the new message's send failure", async ({ page }) => {
  let completed = false;
  await page.route(`**${apiBase}/messages?**`, (route) => {
    const params = new URL(route.request().url()).searchParams;
    const older = {
      id: "older-answer", role: "assistant", created_at: "2026-08-31T00:00:00Z",
      content: completed ? "Older completed answer" : "Older partial answer",
      stream_status: completed ? "completed" : "streaming",
    };
    return route.fulfill({ json: {
      messages: params.get("after") ? [] : [older],
      updates: params.getAll("refresh").includes(older.id) ? [older] : [],
    } });
  });
  await page.route(`**${apiBase}/message/stream`, (route) => route.fulfill({ status: 503, json: { detail: "Unavailable" } }));
  await openChat(page);
  await expect(page.getByText("Older partial answer", { exact: true })).toBeVisible();
  await page.locator("textarea").fill("New question");
  await page.locator("textarea").press("Enter");
  const failure = page.getByText("We couldn't send that message. Please try again.", { exact: true });
  await expect(failure).toBeVisible();
  completed = true;
  await expect(page.getByText("Older completed answer", { exact: true })).toBeVisible();
  await expect(failure).toBeVisible();
});

for (const failure of ["empty-stream", "network-error"]) {
  for (const initiallyRunning of [false, true]) {
    test(`poll repairs ${failure} before the first SSE event, running=${initiallyRunning}`, async ({ page }) => {
      let clientTurnId = "";
      let completed = !initiallyRunning;
      let refreshed = false;
      await page.route(`**${apiBase}/message/stream`, (route) => {
        const form = route.request().postData() || "";
        clientTurnId = /name="client_turn_id"\r?\n\r?\n([^\r\n]+)/.exec(form)?.[1] || "";
        expect(clientTurnId).not.toBe("");
        return failure === "network-error"
          ? route.abort("failed")
          : route.fulfill({ contentType: "text/event-stream", body: "" });
      });
      await page.route(`**${apiBase}/messages?**`, (route) => {
        const params = new URL(route.request().url()).searchParams;
        const refresh = params.getAll("refresh").includes("saved-reply");
        if (refresh) refreshed = true;
        const reply = {
          id: "saved-reply", role: "assistant", client_turn_id: clientTurnId,
          content: completed ? "Recovered answer without SSE" : placeholder,
          stream_status: completed ? "completed" : "running", created_at: "2026-08-31T00:00:01Z",
        };
        return route.fulfill({ json: {
          messages: clientTurnId && !params.get("after") ? [
            { id: "saved-user", role: "user", content: "Question", client_turn_id: clientTurnId, created_at: "2026-08-31T00:00:00Z" },
            reply,
          ] : [],
          updates: clientTurnId && refresh ? [reply] : [],
        } });
      });
      await openChat(page);
      await page.locator("textarea").fill("Question");
      await page.locator("textarea").press("Enter");
      if (initiallyRunning) {
        await expect.poll(() => refreshed).toBe(true);
        await expect(page.locator(".chat-typing-dots")).toHaveCount(1);
        completed = true;
      }
      await expect(page.getByText("Recovered answer without SSE", { exact: true })).toHaveCount(1);
      await expect(page.getByText("Question", { exact: true })).toHaveCount(1);
      await expect(page.locator(".chat-typing-dots")).toHaveCount(0);
      await expect(page.getByText("We couldn't send that message. Please try again.", { exact: true })).toHaveCount(0);
    });
  }
}

for (const terminal of ["completed", "interrupted-placeholder", "interrupted-empty"]) {
  test(`a known SSE reply recovers from transport failure to ${terminal}`, async ({ page }) => {
    if (terminal === "interrupted-placeholder") await page.setViewportSize({ width: 390, height: 844 });
    let clientTurnId = "";
    let allowRecovery = false;
    let finished = false;
    let refreshRequests = 0;
    await page.addInitScript((apiBase) => {
      const state = window as typeof window & {
        publicTestTurnId?: string;
        publicTestStream?: ReadableStreamDefaultController<Uint8Array>;
      };
      const realFetch = window.fetch.bind(window);
      window.fetch = async (input, init) => {
        if (typeof input === "string" && input.endsWith(`${apiBase}/message/stream`)) {
          state.publicTestTurnId = String((init?.body as FormData).get("client_turn_id"));
          return new Response(new ReadableStream({
            start(controller) { state.publicTestStream = controller; },
          }), { headers: { "content-type": "text/event-stream" } });
        }
        return realFetch(input, init);
      };
    }, apiBase);
    await page.route(`**${apiBase}/messages?**`, async (route) => {
      const browserTurnId = await page.evaluate(() => (window as any).publicTestTurnId);
      clientTurnId = browserTurnId || clientTurnId;
      const params = new URL(route.request().url()).searchParams;
      const refresh = params.getAll("refresh").includes("saved-reply");
      if (refresh) refreshRequests++;
      const reply = {
        id: "saved-reply", role: "assistant", client_turn_id: clientTurnId,
        content: !finished ? placeholder : terminal === "completed" ? "Recovered final answer"
          : terminal === "interrupted-empty" ? "" : placeholder,
        stream_status: !finished ? "running" : terminal === "completed" ? "completed" : "interrupted",
        created_at: "2026-08-31T00:00:01Z",
      };
      return route.fulfill({ json: {
        messages: allowRecovery && !params.get("after") ? [
          { id: "saved-user", role: "user", content: "Question", client_turn_id: clientTurnId, created_at: "2026-08-31T00:00:00Z" },
          reply,
        ] : [],
        updates: allowRecovery && refresh ? [reply] : [],
      } });
    });
    await openChat(page);
    await page.locator("textarea").fill("Question");
    await page.locator("textarea").press("Enter");
    await expect(page.locator("textarea")).toBeDisabled();
    await page.evaluate(() => (window as any).publicTestStream.enqueue(new TextEncoder().encode(
      'event: stream_start\ndata: {"message_id":"saved-reply"}\n\n',
    )));
    // Wait for the client to receive and track the ID before disconnecting.
    await expect.poll(() => refreshRequests).toBeGreaterThan(0);
    await page.evaluate(() => (window as any).publicTestStream.error(new TypeError("Transport disconnected")));
    const failure = page.getByText("We couldn't send that message. Please try again.", { exact: true });
    await expect(failure).toBeVisible();
    allowRecovery = true;
    await expect(page.locator(".chat-typing-dots")).toHaveCount(1);
    await expect(failure).toHaveCount(0);
    await page.screenshot({ path: test.info().outputPath("recovered-running.png"), fullPage: true });
    finished = true;
    const finalText = terminal === "completed" ? "Recovered final answer"
      : "The response was interrupted before it could finish.";
    await expect(page.getByText(finalText, { exact: true })).toHaveCount(1);
    await expect(page.locator(".chat-typing-dots")).toHaveCount(0);
    await expect(failure).toHaveCount(0);
    await expect(page.getByText("Question", { exact: true })).toHaveCount(1);
    await page.screenshot({ path: test.info().outputPath("recovered-terminal.png"), fullPage: true });
    if (terminal !== "completed") {
      await page.reload();
      await expect(page.getByText(finalText, { exact: true })).toHaveCount(1);
      await expect(page.locator(".chat-typing-dots")).toHaveCount(0);
      await expect(page.getByRole("button", { name: "Read aloud" })).toHaveCount(0);
      await page.locator("textarea").fill("Follow-up question");
      await page.locator("textarea").press("Tab");
      await expect(page.getByRole("button", { name: "Send message" })).toBeFocused();
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    }
  });
}

test("an interrupted reply with saved text keeps that text", async ({ page }) => {
  await page.route(`**${apiBase}/messages?**`, (route) => route.fulfill({ json: { messages: [{
    id: "interrupted-reply", role: "assistant", content: "Preserved partial reply",
    stream_status: "interrupted", created_at: "2026-08-31T00:00:00Z",
  }] } }));
  await openChat(page);
  await expect(page.getByText("Preserved partial reply", { exact: true })).toHaveCount(1);
  await expect(page.getByText("The response was interrupted before it could finish.", { exact: true })).toHaveCount(0);
});
