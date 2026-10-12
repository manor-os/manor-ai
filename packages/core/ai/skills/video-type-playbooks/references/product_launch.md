# 产品 / App 发布片（Product launch）

适用：软件产品或 App 的发布宣传片，没有现成录屏时用 HTML 搭建的仿真 UI 演示功能。默认：16:9、45s、有配音。

## 结构与节拍
PAS + 三个功能，45s 参考分配（样片实测）：
1. 0–7.6s 钩子：一个痛点数字（例：未读计数 0 → 412 爬升，邮件行越堆越快溢出），然后从噪音中挑出关键的 3 条。
2. 7.6–14.2s 亮相：干净画面，产品 Logo 绘出、字标落定，一句价值主张在下方成形，静止停一拍。
3. 14.2–34.6s 三个功能，各约 6.6–6.9s：每个功能一个 HTML 仿真 UI 场景，光标移动、点击涟漪、卡片动画，演示"动作 → 价值"。
4. 34.6–39.4s 证明：一个具体结果数字（例：计数 412 → 0，时钟 7:58 → 8:03），静止停留。
5. 39.4–45s CTA 落版：Logo + CTA 按钮，光标按下按钮；虚构产品在底部注明"虚构产品 · 概念演示"；最后 0.6s 淡出（全片唯一的退场）。
配音放在每段开头，画面在台词后继续完成并停留；每段的画面动作在说到对应词时发生。

## 视觉规则
- 一个品牌色 + 一个仅用于"成功状态"的绿色；底色柔和（4–10% 品牌色的径向光晕），不用饱和的紫蓝"AI 渐变"。
- UI 卡片：白色圆角 16–24px，1px 浅品牌色描边，最多一层很淡的阴影；所有功能场景共用同一套 UI 组件（头像、行、胶囊、光标）。
- 字体：display 一个 + UI 正文一个，默认沙箱已安装的 Lato（display）+ Roboto（UI），中文用 Noto Sans CJK SC；有品牌字体时用用户提供的字体文件（放进 `assets/`）。不再加第三种。
- 动作：`power3.out` 长尾落定，快入场用 `expo.out`；不用 bounce / back / elastic；所有入场用 fromTo；停留时画面静止。
- 横屏内容放在上 83%（1080p y ≤ 900），底部留给字幕或平台控件。
- 不出现任何真实第三方品牌、Logo 或商标（Gmail、Outlook 等）。
- 不把旁白整句写到屏幕上；屏幕只放关键词与数字。

## 制作路线
1. 读取用户的产品资料、卖点与 CTA；`product-promotion-video` 提供产品身份与证明约束（instructions_only，不执行工具）。虚构产品在 CANVAS 与落版上都标明虚构。
2. 文稿：每个数字、功能名称来自用户材料；画面上的"结果数字"必须是用户能证实的，否则改成定性表述。
3. 配音：`generate_file(kind="audio")`（purpose 为 narration）→ `probe_media` 测实测时长 → `normalize_audio_loudness`（`target_lufs` −16，`target_duration_seconds` 取实测时长或该画面块计划容纳的时长；该参数会做时间伸缩，默认取实测时长，只有有意重新定时才用计划时长，且速度变化保持很小）→ `build_narration_timeline`（默认 `require_normalized_segments` 为 true，只接受带正值 `target_duration_seconds` 回执的归一化输出；分段语音何时用它见 video-production 的 references/production-and-edit.md 第 3 步）；按实测时长分配每段，并决定是延长台词还是让音乐承载停留。
4. 音乐：`search_free_music` → `import_free_music`（明快的流行电子类）→ `normalize_audio_loudness`（−24 LUFS）；点击、弹出音效用 `generate_file(kind="audio")`（purpose 为 sfx）并归一。stem 经 `video_edit(operation="push_assets")` 进项目、单位增益放置，人声段用音量自动化压低音乐。
5. UI 在 `video_edit`（HyperFrames）中用 HTML/CSS/SVG 搭建，每段一个 sub-composition，所有动画挂在 paused timeline 上，是 t 的纯函数，不用实时时钟、不用无种子随机。
6. 有真实产品时优先用用户提供的截图或录屏；网页产品需要自动录屏时改用 `web_product_walkthrough` 类型。
7. 审阅：`video_edit(operation="review", snapshot_times=[...])` 取每段首帧、点击帧、末帧；批准后只用返回的 `review_sha256` 渲染。

## 已知坑
- 配音比画面短：样片中 7 句台词总长 27.6s 而成片 45s，剩下的都是无声停留。要么把文稿写到覆盖约 70–85% 时长，要么设计好停留期间的画面动作与音乐起伏。
- 编造证明数字：样片编排时删掉了一个未经证实的"节省 4 分钟"说法；结果数字只用用户提供的。
- 仿真 UI 太假：字号过小、行高不一、光标瞬移。光标沿减速路径移动，落点略偏离中心再按下。
- 每段都堆满元素：观众抓不到"这个功能的价值"。
- 虚构产品没有标注，可能被误认为真实产品。

## 验收清单
- [ ] `probe_media`：1920×1080、30fps，时长与 CANVAS 一致。
- [ ] 成片首帧（`render_frame_samples` t=0）可读出钩子数字或主体，不是空白。
- [ ] 三个功能段各取一帧：能看到光标或点击反馈与对应 UI 状态变化。
- [ ] 同一组帧中所有数字都能在用户材料或 CANVAS 事实表中找到。
- [ ] 目视判断同一组帧：没有第三方品牌名称或 Logo。
- [ ] 最后一帧有 Logo 与 CTA；虚构产品可读到"虚构产品"说明。
- [ ] `analyze_audio`（成片 MP4）：超过 2s 的静音区间与 CANVAS 计划的停留一致，没有计划外的长静音。
- [ ] `transcribe_video` 文本与文稿一致；配音 stem −16 ±1 LUFS、音乐床 stem −24 ±1 LUFS；成片 true peak ≤ −1 dBTP。
