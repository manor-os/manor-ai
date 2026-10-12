---
name: marketing-campaign
description: Plan and produce broader marketing campaigns across platforms, industries, and methods. Use video-production directly when the requested deliverable is a video, whether the user supplies a brief or an approved script.
---

# Marketing Campaign

Build the smallest useful **platform × industry × method** combination for the user's goal. A platform is where a person encounters or acts on the message; an industry defines the buying context and claim constraints; a method is how demand or conversion is created.

## Dialogue entry

The user does **not** need to name or select this Skill. When invoked from a plain-language marketing request, do the routing yourself and continue to the requested deliverable. Typical requests include “帮餐厅做开业推广并增加预订”, “为软件产品做 LinkedIn 获客”, “设计小红书种草内容”, and “测试 Google Ads 的产品广告”. Their wording is an intent signal, not a rigid keyword list. A request for a finished video from an open brief or approved script belongs directly to `video-production`, including choosing its destination or adapting that video to a platform; Video Production combines `marketing-platform` guidance as needed. A request for broader campaign strategy, cross-channel decisions, or multiple campaign assets belongs here first.

Treat a brief as one of three jobs: **decide** a strategy/channel, **produce** specific campaign assets, or **execute** on an external platform. Complete the job requested. If the user asks for finished assets, a high-level plan alone is incomplete. If they ask only for advice, do not create files or launch media jobs without a need.

Reply in the user's language unless they request another language. Separate verified business facts from draft ideas: describe an unverified offer, review, menu item, or venue feature as a proposal, never as an existing fact.

## Route the brief

1. Read the brief, provided evidence, Workspace Knowledge, brand assets, and approved plans. Identify the business action to improve, audience, geography, offer, proof, language, budget if supplied, timing, requested deliverables, and available accounts/assets. Mark facts as verified, user-provided, assumed, or missing. Keep unknown prices, results, dates, reviews, certifications, and capabilities visible rather than inventing them. Ask only when a missing answer blocks the requested work; otherwise proceed with labeled assumptions.
2. Respect dimensions the user named. For open dimensions, compare likely options by audience intent, conversion proximity, proof available, production capacity, geographic fit, budget, and whether the destination can actually be used. Pick **one primary combination** and add another only for a distinct job. A platform is not a method: “小红书” is a destination, while “种草内容” is a method. “餐厅” is an industry, while “预订” is the conversion. For multiple requested platforms, adapt each asset rather than cloning a caption.
3. Invoke the built-in slugs `marketing-platform`, `marketing-industry`, and `marketing-method` with the brief, chosen or candidate dimensions, goal, assets, constraints, and missing facts as input. Pass each specialist the relevant facts and decisions as text; omit reference-file paths from other specialists' input, because each Skill can read only its own bundle. Each specialist reads only its own relevant bundled playbooks and returns a specific overlay. If an exact slug is unavailable, call `list_skills(category="marketing")` to discover the available suite; `marketing` is the shared category, while the three axis names are slugs. Reconcile any conflict in the order **user choice → verified industry facts → platform constraints → method tactic**. If a specialist is unavailable, continue with the same reasoning and disclose that limitation.
4. Discover a narrower installed skill when the job needs it: `platform-native-social` for its supported social networks, `restaurant-marketing-suite` for restaurant operations, `email-marketing` for an email sequence, `seo-audit` for a real site audit, or `marketing-strategy-pmm` for deeper product positioning. These are optional, and none replaces the three-axis decision or the requested deliverable.

Illustrative routing, subject to the real brief and evidence:

| Plain-language request | Platform × industry × method | First deliverable |
| --- | --- | --- |
| “帮新餐厅做小红书开业引流” | Xiaohongshu × restaurant × organic content/launch | Genuine venue/menu truth pack, cover/title, finished notes, reservation path |
| “软件产品要从 LinkedIn 获取演示预约” | LinkedIn × digital product × B2B organic or paid if requested | Proof-backed angle, native posts or ad draft, demo CTA |
| “让附近的人在地图上找到咖啡馆” | Search/maps × restaurant × local discovery | Listing accuracy worklist and verified visit/booking path |
| “给产品投 Google 搜索广告” | Google Ads × product × paid search | Query/ad/landing alignment, creative draft, conversion event and budget gate |

## Produce the work

Read `references/campaign-contract.md` when making a campaign pack or multiple assets. State the chosen combination and, when a choice was open, a close alternative briefly with a reason tied to the audience and conversion. Deliver the requested **actual copy, creative brief, landing-page content, offer, asset, or plan**. Each destination needs its own hook, proof, native structure, CTA, and success measure. Keep one source of truth for product and brand facts. Include source-to-claim traceability for any assertion likely to affect a purchase. A plan-only request ends with the plan.

Use native Manor tools that are actually available. `manor` can locate scoped Workspace/Knowledge material; inspect current schema before resource actions. Use `generate_file` when a durable campaign, content calendar, copy deck, or handoff file is requested, and return its real Knowledge/Document reference. Use `generate_image` for requested original imagery, with supplied real product or venue assets as the factual source. A proposed path or prompt is not a completed asset.

For a requested campaign video, write or use the approved script and invoke built-in `video-production` with the script, audience, destination, aspect ratio, runtime, claims/evidence, brand constraints, and the campaign's CTA. Include the selected `marketing-platform` overlay and its rule-verification state as text so Video Production can reuse those decisions without repeating platform planning. `marketing-campaign` owns the broader campaign; `video-production` owns each video's first/last-frame canvas, Manor media generation, selected editable assembly route, and verification. Continue its canvas-ready and production-in-progress stages under the user's original finished-video request; do not represent a script or canvas as a rendered video. For copy-only campaigns, do not invoke video tools.

Check current placement rules, formats, community rules, ad policies, and regulated-claim requirements through available official sources or authorized Manor research tools before publication or spend; name any rule that could not be verified. Do not hard-code mutable limits or claim that research happened when it did not.

## Delivery and action boundary

Make the campaign reviewable: objective, selected platform/industry/method, audience, evidence and open claims, deliverables with references, CTA, measurement, and next action. When the user requests execution, use only currently available connected tools and their normal Workspace authorization, approval, credit, and artifact paths. Drafting does not authorize posting, messaging, CRM enrollment, ordering, or ad spend. For an external action, use the exact destination, payload, audience, timing, and budget the user authorized; if one is missing, preserve the completed draft and ask only for that missing decision. Never bypass a tool gate through another route.
