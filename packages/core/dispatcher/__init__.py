"""Dispatcher — atomic step→worker matchmaker.

Sits between PlanExecutor (DAG state) and Worker (actual execution):

  PlanExecutor                 Dispatcher                   Worker
  ─────────────────            ──────────────────           ──────────────
  marks step pending     →     checkout_steps()
                               (FOR UPDATE SKIP LOCKED)
                               creates work_lease           ← lease offered
                                                            executes step
                                                            calls back ↓
                               complete_lease() / fail_lease()
                               updates step + emits event
  next cycle picks up step.result

Public surface:

  checkout_steps_for_worker   atomic SELECT FOR UPDATE SKIP LOCKED that
                              returns N runnable steps the worker can
                              execute, marking each step running and
                              creating a WorkLease row.

  complete_lease              worker reports success — lease + step go
                              terminal, result lands on step.result.

  fail_lease                  worker reports failure — retry semantics
                              respect step.attempt_count vs max_attempts.

  lease_needs_human           worker hit a CAPTCHA / 2FA / approval
                              wall; lease pauses, step → waiting_human,
                              chat receives an interactive prompt.

  expire_leases               periodic sweep — leases past lease_until
                              are released (step→pending or failed if
                              attempts exhausted), credential subleases
                              revoked.

  validate_step_input         JSONSchema check on resolved params before
                              the lease goes out.

  validate_step_output        JSONSchema check on the worker's result
                              before complete_lease persists it.
"""
from packages.core.dispatcher.service import (
    Dispatcher,
    DispatchError,
    LeaseNotActive,
    NoMatchingSteps,
)
from packages.core.dispatcher.validation import (
    SchemaError,
    validate_step_input,
    validate_step_output,
)

__all__ = [
    "Dispatcher",
    "DispatchError",
    "LeaseNotActive",
    "NoMatchingSteps",
    "SchemaError",
    "validate_step_input",
    "validate_step_output",
]
