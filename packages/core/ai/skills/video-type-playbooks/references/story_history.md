# 品牌 / 历史故事（Story & history）

适用：按时间线讲一个品牌、行业或一段历史，画风随年代演变，一个持续的年份计数器把各段串成一条线。默认：16:9、60s、有配音。

## 结构与节拍
60s 约 7–9 个里程碑段，每段 5–9s，相邻段重叠约 0.7s 做模糊交叉溶解：
1. 0–6s 开篇：标题 + 一个时代起点画面，旁白点出跨度（例："从创立到今天的 N 年……"）。
2. 中段：每个里程碑一段 = 年份计数器滚到该年 + 该时代画风的主画面 + 1–2 个事实芯片（时间、地点、数字）。
3. 一个高潮段：最戏剧化的里程碑（例：音障突破），用一次冲击（闪光 ≤ 3 帧或表盘越过刻度）。
4. 最后 6s 收尾：拉远到全景，标题 + 起止年份（例："起始年–今年"），淡出。
节奏随旁白：里程碑画面在旁白说到年份的那一刻翻页，不提前。

## 视觉规则
- 画风随年代演变：例如 羊皮纸墨线草图 → 铜版蚀刻 → 颗粒黑白胶片 → 20 世纪中叶蓝图 → 航天时代照片印刷 → 现代 HUD。每段只用该时代的配色与字体。
- 字体：默认 Noto Serif CJK SC / Caladea（早期）、Noto Sans CJK SC / Roboto（现代）、Liberation Mono（读数），或用户提供的字体文件（放进 `assets/`）。
- 持续元素：年份计数器（是 t 的纯函数）和时间线色带（轨道用 SVG path 绘制，标记位置 = f(年份)）在每个转场都连续，不跳变。
- 主转场：模糊交叉溶解；进入"胶片时代"可用烧片，进入"照片时代"可用过曝显影。全片转场种类 ≤ 3。
- 年代不确定的事实用"约 / c."标注。
- 字幕带（1080p y 880–1060）只放字幕；事实芯片和年份计数器放在字幕带之外。
- 胶片颗粒、划痕、闪烁一律带种子，确定可复现。

## 制作路线
1. 事实表：每个里程碑的日期、人物、地点、数字逐条核实，来源写进 CANVAS.md；不确定的写"约"。
2. 配音：`generate_file(kind="audio")`（purpose 为 narration），一段一个文件 → `probe_media` 测实测时长 → `normalize_audio_loudness`（`target_lufs` −16，`target_duration_seconds` 取实测时长或该画面块计划容纳的时长；该参数会做时间伸缩，默认取实测时长，只有有意重新定时才用计划时长，且速度变化保持很小）→ `build_narration_timeline`（默认 `require_normalized_segments` 为 true，只接受带正值 `target_duration_seconds` 回执的归一化输出；分段语音何时用它见 video-production 的 references/production-and-edit.md 第 3 步） 生成分段时间与字幕 cue。
3. 字幕来自实测语音：`transcribe_video` 分段时间戳 + 段内按字数插值（timestamps_available=false 时退回 `build_narration_timeline` 的分段时间）；`align_subtitles` 生成字幕文件，`validate_subtitles` 校验。
4. 音乐床：`search_free_music` → `import_free_music`，或 `generate_file(kind="audio")`（purpose 为 music，管弦 / 史诗类）→ `normalize_audio_loudness`（−24 LUFS）；stem 经 `video_edit(operation="push_assets")` 进项目、单位增益放置，人声段用音量自动化压低。
5. 画面：插画用 `video_edit`（HyperFrames）以 SVG / Canvas 绘制；需要写实画面时用 `generate_image`（同一时代用同一风格后缀）并在 CANVAS 标注为生成图。不使用未授权的历史照片。
6. 年份计数器与色带写成全片级持续层，各时代场景在其下切换；全部动画在 paused timeline 上，是 t 的纯函数。
7. 审阅：`video_edit(operation="review", snapshot_times=[...])` 取每段年份落定时刻；批准后只用返回的 `review_sha256` 渲染。

## 已知坑
- 音乐盖住旁白：管弦乐床能量大，必须分别测配音与音乐床 stem 的响度，并在人声处压低。
- 日期错一年、人物张冠李戴：每个日期都要有来源。
- 计数器在转场处跳回或停顿：计数器必须在全片级层上连续。
- 画风"演变"做成了随机换风格：每次换风格要与年代对应，相邻两段只改变一到两个维度（色调、线条、质感）。
- 字幕时间用估算而不是实测，越到后面偏差越大。

## 验收清单
- [ ] `probe_media`：1920×1080、30fps，时长 ≥ 最后一个词结束 + 1s。
- [ ] 成片每个里程碑段的年份落定帧（`render_frame_samples`）：计数器读数与 CANVAS 事实表一致。
- [ ] `transcribe_video`（成片）文本中的所有日期、数字与事实表一致。
- [ ] 目视判断相邻两段的转场帧：年份计数器与时间线色带都可见且连续。
- [ ] 目视判断各段中点帧：能看出至少 3 种与年代对应的画风。
- [ ] 字幕在 y 880–1060 带内；抽 3 处取 ±2s 窗口调用 `transcribe_video(input_path, start_seconds, end_seconds)`，插值词时间与字幕出现时间偏差 ≤ 0.5s（timestamps_available=false 时标为"未测量"）。
- [ ] `analyze_audio`：配音 stem −16 ±1 LUFS、音乐床 stem −24 ±1 LUFS（单位增益放置）；成片 MP4 true peak ≤ −1 dBTP。
- [ ] 最后一帧是标题 + 起止年份。
