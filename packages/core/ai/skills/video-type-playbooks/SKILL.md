---
name: video-type-playbooks
description: Per-type production playbooks for a Video Studio catalogue type that a user or Workspace action explicitly selected. Returns structure and beats, visual rules, the Manor production route, known pitfalls and an acceptance checklist to the calling video-production agent. Do not use to guess a type from keywords.
---

# Video Type Playbooks

**何时使用：** 仅当用户或 Workspace 动作**明确选定**了一个目录类型 id（运行输入 video_type）时使用。用 `invoke_skill(skill_id="video-type-playbooks", bundle_file="references/<type_id>.md")` 加载该类型的 playbook（next_char_offset 不为 null 时加 `char_offset` 续读；`read_file` 打不开本技能的文件），不要根据关键词自行推断类型，也不要同时套用多个类型。

**优先级：** playbook 只提供结构与节拍、视觉规则、制作路线、已知坑和验收清单。用户的 brief、文稿、素材和事实永远优先：用户给了时长、画幅、配音选择或镜头顺序，就按用户的来。绝不编造卖点、数据、规格、价格、评价或奖项；每个数字都来自用户材料或经过核实的来源。

**返回什么：** 本技能是 instructions_only，不调用任何工具。它把指导返回给调用方 `video-production` agent，由后者写入 `CANVAS.md`，并按 CANVAS 的镜头 ID（shot ID）落实。playbook 中的 Manor 工具由 `video-production`（以及它调用的 `video-edit`）执行；`product-promotion-video`、`restaurant-promotion-video` 同样是 instructions_only 的约束技能，只提供身份与连续性约束，不执行工具。

**共同规则（各 playbook 默认遵守）：**
- 审阅与渲染：Video Edit 成片前用 `video_edit(operation="review", snapshot_times=[...])` 取审阅帧，用户批准后只用返回的 `review_sha256` 调用 `video_edit(operation="render", review_sha256=...)`。`render_frame_samples` 只用于已渲染、已存入 Knowledge 的 MP4（每次 ≤ 24 帧）。
- 响度：`normalize_audio_loudness` 只处理音频文件。每条 stem 先归一到各自目标，再 `video_edit(operation="push_assets")` 并以单位增益放置；原生时间线路线用 `compose_video_timeline` 的 `loudness_normalization`。成片 MP4 只用 `analyze_audio` 复核。
- 字体：沙箱已安装 Noto Sans / Noto Serif、Noto Sans CJK SC / Noto Serif CJK SC、Lato、Roboto、Liberation Sans / Serif / Mono、Caladea、Carlito。其他字体必须是用户提供的字体文件，放进项目 `assets/`；禁止远程字体。
- 词级时间：`transcribe_video` 只给分段时间戳（不支持时返回 timestamps_available=false），词级时间按段内字数插值，验收容差按此设定。

**能力边界：** playbook 只写今天能做到的方法。没有自动节拍检测、自动抠图、人脸跟踪、HDR 转换或素材库搜索工具时，按文中的近似方法（按 BPM 计算节拍网格、生成时用纯色背景再手绘遮罩、人工设定重构图关键帧、请用户提供 SDR 素材、用户提供素材或生成并标注）执行。

## 索引

| type id | reference | 一句话 |
| --- | --- | --- |
| `motion_showreel` | `references/motion_showreel.md` | 节拍驱动的动效作品集，落版收尾 |
| `kinetic_quote` | `references/kinetic_quote.md` | 一句金句的动态排版，随语音或节拍出字 |
| `data_story` | `references/data_story.md` | 用户数据驱动的计数、柱状、折线动画 |
| `logo_sting` | `references/logo_sting.md` | 3–8 秒 Logo 演绎，矢量重绘，单次音效 |
| `topic_explainer` | `references/topic_explainer.md` | 配音 + 逐词字幕 + 几何图解科普 |
| `story_history` | `references/story_history.md` | 时间线叙事，画风随年代演变 |
| `flat_illustrated_explainer` | `references/flat_illustrated_explainer.md` | SVG 扁平角色与木偶式动作讲解 |
| `whiteboard` | `references/whiteboard.md` | 白板逐笔绘出，边讲边写 |
| `product_launch` | `references/product_launch.md` | 钩子 → 亮相 → 三个 UI 功能 → 证明 → CTA |
| `product_3d_showcase` | `references/product_3d_showcase.md` | 3D 产品主视觉 + 场景镜头 + 品牌收尾 |
| `viral_short` | `references/viral_short.md` | 12–18 秒首帧钩子、一个爽点、手机实拍感 |
| `venue_promo` | `references/venue_promo.md` | 餐厅门店实拍节拍剪辑，书法标题 |
| `talking_head_package` | `references/talking_head_package.md` | 口播逐词字幕、手绘批注、平台重构图 |
| `footage_montage` | `references/footage_montage.md` | 多段素材卡点混剪，统一调色 |
| `screen_tutorial` | `references/screen_tutorial.md` | 录屏跟随放大、点击高亮、步骤卡 |
| `before_after` | `references/before_after.md` | 同步分屏对比，只放实测事实 |
| `picture_book` | `references/picture_book.md` | 统一画风插画 + 视差 + 手写字幕 |
| `ink_wash` | `references/ink_wash.md` | 水墨逐笔、留白、朱印、书法标题 |
| `paper_collage` | `references/paper_collage.md` | 纸张质感分层拼贴，12fps 定格感 |
| `comic_panels` | `references/comic_panels.md` | 分格漫画、镜头逐格推进、可编辑气泡 |
| `isometric_cutaway` | `references/isometric_cutaway.md` | SVG 等距剖面与流程箭头 |
| `cinematic_3d` | `references/cinematic_3d.md` | 按 seek 时间驱动的程序化 3D 电影镜头 |
| `music_video` | `references/music_video.md` | 音乐驱动节奏的 MV，段落与包络 |
| `lyric_video` | `references/lyric_video.md` | 逐词歌词随演唱出现 |
| `web_product_walkthrough` | `references/web_product_walkthrough.md` | 交给 Product Video Studio 的网址录屏讲解 |
