# Manor AI OSS User Troubleshooting

> Use only visible product controls. Contact the deployment owner or official support channel when an issue cannot be resolved in the user interface.

## Contents

- [General checks](#general-checks)
- [Sign-in and page issues](#sign-in-and-page-issues)
- [Chat issues](#chat-issues)
- [Workspace access issues](#workspace-access-issues)
- [Task and Agent issues](#task-and-agent-issues)
- [Knowledge and document issues](#knowledge-and-document-issues)
- [Skill issues](#skill-issues)
- [Integration and channel issues](#integration-and-channel-issues)
- [Automation and notification issues](#automation-and-notification-issues)
- [Ask for help](#ask-for-help)

## General checks

1. Confirm the expected and actual result.
2. Refresh the page and reopen the item.
3. Check whether the action is waiting for approval or input.
4. Check whether the required Workspace, Agent, Skill, Knowledge, or Integration is available.
5. Correct the visible cause before trying again.
6. Retry once and verify the result.

## Sign-in and page issues

### Sign-in does not complete

1. Return to the sign-in screen and try again.
2. Confirm that the device date and time are correct.
3. Refresh the page.
4. Try a supported browser.
5. Ask the deployment owner for help if the issue continues.

### Page is blank or does not update

1. Refresh the page.
2. Reopen Manor AI in a new tab.
3. Confirm that the network connection is stable.
4. Try a supported browser.
5. Check whether other Manor AI pages open normally.

### A button or section is missing

1. Check whether Manor AI is in Chat mode or Workspace mode.
2. Expand Configure or More.
3. Confirm that the relevant Workspace is selected.
4. Confirm that the feature is available to the current user.
5. Ask the Workspace or deployment owner if access appears incorrect.

## Chat issues

### Chat does not respond

1. Send a short request without an attachment.
2. If it succeeds, add the file, Knowledge reference, Skill, or Integration one at a time.
3. Check whether Chat is waiting for approval.
4. Try a new conversation.
5. Ask for help if short requests also fail.

### Chat reports completion but no result appears

1. Review visible progress and action cards.
2. Open the expected destination, such as Tasks or Knowledge.
3. Check whether the action is waiting for approval.
4. Retry with a clear destination and acceptance criteria.
5. Verify the created item or artifact.

### A Skill or Agent is missing

1. Confirm that the Skill or Agent is active and available.
2. Confirm that the Skill has been added to the selected Agent.
3. Confirm that the Agent has been added to the current Workspace.
4. Refresh Chat Composer.

## Workspace access issues

### A Workspace is missing from the list

1. Confirm the Workspace was not moved to trash.
2. Ask an account owner or admin, or the Workspace owner, whether its scope is
   *Only invited members*.
3. If it is, ask to be added under Workspace → Configure → Staff → Share
   workspace.
4. If a membership was time-limited, check whether it expired — access ends at
   that point.

### A Workspace opens but changes are rejected

1. Check the assigned role: a Viewer can open a Workspace but cannot create or
   edit Tasks, Goals, or other content.
2. In an *Everyone in organization* Workspace, non-members can read only — ask
   to be added as a member to edit.
3. Ask a Workspace owner to change the role to Contributor or Editor.
4. Confirm the membership has not expired.

### Workspace settings or members cannot be changed

1. Only an account owner or admin, or a member with the Workspace Owner role,
   can change the scope, settings, or member list.
2. Ask one of those people to make the change.

## Task and Agent issues

### Task remains waiting

1. Open Task Detail.
2. Check for a scheduled time or incomplete dependency.
3. Check for Plan approval or a request for input.
4. Confirm that an Agent or person is assigned.
5. Provide the requested input and continue.

### Task needs attention

1. Open the newest request.
2. Read the proposed action or question.
3. Approve, reject, request changes, or answer.
4. Continue the Task.
5. Confirm that the next Step begins.

### Task fails

1. Find the first visible failed Step.
2. Read its message.
3. Correct the input, source material, access, or connection shown by the product.
4. Use the smallest retry action available.
5. Ask for help if the same Step fails again without a clear user action.

### Agent cannot perform an action

1. Open Agent Detail → Tools and Skills.
2. Confirm that the required capability is present.
3. Confirm that the Agent is added to the correct Workspace service.
4. Confirm that the connected application is available to that Agent.
5. Test with a small reversible action.

## Knowledge and document issues

### Upload fails

1. Check the visible file-size and file-type guidance.
2. Confirm that the file opens normally on the device.
3. Try a smaller supported file.
4. Confirm that the destination folder allows additions.

### Document is present but Chat cannot find it

1. Open the document and check its processing status.
2. Wait until it is ready.
3. Confirm that it has been added to the intended Workspace.
4. Reference it explicitly in Chat.
5. Ask a question based on a clear passage and request a source reference.

### Document does not open

1. Refresh the page.
2. Try the available preview or download action.
3. Confirm that the document has not been moved to Trash.
4. Confirm that it is still shared with the current user.

### Wiki link is missing

1. Open the source page.
2. Check the target page title.
3. Correct the link or create the missing page.
4. Reopen Wiki Map.

## Skill issues

### Skill import fails

1. Confirm that the selected package is supported.
2. Review the displayed validation message.
3. Check for a Skill with the same name.
4. Correct the package and retry.
5. Use only trusted packages.

### Skill test fails

1. Check required test fields.
2. Use a simple representative input.
3. Confirm that required capabilities are available.
4. Review the visible result.

### Skill does not affect the Agent

1. Open Agent Detail → Skills.
2. Confirm that the Skill is added.
3. Save the Agent.
4. Test in the Agent conversation.
5. Invoke it manually from Chat Composer when available.

## Integration and channel issues

### An Integration does not connect

1. Confirm that the Integration is shown as available.
2. Restart the on-screen connection flow.
3. Complete every step in the connected application.
4. Return to Manor AI and confirm the connection state.

### A connected Integration is unavailable to an Agent

1. Confirm that the Integration still shows as connected.
2. Open Agent Detail and check available Tools.
3. Open Workspace → Configure → Capabilities.
4. Add the Integration to the intended scope.
5. Test with a small reversible action.

### An incoming message reaches the wrong place

1. Open Integrations → Agent Channels.
2. Check the selected Workspace or Agent.
3. Open Workspace → Configure → Channels.
4. Correct the routing.
5. Send another test message.

## Automation and notification issues

### Automation does not run

1. Confirm that it is enabled.
2. Check the displayed timing and time zone.
3. Use Run now.
4. If Run now succeeds, review the schedule.
5. If it fails, review the visible result and correct the request.

### Automation completes without the expected result

1. Open the latest run.
2. Review its output and actions.
3. Confirm that the required Agent and Integration are available.
4. Rewrite the instruction with a clear destination and expected result.
5. Run it again and verify the outcome.

### Notification does not arrive

1. Open Notifications and confirm that the event exists.
2. Open Settings → Notifications.
3. Check event preferences and Quiet Hours.
4. Confirm that the user is assigned to or following the relevant work.

## Ask for help

Provide only:

- The page or feature name.
- The approximate time of the issue.
- The visible message.
- The steps that led to the issue.
- A screenshot with personal and business content hidden.

Do not include copied account data, conversation content, uploaded documents, or authentication information.
