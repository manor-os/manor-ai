# Manor AI OSS User Manual

> Public end-user guide for the self-hosted Manor AI edition. Labels may vary slightly by version and user access.

## Contents

- [Getting started](#getting-started)
- [Primary navigation](#primary-navigation)
- [Chat](#chat)
- [Workspaces](#workspaces)
  - [Control who can see and edit a Workspace](#control-who-can-see-and-edit-a-workspace)
- [Tasks](#tasks)
- [Agents](#agents)
- [Skills](#skills)
- [Knowledge and documents](#knowledge-and-documents)
- [Integrations and channels](#integrations-and-channels)
- [Flows and Automations](#flows-and-automations)
- [Reports, notifications, and Settings](#reports-notifications-and-settings)
- [Common workflows](#common-workflows)

## Getting started

1. Sign in to Manor AI.
2. Confirm that Chat opens and an AI model is available.
3. Confirm that the Workspace or feature you need is visible.
4. Start with a small request whose result is easy to verify.

A practical first sequence is:

1. Send a short request to Manor AI.
2. Create a small Workspace.
3. Add a test document to Knowledge.
4. Create or select an Agent.
5. Add the Agent to the Workspace.
6. Create a Task and review its progress.

## Primary navigation

Manor AI uses two navigation modes:

- Chat mode for Manor AI, Workspace conversations, and Agent conversations.
- Workspace mode for operating and configuring work.

Common Workspace-mode entries:

| Group | Entry | Purpose |
| --- | --- | --- |
| Operate | Dashboard | Review work, activity, and items needing attention |
| Operate | Tasks | Use Board, Calendar, and Automations |
| Library | Workspaces | Manage businesses, projects, and operating areas |
| Library | Knowledge | Manage files, documents, retrieval, and Wiki content |
| Configure | Agents | Create and configure Agents |
| Configure | Integrations | Connect applications and channels |
| Configure | Skills | Create, test, and add Skills |

The profile and More menus provide account options, Settings, Help, Chat History, and language choices. The bell opens Notifications.

Only use entries visible to the current user. If an entry is absent, check the selected navigation mode and Workspace access.

## Chat

### Start a conversation

Entry: Chat mode → Manor AI.

1. State the goal, relevant context, expected output, and constraints.
2. Use the attachment menu to add a local file or select a Knowledge document.
3. Type # to reference an available file.
4. Type / to select an available Skill.
5. Type @ to select an Agent or member when mentions are supported.
6. Send the request.
7. Review progress cards, approvals, and generated artifacts.

Verify the result in the relevant page. For example, confirm that a requested Task appears in Tasks or a requested document appears in Knowledge.

### Choose the right conversation

| Need | Recommended conversation |
| --- | --- |
| General research, planning, or cross-Workspace work | Manor AI |
| Work that depends on one Workspace | Workspace Chat |
| Work for one specific AI role | Agent conversation or an Agent mention |
| Visitor conversation | Public Webchat when enabled |

### Use a Skill

1. Type / and a Skill keyword in Chat Composer.
2. Select the Skill.
3. Provide the input, desired format, and acceptance criteria.
4. Send the request.
5. Review the output and resulting artifact.

If the Skill does not appear, confirm that it has been added to the selected Agent or current work context.

### Add files

- Use an attachment for a file needed only in the current request.
- Use a Knowledge reference for a document already organized in Manor AI.
- Confirm that the document is available to the selected Workspace.
- Wait for document processing to finish before expecting retrieval.

### Review an approval

1. Read the proposed action and expected effect.
2. Request a revision if the result is not ready.
3. Approve or reject.
4. Confirm that the next action begins or work stops as expected.

### Manage Chat History

Entry: More → Chat History.

1. Use the available filters.
2. Search by title or content.
3. Open a conversation to review messages and results.
4. Rename it when a clearer title is useful.
5. Confirm before deleting a conversation.

## Workspaces

### Create a Workspace

Entry: Workspaces → New workspace.

1. Describe the business, project, or operating area.
2. State the people served and primary outcome.
3. Add services, goals, Agents, members, Knowledge, channels, Rules, and Automations as prompted.
4. Review the live summary and complete missing items.
5. Edit any section that does not match the intended workflow.
6. Create the Workspace when the draft is ready.
7. Open Overview and confirm the result.

### Use Workspace Detail

- Overview shows status, goals, services, and progress.
- Configure contains Staff, Agents, Capabilities, Channels, Knowledge, Rules, Goals, Automations, and Learning.
- Activity shows important changes and execution progress.
- Settings shows options available to the current user.

### Control who can see and edit a Workspace

Access has two layers: the Workspace scope, and each member's role.

**Scope** — set under Workspace → Configure → Staff → Share workspace:

- *Only invited members* (default): only people on the member list can open it.
- *Everyone in organization*: any account in this deployment can open it.

Account owners and admins can always open every Workspace, in either scope.

**Roles** — assigned per member on the same screen:

| Role | Open it | Create and edit content | Approve work | Change settings and members |
| --- | --- | --- | --- | --- |
| Owner | Yes | Yes | Yes | Yes |
| Editor | Yes | Yes | Tasks and Goals | No |
| Contributor | Yes | Yes | No | No |
| Viewer | Yes | No | No | No |

Two rules are easy to miss:

- Being able to open a Workspace does not mean being able to change it. In an
  *Everyone in organization* Workspace, non-members can read but only members
  can create or edit Tasks, Goals, and other content.
- A membership can carry an expiry date. Once it passes, both viewing and
  editing stop.

Only an account owner or admin, or a member with the Workspace Owner role, can
change the scope or the member list. Others see the current scope but no edit
control.

### Add a member

1. Open Workspace → Configure → Staff.
2. Add an available member.
3. Select the Workspace role that matches what the person should do.
4. Set an expiry date when the access should be temporary.
5. Confirm that the member appears in assignment choices.

### Add an Agent

1. Open Workspace → Configure → Agents.
2. Select the relevant service.
3. Choose an active Agent.
4. Save.
5. Create a small Task to verify the mapping.

### Add capabilities

1. Open Workspace → Configure → Capabilities.
2. Select the intended service or Agent.
3. Add only the capabilities needed for the work.
4. Save.
5. Test with a small reversible action.

### Add Knowledge

1. Open Workspace → Configure → Knowledge.
2. Add the relevant folders or documents.
3. Select the retrieval behavior shown by the product.
4. Confirm that documents are ready.
5. Ask a test question and request a source reference.

### Add a channel

1. Connect the application under Integrations.
2. Open Workspace → Configure → Channels.
3. Choose the intended service or Agent.
4. Save the routing.
5. Send a test message.

### Use Public Webchat

When available:

1. Open Workspace channel settings.
2. Enable Public Webchat.
3. Review the content and actions available to visitors.
4. Copy the public link, QR code, or embed option.
5. Test as a visitor before sharing it.

### Remove or restore a Workspace

Review active Tasks, Knowledge, Agent mappings, and channels before removal. Use Trash or restore controls when available. Confirm carefully before permanent deletion.

## Tasks

### Understand Task views

Entry: Operate → Tasks.

- Board groups work by state.
- Calendar shows scheduled and due work.
- Automations shows repeated and scheduled work.

### Create a Task

1. Select New task.
2. Enter a specific title.
3. Describe the expected output and acceptance criteria.
4. Select a Workspace when relevant.
5. Set priority, assignment, due time, and schedule as needed.
6. Add attachments or Knowledge references.
7. Save and review the Task state.

### Track progress

1. Open Task Detail.
2. Review status, assignee, schedule, comments, and attachments.
3. Review the Plan and current Step during Agent execution.
4. Inspect generated artifacts.
5. Add a comment or requested input when needed.

### Respond when attention is required

1. Open the Task.
2. Find the newest request for input or approval.
3. Read the proposed action.
4. Answer, approve, request changes, or reject.
5. Continue the Task.
6. Confirm that the next Step begins.

### Retry failed work

1. Read the visible failure message.
2. Correct the input, access, connection, or source material.
3. Use the smallest retry action shown.
4. Confirm that progress continues.

Avoid repeated retries without changing the cause.

### Filter and organize

- Search by title or content.
- Filter by Workspace, priority, state, assignee, or due time.
- Move Board cards only to states accepted by the product.
- Refresh if another user changed the Task at the same time.

## Agents

### Create an Agent

Entry: Configure → Agents → New agent.

1. Enter the name, description, category, and visual identity.
2. Define the Agent's responsibility and expected output.
3. Add only the Tools and Skills needed for that role.
4. Test representative messages.
5. Save the Agent.
6. Add it to a Workspace service.
7. Run a small Task and review the result.

### Import an Agent

When import is available:

1. Select Import.
2. Choose a trusted Agent package.
3. Review the displayed configuration.
4. Import it.
5. Add the needed Skills and capabilities.
6. Test before regular use.

### Use Agent Detail

- Overview shows identity, state, and summary.
- Tools shows available actions.
- Skills shows reusable methods added to the Agent.
- Executions shows visible run history and results.
- Settings shows options available to the current user.

## Skills

### Create a Skill

Entry: Configure → Skills.

1. Select New skill or the guided builder.
2. Explain what the Skill should accomplish.
3. Add concrete situations in which it should be used.
4. Add instructions and supporting resources.
5. Test with representative input.
6. Save.
7. Add the Skill to the intended Agent.

### Import a Skill

When import is available:

1. Select Import.
2. Choose a trusted package.
3. Review its displayed contents and requested capabilities.
4. Complete the import.
5. Test before important use.

### Add a Skill to an Agent

1. Open Agent Detail → Skills.
2. Add the Skill.
3. Save.
4. Test it in the Agent conversation or a small Task.
5. Use / in Chat Composer for a manual invocation when available.

## Knowledge and documents

### Browse and organize

Entry: Library → Knowledge.

Use folders, breadcrumbs, search, file-type filters, Recents, Favorites, Trash, Workspace filters, grid or list view, sorting, and pagination.

### Add content

Depending on the visible options:

- Upload or drag and drop.
- Import from a supported source.
- Create a blank document.
- Create an AI-assisted draft.
- Create a Wiki page.

### Work with a document

Actions may include preview, download, rename, move, share, add to Workspace, refresh retrieval processing, edit properties, Favorite, Trash, and restore.

Available actions depend on file type and user access.

### Verify retrieval readiness

1. Open the document.
2. Check its visible processing status.
3. Wait until it is ready.
4. Add it to the intended Workspace.
5. Ask a question based on a clear passage.
6. Request a source reference.

### Use Wiki Map

1. Create or open a Wiki page.
2. Add links to related pages.
3. Open Wiki Map.
4. Review connected and missing pages.
5. Create a missing page or correct its link.

## Integrations and channels

### Connect an Integration

Entry: Configure → Integrations.

1. Browse or search the options shown.
2. Select an application.
3. Review the capabilities presented by Manor AI.
4. Follow the on-screen connection flow.
5. Confirm that it shows as connected.
6. Add it to the intended Agent or Workspace.
7. Test with a small reversible action.

The catalog varies by deployment. Use the current screen as the authority.

### Connect a communication channel

1. Open Integrations → Agent Channels.
2. Select a supported channel.
3. Follow the on-screen connection flow.
4. Choose the intended Workspace or Agent.
5. Send a test message.
6. Confirm that it appears in the correct conversation.

## Flows and Automations

### Create a Flow

Entry: Flows when visible.

1. Select New flow.
2. Choose the trigger shown.
3. Add the required steps.
4. Configure input and order.
5. Save.
6. Run with test data.
7. Review the result.

### Create an Automation

Entry: Tasks → Automations.

1. Select New automation.
2. Choose recurring, interval-based, or one-time execution.
3. Select the timing.
4. Choose Manor AI, an Agent, or Task creation as shown.
5. Write a clear instruction with an expected result.
6. Save and use Run now for a test.
7. Review Recent Runs and verify the output.

## Reports, notifications, and Settings

### Reports

When Reports is visible:

- Tasks reviews task progress and outcomes.
- Usage reviews consumption information.
- Activity reviews recorded events.
- Export options depend on the current screen.

### Notifications

1. Select the bell.
2. Follow the linked Task, approval, or event.
3. Open Settings → Notifications to adjust preferences and Quiet Hours.
4. Confirm that the desired event types are enabled.

### Settings

Common visible areas include Appearance, AI models, Calendar, File permissions, Notifications, and Security.

Use only controls presented to the current user.

## Common workflows

### From a document to a completed Task

1. Add the document to Knowledge.
2. Wait until it is ready.
3. Add it to the Workspace.
4. Choose an Agent with the needed Skill.
5. Create a Task with the document reference and acceptance criteria.
6. Respond to any request for review.
7. Verify the final artifact.

### From an incoming message to a reviewed response

1. Connect a supported channel.
2. Route it to the intended Workspace.
3. Add an Agent and the required capabilities.
4. Add relevant Knowledge.
5. Send a test message.
6. Review the proposed response.
7. Confirm the result through visible conversation history.

### From a repeated Task to an Automation

1. Complete the Task manually.
2. Identify stable inputs, steps, and expected results.
3. Create a Flow when several stages are needed.
4. Create an Automation with the desired timing.
5. Run it immediately with safe test content.
6. Verify the result.
7. Enable regular use only after the test succeeds.
