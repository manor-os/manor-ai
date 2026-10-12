# 扁平插画讲解（Flat illustrated explainer）

适用：用扁平矢量角色和场景讲一个流程或原理（开户流程、产品原理、内部制度），配音讲解。默认：16:9、45s、有配音。

## 结构与节拍
45s 约 5–6 段，每段 6–9s，镜头随配音切换：
1. 0–6s 角色 + 问题：主角出场，遇到一个具体问题（一个动作表现，不靠文字）。
2. 6–30s 流程 3–4 步：每步一个场景，角色做一个动作推进流程，步骤编号与关键词标签随语音出现。
3. 30–39s 结果：角色状态变化（表情、姿态），问题解决。
4. 39–45s 总结 / 行动号召：一句话总结 + 用户提供的入口信息，静止保持 ≥ 1.5s。

## 视觉规则
- 全部是 SVG 矢量：角色、道具、场景都用 SVG 绘制，不用 AI 位图，除非用户明确要求。
- 有限调色板：≤ 5 个主色 + 2 个中性色，在 CANVAS 写成 token；同一物体全片同色。
- 线宽一致：1080p 下统一 3px（或全片无描边）；缩放时用 `vector-effect: non-scaling-stroke` 保持线宽不变。
- 木偶式动作：肢体是独立图层，`transform-origin` 设在关节（肩、肘、髋、膝），只做旋转；走路是腿的交替旋转 + 身体轻微上下，循环用 t 的函数而不是无限 repeat。
- 角色设计一次定稿（正面 / 侧面两个视图），后续场景复用同一组 SVG。
- 字体：默认 Noto Sans CJK SC / Lato，或用户提供的字体文件（放进 `assets/`）。
- 字幕带（1080p y 880–1060）不放角色关键动作；步骤标签 ≥ 27px。
- 动作语法统一：`power2.out` 入场，物体落地可有一次轻微回弹，同类动作同一缓动。

## 制作路线
1. 文稿与流程步骤按用户材料编写；流程顺序、名词、规则不得自行增减。
2. 配音：`generate_file(kind="audio")`（purpose 为 narration）→ `probe_media` 测实测时长 → `normalize_audio_loudness`（`target_lufs` −16，`target_duration_seconds` 取实测时长或该画面块计划容纳的时长；该参数会做时间伸缩，默认取实测时长，只有有意重新定时才用计划时长，且速度变化保持很小）→ `build_narration_timeline`（默认 `require_normalized_segments` 为 true，只接受带正值 `target_duration_seconds` 回执的归一化输出；分段语音何时用它见 video-production 的 references/production-and-edit.md 第 3 步） 得到每段时长与字幕 cue。
3. 在 `video_edit`（HyperFrames）中先做"角色表"sub-composition（所有部件与关节点），再按段落搭场景；每段一个 sub-composition。
4. 所有动画挂在一条 paused timeline 上，是 t 的纯函数；不用实时时钟、不用无种子随机、不用无限 repeat。
5. 音乐床：`search_free_music` → `import_free_music` → `normalize_audio_loudness`（−24 LUFS）；点击、弹出等音效用 `generate_file(kind="audio")`（purpose 为 sfx）并归一。所有 stem 经 `video_edit(operation="push_assets")` 进项目、单位增益放置，人声段用音量自动化压低音乐。
6. 字幕：`align_subtitles` 生成，`validate_subtitles` 校验。
7. 审阅：`video_edit(operation="review", snapshot_times=[...])` 取每段首尾；批准后只用返回的 `review_sha256` 渲染。

## 已知坑
- 关节点错位：transform-origin 没设在关节，旋转时手臂脱离身体。
- 缩放场景导致线宽忽粗忽细，画风显得不统一。
- 混入 AI 位图道具：与矢量风格冲突，且无法改色。
- 角色每段重画一次，脸型、衣服颜色前后不一。
- 画面信息过载：一屏超过 3 个标签，观众跟不上配音。

## 验收清单
- [ ] `probe_media`：1920×1080、30fps，时长 ≥ 最后一个词结束 + 1s。
- [ ] 目视判断成片各段中点帧（`render_frame_samples`）：角色外形、配色与角色表一致。
- [ ] 目视判断同一组帧：颜色不超出 CANVAS 定义的调色板。
- [ ] 目视判断一个走路 / 挥手动作中途帧：四肢与躯干在关节处相连，无断开。
- [ ] 同一组帧中没有位图插画（用户要求的除外）。
- [ ] `transcribe_video`（成片）文本与文稿一致，步骤名称与画面标签一致。
- [ ] 字幕 `validate_subtitles` 通过。
- [ ] `analyze_audio`：配音 stem −16 ±1 LUFS、音乐床 stem −24 ±1 LUFS；成片 MP4 true peak ≤ −1 dBTP。
