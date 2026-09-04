import { expect, test, type Page } from "@playwright/test";

const DRAFT_ID = "draft-e2e";
const WORKSPACE_ID = "workspace-e2e";
const CONVERSATION_ID = "conversation-e2e";

function draft(
  status: "active" | "ready" | "finalized" | "abandoned" = "ready",
  options: {
    suggestedBlueprint?: boolean;
    appliedBlueprint?: boolean;
    withPersonalization?: boolean;
    withChannels?: boolean;
  } = {},
) {
  return {
    id: DRAFT_ID,
    owner_user_id: "user-e2e",
    status,
    ready: status === "ready" || status === "finalized",
    missing: status === "active" ? ["creation_preferences"] : [],
    messages: [],
    fields: {
      name: "Focused product collaboration",
      kind: "team",
      initial_brief: "Plan and deliver product work in Chat.",
      operating_context: "Plan work, assign ownership, and keep weekly progress visible in Chat.",
      primary_work: options.appliedBlueprint
        ? "Run the applied weekly product delivery playbook."
        : "Turn product requirements into accountable weekly delivery.",
      services: [
        { service_key: "product_planning", name: "Product planning", autonomy_level: "supervised", owner_role: "Product lead", description: "Shape the weekly plan." },
        { service_key: "delivery_tracking", name: "Delivery tracking", autonomy_level: "assisted", owner_role: "Delivery lead", description: "Keep commitments visible." },
      ],
      agent_mappings: [
        { service_key: "product_planning", agent_id: "agent-product", agent_name: "Product Strategist", strategy: "existing", rationale: "Best fit for roadmap decisions." },
        {
          service_key: "delivery_tracking",
          strategy: "create_custom",
          rationale: "Owns weekly follow-through.",
          create_agent_draft: {
            agent_name: "Delivery Coordinator",
            agent_description: "Keeps weekly commitments moving.",
            system_prompt: "Coordinate delivery and surface blocked commitments.",
            tool_bindings: ["task.create", "task.update"],
            business_capabilities: ["delivery coordination"],
            skill_bindings: ["weekly-planning"],
            mcp_bindings: ["linear"],
            missing_skill_specs: [{ slug: "risk-review" }],
          },
        },
      ],
      goals: [{ goal_key: "weekly_delivery", title: "Weekly delivery", target: "90%", cadence: "weekly", metric: "commitment_rate" }],
      staff_assignments: [{ staff_id: "01K3E2ESTAFFMEMBER00000001", staff_name: "Ada Product", role: "approver", service_key: "product_planning", rationale: "Owns final roadmap approval." }],
      knowledge_attachments: [
        {
          title: "Product brief",
          purpose: "Planning source",
          mode: "link_existing",
          linked_service_keys: ["product_planning"],
          generate_starter_doc: false,
          approved: true,
        },
        {
          title: "Retired launch notes",
          purpose: "Outdated source",
          mode: "create_new",
          approved: false,
        },
      ],
      channel_config: {
        channels: [{ channel_type: "chat", role: "internal", purpose: "Planning and approvals", login_required: true, linked_service_key: "product_planning" }],
      },
      budget_policy: { monthly_budget_credits: 4000, auto_pause_on_budget: true, notes: "Local account remains unlimited." },
      evaluation: {
        enabled: true,
        cadence: "weekly",
        target_score: 90,
        warning_score: 75,
        scorecard: [{ metric_key: "commitment_rate", weight: 0.6, goal_key: "weekly_delivery" }],
      },
      rules: [{ name: "Approval before publish", rule_type: "approval", severity: "high", scope: "external", action_patterns: ["publish_*"], description: "Require owner approval." }],
      automations: [{ name: "Weekly planning reminder", schedule_kind: "cron", cron_expr: "0 9 * * 1", timezone: "America/Los_Angeles", service_key: "product_planning" }],
      notes: "Keep final decisions in Chat.",
      _draft_schema_version: 2,
      _creation_preferences: {
        goal_confirmed: status !== "active",
        autonomy_confirmed: status !== "active",
      },
      ...(options.withChannels ? {
        _blueprint_channel_config_ids: {} as Record<string, string>,
        _blueprint_channel_requirements: [{
          requirement_key: "channel:0:telegram", label: "Telegram alerts", required: true,
          ready: false, resource_id: null as string | null, reason: "Choose a Telegram account",
          resource_options: [{ id: "first", label: "Primary bot" }, { id: "second", label: "Backup bot" }],
        }],
      } : {}),
      ...(options.withPersonalization ? {
        _blueprint_variable_declarations: [{
          key: "brand_name",
          label: "Brand name",
          purpose: "Used in generated Workspace content.",
          required: true,
          default: "Stored brand",
        }],
        blueprint_personalization: { brand_name: "Stored brand" },
      } : {}),
    },
    suggested_blueprint: options.suggestedBlueprint && !options.appliedBlueprint
      ? {
          id: "blueprint-product-delivery",
          title: "Weekly product delivery",
          summary: "A focused planning and delivery template.",
          tags: ["product"],
          install_count: 8,
        }
      : null,
    applied_blueprint_id: options.appliedBlueprint ? "blueprint-product-delivery" : null,
    finalized_workspace_id: status === "finalized" ? WORKSPACE_ID : null,
    created_at: "2026-08-21T12:00:00Z",
    updated_at: "2026-08-21T12:00:00Z",
  };
}

function messages(streaming: boolean, withPersonalization = false) {
  return [
    {
      id: "message-user",
      conversation_id: CONVERSATION_ID,
      role: "user",
      content: "Create a focused product collaboration workspace.",
      created_at: "2026-08-21T12:00:00Z",
    },
    {
      id: "message-assistant-initial",
      conversation_id: CONVERSATION_ID,
      role: "assistant",
      content: "I started the Workspace draft.",
      meta: { stream_status: "completed" },
      tool_calls: [
        {
          name: "manor",
          status: "success",
          result: '{"artifact_kind":"workspace_draft","draft_id":"draft-e2e","status":"ready"}',
          raw_result: JSON.stringify({
            artifact_kind: "workspace_draft",
            draft_id: DRAFT_ID,
            title: "Initial Workspace draft",
            status: "ready",
            ready: true,
            missing: [],
            fields: draft("ready", { withPersonalization }).fields,
          }),
        },
      ],
      created_at: "2026-08-21T12:00:01Z",
    },
    {
      id: "message-user-refinement",
      conversation_id: CONVERSATION_ID,
      role: "user",
      content: "Add delivery tracking and keep the local account unlimited.",
      created_at: "2026-08-21T12:00:02Z",
    },
    {
      id: "message-assistant",
      conversation_id: CONVERSATION_ID,
      role: "assistant",
      content: "The Workspace draft is ready. Keep chatting to refine it.",
      meta: streaming ? { stream_status: "running" } : { stream_status: "completed" },
      tool_calls: [
        {
          name: "manor",
          status: "success",
          result: '{"artifact_kind":"workspace_draft","draft_id":"draft-e2e","status":"ready","fields":',
          raw_result: JSON.stringify({
            artifact_kind: "workspace_draft",
            draft_id: DRAFT_ID,
            title: "Focused product collaboration",
            status: "ready",
            ready: true,
            missing: [],
            fields: draft("ready", { withPersonalization }).fields,
            assistant_reply: "Keep chatting to refine this Workspace.",
          }),
        },
      ],
      created_at: "2026-08-21T12:00:03Z",
    },
  ];
}

async function mockApp(
  page: Page,
  options: {
    streaming?: boolean;
    withBlueprint?: boolean;
    delayBlueprint?: boolean;
    delayFinalize?: boolean;
    delaySettledDraftRefresh?: boolean;
    withPersonalization?: boolean;
    withChannels?: boolean;
    finalizeFailure?: boolean;
    status?: "ready" | "abandoned";
  } = {},
) {
  let finalizeCalls = 0;
  let applyCalls = 0;
  let fuzzyDocumentLookupCalls = 0;
  let personalizationUpdates: Record<string, unknown>[] = [];
  let currentDraft = draft(options.withChannels ? "active" : options.status || "ready", {
    suggestedBlueprint: options.withBlueprint,
    withPersonalization: options.withPersonalization,
    withChannels: options.withChannels,
  });
  let currentMessages = messages(
    Boolean(options.streaming),
    Boolean(options.withPersonalization),
  );
  let releaseBlueprint = () => undefined;
  const blueprintRelease = new Promise<void>((resolve) => {
    releaseBlueprint = resolve;
  });
  let markBlueprintStarted = () => undefined;
  const blueprintStarted = new Promise<void>((resolve) => {
    markBlueprintStarted = resolve;
  });
  let releaseFinalize = () => undefined;
  const finalizeRelease = new Promise<void>((resolve) => {
    releaseFinalize = resolve;
  });
  let markFinalizeStarted = () => undefined;
  const finalizeStarted = new Promise<void>((resolve) => {
    markFinalizeStarted = resolve;
  });
  let holdSettledDraftRefresh = false;
  let releaseSettledDraftRefresh = () => undefined;
  const settledDraftRefreshRelease = new Promise<void>((resolve) => {
    releaseSettledDraftRefresh = resolve;
  });
  let markSettledDraftRefreshStarted = () => undefined;
  const settledDraftRefreshStarted = new Promise<void>((resolve) => {
    markSettledDraftRefreshStarted = resolve;
  });
  const pageErrors: Error[] = [];
  page.on("pageerror", (error) => pageErrors.push(error));

  await page.route("**/config", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        deployment_mode: "oss",
        environment: "e2e",
        email_enabled: false,
        fs_enabled: false,
        flows_available: false,
        flows_released: false,
        ai_credits_unlimited: true,
        support_tickets_enabled: false,
      }),
    });
  });

  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const { pathname } = url;

    if (pathname === "/api/v1/auth/me") {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          id: "user-e2e",
          username: "workspace-draft-e2e",
          display_name: "Workspace Draft E2E",
          email: "workspace-draft-e2e@example.test",
          entity_id: "entity-e2e",
          role: "owner",
          locale: "en",
        }),
      });
      return;
    }

    if (pathname === `/api/v1/chat/conversations/${CONVERSATION_ID}/messages/page`) {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          items: currentMessages,
          has_more: false,
          next_cursor: null,
        }),
      });
      return;
    }

    if (pathname === `/api/v1/workspace-drafts/${DRAFT_ID}`) {
      if (holdSettledDraftRefresh) {
        markSettledDraftRefreshStarted();
        await settledDraftRefreshRelease;
      }
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(currentDraft),
      });
      return;
    }

    if (pathname === `/api/v1/workspace-drafts/${DRAFT_ID}/apply-blueprint`) {
      expect(request.method()).toBe("POST");
      applyCalls += 1;
      markBlueprintStarted();
      if (options.delayBlueprint) await blueprintRelease;
      currentDraft = draft("active", { appliedBlueprint: true });
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(currentDraft),
      });
      return;
    }

    if (pathname === `/api/v1/workspace-drafts/${DRAFT_ID}/fields`) {
      expect(request.method()).toBe("PATCH");
      const body = request.postDataJSON() as {
        blueprint_personalization?: Record<string, unknown>;
        blueprint_channel_config_ids?: Record<string, string>;
      };
      if (body.blueprint_personalization) {
        personalizationUpdates.push(body.blueprint_personalization);
      }
      currentDraft = {
        ...currentDraft,
        fields: { ...currentDraft.fields, ...body },
        updated_at: "2026-08-21T12:00:04Z",
      };
      if (body.blueprint_channel_config_ids) {
        const selected = body.blueprint_channel_config_ids["channel:0:telegram"];
        expect(selected).toBe("second");
        currentDraft = {
          ...currentDraft, status: "ready", ready: true, missing: [],
          fields: {
            ...currentDraft.fields,
            _blueprint_channel_config_ids: body.blueprint_channel_config_ids,
            _blueprint_channel_requirements: currentDraft.fields._blueprint_channel_requirements?.map(
              (item) => ({ ...item, ready: true, resource_id: selected }),
            ),
          },
        };
      }
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(currentDraft),
      });
      return;
    }

    if (pathname === `/api/v1/workspace-drafts/${DRAFT_ID}/finalize`) {
      expect(request.method()).toBe("POST");
      finalizeCalls += 1;
      markFinalizeStarted();
      if (options.delayFinalize) await finalizeRelease;
      if (options.finalizeFailure) {
        currentDraft = draft("active", {
          appliedBlueprint: Boolean(currentDraft.applied_blueprint_id),
        });
        await route.fulfill({
          status: 400,
          contentType: "application/json",
          body: JSON.stringify({
            detail: "Draft not ready -- still missing: creation_preferences",
          }),
        });
        return;
      }
      currentDraft = draft("finalized", { appliedBlueprint: Boolean(currentDraft.applied_blueprint_id) });
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ workspace_id: WORKSPACE_ID, draft: currentDraft }),
      });
      return;
    }

    if (pathname === "/api/v1/documents") {
      const search = url.searchParams.get("search") || "";
      if (search.toLowerCase().includes("focused product collaboration")) {
        fuzzyDocumentLookupCalls += 1;
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({
            items: [{
              id: "unrelated-document-e2e",
              name: "Focused product collaboration.md",
              mime_type: "text/markdown",
              file_type: "markdown",
            }],
            total: 1,
          }),
        });
        return;
      }
    }

    if (pathname === "/api/v1/chat/conversations") {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify([{
          id: CONVERSATION_ID,
          entity_id: "entity-e2e",
          user_id: "user-e2e",
          title: "Workspace planning",
          channel: "internal",
          status: "active",
        }]),
      });
      return;
    }

    if (
      pathname === "/api/v1/workspaces" ||
      pathname === "/api/v1/agents" ||
      pathname === "/api/v1/chat/flow-entrypoints" ||
      pathname === "/api/v1/auth/users" ||
      pathname === "/api/v1/auth/users/directory" ||
      pathname === "/api/v1/people/directory" ||
      pathname === "/api/v1/staff" ||
      pathname === "/api/v1/staff/roles"
    ) {
      await route.fulfill({ status: 200, contentType: "application/json", body: "[]" });
      return;
    }

    if (pathname === "/api/v1/notifications") {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ items: [], total: 0, unread_count: 0 }),
      });
      return;
    }

    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ items: [], total: 0 }),
    });
  });

  await page.addInitScript(() => {
    window.localStorage.setItem("manor_token", "e2e-token");
    window.localStorage.setItem("manor_locale", "en");
    window.localStorage.setItem("manor_tour_completed", "true");
    window.localStorage.setItem("manor_consent_v1", JSON.stringify({
      v: 1,
      ts: "2026-08-21T00:00:00.000Z",
      locale: "en",
      categories: { functional: false, analytics: false, marketing: false },
    }));
  });

  return {
    finalizeCalls: () => finalizeCalls,
    applyCalls: () => applyCalls,
    fuzzyDocumentLookupCalls: () => fuzzyDocumentLookupCalls,
    personalizationUpdates: () => personalizationUpdates,
    blueprintStarted,
    releaseBlueprint,
    finalizeStarted,
    releaseFinalize,
    settledDraftRefreshStarted,
    releaseSettledDraftRefresh,
    finishStream: async (primaryWork: string) => {
      currentDraft = {
        ...currentDraft,
        fields: { ...currentDraft.fields, primary_work: primaryWork },
      };
      currentMessages = messages(false, Boolean(options.withPersonalization));
      holdSettledDraftRefresh = Boolean(options.delaySettledDraftRefresh);
      await page.evaluate(({ conversationId, messageId }) => {
        window.dispatchEvent(new CustomEvent("manor:chat-stream-snapshot", {
          detail: {
            conversation_id: conversationId,
            message_id: messageId,
            seq: 1,
            status: "completed",
            content: "The Workspace draft is ready. Keep chatting to refine it.",
          },
        }));
      }, {
        conversationId: CONVERSATION_ID,
        messageId: "message-assistant",
      });
    },
    pageErrors,
  };
}

const OPENING_QUESTION = "A workspace keeps the work, context, agents, and rules in one place. What should this workspace run?";

async function mockDraftOpening(page: Page, failOpening = false) {
  const app = await mockApp(page);
  const requests: { draft_id: string; initial_brief?: string }[] = [];
  let savedDraft: Record<string, unknown> | null = null;
  await page.route("**/api/v1/workspace-drafts/**", async (route) => {
    const pathname = new URL(route.request().url()).pathname;
    if (pathname === "/api/v1/workspace-drafts/stream") {
      const body = route.request().postDataJSON();
      requests.push(body);
      if (failOpening) {
        await route.fulfill({
          status: 201,
          contentType: "text/event-stream",
          body: `event: start\ndata: ${JSON.stringify({ draft_id: body.draft_id, mode: "create" })}\n\nevent: error\ndata: ${JSON.stringify({ message: "Please retry the opening." })}\n\n`,
        });
        return;
      }
      const reply = body.initial_brief ? "Who will use this workspace?" : OPENING_QUESTION;
      savedDraft = {
        ...draft("active"),
        id: body.draft_id,
        fields: body.initial_brief ? { initial_brief: body.initial_brief } : {},
        missing: ["name", "kind", "operating_context", "primary_work"],
        messages: [
          ...(body.initial_brief ? [{ role: "user", content: body.initial_brief }] : []),
          { role: "assistant", content: reply },
        ],
      };
      await route.fulfill({
        status: 201,
        contentType: "text/event-stream",
        body: [
          `event: start\ndata: ${JSON.stringify({ draft_id: body.draft_id, mode: "create" })}\n\n`,
          `event: token\ndata: ${JSON.stringify({ content: reply })}\n\n`,
          `event: done\ndata: ${JSON.stringify({ reply, draft: savedDraft })}\n\n`,
        ].join(""),
      });
      return;
    }
    if (savedDraft && pathname === `/api/v1/workspace-drafts/${savedDraft.id}/messages/stream`) {
      const reply = "What should your team deliver first?";
      savedDraft = {
        ...savedDraft,
        messages: [
          ...(savedDraft.messages as object[]),
          { role: "user", content: route.request().postDataJSON().message },
          { role: "assistant", content: reply },
        ],
      };
      await route.fulfill({
        status: 200,
        contentType: "text/event-stream",
        body: `event: token\ndata: ${JSON.stringify({ content: reply })}\n\nevent: done\ndata: ${JSON.stringify({ reply, draft: savedDraft })}\n\n`,
      });
      return;
    }
    if (savedDraft && pathname === `/api/v1/workspace-drafts/${savedDraft.id}`) {
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(savedDraft) });
      return;
    }
    await route.fallback();
  });
  return { ...app, openingRequests: requests, allowOpening: () => { failOpening = false; } };
}

for (const width of [1440, 390]) {
  test(`blank creation opens with an assistant question and survives refresh at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    const app = await mockDraftOpening(page);
    await page.goto("/workspaces/new");

    const question = page.locator(".draft-msg-row.assistant");
    await expect(question).toHaveText(OPENING_QUESTION);
    await expect(page.locator(".draft-msg-row.user")).toHaveCount(0);
    await expect(page.locator(".draft-starters button")).toHaveCount(3);
    await expect(page).toHaveURL(/\/workspaces\/new\?draft=[0-9A-Z]{26}$/);
    expect(app.openingRequests).toHaveLength(1);
    expect(app.openingRequests[0].initial_brief).toBeUndefined();
    const savedUrl = page.url();

    await page.reload();
    await expect(question).toHaveText(OPENING_QUESTION);
    expect(page.url()).toBe(savedUrl);
    expect(app.openingRequests).toHaveLength(1);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    expect(app.pageErrors).toEqual([]);
  });
}

for (const recovery of ["retry", "reload"]) {
  test(`blank creation recovers its opening on the same draft via ${recovery}`, async ({ page }) => {
    const app = await mockDraftOpening(page, true);
    await page.goto("/workspaces/new");
    const retry = page.getByRole("button", { name: "Retry", exact: true });
    await expect(retry).toBeVisible();
    expect(app.openingRequests).toHaveLength(1);
    const firstDraftId = app.openingRequests[0].draft_id;

    app.allowOpening();
    if (recovery === "reload") await page.reload();
    else await retry.click();
    await expect(page.locator(".draft-msg-row.assistant")).toHaveText(OPENING_QUESTION);
    expect(app.openingRequests).toHaveLength(2);
    expect(app.openingRequests[1].draft_id).toBe(firstDraftId);
    await expect(page.locator(".draft-msg-row.user")).toHaveCount(0);
    expect(app.pageErrors).toEqual([]);
  });
}

test("blank creation accepts the user's answer after the opening question", async ({ page }) => {
  const app = await mockDraftOpening(page);
  await page.goto("/workspaces/new");
  await expect(page.locator(".draft-msg-row.assistant")).toHaveText(OPENING_QUESTION);

  const answer = "Help our design team plan the weekly delivery.";
  const composer = page.locator(".draft-chat").getByRole("textbox");
  await composer.fill(answer);
  await composer.press("Enter");
  await expect(page.locator(".draft-msg-row.user")).toHaveText(answer);
  await expect(page.locator(".draft-msg-row.assistant").last()).toHaveText("What should your team deliver first?");
  expect(app.openingRequests).toHaveLength(1);
  expect(app.pageErrors).toEqual([]);
});

test("creation with an initial brief keeps the user context", async ({ page }) => {
  const app = await mockDraftOpening(page);
  const brief = "A workspace for a tiny design team";
  await page.goto(`/workspaces/new?brief=${encodeURIComponent(brief)}`);
  await expect(page.locator(".draft-msg-row.assistant")).toHaveText("Who will use this workspace?");
  await expect(page.locator(".draft-msg-row.user")).toHaveText(brief);
  expect(app.openingRequests).toHaveLength(1);
  expect(app.openingRequests[0].initial_brief).toBe(brief);
  expect(app.pageErrors).toEqual([]);
});

test("ordinary Chat keeps Workspace creation inline and restores the saved draft panel", async ({ page }) => {
  const state = await mockApp(page);
  await page.goto("/chat");

  const artifactCard = page.locator("button.chat-artifact-summary-open", {
    hasText: "Focused product collaboration",
  });
  await expect(page.locator("button.chat-artifact-summary-open")).toHaveCount(1);
  await expect(page.getByText("Initial Workspace draft", { exact: true })).toHaveCount(0);
  await expect(artifactCard).toBeVisible();
  await artifactCard.click();

  const panel = page.getByRole("region", { name: "Draft summary" });
  await expect(panel).toBeVisible();
  await expect(page.getByRole("button", { name: "Edit" })).toHaveCount(0);
  expect(state.fuzzyDocumentLookupCalls()).toBe(0);
  await expect(panel.getByText("Product planning", { exact: true })).toBeVisible();
  await expect(panel.getByText("Delivery tracking", { exact: true })).toBeVisible();
  await expect(panel.getByText("Shape the weekly plan.", { exact: false })).toBeVisible();
  await expect(panel.getByText("Best fit for roadmap decisions.", { exact: false })).toBeVisible();
  await expect(panel.getByText("Delivery Tracking → Delivery Coordinator", { exact: true })).toBeVisible();
  await expect(panel.getByText("Coordinate delivery and surface blocked commitments.", { exact: true })).toBeVisible();
  await expect(panel.getByText("task.create · task.update", { exact: true })).toBeVisible();
  await expect(panel.getByText("Ada Product", { exact: true })).toBeVisible();
  await expect(panel.getByText("Owns final roadmap approval.", { exact: false })).toBeVisible();
  await expect(panel.getByText("Knowledge sources", { exact: true })).toBeVisible();
  await expect(panel.getByText("Product brief", { exact: true })).toBeVisible();
  await expect(panel.getByText("Included", { exact: true })).toBeVisible();
  await expect(panel.getByText("Link Existing", { exact: true })).toBeVisible();
  await expect(panel.getByText("Retired launch notes", { exact: true })).toBeVisible();
  await expect(panel.getByText("Excluded", { exact: true })).toBeVisible();
  await expect(panel.getByText("Login required", { exact: false })).toBeVisible();
  await expect(panel.getByText("Commitment Rate", { exact: true })).toBeVisible();
  await expect(panel.getByText("target: 90", { exact: false })).toBeVisible();
  await expect(panel.getByText("warning: 75", { exact: false })).toBeVisible();
  await expect(panel.getByText("0 9 * * 1", { exact: false })).toBeVisible();
  await expect(panel.getByText("4,000 credits", { exact: true })).toBeVisible();
  await expect(panel.getByText("Auto-pause on", { exact: false })).toBeVisible();
  await expect(panel.getByText("No monthly credit cap", { exact: true })).toHaveCount(0);
  await expect(panel.getByText("Auto-pause off", { exact: true })).toHaveCount(0);
  await expect(page).toHaveURL(/\/chat$/);

  await page.getByRole("button", { name: "Close artifact" }).click();
  await expect(panel).toBeHidden();
  await artifactCard.click();
  await expect(panel).toBeVisible();

  await page.setViewportSize({ width: 390, height: 844 });
  await expect(panel.getByRole("progressbar")).toBeVisible();
  expect(await panel.evaluate((element) => element.scrollWidth <= element.clientWidth)).toBe(true);

  await panel.getByRole("button", { name: "Create Workspace" }).click();
  await expect.poll(state.finalizeCalls).toBe(1);
  await expect(panel.getByRole("button", { name: "Open workspace", exact: true })).toBeVisible();
  await expect(page).toHaveURL(/\/chat$/);
  expect(state.pageErrors).toEqual([]);
});

for (const width of [1280, 390]) {
  test(`Draft channel selection persists and unblocks creation at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    await mockApp(page, { withChannels: true });
    await page.goto("/chat");
    const panel = page.getByRole("region", { name: "Draft summary" });
    await expect(panel.getByRole("button", { name: "Keep chatting until ready", exact: true })).toBeDisabled();
    const selector = panel.getByRole("button", { name: "Telegram alerts", exact: true });
    await selector.click();
    await page.getByRole("option", { name: "Backup bot", exact: true }).click();
    await expect(selector).toContainText("Backup bot");
    await expect(panel.getByRole("button", { name: "Create Workspace", exact: true })).toBeEnabled();
    await page.reload();
    await expect(selector).toContainText("Backup bot");
  });
}

test("a ready draft finalizes directly without a creation-options dialog", async ({ page }) => {
  const state = await mockApp(page);
  await page.goto("/chat");

  await page.locator("button.chat-artifact-summary-open", {
    hasText: "Focused product collaboration",
  }).click();
  const panel = page.getByRole("region", { name: "Draft summary" });
  await panel.getByRole("button", { name: "Create Workspace" }).click();

  await expect(page.getByRole("dialog")).toHaveCount(0);
  await expect.poll(state.finalizeCalls).toBe(1);
  await expect(panel.getByRole("button", { name: "Open workspace", exact: true })).toBeVisible();
  expect(state.pageErrors).toEqual([]);
});

test("a rejected finalize refreshes the persisted not-ready draft", async ({ page }) => {
  const state = await mockApp(page, { finalizeFailure: true });
  await page.goto("/chat");

  await page.locator("button.chat-artifact-summary-open", {
    hasText: "Focused product collaboration",
  }).click();
  const panel = page.getByRole("region", { name: "Draft summary" });
  await panel.getByRole("button", { name: "Create Workspace" }).click();

  await expect.poll(state.finalizeCalls).toBe(1);
  await expect(
    panel.getByRole("button", { name: "Keep chatting until ready" }),
  ).toBeDisabled();
  await expect(panel.getByText("Creation Preferences", { exact: true })).toBeVisible();
  await expect(panel.getByRole("button", { name: "Open workspace", exact: true })).toHaveCount(0);
  expect(state.pageErrors).toEqual([]);
});

test("Create Workspace stays disabled while Chat is updating the draft", async ({ page }) => {
  const state = await mockApp(page, { streaming: true });
  await page.goto("/chat");

  await page.locator("button.chat-artifact-summary-open", {
    hasText: "Focused product collaboration",
  }).click();
  const create = page
    .getByLabel("Output panel", { exact: true })
    .getByRole("button", { name: "Create Workspace" });
  await expect(create).toBeVisible();
  await expect(create).toBeDisabled();
  await expect(create).toHaveAttribute("aria-busy", "true");
  expect(state.finalizeCalls()).toBe(0);
  expect(state.pageErrors).toEqual([]);
});

test("the Draft panel stays locked until persisted fields refresh when a Chat update finishes", async ({ page }) => {
  const state = await mockApp(page, { streaming: true, delaySettledDraftRefresh: true });
  await page.goto("/chat");

  await page.locator("button.chat-artifact-summary-open", {
    hasText: "Focused product collaboration",
  }).click();
  const panel = page.getByRole("region", { name: "Draft summary" });
  const create = panel.getByRole("button", { name: "Create Workspace" });
  const updatedPrimaryWork = "Run the persisted delivery plan after Chat settles.";
  await expect(panel.getByText(
    "Turn product requirements into accountable weekly delivery.",
    { exact: true },
  )).toBeVisible();

  await state.finishStream(updatedPrimaryWork);
  await state.settledDraftRefreshStarted;

  await expect(create).toBeDisabled();
  await expect(create).toHaveAttribute("aria-busy", "true");
  await expect(panel.getByText(
    "Turn product requirements into accountable weekly delivery.",
    { exact: true },
  )).toBeVisible();

  state.releaseSettledDraftRefresh();
  await expect(panel.getByText(updatedPrimaryWork, { exact: true })).toBeVisible();
  await expect(create).toBeEnabled();
  expect(state.pageErrors).toEqual([]);
});

test("unsaved Blueprint personalization survives draft refresh and is saved before creation", async ({ page }) => {
  const state = await mockApp(page, {
    streaming: true,
    withPersonalization: true,
  });
  await page.goto("/chat");

  await page.locator("button.chat-artifact-summary-open", {
    hasText: "Focused product collaboration",
  }).click();
  const panel = page.getByRole("region", { name: "Draft summary" });
  const brandName = panel.getByLabel("Brand name");
  await expect(brandName).toHaveValue("Stored brand");
  await brandName.fill("Unsaved local brand");

  await state.finishStream("Use the latest persisted delivery plan.");
  await expect(panel.getByText(
    "Use the latest persisted delivery plan.",
    { exact: true },
  )).toBeVisible();
  await expect(brandName).toHaveValue("Unsaved local brand");

  await panel.getByRole("button", { name: "Create Workspace" }).click();
  await expect.poll(state.finalizeCalls).toBe(1);
  expect(state.personalizationUpdates()).toEqual([{
    brand_name: "Unsaved local brand",
  }]);
  expect(state.pageErrors).toEqual([]);
});

test("Blueprint application requires fresh conversational creation confirmation", async ({ page }) => {
  const state = await mockApp(page, { withBlueprint: true, delayBlueprint: true });
  await page.goto("/chat");

  await page.locator("button.chat-artifact-summary-open", {
    hasText: "Focused product collaboration",
  }).click();
  const panel = page.getByRole("region", { name: "Draft summary" });
  const create = panel.getByRole("button", { name: "Create Workspace" });
  await panel.getByRole("button", { name: "Use template" }).click();
  await state.blueprintStarted;

  await expect(create).toBeDisabled();
  await expect(create).toHaveAttribute("aria-busy", "true");
  expect(state.finalizeCalls()).toBe(0);

  state.releaseBlueprint();
  await expect(panel.getByText("Run the applied weekly product delivery playbook.")).toBeVisible();
  await expect(create).toHaveCount(0);
  await expect(
    panel.getByRole("button", { name: "Keep chatting until ready" }),
  ).toBeDisabled();
  expect(state.applyCalls()).toBe(1);
  expect(state.finalizeCalls()).toBe(0);
  expect(state.pageErrors).toEqual([]);
});

test("Blueprint application stays locked once Workspace creation starts", async ({ page }) => {
  const state = await mockApp(page, { withBlueprint: true, delayFinalize: true });
  await page.goto("/chat");

  await page.locator("button.chat-artifact-summary-open", {
    hasText: "Focused product collaboration",
  }).click();
  const panel = page.getByRole("region", { name: "Draft summary" });
  const useTemplate = panel.getByRole("button", { name: "Use template" });
  await panel.getByRole("button", { name: "Create Workspace" }).click();
  await state.finalizeStarted;

  await expect(useTemplate).toBeDisabled();
  expect(state.applyCalls()).toBe(0);

  state.releaseFinalize();
  await expect(panel.getByRole("button", { name: "Open workspace", exact: true })).toBeVisible();
  expect(state.finalizeCalls()).toBe(1);
  expect(state.pageErrors).toEqual([]);
});

test("abandoned drafts are read-only and expose no editing entry points", async ({ page }) => {
  const state = await mockApp(page, { withBlueprint: true, status: "abandoned" });
  await page.goto("/chat");

  await page.locator("button.chat-artifact-summary-open", {
    hasText: "Focused product collaboration",
  }).click();
  const panel = page.getByRole("region", { name: "Draft summary" });
  await expect(panel.getByText("Abandoned", { exact: true }).first()).toBeVisible();
  await expect(panel.getByRole("button", { name: "Use template" })).toHaveCount(0);
  await expect(panel.getByRole("button", { name: "Create Workspace" })).toHaveCount(0);
  await expect(panel.getByText("Resume from the Workspaces page anytime.")).toHaveCount(0);
  await expect(panel.getByText("Keep chatting until ready")).toHaveCount(0);
  await expect(panel.getByText("Draft saved", { exact: true })).toBeVisible();
  expect(state.applyCalls()).toBe(0);
  expect(state.finalizeCalls()).toBe(0);
  expect(state.pageErrors).toEqual([]);
});

test("the dedicated creation Chat keeps an abandoned draft composer read-only", async ({ page }) => {
  const state = await mockApp(page, { status: "abandoned" });
  await page.goto(`/workspaces/new?draft=${DRAFT_ID}`);

  await expect(page.getByRole("region", { name: "Draft summary" }).getByText("Abandoned", { exact: true }).first()).toBeVisible();
  const composer = page.getByRole("main").getByRole("textbox");
  await expect(composer).toHaveAttribute("aria-disabled", "true");
  await expect(composer).toHaveAttribute("contenteditable", "false");
  await expect(composer).toHaveAttribute("data-placeholder", "Draft saved");
  await expect(page.getByRole("button", { name: "Weekend coffee popup" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Create Workspace" })).toHaveCount(0);
  expect(state.finalizeCalls()).toBe(0);
  expect(state.pageErrors).toEqual([]);
});
