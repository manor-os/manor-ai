import { expect, test } from "@playwright/test";

const conversationId = "history-conversation";
const message = (id: string, body: string, authorKind: "user" | "agent", minute: number) => ({
  id, conversation_id: conversationId, body, author_kind: authorKind,
  author_user_id: authorKind === "user" ? "history-user" : null,
  author_user_name: authorKind === "user" ? "History Test" : null,
  author_subscription_id: null, message_kind: "text", refs: [], attachments: [], meta: {},
  pending_action: null, resolved_at: null, resolution: null,
  created_at: `2026-10-06T20:${String(minute).padStart(2, "0")}:00.000Z`,
});
const recentMessages = [
  message("recent-user", "Previous user question", "user", 10),
  message("recent-reply", "Previous assistant answer", "agent", 11),
];
const olderMessages = [
  message("older-user", "Earlier user question", "user", 0),
  message("older-reply", "Earlier assistant answer", "agent", 1),
];
const historyIds = [...olderMessages, ...recentMessages].map((item) => item.id);
const frame = (event: string, value: Record<string, unknown>) =>
  `event: ${event}\ndata: ${JSON.stringify(value)}\n\n`;
const supportingResponse = (path: string) => {
  if (path.includes("agent-initiatives")) return { items: [], total: 0 };
  if (path.endsWith("/app-view")) return {
    workspace_id: "app-fixture", title: "Studio", status: "active", can_start: true,
    config: { visual: "plain", layout: "feature" }, actions: [],
    agent_options: [], workflow_options: [], automation_options: [],
  };
  if (path.startsWith("/api/v1/approval-settings/")) return {
    scope: { kind: "workspace", id: "app-fixture", entity_id: "history-entity" },
    revision: 1, version: "history-policy", policy: { mode: "risk_based", rules: [] },
    effective_policy: { mode: "risk_based", rules: [] }, inheritance: "root", root_scope: null,
    policy_versions: [], capabilities: { can_view: true, can_edit_root: true, can_edit_local: true, can_save_rule: true },
    action_catalog: [], effective_rows: [], saved_rules: [], inherited_rules: [], rule_sources: [],
  };
  return [];
};

test.beforeEach(async ({ page }) => {
  page.on("pageerror", (error) => { throw error; });
  await page.route("**/config", (route) => route.fulfill({ json: {} }));
  await page.addInitScript(() => localStorage.setItem("manor_locale", "en"));
});

for (const surface of ["workspace", "embedded", "floating"]) {
  test(`${surface} follows growing replies and respects reading history`, async ({ page }) => {
    let releaseImage!: () => void;
    const imageReady = new Promise<void>((resolve) => { releaseImage = resolve; });
    await page.route("**/chat-scroll-layout.svg", async (route) => {
      await imageReady;
      await route.fulfill({ contentType: "image/svg+xml", body: '<svg xmlns="http://www.w3.org/2000/svg" width="400" height="300"><rect width="400" height="300" fill="gray"/></svg>' });
    });
    try {
      const history = Array.from({ length: 24 }, (_, i) => {
        const row = message(`history-${i}`, `History paragraph ${i + 1}.`, i % 2 ? "agent" : "user", i + 2);
        return { ...row, role: i % 2 ? "assistant" : "user", content: row.body };
      });
      await page.route("**/api/v1/**", (route) => {
        const url = new URL(route.request().url());
        if (url.pathname.endsWith("/messages/page")) return route.fulfill({ json: {
          items: url.searchParams.has("before") ? olderMessages.map((row) => ({
            ...row, role: row.author_kind === "agent" ? "assistant" : "user", content: row.body,
          })) : history,
          conversation_id: conversationId, has_more: !url.searchParams.has("before"), next_cursor: "older-page",
        } });
        if (url.pathname === "/api/v1/chat/conversations") return route.fulfill({ json: [{ id: conversationId, title: "History" }] });
        if (url.pathname.includes("/chat/conversations")) return route.fulfill({ json: { id: conversationId, title: "History" } });
        return route.fulfill({ json: supportingResponse(url.pathname) });
      });
      // Keep the transport open: scrolling must follow rendered deltas before
      // stream completion, through the real decoder, store and typewriter.
      await page.addInitScript(({ conversationId }) => {
        const original = window.fetch.bind(window);
        window.fetch = async (input, init) => {
          if (!String(input instanceof Request ? input.url : input).endsWith("/api/v1/chat/stream")) return original(input, init);
          const encoder = new TextEncoder();
          const clientId = (init?.body as FormData)?.get("client_message_id");
          return new Response(new ReadableStream({ start(controller) {
            const frame = (event: string, data: object) => controller.enqueue(encoder.encode(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`));
            (window as any).scrollStream = { frame, close: () => controller.close() };
            frame("stream_start", { conversation_id: conversationId, message_id: "growing-reply", user_message_id: "sent-question", client_message_id: clientId });
          } }), { headers: { "Content-Type": "text/event-stream", "X-Conversation-ID": conversationId } });
        };
      }, { conversationId });
      await page.goto(`/e2e/fixtures/workspace-app-pane.html?surface=${surface}`);
      if (surface === "floating") await page.getByRole("button", { name: "Chat with Manor AI", exact: true }).click();
      const lastHistory = page.getByText("History paragraph 24.", { exact: true }).first();
      await expect(lastHistory).toBeVisible();
      // Locate the actual scroll viewport independently of its wrapper structure.
      const viewport = await lastHistory.evaluateHandle((node) => {
        let parent = node.parentElement!;
        while (parent && getComputedStyle(parent).overflowY !== "auto") parent = parent.parentElement!;
        return parent;
      });
      const gap = () => viewport.evaluate((el) => el.scrollHeight - el.clientHeight - el.scrollTop);
      await expect.poll(gap).toBeLessThanOrEqual(2);
      expect(await viewport.evaluate((el) => el.scrollHeight - el.clientHeight)).toBeGreaterThan(300);

      if (surface !== "floating") {
        await viewport.evaluate((el) => el.scrollTo({ top: 0, behavior: "instant" }));
        const anchor = page.getByText("History paragraph 1.", { exact: true }).first();
        if (surface === "workspace") {
          // Scrolling up near the top reads the older page and keeps the reader's place.
          await expect(page.getByText("Earlier assistant answer", { exact: true }).first()).toBeAttached();
          await expect.poll(() => viewport.evaluate((el) => el.scrollTop)).toBeGreaterThan(0);
          await expect(anchor).toBeInViewport();
        } else {
          await page.evaluate(() => document.fonts.ready);
          await viewport.evaluate(async (el) => {
            await Promise.all(el.getAnimations({ subtree: true }).filter((animation) => animation.effect?.getTiming().iterations !== Infinity).map((animation) => animation.finished));
          });
          const anchorTop = await anchor.evaluate((el) => el.getBoundingClientRect().top);
          await page.getByRole("button", { name: "Load earlier messages", exact: true }).click();
          await expect(page.getByText("Earlier assistant answer", { exact: true }).first()).toBeVisible();
          await expect.poll(async () => Math.abs(await anchor.evaluate((el) => el.getBoundingClientRect().top) - anchorTop)).toBeLessThanOrEqual(2);
        }
      }
      await page.locator('[role="textbox"][contenteditable="true"]').fill("Show a long reply.");
      await page.getByRole("button", { name: "Send", exact: true }).click();
      await page.waitForFunction(() => Boolean((window as any).scrollStream));
      const append = (content: string) => page.evaluate((content) => (window as any).scrollStream.frame("text_delta", { content }), content);
      await append(Array.from({ length: 24 }, (_, i) => `Reply line ${i + 1}.`).join("\n\n"));
      await expect(page.getByText("Reply line 24.", { exact: true }).first()).toBeVisible();
      await expect.poll(gap).toBeLessThanOrEqual(2);

      await append("\n\n![Delayed layout](/chat-scroll-layout.svg)\n\nWaiting for image.");
      await expect(page.getByText("Waiting for image.", { exact: true }).first()).toBeVisible();
      await expect.poll(gap).toBeLessThanOrEqual(2);
      const beforeImage = await viewport.evaluate((el) => el.scrollHeight);
      releaseImage();
      await expect.poll(() => page.getByRole("img", { name: "Delayed layout", exact: true }).evaluate((el) => (el as HTMLImageElement).naturalHeight)).toBe(300);
      await expect.poll(() => viewport.evaluate((el) => el.scrollHeight)).toBeGreaterThan(beforeImage + 100);
      await expect.poll(gap).toBeLessThanOrEqual(2);

      // A viewport resize has no message/store event to trigger a scroll effect.
      await page.setViewportSize({ width: 1000, height: 640 });
      await expect.poll(gap).toBeLessThanOrEqual(2);
      const rect = await viewport.evaluate((el) => { const r = el.getBoundingClientRect(); return { x: r.x, y: r.y, width: r.width, height: r.height }; });
      await page.mouse.move(rect.x + rect.width / 2, rect.y + rect.height / 2);
      await page.mouse.wheel(0, -350);
      await expect.poll(gap).toBeGreaterThan(200);
      const readingTop = await viewport.evaluate((el) => el.scrollTop);
      await append("\n\nKeep reading history.");
      await expect(page.getByText("Keep reading history.", { exact: true }).first()).toBeAttached();
      await expect.poll(() => viewport.evaluate((el) => el.scrollTop)).toBeCloseTo(readingTop, 0);
      await page.mouse.wheel(0, 10000);
      await expect.poll(gap).toBeLessThanOrEqual(2);
      await append("\n\nFollowing resumed.");
      await expect(page.getByText("Following resumed.", { exact: true }).first()).toBeVisible();
      await expect.poll(gap).toBeLessThanOrEqual(2);

      await viewport.evaluate((el) => el.focus());
      await page.keyboard.press("Home");
      await expect.poll(() => viewport.evaluate((el) => el.scrollTop)).toBeLessThanOrEqual(2);
      await append("\n\nKeyboard reading stays put.");
      await expect(page.getByText("Keyboard reading stays put.", { exact: true }).first()).toBeAttached();
      await expect.poll(() => viewport.evaluate((el) => el.scrollTop)).toBeLessThanOrEqual(2);
      await page.keyboard.press("End");
      await expect.poll(gap).toBeLessThanOrEqual(2);

      if (surface === "floating") {
        await page.getByRole("button", { name: "Close", exact: true }).click();
        await append("\n\nGenerated while closed.");
        await page.getByRole("button", { name: "Chat with Manor AI", exact: true }).click();
        await expect(page.getByText("Generated while closed.", { exact: true }).first()).toBeVisible();
        await expect.poll(gap).toBeLessThanOrEqual(2);
      } else {
        await page.setViewportSize({ width: 1280, height: 800 });
        await page.getByRole("navigation", { name: "Conversation map" }).getByRole("button").first().click();
        await expect(page.getByText("Earlier user question", { exact: true }).first()).toBeVisible();
        // The first message can follow a date divider; navigation targets the message.
        await expect.poll(() => viewport.evaluate((el) => {
          const first = el.querySelector("[data-chat-scroll-marker-id], [data-chat-message-index]")!;
          return Math.abs(first.getBoundingClientRect().top - el.getBoundingClientRect().top);
        })).toBeLessThanOrEqual(16);
        // Navigation scrolls smoothly; read the position once it has come to rest.
        let navigationTop = -1;
        await expect.poll(async () => {
          const top = await viewport.evaluate((el) => el.scrollTop);
          const atRest = top === navigationTop;
          navigationTop = top;
          return atRest;
        }).toBe(true);
        await append("\n\nNavigation keeps its position.");
        await expect(page.getByText("Navigation keeps its position.", { exact: true }).first()).toBeAttached();
        await expect.poll(() => viewport.evaluate((el) => el.scrollTop)).toBeCloseTo(navigationTop, 0);
      }
      await viewport.dispose();
    } finally { releaseImage(); }
  });
}

test("sending preserves loaded history while the stream binds its conversation and refreshes", async ({ page }, testInfo) => {
  let admitStream!: () => void;
  let refreshHistory!: () => void;
  const streamReady = new Promise<void>((resolve) => { admitStream = resolve; });
  const refreshReady = new Promise<void>((resolve) => { refreshHistory = resolve; });
  let streamRequested = false;
  let latestHistoryReads = 0;
  let clientMessageId = "";
  let historyRefreshed = false;
  await page.route("**/api/v1/**", async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname.endsWith("/chat/messages/page")) {
      if (url.searchParams.has("before")) return route.fulfill({ json: {
        items: olderMessages, conversation_id: conversationId, has_more: false, next_cursor: null,
      } });
      latestHistoryReads += 1;
      if (latestHistoryReads > 1) {
        await refreshReady;
        historyRefreshed = true;
        return route.fulfill({ json: {
          items: [...recentMessages,
            { ...message("saved-user", "New user question", "user", 20), meta: { client_message_id: clientMessageId } },
            message("saved-reply", "New assistant answer", "agent", 21)],
          conversation_id: conversationId, has_more: true, next_cursor: "older-page",
        } });
      }
      return route.fulfill({ json: {
        items: recentMessages, conversation_id: conversationId, has_more: true, next_cursor: "older-page",
      } });
    }
    if (url.pathname === "/api/v1/chat/stream") {
      streamRequested = true;
      const body = route.request().postData() || "";
      clientMessageId = body.match(/name="client_message_id"\r\n\r\n([^\r\n]+)/)?.[1] || "";
      expect(body).toContain(conversationId);
      expect(clientMessageId).not.toBe("");
      await streamReady;
      return route.fulfill({
        contentType: "text/event-stream",
        headers: {
          "X-Conversation-ID": conversationId, "X-User-Message-ID": "saved-user",
          "X-Client-Message-ID": clientMessageId,
        },
        body: frame("stream_start", {
          conversation_id: conversationId, message_id: "saved-reply",
          user_message_id: "saved-user", client_message_id: clientMessageId,
        }) + frame("text_delta", { content: "New assistant answer" })
          + frame("stream_end", { conversation_id: conversationId, message_id: "saved-reply", persisted: true }),
      });
    }
    return route.fulfill({ json: supportingResponse(url.pathname) });
  });
  try {
    await page.goto("/e2e/fixtures/workspace-app-pane.html");
    await expect(page.getByText("Previous assistant answer", { exact: true })).toBeVisible();
    await page.getByRole("button", { name: "Load earlier messages", exact: true }).click();
    for (const id of historyIds) await expect(page.locator(`#workspace-chat-message-${id}`)).toHaveCount(1);
    // Watch every DOM update, including the short receipt/completion transition.
    await page.evaluate((ids) => {
      const missing: string[][] = [];
      const observer = new MutationObserver(() => {
        const absent = ids.filter((id) => !document.getElementById(`workspace-chat-message-${id}`));
        if (absent.length) missing.push(absent);
      });
      observer.observe(document.getElementById("root")!, { childList: true, subtree: true });
      (window as any).historyStability = { missing, observer };
    }, historyIds);
    await page.locator('[role="textbox"][contenteditable="true"]').fill("New user question");
    await page.getByRole("button", { name: "Send", exact: true }).click();
    await expect.poll(() => streamRequested).toBe(true);
    await expect(page.getByRole("button", { name: "Stop generating", exact: true })).toBeVisible();
    for (const id of historyIds) await expect(page.locator(`#workspace-chat-message-${id}`)).toHaveCount(1);
    await page.screenshot({ path: testInfo.outputPath("workspace-history-during-send.png") });
    admitStream();
    await expect(page.locator("#workspace-chat-message-saved-reply").getByText("New assistant answer", { exact: true })).toBeVisible();
    await expect.poll(() => latestHistoryReads).toBe(2);
    for (const id of historyIds) await expect(page.locator(`#workspace-chat-message-${id}`)).toHaveCount(1);
    refreshHistory();
    await expect.poll(() => historyRefreshed).toBe(true);
    await expect(page.locator("#workspace-chat-message-saved-user")).toHaveCount(1);
    await expect(page.locator("#workspace-chat-message-saved-reply")).toHaveCount(1);
    for (const id of historyIds) await expect(page.locator(`#workspace-chat-message-${id}`)).toHaveCount(1);
    expect(await page.evaluate(() => {
      const watch = (window as any).historyStability;
      watch.observer.disconnect();
      return watch.missing;
    })).toEqual([]);
  } finally { admitStream(); refreshHistory(); }
});

for (const hasMainConversation of [true, false]) {
  test(`sending with a pinned Plan card preserves replies after reload (${hasMainConversation ? "existing" : "new"} main conversation)`, async ({ page }) => {
    const pinned = {
      ...message("pinned-plan-card", "An older Plan needs approval", "agent", 0),
      conversation_id: "plan-conversation",
      message_kind: "hitl_request",
      pending_action: { kind: "approve_plan", plan_id: "older-plan", options: ["approve", "reject"] },
    };
    const requestText = "Create a task: Prepare the release";
    const replyText = "Created Prepare the release. Status: Pending.";
    let submittedConversationId: string | undefined;
    let savedMessages: ReturnType<typeof message>[] = [];
    let clientMessageId = "";
    await page.route("**/api/v1/**", async (route) => {
      const path = new URL(route.request().url()).pathname;
      if (path.endsWith("/chat/messages/page")) return route.fulfill({ json: {
        items: [pinned, ...(hasMainConversation ? recentMessages : []), ...savedMessages],
        conversation_id: hasMainConversation || savedMessages.length ? conversationId : null,
        has_more: false, next_cursor: null,
      } });
      if (path === "/api/v1/chat/stream") {
        const body = route.request().postData() || "";
        submittedConversationId = body.match(/name="conversation_id"\r\n\r\n([^\r\n]+)/)?.[1] || "";
        clientMessageId = body.match(/name="client_message_id"\r\n\r\n([^\r\n]+)/)?.[1] || "";
        const target = submittedConversationId || conversationId;
        if (target === conversationId) savedMessages = [
          { ...message("saved-user", requestText, "user", 20), meta: { client_message_id: clientMessageId } },
          message("saved-reply", replyText, "agent", 21),
        ];
        return route.fulfill({
          contentType: "text/event-stream",
          headers: { "X-Conversation-ID": target, "X-User-Message-ID": "saved-user", "X-Client-Message-ID": clientMessageId },
          body: frame("stream_start", {
            conversation_id: target, message_id: "saved-reply",
            user_message_id: "saved-user", client_message_id: clientMessageId,
          }) + frame("text_delta", { content: replyText })
            + frame("stream_end", { conversation_id: target, message_id: "saved-reply", persisted: true }),
        });
      }
      return route.fulfill({ json: supportingResponse(path) });
    });
    await page.goto("/e2e/fixtures/workspace-app-pane.html");
    await expect(page.locator("#workspace-chat-message-pinned-plan-card")).toBeVisible();
    await page.locator('[role="textbox"][contenteditable="true"]').fill(requestText);
    await page.getByRole("button", { name: "Send", exact: true }).click();
    await expect.poll(() => submittedConversationId).toBe(hasMainConversation ? conversationId : "");
    await expect(page.locator("#workspace-chat-message-saved-reply")).toContainText(replyText);
    await page.reload();
    await expect(page.locator("#workspace-chat-message-saved-user")).toContainText(requestText);
    await expect(page.locator("#workspace-chat-message-saved-reply")).toContainText(replyText);
    await expect(page.locator("#workspace-chat-message-pinned-plan-card")).toBeVisible();
  });
}

for (const surface of ["workspace", "embedded"]) {
  test(`${surface} switches scope without accepting a late history page`, async ({ page }) => {
    let loadNextWorkspace!: () => void;
    const nextReady = new Promise<void>((resolve) => { loadNextWorkspace = resolve; });
    let loadOlderPage!: () => void;
    const olderReady = new Promise<void>((resolve) => { loadOlderPage = resolve; });
    let olderRequested = false;
    let nextRequested = false;
    await page.route("**/api/v1/**", async (route) => {
      const url = new URL(route.request().url());
      const path = url.pathname;
      const asChat = (rows: typeof recentMessages) => rows.map((row) => ({ ...row, content: row.body, role: row.author_kind === "agent" ? "assistant" : "user" }));
      if (path.endsWith("/messages/page") && (path.includes("app-fixture") || path.includes(conversationId))) {
        if (url.searchParams.has("before")) {
          olderRequested = true;
          await olderReady;
          return route.fulfill({ json: { items: asChat(olderMessages), conversation_id: conversationId, has_more: false, next_cursor: null } });
        }
        return route.fulfill({ json: {
          items: asChat(recentMessages), conversation_id: conversationId, has_more: true, next_cursor: "old-cursor",
        } });
      }
      if (path.endsWith("/messages/page") && (path.includes("second-fixture") || path.includes("other-conversation"))) {
        nextRequested = true;
        await nextReady;
        return route.fulfill({ json: {
          items: asChat(Array.from({ length: 24 }, (_, i) => ({
            ...message(`second-${i}`, i === 23 ? "Other Workspace answer" : `Other conversation ${i}`, "agent", i + 30),
            conversation_id: "other-conversation",
          }))),
          conversation_id: "other-conversation", has_more: false, next_cursor: null,
        } });
      }
      return route.fulfill({ json: supportingResponse(path) });
    });
    try {
      await page.goto(`/e2e/fixtures/workspace-app-pane.html?surface=${surface}`);
      await expect(page.getByText("Previous assistant answer", { exact: true })).toBeVisible();
      await page.getByRole("button", { name: "Load earlier messages", exact: true }).click();
      await expect.poll(() => olderRequested).toBe(true);
      await page.getByRole("button", { name: surface === "workspace" ? "Switch Workspace" : "Switch conversation", exact: true }).click();
      await expect.poll(() => nextRequested).toBe(true);
      await expect(page.getByText("Previous user question", { exact: true })).toHaveCount(0);
      await expect(page.getByText("Previous assistant answer", { exact: true })).toHaveCount(0);
      loadNextWorkspace();
      await expect(page.getByText("Other Workspace answer", { exact: true }).first()).toBeVisible();
      const olderResponse = page.waitForResponse((response) => response.url().includes("before="));
      loadOlderPage();
      await (await olderResponse).finished();
      await page.setViewportSize({ width: 1280, height: 600 });
      // Allow the response's React commit and its resize observer to run.
      await page.evaluate(async () => { await new Promise(requestAnimationFrame); await new Promise(requestAnimationFrame); });
      await expect(page.getByText("Earlier assistant answer", { exact: true })).toHaveCount(0);
      await expect(page.getByText("Previous assistant answer", { exact: true })).toHaveCount(0);
      await expect.poll(() => page.locator(".embedded-chat-body").evaluate((el) => el.scrollHeight - el.clientHeight - el.scrollTop)).toBeLessThanOrEqual(2);
    } finally { loadNextWorkspace(); loadOlderPage(); }
  });
}
