# 动效作品集 / 品牌动效（Motion showreel）

适用：无旁白、由音乐驱动的动效展示，用一连串视觉点子证明"动效能力"或品牌气质，最后落在一个干净的落版上。默认：16:9、40s、无配音。

## 结构与节拍
时间一律按拍写（B0、B8…），不按秒拍脑袋。先确定 BPM，节拍时长 = 60 / BPM（例：128 BPM → 0.46875s/拍，8 拍 = 3.75s）。每 2–4 拍出现一个新点子，重拍（downbeat）上做硬切或冲击。
1. B0–B8 开场：大字 kinetic type 在第一个重拍砸入，随后 2–3 次反色硬切（ink ↔ paper ↔ accent）。
2. B8–B16 线条：SVG 路径绘制（缓动曲线、Lissajous），结尾收成一个点，作为匹配剪辑的锚点。
3. B16–B24 形状：点 → 圆 → 方 → 三角 → 星，每 2 拍变一次（clip-path polygon morph），形内遮罩滚动文字；最后放大成 iris 转场。
4. B24–B32 网格 / 粒子：网格从中心错峰出现，正弦波纹扫过，拼出一个词后炸成粒子。
5. B32–B40 数字：一个 count-up（如帧数 0 → 1,230），在第 4 拍落定；进度条匹配剪辑进下一场。
6. B40–B48 文字墙：marquee 行倾斜、整体反色、逐行飞出，中间一行 zoom-through。
7. B48–B56 Break：伪 3D（CSS 3D 隧道、translateZ 文字层、景深模糊），用升调收进白场。
8. B56–B72 Drop：一拍一个动词，每个词"做它说的事"（SNAP / SLAM / WHIP / STRETCH / MASK / DRAW），接节拍均衡器。
9. 最后 8 拍：落版 lockup（主标题 + 一行小字），冲击后静止保持 ≥ 1.5s，再淡出。

## 视觉规则
- 一个大胆的调色板：近黑 + 一个电光强调色 + 米白（样片：#0d0c0b / #ff4b1f / #f2eee6）。强调色只有一个，反色在三者之间切换。
- 一个 display 字体 + 一个 mono 字体：默认用沙箱已安装的 Roboto Black（display）+ Liberation Mono，或用户提供的字体文件（放进 `assets/`）。mono 用作"代码注释"式 HUD（时间码、拍号、读数）。
- 展示范围要全：kinetic type（拆字 / 错峰 / 缩放）、shape morph 与 mask、SVG line draw、网格 / 粒子、伪 3D、count-up、反色、smash / whip / match cut。
- Whip 转场：单条方向模糊，模糊峰值 16–24px 且正好落在剪辑点，`power3.inOut`。
- 颗粒层（grain）只做有限次迭代；不使用无限 repeat。
- 落版居中，四周留白 ≥ 15% 画面宽。

## 制作路线
1. 音乐：用户提供优先；否则 `generate_file(kind="audio")`（purpose 为 music，在 prompt 里写明 BPM、调性、需要的 break 与 drop 位置），或 `search_free_music` → `import_free_music` 取得可商用曲目。记录 BPM 来源；没有可靠 BPM 时，取曲目标称 BPM 并在第一个重拍处对齐偏移，按 BPM 计算节拍网格。
2. 在 CANVAS.md 写节拍表：每个镜头 ID 对应 Bx–By、点子、使用的动效规则。
3. 音频 stem（音乐、冲击、whoosh、打字声；音效用 `generate_file(kind="audio")`，purpose 为 sfx）先用 `normalize_audio_loudness` 归一（例：音乐 −16 LUFS，音效按需），再 `video_edit(operation="push_assets")`，在项目里以单位增益放置并对齐到拍。
4. 用 `video_edit`（Video Edit，HyperFrames 沙箱）搭建：每个场景一个 sub-composition，挂在一条 paused GSAP timeline 上；所有状态是时间 t 的纯函数，不用 Date / performance / rAF 时钟，随机数一律带种子。
5. 审阅：`video_edit(operation="review", snapshot_times=[...])`，时刻取 t=0、每个场景首帧、3 个重拍时刻、落版段；用户批准后只用返回的 `review_sha256` 渲染。
6. 成片 MP4 用 `probe_media`、`render_frame_samples`、`analyze_audio` 验收。

## 已知坑
- 第一帧不能是模糊或白闪：平台用首帧做封面。开场冲击放在 B0 之后几帧，t=0 是一张清晰的构图。
- 单个巨型 `index.html` 难以审阅和修改：每个场景拆成 sub-composition；跨场景的匹配剪辑只共享锚点坐标。
- 节拍偏移：BPM 对了但第一拍偏 1–2 帧，后面所有硬切都会"拖拍"。先校准第一个重拍时间。
- 每个点子都在动 = 没有重点；每场至少一次短暂停顿让眼睛落地。
- 白场 / 闪光超过 3 帧会洗掉画面，也不利于光敏观众。

## 验收清单
- [ ] `probe_media`：1920×1080、30fps、时长与 CANVAS 一致（±0.1s）。
- [ ] 目视判断成片 t=0 帧（`render_frame_samples`）：清晰构图，不是模糊、白闪或黑场。
- [ ] 按 BPM 网格抽取 3 个重拍时刻的帧：画面在这些帧上有硬切或冲击。
- [ ] 目视判断各场景首帧：能看到至少 6 种不同手法（kinetic type、morph/mask、line draw、粒子/网格、伪 3D、count-up、反色、whip）。
- [ ] 目视判断同一组帧：只有一个强调色、两个字体家族。
- [ ] 最后一帧是完整落版；落版段相隔 1s 的两帧位置一致（静止 ≥ 1.5s）。
- [ ] 在 break 白场与 drop 冲击的计划时刻 ±5 帧内逐帧抽取（每次 ≤ 24 帧）：连续纯白画面不超过 3 帧。
- [ ] `analyze_audio`（成片 MP4）：整合响度在 −20 到 −14 LUFS，true peak ≤ −1 dBTP，片头片尾无意外静音。
