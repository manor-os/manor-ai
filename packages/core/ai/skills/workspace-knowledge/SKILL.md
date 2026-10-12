---
name: workspace-knowledge
description: Find Workspace Knowledge and artifacts, manage document attachments and policy, and read or modify files through their durable Document references.
version: 1.0.0
---

# Workspace Knowledge and Files

Use Workspace search for scoped documents and background facts. list_knowledge
shows the Workspace's configured references/policy. manor list_workspace_artifacts
lists durable outputs in its artifact folders. Tool discovery through search_tools
finds capabilities; it does not find the user's documents or inspect App configuration.

Use canonical document IDs and accessible folder references for Knowledge
attachments. create_knowledge_folder/add_knowledge_documents attach actual
documents; remove_knowledge_document detaches a reference and does not imply
deleting the underlying entity-owned file. Preserve strict/flexible Knowledge
policy and change it only when requested, using the tool's approved path.

For exact content, obtain document details and its real fs_path, then read_file.
Inspect the actual file/format before patch_file or replace_file; retain revision
or expected hash checks when provided. generate_file creates supported durable
deliverables through the file engine. A local temporary file is not a delivered
Workspace artifact until its Knowledge Document projection succeeds.

Include the relevant document references/query when handing work to a Task,
Flow or service. In strict Knowledge mode, cite evidence or state the missing
facts. Return canonical document/viewer links from tool receipts, not fabricated
paths. Permission errors, failed indexing and unavailable catalogs are unknown
or failed reads; they are not evidence that no document exists.
