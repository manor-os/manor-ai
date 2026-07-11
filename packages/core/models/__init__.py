# Manor AI — SQLAlchemy models
# Import all models so Alembic auto-detects them

from packages.core.models.base import Base, generate_ulid, TimestampMixin, SoftDeleteMixin
from packages.core.models.user import Entity, User, OAuthAccount, UserMembership
from packages.core.models.workspace import (
    Workspace,
    WorkspaceStaff,
    Agent,
    AgentSubscription,
    ToolDefinition,
    AgentToolBinding,
    WorkspaceOperationDraft,
    WorkspaceWorkBatch,
)
from packages.core.models.task import (
    TaskCategory, Task, TaskLog, Conversation, Message,
)
from packages.core.models.task_template import TaskTemplate
from packages.core.models.document import DocumentGroup, Document, DocumentFolder, DocumentGroupMember, Integration, Channel
from packages.core.models.document_version import DocumentVersion
from packages.core.models.notification import Notification
from packages.core.models.audit import AuditLog
from packages.core.models.event import EventLog
from packages.core.models.people import Client
from packages.core.models.usage import TokenUsageLog, ToolCallLog
from packages.core.models.user_session import UserPageViewLog, UserSessionLog
from packages.core.models.execution import ExecutionPlan, ExecutionStep
from packages.core.models.goal import Goal, GoalMeasurement, GoalTaskLink
from packages.core.models.scheduler import ScheduledJob, ScheduledJobRun, AgentExecution
from packages.core.models.webhook import WebhookEndpoint, WebhookDelivery
from packages.core.models.api_key import ApiKey
from packages.core.models.conversation_share import ConversationShare
from packages.core.models.chat_feedback import ChatMessageFeedback
from packages.core.models.skill import Skill, AgentSkillBinding
from packages.core.models.custom_field import CustomFieldDefinition
from packages.core.models.memory import AgentMemory
from packages.core.models.runtime_learning import RuntimeEvidence, RuntimeEventLog, AgentLearningCandidate
from packages.core.models.comment import Comment
from packages.core.models.quota import EntityQuota
from packages.core.models.favorite import Favorite
from packages.core.models.tag import Tag, ResourceTag
from packages.core.models.workflow import WorkflowDefinition, WorkflowRun
from packages.core.models.billing import SubscriptionPlan, CreditReservation, CreditUsageAllocation, CreditUsageLog, PaymentLog, Order
from packages.core.models.order import BusinessOrder, BusinessOrderItem
from packages.core.models.channel import ChannelConfig, MessageLog, PhoneNumber, Announcement, AnnouncementRecipient
from packages.core.models.feature import Feature, FeaturePackage, EntityFeature
from packages.core.models.staff import Department, StaffRole, Staff, StaffSchedule, StaffScheduleAdjustment
from packages.core.models.mcp import MCPServer, AgentMCPBinding
from packages.core.models.vault_audit import VaultAuditLog
from packages.core.models.worker import (
    Worker, SubscriptionWorker, WorkLease, WorkerActivityLog, CredentialSublease,
)
from packages.core.models.integration_session import IntegrationSession
from packages.core.models.governance import GovernancePolicy, GovernanceRevision
from packages.core.models.channel_pairing import ChannelPairingCode
from packages.core.models.blueprint import WorkspaceBlueprint
from packages.core.models.merchant import MerchantAccount
from packages.core.models.blueprint_purchase import BlueprintPurchase
from packages.core.models.workspace_draft import WorkspaceDraft
from packages.core.models.nango_webhook_event import NangoWebhookEvent
from packages.core.models.ai_tool_spec import CLIToolSpec, BrowserToolSpec
from packages.core.models.feature_flag import FeatureFlag, FeatureFlagOverride
from packages.core.models.platform_announcement import (
    PlatformAnnouncement,
    PlatformAnnouncementDismissal,
)
from packages.core.models.support_ticket import SupportMessage, SupportTicket
from packages.core.models.invitation_code import InvitationCode, InvitationCodeRedemption
from packages.core.models.media_job import MediaJob
from packages.core.models.waiting_list import WaitingListEntry
from packages.core.models.model_provider import PlatformModelProviderKey
from packages.core.models.platform_setting import PlatformSetting
from packages.core.models.permission import (
    ResourceGrant,
    ResourceGrantPending,
    Share,
    PermissionAudit,
    DocumentAccessLog,
    ResourceType,
    SubjectType,
    Capability,
    Visibility,
    Classification,
    GrantStatus,
    PendingStatus,
)
from packages.core.models.oauth_provider import OAuthClientApp, OAuthAuthorizationCode
from packages.core.models.client_error import ClientErrorEvent

__all__ = [
    "Base", "generate_ulid", "TimestampMixin", "SoftDeleteMixin",
    "Entity", "User", "OAuthAccount", "UserMembership",
    "Workspace", "WorkspaceStaff", "Agent", "AgentSubscription", "ToolDefinition", "AgentToolBinding",
    "WorkspaceOperationDraft", "WorkspaceWorkBatch",
    "TaskCategory", "Task", "TaskLog", "Conversation", "Message",
    "TaskTemplate",
    "DocumentGroup", "Document", "DocumentFolder", "DocumentGroupMember", "DocumentVersion", "Integration", "Channel",
    "Notification",
    "AuditLog",
    "EventLog",
    "Client",
    "TokenUsageLog", "ToolCallLog", "UserSessionLog", "UserPageViewLog",
    "ExecutionPlan", "ExecutionStep",
    "Goal", "GoalMeasurement", "GoalTaskLink",
    "ScheduledJob", "ScheduledJobRun", "AgentExecution",
    "WebhookEndpoint", "WebhookDelivery",
    "ApiKey",
    "ConversationShare",
    "Skill",
    "CustomFieldDefinition",
    "AgentMemory",
    "RuntimeEvidence", "RuntimeEventLog", "AgentLearningCandidate",
    "Comment",
    "EntityQuota",
    "Favorite",
    "Tag", "ResourceTag",
    "WorkflowDefinition", "WorkflowRun",
    "SubscriptionPlan", "CreditReservation", "CreditUsageAllocation", "CreditUsageLog", "PaymentLog", "Order",
    "ChannelConfig", "MessageLog", "PhoneNumber", "Announcement", "AnnouncementRecipient",
    "Feature", "FeaturePackage", "EntityFeature",
    "Department", "StaffRole", "Staff", "StaffSchedule", "StaffScheduleAdjustment",
    "BusinessOrder", "BusinessOrderItem",
    "MCPServer", "AgentMCPBinding",
    "AgentSkillBinding",
    "VaultAuditLog",
    "Worker", "SubscriptionWorker", "WorkLease", "WorkerActivityLog", "CredentialSublease",
    "IntegrationSession",
    "GovernancePolicy", "GovernanceRevision",
    "ChannelPairingCode",
    "WorkspaceBlueprint",
    "MerchantAccount",
    "BlueprintPurchase",
    "WorkspaceDraft",
    "NangoWebhookEvent",
    "CLIToolSpec", "BrowserToolSpec",
    "FeatureFlag", "FeatureFlagOverride",
    "PlatformAnnouncement",
    "PlatformAnnouncementDismissal",
    "SupportMessage",
    "SupportTicket",
    "InvitationCode", "InvitationCodeRedemption",
    "MediaJob",
    "WaitingListEntry",
    "PlatformModelProviderKey",
    "PlatformSetting",
    "ResourceGrant", "ResourceGrantPending", "Share",
    "PermissionAudit", "DocumentAccessLog",
    "ResourceType", "SubjectType", "Capability",
    "Visibility", "Classification",
    "GrantStatus", "PendingStatus",
    "OAuthClientApp", "OAuthAuthorizationCode",
    "ClientErrorEvent",
]
