---
name: marketing-method
description: "Choose and execute the growth mechanism for a campaign: organic, paid, search/local, lifecycle, creator/partner, referral, conversion, or launch. Use as the method axis; platform and industry are selected separately."
---

# Marketing Method

The method is **how demand is created or a buyer moves to action**. Respect an explicit method; otherwise choose from goal, purchase stage, budget, time horizon, owned reach/consent, existing assets, and sales cycle. Recommend one primary mechanism and only necessary support. “Ads” names paid acquisition but still needs a platform and industry proof; “email” names a destination while the method might be welcome, win-back, education, or conversion.

Read only the relevant bundled reference with `read_file`:
Only the reference paths in this table are readable in this skill. A parent may pass platform or industry results that name their own files; use those results as context and never call `read_file` for a path absent from this table, including `references/restaurant.md` or `references/search-and-maps.md`.

| Mechanism | Reference |
| --- | --- |
| Organic editorial, content series, community education | `references/organic-content.md` |
| Paid social/search/video, retargeting, performance ads | `references/paid-acquisition.md` |
| SEO, search intent, maps and local discovery | `references/search-local.md` |
| Email, SMS, CRM, onboarding, win-back, retention | `references/lifecycle.md` |
| Creator, affiliate, partnership, referral, loyalty | `references/partners-and-referrals.md` |
| Landing-page conversion, launch, opening, event, promotion | `references/conversion-and-launch.md` |

Read two references when the user requests a genuine combined mechanism, such as organic content that feeds a paid test or a launch followed by lifecycle messages. Do not inflate a small request into every tactic. For an unlisted method, name its causal path from asset to business action and the evidence required to evaluate it.

Return a concise **method overlay**: why this method fits, audience stage/eligibility, concrete sequence of actions, required assets and data, primary metric, test or iteration rule, and execution gates. Draft copy or structure only if requested by the parent campaign. Avoid invented forecasts, ROI, list consent, baselines, or causal lift. For paid and outbound work, an approved draft does not authorize spend or sending; exact account, audience, payload, timing, scope, and Manor's normal gates apply.
