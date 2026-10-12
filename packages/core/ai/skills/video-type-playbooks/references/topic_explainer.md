# 图形科普讲解（Topic explainer）

适用：用配音 + 逐词字幕 + 图解动画把一个原理讲清楚（"GPS 怎么知道你在哪"这类问题），无真人出镜。默认：16:9、60s、有配音。

## 结构与节拍
配音驱动全部节奏：先锁定文稿和配音，再按实测语音时长切镜头。60s 约 6–8 段，每段 6–14s：
1. 0–7s 反直觉钩子：一句颠覆常识的话 + 一个主体图形（样片："手机从不向卫星发信号，它只是在听"）。
2. 系统登场：命名主角（例：约 31 颗在轨卫星），一个计数 + 一张结构图。
3. 核心机制：一个公式逐项拼出（例：传播时间 × 光速 = 距离）。
4. 几何推理：一步步加条件（一个圆 → 两圆交两点 → 三圆交一点 → 推广到三维球面）。
5. 难点 / 例外：一个让人意外的修正（例：第四颗卫星解出接收机钟差）。
6. 彩蛋：再深一层的事实（例：相对论修正约 38 µs/天）。
7. 落点：一句话回到开头的问题，画面 callback 第 2 段的结构图，静止收尾。
每个元素在被说到的那一刻出现，不提前堆满。

## 视觉规则
- 技术图解风：深色底 + 极淡网格，线条而非色块；两个强调色各有固定含义（样片：青色 = 信号 / 几何，琥珀色 = 你 / 时间 / 误差），不出现第三个强调色。
- 同一套"演员"贯穿全片：地球、卫星、手机点、距离圈、时钟芯片在每段长得一样。
- 图形用几何计算而不是摆位：圆的交点要真的相交（由圆心与半径解出交点坐标），轨道角度是 t 的函数。
- 字幕带：1080p 下 y 880–1060 只放字幕，关键内容放在 y ≤ 880；承载信息的文字 ≥ 27px，标签 ≥ 18px 大写加字距。
- 字体：默认 Noto Sans CJK SC（正文）+ Liberation Mono（读数），或用户提供的字体文件（放进 `assets/`）。
- 字幕：逐词同步，英文 2–4 词一组、中文 4–8 字一组，当前词高亮（强调色或加粗），组按语义切分。
- 不把旁白句子再写成屏幕大字；屏幕字只放关键词、数字和公式。
- 无照片、无素材库画面、无"AI 紫蓝渐变"、无投影阴影。

## 制作路线
1. 文稿：每个数字都核实并在 CANVAS.md 记录来源链接；用户给的事实优先。
2. 配音：`generate_file(kind="audio")`（purpose 为 narration，指定 language 与 voice），一段一个文件 → `probe_media` 测实测时长 → `normalize_audio_loudness`（`target_lufs` −16，`target_duration_seconds` 取实测时长或该画面块计划容纳的时长；该参数会做时间伸缩，默认取实测时长，只有有意重新定时才用计划时长，且速度变化保持很小）→ `build_narration_timeline`（默认 `require_normalized_segments` 为 true，只接受带正值 `target_duration_seconds` 回执的归一化输出；分段语音何时用它见 video-production 的 references/production-and-edit.md 第 3 步） 得到分段时间与字幕 cue。
3. 词级时间：`transcribe_video` 只给分段时间戳；段内逐词时间按字数比例插值。返回 timestamps_available=false 时，用 `build_narration_timeline` 的分段时间做插值并标注"估算"。
4. 音乐床：`search_free_music` → `import_free_music`（或 `generate_file(kind="audio")` purpose 为 music）→ `normalize_audio_loudness`（−24 LUFS）。两条 stem 经 `video_edit(operation="push_assets")` 进项目，单位增益放置，只在人声段用音量自动化再压低音乐。
5. 图解在 `video_edit`（HyperFrames）中用 SVG 构建，一段一个 sub-composition，所有动画在 paused timeline 上，是 t 的纯函数，不用实时时钟、不用无种子随机。
6. 字幕文件可用 `align_subtitles` 生成，`validate_subtitles` 检查时序与重叠；画面内逐词字幕在 Video Edit 中实现。
7. 审阅：`video_edit(operation="review", snapshot_times=[...])` 取每段首尾和每个公式 / 交点落定时刻；批准后只用返回的 `review_sha256` 渲染。

## 已知坑
- 字幕组切得碎：一组只有 1 个词或闪现不足 0.3s，读起来一跳一跳；组按短语切，最短显示 0.5s。
- 字幕组跨句：一组里前半句和后半句混在一起，句末停顿时字幕不消失。
- 交点"看起来差不多"：手摆坐标的圆不真正交于一点，懂行的观众一眼看穿。
- 数字未核实或单位错误（µs 写成 ms）。
- 音乐盖住旁白：配音与音乐床响度差不足。

## 验收清单
- [ ] `probe_media`：1920×1080、30fps、时长 ≥ 最后一个词结束时间 + 1s。
- [ ] `transcribe_video`（成片）文本与锁定文稿一致，所有数字与 CANVAS 事实来源一致。
- [ ] 目视判断成片各段中点帧（`render_frame_samples`）：字幕只在 y 880–1060 带内，关键图形与数字都在 y ≤ 880。
- [ ] 抽 3 个字幕时刻：对每处取 ±2s 窗口调用 `transcribe_video(input_path, start_seconds, end_seconds)`，由返回分段按字数插值得到的词时间与画面高亮词时间偏差 ≤ 0.5s（timestamps_available=false 时标为"未测量"）。
- [ ] 同一组帧中没有只含 1 个词的字幕组（短语末尾单词除外）。
- [ ] 目视判断几何落定帧：三个圆交于同一点，放大后无可见偏差。
- [ ] 目视判断同一组帧：强调色只有两种，含义与 CANVAS 一致。
- [ ] `analyze_audio`：配音 stem −16 ±1 LUFS、音乐床 stem −24 ±1 LUFS（单位增益放置）；成片 MP4 true peak ≤ −1 dBTP。
