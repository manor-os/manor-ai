---
name: dashboard-module-builder
version: 1.2.1
description: Use this skill when the user asks Manor AI to add, edit, remove, rearrange, or personalize content on their Dashboard, including requests such as "show daily news", "add a stock module", or "change this dashboard module". It generates a private, loadable Dashboard module and submits it for live preview instead of returning code in chat.
---

# Dashboard Module Builder

## Purpose

Turn the user's Dashboard request into a complete private Dashboard preview. Use the supplied current layout as the source of truth, use authorized tools for data discovery, use the Code tool to validate the complete implementation, and finish by calling `dashboard_submit_module` exactly once.

Do not paste module source code into the chat response. The submit tool is the only successful completion path.

This is one universal builder, not a catalog of request-specific implementations. Interpret every request during the current turn. Never add fixed locations, topics, titles, aliases, sample results, source choices, or UI templates for a particular prompt. Request values belong only in that user's generated module and its data requests; the host runtime stays domain-neutral.

## Inputs

The skill input contains:

- `user_request`: what the user wants to see or change.
- `available_widgets`: built-in Dashboard widgets and their purpose.
- `current_widgets`: current order and visibility.
- `current_modules`: generated modules already saved or being previewed.
- `target_module`: the one module being edited, or `null` for a Dashboard-level request.

Treat all layout state in the input as authoritative. Preserve anything the user did not ask to change.

## Required workflow

1. Interpret the user request in the context of the current layout.
2. Decide whether a built-in widget satisfies it.
3. If a generated module is needed, select the narrowest authorized data source.
4. Use read-only Manor tools only when the request requires external or connected data discovery.
5. Generate the complete dependency-free HTML, CSS, and JavaScript bundle.
6. Call `code` with `action: dashboard_module_validate` and the complete code bundle.
7. Revise every validation error and warning, then validate the full bundle again until `platform_ready` is true.
8. Call `dashboard_submit_module` exactly once using the exact validated code bundle.
9. Return one short sentence confirming that the preview is ready.

If the request is ambiguous enough that a useful module cannot be produced, ask one concise question and do not call the submit tool.

## Existing module edits

When `target_module` is not `null`:

- Return exactly one `module_changes` entry.
- Use the target module id.
- Use `action: update`, unless the user explicitly asks to delete it.
- Regenerate the complete code bundle for every update.
- Do not create another module.
- Do not change built-in widgets or any other generated module.
- Preserve the target title unless the user asks to rename it.
- When the user changes only presentation, layout, grouping, or styling, preserve the target's data meaning, filters, locations, tool names, and tool arguments. Change the data source only when the requested presentation requires fields the current source cannot provide.

When `target_module` is `null` and the requested module already exists in `current_modules`, update that module instead of creating a duplicate. Match by purpose and normalized title, not only by exact capitalization.

## Built-in widgets

Use a built-in widget when it already represents the requested information. The submit payload must include every known widget exactly once with `id` and `visible`.

Preserve current order and visibility unless the user asks to show, hide, or reorder a widget.

## Generated module code

Generated code runs inside a sandboxed iframe. It has no direct network, storage, parent-page, form, popup, or external asset access.

The code bundle must contain:

- `version: 1`
- `runtime: sandboxed_html`
- `html`: body markup only; the host renders the module title.
- `css`: dependency-free CSS using host variables.
- `javascript`: defines `window.renderDashboardModule(data, context)`.
- `data_requests`: zero or more authorized host data requests.

The renderer must:

- Read values only from the `data` argument.
- Render into elements supplied by `html`.
- Use `textContent` for all data-derived text.
- Handle missing, empty, malformed, and loading-complete data without throwing.
- Clear old rows before rendering new rows.
- Work at 320px width without horizontal scrolling.
- Use the user's language for labels and empty states.

Never use:

- `fetch`, `XMLHttpRequest`, `WebSocket`, or `EventSource`
- `localStorage`, `sessionStorage`, `indexedDB`, or cookies
- `eval`, `Function`, dynamic import, or injected scripts
- `window.parent`, `window.top`, `window.opener`, or `postMessage`
- forms, iframes, popups, inline event attributes, or external assets
- `@import`, CSS `url()`, script tags, or style tags in HTML

Use these CSS variables:

- `--module-text`
- `--module-muted`
- `--module-faint`
- `--module-border`
- `--module-border-strong`
- `--module-surface`
- `--module-row`
- `--module-row-hover`
- `--module-accent`
- `--module-accent-soft`
- `--module-danger`
- `--module-warning`
- `--module-info`
- `--module-focus`
- `--module-font`
- `--module-radius-sm`
- `--module-radius-md`
- `--module-control-height`
- `--module-space-1` through `--module-space-5`
- `--module-type-xs`, `--module-type-sm`, `--module-type-md`, `--module-type-lg`
- `--module-type-title`, `--module-type-metric`

## Platform UX

The Dashboard host owns the module title, icon, edit controls, loading state, error state, conversation, and preview confirmation. Do not recreate those surfaces inside generated code.

The module body must feel native to Manor:

- Use quiet, compact operational layouts optimized for scanning and repeated use.
- Treat the host as the only outer card. Start the module body as an unframed layout; never recreate the module title, icon, description, or outer panel.
- Use the Manor type scale: 10px metadata, 11px secondary text, 12px body text, 13px row labels, 16px internal section titles, and at most 32px for a primary metric.
- Use the Manor spacing scale through `--module-space-1` to `--module-space-5`; prefer 4, 6, 8, 12, and 16px rhythm.
- Use `--module-control-height` for compact controls and the radius tokens for framed rows or controls.
- Use `--module-row` for quiet grouped rows and `--module-row-hover` for interactive hover states.
- Use semantic color tokens only. Do not hard-code hex, RGB, HSL, or named palette colors.
- Inherit `--module-font`; do not introduce another font family.
- Let the host own elevation. Do not add box shadows inside generated modules.
- Use unframed sections, rows, tables, charts, calendars, or compact controls as the content requires.
- Do not wrap the entire body in another decorative card and do not nest cards.
- Keep framed surfaces at 8px radius or less. Use the host radius variables.
- Do not use decorative gradients, background blobs, oversized headings, negative letter spacing, or viewport-scaled type.
- Prefer borders, spacing, typography, and semantic tokens over hard-coded colors.
- Keep controls at stable dimensions and use familiar symbols for icon-only actions.
- Ensure long labels wrap or truncate without changing surrounding layout dimensions.
- Use responsive grid/flex constraints so the module works in compact and wide Dashboard columns.
- Keep user-facing copy concise and render an honest empty state when no data matches.

## Code development tool

`code(action="dashboard_module_validate", params={"code": <complete bundle>})` is mandatory for every created or updated module. In Dashboard conversations, no other Code action is permitted.

Validation checks the sandbox security contract, data-request structure, responsive implementation, and Manor platform styling. A bundle is ready only when the result has `platform_ready: true` and `recorded_for_dashboard_submission: true`. Warnings are blocking for Dashboard submission: revise them instead of ignoring them.

## Data requests

Each data request has:

```json
{
  "key": "safe_identifier",
  "source": "news",
  "params": {}
}
```

The `key` must start with a lowercase letter and contain only lowercase letters, digits, and underscores.

### Built-in sources

Choose the source that best satisfies the user's requested content, freshness, scope, and presentation. Built-in sources are typed and already authorized; connected read-only tools may be more appropriate when they provide a better result. Do not choose a source from keywords alone: generate against its documented or observed result shape.

`stats`

- Returns the Dashboard statistics object.
- Use `params: {}`.

`tasks`

- Returns a task array.
- Params may include `statuses`, `priorities`, `query`, `days`, and `limit`.

`workspaces`

- Returns a workspace array.
- Params may include `statuses`, `query`, and `limit`.

`activity`

- Returns recent activity items.
- Params may include `actions`, `query`, `days`, and `limit`.
- This source is only for Manor-internal work activity. It does not represent public events, local activities, or things to do.

`task_trends`

- Returns `{date, created, completed}` rows.
- Params may include `days`.

`news`

- Returns an array of `{id, title, url, source, published_at, language}`.
- Params may include `query`, `days`, and `limit`.

`stocks`

- Returns quote rows containing `symbol`, `price`, `change`, `change_percent`, `currency`, `updated_at`, `status`, and `provider`.
- Params must include `symbols` and may include `refresh_seconds`.

`http_json`

- Fetches an unauthenticated public JSON endpoint chosen by the generated module code.
- Put the complete HTTPS endpoint in top-level `url`, keep `params: {}`, and set `refresh_seconds` between 30 and 3600.
- The raw JSON response is delivered under the request `key`; the module's JavaScript owns all domain-specific parsing and presentation.
- This is the default for public real-time APIs when the generated code can name a stable JSON endpoint. The platform does not provide domain-specific weather, news, sports, or other request implementations.
- Only standard-port HTTPS GET is available. Redirects, credentials, custom headers, private-network addresses, oversized responses, and non-JSON content are rejected by the generic egress boundary.
- Do not use `http_json` for Manor data, connected accounts, private APIs, or secrets. Those require an authorized built-in source or connected read-only tool.

Example:

```json
{
  "key": "live_public_data",
  "source": "http_json",
  "params": {},
  "url": "https://public-api.example/v1/data?scope=user-requested-value",
  "refresh_seconds": 300
}
```

The URL, query construction, expected response shape, and parser belong to that user's private generated code bundle. During development, inspect the endpoint's real response before writing the renderer. Do not add request-specific behavior to the Dashboard host.

### Connected read-only tools

Use `source: tool` when an available connected read-only tool is the best authorized source for the requested module.

Requests for public or location-based external information, including local events, things to do, conferences, meetups, exhibitions, and performances, require an authorized external or connected read-only tool unless a built-in source genuinely supplies that information. Never silently replace external information with Manor tasks, workspaces, or internal activity.

For date-aware public event views, use `web_event_search` when available. Pass `location` as a geographic area only, `topics` as the user's required interests, and an explicit date range. Do not put UI instructions such as "add", "show", "weekly", or "calendar" into `location`, and do not drop a requested topic to increase the result count. The tool returns `{query, location, topics, start_date, end_date, events, sources_checked}` where each event contains `title`, `url`, `start_at`, `end_at`, `venue`, `summary`, `source`, and `location_query`. A calendar grouped by day requires this structured date data; do not group ordinary web search result pages into calendar dates.

For private inbox, email, Gmail, Outlook, or unread-message views, use a connected read-only email tool rather than Manor tasks or activity. Use `search_tools` for the user's available provider, then prefer a list/read tool such as `mcp__gmail__list_messages`, `mcp__email__list_messages`, or the provider's equivalent. For Gmail list views, pass `include_details: true`, a narrow query such as `newer_than:7d` or `is:unread newer_than:7d` when the user asks for unread mail, and a small `max_results` limit so the renderer receives subjects, senders, dates, snippets, and labels in one refreshable data request. Never use send, reply, draft, mark, archive, delete, or label-modifying email tools from a passive Dashboard module. If the user asks the Dashboard to perform an email side effect, explain that Dashboard modules can only display read-only email data and do not submit a preview.

The request must include:

```json
{
  "key": "connected_data",
  "source": "tool",
  "params": {},
  "tool_name": "exact_registered_tool_name",
  "tool_arguments": {},
  "refresh_seconds": 300
}
```

Before declaring a connected tool:

1. Use `search_tools` if the exact tool is not already known.
2. Call the read-only tool once with representative arguments.
3. Inspect the real JSON result shape.
4. Generate the renderer against that exact result shape.
5. Declare the same tool name and arguments in `data_requests`.

When the request covers multiple independent locations or categories and one combined call would make coverage unreliable, declare one read-only data request per segment. Merge and deduplicate those results in the renderer so every requested segment remains visible in one module.

Never declare mutation, send, publish, upload, browser-control, shell, file-writing, approval, or interactive tools. If the required data needs such a tool, explain that a passive Dashboard module cannot perform the action and do not submit a preview.

## Submission contract

Call `dashboard_submit_module` exactly once.

The submit tool rejects create/update code that was not validated during the same turn. Do not modify HTML, CSS, JavaScript, or data requests after successful Code validation; any change requires validating the complete bundle again.

For a new module, the payload follows this structure:

```json
{
  "widgets": [{"id": "daily_brief", "visible": true}],
  "module_changes": [
    {
      "action": "create",
      "title": "Priority Work Monitor",
      "description": "Current high-priority work requiring attention.",
      "visible": true,
      "size": "wide",
      "code": {
        "version": 1,
        "runtime": "sandboxed_html",
        "html": "...",
        "css": "...",
        "javascript": "...",
        "data_requests": [
          {
            "key": "priority_work",
            "source": "tasks",
            "params": {"priorities": [1, 2], "limit": 8}
          }
        ]
      }
    }
  ],
  "assistant_message": "I built the requested module and it is ready to preview."
}
```

Include all current built-in widgets in the real payload, not only the example widget above.

## Quality checks

Before submitting, verify:

- The request is reflected in the module's actual visible content.
- The selected source and renderer match the data contract the AI chose.
- Public API modules keep their endpoint and domain-specific parsing in the generated bundle rather than relying on a request-specific host tool.
- Public or location-based external information has not fallen back to internal tasks or activity.
- Presentation-only edits preserve the module's original data scope and constraints.
- Existing equivalent modules are updated instead of duplicated.
- Every update includes complete HTML, CSS, JavaScript, and data requests.
- Code validation returned `platform_ready: true` for the exact submitted bundle.
- No forbidden API or external asset appears in generated code.
- Empty data produces a useful empty state.
- Long titles and values wrap or truncate cleanly.
- The module works at compact and wide sizes.
- The submit payload changes only what the user requested.
