# 手绘白板讲解（Whiteboard explainer）

适用：白板上一笔一笔画出来的讲解，边说边写边画，适合培训、知识科普和流程说明。默认：16:9、45s、有配音。

## 结构与节拍
画的节奏跟着说的节奏：每个图形开始绘制的时间 = 旁白说到它的时间。
1. 0–5s 标题：标题手写出现（首帧已有第一笔，不是空白板）。
2. 5–35s 主体 3–4 个要点：每个要点 = 一个手绘图形 + 一个关键词；画完一个要点后停 0.5–1s 再进入下一个。
3. 35–41s 关系：用箭头 / 框线把要点连起来，形成一张完整的图。
4. 41–45s 总结：整板拉远或圈出核心词，静止 ≥ 1.5s。
单一画面的板面最多容纳 4–5 个图形；写满就"擦除"或平移到新板面。

## 视觉规则
- 白色背景（纯白或略暖的 #FAFAF7），可加极淡纸纹。
- 马克笔配色：黑色为主线，蓝 / 红 / 绿各有一个固定用途（例：红 = 问题，绿 = 结果），全片一致。
- 逐笔绘出：每条路径用 stroke-dasharray / stroke-dashoffset 从全长画到 0，路径长度在构建时计算；按书写顺序（从左到右、从上到下）排列笔画。
- 文字按说话写出：字体轮廓不带笔顺，逐笔"写出"的关键词必须为每一笔手工编写中线路径作遮罩，按笔顺排列；没有手写笔画路径的普通文字改用从左到右的擦除揭示（wipe），不冒充手写。书写开始时间对齐该词语音，书写时长不超过该词所在短语的时长。
- 字体：手写体只用用户提供的字体文件（放进 `assets/`）；否则用沙箱已安装的 Noto Sans CJK SC / Lato。
- 手 / 马克笔光标可选：若使用，笔尖必须跟随当前笔画末端坐标，抬笔时离开画面或停在旁边。
- 线宽 4–6px（1080p），手写字高 ≥ 40px；字幕带（y 880–1060）留空。

## 制作路线
1. 文稿：用户材料为准；每个要点对应一个图形，写进 CANVAS 的镜头表。
2. 配音：`generate_file(kind="audio")`（purpose 为 narration）→ `probe_media` 测实测时长 → `normalize_audio_loudness`（`target_lufs` −16，`target_duration_seconds` 取实测时长或该画面块计划容纳的时长；该参数会做时间伸缩，默认取实测时长，只有有意重新定时才用计划时长，且速度变化保持很小）→ `build_narration_timeline`（默认 `require_normalized_segments` 为 true，只接受带正值 `target_duration_seconds` 回执的归一化输出；分段语音何时用它见 video-production 的 references/production-and-edit.md 第 3 步）。
3. 词级时间：`transcribe_video` 分段时间戳，段内按字数比例插值，作为每个关键词的书写开始时间（timestamps_available=false 时用 `build_narration_timeline` 的分段时间插值并标注"估算"）。
4. 在 `video_edit`（HyperFrames）里用内联 SVG 手绘风路径（可用略带抖动的手绘线条，抖动用固定种子生成），每个要点一个分组。
5. 所有绘制挂在一条 paused timeline 上，是 t 的纯函数；不用实时时钟、不用无种子随机。
6. 马克笔书写声可用 `generate_file(kind="audio")`（purpose 为 sfx）；音乐床用 `search_free_music` → `import_free_music`。各 stem 先 `normalize_audio_loudness`（配音 −16、音乐 −24、书写声 −30 LUFS 量级），再 `video_edit(operation="push_assets")`，单位增益放置。
7. 审阅：`video_edit(operation="review", snapshot_times=[...])` 取每个要点画完的时刻；批准后只用返回的 `review_sha256` 渲染。

## 已知坑
- 字体直接淡入而不是写出来，白板感消失；而用字体轮廓做"描边动画"笔顺是乱的。
- 画比说快太多：观众看完图再听旁白；或画比说慢：旁白已讲下一点图还没画完。
- 手光标与笔画脱节：笔尖不在线条末端，一眼假。
- 板面写满仍继续加图，最后一屏无法阅读。
- 手绘抖动每次渲染不同（未设种子），审阅版与成片不一致。

## 验收清单
- [ ] `probe_media`：1920×1080、30fps，时长 ≥ 最后一个词结束 + 1s。
- [ ] 成片 t=0 帧（`render_frame_samples`）不是空白板。
- [ ] 目视判断一个书写中途帧：笔画是部分绘出的（不是整体淡入）。
- [ ] 抽 3 个关键词：取 ±2s 窗口调用 `transcribe_video(input_path, start_seconds, end_seconds)`，插值词时间与书写开始时间偏差 ≤ 0.5s（timestamps_available=false 时标为"未测量"）。
- [ ] 目视判断：背景为白色或浅暖白，马克笔颜色用途与 CANVAS 一致。
- [ ] 任一要点完成帧中板面图形不超过 5 个。
- [ ] 最后 1.5s 两帧静止一致。
- [ ] `analyze_audio`：配音 stem −16 ±1 LUFS；成片 MP4 整合响度在 −20 到 −14 LUFS，true peak ≤ −1 dBTP。
