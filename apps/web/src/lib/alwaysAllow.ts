import { api } from "./api";
import { t } from "./i18n";
import { ALWAYS_ALLOW_CHOICE } from "./recommendedOptions";

/**
 * "Always allow" on an approval backed by a request: save the allow rule the
 * server offers for this exact action, then approve.
 *
 * Task approvals decide and save atomically. Other origins only save the rule
 * here and approve through the card's own path (`approveOnce`), so a chat or
 * Workflow approval resumes exactly as an ordinary click would.
 */
export async function alwaysAllow(requestId: string, approveOnce: () => unknown): Promise<void> {
  const projection = await api.approvalCommands.get(requestId);
  const option = projection.rule_options.find((item) => item.coverage === "all_targets_for_action")
    || projection.rule_options[0];
  const atomic = projection.available_decisions.includes(ALWAYS_ALLOW_CHOICE);
  if (!option || !projection.policy_version || (!atomic && !projection.available_decisions.includes("save_rule"))) {
    throw new Error(t("approval_settings.unavailable"));
  }
  await api.approvalCommands.saveRule({
    request: projection.request_ref,
    choice: atomic ? ALWAYS_ALLOW_CHOICE : "save_rule",
    expectedDigest: projection.digest,
    idempotencyKey: crypto.randomUUID(),
    expectedPolicyVersion: projection.policy_version,
    optionId: option.id,
  });
  if (!atomic) await approveOnce();
}
