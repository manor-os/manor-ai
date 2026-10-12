---
title: Group Chat
description: Invite people, share files, and work with AI inside a room-scoped conversation.
---

# Group Chat

Group chat brings people and AI agents into one conversation. A group can be
owned personally or by the current company, but joining it does not grant
company membership or access to a [Workspace](workspaces-knowledge.md).

## Create a Group and Invite People

1. Open **Chat**, start a new message, and add one or more recipients.
2. Select **Make this a group chat**, enter a name, choose **Personal** or
   **Current company**, and select **Create group**.
3. To invite someone later, open **Group details → Members → Invite**.

Every recipient is sent an email invitation through your connected SMTP,
Gmail, or Outlook account, including existing Manor users. The default sending
account is shared with eSign and managed in **Integrations → For Agents → Email**.
Open the SMTP, Gmail, or Outlook integration card and choose **Set as default**
from the account menu to use that mailbox for invitations and eSign.
Users with a verified Manor email also receive an in-app invitation and join
only after they accept. New or unverified users must sign up if needed, verify
the invited address, then accept. If no sending account is connected or email
delivery fails, the invitation is still created; copy the displayed
invitation link and share it directly. Any member can invite others and
cancel or reissue their own pending invitations; owners and admins can
manage all pending invitations. A new member sees only messages sent after
joining; the invitation never grants company or Workspace access.

## Messages, Notifications, and Files

Type `@` to mention a member or an AI agent. You can attach images and other
files, reply to a specific message, react with an emoji, and retract your own
message. Search within **Messages**; the Chat sidebar shows unread counts.
In **Group details → Settings**, turn mention and reply notifications on or
off for yourself.

The **Files** tab lists files shared in this group, including AI-generated
files. Generated files are also stored in a private Knowledge subfolder under
**Group chat files**, named for the group and its ID. Preview or download a
file, or remove it when you have the delete control. Removing a group file
does not delete its original Knowledge document. Retracting a message does
not separately remove a file shared in that message.

## Work with AI

In **Group details → AI**, the owner or an admin can add an available
[agent](agents.md). Any member can `@` an agent for a reply, ask for a web
search about the current request, or ask it to analyze an attachment they
just sent. The group agent uses the shared conversation, not personal
Knowledge, private Workspace files, or integration credentials.

The owner can enable **AI group file access**. Once enabled, the owner or an
admin can ask AI to read, create, and manage files in this group only.
Supported generation includes documents, presentations, spreadsheets, images,
audio, and video; a completed file appears in the group. These group-file
actions do not need another per-action approval while the setting is on.
The setting does not give AI access to other groups or private data. A
Workspace agent's reply may still require approval from someone with both
group-management and Workspace-approval permissions under its
[governance rules](hitl-governance.md).

The owner can assign admins and close the group. Admins can remove regular
members; any member can invite others, and non-owners can leave the group.
