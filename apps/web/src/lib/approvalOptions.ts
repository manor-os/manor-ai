export const APPROVAL_CHOICE_APPROVE = "approve";
export const APPROVAL_CHOICE_ALWAYS_APPROVE = "always_approve";
export const APPROVAL_CHOICE_REVISE = "revise";
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

export const REVIEW_APPROVAL_OPTIONS = [
  APPROVAL_CHOICE_APPROVE,
  "request_changes",
  APPROVAL_CHOICE_REJECT,
];

/** Filter a card's option list down to the one-time vocabulary. Applied to
 *  whatever the blob carries, so a card minted with the three-button list
 *  still renders no "Always" on a surface that has no standing version. */
export function oneTimeApprovalOptions(options?: string[] | null): string[] {
  const chosen = options && options.length ? options : ONE_TIME_APPROVAL_OPTIONS;
  const filtered = chosen.filter(
    (opt) => String(opt || "").trim().toLowerCase() !== APPROVAL_CHOICE_ALWAYS_APPROVE,
  );
  return filtered.length ? filtered : [...ONE_TIME_APPROVAL_OPTIONS];
}

/** Typed review choices. Persisted legacy cards can carry a stale generic
 * option list, including an `always_approve`-only list. Reviews never have a
 * standing subject, so strip that value and fail closed to the review
 * vocabulary instead of letting ApprovalCard inject its generic defaults. */
export function reviewApprovalOptions(options?: string[] | null): string[] {
  const chosen = options && options.length ? options : REVIEW_APPROVAL_OPTIONS;
  if (chosen.some(
    (opt) => String(opt || "").trim().toLowerCase() === APPROVAL_CHOICE_ALWAYS_APPROVE,
  )) {
    return [...REVIEW_APPROVAL_OPTIONS];
  }
  const filtered = chosen.filter(
    (opt) => String(opt || "").trim().toLowerCase() !== APPROVAL_CHOICE_ALWAYS_APPROVE,
  );
  return filtered.length ? filtered : [...REVIEW_APPROVAL_OPTIONS];
}
