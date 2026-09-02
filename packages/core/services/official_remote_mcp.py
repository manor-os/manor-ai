"""Typed contracts for vendor-hosted MCP integrations.

Provider endpoints, action names, effects, fallback schemas, and PayPal OAuth
metadata are generated here so runtime registration, approval policy, catalog
seeding, and Skill packs cannot silently drift apart.
"""

from __future__ import annotations

import os
import hashlib
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Iterable, Mapping

from packages.core.services.robinhood_oauth import ROBINHOOD_MCP_ENDPOINT


class OfficialRemoteMCPProvider(StrEnum):
    STRIPE = "stripe"
    PAYPAL = "paypal"
    ROBINHOOD = "robinhood"


# Explicitly documented reads only. Vendor-added actions stay approval-gated,
# even when tools/list labels them read-only. Schemas come from live discovery.
# https://robinhood.com/us/en/support/articles/trading-with-your-agent/
_ROBINHOOD_READ_ACTIONS = frozenset({
    "get_accounts", "get_portfolio", "get_realized_pnl", "get_pnl_trade_history",
    "search", "get_watchlists", "get_watchlist_items", "get_option_watchlist",
    "get_popular_watchlists", "get_equity_historicals", "get_equity_fundamentals",
    "get_financials", "get_equity_price_book", "get_equity_technical_indicators",
    "get_earnings_results", "get_earnings_calendar", "get_indexes", "get_index_quotes",
    "get_equity_positions", "get_equity_tax_lots", "get_equity_quotes",
    "get_equity_orders", "get_equity_tradability", "get_option_historicals",
    "get_option_chains", "get_option_instruments", "get_option_quotes",
    "get_option_positions", "get_option_orders", "get_currency_pairs",
    "get_crypto_quotes", "get_crypto_positions", "get_crypto_orders",
    "get_scans", "get_scanner_filter_specs", "run_scan",
})


class MCPActionEffect(StrEnum):
    """Shared effect vocabulary for managed and custom MCP operations."""

    READ = "read"
    WRITE = "write"
    DESTRUCTIVE = "destructive"


@dataclass(frozen=True, slots=True)
class OfficialRemoteMCPActionPolicy:
    """Governance projection of one typed remote MCP effect."""

    effect: MCPActionEffect
    risk_level: str
    required_approval: bool
    operation: str
    title: str


class OfficialRemoteMCPActionPolicyFactory:
    """Keep Chat, Planner, and Worker effect/risk semantics identical."""

    @staticmethod
    def create(
        provider: OfficialRemoteMCPProvider | str | None,
        effect: MCPActionEffect | str,
    ) -> OfficialRemoteMCPActionPolicy:
        try:
            resolved_effect = MCPActionEffect(effect)
        except (TypeError, ValueError):
            resolved_effect = MCPActionEffect.WRITE
        provider_value = (
            provider.value if isinstance(provider, StrEnum) else str(provider or "")
        )
        try:
            official_provider = OfficialRemoteMCPProvider(provider_value)
        except ValueError:
            official_provider = None
        if resolved_effect is MCPActionEffect.READ:
            return OfficialRemoteMCPActionPolicy(
                effect=resolved_effect,
                risk_level="low",
                required_approval=False,
                operation="read",
                title="read external account data",
            )
        destructive = resolved_effect is MCPActionEffect.DESTRUCTIVE
        payment_write = official_provider is not None
        return OfficialRemoteMCPActionPolicy(
            effect=resolved_effect,
            risk_level="high" if destructive or payment_write else "medium",
            required_approval=True,
            operation="delete" if destructive else "modify",
            title=(
                "run destructive external action"
                if destructive
                else (
                    "run external payment action"
                    if payment_write
                    else "run external action"
                )
            ),
        )


class OfficialRemoteMCPToolNameFactory:
    """Build a stable public Runtime name without losing vendor identity.

    Vendor actions may contain punctuation that is illegal in an OpenAI tool
    name.  A normalized name alone is ambiguous (``future-tool`` and
    ``future_tool`` collide), so changed action names receive a deterministic
    suffix.  The original action remains the value sent to the vendor.
    """

    _UNSAFE_COMPONENT = re.compile(r"[^A-Za-z0-9_]")

    @classmethod
    def _component(cls, value: object) -> str:
        return cls._UNSAFE_COMPONENT.sub("_", str(value or "").strip())

    @classmethod
    def create(
        cls,
        provider: OfficialRemoteMCPProvider | str,
        action: StrEnum | str,
    ) -> str:
        provider_value = (
            provider.value if isinstance(provider, StrEnum) else str(provider)
        ).strip()
        action_value = action.value if isinstance(action, StrEnum) else str(action)
        safe_provider = cls._component(provider_value)
        safe_action = cls._component(action_value)
        if not safe_provider or not safe_action:
            raise ValueError("Official remote MCP provider and action are required")
        if safe_action != action_value:
            suffix = hashlib.sha256(action_value.encode("utf-8")).hexdigest()[:8]
            safe_action = f"{safe_action}_{suffix}"
        return f"mcp__{safe_provider}__{safe_action}"


class PayPalMCPEnvironment(StrEnum):
    SANDBOX = "sandbox"
    PRODUCTION = "production"


class PayPalMCPAction(StrEnum):
    CREATE_INVOICE = "create_invoice"
    CREATE_RECURRING_SERIES = "create_recurring_series"
    LIST_INVOICES = "list_invoices"
    GET_INVOICE = "get_invoice"
    SEND_INVOICE = "send_invoice"
    SEND_INVOICE_REMINDER = "send_invoice_reminder"
    CANCEL_SENT_INVOICE = "cancel_sent_invoice"
    GENERATE_INVOICE_QR_CODE = "generate_invoice_qr_code"
    CREATE_ORDER = "create_order"
    GET_ORDER = "get_order"
    PAY_ORDER = "pay_order"
    CREATE_REFUND = "create_refund"
    GET_REFUND = "get_refund"
    LIST_DISPUTES = "list_disputes"
    GET_DISPUTE = "get_dispute"
    ACCEPT_DISPUTE_CLAIM = "accept_dispute_claim"
    CREATE_PRODUCT = "create_product"
    LIST_PRODUCTS = "list_products"
    SHOW_PRODUCT_DETAILS = "show_product_details"
    CREATE_SUBSCRIPTION_PLAN = "create_subscription_plan"
    LIST_SUBSCRIPTION_PLANS = "list_subscription_plans"
    SHOW_SUBSCRIPTION_PLAN_DETAILS = "show_subscription_plan_details"
    CREATE_SUBSCRIPTION = "create_subscription"
    SHOW_SUBSCRIPTION_DETAILS = "show_subscription_details"
    UPDATE_SUBSCRIPTION = "update_subscription"
    CANCEL_SUBSCRIPTION = "cancel_subscription"
    CREATE_SHIPMENT_TRACKING = "create_shipment_tracking"
    GET_SHIPMENT_TRACKING = "get_shipment_tracking"
    UPDATE_SHIPMENT_TRACKING = "update_shipment_tracking"
    LIST_TRANSACTIONS = "list_transactions"
    GET_MERCHANT_INSIGHTS = "get_merchant_insights"


class StripeMCPAction(StrEnum):
    GET_STRIPE_ACCOUNT_INFO = "get_stripe_account_info"
    RETRIEVE_BALANCE = "retrieve_balance"
    CREATE_COUPON = "create_coupon"
    LIST_COUPONS = "list_coupons"
    CREATE_CUSTOMER = "create_customer"
    LIST_CUSTOMERS = "list_customers"
    LIST_DISPUTES = "list_disputes"
    UPDATE_DISPUTE = "update_dispute"
    CREATE_INVOICE = "create_invoice"
    CREATE_INVOICE_ITEM = "create_invoice_item"
    FINALIZE_INVOICE = "finalize_invoice"
    LIST_INVOICES = "list_invoices"
    CREATE_PAYMENT_LINK = "create_payment_link"
    LIST_PAYMENT_INTENTS = "list_payment_intents"
    CREATE_PRICE = "create_price"
    LIST_PRICES = "list_prices"
    CREATE_PRODUCT = "create_product"
    LIST_PRODUCTS = "list_products"
    CREATE_REFUND = "create_refund"
    CANCEL_SUBSCRIPTION = "cancel_subscription"
    LIST_SUBSCRIPTIONS = "list_subscriptions"
    UPDATE_SUBSCRIPTION = "update_subscription"
    SEARCH_STRIPE_RESOURCES = "search_stripe_resources"
    FETCH_STRIPE_RESOURCES = "fetch_stripe_resources"
    SEARCH_STRIPE_DOCUMENTATION = "search_stripe_documentation"


class PayPalOAuthScope(StrEnum):
    OPENID = "openid"
    PROFILE = "profile"
    EMAIL = "email"
    INVOICING = "https://uri.paypal.com/services/invoicing"
    REALTIME_PAYMENT = "https://uri.paypal.com/services/payments/realtimepayment"
    PAYMENT_CAPTURE = "https://uri.paypal.com/services/payments/payment/authcapture"
    PAYMENT_REFUND = "https://uri.paypal.com/services/payments/refund"
    DISPUTES_READ_SELLER = "https://uri.paypal.com/services/disputes/read-seller"
    DISPUTES_UPDATE_SELLER = "https://uri.paypal.com/services/disputes/update-seller"
    SUBSCRIPTIONS = "https://uri.paypal.com/services/subscriptions"
    TRANSACTION_SEARCH = "https://uri.paypal.com/services/reporting/search/read"


@dataclass(frozen=True, slots=True)
class OfficialRemoteMCPActionSpec:
    name: str
    effect: MCPActionEffect
    description: str
    input_schema: dict[str, Any]


@dataclass(frozen=True, slots=True)
class OfficialRemoteMCPDefinition:
    provider: OfficialRemoteMCPProvider
    endpoint: str
    action_specs: tuple[OfficialRemoteMCPActionSpec, ...]

    @property
    def actions(self) -> tuple[str, ...]:
        return tuple(spec.name for spec in self.action_specs)

    @property
    def read_only_actions(self) -> frozenset[str]:
        if self.provider is OfficialRemoteMCPProvider.ROBINHOOD:
            return _ROBINHOOD_READ_ACTIONS
        return frozenset(spec.name for spec in self.action_specs if spec.effect is MCPActionEffect.READ)


@dataclass(frozen=True, slots=True)
class OfficialRemoteMCPOAuthDefinition:
    authorize_url: str
    token_url: str
    identity_url: str
    scopes: str
    client_id_env: str
    client_secret_env: str


_ACTION_ENUMS: Mapping[
    OfficialRemoteMCPProvider,
    type[PayPalMCPAction] | type[StripeMCPAction],
] = {
    OfficialRemoteMCPProvider.PAYPAL: PayPalMCPAction,
    OfficialRemoteMCPProvider.STRIPE: StripeMCPAction,
}


def _action_effects(
    action_enum: type[StrEnum],
    *,
    read: Iterable[StrEnum],
    write: Iterable[StrEnum],
    destructive: Iterable[StrEnum],
) -> Mapping[StrEnum, MCPActionEffect]:
    groups = {
        MCPActionEffect.READ: tuple(read),
        MCPActionEffect.WRITE: tuple(write),
        MCPActionEffect.DESTRUCTIVE: tuple(destructive),
    }
    effects: dict[StrEnum, MCPActionEffect] = {}
    for effect, actions in groups.items():
        for action in actions:
            if action in effects:
                raise RuntimeError(f"MCP action {action} has more than one effect")
            effects[action] = effect
    expected = set(action_enum)
    if set(effects) != expected:
        missing = sorted(action.value for action in expected - set(effects))
        extra = sorted(str(action) for action in set(effects) - expected)
        raise RuntimeError(f"MCP action effects are incomplete: missing={missing}, extra={extra}")
    return effects


_ACTION_EFFECTS: Mapping[
    OfficialRemoteMCPProvider,
    Mapping[StrEnum, MCPActionEffect],
] = {
    OfficialRemoteMCPProvider.PAYPAL: _action_effects(
        PayPalMCPAction,
        read=(
            PayPalMCPAction.GET_DISPUTE,
            PayPalMCPAction.GET_INVOICE,
            PayPalMCPAction.GET_MERCHANT_INSIGHTS,
            PayPalMCPAction.GET_ORDER,
            PayPalMCPAction.GET_REFUND,
            PayPalMCPAction.GET_SHIPMENT_TRACKING,
            PayPalMCPAction.LIST_DISPUTES,
            PayPalMCPAction.LIST_INVOICES,
            PayPalMCPAction.LIST_PRODUCTS,
            PayPalMCPAction.LIST_SUBSCRIPTION_PLANS,
            PayPalMCPAction.LIST_TRANSACTIONS,
            PayPalMCPAction.SHOW_PRODUCT_DETAILS,
            PayPalMCPAction.SHOW_SUBSCRIPTION_DETAILS,
            PayPalMCPAction.SHOW_SUBSCRIPTION_PLAN_DETAILS,
        ),
        write=(
            PayPalMCPAction.CREATE_INVOICE,
            PayPalMCPAction.CREATE_RECURRING_SERIES,
            PayPalMCPAction.SEND_INVOICE,
            PayPalMCPAction.SEND_INVOICE_REMINDER,
            PayPalMCPAction.GENERATE_INVOICE_QR_CODE,
            PayPalMCPAction.CREATE_ORDER,
            PayPalMCPAction.PAY_ORDER,
            PayPalMCPAction.CREATE_REFUND,
            PayPalMCPAction.ACCEPT_DISPUTE_CLAIM,
            PayPalMCPAction.CREATE_PRODUCT,
            PayPalMCPAction.CREATE_SUBSCRIPTION_PLAN,
            PayPalMCPAction.CREATE_SUBSCRIPTION,
            PayPalMCPAction.UPDATE_SUBSCRIPTION,
            PayPalMCPAction.CREATE_SHIPMENT_TRACKING,
            PayPalMCPAction.UPDATE_SHIPMENT_TRACKING,
        ),
        destructive=(
            PayPalMCPAction.CANCEL_SENT_INVOICE,
            PayPalMCPAction.CANCEL_SUBSCRIPTION,
        ),
    ),
    OfficialRemoteMCPProvider.STRIPE: _action_effects(
        StripeMCPAction,
        read=(
            StripeMCPAction.GET_STRIPE_ACCOUNT_INFO,
            StripeMCPAction.RETRIEVE_BALANCE,
            StripeMCPAction.LIST_COUPONS,
            StripeMCPAction.LIST_CUSTOMERS,
            StripeMCPAction.LIST_DISPUTES,
            StripeMCPAction.LIST_INVOICES,
            StripeMCPAction.LIST_PAYMENT_INTENTS,
            StripeMCPAction.LIST_PRICES,
            StripeMCPAction.LIST_PRODUCTS,
            StripeMCPAction.LIST_SUBSCRIPTIONS,
            StripeMCPAction.SEARCH_STRIPE_RESOURCES,
            StripeMCPAction.FETCH_STRIPE_RESOURCES,
            StripeMCPAction.SEARCH_STRIPE_DOCUMENTATION,
        ),
        write=(
            StripeMCPAction.CREATE_COUPON,
            StripeMCPAction.CREATE_CUSTOMER,
            StripeMCPAction.UPDATE_DISPUTE,
            StripeMCPAction.CREATE_INVOICE,
            StripeMCPAction.CREATE_INVOICE_ITEM,
            StripeMCPAction.FINALIZE_INVOICE,
            StripeMCPAction.CREATE_PAYMENT_LINK,
            StripeMCPAction.CREATE_PRICE,
            StripeMCPAction.CREATE_PRODUCT,
            StripeMCPAction.CREATE_REFUND,
            StripeMCPAction.UPDATE_SUBSCRIPTION,
        ),
        destructive=(StripeMCPAction.CANCEL_SUBSCRIPTION,),
    ),
}


def _property(description: str, value_type: str = "string", **extra: Any) -> dict[str, Any]:
    return {"type": value_type, "description": description, **extra}


def _object_schema(
    properties: Mapping[str, dict[str, Any]] | None = None,
    required: Iterable[str] = (),
) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": dict(properties or {}),
        "required": list(required),
        # The live vendor schema remains authoritative and can add optional
        # fields between Manor releases.
        "additionalProperties": True,
    }


_PAGINATION = {
    "page": _property("Page number.", "integer", minimum=1),
    "page_size": _property("Maximum number of results.", "integer", minimum=1),
}
_ITEMS = _property(
    "Items in the invoice or order.",
    "array",
    items={
        "type": "object",
        "properties": {
            "name": _property("Item name."),
            "quantity": _property("Item quantity.", "number"),
            "unit_price": _property("Price per unit.", "number"),
        },
        "required": ["name", "quantity", "unit_price"],
        "additionalProperties": True,
    },
)


_PAYPAL_SCHEMAS: Mapping[PayPalMCPAction, dict[str, Any]] = {
    PayPalMCPAction.CREATE_INVOICE: _object_schema(
        {"recipient_email": _property("Invoice recipient email."), "items": _ITEMS},
        ("recipient_email", "items"),
    ),
    PayPalMCPAction.CREATE_RECURRING_SERIES: _object_schema(
        {
            "recipient_email": _property("Recurring invoice recipient email."),
            "items": _ITEMS,
            "frequency": _property("Recurring invoice schedule."),
        },
        ("recipient_email", "items", "frequency"),
    ),
    PayPalMCPAction.LIST_INVOICES: _object_schema({**_PAGINATION, "status": _property("Invoice status filter.")}),
    PayPalMCPAction.GET_INVOICE: _object_schema({"invoice_id": _property("PayPal invoice ID.")}, ("invoice_id",)),
    PayPalMCPAction.SEND_INVOICE: _object_schema({"invoice_id": _property("PayPal invoice ID.")}, ("invoice_id",)),
    PayPalMCPAction.SEND_INVOICE_REMINDER: _object_schema(
        {"invoice_id": _property("PayPal invoice ID.")}, ("invoice_id",)
    ),
    PayPalMCPAction.CANCEL_SENT_INVOICE: _object_schema(
        {"invoice_id": _property("PayPal invoice ID.")}, ("invoice_id",)
    ),
    PayPalMCPAction.GENERATE_INVOICE_QR_CODE: _object_schema(
        {"invoice_id": _property("PayPal invoice ID.")}, ("invoice_id",)
    ),
    PayPalMCPAction.CREATE_ORDER: _object_schema(
        {"items": _ITEMS, "currency": _property("Three-letter currency code.")},
        ("items", "currency"),
    ),
    PayPalMCPAction.GET_ORDER: _object_schema({"order_id": _property("PayPal order ID.")}, ("order_id",)),
    PayPalMCPAction.PAY_ORDER: _object_schema({"order_id": _property("Authorized PayPal order ID.")}, ("order_id",)),
    PayPalMCPAction.CREATE_REFUND: _object_schema(
        {
            "capture_id": _property("Captured payment ID."),
            "amount": _property("Refund amount; omit for a full refund.", "number"),
            "currency": _property("Three-letter currency code."),
        },
        ("capture_id",),
    ),
    PayPalMCPAction.GET_REFUND: _object_schema({"refund_id": _property("PayPal refund ID.")}, ("refund_id",)),
    PayPalMCPAction.LIST_DISPUTES: _object_schema({"status": _property("Dispute status filter.")}),
    PayPalMCPAction.GET_DISPUTE: _object_schema({"dispute_id": _property("PayPal dispute ID.")}, ("dispute_id",)),
    PayPalMCPAction.ACCEPT_DISPUTE_CLAIM: _object_schema(
        {"dispute_id": _property("PayPal dispute ID.")}, ("dispute_id",)
    ),
    PayPalMCPAction.CREATE_PRODUCT: _object_schema(
        {
            "name": _property("Product name."),
            "type": _property("Product type.", enum=["PHYSICAL", "DIGITAL", "SERVICE"]),
        },
        ("name", "type"),
    ),
    PayPalMCPAction.LIST_PRODUCTS: _object_schema(_PAGINATION),
    PayPalMCPAction.SHOW_PRODUCT_DETAILS: _object_schema(
        {"product_id": _property("PayPal product ID.")}, ("product_id",)
    ),
    PayPalMCPAction.CREATE_SUBSCRIPTION_PLAN: _object_schema(
        {
            "product_id": _property("PayPal product ID."),
            "name": _property("Subscription plan name."),
            "billing_cycles": _property("Trial and regular billing cycles.", "array", items={"type": "object"}),
            "payment_preferences": _property("Subscription payment preferences.", "object"),
        },
        ("product_id", "name", "billing_cycles", "payment_preferences"),
    ),
    PayPalMCPAction.LIST_SUBSCRIPTION_PLANS: _object_schema(
        {**_PAGINATION, "product_id": _property("Optional product ID filter.")}
    ),
    PayPalMCPAction.SHOW_SUBSCRIPTION_PLAN_DETAILS: _object_schema(
        {"billing_plan_id": _property("PayPal billing plan ID.")}, ("billing_plan_id",)
    ),
    PayPalMCPAction.CREATE_SUBSCRIPTION: _object_schema(
        {
            "plan_id": _property("PayPal subscription plan ID."),
            "subscriber": _property("Subscriber details.", "object"),
        },
        ("plan_id",),
    ),
    PayPalMCPAction.SHOW_SUBSCRIPTION_DETAILS: _object_schema(
        {"subscription_id": _property("PayPal subscription ID.")}, ("subscription_id",)
    ),
    PayPalMCPAction.UPDATE_SUBSCRIPTION: _object_schema(
        {
            "subscription_id": _property("PayPal subscription ID."),
            "fixed_price": _property("Replacement fixed-price details.", "object"),
            "shipping_amount": _property("Replacement shipping amount."),
        },
        ("subscription_id",),
    ),
    PayPalMCPAction.CANCEL_SUBSCRIPTION: _object_schema(
        {"subscription_id": _property("PayPal subscription ID."), "reason": _property("Cancellation reason.")},
        ("subscription_id",),
    ),
    PayPalMCPAction.CREATE_SHIPMENT_TRACKING: _object_schema(
        {
            "tracking_number": _property("Shipment tracking number."),
            "transaction_id": _property("Related PayPal transaction ID."),
            "carrier": _property("Shipping carrier."),
            "order_id": _property("Related PayPal order ID."),
            "status": _property(
                "Shipment status.", enum=["ON_HOLD", "SHIPPED", "DELIVERED", "CANCELLED", "LOCAL_PICKUP"]
            ),
        },
        ("tracking_number", "transaction_id", "carrier"),
    ),
    PayPalMCPAction.GET_SHIPMENT_TRACKING: _object_schema(
        {"order_id": _property("PayPal order ID."), "transaction_id": _property("Related transaction ID.")},
        ("order_id",),
    ),
    PayPalMCPAction.UPDATE_SHIPMENT_TRACKING: _object_schema(
        {
            "transaction_id": _property("Related PayPal transaction ID."),
            "tracking_number": _property("Existing tracking number."),
            "new_tracking_number": _property("Replacement tracking number."),
            "status": _property("Shipment status."),
            "carrier": _property("Shipping carrier."),
        },
        ("transaction_id", "tracking_number", "status"),
    ),
    PayPalMCPAction.LIST_TRANSACTIONS: _object_schema(
        {
            "start_date": _property("Inclusive ISO-8601 start date."),
            "end_date": _property("Inclusive ISO-8601 end date."),
        },
    ),
    PayPalMCPAction.GET_MERCHANT_INSIGHTS: _object_schema(
        {
            "start_date": _property("Inclusive start date."),
            "end_date": _property("Inclusive end date."),
            "insight_type": _property("Insight type.", enum=["ORDERS", "SALES"]),
            "time_interval": _property(
                "Aggregation interval.", enum=["DAILY", "WEEKLY", "MONTHLY", "QUARTERLY", "YEARLY"]
            ),
        },
        ("start_date", "end_date", "insight_type", "time_interval"),
    ),
}


_STRIPE_SCHEMAS: Mapping[StripeMCPAction, dict[str, Any]] = {
    StripeMCPAction.GET_STRIPE_ACCOUNT_INFO: _object_schema(),
    StripeMCPAction.RETRIEVE_BALANCE: _object_schema(),
    StripeMCPAction.CREATE_COUPON: _object_schema(
        {
            "percent_off": _property("Percentage discount.", "number"),
            "duration": _property("Coupon duration."),
        },
        ("duration",),
    ),
    StripeMCPAction.LIST_COUPONS: _object_schema({"limit": _property("Maximum results.", "integer")}),
    StripeMCPAction.CREATE_CUSTOMER: _object_schema(
        {"email": _property("Customer email."), "name": _property("Customer name.")}
    ),
    StripeMCPAction.LIST_CUSTOMERS: _object_schema({"email": _property("Optional email filter.")}),
    StripeMCPAction.LIST_DISPUTES: _object_schema({"payment_intent": _property("PaymentIntent filter.")}),
    StripeMCPAction.UPDATE_DISPUTE: _object_schema(
        {
            "dispute": _property("Dispute ID."),
            "evidence": _property("Dispute evidence fields.", "object"),
        },
        ("dispute", "evidence"),
    ),
    StripeMCPAction.CREATE_INVOICE: _object_schema(
        {"customer": _property("Customer ID."), "collection_method": _property("Collection method.")},
        ("customer",),
    ),
    StripeMCPAction.CREATE_INVOICE_ITEM: _object_schema(
        {
            "customer": _property("Customer ID."),
            "invoice": _property("Invoice ID."),
            "price": _property("Price ID."),
            "amount": _property("Amount in the smallest currency unit.", "integer"),
            "currency": _property("Three-letter currency code."),
        },
        ("customer",),
    ),
    StripeMCPAction.FINALIZE_INVOICE: _object_schema(
        {"invoice": _property("Invoice ID.")},
        ("invoice",),
    ),
    StripeMCPAction.LIST_INVOICES: _object_schema({"customer": _property("Customer ID filter.")}),
    StripeMCPAction.CREATE_PAYMENT_LINK: _object_schema(
        {"line_items": _property("Price and quantity entries.", "array", items={"type": "object"})},
        ("line_items",),
    ),
    StripeMCPAction.LIST_PAYMENT_INTENTS: _object_schema({"customer": _property("Customer ID filter.")}),
    StripeMCPAction.CREATE_PRICE: _object_schema(
        {
            "product": _property("Product ID."),
            "currency": _property("Three-letter currency code."),
            "unit_amount": _property("Amount in the smallest currency unit.", "integer"),
        },
        ("product", "currency", "unit_amount"),
    ),
    StripeMCPAction.LIST_PRICES: _object_schema({"product": _property("Product ID filter.")}),
    StripeMCPAction.CREATE_PRODUCT: _object_schema(
        {"name": _property("Product name.")},
        ("name",),
    ),
    StripeMCPAction.LIST_PRODUCTS: _object_schema({"active": _property("Filter by active status.", "boolean")}),
    StripeMCPAction.SEARCH_STRIPE_DOCUMENTATION: _object_schema(
        {"question": _property("Question to search in Stripe documentation.")}, ("question",)
    ),
    StripeMCPAction.CREATE_REFUND: _object_schema(
        {
            "payment_intent": _property("PaymentIntent ID."),
            "charge": _property("Charge ID."),
            "amount": _property("Partial amount; omit for a full refund.", "integer"),
            "reason": _property("Refund reason."),
        }
    ),
    StripeMCPAction.CANCEL_SUBSCRIPTION: _object_schema(
        {"subscription": _property("Subscription ID.")},
        ("subscription",),
    ),
    StripeMCPAction.LIST_SUBSCRIPTIONS: _object_schema({"customer": _property("Customer ID filter.")}),
    StripeMCPAction.UPDATE_SUBSCRIPTION: _object_schema(
        {
            "subscription": _property("Subscription ID."),
            "items": _property("Replacement subscription items.", "array", items={"type": "object"}),
        },
        ("subscription",),
    ),
    StripeMCPAction.SEARCH_STRIPE_RESOURCES: _object_schema(
        {"query": _property("Stripe resource search query.")},
        ("query",),
    ),
    StripeMCPAction.FETCH_STRIPE_RESOURCES: _object_schema(
        {"id": _property("Stripe resource identifier or URI.")},
        ("id",),
    ),
}


_FALLBACK_SCHEMAS: Mapping[
    OfficialRemoteMCPProvider,
    Mapping[StrEnum, dict[str, Any]],
] = {
    OfficialRemoteMCPProvider.PAYPAL: _PAYPAL_SCHEMAS,
    OfficialRemoteMCPProvider.STRIPE: _STRIPE_SCHEMAS,
}


class OfficialRemoteMCPToolSchemaFactory:
    """Normalize live ``tools/list`` data or build a bounded local fallback."""

    @classmethod
    def create(
        cls,
        provider: OfficialRemoteMCPProvider | str,
        *,
        discovered_tools: Iterable[Mapping[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        resolved_provider = OfficialRemoteMCPProvider(provider)
        provider_name = resolved_provider.value.capitalize()
        if discovered_tools is not None:
            schemas: list[dict[str, Any]] = []
            for tool in discovered_tools:
                if not isinstance(tool, Mapping):
                    continue
                name = str(tool.get("name") or "").strip()
                input_schema = tool.get("inputSchema")
                if not name or not isinstance(input_schema, Mapping):
                    continue
                schema: dict[str, Any] = {
                    "name": name,
                    "description": str(
                        tool.get("description") or f"Official {provider_name} MCP action: {name.replace('_', ' ')}."
                    ),
                    "parameters": dict(input_schema),
                }
                output_schema = tool.get("outputSchema")
                if not isinstance(output_schema, Mapping):
                    output_schema = tool.get("output_schema")
                if isinstance(output_schema, Mapping):
                    schema["outputSchema"] = dict(output_schema)
                annotations = tool.get("annotations")
                if isinstance(annotations, Mapping):
                    schema["annotations"] = dict(annotations)
                schemas.append(schema)
            return schemas

        definition = OfficialRemoteMCPFactory.create(resolved_provider)
        schemas = []
        for action_spec in definition.action_specs:
            schemas.append(
                {
                    "name": action_spec.name,
                    "description": action_spec.description,
                    "parameters": dict(action_spec.input_schema),
                }
            )
        return schemas


class OfficialRemoteMCPFactory:
    """Build effective endpoint, action, effect, OAuth, and schema contracts."""

    @staticmethod
    def paypal_environment(value: str | None = None) -> PayPalMCPEnvironment:
        raw = value if value is not None else os.getenv("PAYPAL_ENVIRONMENT", "sandbox")
        normalized = str(raw).strip().lower()
        if normalized == "live":
            normalized = PayPalMCPEnvironment.PRODUCTION.value
        try:
            return PayPalMCPEnvironment(normalized)
        except ValueError as exc:
            raise ValueError("PAYPAL_ENVIRONMENT must be 'sandbox', 'live', or 'production'.") from exc

    @classmethod
    def create(
        cls,
        provider: OfficialRemoteMCPProvider | str,
        *,
        paypal_environment: str | None = None,
    ) -> OfficialRemoteMCPDefinition:
        resolved_provider = OfficialRemoteMCPProvider(provider)
        if resolved_provider is OfficialRemoteMCPProvider.STRIPE:
            endpoint = "https://mcp.stripe.com"
        elif resolved_provider is OfficialRemoteMCPProvider.ROBINHOOD:
            # No guessed schemas for financial tools. Until this user's
            # official tools/list succeeds there is no callable fallback.
            return OfficialRemoteMCPDefinition(
                provider=resolved_provider,
                endpoint=ROBINHOOD_MCP_ENDPOINT,
                action_specs=(),
            )
        elif cls.paypal_environment(paypal_environment) is PayPalMCPEnvironment.PRODUCTION:
            endpoint = "https://mcp.paypal.com/http"
        else:
            endpoint = "https://mcp.sandbox.paypal.com/http"
        action_enum = _ACTION_ENUMS[resolved_provider]
        effects = _ACTION_EFFECTS[resolved_provider]
        fallbacks = _FALLBACK_SCHEMAS[resolved_provider]
        provider_name = resolved_provider.value.capitalize()
        return OfficialRemoteMCPDefinition(
            provider=resolved_provider,
            endpoint=endpoint,
            action_specs=tuple(
                OfficialRemoteMCPActionSpec(
                    name=action.value,
                    effect=effects[action],
                    description=(f"Official {provider_name} MCP action: {action.value.replace('_', ' ')}."),
                    input_schema=dict(fallbacks[action]),
                )
                for action in action_enum
            ),
        )

    @classmethod
    def action_effect(
        cls,
        provider: OfficialRemoteMCPProvider | str,
        action: StrEnum | str,
    ) -> MCPActionEffect:
        definition = cls.create(provider)
        action_name = action.value if isinstance(action, StrEnum) else str(action)
        if action_name in definition.read_only_actions:
            return MCPActionEffect.READ
        spec = next(
            (item for item in definition.action_specs if item.name == action_name),
            None,
        )
        if spec is None:
            raise ValueError(f"Unknown {definition.provider.value} MCP action: {action_name}")
        return spec.effect

    @classmethod
    def resolve_action_effect(
        cls,
        provider: OfficialRemoteMCPProvider | str,
        action: StrEnum | str,
        *,
        annotations: Mapping[str, Any] | None = None,
    ) -> MCPActionEffect:
        """Resolve known effects and safely classify vendor-added actions."""

        try:
            return cls.action_effect(provider, action)
        except ValueError:
            if annotations and annotations.get("destructiveHint") is True:
                return MCPActionEffect.DESTRUCTIVE
            return MCPActionEffect.WRITE

    @classmethod
    def paypal_oauth(
        cls,
        *,
        paypal_environment: str | None = None,
    ) -> OfficialRemoteMCPOAuthDefinition:
        production = cls.paypal_environment(paypal_environment) is PayPalMCPEnvironment.PRODUCTION
        host = "paypal.com" if production else "sandbox.paypal.com"
        api_host = "api-m.paypal.com" if production else "api-m.sandbox.paypal.com"
        return OfficialRemoteMCPOAuthDefinition(
            authorize_url=f"https://www.{host}/connect",
            token_url=f"https://{api_host}/v1/oauth2/token",
            identity_url=f"https://{api_host}/v1/identity/oauth2/userinfo",
            scopes=" ".join(scope.value for scope in PayPalOAuthScope),
            client_id_env="PAYPAL_CLIENT_ID",
            client_secret_env="PAYPAL_CLIENT_SECRET",
        )

    @classmethod
    def tool_schemas(
        cls,
        provider: OfficialRemoteMCPProvider | str,
        *,
        discovered_tools: Iterable[Mapping[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        return OfficialRemoteMCPToolSchemaFactory.create(
            provider,
            discovered_tools=discovered_tools,
        )

    @classmethod
    def tools_cache(
        cls,
        provider: OfficialRemoteMCPProvider | str,
        *,
        discovered_tools: Iterable[Mapping[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Build the credential-free catalog shared by Workflow surfaces."""
        resolved_provider = OfficialRemoteMCPProvider(provider)
        schemas = cls.tool_schemas(
            resolved_provider,
            discovered_tools=discovered_tools,
        )
        tools: list[dict[str, Any]] = []
        for schema in schemas:
            annotations = schema.get("annotations")
            effect = cls.resolve_action_effect(
                resolved_provider,
                schema["name"],
                annotations=(annotations if isinstance(annotations, Mapping) else None),
            )
            tool = {
                "name": schema["name"],
                "description": schema["description"],
                "inputSchema": schema["parameters"],
                "effect": effect.value,
            }
            if isinstance(annotations, Mapping):
                tool["annotations"] = dict(annotations)
            output_schema = schema.get("outputSchema")
            if isinstance(output_schema, Mapping):
                tool["outputSchema"] = dict(output_schema)
            tools.append(tool)
        return {
            "source": "official_fallback" if discovered_tools is None else "discovery",
            "tools": tools,
        }
