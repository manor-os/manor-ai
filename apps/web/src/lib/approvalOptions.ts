export const APPROVAL_CHOICE_APPROVE = "approve";
export const APPROVAL_CHOICE_ALWAYS_APPROVE = "always_approve";
export const APPROVAL_CHOICE_REJECT = "reject";

export const DEFAULT_APPROVAL_OPTIONS = [
  APPROVAL_CHOICE_APPROVE,
  APPROVAL_CHOICE_ALWAYS_APPROVE,
  APPROVAL_CHOICE_REJECT,
];

/** Approve-once / reject, for a card whose subject HAS no standing version.
 *
 *  Not "this is too dangerous to blanket-approve" — the user is the authority
 *  on that, and `never_allow` is the only hard block. A `review` card is a
 *  verdict on ONE diff, so "always apply whatever the next draft says" is not
 *  a subject anyone can consent to. Mirrors `one_time_approval_options` in
 *  packages/core/services/hitl_options.py. */
export const ONE_TIME_APPROVAL_OPTIONS = [
  APPROVAL_CHOICE_APPROVE,
  APPROVAL_CHOICE_REJECT,
];

/** Filter a card's option list down to the one-time vocabulary. Applied to
 *  whatever the blob carries, so a card minted with the three-button list
 *  still renders no "Always" on a surface that has no standing version. */
export function oneTimeApprovalOptions(options?: string[] | null): string[] {
  const chosen = options && options.length ? options : ONE_TIME_APPROVAL_OPTIONS;
  return chosen.filter(
    (opt) => String(opt || "").trim().toLowerCase() !== APPROVAL_CHOICE_ALWAYS_APPROVE,
  );
}
