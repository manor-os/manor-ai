"""OSS initial schema

Revision ID: 0001_oss_initial
Revises:
Create Date: 2026-07-06
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0001_oss_initial"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('agent_executions',
    sa.Column('id', sa.String(length=26), nullable=False),
    sa.Column('entity_id', sa.String(length=26), nullable=True),
    sa.Column('workspace_id', sa.String(length=26), nullable=True),
    sa.Column('agent_id', sa.String(length=100), nullable=True),
    sa.Column('task_id', sa.String(length=26), nullable=True),
    sa.Column('conversation_id', sa.String(length=100), nullable=True),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('turns_used', sa.Integer(), nullable=False),
    sa.Column('max_turns', sa.Integer(), nullable=False),
    sa.Column('supervisor_verdict', sa.String(length=20), nullable=True),
    sa.Column('input_message', sa.Text(), nullable=True),
    sa.Column('output_message', sa.Text(), nullable=True),
    sa.Column('tools_used', postgresql.JSONB(astext_type=sa.Text()), server_default='[]', nullable=False),
    sa.Column('token_usage', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('duration_ms', sa.Float(), nullable=True),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('agent_learning_candidates',
    sa.Column('id', sa.String(length=26), nullable=False),
    sa.Column('entity_id', sa.String(length=26), nullable=False),
    sa.Column('workspace_id', sa.String(length=26), nullable=True),
    sa.Column('agent_id', sa.String(length=26), nullable=True),
    sa.Column('user_id', sa.String(length=26), nullable=True),
    sa.Column('candidate_type', sa.String(length=50), nullable=False),
    sa.Column('scope', sa.String(length=32), nullable=False),
    sa.Column('title', sa.String(length=255), nullable=False),
    sa.Column('summary', sa.Text(), nullable=False),
    sa.Column('payload', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
    sa.Column('evidence_ids', postgresql.JSONB(astext_type=sa.Text()), server_default='[]', nullable=False),
    sa.Column('dedupe_key', sa.String(length=255), nullable=True),
    sa.Column('risk_level', sa.String(length=20), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('confidence', sa.Float(), server_default='0.5', nullable=False),
    sa.Column('created_by', sa.String(length=50), nullable=False),
    sa.Column('applied_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('resolved_by_user_id', sa.String(length=26), nullable=True),
    sa.Column('resolution', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('agent_mcp_bindings',
    sa.Column('id', sa.String(length=26), nullable=False),
    sa.Column('agent_id', sa.String(length=26), nullable=False),
    sa.Column('mcp_server_id', sa.String(length=26), nullable=False),
    sa.Column('allowed_tools', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('config_override', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('agent_id', 'mcp_server_id', name='uq_agent_mcp_bindings_pair')
    )
    op.create_table('agent_memories',
    sa.Column('id', sa.String(length=26), nullable=False),
    sa.Column('entity_id', sa.String(length=26), nullable=False),
    sa.Column('agent_id', sa.String(length=26), nullable=True),
    sa.Column('user_id', sa.String(length=26), nullable=True),
    sa.Column('workspace_id', sa.String(length=26), nullable=True),
    sa.Column('memory_type', sa.String(length=50), nullable=False),
    sa.Column('scope', sa.String(length=32), nullable=True),
    sa.Column('content', sa.Text(), nullable=False),
    sa.Column('importance', sa.SmallInteger(), nullable=False),
    sa.Column('confidence', sa.Float(), server_default='1.0', nullable=False),
    sa.Column('source', sa.String(length=100), nullable=True),
    sa.Column('metadata', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('visibility', sa.String(length=20), server_default='entity', nullable=False),
    sa.Column('classification', sa.String(length=20), server_default='internal', nullable=False),
    sa.Column('owner_id', sa.String(length=26), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('agent_skill_bindings',
    sa.Column('id', sa.String(length=26), nullable=False),
    sa.Column('agent_id', sa.String(length=26), nullable=False),
    sa.Column('skill_id', sa.String(length=26), nullable=False),
    sa.Column('config', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('agent_id', 'skill_id', name='uq_agent_skill_bindings_pair')
    )
    op.create_table('agent_subscriptions',
    sa.Column('id', sa.String(length=26), nullable=False),
    sa.Column('entity_id', sa.String(length=26), nullable=False),
    sa.Column('agent_id', sa.String(length=26), nullable=False),
    sa.Column('workspace_id', sa.String(length=26), nullable=True),
    sa.Column('name', sa.String(length=255), nullable=True),
    sa.Column('service_key', sa.String(length=100), nullable=True),
    sa.Column('custom_prompt', sa.String(), nullable=True),
    sa.Column('config', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('agent_tool_bindings',
    sa.Column('agent_id', sa.String(length=26), nullable=False),
    sa.Column('tool_id', sa.String(length=26), nullable=False),
    sa.PrimaryKeyConstraint('agent_id', 'tool_id')
    )
    op.create_table('agents',
    sa.Column('id', sa.String(length=26), nullable=False),
    sa.Column('entity_id', sa.String(length=26), nullable=True),
    sa.Column('name', sa.String(length=255), nullable=False),
    sa.Column('slug', sa.String(length=100), nullable=True),
    sa.Column('description', sa.String(), nullable=True),
    sa.Column('avatar_url', sa.String(length=500), nullable=True),
    sa.Column('system_prompt', sa.String(), nullable=True),
    sa.Column('config', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
    sa.Column('is_template', sa.Boolean(), nullable=False),
    sa.Column('is_public', sa.Boolean(), nullable=False),
    sa.Column('category', sa.String(length=100), nullable=True),
    sa.Column('tags', postgresql.ARRAY(sa.String()), server_default='{}', nullable=False),
    sa.Column('source', sa.String(length=20), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('version', sa.String(length=20), server_default='1.0', nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('announcement_recipients',
    sa.Column('id', sa.String(length=26), nullable=False),
    sa.Column('announcement_id', sa.String(length=26), nullable=False),
    sa.Column('recipient_address', sa.String(length=500), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('error_message', sa.String(), nullable=True),
    sa.Column('sent_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('announcements',
    sa.Column('id', sa.String(length=26), nullable=False),
    sa.Column('entity_id', sa.String(length=26), nullable=False),
    sa.Column('workspace_id', sa.String(length=26), nullable=True),
    sa.Column('channel_config_id', sa.String(length=26), nullable=True),
    sa.Column('channel_type', sa.String(length=30), nullable=False),
    sa.Column('title', sa.String(length=500), nullable=True),
    sa.Column('content', sa.String(), nullable=False),
    sa.Column('template_id', sa.String(length=255), nullable=True),
    sa.Column('template_name', sa.String(length=255), nullable=True),
    sa.Column('template_language', sa.String(length=10), nullable=True),
    sa.Column('schedule_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('sent_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('recipient_count', sa.Integer(), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('error_message', sa.String(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('api_keys',
    sa.Column('id', sa.String(length=26), nullable=False),
    sa.Column('entity_id', sa.String(length=26), nullable=False),
    sa.Column('name', sa.String(length=255), nullable=False),
    sa.Column('provider', sa.String(length=50), nullable=False),
    sa.Column('key_hash', sa.String(length=255), nullable=False),
    sa.Column('key_prefix', sa.String(length=20), nullable=True),
    sa.Column('base_url', sa.String(length=500), nullable=True),
    sa.Column('default_model', sa.String(length=100), nullable=True),
    sa.Column('is_default', sa.Boolean(), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('last_used_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('usage_count', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('audit_log',
    sa.Column('id', sa.String(length=26), nullable=False),
    sa.Column('entity_id', sa.String(length=26), nullable=True),
    sa.Column('user_id', sa.String(length=26), nullable=True),
    sa.Column('action', sa.String(length=100), nullable=False),
    sa.Column('resource_type', sa.String(length=50), nullable=True),
    sa.Column('resource_id', sa.String(length=26), nullable=True),
    sa.Column('details', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
    sa.Column('ip_address', sa.String(length=128), nullable=True),
    sa.Column('user_agent', sa.String(length=500), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('blueprint_purchases',
    sa.Column('id', sa.String(length=26), nullable=False),
    sa.Column('blueprint_id', sa.String(length=26), nullable=False),
    sa.Column('buyer_entity_id', sa.String(length=26), nullable=False),
    sa.Column('buyer_user_id', sa.String(length=26), nullable=False),
    sa.Column('order_id', sa.String(length=26), nullable=True),
    sa.Column('amount_cents', sa.Integer(), nullable=False),
    sa.Column('currency', sa.String(length=10), server_default='usd', nullable=False),
    sa.Column('platform_fee_cents', sa.Integer(), server_default='0', nullable=False),
    sa.Column('seller_amount_cents', sa.Integer(), nullable=False),
    sa.Column('stripe_checkout_session_id', sa.String(length=255), nullable=True),
    sa.Column('stripe_payment_intent_id', sa.String(length=255), nullable=True),
    sa.Column('payload_snapshot', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('blueprint_title', sa.String(length=200), nullable=False),
    sa.Column('status', sa.String(length=20), server_default='pending', nullable=False),
    sa.Column('purchased_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('refunded_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('business_order_items',
    sa.Column('id', sa.String(length=26), nullable=False),
    sa.Column('order_id', sa.String(length=26), nullable=False),
    sa.Column('name', sa.String(length=255), nullable=False),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('quantity', sa.Integer(), server_default='1', nullable=False),
    sa.Column('unit_price', sa.Float(), server_default='0', nullable=False),
    sa.Column('total_price', sa.Float(), server_default='0', nullable=False),
    sa.Column('details', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('business_orders',
    sa.Column('id', sa.String(length=26), nullable=False),
    sa.Column('entity_id', sa.String(length=26), nullable=False),
    sa.Column('order_number', sa.String(length=30), nullable=False),
    sa.Column('title', sa.String(length=255), nullable=False),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('client_id', sa.String(length=26), nullable=True),
    sa.Column('assignee_id', sa.String(length=26), nullable=True),
    sa.Column('creator_id', sa.String(length=26), nullable=True),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('order_type', sa.String(length=30), nullable=False),
    sa.Column('amount', sa.Float(), server_default='0', nullable=False),
    sa.Column('currency', sa.String(length=10), server_default='USD', nullable=False),
    sa.Column('paid_amount', sa.Float(), server_default='0', nullable=False),
    sa.Column('payment_status', sa.String(length=20), server_default='unpaid', nullable=False),
    sa.Column('details', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('due_date', sa.DateTime(timezone=True), nullable=True),
    sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('order_number')
    )
    op.create_table('channel_configs',
    sa.Column('id', sa.String(length=26), nullable=False),
    sa.Column('entity_id', sa.String(length=26), nullable=False),
    sa.Column('workspace_id', sa.String(length=26), nullable=True),
    sa.Column('channel_type', sa.String(length=30), nullable=False),
    sa.Column('provider', sa.String(length=30), nullable=False),
    sa.Column('name', sa.String(length=255), nullable=True),
    sa.Column('config', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
    sa.Column('credentials', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
    sa.Column('credential_ref', sa.Text(), nullable=True),
    sa.Column('credential_scheme', sa.String(length=32), server_default='legacy_jsonb', nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('channel_contacts',
    sa.Column('id', sa.String(length=26), nullable=False),
    sa.Column('entity_id', sa.String(length=26), nullable=False),
    sa.Column('channel_config_id', sa.String(length=26), nullable=False),
    sa.Column('channel_type', sa.String(length=30), nullable=False),
    sa.Column('source_id', sa.String(length=255), nullable=False),
    sa.Column('display_name', sa.String(length=255), nullable=True),
    sa.Column('username', sa.String(length=255), nullable=True),
    sa.Column('profile', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
    sa.Column('contact_id', sa.String(length=26), nullable=True),
    sa.Column('user_id', sa.String(length=26), nullable=True),
    sa.Column('role', sa.String(length=32), nullable=False),
    sa.Column('agent_subscription_id', sa.String(length=26), nullable=True),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('last_seen_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('channel_link_tokens',
    sa.Column('id', sa.String(length=26), nullable=False),
    sa.Column('token', sa.String(length=64), nullable=False),
    sa.Column('user_id', sa.String(length=26), nullable=False),
    sa.Column('entity_id', sa.String(length=26), nullable=False),
    sa.Column('channel_type', sa.String(length=30), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('claimed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('claimed_contact_id', sa.String(length=26), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('channel_pairing_codes',
    sa.Column('code', sa.String(length=8), nullable=False),
    sa.Column('entity_id', sa.String(length=26), nullable=False),
    sa.Column('user_id', sa.String(length=26), nullable=True),
    sa.Column('workspace_id', sa.String(length=26), nullable=True),
    sa.Column('channel_type', sa.String(length=30), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('consumed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_channel_id', sa.String(length=26), nullable=True),
    sa.Column('hint', sa.String(length=255), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('code')
    )
    op.create_table('channels',
    sa.Column('id', sa.String(length=26), nullable=False),
    sa.Column('entity_id', sa.String(length=26), nullable=False),
    sa.Column('user_id', sa.String(length=26), nullable=True),
    sa.Column('workspace_id', sa.String(length=26), nullable=True),
    sa.Column('type', sa.String(length=50), nullable=False),
    sa.Column('name', sa.String(length=255), nullable=True),
    sa.Column('config', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
    sa.Column('agent_id', sa.String(length=26), nullable=True),
    sa.Column('agent_subscription_id', sa.String(length=26), nullable=True),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('chat_message_feedback',
    sa.Column('id', sa.String(length=26), nullable=False),
    sa.Column('entity_id', sa.String(length=26), nullable=False),
    sa.Column('user_id', sa.String(length=26), nullable=False),
    sa.Column('conversation_id', sa.String(length=26), nullable=False),
    sa.Column('message_id', sa.String(length=26), nullable=False),
    sa.Column('rating', sa.String(length=10), nullable=False),
    sa.Column('content_preview', sa.Text(), nullable=True),
    sa.Column('request_preview', sa.Text(), nullable=True),
    sa.Column('metadata', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('message_id', 'user_id', name='uq_chat_feedback_message_user')
    )
    op.create_table('client_error_events',
    sa.Column('id', sa.String(length=26), nullable=False),
    sa.Column('entity_id', sa.String(length=26), nullable=True),
    sa.Column('user_id', sa.String(length=26), nullable=True),
    sa.Column('source', sa.String(length=40), server_default='web', nullable=False),
    sa.Column('level', sa.String(length=20), server_default='error', nullable=False),
    sa.Column('handled', sa.Boolean(), server_default='false', nullable=False),
    sa.Column('name', sa.String(length=120), nullable=True),
    sa.Column('message', sa.Text(), nullable=False),
    sa.Column('stack', sa.Text(), nullable=True),
    sa.Column('component_stack', sa.Text(), nullable=True),
    sa.Column('fingerprint', sa.String(length=96), nullable=False),
    sa.Column('route', sa.String(length=500), nullable=True),
    sa.Column('url', sa.Text(), nullable=True),
    sa.Column('release', sa.String(length=120), nullable=True),
    sa.Column('environment', sa.String(length=80), nullable=True),
    sa.Column('request_id', sa.String(length=80), nullable=True),
    sa.Column('tags', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
    sa.Column('extra', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
    sa.Column('context', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
    sa.Column('ip_address', sa.String(length=128), nullable=True),
    sa.Column('user_agent', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('clients',
    sa.Column('id', sa.String(length=26), nullable=False),
    sa.Column('entity_id', sa.String(length=26), nullable=False),
    sa.Column('name', sa.String(length=255), nullable=False),
    sa.Column('email', sa.String(length=255), nullable=True),
    sa.Column('phone', sa.String(length=50), nullable=True),
    sa.Column('address', sa.String(), nullable=True),
    sa.Column('metadata', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('comments',
    sa.Column('id', sa.String(length=26), nullable=False),
    sa.Column('entity_id', sa.String(length=26), nullable=False),
    sa.Column('resource_type', sa.String(length=50), nullable=False),
    sa.Column('resource_id', sa.String(length=26), nullable=False),
    sa.Column('parent_id', sa.String(length=26), nullable=True),
    sa.Column('user_id', sa.String(length=26), nullable=False),
    sa.Column('user_email', sa.String(length=255), nullable=True),
    sa.Column('content', sa.Text(), nullable=False),
    sa.Column('mentions', postgresql.JSONB(astext_type=sa.Text()), server_default='[]', nullable=False),
    sa.Column('anchor', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
    sa.Column('reactions', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
    sa.Column('is_edited', sa.Boolean(), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('conversation_shares',
    sa.Column('id', sa.String(length=26), nullable=False),
    sa.Column('conversation_id', sa.String(length=26), nullable=False),
    sa.Column('entity_id', sa.String(length=26), nullable=False),
    sa.Column('shared_by', sa.String(length=26), nullable=False),
    sa.Column('share_token', sa.String(length=64), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('is_active', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('share_token')
    )
    op.create_table('conversations',
    sa.Column('id', sa.String(length=26), nullable=False),
    sa.Column('entity_id', sa.String(length=26), nullable=False),
    sa.Column('user_id', sa.String(length=26), nullable=True),
    sa.Column('agent_id', sa.String(length=26), nullable=True),
    sa.Column('agent_subscription_id', sa.String(length=26), nullable=True),
    sa.Column('workspace_id', sa.String(length=26), nullable=True),
    sa.Column('title', sa.String(length=500), nullable=True),
    sa.Column('summary', sa.Text(), nullable=True),
    sa.Column('channel', sa.String(length=50), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('metadata', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
    sa.Column('scope', sa.String(length=20), server_default='channel', nullable=False),
    sa.Column('thread_ref_kind', sa.String(length=16), nullable=True),
    sa.Column('thread_ref_id', sa.String(length=26), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('credential_subleases',
    sa.Column('id', sa.String(length=26), nullable=False),
    sa.Column('work_lease_id', sa.String(length=26), nullable=False),
    sa.Column('integration_id', sa.String(length=26), nullable=False),
    sa.Column('vault_lease_id', sa.String(length=255), nullable=True),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('revocation_reason', sa.String(length=64), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('custom_field_definitions',
    sa.Column('id', sa.String(length=26), nullable=False),
    sa.Column('entity_id', sa.String(length=26), nullable=False),
    sa.Column('workspace_id', sa.String(length=26), nullable=True),
    sa.Column('name', sa.String(length=100), nullable=False),
    sa.Column('display_name', sa.String(length=255), nullable=False),
    sa.Column('field_type', sa.String(length=50), nullable=False),
    sa.Column('target', sa.String(length=50), nullable=False),
    sa.Column('options', postgresql.JSONB(astext_type=sa.Text()), server_default='[]', nullable=False),
    sa.Column('default_value', sa.String(length=500), nullable=True),
    sa.Column('required', sa.Boolean(), nullable=False),
    sa.Column('sort_order', sa.SmallInteger(), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('departments',
    sa.Column('id', sa.String(length=26), nullable=False),
    sa.Column('entity_id', sa.String(length=26), nullable=False),
    sa.Column('name', sa.String(length=255), nullable=False),
    sa.Column('parent_id', sa.String(length=26), nullable=True),
    sa.Column('description', sa.String(length=500), nullable=True),
    sa.Column('sort_order', sa.Integer(), server_default='0', nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['parent_id'], ['departments.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('document_access_log',
    sa.Column('id', sa.String(length=26), nullable=False),
    sa.Column('ts', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('entity_id', sa.String(length=26), nullable=False),
    sa.Column('document_id', sa.String(length=26), nullable=False),
    sa.Column('workspace_id', sa.String(length=26), nullable=True),
    sa.Column('actor_type', sa.String(length=20), nullable=False),
    sa.Column('actor_id', sa.String(length=120), nullable=True),
    sa.Column('action', sa.String(length=40), nullable=False),
    sa.Column('classification_at_access', sa.String(length=20), nullable=True),
    sa.Column('ip', sa.String(length=45), nullable=True),
    sa.Column('user_agent', sa.Text(), nullable=True),
    sa.Column('share_id', sa.String(length=26), nullable=True),
    sa.Column('agent_session_id', sa.String(length=80), nullable=True),
    sa.Column('redacted', sa.Boolean(), server_default='false', nullable=False),
    sa.Column('watermark_id', sa.String(length=80), nullable=True),
    sa.Column('context', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('document_folders',
    sa.Column('id', sa.String(length=26), nullable=False),
    sa.Column('entity_id', sa.String(length=26), nullable=False),
    sa.Column('name', sa.String(length=255), nullable=False),
    sa.Column('parent_id', sa.String(length=26), nullable=True),
    sa.Column('visibility', sa.String(length=20), server_default='entity', nullable=False),
    sa.Column('classification', sa.String(length=20), server_default='internal', nullable=False),
    sa.Column('owner_id', sa.String(length=26), nullable=True),
    sa.Column('client_visible', sa.Boolean(), server_default='false', nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('document_group_members',
    sa.Column('document_id', sa.String(length=26), nullable=False),
    sa.Column('group_id', sa.String(length=26), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('document_id', 'group_id')
    )
    op.create_table('document_groups',
    sa.Column('id', sa.String(length=26), nullable=False),
    sa.Column('entity_id', sa.String(length=26), nullable=False),
    sa.Column('name', sa.String(length=255), nullable=False),
    sa.Column('workspace_id', sa.String(length=26), nullable=True),
    sa.Column('vector_store_id', sa.String(length=255), nullable=True),
    sa.Column('settings', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('document_versions',
    sa.Column('id', sa.String(length=26), nullable=False),
    sa.Column('document_id', sa.String(length=26), nullable=False),
    sa.Column('version_number', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=500), nullable=False),
    sa.Column('fs_path', sa.String(length=1000), nullable=True),
    sa.Column('file_size', sa.BigInteger(), nullable=True),
    sa.Column('change_summary', sa.String(length=500), nullable=True),
    sa.Column('created_by', sa.String(length=100), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('documents',
    sa.Column('id', sa.String(length=26), nullable=False),
    sa.Column('entity_id', sa.String(length=26), nullable=False),
    sa.Column('name', sa.String(length=500), nullable=False),
    sa.Column('fs_path', sa.String(length=1000), nullable=True),
    sa.Column('file_url', sa.String(length=1000), nullable=True),
    sa.Column('file_size', sa.BigInteger(), nullable=True),
    sa.Column('file_type', sa.String(length=20), nullable=True),
    sa.Column('mime_type', sa.String(length=100), nullable=True),
    sa.Column('vector_status', sa.String(length=20), nullable=False),
    sa.Column('source', sa.String(length=20), nullable=False),
    sa.Column('metadata', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
    sa.Column('created_by', sa.String(length=100), nullable=True),
    sa.Column('folder_id', sa.String(length=26), nullable=True),
    sa.Column('is_trashed', sa.Boolean(), server_default='false', nullable=False),
    sa.Column('trashed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('trashed_by', sa.String(length=100), nullable=True),
    sa.Column('visibility', sa.String(length=20), server_default='entity', nullable=False),
    sa.Column('classification', sa.String(length=20), server_default='internal', nullable=False),
    sa.Column('owner_id', sa.String(length=26), nullable=True),
    sa.Column('client_visible', sa.Boolean(), server_default='false', nullable=False),
    sa.Column('legal_hold', sa.Boolean(), server_default='false', nullable=False),
    sa.Column('legal_hold_reason', sa.Text(), nullable=True),
    sa.Column('legal_hold_set_by', sa.String(length=26), nullable=True),
    sa.Column('legal_hold_set_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('pii_detected', sa.Boolean(), server_default='false', nullable=False),
    sa.Column('quarantine_status', sa.String(length=20), server_default='clean', nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('entities',
    sa.Column('id', sa.String(length=26), nullable=False),
    sa.Column('name', sa.String(length=255), nullable=False),
    sa.Column('slug', sa.String(length=100), nullable=True),
    sa.Column('address', sa.String(), nullable=True),
    sa.Column('phone', sa.String(length=50), nullable=True),
    sa.Column('email', sa.String(length=100), nullable=True),
    sa.Column('logo_url', sa.String(length=500), nullable=True),
    sa.Column('llm_model', sa.String(length=100), nullable=True),
    sa.Column('settings', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
    sa.Column('plan_id', sa.String(length=26), server_default='plan_free', nullable=True),
    sa.Column('stripe_customer_id', sa.String(length=255), nullable=True),
    sa.Column('trial_ends_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('slug')
    )
    op.create_table('entity_quotas',
    sa.Column('id', sa.String(length=26), nullable=False),
    sa.Column('entity_id', sa.String(length=26), nullable=False),
    sa.Column('plan_name', sa.String(length=100), server_default='free', nullable=False),
    sa.Column('max_users', sa.Integer(), server_default='5', nullable=False),
    sa.Column('max_agents', sa.Integer(), server_default='3', nullable=False),
    sa.Column('max_documents', sa.Integer(), server_default='100', nullable=False),
    sa.Column('max_storage_bytes', sa.BigInteger(), server_default='1073741824', nullable=False),
    sa.Column('max_tokens_monthly', sa.BigInteger(), server_default='1000000', nullable=False),
    sa.Column('max_api_calls_daily', sa.Integer(), server_default='10000', nullable=False),
    sa.Column('tokens_used_this_month', sa.BigInteger(), server_default='0', nullable=False),
    sa.Column('api_calls_today', sa.Integer(), server_default='0', nullable=False),
    sa.Column('storage_used_bytes', sa.BigInteger(), server_default='0', nullable=False),
    sa.Column('current_period_start', sa.Date(), nullable=True),
    sa.Column('last_daily_reset', sa.Date(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('entity_id')
    )
    op.create_table('event_logs',
    sa.Column('id', sa.String(length=26), nullable=False),
    sa.Column('entity_id', sa.String(length=26), nullable=True),
    sa.Column('event_type', sa.String(length=100), nullable=False),
    sa.Column('source', sa.String(length=100), nullable=True),
    sa.Column('payload', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('execution_plans',
    sa.Column('id', sa.String(length=26), nullable=False),
    sa.Column('entity_id', sa.String(length=26), nullable=False),
    sa.Column('workspace_id', sa.String(length=26), nullable=True),
    sa.Column('task_id', sa.String(length=26), nullable=True),
    sa.Column('agent_subscription_id', sa.String(length=26), nullable=True),
    sa.Column('plan_dag', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
    sa.Column('planner_version', sa.String(length=32), nullable=True),
    sa.Column('parent_plan_id', sa.String(length=26), nullable=True),
    sa.Column('status', sa.String(length=24), nullable=False),
    sa.Column('approval_required', sa.Boolean(), server_default='false', nullable=False),
    sa.Column('execution_mode', sa.String(length=16), server_default='live', nullable=False),
    sa.Column('cost_tracking', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
    sa.Column('evaluation', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('dispatcher_state', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
    sa.Column('last_error', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('execution_steps',
    sa.Column('id', sa.String(length=26), nullable=False),
    sa.Column('plan_id', sa.String(length=26), nullable=False),
    sa.Column('entity_id', sa.String(length=26), nullable=False),
    sa.Column('workspace_id', sa.String(length=26), nullable=True),
    sa.Column('step_key', sa.String(length=64), nullable=False),
    sa.Column('kind', sa.String(length=20), nullable=False),
    sa.Column('service_key', sa.String(length=100), nullable=True),
    sa.Column('resolved_subscription_id', sa.String(length=26), nullable=True),
    sa.Column('resolved_agent_id', sa.String(length=26), nullable=True),
    sa.Column('provider', sa.String(length=64), nullable=True),
    sa.Column('action_key', sa.String(length=64), nullable=True),
    sa.Column('capability_id', sa.String(length=80), nullable=True),
    sa.Column('integration_id', sa.String(length=26), nullable=True),
    sa.Column('params', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
    sa.Column('expected_input_schema', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('expected_output_schema', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('result', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('evidence_refs', postgresql.JSONB(astext_type=sa.Text()), server_default='[]', nullable=False),
    sa.Column('depends_on', postgresql.JSONB(astext_type=sa.Text()), server_default='[]', nullable=False),
    sa.Column('step_status', sa.String(length=20), server_default='pending', nullable=False),
    sa.Column('attempt_count', sa.Integer(), nullable=False),
    sa.Column('max_attempts', sa.Integer(), nullable=False),
    sa.Column('risk_level', sa.String(length=8), server_default='low', nullable=False),
    sa.Column('requires_approval', sa.Boolean(), server_default='false', nullable=False),
    sa.Column('current_lease_id', sa.String(length=26), nullable=True),
    sa.Column('human_input_prompt', sa.Text(), nullable=True),
    sa.Column('human_input_response', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('cost', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
    sa.Column('error', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('plan_id', 'step_key', name='uq_steps_plan_key')
    )
    op.create_table('favorites',
    sa.Column('id', sa.String(length=26), nullable=False),
    sa.Column('entity_id', sa.String(length=26), nullable=False),
    sa.Column('user_id', sa.String(length=26), nullable=False),
    sa.Column('resource_type', sa.String(length=50), nullable=False),
    sa.Column('resource_id', sa.String(length=26), nullable=False),
    sa.Column('favorite_type', sa.String(length=20), nullable=False),
    sa.Column('note', sa.String(length=500), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('user_id', 'resource_type', 'resource_id', 'favorite_type', name='uq_favorite_user_resource')
    )
    op.create_table('feature_flag_overrides',
    sa.Column('id', sa.String(length=26), nullable=False),
    sa.Column('flag_key', sa.String(length=80), nullable=False),
    sa.Column('scope', sa.String(length=20), nullable=False),
    sa.Column('scope_id', sa.String(length=64), nullable=False),
    sa.Column('enabled', sa.Boolean(), nullable=False),
    sa.Column('set_by_admin_id', sa.String(length=26), nullable=True),
    sa.Column('set_reason', sa.Text(), nullable=True),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('flag_key', 'scope', 'scope_id', name='uq_feature_flag_overrides_target')
    )
    op.create_table('feature_flags',
    sa.Column('id', sa.String(length=26), nullable=False),
    sa.Column('key', sa.String(length=80), nullable=False),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('default_enabled', sa.Boolean(), server_default='false', nullable=False),
    sa.Column('status', sa.String(length=20), server_default='active', nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('key')
    )
    op.create_table('goal_measurements',
    sa.Column('goal_id', sa.String(length=26), nullable=False),
    sa.Column('measured_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('value', sa.Numeric(precision=20, scale=4), nullable=False),
    sa.Column('source', sa.String(length=64), nullable=True),
    sa.Column('meta', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.PrimaryKeyConstraint('goal_id', 'measured_at')
    )
    op.create_table('goal_task_links',
    sa.Column('goal_id', sa.String(length=26), nullable=False),
    sa.Column('task_id', sa.String(length=26), nullable=False),
    sa.Column('contribution', sa.String(length=16), nullable=False),
    sa.Column('estimated_impact', sa.Numeric(precision=20, scale=4), nullable=True),
    sa.Column('actual_impact', sa.Numeric(precision=20, scale=4), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('goal_id', 'task_id')
    )
    op.create_table('goals',
    sa.Column('id', sa.String(length=26), nullable=False),
    sa.Column('entity_id', sa.String(length=26), nullable=False),
    sa.Column('workspace_id', sa.String(length=26), nullable=True),
    sa.Column('title', sa.String(length=255), nullable=False),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('metric_key', sa.String(length=100), nullable=False),
    sa.Column('target_value', sa.Numeric(precision=20, scale=4), nullable=False),
    sa.Column('baseline_value', sa.Numeric(precision=20, scale=4), nullable=True),
    sa.Column('current_value', sa.Numeric(precision=20, scale=4), nullable=True),
    sa.Column('current_value_updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('deadline', sa.Date(), nullable=True),
    sa.Column('pace_status', sa.String(length=20), nullable=True),
    sa.Column('pace_computed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('measurement_source', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('measurement_cadence', sa.String(length=64), nullable=True),
    sa.Column('priority', sa.SmallInteger(), nullable=False),
    sa.Column('outcome_window_days', sa.SmallInteger(), server_default='7', nullable=False),
    sa.Column('achieved_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('governance_policies',
    sa.Column('workspace_id', sa.String(length=26), nullable=False),
    sa.Column('entity_id', sa.String(length=26), nullable=False),
    sa.Column('policy', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
    sa.Column('revision', sa.Integer(), server_default='1', nullable=False),
    sa.Column('updated_by', sa.String(length=26), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('workspace_id')
    )
    op.create_table('governance_revisions',
    sa.Column('id', sa.String(length=26), nullable=False),
    sa.Column('workspace_id', sa.String(length=26), nullable=False),
    sa.Column('revision', sa.Integer(), nullable=False),
    sa.Column('policy', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('change_summary', sa.String(length=500), nullable=True),
    sa.Column('changed_by', sa.String(length=26), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('workspace_id', 'revision', name='uq_governance_revisions_workspace_revision')
    )
    op.create_table('integration_sessions',
    sa.Column('id', sa.String(length=26), nullable=False),
    sa.Column('entity_id', sa.String(length=26), nullable=False),
    sa.Column('provider', sa.String(length=50), nullable=False),
    sa.Column('label', sa.String(length=100), nullable=True),
    sa.Column('session_state_ref', sa.Text(), nullable=True),
    sa.Column('credential_scheme', sa.String(length=32), server_default='vault_transit', nullable=False),
    sa.Column('status', sa.String(length=20), server_default='pending', nullable=False),
    sa.Column('last_validated_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('validated_steps', sa.Integer(), server_default='0', nullable=False),
    sa.Column('expired_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('expired_reason', sa.String(length=200), nullable=True),
    sa.Column('health_check', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
    sa.Column('metadata_json', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('entity_id', 'provider', 'label', name='uq_integration_sessions_label')
    )
    op.create_table('integrations',
    sa.Column('id', sa.String(length=26), nullable=False),
    sa.Column('entity_id', sa.String(length=26), nullable=False),
    sa.Column('provider', sa.String(length=50), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('config', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
    sa.Column('credentials', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
    sa.Column('credential_ref', sa.Text(), nullable=True),
    sa.Column('credential_scheme', sa.String(length=32), server_default='legacy_jsonb', nullable=False),
    sa.Column('required_permission', sa.String(length=64), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('invitation_code_redemptions',
    sa.Column('id', sa.String(length=26), nullable=False),
    sa.Column('code', sa.String(length=64), nullable=False),
    sa.Column('user_id', sa.String(length=26), nullable=False),
    sa.Column('entity_id', sa.String(length=26), nullable=False),
    sa.Column('redeemed_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('invitation_codes',
    sa.Column('code', sa.String(length=64), nullable=False),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('max_uses', sa.Integer(), nullable=True),
    sa.Column('uses', sa.Integer(), server_default='0', nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_by_admin_id', sa.String(length=26), nullable=True),
    sa.Column('assign_role', sa.String(length=20), nullable=True),
    sa.Column('assign_plan_id', sa.String(length=26), nullable=True),
    sa.Column('bonus_credits', sa.BigInteger(), server_default='0', nullable=False),
    sa.Column('status', sa.String(length=20), server_default='active', nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('code')
    )
    op.create_table('mcp_servers',
    sa.Column('id', sa.String(length=26), nullable=False),
    sa.Column('server_key', sa.String(length=64), nullable=False),
    sa.Column('name', sa.String(length=120), nullable=False),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('transport', sa.String(length=16), nullable=False),
    sa.Column('endpoint', sa.String(length=500), nullable=True),
    sa.Column('auth_type', sa.String(length=16), nullable=False),
    sa.Column('scopes', sa.String(length=500), nullable=True),
    sa.Column('tools_cached', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
    sa.Column('tools_cached_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('default_config', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
    sa.Column('default_allowed_tools', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('credential_ref', sa.Text(), nullable=True),
    sa.Column('credential_scheme', sa.String(length=32), nullable=True),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('server_key')
    )
    op.create_table('media_jobs',
    sa.Column('id', sa.String(length=26), nullable=False),
    sa.Column('entity_id', sa.String(length=26), nullable=False),
    sa.Column('user_id', sa.String(length=26), nullable=True),
    sa.Column('agent_id', sa.String(length=26), nullable=True),
    sa.Column('conversation_id', sa.String(length=26), nullable=True),
    sa.Column('kind', sa.String(length=20), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('prompt', sa.Text(), nullable=False),
    sa.Column('model', sa.String(length=100), nullable=True),
    sa.Column('params', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
    sa.Column('result_url', sa.Text(), nullable=True),
    sa.Column('source_url', sa.Text(), nullable=True),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('file_size', sa.Integer(), nullable=True),
    sa.Column('duration_seconds', sa.Integer(), nullable=True),
    sa.Column('cost_usd', sa.Float(), nullable=True),
    sa.Column('credits', sa.Integer(), nullable=True),
    sa.Column('byok', sa.Boolean(), nullable=False),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('merchant_accounts',
    sa.Column('id', sa.String(length=26), nullable=False),
    sa.Column('entity_id', sa.String(length=26), nullable=False),
    sa.Column('stripe_account_id', sa.String(length=255), nullable=False),
    sa.Column('onboarding_status', sa.String(length=20), server_default='pending', nullable=False),
    sa.Column('charges_enabled', sa.Boolean(), server_default='false', nullable=False),
    sa.Column('payouts_enabled', sa.Boolean(), server_default='false', nullable=False),
    sa.Column('country', sa.String(length=2), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('message_logs',
    sa.Column('id', sa.String(length=26), nullable=False),
    sa.Column('entity_id', sa.String(length=26), nullable=False),
    sa.Column('channel_config_id', sa.String(length=26), nullable=True),
    sa.Column('conversation_id', sa.String(length=26), nullable=True),
    sa.Column('direction', sa.String(length=10), nullable=False),
    sa.Column('channel_type', sa.String(length=30), nullable=False),
    sa.Column('from_address', sa.String(length=500), nullable=True),
    sa.Column('to_address', sa.String(length=500), nullable=True),
    sa.Column('subject', sa.String(length=1000), nullable=True),
    sa.Column('content', sa.String(), nullable=True),
    sa.Column('html_content', sa.String(), nullable=True),
    sa.Column('attachments', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('external_id', sa.String(length=255), nullable=True),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('error_message', sa.String(), nullable=True),
    sa.Column('cost_amount', sa.Numeric(precision=10, scale=4), nullable=True),
    sa.Column('cost_currency', sa.String(length=3), nullable=True),
    sa.Column('duration_seconds', sa.Integer(), nullable=True),
    sa.Column('recording_url', sa.String(length=1000), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('messages',
    sa.Column('id', sa.String(length=26), nullable=False),
    sa.Column('conversation_id', sa.String(length=26), nullable=False),
    sa.Column('role', sa.String(length=20), nullable=False),
    sa.Column('content', sa.String(), nullable=True),
    sa.Column('tool_calls', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('attachments', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('token_usage', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('metadata', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('author_kind', sa.String(length=16), server_default='user', nullable=False),
    sa.Column('author_subscription_id', sa.String(length=26), nullable=True),
    sa.Column('message_kind', sa.String(length=32), server_default='text', nullable=False),
    sa.Column('refs', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('pending_action', postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), nullable=True),
    sa.Column('resolved_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('resolved_by_user_id', sa.String(length=26), nullable=True),
    sa.Column('resolution', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('nango_webhook_events',
    sa.Column('id', sa.String(length=26), nullable=False),
    sa.Column('received_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('nango_type', sa.String(length=40), nullable=False),
    sa.Column('provider', sa.String(length=64), nullable=True),
    sa.Column('provider_config_key', sa.String(length=64), nullable=True),
    sa.Column('connection_id', sa.String(length=120), nullable=True),
    sa.Column('entity_id', sa.String(length=26), nullable=True),
    sa.Column('integration_id', sa.String(length=26), nullable=True),
    sa.Column('processing_status', sa.String(length=20), server_default='received', nullable=False),
    sa.Column('processing_detail', sa.Text(), nullable=True),
    sa.Column('payload', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('notifications',
    sa.Column('id', sa.String(length=26), nullable=False),
    sa.Column('entity_id', sa.String(length=26), nullable=False),
    sa.Column('user_id', sa.String(length=26), nullable=False),
    sa.Column('type', sa.String(length=50), nullable=False),
    sa.Column('title', sa.String(length=500), nullable=True),
    sa.Column('content', sa.String(), nullable=True),
    sa.Column('metadata', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
    sa.Column('read_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('deliver_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('dispatch_status', sa.String(length=20), server_default='dispatched', nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('oauth_accounts',
    sa.Column('id', sa.String(length=26), nullable=False),
    sa.Column('user_id', sa.String(length=26), nullable=False),
    sa.Column('provider', sa.String(length=50), nullable=False),
    sa.Column('provider_user_id', sa.String(length=255), nullable=False),
    sa.Column('access_token', sa.String(), nullable=True),
    sa.Column('refresh_token', sa.String(), nullable=True),
    sa.Column('token_expires_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('profile', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('oauth_authorization_codes',
    sa.Column('id', sa.String(length=26), nullable=False),
    sa.Column('code', sa.String(length=64), nullable=False),
    sa.Column('client_id', sa.String(length=64), nullable=False),
    sa.Column('user_id', sa.String(length=26), nullable=False),
    sa.Column('entity_id', sa.String(length=26), nullable=False),
    sa.Column('redirect_uri', sa.String(length=2048), nullable=False),
    sa.Column('scope', postgresql.JSONB(astext_type=sa.Text()), server_default='[]', nullable=False),
    sa.Column('state', sa.String(length=255), nullable=True),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('redeemed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('code')
    )
    op.create_table('oauth_client_apps',
    sa.Column('id', sa.String(length=26), nullable=False),
    sa.Column('client_id', sa.String(length=64), nullable=False),
    sa.Column('client_secret_hash', sa.String(length=255), nullable=False),
    sa.Column('name', sa.String(length=255), nullable=False),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('redirect_uris', postgresql.JSONB(astext_type=sa.Text()), server_default='[]', nullable=False),
    sa.Column('allowed_scopes', postgresql.JSONB(astext_type=sa.Text()), server_default='["openid","profile","email"]', nullable=False),
    sa.Column('restricted_entity_id', sa.String(length=26), nullable=True),
    sa.Column('active', sa.Boolean(), server_default='true', nullable=False),
    sa.Column('access_token_ttl_minutes', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('client_id')
    )
    op.create_table('permission_audit',
    sa.Column('id', sa.String(length=26), nullable=False),
    sa.Column('ts', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('entity_id', sa.String(length=26), nullable=True),
    sa.Column('actor_type', sa.String(length=20), nullable=False),
    sa.Column('actor_id', sa.String(length=120), nullable=True),
    sa.Column('action', sa.String(length=80), nullable=False),
    sa.Column('resource_type', sa.String(length=40), nullable=True),
    sa.Column('resource_id', sa.String(length=26), nullable=True),
    sa.Column('decision', sa.String(length=10), nullable=False),
    sa.Column('reason', sa.String(length=120), nullable=True),
    sa.Column('request_id', sa.String(length=80), nullable=True),
    sa.Column('ip', sa.String(length=45), nullable=True),
    sa.Column('user_agent', sa.Text(), nullable=True),
    sa.Column('context', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('phone_numbers',
    sa.Column('id', sa.String(length=26), nullable=False),
    sa.Column('entity_id', sa.String(length=26), nullable=False),
    sa.Column('phone_number', sa.String(length=30), nullable=False),
    sa.Column('provider', sa.String(length=30), nullable=False),
    sa.Column('provider_id', sa.String(length=255), nullable=True),
    sa.Column('capabilities', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
    sa.Column('monthly_cost', sa.Numeric(precision=10, scale=2), nullable=True),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('platform_announcement_audiences',
    sa.Column('announcement_id', sa.String(length=26), nullable=False),
    sa.Column('term', sa.String(length=80), nullable=False),
    sa.PrimaryKeyConstraint('announcement_id', 'term', name='pk_platform_announcement_audiences')
    )
    op.create_table('platform_announcement_dismissals',
    sa.Column('announcement_id', sa.String(length=26), nullable=False),
    sa.Column('user_id', sa.String(length=26), nullable=False),
    sa.Column('dismissed_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('announcement_id', 'user_id', name='pk_platform_announcement_dismissals')
    )
    op.create_table('platform_announcements',
    sa.Column('id', sa.String(length=26), nullable=False),
    sa.Column('title', sa.String(length=200), nullable=False),
    sa.Column('body_md', sa.Text(), nullable=False),
    sa.Column('severity', sa.String(length=20), server_default='info', nullable=False),
    sa.Column('audience', sa.String(length=80), server_default='all', nullable=False),
    sa.Column('show_in_app', sa.Boolean(), server_default='true', nullable=False),
    sa.Column('send_email', sa.Boolean(), server_default='false', nullable=False),
    sa.Column('starts_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('ends_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_by_admin_id', sa.String(length=26), nullable=True),
    sa.Column('status', sa.String(length=20), server_default='active', nullable=False),
    sa.Column('email_sent_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('platform_model_provider_keys',
    sa.Column('id', sa.String(length=26), nullable=False),
    sa.Column('provider', sa.String(length=40), nullable=False),
    sa.Column('display_name', sa.String(length=100), nullable=True),
    sa.Column('status', sa.String(length=20), server_default='active', nullable=False),
    sa.Column('config', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
    sa.Column('credential_ref', sa.Text(), nullable=True),
    sa.Column('credential_scheme', sa.String(length=32), server_default='legacy_jsonb', nullable=False),
    sa.Column('created_by', sa.String(length=26), nullable=True),
    sa.Column('updated_by', sa.String(length=26), nullable=True),
    sa.Column('last_rotated_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('platform_settings',
    sa.Column('id', sa.String(length=26), nullable=False),
    sa.Column('key', sa.String(length=64), nullable=False),
    sa.Column('value', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
    sa.Column('updated_by', sa.String(length=26), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('resource_grants',
    sa.Column('id', sa.String(length=26), nullable=False),
    sa.Column('entity_id', sa.String(length=26), nullable=False),
    sa.Column('resource_type', sa.String(length=40), nullable=False),
    sa.Column('resource_id', sa.String(length=26), nullable=False),
    sa.Column('subject_type', sa.String(length=40), nullable=False),
    sa.Column('subject_id', sa.String(length=120), nullable=False),
    sa.Column('capabilities', postgresql.ARRAY(sa.String(length=40)), server_default='{}', nullable=False),
    sa.Column('granted_by', sa.String(length=26), nullable=True),
    sa.Column('granted_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('status', sa.String(length=20), server_default='active', nullable=False),
    sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('revoked_by', sa.String(length=26), nullable=True),
    sa.Column('metadata', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('resource_grants_pending',
    sa.Column('id', sa.String(length=26), nullable=False),
    sa.Column('entity_id', sa.String(length=26), nullable=False),
    sa.Column('resource_type', sa.String(length=40), nullable=False),
    sa.Column('resource_id', sa.String(length=26), nullable=False),
    sa.Column('requester_user_id', sa.String(length=26), nullable=False),
    sa.Column('requested_capabilities', postgresql.ARRAY(sa.String(length=40)), nullable=False),
    sa.Column('reason', sa.Text(), nullable=True),
    sa.Column('status', sa.String(length=20), server_default='pending', nullable=False),
    sa.Column('decided_by', sa.String(length=26), nullable=True),
    sa.Column('decided_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('decision_note', sa.Text(), nullable=True),
    sa.Column('granted_grant_id', sa.String(length=26), nullable=True),
    sa.Column('metadata', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('resource_tags',
    sa.Column('tag_id', sa.String(length=26), nullable=False),
    sa.Column('resource_type', sa.String(length=50), nullable=False),
    sa.Column('resource_id', sa.String(length=26), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('tag_id', 'resource_type', 'resource_id')
    )
    op.create_table('runtime_event_logs',
    sa.Column('id', sa.String(length=26), nullable=False),
    sa.Column('entity_id', sa.String(length=26), nullable=False),
    sa.Column('workspace_id', sa.String(length=26), nullable=True),
    sa.Column('agent_id', sa.String(length=26), nullable=True),
    sa.Column('user_id', sa.String(length=26), nullable=True),
    sa.Column('conversation_id', sa.String(length=26), nullable=True),
    sa.Column('message_id', sa.String(length=26), nullable=True),
    sa.Column('task_id', sa.String(length=26), nullable=True),
    sa.Column('trace_id', sa.String(length=64), nullable=True),
    sa.Column('surface', sa.String(length=64), nullable=False),
    sa.Column('profile', sa.String(length=64), nullable=False),
    sa.Column('principal_kind', sa.String(length=64), nullable=False),
    sa.Column('event_type', sa.String(length=64), nullable=False),
    sa.Column('tool_name', sa.String(length=255), nullable=True),
    sa.Column('source', sa.String(length=50), nullable=False),
    sa.Column('sequence', sa.Integer(), server_default='0', nullable=False),
    sa.Column('event_data', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
    sa.Column('runtime', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('runtime_evidence',
    sa.Column('id', sa.String(length=26), nullable=False),
    sa.Column('entity_id', sa.String(length=26), nullable=False),
    sa.Column('workspace_id', sa.String(length=26), nullable=True),
    sa.Column('agent_id', sa.String(length=26), nullable=True),
    sa.Column('user_id', sa.String(length=26), nullable=True),
    sa.Column('conversation_id', sa.String(length=26), nullable=True),
    sa.Column('message_id', sa.String(length=26), nullable=True),
    sa.Column('task_id', sa.String(length=26), nullable=True),
    sa.Column('trace_id', sa.String(length=64), nullable=True),
    sa.Column('evidence_type', sa.String(length=50), nullable=False),
    sa.Column('source', sa.String(length=50), nullable=False),
    sa.Column('status', sa.String(length=24), nullable=False),
    sa.Column('summary', sa.Text(), nullable=False),
    sa.Column('details', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
    sa.Column('metrics', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('scheduled_job_runs',
    sa.Column('id', sa.String(length=26), nullable=False),
    sa.Column('job_id', sa.String(length=100), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('trigger_type', sa.String(length=20), nullable=True),
    sa.Column('result', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('duration_ms', sa.Float(), nullable=True),
    sa.Column('prompt_tokens', sa.Integer(), nullable=True),
    sa.Column('completion_tokens', sa.Integer(), nullable=True),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('scheduled_jobs',
    sa.Column('id', sa.String(length=26), nullable=False),
    sa.Column('job_id', sa.String(length=100), nullable=False),
    sa.Column('entity_id', sa.String(length=26), nullable=True),
    sa.Column('workspace_id', sa.String(length=26), nullable=True),
    sa.Column('name', sa.String(length=255), nullable=True),
    sa.Column('job_type', sa.String(length=50), nullable=False),
    sa.Column('schedule_kind', sa.String(length=20), nullable=True),
    sa.Column('cron_expr', sa.String(length=100), nullable=True),
    sa.Column('every_seconds', sa.Float(), nullable=True),
    sa.Column('run_at', sa.String(length=100), nullable=True),
    sa.Column('timezone', sa.String(length=50), nullable=False),
    sa.Column('payload_message', sa.Text(), nullable=True),
    sa.Column('agent_id', sa.String(length=100), nullable=True),
    sa.Column('execution_type', sa.String(length=50), nullable=True),
    sa.Column('execution_target', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
    sa.Column('execution_script', sa.Text(), nullable=True),
    sa.Column('conversation_id', sa.String(length=100), nullable=True),
    sa.Column('user_id', sa.String(length=26), nullable=True),
    sa.Column('default_delivery_mode', sa.String(length=20), nullable=True),
    sa.Column('goal_id', sa.String(length=100), nullable=True),
    sa.Column('goal_step_id', sa.String(length=100), nullable=True),
    sa.Column('manor_task_id', sa.String(length=26), nullable=True),
    sa.Column('enabled', sa.Boolean(), nullable=False),
    sa.Column('delete_after_run', sa.Boolean(), nullable=True),
    sa.Column('last_run_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_status', sa.String(length=20), nullable=True),
    sa.Column('consecutive_errors', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('job_id')
    )
    op.create_table('shares',
    sa.Column('id', sa.String(length=26), nullable=False),
    sa.Column('entity_id', sa.String(length=26), nullable=False),
    sa.Column('resource_type', sa.String(length=40), nullable=False),
    sa.Column('resource_id', sa.String(length=26), nullable=False),
    sa.Column('token_hash', sa.String(length=128), nullable=False),
    sa.Column('capabilities', postgresql.ARRAY(sa.String(length=40)), server_default='{view}', nullable=False),
    sa.Column('audience', sa.String(length=255), nullable=True),
    sa.Column('require_otp', sa.Boolean(), server_default='false', nullable=False),
    sa.Column('watermark', sa.Boolean(), server_default='true', nullable=False),
    sa.Column('allow_download', sa.Boolean(), server_default='false', nullable=False),
    sa.Column('created_by', sa.String(length=26), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('max_uses', sa.Integer(), nullable=True),
    sa.Column('use_count', sa.Integer(), server_default='0', nullable=False),
    sa.Column('last_used_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('revoked_by', sa.String(length=26), nullable=True),
    sa.Column('status', sa.String(length=20), server_default='active', nullable=False),
    sa.Column('metadata', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('token_hash')
    )
    op.create_table('skills',
    sa.Column('id', sa.String(length=26), nullable=False),
    sa.Column('entity_id', sa.String(length=26), nullable=True),
    sa.Column('name', sa.String(length=100), nullable=False),
    sa.Column('slug', sa.String(length=100), nullable=True),
    sa.Column('display_name', sa.String(length=255), nullable=True),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('system_prompt', sa.Text(), nullable=False),
    sa.Column('tools', postgresql.ARRAY(sa.String()), server_default='{}', nullable=False),
    sa.Column('input_schema', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
    sa.Column('output_format', sa.String(length=50), nullable=False),
    sa.Column('category', sa.String(length=50), nullable=True),
    sa.Column('tags', postgresql.ARRAY(sa.String()), server_default='{}', nullable=False),
    sa.Column('is_public', sa.Boolean(), nullable=False),
    sa.Column('version', sa.String(length=20), nullable=False),
    sa.Column('config', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('staff_roles',
    sa.Column('id', sa.String(length=26), nullable=False),
    sa.Column('entity_id', sa.String(length=26), nullable=False),
    sa.Column('name', sa.String(length=100), nullable=False),
    sa.Column('permissions', postgresql.JSONB(astext_type=sa.Text()), server_default='[]', nullable=False),
    sa.Column('is_default', sa.Boolean(), server_default='false', nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('subscription_workers',
    sa.Column('subscription_id', sa.String(length=26), nullable=False),
    sa.Column('worker_id', sa.String(length=26), nullable=False),
    sa.Column('priority', sa.SmallInteger(), nullable=False),
    sa.Column('is_preferred', sa.Boolean(), server_default='false', nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('subscription_id', 'worker_id')
    )
    op.create_table('support_messages',
    sa.Column('id', sa.String(length=26), nullable=False),
    sa.Column('ticket_id', sa.String(length=26), nullable=False),
    sa.Column('sender_kind', sa.String(length=10), nullable=False),
    sa.Column('sender_user_id', sa.String(length=26), nullable=True),
    sa.Column('sender_display_name', sa.String(length=255), nullable=True),
    sa.Column('body', sa.Text(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('support_tickets',
    sa.Column('id', sa.String(length=26), nullable=False),
    sa.Column('entity_id', sa.String(length=26), nullable=True),
    sa.Column('user_id', sa.String(length=26), nullable=True),
    sa.Column('user_email', sa.String(length=255), nullable=False),
    sa.Column('user_display_name', sa.String(length=255), nullable=True),
    sa.Column('subject', sa.String(length=200), nullable=False),
    sa.Column('status', sa.String(length=20), server_default='open', nullable=False),
    sa.Column('priority', sa.String(length=20), server_default='normal', nullable=False),
    sa.Column('assigned_admin_id', sa.String(length=26), nullable=True),
    sa.Column('last_message_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_user_message_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_admin_message_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('unread_user_count', sa.Integer(), server_default='0', nullable=False),
    sa.Column('unread_admin_count', sa.Integer(), server_default='0', nullable=False),
    sa.Column('resolved_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('resolved_by_admin_id', sa.String(length=26), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('tags',
    sa.Column('id', sa.String(length=26), nullable=False),
    sa.Column('entity_id', sa.String(length=26), nullable=False),
    sa.Column('name', sa.String(length=100), nullable=False),
    sa.Column('color', sa.String(length=20), nullable=True),
    sa.Column('description', sa.String(length=500), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('entity_id', 'name', name='uq_tags_entity_name')
    )
    op.create_table('task_categories',
    sa.Column('id', sa.String(length=26), nullable=False),
    sa.Column('entity_id', sa.String(length=26), nullable=False),
    sa.Column('name', sa.String(length=100), nullable=False),
    sa.Column('icon', sa.String(length=50), nullable=True),
    sa.Column('color', sa.String(length=20), nullable=True),
    sa.Column('sort_order', sa.SmallInteger(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('task_checklists',
    sa.Column('id', sa.String(length=26), nullable=False),
    sa.Column('task_id', sa.String(length=26), nullable=False),
    sa.Column('content', sa.String(length=500), nullable=False),
    sa.Column('is_completed', sa.Boolean(), nullable=False),
    sa.Column('sort_order', sa.SmallInteger(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('task_escalation_rules',
    sa.Column('id', sa.String(length=26), nullable=False),
    sa.Column('sla_policy_id', sa.String(length=26), nullable=False),
    sa.Column('escalation_level', sa.SmallInteger(), nullable=False),
    sa.Column('delay_seconds', sa.Integer(), nullable=False),
    sa.Column('notify_user_ids', postgresql.ARRAY(sa.String()), server_default='{}', nullable=False),
    sa.Column('notify_email', sa.String(length=500), nullable=True),
    sa.Column('action_type', sa.String(length=20), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('task_logs',
    sa.Column('id', sa.String(length=26), nullable=False),
    sa.Column('task_id', sa.String(length=26), nullable=False),
    sa.Column('log_type', sa.String(length=50), nullable=False),
    sa.Column('content', sa.String(), nullable=True),
    sa.Column('metadata', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
    sa.Column('created_by', sa.String(length=100), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('task_sla_policies',
    sa.Column('id', sa.String(length=26), nullable=False),
    sa.Column('entity_id', sa.String(length=26), nullable=False),
    sa.Column('name', sa.String(length=255), nullable=False),
    sa.Column('priority', sa.String(length=20), nullable=True),
    sa.Column('category_id', sa.String(length=26), nullable=True),
    sa.Column('response_seconds', sa.Integer(), nullable=False),
    sa.Column('resolution_seconds', sa.Integer(), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('task_templates',
    sa.Column('id', sa.String(length=26), nullable=False),
    sa.Column('entity_id', sa.String(length=26), nullable=False),
    sa.Column('name', sa.String(length=255), nullable=False),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('title_template', sa.String(length=500), nullable=False),
    sa.Column('description_template', sa.Text(), nullable=True),
    sa.Column('priority', sa.SmallInteger(), nullable=False),
    sa.Column('task_type', sa.String(length=50), nullable=False),
    sa.Column('category_id', sa.String(length=26), nullable=True),
    sa.Column('default_assignee_id', sa.String(length=26), nullable=True),
    sa.Column('default_agent_id', sa.String(length=26), nullable=True),
    sa.Column('agent_type', sa.String(length=50), nullable=True),
    sa.Column('details_template', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
    sa.Column('tags', postgresql.ARRAY(sa.String()), server_default='{}', nullable=False),
    sa.Column('is_recurring', sa.Boolean(), nullable=False),
    sa.Column('recurrence_rule', sa.String(length=100), nullable=True),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('sla_policy_id', sa.String(length=26), nullable=True),
    sa.Column('estimated_hours', sa.Float(), nullable=True),
    sa.Column('required_skills', postgresql.ARRAY(sa.String()), server_default='{}', nullable=True),
    sa.Column('steps', postgresql.JSONB(astext_type=sa.Text()), server_default='[]', nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('tasks',
    sa.Column('id', sa.String(length=26), nullable=False),
    sa.Column('entity_id', sa.String(length=26), nullable=False),
    sa.Column('workspace_id', sa.String(length=26), nullable=True),
    sa.Column('category_id', sa.String(length=26), nullable=True),
    sa.Column('title', sa.String(), nullable=False),
    sa.Column('description', sa.String(), nullable=True),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('priority', sa.SmallInteger(), nullable=False),
    sa.Column('task_type', sa.String(length=50), nullable=False),
    sa.Column('assignee_id', sa.String(length=26), nullable=True),
    sa.Column('agent_id', sa.String(length=26), nullable=True),
    sa.Column('agent_type', sa.String(length=50), nullable=True),
    sa.Column('creator_id', sa.String(length=26), nullable=True),
    sa.Column('details', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
    sa.Column('conversation_id', sa.String(length=26), nullable=True),
    sa.Column('deadline', sa.DateTime(timezone=True), nullable=True),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('sla_policy_id', sa.String(length=26), nullable=True),
    sa.Column('sla_breached', sa.Boolean(), server_default='false', nullable=False),
    sa.Column('escalation_level', sa.Integer(), server_default='0', nullable=False),
    sa.Column('template_id', sa.String(length=26), nullable=True),
    sa.Column('vendor_id', sa.String(length=26), nullable=True),
    sa.Column('estimated_hours', sa.Float(), nullable=True),
    sa.Column('parent_task_id', sa.String(length=26), nullable=True),
    sa.Column('required_skills', postgresql.ARRAY(sa.String()), server_default='{}', nullable=False),
    sa.Column('owner_service_key', sa.String(length=100), nullable=True),
    sa.Column('owner_subscription_id', sa.String(length=26), nullable=True),
    sa.Column('delegate_service_keys', postgresql.JSONB(astext_type=sa.Text()), server_default='[]', nullable=False),
    sa.Column('input_contract', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('expected_output', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('actual_output', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('visibility', sa.String(length=20), server_default='entity', nullable=False),
    sa.Column('owner_id', sa.String(length=26), nullable=True),
    sa.Column('client_visible', sa.Boolean(), server_default='false', nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('token_usage_logs',
    sa.Column('id', sa.String(length=26), nullable=False),
    sa.Column('entity_id', sa.String(length=26), nullable=False),
    sa.Column('workspace_id', sa.String(length=26), nullable=True),
    sa.Column('agent_id', sa.String(length=26), nullable=True),
    sa.Column('user_id', sa.String(length=26), nullable=True),
    sa.Column('conversation_id', sa.String(length=26), nullable=True),
    sa.Column('model', sa.String(length=100), nullable=True),
    sa.Column('provider', sa.String(length=50), nullable=True),
    sa.Column('prompt_tokens', sa.Integer(), server_default='0', nullable=False),
    sa.Column('completion_tokens', sa.Integer(), server_default='0', nullable=False),
    sa.Column('total_tokens', sa.Integer(), server_default='0', nullable=False),
    sa.Column('cache_read_tokens', sa.Integer(), server_default='0', nullable=False),
    sa.Column('cache_creation_tokens', sa.Integer(), server_default='0', nullable=False),
    sa.Column('context_breakdown', sa.JSON(), nullable=True),
    sa.Column('cost_usd', sa.Numeric(precision=10, scale=6), nullable=True),
    sa.Column('duration_ms', sa.BigInteger(), nullable=True),
    sa.Column('source', sa.String(length=50), nullable=True),
    sa.Column('billing_mode', sa.String(length=20), nullable=True),
    sa.Column('api_key_source', sa.String(length=30), nullable=True),
    sa.Column('pricing_source', sa.String(length=50), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('tool_call_logs',
    sa.Column('id', sa.String(length=26), nullable=False),
    sa.Column('entity_id', sa.String(length=26), nullable=False),
    sa.Column('workspace_id', sa.String(length=26), nullable=True),
    sa.Column('agent_id', sa.String(length=26), nullable=True),
    sa.Column('user_id', sa.String(length=26), nullable=True),
    sa.Column('conversation_id', sa.String(length=26), nullable=True),
    sa.Column('tool_name', sa.String(length=120), nullable=False),
    sa.Column('source', sa.String(length=50), nullable=True),
    sa.Column('round_num', sa.Integer(), nullable=True),
    sa.Column('duration_ms', sa.BigInteger(), nullable=True),
    sa.Column('result_chars', sa.Integer(), nullable=True),
    sa.Column('success', sa.Boolean(), server_default='true', nullable=False),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('tool_args', sa.JSON(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('tool_definitions',
    sa.Column('id', sa.String(length=26), nullable=False),
    sa.Column('name', sa.String(length=100), nullable=False),
    sa.Column('display_name', sa.String(length=200), nullable=True),
    sa.Column('description', sa.String(), nullable=True),
    sa.Column('category', sa.String(length=50), nullable=True),
    sa.Column('schema', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('status', sa.String(length=10), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('name')
    )
    op.create_table('user_memberships',
    sa.Column('id', sa.String(length=26), nullable=False),
    sa.Column('user_id', sa.String(length=26), nullable=False),
    sa.Column('entity_id', sa.String(length=26), nullable=False),
    sa.Column('role', sa.String(length=20), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('staff_id', sa.String(length=26), nullable=True),
    sa.Column('is_primary', sa.Boolean(), server_default='false', nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('user_id', 'entity_id', name='uq_user_memberships_user_entity')
    )
    op.create_table('user_page_view_logs',
    sa.Column('id', sa.String(length=26), nullable=False),
    sa.Column('entity_id', sa.String(length=26), nullable=False),
    sa.Column('user_id', sa.String(length=26), nullable=False),
    sa.Column('session_id', sa.String(length=26), nullable=True),
    sa.Column('path', sa.String(length=500), nullable=False),
    sa.Column('duration_seconds', sa.Integer(), server_default='0', nullable=False),
    sa.Column('started_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('ended_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('user_session_logs',
    sa.Column('id', sa.String(length=26), nullable=False),
    sa.Column('entity_id', sa.String(length=26), nullable=False),
    sa.Column('user_id', sa.String(length=26), nullable=False),
    sa.Column('source', sa.String(length=50), server_default='web', nullable=False),
    sa.Column('status', sa.String(length=20), server_default='active', nullable=False),
    sa.Column('ip_address', sa.String(length=128), nullable=True),
    sa.Column('user_agent', sa.Text(), nullable=True),
    sa.Column('started_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('last_seen_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('ended_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('duration_seconds', sa.Integer(), server_default='0', nullable=False),
    sa.Column('heartbeat_count', sa.Integer(), server_default='0', nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('current_path', sa.String(length=500), nullable=True),
    sa.Column('current_path_started_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('country_code', sa.String(length=2), nullable=True),
    sa.Column('country', sa.String(length=80), nullable=True),
    sa.Column('city', sa.String(length=120), nullable=True),
    sa.Column('latitude', sa.Float(), nullable=True),
    sa.Column('longitude', sa.Float(), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('users',
    sa.Column('id', sa.String(length=26), nullable=False),
    sa.Column('entity_id', sa.String(length=26), nullable=False),
    sa.Column('email', sa.String(length=255), nullable=False),
    sa.Column('display_name', sa.String(length=255), nullable=True),
    sa.Column('first_name', sa.String(length=100), nullable=True),
    sa.Column('last_name', sa.String(length=100), nullable=True),
    sa.Column('phone', sa.String(length=20), nullable=True),
    sa.Column('avatar_url', sa.String(length=500), nullable=True),
    sa.Column('password_hash', sa.String(length=255), nullable=False),
    sa.Column('role', sa.String(length=20), nullable=False),
    sa.Column('llm_model', sa.String(length=100), nullable=True),
    sa.Column('timezone', sa.String(length=50), nullable=False),
    sa.Column('locale', sa.String(length=10), nullable=False),
    sa.Column('preferences', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('last_login_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_login_ip', sa.String(length=128), nullable=True),
    sa.Column('totp_secret', sa.String(length=255), nullable=True),
    sa.Column('totp_enabled', sa.Boolean(), server_default='false', nullable=False),
    sa.Column('backup_codes', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('email')
    )
    op.create_table('vault_audit_log',
    sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
    sa.Column('credential_ref', sa.String(length=64), nullable=True),
    sa.Column('action', sa.String(length=32), nullable=False),
    sa.Column('requester_kind', sa.String(length=32), nullable=True),
    sa.Column('requester_id', sa.String(length=64), nullable=True),
    sa.Column('step_id', sa.String(length=26), nullable=True),
    sa.Column('reason', sa.Text(), nullable=True),
    sa.Column('ttl_seconds', sa.Integer(), nullable=True),
    sa.Column('occurred_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('waiting_list_entries',
    sa.Column('id', sa.String(length=26), nullable=False),
    sa.Column('name', sa.String(length=255), nullable=False),
    sa.Column('email', sa.String(length=255), nullable=False),
    sa.Column('company', sa.String(length=255), nullable=True),
    sa.Column('interested', sa.String(length=120), nullable=False),
    sa.Column('message', sa.Text(), nullable=False),
    sa.Column('source', sa.String(length=80), server_default='landing', nullable=False),
    sa.Column('status', sa.String(length=20), server_default='new', nullable=False),
    sa.Column('ip_address', sa.String(length=45), nullable=True),
    sa.Column('user_agent', sa.Text(), nullable=True),
    sa.Column('internal_note', sa.Text(), nullable=True),
    sa.Column('invited_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('invited_code', sa.String(length=64), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('webhook_deliveries',
    sa.Column('id', sa.String(length=26), nullable=False),
    sa.Column('endpoint_id', sa.String(length=26), nullable=False),
    sa.Column('event_type', sa.String(length=100), nullable=False),
    sa.Column('payload', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('status_code', sa.Integer(), nullable=True),
    sa.Column('response_body', sa.Text(), nullable=True),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('attempt', sa.Integer(), server_default='1', nullable=False),
    sa.Column('duration_ms', sa.Float(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('webhook_endpoints',
    sa.Column('id', sa.String(length=26), nullable=False),
    sa.Column('entity_id', sa.String(length=26), nullable=False),
    sa.Column('url', sa.String(length=1000), nullable=False),
    sa.Column('secret', sa.String(length=255), nullable=True),
    sa.Column('events', postgresql.ARRAY(sa.String()), server_default='{}', nullable=False),
    sa.Column('headers', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
    sa.Column('enabled', sa.Boolean(), server_default='true', nullable=False),
    sa.Column('description', sa.String(length=500), nullable=True),
    sa.Column('last_triggered_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_status', sa.String(length=20), nullable=True),
    sa.Column('consecutive_failures', sa.Integer(), server_default='0', nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('work_leases',
    sa.Column('id', sa.String(length=26), nullable=False),
    sa.Column('step_id', sa.String(length=26), nullable=False),
    sa.Column('plan_id', sa.String(length=26), nullable=False),
    sa.Column('entity_id', sa.String(length=26), nullable=False),
    sa.Column('workspace_id', sa.String(length=26), nullable=True),
    sa.Column('worker_id', sa.String(length=26), nullable=False),
    sa.Column('subscription_id', sa.String(length=26), nullable=True),
    sa.Column('leased_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('lease_until', sa.DateTime(timezone=True), nullable=False),
    sa.Column('extended_count', sa.SmallInteger(), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('budget_limit_usd', sa.Numeric(precision=10, scale=2), nullable=True),
    sa.Column('budget_spent_usd', sa.Numeric(precision=10, scale=2), server_default='0', nullable=False),
    sa.Column('last_heartbeat_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('heartbeat_count', sa.Integer(), nullable=False),
    sa.Column('progress', sa.Float(), nullable=True),
    sa.Column('result', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('evidence_refs', postgresql.JSONB(astext_type=sa.Text()), server_default='[]', nullable=False),
    sa.Column('cost', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
    sa.Column('error', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('credential_leases', postgresql.JSONB(astext_type=sa.Text()), server_default='[]', nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('worker_activity_log',
    sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
    sa.Column('worker_id', sa.String(length=26), nullable=True),
    sa.Column('event', sa.String(length=32), nullable=False),
    sa.Column('lease_id', sa.String(length=26), nullable=True),
    sa.Column('ip', sa.String(length=45), nullable=True),
    sa.Column('user_agent', sa.String(length=255), nullable=True),
    sa.Column('payload_summary', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('occurred_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('workers',
    sa.Column('id', sa.String(length=26), nullable=False),
    sa.Column('entity_id', sa.String(length=26), nullable=False),
    sa.Column('kind', sa.String(length=32), nullable=False),
    sa.Column('display_name', sa.String(length=255), nullable=False),
    sa.Column('version', sa.String(length=64), nullable=True),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('capabilities', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
    sa.Column('secret_hash', sa.String(length=255), nullable=True),
    sa.Column('trust_level', sa.String(length=16), nullable=False),
    sa.Column('allowed_ips', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('preferences', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
    sa.Column('monthly_budget_usd', sa.Numeric(precision=12, scale=6), nullable=True),
    sa.Column('monthly_spent_usd', sa.Numeric(precision=12, scale=6), server_default='0', nullable=False),
    sa.Column('budget_reset_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('auto_pause_on_budget', sa.Boolean(), server_default='true', nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('last_heartbeat_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_seen_ip', sa.String(length=45), nullable=True),
    sa.Column('consecutive_failures', sa.Integer(), nullable=False),
    sa.Column('created_by_user_id', sa.String(length=26), nullable=True),
    sa.Column('last_rotated_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('workflow_definitions',
    sa.Column('id', sa.String(length=26), nullable=False),
    sa.Column('entity_id', sa.String(length=26), nullable=False),
    sa.Column('name', sa.String(length=255), nullable=False),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('trigger_type', sa.String(length=50), nullable=False),
    sa.Column('trigger_config', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
    sa.Column('steps', postgresql.JSONB(astext_type=sa.Text()), server_default='[]', nullable=False),
    sa.Column('variables', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
    sa.Column('category', sa.String(length=50), nullable=True),
    sa.Column('tags', postgresql.ARRAY(sa.String()), server_default='{}', nullable=False),
    sa.Column('is_active', sa.Boolean(), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('workflow_runs',
    sa.Column('id', sa.String(length=26), nullable=False),
    sa.Column('workflow_id', sa.String(length=26), nullable=False),
    sa.Column('entity_id', sa.String(length=26), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('current_step_id', sa.String(length=100), nullable=True),
    sa.Column('variables', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
    sa.Column('step_results', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
    sa.Column('trigger_data', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('started_by', sa.String(length=26), nullable=True),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('workspace_activities',
    sa.Column('id', sa.String(length=26), nullable=False),
    sa.Column('workspace_id', sa.String(length=26), nullable=False),
    sa.Column('entity_id', sa.String(length=26), nullable=False),
    sa.Column('event_type', sa.String(length=50), nullable=False),
    sa.Column('summary', sa.String(), nullable=False),
    sa.Column('details', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('user_id', sa.String(length=26), nullable=True),
    sa.Column('agent_id', sa.String(length=26), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('workspace_blueprints',
    sa.Column('id', sa.String(length=26), nullable=False),
    sa.Column('entity_id', sa.String(length=26), nullable=False),
    sa.Column('slug', sa.String(length=120), nullable=False),
    sa.Column('source_workspace_id', sa.String(length=26), nullable=True),
    sa.Column('title', sa.String(length=200), nullable=False),
    sa.Column('summary', sa.String(length=500), nullable=True),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('cover_image_url', sa.String(length=500), nullable=True),
    sa.Column('tags', postgresql.JSONB(astext_type=sa.Text()), server_default='[]', nullable=False),
    sa.Column('payload', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('payload_version', sa.String(length=20), server_default='1.0', nullable=False),
    sa.Column('status', sa.String(length=20), server_default='draft', nullable=False),
    sa.Column('install_count', sa.Integer(), server_default='0', nullable=False),
    sa.Column('price_cents', sa.Integer(), nullable=True),
    sa.Column('currency', sa.String(length=10), server_default='usd', nullable=False),
    sa.Column('purchase_count', sa.Integer(), server_default='0', nullable=False),
    sa.Column('share_token', sa.String(length=64), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('published_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('entity_id', 'slug', name='uq_workspace_blueprints_entity_slug')
    )
    op.create_table('workspace_drafts',
    sa.Column('id', sa.String(length=26), nullable=False),
    sa.Column('entity_id', sa.String(length=26), nullable=False),
    sa.Column('user_id', sa.String(length=26), nullable=True),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('fields', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
    sa.Column('messages', postgresql.JSONB(astext_type=sa.Text()), server_default='[]', nullable=False),
    sa.Column('missing', postgresql.JSONB(astext_type=sa.Text()), server_default='[]', nullable=False),
    sa.Column('ready', sa.Boolean(), server_default='false', nullable=False),
    sa.Column('suggested_blueprint_id', sa.String(length=26), nullable=True),
    sa.Column('applied_blueprint_id', sa.String(length=26), nullable=True),
    sa.Column('finalized_workspace_id', sa.String(length=26), nullable=True),
    sa.Column('finalized_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('workspace_operation_drafts',
    sa.Column('id', sa.String(length=26), nullable=False),
    sa.Column('workspace_id', sa.String(length=26), nullable=False),
    sa.Column('entity_id', sa.String(length=26), nullable=False),
    sa.Column('created_by_user_id', sa.String(length=26), nullable=True),
    sa.Column('source_event_id', sa.String(length=100), nullable=True),
    sa.Column('base_revision', sa.Integer(), server_default='0', nullable=False),
    sa.Column('status', sa.String(length=20), server_default='open', nullable=False),
    sa.Column('current_state', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
    sa.Column('patches', postgresql.JSONB(astext_type=sa.Text()), server_default='[]', nullable=False),
    sa.Column('validation', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
    sa.Column('diff', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
    sa.Column('applied_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('discarded_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('workspace_staff',
    sa.Column('id', sa.String(length=26), nullable=False),
    sa.Column('workspace_id', sa.String(length=26), nullable=False),
    sa.Column('staff_id', sa.String(length=26), nullable=True),
    sa.Column('user_id', sa.String(length=26), nullable=True),
    sa.Column('role', sa.String(length=50), nullable=True),
    sa.Column('added_by', sa.String(length=26), nullable=True),
    sa.Column('added_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('status', sa.String(length=20), server_default='active', nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('workspace_work_batches',
    sa.Column('id', sa.String(length=26), nullable=False),
    sa.Column('workspace_id', sa.String(length=26), nullable=False),
    sa.Column('entity_id', sa.String(length=26), nullable=False),
    sa.Column('created_by_user_id', sa.String(length=26), nullable=True),
    sa.Column('source_draft_id', sa.String(length=26), nullable=True),
    sa.Column('source_kind', sa.String(length=50), nullable=True),
    sa.Column('summary', sa.String(), nullable=True),
    sa.Column('status', sa.String(length=20), server_default='active', nullable=False),
    sa.Column('task_ids', postgresql.JSONB(astext_type=sa.Text()), server_default='[]', nullable=False),
    sa.Column('details', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
    sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('workspaces',
    sa.Column('id', sa.String(length=26), nullable=False),
    sa.Column('entity_id', sa.String(length=26), nullable=False),
    sa.Column('name', sa.String(length=255), nullable=False),
    sa.Column('description', sa.String(), nullable=True),
    sa.Column('category', sa.String(length=100), nullable=True),
    sa.Column('address', sa.String(), nullable=True),
    sa.Column('kind', sa.String(length=100), nullable=True),
    sa.Column('operating_context', sa.String(), nullable=True),
    sa.Column('primary_work', sa.String(), nullable=True),
    sa.Column('operating_model', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
    sa.Column('operation_revision', sa.Integer(), server_default='0', nullable=False),
    sa.Column('settings', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('longitude', sa.Numeric(precision=10, scale=7), nullable=True),
    sa.Column('latitude', sa.Numeric(precision=10, scale=7), nullable=True),
    sa.Column('cover_image_url', sa.String(length=500), nullable=True),
    sa.Column('attribute_tags', postgresql.ARRAY(sa.String()), server_default='{}', nullable=False),
    sa.Column('identity_label', sa.String(length=255), nullable=True),
    sa.Column('property_type', sa.String(length=50), nullable=True),
    sa.Column('occupancy_status', sa.String(length=50), nullable=True),
    sa.Column('pms_property_id', sa.String(length=100), nullable=True),
    sa.Column('pms_unit_id', sa.String(length=100), nullable=True),
    sa.Column('heartbeat_enabled', sa.Boolean(), nullable=False),
    sa.Column('heartbeat_cadence', sa.String(length=50), nullable=True),
    sa.Column('last_heartbeat_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('monthly_budget_usd', sa.Numeric(precision=12, scale=6), nullable=True),
    sa.Column('monthly_spent_usd', sa.Numeric(precision=12, scale=6), server_default='0', nullable=False),
    sa.Column('budget_reset_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('auto_pause_on_budget', sa.Boolean(), server_default='true', nullable=False),
    sa.Column('budget_alert_state', sa.String(length=20), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('browser_tool_specs',
    sa.Column('id', sa.String(length=26), nullable=False),
    sa.Column('mcp_server_id', sa.String(length=26), nullable=False),
    sa.Column('login_url', sa.String(length=500), nullable=False),
    sa.Column('session_check_selector', sa.String(length=500), nullable=True),
    sa.Column('provider_module', sa.String(length=120), nullable=False),
    sa.Column('tool_actions', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
    sa.Column('cookie_ttl_days', sa.Integer(), server_default='30', nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['mcp_server_id'], ['mcp_servers.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('mcp_server_id')
    )
    op.create_table('notification_deliveries',
    sa.Column('id', sa.String(length=26), nullable=False),
    sa.Column('notification_id', sa.String(length=26), nullable=False),
    sa.Column('entity_id', sa.String(length=26), nullable=False),
    sa.Column('user_id', sa.String(length=26), nullable=False),
    sa.Column('channel_contact_id', sa.String(length=26), nullable=False),
    sa.Column('channel_type', sa.String(length=30), nullable=False),
    sa.Column('conversation_id', sa.String(length=26), nullable=True),
    sa.Column('message_log_id', sa.String(length=26), nullable=True),
    sa.Column('actions', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('callback_kind', sa.String(length=64), nullable=True),
    sa.Column('callback_payload', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('resolved_action_key', sa.String(length=64), nullable=True),
    sa.Column('resolved_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('error_message', sa.String(), nullable=True),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['notification_id'], ['notifications.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('staff',
    sa.Column('id', sa.String(length=26), nullable=False),
    sa.Column('entity_id', sa.String(length=26), nullable=False),
    sa.Column('kind', sa.String(length=20), server_default='employee', nullable=False),
    sa.Column('name', sa.String(length=255), nullable=False),
    sa.Column('email', sa.String(length=255), nullable=True),
    sa.Column('phone', sa.String(length=50), nullable=True),
    sa.Column('avatar_url', sa.String(length=500), nullable=True),
    sa.Column('user_id', sa.String(length=26), nullable=True),
    sa.Column('title', sa.String(length=255), nullable=True),
    sa.Column('department_id', sa.String(length=26), nullable=True),
    sa.Column('role_id', sa.String(length=26), nullable=True),
    sa.Column('skills', postgresql.ARRAY(sa.String()), nullable=True),
    sa.Column('service_categories', postgresql.ARRAY(sa.String()), nullable=True),
    sa.Column('company_name', sa.String(length=255), nullable=True),
    sa.Column('tax_id', sa.String(length=64), nullable=True),
    sa.Column('billing_rate', sa.Numeric(precision=12, scale=2), nullable=True),
    sa.Column('billing_currency', sa.String(length=8), nullable=True),
    sa.Column('payment_terms', sa.String(length=64), nullable=True),
    sa.Column('preferred_payment_method', sa.String(length=32), nullable=True),
    sa.Column('address', sa.String(), nullable=True),
    sa.Column('website', sa.String(length=255), nullable=True),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('metadata', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['department_id'], ['departments.id'], ),
    sa.ForeignKeyConstraint(['role_id'], ['staff_roles.id'], ),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('staff_schedule_adjustments',
    sa.Column('id', sa.String(length=26), nullable=False),
    sa.Column('staff_id', sa.String(length=26), nullable=False),
    sa.Column('date', sa.DateTime(timezone=True), nullable=False),
    sa.Column('adjustment_type', sa.String(length=30), nullable=False),
    sa.Column('shift_start', sa.Time(), nullable=True),
    sa.Column('shift_end', sa.Time(), nullable=True),
    sa.Column('reason', sa.String(length=500), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['staff_id'], ['staff.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('staff_schedules',
    sa.Column('id', sa.String(length=26), nullable=False),
    sa.Column('staff_id', sa.String(length=26), nullable=False),
    sa.Column('day_of_week', sa.SmallInteger(), nullable=False),
    sa.Column('shift_start', sa.Time(), nullable=False),
    sa.Column('shift_end', sa.Time(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['staff_id'], ['staff.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_learning_candidates_agent_status', 'agent_learning_candidates', ['entity_id', 'agent_id', 'status'], unique=False)
    op.create_index('ix_learning_candidates_dedupe', 'agent_learning_candidates', ['entity_id', 'dedupe_key'], unique=False)
    op.create_index('ix_learning_candidates_entity_status', 'agent_learning_candidates', ['entity_id', 'status', 'created_at'], unique=False)
    op.create_index('ix_learning_candidates_type_status', 'agent_learning_candidates', ['candidate_type', 'status'], unique=False)
    op.create_index('ix_learning_candidates_workspace_status', 'agent_learning_candidates', ['entity_id', 'workspace_id', 'status'], unique=False)
    op.create_index('ix_agent_mcp_bindings_agent', 'agent_mcp_bindings', ['agent_id'], unique=False)
    op.create_index('ix_agent_memories_entity_agent', 'agent_memories', ['entity_id', 'agent_id'], unique=False)
    op.create_index('ix_agent_memories_importance', 'agent_memories', ['importance'], unique=False)
    op.create_index('ix_agent_memories_workspace_scope', 'agent_memories', ['workspace_id', 'scope'], unique=False)
    op.create_index('ix_agent_skill_bindings_agent', 'agent_skill_bindings', ['agent_id'], unique=False)
    op.create_index(op.f('ix_agents_deleted_at'), 'agents', ['deleted_at'], unique=False)
    op.create_index('ix_agents_entity', 'agents', ['entity_id'], unique=False)
    op.create_index('ix_agents_tags', 'agents', ['tags'], unique=False, postgresql_using='gin')
    op.create_index('ix_announcement_recipients_ann', 'announcement_recipients', ['announcement_id'], unique=False)
    op.create_index('ix_announcements_entity', 'announcements', ['entity_id'], unique=False)
    op.create_index('ix_api_keys_entity', 'api_keys', ['entity_id'], unique=False)
    op.create_index('ix_api_keys_entity_default', 'api_keys', ['entity_id', 'is_default'], unique=False)
    op.create_index('ix_audit_entity', 'audit_log', ['entity_id', 'created_at'], unique=False)
    op.create_index('ix_blueprint_purchases_blueprint', 'blueprint_purchases', ['blueprint_id'], unique=False)
    op.create_index('ix_blueprint_purchases_buyer', 'blueprint_purchases', ['buyer_entity_id'], unique=False)
    op.create_index('ux_blueprint_purchases_checkout_session', 'blueprint_purchases', ['stripe_checkout_session_id'], unique=True, postgresql_where=sa.text('stripe_checkout_session_id IS NOT NULL'))
    op.create_index('ux_blueprint_purchases_live_entitlement', 'blueprint_purchases', ['blueprint_id', 'buyer_entity_id'], unique=True, postgresql_where=sa.text("status != 'refunded'"))
    op.create_index('ix_business_order_items_order', 'business_order_items', ['order_id'], unique=False)
    op.create_index('ix_business_orders_client', 'business_orders', ['client_id'], unique=False)
    op.create_index('ix_business_orders_entity_status', 'business_orders', ['entity_id', 'status'], unique=False)
    op.create_index('ix_business_orders_number', 'business_orders', ['order_number'], unique=True)
    op.create_index('ix_channel_configs_entity', 'channel_configs', ['entity_id'], unique=False)
    op.create_index('ix_channel_configs_type', 'channel_configs', ['entity_id', 'channel_type'], unique=False)
    op.create_index('ix_channel_contacts_contact', 'channel_contacts', ['contact_id'], unique=False)
    op.create_index('ix_channel_contacts_entity', 'channel_contacts', ['entity_id'], unique=False)
    op.create_index('uq_channel_contact_source', 'channel_contacts', ['channel_config_id', 'source_id'], unique=True)
    op.create_index('ix_channel_link_user', 'channel_link_tokens', ['user_id'], unique=False)
    op.create_index('ux_channel_link_token', 'channel_link_tokens', ['token'], unique=True)
    op.create_index(op.f('ix_channel_pairing_codes_entity_id'), 'channel_pairing_codes', ['entity_id'], unique=False)
    op.create_index(op.f('ix_channel_pairing_codes_expires_at'), 'channel_pairing_codes', ['expires_at'], unique=False)
    op.create_index('ix_chat_feedback_conversation', 'chat_message_feedback', ['conversation_id', 'created_at'], unique=False)
    op.create_index('ix_chat_feedback_entity_created', 'chat_message_feedback', ['entity_id', 'created_at'], unique=False)
    op.create_index('ix_chat_feedback_rating', 'chat_message_feedback', ['rating', 'created_at'], unique=False)
    op.create_index('ix_client_errors_created', 'client_error_events', ['created_at'], unique=False)
    op.create_index('ix_client_errors_entity_created', 'client_error_events', ['entity_id', 'created_at'], unique=False)
    op.create_index('ix_client_errors_fingerprint_created', 'client_error_events', ['fingerprint', 'created_at'], unique=False)
    op.create_index('ix_client_errors_level_created', 'client_error_events', ['level', 'created_at'], unique=False)
    op.create_index('ix_client_errors_source_created', 'client_error_events', ['source', 'created_at'], unique=False)
    op.create_index(op.f('ix_clients_deleted_at'), 'clients', ['deleted_at'], unique=False)
    op.create_index('ix_comments_parent', 'comments', ['parent_id'], unique=False)
    op.create_index('ix_comments_resource', 'comments', ['resource_type', 'resource_id'], unique=False)
    op.create_index('ix_conversation_shares_entity', 'conversation_shares', ['entity_id'], unique=False)
    op.create_index('ix_conversation_shares_token', 'conversation_shares', ['share_token'], unique=True)
    op.create_index('ix_conversations_entity', 'conversations', ['entity_id'], unique=False)
    op.create_index('ix_conversations_workspace_scope', 'conversations', ['workspace_id', 'scope'], unique=False)
    op.create_index('ix_csub_lease', 'credential_subleases', ['work_lease_id'], unique=False)
    op.create_index('ix_cfd_entity_target', 'custom_field_definitions', ['entity_id', 'target'], unique=False)
    op.create_index('ix_cfd_workspace', 'custom_field_definitions', ['workspace_id'], unique=False)
    op.create_index(op.f('ix_departments_deleted_at'), 'departments', ['deleted_at'], unique=False)
    op.create_index('ix_departments_entity', 'departments', ['entity_id'], unique=False)
    op.create_index('ix_doc_access_log_actor_ts', 'document_access_log', ['actor_type', 'actor_id', 'ts'], unique=False)
    op.create_index('ix_doc_access_log_doc_ts', 'document_access_log', ['document_id', 'ts'], unique=False)
    op.create_index('ix_doc_access_log_entity_ts', 'document_access_log', ['entity_id', 'ts'], unique=False)
    op.create_index('ix_document_folders_entity', 'document_folders', ['entity_id'], unique=False)
    op.create_index('uq_document_folders_entity_parent_name', 'document_folders', ['entity_id', sa.literal_column("coalesce(parent_id, '')"), 'name'], unique=True)
    op.create_index(op.f('ix_document_versions_document_id'), 'document_versions', ['document_id'], unique=False)
    op.create_index('ix_documents_entity', 'documents', ['entity_id'], unique=False)
    op.create_index('ix_documents_fs_path', 'documents', ['fs_path'], unique=False)
    op.create_index('ix_documents_name', 'documents', ['entity_id', 'name'], unique=False)
    op.create_index(op.f('ix_entities_deleted_at'), 'entities', ['deleted_at'], unique=False)
    op.create_index('ix_event_entity_created', 'event_logs', ['entity_id', 'created_at'], unique=False)
    op.create_index('ix_event_type', 'event_logs', ['event_type'], unique=False)
    op.create_index('ix_plans_entity_status', 'execution_plans', ['entity_id', 'status'], unique=False)
    op.create_index('ix_plans_task', 'execution_plans', ['task_id'], unique=False)
    op.create_index('ix_plans_workspace_status', 'execution_plans', ['workspace_id', 'status'], unique=False)
    op.create_index('ix_steps_plan_status', 'execution_steps', ['plan_id', 'step_status'], unique=False)
    op.create_index('ix_feature_flag_overrides_flag', 'feature_flag_overrides', ['flag_key'], unique=False)
    op.create_index('ix_feature_flag_overrides_scope', 'feature_flag_overrides', ['scope', 'scope_id'], unique=False)
    op.create_index('ix_goal_task_links_task', 'goal_task_links', ['task_id'], unique=False)
    op.create_index('ix_goals_entity_status', 'goals', ['entity_id', 'status'], unique=False)
    op.create_index('ix_goals_workspace_status', 'goals', ['workspace_id', 'status'], unique=False)
    op.create_index(op.f('ix_governance_policies_entity_id'), 'governance_policies', ['entity_id'], unique=False)
    op.create_index(op.f('ix_governance_revisions_workspace_id'), 'governance_revisions', ['workspace_id'], unique=False)
    op.create_index(op.f('ix_integration_sessions_entity_id'), 'integration_sessions', ['entity_id'], unique=False)
    op.create_index(op.f('ix_integration_sessions_provider'), 'integration_sessions', ['provider'], unique=False)
    op.create_index('ix_invite_redemptions_code', 'invitation_code_redemptions', ['code', 'redeemed_at'], unique=False)
    op.create_index('ix_invite_redemptions_user', 'invitation_code_redemptions', ['user_id'], unique=False)
    op.create_index('ix_invitation_codes_expires', 'invitation_codes', ['expires_at'], unique=False)
    op.create_index('ix_invitation_codes_status', 'invitation_codes', ['status'], unique=False)
    op.create_index('ix_mcp_servers_status', 'mcp_servers', ['status'], unique=False)
    op.create_index('ix_media_jobs_conversation', 'media_jobs', ['conversation_id'], unique=False)
    op.create_index('ix_media_jobs_entity', 'media_jobs', ['entity_id'], unique=False)
    op.create_index('ix_media_jobs_status', 'media_jobs', ['entity_id', 'status'], unique=False)
    op.create_index('ux_merchant_accounts_entity', 'merchant_accounts', ['entity_id'], unique=True)
    op.create_index('ux_merchant_accounts_stripe', 'merchant_accounts', ['stripe_account_id'], unique=True)
    op.create_index('ix_message_logs_channel_config', 'message_logs', ['channel_config_id'], unique=False)
    op.create_index('ix_message_logs_conversation', 'message_logs', ['conversation_id'], unique=False)
    op.create_index('ix_message_logs_entity', 'message_logs', ['entity_id'], unique=False)
    op.create_index('ix_message_logs_external', 'message_logs', ['external_id'], unique=False)
    op.create_index('ix_messages_conv', 'messages', ['conversation_id', 'created_at'], unique=False)
    op.create_index('ix_nango_webhook_events_connection_id', 'nango_webhook_events', ['connection_id'], unique=False)
    op.create_index('ix_nango_webhook_events_entity_id', 'nango_webhook_events', ['entity_id'], unique=False)
    op.create_index('ix_nango_webhook_events_received_at', 'nango_webhook_events', ['received_at'], unique=False)
    op.create_index('ix_notifications_due', 'notifications', ['dispatch_status', 'deliver_at'], unique=False)
    op.create_index('ix_notifications_user', 'notifications', ['user_id', 'created_at'], unique=False)
    op.create_index('ix_oauth_codes_client_user', 'oauth_authorization_codes', ['client_id', 'user_id'], unique=False)
    op.create_index('ix_oauth_codes_code', 'oauth_authorization_codes', ['code'], unique=True)
    op.create_index('ix_oauth_codes_expires_at', 'oauth_authorization_codes', ['expires_at'], unique=False)
    op.create_index('ix_oauth_client_apps_client_id', 'oauth_client_apps', ['client_id'], unique=True)
    op.create_index('ix_permission_audit_actor', 'permission_audit', ['actor_type', 'actor_id', 'ts'], unique=False)
    op.create_index('ix_permission_audit_decision', 'permission_audit', ['decision', 'ts'], unique=False)
    op.create_index('ix_permission_audit_resource', 'permission_audit', ['resource_type', 'resource_id', 'ts'], unique=False)
    op.create_index('ix_permission_audit_ts', 'permission_audit', ['ts'], unique=False)
    op.create_index('ix_phone_numbers_entity', 'phone_numbers', ['entity_id'], unique=False)
    op.create_index('ix_phone_numbers_number', 'phone_numbers', ['phone_number'], unique=True)
    op.create_index('ix_platform_announcement_audiences_term', 'platform_announcement_audiences', ['term'], unique=False)
    op.create_index('ix_platform_announcement_dismissals_user', 'platform_announcement_dismissals', ['user_id'], unique=False)
    op.create_index('ix_platform_announcements_active', 'platform_announcements', ['starts_at', 'ends_at', 'status'], unique=False)
    op.create_index('ix_platform_model_provider_keys_provider', 'platform_model_provider_keys', ['provider'], unique=True)
    op.create_index('ix_platform_model_provider_keys_status', 'platform_model_provider_keys', ['status'], unique=False)
    op.create_index('ix_platform_settings_key', 'platform_settings', ['key'], unique=True)
    op.create_index('ix_resource_grants_entity_status', 'resource_grants', ['entity_id', 'status'], unique=False)
    op.create_index('ix_resource_grants_resource', 'resource_grants', ['resource_type', 'resource_id'], unique=False)
    op.create_index('ix_resource_grants_subject', 'resource_grants', ['subject_type', 'subject_id'], unique=False)
    op.create_index('ix_resource_grants_pending_requester', 'resource_grants_pending', ['requester_user_id', 'status'], unique=False)
    op.create_index('ix_resource_grants_pending_resource', 'resource_grants_pending', ['resource_type', 'resource_id'], unique=False)
    op.create_index('ix_runtime_event_logs_conversation_created', 'runtime_event_logs', ['conversation_id', 'created_at'], unique=False)
    op.create_index('ix_runtime_event_logs_entity_created', 'runtime_event_logs', ['entity_id', 'created_at'], unique=False)
    op.create_index('ix_runtime_event_logs_task_created', 'runtime_event_logs', ['task_id', 'created_at'], unique=False)
    op.create_index('ix_runtime_event_logs_tool_created', 'runtime_event_logs', ['tool_name', 'created_at'], unique=False)
    op.create_index('ix_runtime_event_logs_type_created', 'runtime_event_logs', ['event_type', 'created_at'], unique=False)
    op.create_index('ix_runtime_evidence_agent_created', 'runtime_evidence', ['entity_id', 'agent_id', 'created_at'], unique=False)
    op.create_index('ix_runtime_evidence_entity_created', 'runtime_evidence', ['entity_id', 'created_at'], unique=False)
    op.create_index('ix_runtime_evidence_task_created', 'runtime_evidence', ['task_id', 'created_at'], unique=False)
    op.create_index('ix_runtime_evidence_type_status', 'runtime_evidence', ['evidence_type', 'status'], unique=False)
    op.create_index('ix_runtime_evidence_workspace_created', 'runtime_evidence', ['entity_id', 'workspace_id', 'created_at'], unique=False)
    op.create_index('ix_shares_entity_status', 'shares', ['entity_id', 'status'], unique=False)
    op.create_index('ix_shares_expires', 'shares', ['expires_at'], unique=False)
    op.create_index('ix_shares_resource', 'shares', ['resource_type', 'resource_id'], unique=False)
    op.create_index('ix_skills_category', 'skills', ['category'], unique=False)
    op.create_index('ix_skills_entity', 'skills', ['entity_id'], unique=False)
    op.create_index('ix_skills_slug', 'skills', ['slug'], unique=False)
    op.create_index('ix_skills_tags', 'skills', ['tags'], unique=False, postgresql_using='gin')
    op.create_index('ix_staff_roles_entity', 'staff_roles', ['entity_id'], unique=False)
    op.create_index('ix_sub_workers_worker', 'subscription_workers', ['worker_id'], unique=False)
    op.create_index('ix_support_messages_ticket', 'support_messages', ['ticket_id', 'created_at'], unique=False)
    op.create_index('ix_support_tickets_last_message', 'support_tickets', ['last_message_at'], unique=False)
    op.create_index('ix_support_tickets_status', 'support_tickets', ['status'], unique=False)
    op.create_index('ix_support_tickets_user', 'support_tickets', ['user_id', 'created_at'], unique=False)
    op.create_index(op.f('ix_tags_entity_id'), 'tags', ['entity_id'], unique=False)
    op.create_index('ix_task_checklists_task', 'task_checklists', ['task_id'], unique=False)
    op.create_index('ix_task_escalation_rules_sla', 'task_escalation_rules', ['sla_policy_id'], unique=False)
    op.create_index('ix_task_logs_task', 'task_logs', ['task_id', 'created_at'], unique=False)
    op.create_index('ix_task_sla_policies_entity', 'task_sla_policies', ['entity_id'], unique=False)
    op.create_index('ix_tasks_details', 'tasks', ['details'], unique=False, postgresql_using='gin')
    op.create_index('ix_tasks_entity_status', 'tasks', ['entity_id', 'status'], unique=False)
    op.create_index('ix_tasks_workspace', 'tasks', ['workspace_id'], unique=False)
    op.create_index('ix_token_usage_agent', 'token_usage_logs', ['entity_id', 'agent_id'], unique=False)
    op.create_index('ix_token_usage_entity', 'token_usage_logs', ['entity_id', 'created_at'], unique=False)
    op.create_index('ix_token_usage_workspace', 'token_usage_logs', ['entity_id', 'workspace_id', 'created_at'], unique=False)
    op.create_index('ix_tool_call_entity_created', 'tool_call_logs', ['entity_id', 'created_at'], unique=False)
    op.create_index('ix_tool_call_tool_name', 'tool_call_logs', ['entity_id', 'tool_name', 'created_at'], unique=False)
    op.create_index('ix_tool_call_workspace', 'tool_call_logs', ['entity_id', 'workspace_id', 'created_at'], unique=False)
    op.create_index(op.f('ix_user_memberships_deleted_at'), 'user_memberships', ['deleted_at'], unique=False)
    op.create_index('ix_user_memberships_entity', 'user_memberships', ['entity_id'], unique=False)
    op.create_index('ix_user_memberships_status', 'user_memberships', ['entity_id', 'status'], unique=False)
    op.create_index('ix_user_memberships_user', 'user_memberships', ['user_id'], unique=False)
    op.create_index('ix_page_view_entity_started', 'user_page_view_logs', ['entity_id', 'started_at'], unique=False)
    op.create_index('ix_page_view_user_path', 'user_page_view_logs', ['user_id', 'path'], unique=False)
    op.create_index('ix_user_session_entity_started', 'user_session_logs', ['entity_id', 'started_at'], unique=False)
    op.create_index('ix_user_session_entity_status', 'user_session_logs', ['entity_id', 'status', 'last_seen_at'], unique=False)
    op.create_index('ix_user_session_user_started', 'user_session_logs', ['user_id', 'started_at'], unique=False)
    op.create_index(op.f('ix_users_deleted_at'), 'users', ['deleted_at'], unique=False)
    op.create_index('ix_users_entity', 'users', ['entity_id'], unique=False)
    op.create_index('ix_vault_audit_action_time', 'vault_audit_log', ['action', 'occurred_at'], unique=False)
    op.create_index('ix_vault_audit_ref_time', 'vault_audit_log', ['credential_ref', 'occurred_at'], unique=False)
    op.create_index('ix_waiting_list_entries_created_at', 'waiting_list_entries', ['created_at'], unique=False)
    op.create_index('ix_waiting_list_entries_email', 'waiting_list_entries', ['email'], unique=False)
    op.create_index('ix_waiting_list_entries_interested', 'waiting_list_entries', ['interested'], unique=False)
    op.create_index('ix_waiting_list_entries_status', 'waiting_list_entries', ['status'], unique=False)
    op.create_index('ix_webhook_deliveries_endpoint', 'webhook_deliveries', ['endpoint_id'], unique=False)
    op.create_index('ix_webhook_endpoints_entity', 'webhook_endpoints', ['entity_id'], unique=False)
    op.create_index('ix_leases_expiry_scan', 'work_leases', ['status', 'lease_until'], unique=False)
    op.create_index('ix_leases_step_status', 'work_leases', ['step_id', 'status'], unique=False)
    op.create_index('ix_leases_worker_status', 'work_leases', ['worker_id', 'status'], unique=False)
    op.create_index('ix_worker_activity_recent', 'worker_activity_log', ['worker_id', 'occurred_at'], unique=False)
    op.create_index('ix_workers_entity_status', 'workers', ['entity_id', 'status'], unique=False)
    op.create_index('ix_ws_activity_workspace', 'workspace_activities', ['workspace_id', 'created_at'], unique=False)
    op.create_index(op.f('ix_workspace_blueprints_entity_id'), 'workspace_blueprints', ['entity_id'], unique=False)
    op.create_index('ux_workspace_blueprints_share_token', 'workspace_blueprints', ['share_token'], unique=True, postgresql_where=sa.text('share_token IS NOT NULL'))
    op.create_index('ix_workspace_drafts_entity_status', 'workspace_drafts', ['entity_id', 'status'], unique=False)
    op.create_index('ix_workspace_drafts_user', 'workspace_drafts', ['user_id'], unique=False)
    op.create_index('ix_ws_operation_drafts_entity_workspace', 'workspace_operation_drafts', ['entity_id', 'workspace_id'], unique=False)
    op.create_index('ix_ws_operation_drafts_workspace_status', 'workspace_operation_drafts', ['workspace_id', 'status'], unique=False)
    op.create_index('ix_workspace_staff_user', 'workspace_staff', ['user_id'], unique=False)
    op.create_index('ix_workspace_staff_workspace_user', 'workspace_staff', ['workspace_id', 'user_id'], unique=False)
    op.create_index('ix_ws_work_batches_entity_workspace', 'workspace_work_batches', ['entity_id', 'workspace_id'], unique=False)
    op.create_index('ix_ws_work_batches_workspace_status', 'workspace_work_batches', ['workspace_id', 'status'], unique=False)
    op.create_index(op.f('ix_workspaces_deleted_at'), 'workspaces', ['deleted_at'], unique=False)
    op.create_index('ix_workspaces_entity', 'workspaces', ['entity_id'], unique=False)
    op.create_index('ix_notif_delivery_notification', 'notification_deliveries', ['notification_id'], unique=False)
    op.create_index('ix_notif_delivery_open_by_contact', 'notification_deliveries', ['channel_contact_id', 'status'], unique=False)
    op.create_index('ix_notif_delivery_open_by_conv', 'notification_deliveries', ['conversation_id', 'status'], unique=False)
    op.create_index(op.f('ix_staff_deleted_at'), 'staff', ['deleted_at'], unique=False)
    op.create_index('ix_staff_entity', 'staff', ['entity_id'], unique=False)
    op.create_index('ix_staff_kind', 'staff', ['entity_id', 'kind'], unique=False)
    op.create_index('ix_staff_user', 'staff', ['user_id'], unique=False)
    op.create_index('ix_staff_adj_staff_date', 'staff_schedule_adjustments', ['staff_id', 'date'], unique=False)
    op.create_index('ix_staff_schedules_staff', 'staff_schedules', ['staff_id'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_staff_schedules_staff', table_name='staff_schedules')
    op.drop_table('staff_schedules')
    op.drop_index('ix_staff_adj_staff_date', table_name='staff_schedule_adjustments')
    op.drop_table('staff_schedule_adjustments')
    op.drop_index('ix_staff_user', table_name='staff')
    op.drop_index('ix_staff_kind', table_name='staff')
    op.drop_index('ix_staff_entity', table_name='staff')
    op.drop_index(op.f('ix_staff_deleted_at'), table_name='staff')
    op.drop_table('staff')
    op.drop_index('ix_notif_delivery_open_by_conv', table_name='notification_deliveries')
    op.drop_index('ix_notif_delivery_open_by_contact', table_name='notification_deliveries')
    op.drop_index('ix_notif_delivery_notification', table_name='notification_deliveries')
    op.drop_table('notification_deliveries')
    op.drop_table('browser_tool_specs')
    op.drop_index('ix_workspaces_entity', table_name='workspaces')
    op.drop_index(op.f('ix_workspaces_deleted_at'), table_name='workspaces')
    op.drop_table('workspaces')
    op.drop_index('ix_ws_work_batches_workspace_status', table_name='workspace_work_batches')
    op.drop_index('ix_ws_work_batches_entity_workspace', table_name='workspace_work_batches')
    op.drop_table('workspace_work_batches')
    op.drop_index('ix_workspace_staff_workspace_user', table_name='workspace_staff')
    op.drop_index('ix_workspace_staff_user', table_name='workspace_staff')
    op.drop_table('workspace_staff')
    op.drop_index('ix_ws_operation_drafts_workspace_status', table_name='workspace_operation_drafts')
    op.drop_index('ix_ws_operation_drafts_entity_workspace', table_name='workspace_operation_drafts')
    op.drop_table('workspace_operation_drafts')
    op.drop_index('ix_workspace_drafts_user', table_name='workspace_drafts')
    op.drop_index('ix_workspace_drafts_entity_status', table_name='workspace_drafts')
    op.drop_table('workspace_drafts')
    op.drop_index('ux_workspace_blueprints_share_token', table_name='workspace_blueprints', postgresql_where=sa.text('share_token IS NOT NULL'))
    op.drop_index(op.f('ix_workspace_blueprints_entity_id'), table_name='workspace_blueprints')
    op.drop_table('workspace_blueprints')
    op.drop_index('ix_ws_activity_workspace', table_name='workspace_activities')
    op.drop_table('workspace_activities')
    op.drop_table('workflow_runs')
    op.drop_table('workflow_definitions')
    op.drop_index('ix_workers_entity_status', table_name='workers')
    op.drop_table('workers')
    op.drop_index('ix_worker_activity_recent', table_name='worker_activity_log')
    op.drop_table('worker_activity_log')
    op.drop_index('ix_leases_worker_status', table_name='work_leases')
    op.drop_index('ix_leases_step_status', table_name='work_leases')
    op.drop_index('ix_leases_expiry_scan', table_name='work_leases')
    op.drop_table('work_leases')
    op.drop_index('ix_webhook_endpoints_entity', table_name='webhook_endpoints')
    op.drop_table('webhook_endpoints')
    op.drop_index('ix_webhook_deliveries_endpoint', table_name='webhook_deliveries')
    op.drop_table('webhook_deliveries')
    op.drop_index('ix_waiting_list_entries_status', table_name='waiting_list_entries')
    op.drop_index('ix_waiting_list_entries_interested', table_name='waiting_list_entries')
    op.drop_index('ix_waiting_list_entries_email', table_name='waiting_list_entries')
    op.drop_index('ix_waiting_list_entries_created_at', table_name='waiting_list_entries')
    op.drop_table('waiting_list_entries')
    op.drop_index('ix_vault_audit_ref_time', table_name='vault_audit_log')
    op.drop_index('ix_vault_audit_action_time', table_name='vault_audit_log')
    op.drop_table('vault_audit_log')
    op.drop_index('ix_users_entity', table_name='users')
    op.drop_index(op.f('ix_users_deleted_at'), table_name='users')
    op.drop_table('users')
    op.drop_index('ix_user_session_user_started', table_name='user_session_logs')
    op.drop_index('ix_user_session_entity_status', table_name='user_session_logs')
    op.drop_index('ix_user_session_entity_started', table_name='user_session_logs')
    op.drop_table('user_session_logs')
    op.drop_index('ix_page_view_user_path', table_name='user_page_view_logs')
    op.drop_index('ix_page_view_entity_started', table_name='user_page_view_logs')
    op.drop_table('user_page_view_logs')
    op.drop_index('ix_user_memberships_user', table_name='user_memberships')
    op.drop_index('ix_user_memberships_status', table_name='user_memberships')
    op.drop_index('ix_user_memberships_entity', table_name='user_memberships')
    op.drop_index(op.f('ix_user_memberships_deleted_at'), table_name='user_memberships')
    op.drop_table('user_memberships')
    op.drop_table('tool_definitions')
    op.drop_index('ix_tool_call_workspace', table_name='tool_call_logs')
    op.drop_index('ix_tool_call_tool_name', table_name='tool_call_logs')
    op.drop_index('ix_tool_call_entity_created', table_name='tool_call_logs')
    op.drop_table('tool_call_logs')
    op.drop_index('ix_token_usage_workspace', table_name='token_usage_logs')
    op.drop_index('ix_token_usage_entity', table_name='token_usage_logs')
    op.drop_index('ix_token_usage_agent', table_name='token_usage_logs')
    op.drop_table('token_usage_logs')
    op.drop_index('ix_tasks_workspace', table_name='tasks')
    op.drop_index('ix_tasks_entity_status', table_name='tasks')
    op.drop_index('ix_tasks_details', table_name='tasks', postgresql_using='gin')
    op.drop_table('tasks')
    op.drop_table('task_templates')
    op.drop_index('ix_task_sla_policies_entity', table_name='task_sla_policies')
    op.drop_table('task_sla_policies')
    op.drop_index('ix_task_logs_task', table_name='task_logs')
    op.drop_table('task_logs')
    op.drop_index('ix_task_escalation_rules_sla', table_name='task_escalation_rules')
    op.drop_table('task_escalation_rules')
    op.drop_index('ix_task_checklists_task', table_name='task_checklists')
    op.drop_table('task_checklists')
    op.drop_table('task_categories')
    op.drop_index(op.f('ix_tags_entity_id'), table_name='tags')
    op.drop_table('tags')
    op.drop_index('ix_support_tickets_user', table_name='support_tickets')
    op.drop_index('ix_support_tickets_status', table_name='support_tickets')
    op.drop_index('ix_support_tickets_last_message', table_name='support_tickets')
    op.drop_table('support_tickets')
    op.drop_index('ix_support_messages_ticket', table_name='support_messages')
    op.drop_table('support_messages')
    op.drop_index('ix_sub_workers_worker', table_name='subscription_workers')
    op.drop_table('subscription_workers')
    op.drop_index('ix_staff_roles_entity', table_name='staff_roles')
    op.drop_table('staff_roles')
    op.drop_index('ix_skills_tags', table_name='skills', postgresql_using='gin')
    op.drop_index('ix_skills_slug', table_name='skills')
    op.drop_index('ix_skills_entity', table_name='skills')
    op.drop_index('ix_skills_category', table_name='skills')
    op.drop_table('skills')
    op.drop_index('ix_shares_resource', table_name='shares')
    op.drop_index('ix_shares_expires', table_name='shares')
    op.drop_index('ix_shares_entity_status', table_name='shares')
    op.drop_table('shares')
    op.drop_table('scheduled_jobs')
    op.drop_table('scheduled_job_runs')
    op.drop_index('ix_runtime_evidence_workspace_created', table_name='runtime_evidence')
    op.drop_index('ix_runtime_evidence_type_status', table_name='runtime_evidence')
    op.drop_index('ix_runtime_evidence_task_created', table_name='runtime_evidence')
    op.drop_index('ix_runtime_evidence_entity_created', table_name='runtime_evidence')
    op.drop_index('ix_runtime_evidence_agent_created', table_name='runtime_evidence')
    op.drop_table('runtime_evidence')
    op.drop_index('ix_runtime_event_logs_type_created', table_name='runtime_event_logs')
    op.drop_index('ix_runtime_event_logs_tool_created', table_name='runtime_event_logs')
    op.drop_index('ix_runtime_event_logs_task_created', table_name='runtime_event_logs')
    op.drop_index('ix_runtime_event_logs_entity_created', table_name='runtime_event_logs')
    op.drop_index('ix_runtime_event_logs_conversation_created', table_name='runtime_event_logs')
    op.drop_table('runtime_event_logs')
    op.drop_table('resource_tags')
    op.drop_index('ix_resource_grants_pending_resource', table_name='resource_grants_pending')
    op.drop_index('ix_resource_grants_pending_requester', table_name='resource_grants_pending')
    op.drop_table('resource_grants_pending')
    op.drop_index('ix_resource_grants_subject', table_name='resource_grants')
    op.drop_index('ix_resource_grants_resource', table_name='resource_grants')
    op.drop_index('ix_resource_grants_entity_status', table_name='resource_grants')
    op.drop_table('resource_grants')
    op.drop_index('ix_platform_settings_key', table_name='platform_settings')
    op.drop_table('platform_settings')
    op.drop_index('ix_platform_model_provider_keys_status', table_name='platform_model_provider_keys')
    op.drop_index('ix_platform_model_provider_keys_provider', table_name='platform_model_provider_keys')
    op.drop_table('platform_model_provider_keys')
    op.drop_index('ix_platform_announcements_active', table_name='platform_announcements')
    op.drop_table('platform_announcements')
    op.drop_index('ix_platform_announcement_dismissals_user', table_name='platform_announcement_dismissals')
    op.drop_table('platform_announcement_dismissals')
    op.drop_index('ix_platform_announcement_audiences_term', table_name='platform_announcement_audiences')
    op.drop_table('platform_announcement_audiences')
    op.drop_index('ix_phone_numbers_number', table_name='phone_numbers')
    op.drop_index('ix_phone_numbers_entity', table_name='phone_numbers')
    op.drop_table('phone_numbers')
    op.drop_index('ix_permission_audit_ts', table_name='permission_audit')
    op.drop_index('ix_permission_audit_resource', table_name='permission_audit')
    op.drop_index('ix_permission_audit_decision', table_name='permission_audit')
    op.drop_index('ix_permission_audit_actor', table_name='permission_audit')
    op.drop_table('permission_audit')
    op.drop_index('ix_oauth_client_apps_client_id', table_name='oauth_client_apps')
    op.drop_table('oauth_client_apps')
    op.drop_index('ix_oauth_codes_expires_at', table_name='oauth_authorization_codes')
    op.drop_index('ix_oauth_codes_code', table_name='oauth_authorization_codes')
    op.drop_index('ix_oauth_codes_client_user', table_name='oauth_authorization_codes')
    op.drop_table('oauth_authorization_codes')
    op.drop_table('oauth_accounts')
    op.drop_index('ix_notifications_user', table_name='notifications')
    op.drop_index('ix_notifications_due', table_name='notifications')
    op.drop_table('notifications')
    op.drop_index('ix_nango_webhook_events_received_at', table_name='nango_webhook_events')
    op.drop_index('ix_nango_webhook_events_entity_id', table_name='nango_webhook_events')
    op.drop_index('ix_nango_webhook_events_connection_id', table_name='nango_webhook_events')
    op.drop_table('nango_webhook_events')
    op.drop_index('ix_messages_conv', table_name='messages')
    op.drop_table('messages')
    op.drop_index('ix_message_logs_external', table_name='message_logs')
    op.drop_index('ix_message_logs_entity', table_name='message_logs')
    op.drop_index('ix_message_logs_conversation', table_name='message_logs')
    op.drop_index('ix_message_logs_channel_config', table_name='message_logs')
    op.drop_table('message_logs')
    op.drop_index('ux_merchant_accounts_stripe', table_name='merchant_accounts')
    op.drop_index('ux_merchant_accounts_entity', table_name='merchant_accounts')
    op.drop_table('merchant_accounts')
    op.drop_index('ix_media_jobs_status', table_name='media_jobs')
    op.drop_index('ix_media_jobs_entity', table_name='media_jobs')
    op.drop_index('ix_media_jobs_conversation', table_name='media_jobs')
    op.drop_table('media_jobs')
    op.drop_index('ix_mcp_servers_status', table_name='mcp_servers')
    op.drop_table('mcp_servers')
    op.drop_index('ix_invitation_codes_status', table_name='invitation_codes')
    op.drop_index('ix_invitation_codes_expires', table_name='invitation_codes')
    op.drop_table('invitation_codes')
    op.drop_index('ix_invite_redemptions_user', table_name='invitation_code_redemptions')
    op.drop_index('ix_invite_redemptions_code', table_name='invitation_code_redemptions')
    op.drop_table('invitation_code_redemptions')
    op.drop_table('integrations')
    op.drop_index(op.f('ix_integration_sessions_provider'), table_name='integration_sessions')
    op.drop_index(op.f('ix_integration_sessions_entity_id'), table_name='integration_sessions')
    op.drop_table('integration_sessions')
    op.drop_index(op.f('ix_governance_revisions_workspace_id'), table_name='governance_revisions')
    op.drop_table('governance_revisions')
    op.drop_index(op.f('ix_governance_policies_entity_id'), table_name='governance_policies')
    op.drop_table('governance_policies')
    op.drop_index('ix_goals_workspace_status', table_name='goals')
    op.drop_index('ix_goals_entity_status', table_name='goals')
    op.drop_table('goals')
    op.drop_index('ix_goal_task_links_task', table_name='goal_task_links')
    op.drop_table('goal_task_links')
    op.drop_table('goal_measurements')
    op.drop_table('feature_flags')
    op.drop_index('ix_feature_flag_overrides_scope', table_name='feature_flag_overrides')
    op.drop_index('ix_feature_flag_overrides_flag', table_name='feature_flag_overrides')
    op.drop_table('feature_flag_overrides')
    op.drop_table('favorites')
    op.drop_index('ix_steps_plan_status', table_name='execution_steps')
    op.drop_table('execution_steps')
    op.drop_index('ix_plans_workspace_status', table_name='execution_plans')
    op.drop_index('ix_plans_task', table_name='execution_plans')
    op.drop_index('ix_plans_entity_status', table_name='execution_plans')
    op.drop_table('execution_plans')
    op.drop_index('ix_event_type', table_name='event_logs')
    op.drop_index('ix_event_entity_created', table_name='event_logs')
    op.drop_table('event_logs')
    op.drop_table('entity_quotas')
    op.drop_index(op.f('ix_entities_deleted_at'), table_name='entities')
    op.drop_table('entities')
    op.drop_index('ix_documents_name', table_name='documents')
    op.drop_index('ix_documents_fs_path', table_name='documents')
    op.drop_index('ix_documents_entity', table_name='documents')
    op.drop_table('documents')
    op.drop_index(op.f('ix_document_versions_document_id'), table_name='document_versions')
    op.drop_table('document_versions')
    op.drop_table('document_groups')
    op.drop_table('document_group_members')
    op.drop_index('uq_document_folders_entity_parent_name', table_name='document_folders')
    op.drop_index('ix_document_folders_entity', table_name='document_folders')
    op.drop_table('document_folders')
    op.drop_index('ix_doc_access_log_entity_ts', table_name='document_access_log')
    op.drop_index('ix_doc_access_log_doc_ts', table_name='document_access_log')
    op.drop_index('ix_doc_access_log_actor_ts', table_name='document_access_log')
    op.drop_table('document_access_log')
    op.drop_index('ix_departments_entity', table_name='departments')
    op.drop_index(op.f('ix_departments_deleted_at'), table_name='departments')
    op.drop_table('departments')
    op.drop_index('ix_cfd_workspace', table_name='custom_field_definitions')
    op.drop_index('ix_cfd_entity_target', table_name='custom_field_definitions')
    op.drop_table('custom_field_definitions')
    op.drop_index('ix_csub_lease', table_name='credential_subleases')
    op.drop_table('credential_subleases')
    op.drop_index('ix_conversations_workspace_scope', table_name='conversations')
    op.drop_index('ix_conversations_entity', table_name='conversations')
    op.drop_table('conversations')
    op.drop_index('ix_conversation_shares_token', table_name='conversation_shares')
    op.drop_index('ix_conversation_shares_entity', table_name='conversation_shares')
    op.drop_table('conversation_shares')
    op.drop_index('ix_comments_resource', table_name='comments')
    op.drop_index('ix_comments_parent', table_name='comments')
    op.drop_table('comments')
    op.drop_index(op.f('ix_clients_deleted_at'), table_name='clients')
    op.drop_table('clients')
    op.drop_index('ix_client_errors_source_created', table_name='client_error_events')
    op.drop_index('ix_client_errors_level_created', table_name='client_error_events')
    op.drop_index('ix_client_errors_fingerprint_created', table_name='client_error_events')
    op.drop_index('ix_client_errors_entity_created', table_name='client_error_events')
    op.drop_index('ix_client_errors_created', table_name='client_error_events')
    op.drop_table('client_error_events')
    op.drop_index('ix_chat_feedback_rating', table_name='chat_message_feedback')
    op.drop_index('ix_chat_feedback_entity_created', table_name='chat_message_feedback')
    op.drop_index('ix_chat_feedback_conversation', table_name='chat_message_feedback')
    op.drop_table('chat_message_feedback')
    op.drop_table('channels')
    op.drop_index(op.f('ix_channel_pairing_codes_expires_at'), table_name='channel_pairing_codes')
    op.drop_index(op.f('ix_channel_pairing_codes_entity_id'), table_name='channel_pairing_codes')
    op.drop_table('channel_pairing_codes')
    op.drop_index('ux_channel_link_token', table_name='channel_link_tokens')
    op.drop_index('ix_channel_link_user', table_name='channel_link_tokens')
    op.drop_table('channel_link_tokens')
    op.drop_index('uq_channel_contact_source', table_name='channel_contacts')
    op.drop_index('ix_channel_contacts_entity', table_name='channel_contacts')
    op.drop_index('ix_channel_contacts_contact', table_name='channel_contacts')
    op.drop_table('channel_contacts')
    op.drop_index('ix_channel_configs_type', table_name='channel_configs')
    op.drop_index('ix_channel_configs_entity', table_name='channel_configs')
    op.drop_table('channel_configs')
    op.drop_index('ix_business_orders_number', table_name='business_orders')
    op.drop_index('ix_business_orders_entity_status', table_name='business_orders')
    op.drop_index('ix_business_orders_client', table_name='business_orders')
    op.drop_table('business_orders')
    op.drop_index('ix_business_order_items_order', table_name='business_order_items')
    op.drop_table('business_order_items')
    op.drop_index('ux_blueprint_purchases_live_entitlement', table_name='blueprint_purchases', postgresql_where=sa.text("status != 'refunded'"))
    op.drop_index('ux_blueprint_purchases_checkout_session', table_name='blueprint_purchases', postgresql_where=sa.text('stripe_checkout_session_id IS NOT NULL'))
    op.drop_index('ix_blueprint_purchases_buyer', table_name='blueprint_purchases')
    op.drop_index('ix_blueprint_purchases_blueprint', table_name='blueprint_purchases')
    op.drop_table('blueprint_purchases')
    op.drop_index('ix_audit_entity', table_name='audit_log')
    op.drop_table('audit_log')
    op.drop_index('ix_api_keys_entity_default', table_name='api_keys')
    op.drop_index('ix_api_keys_entity', table_name='api_keys')
    op.drop_table('api_keys')
    op.drop_index('ix_announcements_entity', table_name='announcements')
    op.drop_table('announcements')
    op.drop_index('ix_announcement_recipients_ann', table_name='announcement_recipients')
    op.drop_table('announcement_recipients')
    op.drop_index('ix_agents_tags', table_name='agents', postgresql_using='gin')
    op.drop_index('ix_agents_entity', table_name='agents')
    op.drop_index(op.f('ix_agents_deleted_at'), table_name='agents')
    op.drop_table('agents')
    op.drop_table('agent_tool_bindings')
    op.drop_table('agent_subscriptions')
    op.drop_index('ix_agent_skill_bindings_agent', table_name='agent_skill_bindings')
    op.drop_table('agent_skill_bindings')
    op.drop_index('ix_agent_memories_workspace_scope', table_name='agent_memories')
    op.drop_index('ix_agent_memories_importance', table_name='agent_memories')
    op.drop_index('ix_agent_memories_entity_agent', table_name='agent_memories')
    op.drop_table('agent_memories')
    op.drop_index('ix_agent_mcp_bindings_agent', table_name='agent_mcp_bindings')
    op.drop_table('agent_mcp_bindings')
    op.drop_index('ix_learning_candidates_workspace_status', table_name='agent_learning_candidates')
    op.drop_index('ix_learning_candidates_type_status', table_name='agent_learning_candidates')
    op.drop_index('ix_learning_candidates_entity_status', table_name='agent_learning_candidates')
    op.drop_index('ix_learning_candidates_dedupe', table_name='agent_learning_candidates')
    op.drop_index('ix_learning_candidates_agent_status', table_name='agent_learning_candidates')
    op.drop_table('agent_learning_candidates')
    op.drop_table('agent_executions')
