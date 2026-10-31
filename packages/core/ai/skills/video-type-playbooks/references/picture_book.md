# 绘本 / 明信片故事（Picture-book story）

适用：第一人称或旁白讲述的小故事，用统一画风的 AI 插画 + 分层视差 + 手写字幕 + 配音完成，例如"来自 2080 年火星的明信片"。默认：16:9、50s、有配音。

## 结构与节拍
50s 约 7 段，相邻段重叠约 0.6s：
1. 0–5s 开篇物件：明信片背面，邮票落下、邮戳盖下（配一声闷响），标题与称呼随配音写出来；镜头推进邮票，邮票画面成为第一个场景（zoom-through 匹配剪辑）。
2. 5–44s 4–5 个场景，每个 6–10s：一张背景插画 + 0–1 个前景角色，慢速 Ken Burns（推或拉）+ 视差 + 少量环境粒子（尘埃、光斑）；每段 1–2 句手写字幕。
3. 44–50s 收尾：从邮票拉回整张明信片，签名写出，再盖一个印章，淡出。可选：片尾列出制作方式（代码 / AI 配音 / AI 插画 / 音乐来源）。

## 视觉规则
- 每个图像 prompt 末尾追加同一段风格后缀（画材、色板、光线 + "无文字、无字母、无 Logo、无水印、无边框"），在 CANVAS 写明原文。
- 前景角色单独生成在纯白或纯色背景上，用于视差分层；背景插画不含人物。
- 视差：背景移动慢、前景快（速度比约 1 : 1.5–2），前景随推镜略放大；层间可加一层雾或尘。
- 手写字幕：需要用户提供的手写体字体文件（放进 `assets/`）。字体轮廓不带笔顺，"逐笔写出"要为每一笔手工编写中线遮罩路径；工作量大时只对标题和签名逐笔写出，正文字幕用从左到右的擦除揭示。没有手写字体时用 Noto Serif CJK SC。每张卡 ≤ 2 行，放在画面下三分之一的纸卡上，与配音同步。
- 邮票 / 邮戳母题可选：邮票图案就是下一场景的画面缩略。
- 暖色、柔和、低对比；全片光线方向一致。
- 字幕与关键元素不贴边：1080p 四边留 ≥ 80px。

## 制作路线
1. 故事文稿按用户 brief 写，人物、地点、情节由用户决定；文稿分 6–8 句配音行，每行对应一个镜头 ID。
2. 插画：`generate_image`，每个场景一张背景 + 需要的前景角色；全部带同一风格后缀，设生成次数上限（例：≤ 10 张），在 CANVAS 记录每张的 prompt 与用途。
3. 前景分层：没有自动抠图工具；前景在纯白背景上生成，在 Video Edit 中用 CSS mask / 手绘 SVG clip-path 或亮度遮罩取出主体，边缘做 1–2px 羽化。
4. 配音：`generate_file(kind="audio")`（purpose 为 narration，语速略慢）→ `probe_media` 测实测时长 → `normalize_audio_loudness`（`target_lufs` −16，`target_duration_seconds` 取实测时长或该画面块计划容纳的时长；该参数会做时间伸缩，默认取实测时长，只有有意重新定时才用计划时长，且速度变化保持很小）→ `build_narration_timeline`（默认 `require_normalized_segments` 为 true，只接受带正值 `target_duration_seconds` 回执的归一化输出；分段语音何时用它见 video-production 的 references/production-and-edit.md 第 3 步）；词级时间用 `transcribe_video` 分段时间戳 + 段内按字数插值（timestamps_available=false 时用 `build_narration_timeline` 的分段时间）。
5. 音乐：`search_free_music` → `import_free_music`（安静的氛围类）→ `normalize_audio_loudness`（−24 LUFS）；邮戳闷响用 `generate_file(kind="audio")`（purpose 为 sfx）并归一。所有 stem 经 `video_edit(operation="push_assets")` 进项目、单位增益放置，人声段用音量自动化压低音乐。
6. 在 `video_edit`（HyperFrames）中合成 Ken Burns、视差、粒子、手写字幕；全部是 paused timeline 上 t 的纯函数，粒子用固定种子。
7. 审阅：`video_edit(operation="review", snapshot_times=[...])` 取每段中点；批准后只用返回的 `review_sha256` 渲染。

## 已知坑
- 生成图自带画框或米色边框（"painted border"）：即使 prompt 写了"无边框"也会出现；进剪辑前裁掉边框再按 16:9 中心裁切。
- 画风漂移：某张图没带风格后缀或换了模型，色板明显不同。
- 生成图里出现文字或伪文字：prompt 写"无文字"，并逐张检查。
- 前景遮罩边缘有白边：羽化不足或遮罩过大。
- 用字体轮廓做描边动画冒充手写：笔顺错乱。
- 字幕时间估算偏差累积：每段以该段配音的实测起点重新对齐。
- 同一张插画放两层（邮票缩略 + 场景背景、模糊底图 + 前景）：同一文件里两个 `<img>` 的 src、data-start、data-duration 全相同会让严格审阅报 duplicate_media_discovery_risk 而失败；只保留一个 `<img>`，另一层用 `<div>` 的 CSS `background-image`。

## 验收清单
- [ ] `probe_media`：1920×1080、30fps，时长 ≥ 最后一个词结束 + 1s。
- [ ] 目视判断成片每段中点帧（`render_frame_samples`）：画风（色板、笔触）一致，没有画框边、没有文字或水印。
- [ ] 目视判断有前景角色的段落帧：角色边缘没有白边或残留背景。
- [ ] 同一段相隔 2s 的两帧：背景与前景的位移量不同（视差存在）。
- [ ] 抽 3 处字幕：取 ±2s 窗口调用 `transcribe_video(input_path, start_seconds, end_seconds)`，字幕内容与该窗口转写一致，插值时间偏差 ≤ 0.5s（timestamps_available=false 时标为"未测量"）。
- [ ] `transcribe_video`（成片）文本与文稿一致。
- [ ] `analyze_audio`：配音 stem −16 ±1 LUFS、音乐 stem −24 ±1 LUFS（单位增益放置）；成片 MP4 true peak ≤ −1 dBTP。
- [ ] CANVAS 记录了每张生成图的 prompt，且标注为 AI 生成。
