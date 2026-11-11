# Manor AI — SQLAlchemy models
# Import all models so Alembic auto-detects them

from packages.core.models.base import Base, generate_ulid, TimestampMixin, SoftDeleteMixin
from packages.core.models.browser_session import BrowserSession
from packages.core.models.blueprint_upgrade import WorkspaceBlueprintUpgradeState
from packages.core.models.user import (
    Entity,
    OAuthAccount,
    User,
    UserMembership,
)
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
from packages.core.models.chat_room import ChatRoom, ChatRoomMember, ChatRoomInvite, ChatRoomMessage, ChatRoomMessageReaction, ChatRoomFile, ChatRoomFileChunk, ChatRoomAgentBinding, ChatRoomAgentRun, ChatRoomToolApproval  # noqa: F401
from packages.core.models.agent_delegation_session import AgentContextArchive, AgentDelegationSession
from packages.core.models.conversation_transcript import ConversationTranscriptItem
from packages.core.models.task_template import TaskTemplate
from packages.core.models.document import DocumentGroup, Document, DocumentFolder, DocumentGroupMember, Integration, Channel
from packages.core.models.document_version import DocumentVersion
from packages.core.models.artifact_purge import (
    WorkspaceArtifactPurgeJob as WorkspaceArtifactPurgeJob,
)
from packages.core.models.notification import Notification, NotificationOutboxEvent
from packages.core.models.audit import AuditLog
from packages.core.models.event import EventLog
from packages.core.models.people import Client
from packages.core.models.crm import ClientActivity, ClientOpportunity, ClientFollowup
from packages.core.models.client_delivery import ClientContact, ClientWorkspaceDelivery
from packages.core.models.workspace_collaboration import WorkspaceCollaboration, WorkspaceCollaborationEntry
from packages.core.models.usage import TokenUsageLog, ToolCallLog
from packages.core.models.metrics import MetricsDailyUsage, MetricsDailyToolCalls
from packages.core.models.workspace_health import MetricsWorkspaceDaily, WorkspaceHealthSnapshot
from packages.core.models.attention import AttentionItem
from packages.core.models.system_metrics import SystemMetricsSample
from packages.core.models.http_stats import HttpRequestHourly
from packages.core.models.user_session import (
    UserPageViewLog,
    UserSessionLease,
    UserSessionLog,
)
from packages.core.models.execution import ExecutionPlan, ExecutionStep
from packages.core.models.execution_claim import RuntimeExecutionClaim
from packages.core.models.hitl_request import HitlRequest
from packages.core.models.workspace_event import WorkspaceEvent
from packages.core.models.review_run import ReviewRun
from packages.core.models.consolidation_report import ConsolidationReport
from packages.core.models.proposal import ProposalRecord, ProposalItemRecord
from packages.core.models.participant import (
    ParticipantProfile,
    HumanCommitment,
    HumanContribution,
)
from packages.core.models.automation_revision import AutomationRevision
from packages.core.models.experiment import Experiment
from packages.core.models.goal import Goal, GoalMeasurement, GoalTaskLink
from packages.core.models.workspace_stat import WorkspaceStat, WorkspaceStatObservation
from packages.core.models.agent_initiative import (
    AgentInitiative,
    AgentInitiativeDelivery,
    AgentInitiativeRun,
    AgentInitiativeState,
)
from packages.core.models.scheduler import ScheduledJob, ScheduledJobRun, AgentExecution
from packages.core.models.product_growth import ProductGrowthEvent
from packages.core.models.webhook import WebhookEndpoint, WebhookDelivery
from packages.core.models.api_key import ApiKey
from packages.core.models.conversation_share import ConversationShare
from packages.core.models.site import Site, SiteEvent
from packages.core.models.code_project import CodeProject
from packages.core.models.chat_feedback import ChatMessageFeedback
from packages.core.models.skill import Skill, AgentSkillBinding
from packages.core.models.custom_field import CustomFieldDefinition
from packages.core.models.memory import AgentMemory
from packages.core.models.runtime_learning import RuntimeEvidence, RuntimeEventLog, AgentLearningCandidate
from packages.core.models.comment import Comment
from packages.core.models.quota import EntityQuota
from packages.core.models.favorite import Favorite
from packages.core.models.tag import Tag, ResourceTag
from packages.core.models.workflow import (
    WorkflowActionGrant,
    WorkflowBinding,
    WorkflowDefinition,
    WorkflowProject,
    WorkflowRun,
    WorkflowTemplateInstallation,
)
from packages.core.models.billing import SubscriptionPlan, CreditReservation, CreditUsageAllocation, CreditUsageLog, PaymentLog, Order
from packages.core.models.order import BusinessOrder, BusinessOrderItem
from packages.core.models.channel import (
    ChannelConfig,
    MessageLog,
    PhoneNumber,
    Announcement,
    AnnouncementRecipient,
    TwilioVoiceCallSession,
)
from packages.core.models.feature import Feature, FeaturePackage, EntityFeature
from packages.core.models.staff import Department, StaffRole, Staff, StaffSchedule, StaffScheduleAdjustment
from packages.core.models.mcp import AgentMCPBinding, MCPAccountToolCatalog, MCPServer
from packages.core.models.vault_audit import VaultAuditLog
from packages.core.models.worker import (
    Worker, SubscriptionWorker, WorkLease, WorkerActivityLog, CredentialSublease,
)
from packages.core.models.integration_session import IntegrationSession, WechatPersonalSession
from packages.core.models.governance import GovernancePolicy, GovernanceRevision
from packages.core.models.channel_pairing import ChannelPairingCode
from packages.core.models.blueprint import (
    BlueprintFavorite,
    WorkspaceBlueprint,
)
from packages.core.models.blueprint_registry import (
    BlueprintDistTag,
    BlueprintPackage,
    BlueprintVersion,
    WorkspaceBlueprintUpgrade,
)
from packages.core.models.marketplace_resource_link import MarketplaceResourceLink
from packages.core.models.workspace_draft import WorkspaceDraft
from packages.core.models.nango_webhook_event import NangoWebhookEvent
from packages.core.models.ai_tool_spec import CLIToolSpec, BrowserToolSpec
from packages.core.models.feature_flag import FeatureFlag, FeatureFlagOverride
from packages.core.models.platform_announcement import (
    PlatformAnnouncement,
    PlatformAnnouncementBannerDismissal,
    PlatformAnnouncementDismissal,
)
from packages.core.models.support_ticket import SupportMessage, SupportTicket
from packages.core.models.invitation_code import InvitationCode, InvitationCodeRedemption
from packages.core.models.media_job import MediaJob
from packages.core.models.apify_collection import ApifyCollection
from packages.core.models.waiting_list import WaitingListEntry
from packages.core.models.model_provider import PlatformModelProviderKey
from packages.core.models.platform_setting import PlatformSetting
from packages.core.models.permission import (
    ResourceGrant,
    ResourceGrantPending,
    Share,
    DocumentAccessLog,
    ResourceType,
    SubjectType,
    Capability,
    Visibility,
    Classification,
    GrantStatus,
    PendingStatus,
)
from packages.core.models.connection_use import IntegrationConnectionUse
from packages.core.models.oauth_provider import OAuthClientApp, OAuthAuthorizationCode, OAuthRefreshToken, PersonalAccessToken
from packages.core.models.client_error import ClientErrorEvent
from packages.core.models.tool_path_memory import ToolIntentPath
from packages.core.models.runtime_run import (
    RuntimeOutboxEvent,
    RuntimeRun,
    RuntimeRunStatus,
    SandboxInstance,
    SandboxRequesterKind,
    SandboxReservation,
    SandboxReservationStatus,
    SandboxRunner,
    SandboxRunnerStatus,
)
from packages.core.models.task_coordinator import TaskCoordinatorPhase, TaskCoordinatorRun
from packages.core.models.task_agent_child import TaskAgentChildRun
from packages.core.models.task_run_journal import TaskRunJournal, TaskRunJournalKind, TaskRunKind

__all__ = [
    "Base", "generate_ulid", "TimestampMixin", "SoftDeleteMixin",
    "WorkspaceBlueprintUpgradeState",
    "Entity", "User", "OAuthAccount", "UserMembership",
    "Workspace", "WorkspaceStaff", "Agent", "AgentSubscription", "ToolDefinition", "AgentToolBinding",
    "WorkspaceOperationDraft", "WorkspaceWorkBatch",
    "TaskCategory", "Task", "TaskLog", "Conversation", "Message",
    "AgentContextArchive", "AgentDelegationSession",
    "ConversationTranscriptItem",
    "TaskTemplate",
    "DocumentGroup", "Document", "DocumentFolder", "DocumentGroupMember", "DocumentVersion", "Integration", "Channel",
    "Notification", "NotificationOutboxEvent",
    "AuditLog",
    "EventLog",
    "Client", "ClientActivity", "ClientOpportunity", "ClientFollowup",
    "ClientContact", "ClientWorkspaceDelivery", "WorkspaceCollaboration", "WorkspaceCollaborationEntry",
    "TokenUsageLog", "ToolCallLog", "UserSessionLog", "UserSessionLease", "UserPageViewLog",
    "MetricsDailyUsage", "MetricsDailyToolCalls",
    "MetricsWorkspaceDaily", "WorkspaceHealthSnapshot",
    "AttentionItem",
    "SystemMetricsSample",
    "HttpRequestHourly",
    "ExecutionPlan", "ExecutionStep", "RuntimeExecutionClaim",
    "HitlRequest",
    "WorkspaceEvent",
    "ReviewRun",
    "ConsolidationReport",
    "ProposalRecord", "ProposalItemRecord",
    "AutomationRevision",
    "Experiment",
    "ParticipantProfile", "HumanCommitment", "HumanContribution",
    "Goal", "GoalMeasurement", "GoalTaskLink", "WorkspaceStat", "WorkspaceStatObservation",
    "AgentInitiative", "AgentInitiativeState", "AgentInitiativeRun",
    "AgentInitiativeDelivery",
    "ScheduledJob", "ScheduledJobRun", "AgentExecution", "ProductGrowthEvent",
    "WebhookEndpoint", "WebhookDelivery",
    "ApiKey",
    "ConversationShare",
    "ChatMessageFeedback",
    "Site", "SiteEvent", "CodeProject",
    "Skill",
    "CustomFieldDefinition",
    "AgentMemory",
    "RuntimeEvidence", "RuntimeEventLog", "AgentLearningCandidate",
    "Comment",
    "EntityQuota",
    "Favorite",
    "Tag", "ResourceTag",
    "WorkflowActionGrant", "WorkflowBinding", "WorkflowDefinition", "WorkflowProject", "WorkflowRun",
    "WorkflowTemplateInstallation", "MarketplaceResourceLink",
    "SubscriptionPlan", "CreditReservation", "CreditUsageAllocation", "CreditUsageLog", "PaymentLog", "Order",
    "ChannelConfig", "MessageLog", "PhoneNumber", "Announcement", "AnnouncementRecipient",
    "TwilioVoiceCallSession",
    "Feature", "FeaturePackage", "EntityFeature",
    "Department", "StaffRole", "Staff", "StaffSchedule", "StaffScheduleAdjustment",
    "BusinessOrder", "BusinessOrderItem",
    "MCPServer", "AgentMCPBinding", "MCPAccountToolCatalog",
    "AgentSkillBinding",
    "VaultAuditLog",
    "Worker", "SubscriptionWorker", "WorkLease", "WorkerActivityLog", "CredentialSublease",
    "IntegrationSession", "WechatPersonalSession",
    "GovernancePolicy", "GovernanceRevision",
    "ChannelPairingCode",
    "WorkspaceBlueprint", "BlueprintFavorite",
    "BlueprintPackage", "BlueprintVersion", "BlueprintDistTag", "WorkspaceBlueprintUpgrade",
    "WorkspaceDraft",
    "NangoWebhookEvent",
    "CLIToolSpec", "BrowserToolSpec",
    "FeatureFlag", "FeatureFlagOverride",
    "PlatformAnnouncement",
    "PlatformAnnouncementBannerDismissal",
    "PlatformAnnouncementDismissal",
    "SupportMessage",
    "SupportTicket",
    "InvitationCode", "InvitationCodeRedemption",
    "MediaJob",
    "ApifyCollection",
    "WaitingListEntry",
    "PlatformModelProviderKey",
    "PlatformSetting",
    "ResourceGrant", "ResourceGrantPending", "Share",
    "IntegrationConnectionUse",
    "DocumentAccessLog",
    "ResourceType", "SubjectType", "Capability",
    "Visibility", "Classification",
    "GrantStatus", "PendingStatus",
    "OAuthClientApp", "OAuthAuthorizationCode", "OAuthRefreshToken", "PersonalAccessToken",
    "ClientErrorEvent",
    "WebchatResidentBinding",
    "ResidentCreateGrant", "ResidentMaintenanceDraft",
    "ToolIntentPath",
    "RuntimeRun", "RuntimeRunStatus", "RuntimeFileCommit", "SandboxReservation", "SandboxReservationStatus",
    "SandboxRequesterKind",
    "SandboxRunner", "SandboxRunnerStatus", "SandboxInstance", "RuntimeOutboxEvent",
    "TaskCoordinatorPhase", "TaskCoordinatorRun", "TaskAgentChildRun",
    "TaskRunJournal", "TaskRunJournalKind", "TaskRunKind",
]

from packages.core.models.webchat_resident import WebchatResidentBinding
from packages.core.models.runtime_file_commit import RuntimeFileCommit

from packages.core.models.webchat_resident_create import ResidentCreateGrant, ResidentMaintenanceDraft

