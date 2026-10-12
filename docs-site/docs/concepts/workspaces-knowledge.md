---
title: Workspaces and Knowledge
---

# Workspaces and Knowledge

Workspaces organize people, agents, documents, tasks, workflows, and knowledge
under one operational boundary — typically one per part of your business: a
property you manage, a support inbox, a marketing function.

## Interface Modes

A saved choice is kept. Without one, existing users default to **Professional**
without a first-use prompt; new users see **User Guide** with **Simple**
preselected. Select **Start** to save and begin the tour. Reopen it from
**More → User Guide**, or change mode in **Settings → Appearance → Interface mode**.
Your choice is saved in this browser.

- **Simple** focuses on chat and **Install Workspace**, which imports a
  [Blueprint](blueprints.md) JSON file in self-hosted Manor AI. Workspace chat
  still shows Manor and the agents' avatars and names, plus the member count,
  as read-only team information. Necessary run statuses, approvals, artifacts,
  and connection setup remain accessible.
- **Professional** provides the full Workspace pages, configuration controls,
  and detailed chat activity.

Switching modes changes presentation, not permissions, execution, or data.
Existing conversations and text drafts remain available.

## Workspaces

A workspace scopes:

- Agents and skills (via service mappings and subscriptions).
- Conversations and tasks.
- [Goals](goals.md) and their execution plans.
- Documents and file assets.
- Knowledge sources.
- Integration credentials and [channels](../integrations/channels.md).
- Governance rules, permissions, and audit history.

In Professional mode, a workspace page has tabs for its overview (services,
matched agents, current work, operating score), goals, tasks, documents,
channels, agents, activity, and settings. Workspace chat is contextual: it sees the workspace's services,
goals, agents, and recent activity, and can start
[workflows](workflows.md) directly from a message.

In Professional mode, creating a workspace is a guided, chat-style flow — Manor
helps you fill in the workspace's purpose, services, staff, channels, and seed knowledge, then
creates it. You can also install a [blueprint](blueprints.md) to start from a
packaged configuration.

### Deleting a Workspace

Deleting a workspace moves it to Trash for the configured grace period
(30 days by default), during which it can be restored. New workspace work
stops immediately; user-created automations and live Workflow action grants
are removed. Restore recreates the workspace's built-in runtime schedules.
After the grace period, background purge permanently removes its runtime
records, tasks, conversations, bindings, and workspace-specific Knowledge
groups and links.

Knowledge Documents, folders, source files, versions, and their access controls
are entity-owned and retained. Deleting a workspace does not delete those
documents or widen their access: members-only Knowledge remains restricted
after its source workspace is removed. Manage retained files through Knowledge
with the appropriate resource permissions.

## Knowledge

The knowledge system indexes documents so agents can cite them. In Professional
mode, upload PDFs, Word and Excel files, Markdown, plain text, slide decks, or images to
**Knowledge**, and group documents into knowledge bases by purpose ("House
Rules", "Vendor Contacts", "FAQ"). Attach groups to a workspace and its
agents use them automatically.

### Sharing Roles

Use **Share → Add people** to choose one of four roles on a document or folder:

- **Viewer** can read it.
- **Commenter** can read and comment.
- **Editor** can edit it and upload into shared folders.
- **Co-owner** has the same resource permissions as the owner, including
  external sharing, signature requests, deletion, and access management.

The former **Curator / Access manager** option has been removed. Existing
grants with editing access display as **Editor** and keep their capabilities
until you explicitly change or remove access. Grants without editing access
appear as Viewer or Commenter. Selecting Editor applies the standard Editor permissions.

Select **Co-owner** in a colleague's role menu when they need full control.
The original owner remains recorded as the owner. Classification restrictions
and required approvals apply to both roles. Temporary Co-owner access cannot
create grants or sharing links that outlast that access.
Approved external sharing links start their configured lifetime when approval
is granted, bounded by the requester's original and current access expiry.
Renaming a Workspace root folder requires Workspace management permission.
Moving a document to Trash revokes its sharing grants and links, including
Co-owner access. The original owner or a global Knowledge manager can restore
it; restoration does not reinstate revoked sharing grants.

To send a PDF for signature, a Co-owner uses their own connected email account.
Add each person with **Add signer** and assign a required signature field
to each signer before continuing to the preview. In **Recipient emails**, enter
one or more addresses separated by commas for the same person. Any invited
address can verify by email and complete that person's signature once; the
signature history records the address used. A request supports up to 10 email
addresses across all signers. Every signer must complete their own fields
before a request with multiple signers is finalized.

### How Retrieval Works

Retrieval is chunk-level and hybrid, fused with Reciprocal Rank Fusion:

1. Documents are split into ~1,600-character chunks; each chunk gets its own
   embedding (pgvector, generated by the bundled Ollama service).
2. A query runs vector search and trigram/lexical search (CJK-safe) over
   chunks, plus filename matching.
3. Results are deduplicated to the best chunk per document and rank-fused,
   so exact-term matches and semantic matches both surface.

Freshly uploaded documents that aren't indexed yet can still be found in
explicitly scoped searches — the runtime falls back to reading the file
directly within a workspace or named knowledge-base scope.

### Global Search

The search bar (and `GET /api/v1/search?q=`) does fast substring search
across tasks, documents, agents, and conversations, honoring per-user
document and workspace permissions. It complements — not replaces — the
semantic `rag` retrieval agents use for content questions.

## Data Isolation

Everything is scoped to your entity (organization), and workspace boundaries
apply within it: agents see the workspaces they're mapped into, and
workspace-scoped results are permission-filtered per user. Keep workspace
boundaries meaningful — do not mix unrelated teams or clients in one
workspace unless they intentionally share agents, documents, and memory.
