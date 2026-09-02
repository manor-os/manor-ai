#!/usr/bin/env node
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const workspaceChatSource = await readFile(
  new URL("../src/components/WorkspaceChat.tsx", import.meta.url),
  "utf8",
);
const chatStreamSource = await readFile(
  new URL("../src/lib/chatStream.ts", import.meta.url),
  "utf8",
);
const apiSource = await readFile(
  new URL("../src/lib/api.ts", import.meta.url),
  "utf8",
);
const indexCssSource = await readFile(
  new URL("../src/index.css", import.meta.url),
  "utf8",
);
const taskSource = await readFile(
  new URL("../../../packages/core/tasks/ai_tasks.py", import.meta.url),
  "utf8",
);
const strategistOrchestratorSource = await readFile(
  new URL("../../../packages/core/strategist/orchestrator.py", import.meta.url),
  "utf8",
);
const workspaceChatServiceSource = await readFile(
  new URL("../../../packages/core/workspace_chat/service.py", import.meta.url),
  "utf8",
);

test("workspace agent greetings retain their server sequence", () => {
  assert.match(taskSource, /sequence=i,[\s\S]*?total=len\(agent_data\)/);
  assert.doesNotMatch(taskSource, /asyncio\.sleep\(1\.5\)/);
  assert.match(workspaceChatSource, /agent_greeting_sequence/);
  assert.match(workspaceChatSource, /agent_greeting_total/);
});

test("workspace agent greetings reuse normal chat typewriter pacing", () => {
  assert.match(chatStreamSource, /export const TYPEWRITER_TICK_MS = 36/);
  assert.match(chatStreamSource, /export function nextTypewriterSlice/);
  assert.match(
    workspaceChatSource,
    /nextTypewriterSlice,[\s\S]*?TYPEWRITER_TICK_MS,[\s\S]*?from "\.\.\/lib\/chatStream"/,
  );
  assert.match(workspaceChatSource, /className="chat-streaming-cursor"/);
});

test("workspace agent greetings reveal one message at a time without replaying history", () => {
  assert.match(
    workspaceChatSource,
    /useLayoutEffect\(\(\) => \{\s+if \(threadRef \|\| hasAgentGreetingPlaybackCompleted\(workspaceId\)\) return;/,
  );
  assert.match(workspaceChatSource, /hiddenAgentGreetingIds/);
  assert.match(workspaceChatSource, /activeAgentGreetingId/);
  assert.match(workspaceChatSource, /markAgentGreetingMessagePlayed/);
  assert.match(workspaceChatSource, /hasAgentGreetingMessagePlayed/);
  assert.match(
    workspaceChatSource,
    /if \(hasAgentGreetingPlaybackCompleted\(workspaceId\)\) return;/,
  );
  assert.match(workspaceChatSource, /hasAgentGreetingPlaybackCompleted\(workspaceId\)/);
  assert.match(
    workspaceChatSource,
    /activeIndex < agentGreetingPlayback\.messageIds\.length/,
  );
  assert.match(workspaceChatSource, /prefers-reduced-motion: reduce/);
});

test("realtime strategist activity reuses chat typewriter pacing without replaying history", () => {
  assert.match(
    strategistOrchestratorSource,
    /finish_strategist_review_activity\([\s\S]*?review_id=activity_review_id,[\s\S]*?state="failed"/,
  );
  assert.match(
    workspaceChatServiceSource,
    /event_data\["strategist_activity"\][\s\S]*?"state": strategist_activity\.get\("state"\)/,
  );
  assert.match(workspaceChatSource, /realtimeStrategistActivityIds/);
  assert.match(workspaceChatSource, /realtimeStrategistActivityById/);
  assert.match(
    workspaceChatSource,
    /data\.message_kind === "strategist_activity" &&[\s\S]*?setRealtimeStrategistActivityById/,
  );
  assert.match(
    workspaceChatSource,
    /activity\.state === "running"[\s\S]*?setRealtimeStrategistActivityIds/,
  );
  assert.match(
    workspaceChatSource,
    /terminalActivity[\s\S]*?setWsMessages\(\(current\) =>[\s\S]*?applyStrategistActivityTransition/,
  );
  assert.match(
    workspaceChatSource,
    /function canApplyStrategistActivityState[\s\S]*?!isStrategistActivityTerminalState\(currentState\)[\s\S]*?currentState === "skipped" && nextState === "failed"/,
  );
  assert.match(
    workspaceChatSource,
    /function mergeWorkspaceMessages[\s\S]*?currentActivity\?\.state[\s\S]*?nextActivity\?\.state[\s\S]*?strategist_activity: currentActivity/,
  );
  assert.match(
    workspaceChatSource,
    /preserveReconciliationStop[\s\S]*?reconciliation_stopped_at:[\s\S]*?currentActivity\.reconciliation_stopped_at/,
  );
  assert.match(
    workspaceChatSource,
    /const reconciliationStopped[\s\S]*?if \(!reconciliationStopped\)[\s\S]*?setRealtimeStrategistActivityById/,
  );
  assert.doesNotMatch(workspaceChatSource, /if \(!activityMessageIsLoaded\)/);
  assert.match(
    apiSource,
    /getMessage: \(wsId: string, messageId: string, signal\?: AbortSignal\)/,
  );
  assert.match(
    workspaceChatSource,
    /strategistActivityReconciliationIdsKey[\s\S]*?api\.workspaces\.chat\.getMessage/,
  );
  assert.match(
    workspaceChatSource,
    /const reconciliationAbortController = new AbortController\(\)[\s\S]*?const roundAbortController = new AbortController\(\)[\s\S]*?STRATEGIST_ACTIVITY_RECONCILE_TIMEOUT_MS[\s\S]*?api\.workspaces\.chat\.getMessage\([\s\S]*?roundAbortController\.signal[\s\S]*?finally[\s\S]*?STRATEGIST_ACTIVITY_RECONCILE_MS[\s\S]*?window\.setTimeout\(/,
  );
  assert.match(
    workspaceChatSource,
    /STRATEGIST_ACTIVITY_RECONCILE_MAX_MS\s*=\s*11 \* 60 \* 1_000/,
  );
  assert.match(
    workspaceChatSource,
    /strategistActivityReconciliationStateRef[\s\S]*?Map<string, \{ startedAt: number; attempt: number \}>/,
  );
  assert.doesNotMatch(workspaceChatSource, /stoppedStrategistActivityIds/);
  assert.match(
    workspaceChatSource,
    /stopRemainingReconciliation[\s\S]*?reconciliation_stopped_at[\s\S]*?setRealtimeStrategistActivityIds[\s\S]*?setRealtimeStrategistActivityById[\s\S]*?reconciliationState\.delete/,
  );
  assert.match(
    workspaceChatSource,
    /reconciliationState\.get\(messageId\)\?\.startedAt[\s\S]*?state\?\.attempt[\s\S]*?STRATEGIST_ACTIVITY_RECONCILE_MAX_DELAY_MS[\s\S]*?state\.attempt \+= 1/,
  );
  assert.match(
    workspaceChatSource,
    /reconciliationAbortController\.abort\(\)[\s\S]*?window\.clearTimeout\(timer\)/,
  );
  assert.match(
    workspaceChatSource,
    /const missingMessageIds = new Set<string>\(\)[\s\S]*?error\.status === 404[\s\S]*?missingMessageIds\.add\(messageId\)[\s\S]*?reconciliationState\.delete\(messageId\)[\s\S]*?setWsMessages\(\(current\) =>[\s\S]*?message\.id !== messageId[\s\S]*?retryMessageIds/,
  );
  assert.match(
    workspaceChatSource,
    /isWorkspaceMainChat &&[\s\S]*?data\.message_kind === "strategist_activity"/,
  );
  assert.match(
    workspaceChatSource,
    /if \(!isWorkspaceMainChat \|\| !strategistActivityReconciliationIdsKey\) return/,
  );
  assert.doesNotMatch(
    workspaceChatSource,
    /window\.setInterval\([\s\S]*?STRATEGIST_ACTIVITY_RECONCILE_MS/,
  );
  assert.match(
    workspaceChatSource,
    /isStrategistActivityTerminalState\([\s\S]*?activity\?\.state[\s\S]*?setRealtimeStrategistActivityById/,
  );
  assert.match(
    workspaceChatSource,
    /terminalPersistedStrategistActivityIds[\s\S]*?setRealtimeStrategistActivityById[\s\S]*?next\.delete\(messageId\)/,
  );
  assert.match(
    workspaceChatSource,
    /persistedStrategistActivityIsTerminal\s*=\s*[\s\S]*?isStrategistActivityTerminalState\(persistedStrategistState\)[\s\S]*?!persistedStrategistActivityIsTerminal/,
  );
  assert.match(
    workspaceChatSource,
    /function WsActivityLine[\s\S]*?nextTypewriterSlice\(remaining\)/,
  );
  assert.match(
    workspaceChatSource,
    /item\.msg\.meta\?\.workspace_lifecycle !== true[\s\S]*?Boolean\(item\.msg\.meta\?\.strategist_activity\)[\s\S]*?realtimeStrategistActivityIds\.has\(item\.msg\.id\)/,
  );
  assert.match(workspaceChatSource, /activelyStreaming && \(/);
  assert.match(workspaceChatSource, /onStreamComplete\?\.\(msg\.id\)/);
  assert.match(workspaceChatSource, /collecting_feedback[\s\S]*?IconSearch/);
  assert.match(workspaceChatSource, /analyzing_context[\s\S]*?IconBrain/);
  assert.match(workspaceChatSource, /generating_plan[\s\S]*?IconSparkles/);
  assert.match(workspaceChatSource, /finalizing_plan[\s\S]*?IconDocument/);
  assert.doesNotMatch(workspaceChatSource, /activeStrategistActivityId/);
  assert.match(
    workspaceChatSource,
    /processing=\{[\s\S]*?strategistActivity\?\.state === "running" &&[\s\S]*?!strategistActivityReconciliationIsStopped[\s\S]*?Boolean\(realtimeStrategistActivity\)[\s\S]*?strategistActivity\?\.started_at === "string"[\s\S]*?\}/,
  );
  assert.match(
    workspaceChatSource,
    /useLayoutEffect\(\(\) => \{[\s\S]*?const streamStarted = stream && !previousStreamRef\.current;[\s\S]*?streamCompleteNotifiedRef\.current = false;[\s\S]*?setVisibleCharacters\(0\)/,
  );
  assert.match(
    workspaceChatSource,
    /strategistState === "failed"[\s\S]*?`\$\{text\} — failed`/,
  );
  assert.match(workspaceChatSource, /data-processing=\{\(isStrategistActivity && processing\) \|\| undefined\}/);
  assert.match(
    workspaceChatSource,
    /aria-live=\{workspaceLifecycleActivity \? "polite" : undefined\}/,
  );
  assert.match(
    workspaceChatSource,
    /aria-hidden=\{[\s\S]*?isStrategistActivity && \(stream \|\| shouldAnnounceStrategistStatus\)[\s\S]*?undefined[\s\S]*?\}/,
  );
  assert.match(
    workspaceChatSource,
    /failureShouldAnnounce[\s\S]*?shouldAnnounceStrategistStatus[\s\S]*?className="sr-only"[\s\S]*?role="status"[\s\S]*?aria-atomic="true"/,
  );
  assert.match(
    indexCssSource,
    /\.ws-activity-line-row--strategist\s*\{\s*justify-content:\s*center;/,
  );
  assert.match(
    indexCssSource,
    /\.ws-activity-line-row--strategist \.ws-activity-line\s*\{[\s\S]*?width:\s*min\(100%, 760px\);/,
  );
  assert.match(
    indexCssSource,
    /\[data-processing="true"\] \.ws-activity-line-glyph svg[\s\S]*?animation:\s*ws-strategist-processing-breathe/,
  );
  assert.match(
    indexCssSource,
    /@media \(prefers-reduced-motion: reduce\)[\s\S]*?\[data-processing="true"\][\s\S]*?animation:\s*none/,
  );
});
