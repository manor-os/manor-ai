#!/usr/bin/env node
/* eslint-disable no-console */
//
// manor-worker CLI — register, start, status, deregister.
//
//   manor-worker register \
//     --endpoint https://manor.example.com \
//     --admin-token $MANOR_ADMIN_TOKEN \
//     --kind custom_http \
//     --name "my shopify worker" \
//     --capability action=shopify
//
//   manor-worker start --handler ./my-handler.js
//   manor-worker status
//   manor-worker deregister
//
// Credentials default to ~/.manor/worker.json. Override with --config
// or MANOR_WORKER_CONFIG.

import { mkdir, readFile, writeFile, chmod } from "node:fs/promises";
import { homedir } from "node:os";
import { dirname, resolve } from "node:path";
import { pathToFileURL } from "node:url";

import { ManorClient } from "./client.js";
import { ManorWorker } from "./worker.js";

interface SavedCreds {
  endpoint: string;
  worker_id: string;
  worker_secret: string;
  registered_at: string;
}

const HELP = `manor-worker — Manor.os worker CLI

USAGE
  manor-worker <command> [options]

COMMANDS
  register      Register a new worker with the Manor server.
  start         Start the worker loop (loads a handler module).
  status        Send one heartbeat and print the server's response.
  deregister    Tell the server to forget this worker.
  help          Show this help.

GLOBAL OPTIONS
  --config <path>   Path to credentials file (default: ~/.manor/worker.json,
                    or env MANOR_WORKER_CONFIG).

REGISTER OPTIONS
  --endpoint <url>            Manor base URL (env: MANOR_ENDPOINT).
  --admin-token <jwt>         Admin User JWT (env: MANOR_ADMIN_TOKEN).
  --kind <k>                  Worker kind. Required.
                              One of: claude_code, openclaw, paperclip_bridge,
                              custom_http, shell_script, mcp_reverse.
  --name <s>                  Display name. Required.
  --description <s>           Optional description.
  --version <s>               Worker version string.
  --trust-level <l>           high | standard | low (default: standard).
  --capability <kind=provider>  Repeatable. e.g. --capability action=shopify
  --monthly-budget-usd <n>    Monthly cap in USD.
  --uses-manor-credentials    Worker wants Manor to lease credentials.

START OPTIONS
  --endpoint <url>          Override saved endpoint.
  --handler <path>          Path to a JS / TS module exporting register(worker).
                            Required.
  --max-concurrent <n>      Max parallel leases (default: 1).
`;

async function main(argv: string[]): Promise<number> {
  const [cmd, ...rest] = argv;
  if (!cmd || cmd === "help" || cmd === "-h" || cmd === "--help") {
    console.log(HELP);
    return 0;
  }
  switch (cmd) {
    case "register":
      return cmdRegister(parseFlags(rest));
    case "start":
      return cmdStart(parseFlags(rest));
    case "status":
      return cmdStatus(parseFlags(rest));
    case "deregister":
      return cmdDeregister(parseFlags(rest));
    default:
      console.error(`unknown command: ${cmd}\n`);
      console.error(HELP);
      return 2;
  }
}

// ── Commands ──────────────────────────────────────────────────────────

async function cmdRegister(flags: Flags): Promise<number> {
  const endpoint = required(flags, "endpoint", "MANOR_ENDPOINT");
  const adminToken = required(flags, "admin-token", "MANOR_ADMIN_TOKEN");
  const kind = required(flags, "kind");
  const name = required(flags, "name");

  const capabilities = parseCapabilities(arrayFlag(flags, "capability"));
  if (capabilities.handles.length === 0) {
    fail("at least one --capability kind=provider is required");
  }

  const payload: Record<string, unknown> = {
    kind,
    display_name: name,
    capabilities: {
      handles: capabilities.handles,
      uses_manor_credentials: flags["uses-manor-credentials"] === true,
    },
    trust_level: stringFlag(flags, "trust-level") ?? "standard",
  };
  const desc = stringFlag(flags, "description");
  if (desc) payload.description = desc;
  const ver = stringFlag(flags, "version");
  if (ver) payload.version = ver;
  const budget = stringFlag(flags, "monthly-budget-usd");
  if (budget) payload.monthly_budget_usd = Number(budget);

  const url = `${endpoint.replace(/\/+$/, "")}/api/v1/workers/register`;
  const resp = await fetch(url, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${adminToken}`,
    },
    body: JSON.stringify(payload),
  });
  if (!resp.ok) {
    const body = await resp.text();
    fail(`register failed (${resp.status}): ${body.slice(0, 500)}`);
  }
  const out = (await resp.json()) as { worker_id: string; worker_secret: string };

  const creds: SavedCreds = {
    endpoint,
    worker_id: out.worker_id,
    worker_secret: out.worker_secret,
    registered_at: new Date().toISOString(),
  };
  const path = configPath(flags);
  await mkdir(dirname(path), { recursive: true });
  await writeFile(path, JSON.stringify(creds, null, 2) + "\n");
  await chmod(path, 0o600).catch(() => {}); // best-effort on Windows

  console.log(`✓ registered worker_id=${out.worker_id}`);
  console.log(`  credentials saved → ${path}`);
  console.log(
    `  the worker_secret is shown ONCE — keep it safe; rotate via 'manor-worker rotate' if leaked.`,
  );
  return 0;
}

async function cmdStart(flags: Flags): Promise<number> {
  const creds = await loadCreds(flags);
  const handlerPath = required(flags, "handler");
  const maxConcurrent = Number(stringFlag(flags, "max-concurrent") ?? "1");

  const handlerModule = await importHandler(handlerPath);
  const register = handlerModule.register;
  if (typeof register !== "function") {
    fail(`handler module ${handlerPath} must export a 'register(worker)' function`);
  }

  const worker = new ManorWorker({
    endpoint: stringFlag(flags, "endpoint") ?? creds.endpoint,
    workerId: creds.worker_id,
    secret: creds.worker_secret,
    maxConcurrentLeases: maxConcurrent,
  });
  await register(worker);
  console.log(`▶ starting worker ${creds.worker_id} (max ${maxConcurrent} concurrent)`);
  await worker.runForever();
  console.log("✓ worker stopped");
  return 0;
}

async function cmdStatus(flags: Flags): Promise<number> {
  const creds = await loadCreds(flags);
  const client = new ManorClient({
    endpoint: stringFlag(flags, "endpoint") ?? creds.endpoint,
    workerId: creds.worker_id,
    secret: creds.worker_secret,
  });
  const resp = await client.heartbeat({
    state: "idle",
    timestamp: new Date().toISOString(),
    active_leases: [],
    completed_since_last: [],
    capacity: { can_accept_leases: 0 },
  });
  console.log(JSON.stringify(resp, null, 2));
  return 0;
}

async function cmdDeregister(flags: Flags): Promise<number> {
  const creds = await loadCreds(flags);
  const client = new ManorClient({
    endpoint: stringFlag(flags, "endpoint") ?? creds.endpoint,
    workerId: creds.worker_id,
    secret: creds.worker_secret,
  });
  await client.deregister();
  console.log(`✓ deregistered ${creds.worker_id}`);
  console.log(`  (local credentials at ${configPath(flags)} not deleted; remove manually if desired)`);
  return 0;
}

// ── Flag / config helpers ──────────────────────────────────────────────

type FlagValue = string | boolean | string[];
type Flags = Record<string, FlagValue>;

function parseFlags(argv: string[]): Flags {
  const out: Flags = {};
  for (let i = 0; i < argv.length; i++) {
    const tok = argv[i];
    if (!tok.startsWith("--")) continue;
    const key = tok.slice(2);
    const next = argv[i + 1];
    if (next === undefined || next.startsWith("--")) {
      out[key] = true;
    } else {
      const cur = out[key];
      if (Array.isArray(cur)) cur.push(next);
      else if (typeof cur === "string") out[key] = [cur, next];
      else out[key] = next;
      i++;
    }
  }
  return out;
}

function arrayFlag(flags: Flags, key: string): string[] {
  const v = flags[key];
  if (v === undefined) return [];
  if (Array.isArray(v)) return v;
  if (typeof v === "string") return [v];
  return [];
}

function required(flags: Flags, key: string, envName?: string): string {
  const v = flags[key];
  if (typeof v === "string" && v.length > 0) return v;
  if (envName) {
    const env = process.env[envName];
    if (env) return env;
  }
  fail(
    `missing required --${key}${envName ? ` (or env ${envName})` : ""}`,
  );
}

function fail(msg: string): never {
  console.error(`error: ${msg}`);
  process.exit(2);
}

function configPath(flags: Flags): string {
  const fromFlag = stringFlag(flags, "config");
  if (fromFlag) return resolve(fromFlag);
  if (process.env.MANOR_WORKER_CONFIG) return resolve(process.env.MANOR_WORKER_CONFIG);
  return resolve(homedir(), ".manor", "worker.json");
}

function stringFlag(flags: Flags, key: string): string | undefined {
  const v = flags[key];
  return typeof v === "string" ? v : undefined;
}

async function loadCreds(flags: Flags): Promise<SavedCreds> {
  const path = configPath(flags);
  try {
    const raw = await readFile(path, "utf8");
    const parsed = JSON.parse(raw) as Partial<SavedCreds>;
    if (!parsed.endpoint || !parsed.worker_id || !parsed.worker_secret) {
      fail(`config at ${path} is incomplete — re-run 'manor-worker register'`);
    }
    return parsed as SavedCreds;
  } catch (exc) {
    if ((exc as NodeJS.ErrnoException).code === "ENOENT") {
      fail(
        `no credentials at ${path} — run 'manor-worker register' first`,
      );
    }
    throw exc;
  }
}

interface HandlerModule {
  register?: (worker: ManorWorker) => void | Promise<void>;
}

async function importHandler(path: string): Promise<HandlerModule> {
  const abs = resolve(path);
  const url = pathToFileURL(abs).href;
  return (await import(url)) as HandlerModule;
}

interface ParsedCapabilities {
  handles: { kind: string; provider?: string }[];
}

function parseCapabilities(raw: string[]): ParsedCapabilities {
  const handles: { kind: string; provider?: string }[] = [];
  for (const entry of raw) {
    const [kind, provider] = entry.split("=", 2);
    if (!kind) fail(`malformed --capability ${JSON.stringify(entry)}`);
    handles.push(provider ? { kind, provider } : { kind });
  }
  return { handles };
}

// ── entry ──────────────────────────────────────────────────────────────

main(process.argv.slice(2)).then(
  (code) => process.exit(code),
  (err) => {
    console.error("fatal:", err);
    process.exit(1);
  },
);
