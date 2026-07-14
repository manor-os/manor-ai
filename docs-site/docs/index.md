---
sidebar_position: 1
title: Overview
---

<section className="ma-hero">
  <div className="ma-hero__copy">
    <span className="ma-kicker">Self-hosted AI workspace runtime</span>
    <h1>Manor AI</h1>
    <p className="ma-lede">
      Run agents, tasks, documents, tools, integrations, and approval gates in
      one workspace that your team can inspect and operate.
    </p>
    <div className="ma-actions">
      <a className="ma-button ma-button--primary" href="quickstart">Start locally</a>
      <a className="ma-button ma-button--secondary" href="https://github.com/manor-os/manor-ai">View GitHub</a>
    </div>
    <div className="ma-hero__facts" aria-label="Runtime highlights">
      <span>BYOK model providers</span>
      <span>Human approval for sensitive actions</span>
      <span>Local data ownership</span>
    </div>
  </div>
  <figure className="ma-hero__visual">
    <img src="img/manor-ai-runtime.png" alt="Manor AI workspace runtime showing goals, tasks, documents, agents, and workspace health" />
  </figure>
</section>

<section className="ma-section ma-section--paths">
  <div className="ma-section__heading">
    <span className="ma-kicker">Choose a path</span>
    <h2>Get from clone to operating workspace.</h2>
  </div>
  <div className="ma-path-grid">
    <a className="ma-path-card" href="quickstart">
      <span>Run locally</span>
      <strong>Docker Compose quick start</strong>
      <p>Boot the web app, API, worker, PostgreSQL, Redis, MinIO, and sandbox on one machine.</p>
    </a>
    <a className="ma-path-card" href="concepts/agents">
      <span>Understand the runtime</span>
      <strong>Agents, skills, tools, and HITL</strong>
      <p>Learn how workspaces scope context, tool access, and review requirements.</p>
    </a>
    <a className="ma-path-card" href="configuration">
      <span>Prepare a deployment</span>
      <strong>Configuration and operations</strong>
      <p>Set secrets, model providers, storage, backups, and upgrade routines before inviting users.</p>
    </a>
  </div>
</section>

<section className="ma-section ma-scenario">
  <div className="ma-scenario__copy">
    <span className="ma-kicker">Human-in-the-loop controls</span>
    <h2>Governance is part of the runtime, not a policy document.</h2>
    <p>
      Operators can write plain-language rules, map them to runtime action
      patterns, and require approval before customer-facing or irreversible
      actions run.
    </p>
    <ul className="ma-check-list">
      <li>Rules can require review before email, chat, or social posts are sent.</li>
      <li>Destructive actions can be denied at runtime instead of relying on prompt wording.</li>
      <li>Policy revisions make operator changes visible during audits.</li>
    </ul>
  </div>
  <figure className="ma-scenario__visual">
    <img src="img/manor-ai-governance.png" alt="Manor AI governance rules requiring approval for external messages and blocking destructive actions" />
  </figure>
</section>

<section className="ma-section ma-evidence">
  <div className="ma-section__heading">
    <span className="ma-kicker">Developer surface</span>
    <h2>Use the web app, or integrate directly against the API.</h2>
  </div>
  <div className="ma-evidence__grid">
    <figure className="ma-evidence__visual">
      <img src="img/manor-ai-goals.png" alt="Manor AI goal execution canvas showing goals connected to workspace tasks" />
    </figure>
    <figure className="ma-evidence__visual">
      <img src="img/manor-ai-api-reference.png" alt="Manor AI OpenAPI reference with authentication endpoints" />
    </figure>
  </div>
</section>

<section className="ma-section ma-capabilities">
  <div className="ma-section__heading">
    <span className="ma-kicker">What ships</span>
    <h2>The self-hosted stack includes the runtime surface, not just an SDK.</h2>
  </div>
  <div className="ma-capability-grid">
    <div className="ma-capability">
      <span>01</span>
      <h3>Workspaces</h3>
      <p>Shared operating rooms for goals, tasks, documents, knowledge, channels, and activity.</p>
    </div>
    <div className="ma-capability">
      <span>02</span>
      <h3>Agents and tools</h3>
      <p>Scoped skills, tool calls, model routing, task execution, and audit-friendly traces.</p>
    </div>
    <div className="ma-capability">
      <span>03</span>
      <h3>HITL governance</h3>
      <p>Approval policies for externally visible, irreversible, or high-risk actions.</p>
    </div>
    <div className="ma-capability">
      <span>04</span>
      <h3>Self-hosted services</h3>
      <p>FastAPI, React, workers, PostgreSQL with pgvector, Redis, MinIO, and sandbox execution.</p>
    </div>
  </div>
</section>

<section className="ma-section ma-boundary">
  <div>
    <span className="ma-kicker">Scope</span>
    <h2>Designed for operators who want control.</h2>
  </div>
  <p>
    Manor AI does not require hosted Manor AI services to boot, create
    workspaces, configure model keys, run agents, use the sandbox, or manage
    documents and knowledge. Start with <a href="quickstart">Quick Start</a>,
    then review <a href="security">Security</a> and <a href="operations/backup-restore">Backup and Restore</a>
    before running important workloads.
  </p>
</section>
