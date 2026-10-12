# Logo 动画 / 片尾落版（Logo sting）

适用：3–8 秒的品牌 Logo 演绎或片尾落版，用于片头、片尾或社媒头像动画。默认：16:9、6s、无配音。

## 结构与节拍
时长严格控制在 3–8s。以 6s 为例：
1. 0–0.5s 预备：背景与一个引导元素（线、点、光）先出现，首帧不是纯黑。
2. 0.5–3.5s 组装：Logo 由几何部件组装、描边绘出、遮罩揭示或拼合；动作只用一种主手法。
3. 约 3.5s 冲击点：Logo 完整成形的那一帧，对齐唯一一个设计好的音效 hit。
4. 3.5–5s 品牌名 / 标语：字标（wordmark）与标语错峰出现（若用户提供标语）。
5. 最后 ≥ 1s：完整落版静止保持，不再位移，只允许轻微光泽扫过一次。

## 视觉规则
- Logo 必须从用户提供的文件重绘为矢量（SVG 优先；只有位图时逐像素描摹，并叠回原图比对到 1px 级误差）。绝不用 `generate_image` 生成 Logo，也不"优化"比例、字距、颜色。
- 品牌色以用户的品牌规范为准；没有规范时从提供的 Logo 文件取色，并在 CANVAS 写明取色来源。
- 落版尺寸：Logo 宽度约为画面宽的 25–35%，居中；四周留白 ≥ Logo 高度。
- 动作从品牌性格出发：稳重品牌用 `power3.out` / `expo.out` 长尾缓动；活泼品牌可用一次弹性，但最后静止帧必须精确对位。
- 片尾版本：结尾帧可以作为封面或接视频结尾，保持纯净背景，不加多余元素。
- 字标用 SVG 路径；标语文字用沙箱已安装字体（Noto Sans CJK SC、Lato、Roboto 等）或用户提供的品牌字体文件（放进 `assets/`）。

## 制作路线
1. 读取用户品牌资料（Logo 文件、品牌色、字体）；缺 Logo 文件时停下来索要，不自行生成。
2. 在 `video_edit`（HyperFrames）中把 Logo 写成内联 SVG，按路径 / 部件拆层；描边绘制用 stroke-dasharray / stroke-dashoffset，路径长度在构建时算出。
3. 音效：`generate_file(kind="audio")`（purpose 为 sfx），只要一个 hit（可带极短的上扬前奏）→ `normalize_audio_loudness`（例：−16 LUFS、true peak −1.5）→ `video_edit(operation="push_assets")`，单位增益放置，对齐 Logo 成形帧。
4. 所有动画挂在一条 paused timeline 上，是时间 t 的纯函数；不用实时时钟和无种子随机。
5. 审阅：`video_edit(operation="review", snapshot_times=[...])` 取首帧、冲击帧、最后一帧；最后一帧与原 Logo 文件并排比对；批准后只用返回的 `review_sha256` 渲染。
6. 成片 MP4 用 `probe_media`、`render_frame_samples`、`analyze_audio` 验收。

## 已知坑
- 生成或"手绘近似"的 Logo 会改变字形和比例，这是品牌事故；只能用用户文件重绘。
- 描边动画画完后忘了切换到实心填充，最终帧是空心 Logo。
- 多个音效叠在一起显得廉价；全片只要一个 hit。
- 落版静止时间不足 1s，作为片尾接其他视频时一闪而过。
- 品牌字体未提供：用相近字体替代字标会走样；字标应作为 SVG 路径重绘。

## 验收清单
- [ ] `probe_media`：时长在 3–8s 之间，分辨率与画幅符合 CANVAS。
- [ ] 目视判断成片最后一帧（`render_frame_samples`）与用户 Logo 文件并排：形状、比例、颜色一致。
- [ ] 成片 t=0 帧不是纯黑或空白。
- [ ] 最后 1s 内两帧（间隔 ≥ 0.9s）Logo 位置与大小完全一致。
- [ ] 在计划冲击时刻 ±5 帧逐帧抽取：Logo 恰在计划帧完整成形。
- [ ] `analyze_audio`（成片 MP4）：静音区间显示只有一段主要声音（一个 hit），true peak ≤ −1 dBTP。
