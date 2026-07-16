---
title: Workspaces and Knowledge
---

# Workspaces and Knowledge

Workspaces organize people, agents, documents, tasks, workflows, and knowledge
under one operational boundary.

## Workspaces

A workspace scopes:

- Agents and skills.
- Conversations and tasks.
- Documents and file assets.
- Knowledge sources.
- API keys and integration credentials.
- Permissions and audit history.

## Knowledge

The knowledge system indexes documents and extracted text for semantic search.
It uses PostgreSQL with pgvector for retrieval.

Typical sources:

- Uploaded documents.
- Workspace files.
- Extracted text from PDFs and office documents.
- Generated artifacts that should remain searchable.

## Data Isolation

Keep workspace boundaries meaningful. Do not mix unrelated teams or clients in
one workspace unless they intentionally share agents, documents, and memory.
