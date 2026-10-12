---
name: marketing-industry
description: Shape marketing around the business model, customer decision, and claims that can be supported. Use as the industry axis of a campaign; platform and marketing method are chosen separately.
---

# Marketing Industry

The industry defines **what a buyer needs to believe before acting** and which claims the business can support. Classify the actual business model and customer: a restaurant selling catering to offices uses food-service proof with a B2B inquiry path; a machine vendor serving restaurants sells equipment to operators, not meals to diners.

Read only the relevant bundled reference with `read_file`:
Only the reference paths in this table are readable in this skill. A parent may pass platform or method results that name their own files; use those results as context and never call `read_file` for a path absent from this table, including `references/search-and-maps.md`.

| Business and buyer | Reference |
| --- | --- |
| Restaurant, café, bar, bakery, takeaway/delivery, catering, food service | `references/restaurant.md` |
| SaaS, app, digital service, B2B software, product UI | `references/digital-product.md` |
| Physical goods, consumer products, equipment, supplies, ecommerce | `references/physical-product.md` |
| Local service, hospitality, education, regulated or unfamiliar industry | `references/services-and-other.md` |

Read two references when the offer truly spans them, such as restaurant equipment or hospitality dining. For an unfamiliar sector, derive the buying decision, evidence, decision cycle, disallowed assumptions, and next action from the user's facts. Do not claim sector-specific expertise that has not been verified.

Return a concise **industry overlay** with: buyer and purchase occasion/problem; actual offer/SKU/service; one viable value proposition; proof required and available; unsupported claims to remove or label; operational details needed to fulfill the promise; and the next business action. Include a claim ledger for purchase-affecting assertions with source or `[verification needed]`.

Never fabricate reviews, prices, health or financial results, endorsements, availability, certifications, sourcing, dietary labels, UI features, integrations, or comparative superiority. Generated imagery can illustrate a concept but cannot prove a real product, dish, venue, or customer outcome. Preserve customer privacy and rights for testimonials and real footage. Return guidance to `marketing-campaign`; this specialist does not publish or generate the final video.
