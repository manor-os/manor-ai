# Evidence Protocol

Use this protocol before scoring. The goal is not to collect the largest number
of sources; it is to distinguish decision-relevant evidence from plausible noise.

## Claim labels

- **Verified source** — exact current source was opened and supports the claim
- **Provided fact** — present in a user-selected record or directly stated by the user
- **Observed behavior** — documented action, effort, usage, purchase, workaround, or consequence
- **Founder report** — founder's recollection or interpretation, not independently checked
- **Estimate** — calculation with visible assumptions
- **Inference** — analytical interpretation of available evidence
- **Assumption** — untested belief required by the business model
- **Unknown** — evidence unavailable or scope did not include it

Do not upgrade a founder report to verified fact by repeating it confidently.

## Evidence levels

Assign levels to specific hypotheses, not to the whole idea.

| Level | Meaning | Examples | What it can support |
|---|---|---|---|
| E0 | Unsupported | founder belief, analogy, AI-generated persona | hypothesis only |
| E1 | Indication | one relevant interview, request, review, or credible secondary source | reason to investigate |
| E2 | Repeated signal | multiple independent relevant records with consistent context | directional problem confidence |
| E3 | Behavior | current workaround, active search, meaningful time/cost/risk, completed task behavior | stronger problem or channel evidence |
| E4 | Commitment | payment, pre-order, signed pilot, repeat usage, renewal, or comparable costly commitment | named hypothesis under tested conditions |

Payment can still be misleading if it came from friends, bundled favors, deep
discounts, or nonrepresentative buyers. Record context.

## Evidence ledger

```markdown
| ID | Hypothesis / claim | Label | Level | Source and date | Supports / contradicts | Relevance | Limitation |
|---|---|---|---:|---|---|---|---|
```

Maintain separate rows for:

- problem existence, frequency, and consequence
- buyer identity and decision authority
- current alternative and switching trigger
- willingness to pay and price
- acquisition channel and reach
- delivery feasibility and support burden
- cost, margin, retention, and repeatability
- legal, policy, safety, privacy, data, and vendor dependency

## Source handling

### Customer material

- Preserve participant and record boundaries.
- Count unique comparable sources and show the denominator.
- Exact quotes require exact source text; otherwise paraphrase.
- Ask about past behavior and current alternatives before hypothetical interest.
- Do not turn a requested feature into evidence that a buyer wants the proposed product.

### Public research

- Prefer primary sources for price, product, policy, regulation, and company facts.
- Use current independent sources to corroborate category claims and buyer voice.
- Open the page; a search result snippet is not evidence.
- Date claims and flag stale or version-specific material.
- Preserve conflicting sources and explain which claim remains uncertain.
- Follower counts, broad market reports, funding, and competitor existence are
  context, not proof that the target buyer has this problem or will pay this founder.

### Internal data

- Record population, time period, exclusions, and collection method.
- Separate correlation from cause.
- Do not combine interviews, survey rows, support tickets, and reviews into one
  prevalence percentage unless they share a valid denominator.
- Minimize personal and commercially sensitive data in the final artifact.

## Confidence

Set confidence independently of desirability:

- **Low:** mostly E0–E1, weak relevance, small/biased sample, stale or conflicting sources
- **Medium:** multiple relevant sources, some E2–E3 behavior, limitations remain
- **High:** relevant E3–E4 evidence from representative buyers plus corroboration

High confidence can support a negative verdict. Low confidence can accompany a
promising test. Do not treat confidence as optimism.

## Validation language

Allowed:

- `Five of eight interviewed agency owners described the same workaround.`
- `Two qualified buyers paid $650 under the stated pilot conditions.`
- `The problem hypothesis has E3 support; repeatability remains E0.`

Not allowed:

- `Customers love it.`
- `The market is validated.`
- `This idea has product-market fit.`
- `A 78 score means a 78% chance of success.`
