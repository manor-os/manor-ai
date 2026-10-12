# 录屏教程（Screen tutorial）

适用：把用户提供的软件录屏剪成清晰的教程：跟随放大、点击高亮、步骤卡、等待加速、敏感信息打码，配讲解。默认：16:9、60s、有配音。

## 结构与节拍
1. 0–4s 目标：一句话说明"学完能做到什么" + 最终效果的一帧预览。
2. 步骤段（3–6 步）：每步 = 步骤卡（编号 + 动词短语，0.8–1.2s）→ 录屏操作 → 结果停留 0.5–1s。
3. 等待段（加载、渲染、上传）：加速 ≥ 2x，画面角落显示"加速 ×N"标签；超过 10s 的等待直接剪掉并用转场说明。
4. 最后 4–6s：回顾全部步骤的清单卡，静止 ≥ 1.5s。
讲解台词与操作同步：说"点击保存"时光标正落在保存按钮上。

## 视觉规则
- 跟随放大：放大到点击区域（1.5–2.5 倍），以点击点为中心；放大前后用 0.4–0.6s 的 `power2.inOut` 移动，放大期间画面不抖。
- 点击涟漪：每次点击出现一个圆形涟漪（品牌色或高对比色，0.4s 扩散淡出），位置精确对准点击坐标。
- 步骤卡：左上或左下固定位置，编号 + 动词开头的短语（"1 新建项目"），全片同一样式；字体默认 Noto Sans CJK SC / Roboto，或用户提供的字体文件。
- 加速标签：固定在右上角，"加速 ×2"字样，只在加速期间显示。
- 敏感信息（邮箱、手机号、令牌、客户姓名、金额）用模糊块覆盖，模糊块跟随内容位置。
- 录屏文字在成片中的等效字号 ≥ 18px（1080p），不够就放大。
- 字幕带（1080p y 880–1060）不压住操作区域；必要时字幕上移或缩小录屏。

## 制作路线
1. `probe_media` 录屏（分辨率、帧率、时长、是否有音轨）；`render_frame_samples` 按间隔抽原录屏帧（每次 ≤ 24 帧，必要时分多次），找出每次点击的时间与屏幕坐标，记入 CANVAS（镜头 ID、时间、坐标、动作）。没有自动光标跟踪，点击坐标人工从抽帧读取。
2. 有讲解音轨时 `transcribe_video` 获取文本；需要新配音时 `generate_file(kind="audio")`（purpose 为 narration）→ `probe_media` 测实测时长 → `normalize_audio_loudness`（`target_lufs` −16，`target_duration_seconds` 取实测时长或该画面块计划容纳的时长；该参数会做时间伸缩，默认取实测时长，只有有意重新定时才用计划时长，且速度变化保持很小）→ `build_narration_timeline`（默认 `require_normalized_segments` 为 true，只接受带正值 `target_duration_seconds` 回执的归一化输出；分段语音何时用它见 video-production 的 references/production-and-edit.md 第 3 步）。
3. 剪辑与加速：在 `video_edit`（HyperFrames）中保留原录屏，以图层的入出点与播放速度表达裁切和加速；配音作为独立 stem 经 `video_edit(operation="push_assets")` 进项目、单位增益放置，不随加速变调。
4. 在 Video Edit 中按点击时间与坐标做跟随放大关键帧、点击涟漪、步骤卡、加速标签和打码块；全部是 paused timeline 上 t 的纯函数。
5. 字幕：`align_subtitles` 生成，`validate_subtitles` 校验。
6. 审阅：`video_edit(operation="review", snapshot_times=[...])` 取每次点击时刻与每个打码区域；批准后只用返回的 `review_sha256` 渲染。

## 已知坑
- 放大中心与点击点不一致：观众看不到被点的按钮。
- 加速后没有提示，观众以为软件很快；或加速段配音被加速变调（配音必须与加速段分开处理）。
- 打码块在画面滚动时没跟上，露出一两帧敏感信息。
- 光标快速移动时跟随放大来回晃，看得头晕；短距离移动不跟随。
- 录屏帧率低于 30fps，放大后卡顿明显。

## 验收清单
- [ ] `probe_media`：1920×1080、30fps，时长与 CANVAS 一致。
- [ ] 成片每次点击时刻各取一帧（`render_frame_samples`）：涟漪中心与被点控件重合，放大区域包含该控件。
- [ ] 加速段的帧都能看到"加速 ×N"标签，且 N ≥ 2。
- [ ] 对 CANVAS 列出的每个敏感区域，在其出现区间首、中、尾各取一帧：都被模糊覆盖。
- [ ] 步骤卡编号连续，文字与配音中的步骤一致。
- [ ] `transcribe_video`（成片）文本与文稿一致；抽 3 处操作取 ±2s 窗口调用 `transcribe_video(input_path, start_seconds, end_seconds)`，插值词时间与点击时间偏差 ≤ 0.5s（timestamps_available=false 时标为"未测量"）。
- [ ] 字幕 `validate_subtitles` 通过。
- [ ] `analyze_audio`：配音 stem −16 ±1 LUFS；成片 MP4 true peak ≤ −1 dBTP。
