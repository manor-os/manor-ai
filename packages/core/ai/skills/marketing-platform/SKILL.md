---
name: marketing-platform
description: Shared destination guidance for marketing-campaign and video-production. Adapt requested creative, video delivery, CTA, and measurement to the selected platform; the calling skill owns planning and production.
---

# Marketing Platform

Work only on the **destination platform**. Preserve an explicit platform from the user. If none is named, recommend the smallest useful destination based on where the audience already expresses the relevant intent, geography, available accounts/assets, conversion path, and effort. Do not infer a platform solely from “video”, “post”, or “ad”. Do not invent a current platform limit or a connected account.

Accept calls from either `marketing-campaign` for a campaign's platform axis or `video-production` for a video with a requested destination. For a video call, preserve the supplied script, runtime, language, and creative decisions; tailor format, composition, captions, and delivery to the platform. Suggest changes to an open brief only within the user's goal. A destination by itself does not require a broader campaign plan. If the caller supplies an existing overlay for the same destination, reuse its verified decisions and update only changed or unresolved constraints.

Read only the relevant bundled reference with `read_file`; read two when the user explicitly requests a cross-platform adaptation:
Only the reference paths in this table belong to this skill's bundle. Use any industry or method facts passed in the input without trying to read their other bundles here.

| Destination | Reference |
| --- | --- |
| TikTok/Douyin, Reels, Shorts, Kuaishou, YouTube/Bilibili video | `references/video-platforms.md` |
| Xiaohongshu/RedNote, Pinterest, LinkedIn, X, Reddit, WeChat/social communities | `references/social-platforms.md` |
| Google/Baidu search, Google Business Profile, Apple Maps, Meituan/Dianping, local directories | `references/search-and-maps.md` |
| Amazon, Tmall/JD, Shopify, TikTok Shop, owned site/app, email/SMS destinations | `references/commerce-and-owned.md` |

If the destination is outside these families, identify its actual audience behavior, native units, allowed CTA path, account availability, current constraints, and observable conversion before suggesting creative. Method choices such as organic, paid, creator, search, and lifecycle belong to `marketing-method`; product or restaurant proof belongs to `marketing-industry`.

Return one concise **platform overlay per selected destination**:

1. Audience intent and why this platform fits the requested deliverable.
2. Native content or placement type, discovery surface, and one viable CTA path.
3. Concrete creative structure: hook/title, evidence sequence, asset needs, and text/visual treatment.
4. Current-rule checks required before export or publication, with verification state (`verified` or `not checked`).
5. Primary outcome metric, data source, and a likely measurement blind spot.

For a video, make the overlay usable in the shot canvas: selected placement, requested aspect ratio and runtime, framing/crop, caption and interface-safe regions, muted-viewing needs, sound/music constraints, opening and ending guidance, and export/publication checks. Mark unchecked current rules clearly. Preserve an approved script's order and dialogue; add a CTA only when the brief calls for one.

Return the overlay to the **calling skill**: `marketing-campaign` combines it with industry and method, while `video-production` applies it to its canvas, generation, edit, and delivery. Do not invoke either caller from this specialist, publish, buy ads, or create finished media. If a user asks for several platforms, preserve the same underlying facts and any approved script while adapting composition, format, text, CTA when requested, and measurement; rework the opening or order only when the creative brief permits it.
