"""One approval policy model and pure, target-aware policy evaluator.

Authentication, budgets, lifecycle, requests and receipts remain outside this
module.  In particular an ``allow`` result cannot resolve a pending request.
"""
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ApprovalMode = Literal['confirm_changes', 'risk_based', 'automatic']
ApprovalEffect = Literal['allow', 'ask', 'deny']
ApprovalCategory = Literal['read', 'browser', 'files', 'communication', 'publishing', 'deletion', 'purchases', 'automation', 'workspace', 'unknown']
ApprovalImpact = Literal['read', 'reversible', 'external', 'destructive', 'unknown']


class PolicyModel(BaseModel):
    model_config = ConfigDict(extra='forbid')


class ApprovalScope(PolicyModel):
    kind: Literal['account', 'personal', 'workspace', 'room']
    id: str = Field(min_length=1, max_length=128)
    entity_id: str = Field(min_length=1, max_length=128)


class CategorySelector(PolicyModel):
    kind: Literal['category'] = 'category'
    category: ApprovalCategory


class ActionSelector(PolicyModel):
    kind: Literal['action'] = 'action'
    action_key: str = Field(min_length=1, max_length=200, pattern=r'^[a-zA-Z0-9][a-zA-Z0-9_.:/-]*$')


class ResourceSelector(ActionSelector):
    kind: Literal['resource'] = 'resource'
    account_id: str | None = Field(default=None, min_length=1, max_length=128)
    resource_kind: str = Field(min_length=1, max_length=80)
    resource_ids: list[str] = Field(min_length=1, max_length=100)

    @field_validator('resource_ids')
    @classmethod
    def normalize_ids(cls, values: list[str]) -> list[str]:
        if any(not v or v != v.strip() or len(v) > 1000 for v in values):
            raise ValueError('resource IDs must be nonempty normalized IDs')
        return sorted(set(values))


Selector = Annotated[CategorySelector | ActionSelector | ResourceSelector, Field(discriminator='kind')]


class RuleDraft(PolicyModel):
    id: str | None = Field(default=None, min_length=1, max_length=128)
    effect: ApprovalEffect
    selector: Selector


class ApprovalRule(PolicyModel):
    id: str = Field(min_length=1, max_length=128)
    effect: ApprovalEffect
    selector: Selector
    source_kind: Literal['user', 'workspace_rule'] = 'user'
    source_id: str | None = Field(default=None, max_length=128)

    @model_validator(mode='after')
    def valid_source(self):
        if self.source_kind == 'workspace_rule' and not self.source_id:
            raise ValueError('workspace rules require a source ID')
        if self.source_kind == 'user' and self.source_id is not None:
            raise ValueError('user rules cannot specify a source ID')
        return self


class ApprovalPolicy(PolicyModel):
    mode: ApprovalMode = 'risk_based'
    rules: list[ApprovalRule] = Field(default_factory=list, max_length=200)


class ApprovalPolicyOverride(PolicyModel):
    mode: ApprovalMode | None = None
    rules: list[ApprovalRule] = Field(default_factory=list, max_length=200)


class ApprovalPolicyBinding(PolicyModel):
    root_entity_id: str = Field(min_length=1, max_length=128)
    policy: ApprovalPolicyOverride = Field(default_factory=ApprovalPolicyOverride)


class PolicyDraft(PolicyModel):
    mode: ApprovalMode | None
    user_rules: list[RuleDraft] = Field(default_factory=list, max_length=200)


class PolicyVersion(PolicyModel):
    scope: ApprovalScope
    revision: int = Field(ge=1)


class PolicySnapshot(PolicyModel):
    scope: ApprovalScope
    revision: int = Field(ge=1)
    version: str
    policy: ApprovalPolicy | ApprovalPolicyOverride
    effective_policy: ApprovalPolicy
    root_scope: ApprovalScope | None = None
    policy_versions: list[PolicyVersion]
    inheritance: Literal['root', 'following', 'custom']


class ResolvedPolicyChain(PolicyModel):
    root: PolicySnapshot
    local: PolicySnapshot | None = None

    @model_validator(mode='after')
    def validate_chain(self):
        if self.root.scope.kind != 'account' or self.root.policy.mode is None:
            raise ValueError('policy chain requires an account default')
        if self.local and (self.local.scope.kind == 'account' or self.local.root_scope != self.root.scope):
            raise ValueError('local policy is bound to a different account')
        return self


class ApprovalTarget(PolicyModel):
    account_id: str | None = None
    resource_kind: str
    resource_id: str


class ActionFacts(PolicyModel):
    action_key: str
    category: ApprovalCategory = 'unknown'
    impact: ApprovalImpact = 'unknown'
    risk_level: Literal['low', 'medium', 'high'] = 'medium'
    targets: list[ApprovalTarget] = Field(default_factory=list, max_length=1000)
    # ``leaf_only`` actions route, ask or hand off; only an explicit
    # action/resource rule for the exact key applies to them.
    gate: Literal['policy', 'leaf_only'] = 'policy'
    # Other keys the same action was named by (a plan step's raw tool name):
    # explicit action/resource rules on them still apply.
    aliases: list[str] = Field(default_factory=list, max_length=8)
    # Keys whose explicit rules may only restrict (deny/ask), never allow:
    # a relay judged as the operation it guards keeps its own key here, so an
    # allow on the relay's generic key cannot approve the guarded operation.
    restrict_only_keys: list[str] = Field(default_factory=list, max_length=8)


class TargetDecision(PolicyModel):
    target: ApprovalTarget | None = None
    outcome: ApprovalEffect
    reason_code: str
    reason_params: dict = Field(default_factory=dict)
    rule_id: str | None = None
    scope: ApprovalScope
    policy_revision: int
    source: Literal['mode', 'rule', 'constraint']


class PolicyEvaluation(TargetDecision):
    target_decisions: list[TargetDecision] = Field(default_factory=list)


def _matches(rule: ApprovalRule, facts: ActionFacts, target: ApprovalTarget | None) -> bool:
    selector = rule.selector
    if isinstance(selector, CategorySelector):
        return selector.category == facts.category
    if selector.action_key != facts.action_key and selector.action_key not in facts.aliases:
        return False
    if isinstance(selector, ResourceSelector):
        return bool(target and selector.account_id == target.account_id
                    and selector.resource_kind == target.resource_kind
                    and target.resource_id in selector.resource_ids)
    return True


def _baseline(mode: ApprovalMode, facts: ActionFacts) -> ApprovalEffect:
    """The chosen approval mode decides; explicit rules were matched before this.

    An action whose effect the classifier could not name follows the mode
    like any other: "Run automatically" runs it and "Confirm changes" asks.
    "Confirm by risk" asks, since an unnamed effect cannot be shown routine.
    """
    if facts.impact == 'read':
        return 'allow'
    if facts.category == 'browser' and facts.impact == 'reversible':
        return 'allow'
    if mode == 'confirm_changes':
        return 'ask'
    if mode == 'automatic':
        return 'allow'
    if facts.impact == 'unknown':
        return 'ask'
    return 'allow' if facts.impact == 'reversible' and facts.risk_level != 'high' else 'ask'


def evaluate_policy(chain: ResolvedPolicyChain, facts: ActionFacts) -> PolicyEvaluation:
    """Evaluate every target using the same policy on every execution surface."""
    decisions: list[TargetDecision] = []
    mode_owner = chain.local if chain.local and chain.local.policy.mode is not None else chain.root
    for target in facts.targets or [None]:
        matches = []
        for locality, snapshot in enumerate([chain.root, chain.local]):
            if snapshot is None:
                continue
            for rule in snapshot.policy.rules:
                # A leaf_only fact is judged only by explicit action/resource rules.
                if facts.gate == 'leaf_only' and rule.selector.kind == 'category':
                    continue
                if (rule.effect == 'allow' and rule.selector.kind != 'category'
                        and rule.selector.action_key in facts.restrict_only_keys):
                    continue
                if _matches(rule, facts, target):
                    precision = {'category': 1, 'action': 2, 'resource': 3}[rule.selector.kind]
                    matches.append((rule.effect == 'deny', precision, locality, rule.effect == 'ask', rule.id, rule, snapshot))
        if matches:
            *_, rule, owner = max(matches, key=lambda row: row[:5])
            decisions.append(TargetDecision(target=target, outcome=rule.effect, reason_code='approval.rule',
                rule_id=rule.id, scope=owner.scope, policy_revision=owner.revision, source='rule',
                reason_params={'inherited': owner is chain.root and chain.local is not None}))
        elif facts.gate == 'leaf_only' and facts.impact != 'unknown':
            # A leaf_only fact with an unknown effect gets the mode baseline.
            decisions.append(TargetDecision(target=target, outcome='allow', reason_code='approval.leaf_only',
                reason_params={'mode': mode_owner.policy.mode, 'impact': facts.impact},
                scope=mode_owner.scope, policy_revision=mode_owner.revision, source='mode'))
        else:
            decisions.append(TargetDecision(target=target, outcome=_baseline(mode_owner.policy.mode, facts),
                reason_code='approval.unknown_impact' if facts.impact == 'unknown' else 'approval.mode',
                reason_params={'mode': mode_owner.policy.mode, 'impact': facts.impact},
                scope=mode_owner.scope, policy_revision=mode_owner.revision, source='mode'))
    result = max(decisions, key=lambda item: {'allow': 0, 'ask': 1, 'deny': 2}[item.outcome])
    return PolicyEvaluation(**result.model_dump(), target_decisions=decisions)


def __getattr__(name: str):
    # ACTION_CATALOG moved to action_effects (derived from the declarations);
    # this module keeps re-exporting the same object. Lazy: action_effects
    # imports this module's category/impact types.
    if name == 'ACTION_CATALOG':
        from packages.core.governance.action_effects import ACTION_CATALOG

        return ACTION_CATALOG
    raise AttributeError(f'module {__name__!r} has no attribute {name!r}')


def __dir__() -> list[str]:
    return sorted({*globals(), 'ACTION_CATALOG'})
