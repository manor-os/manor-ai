> See [`image-generator.md`](./image-generator.md) for generated-image behavior.

# Image Acquisition Common Reference

Shared baseline for the built-in generated-image path.

---

## 1. Trigger Condition

Active when at least one resource list row has `Acquire Via: ai`. Rows with `user` / `placeholder` are skipped.

| Mode | Trigger |
|---|---|
| In-pipeline | `generate-ppt` workflow, image rows present |
| Standalone | Direct request against an existing project |

---

## 2. Image Resource List Format

Defined in `design_spec.md §VIII`. Status enum: see [`svg-image-embedding.md`](svg-image-embedding.md).

| Filename | Dimensions | Purpose | Type | Acquire Via | Status | Reference |
|---|---|---|---|---|---|---|
| cover.png | 1280x720 | Cover background | Background | `ai` | Pending | Modern tech abstract, deep blue gradient #0A2540 |
| hero_detail.png | 800x600 | Supporting visual | Illustration | `ai` | Pending | Diverse engineering team in modern office |

**Required per non-skipped row**: `Acquire Via`, `Status`, `Reference`.

---

## 3. Path Dispatch

For each row with `Status: Pending`:

| Acquire Via | Load reference | Run | Success status |
|---|---|---|---|
| `ai` | [`image-generator.md`](./image-generator.md) | Manor system image tool; `image_prompts.py` manages sidecar/status only | `Generated` |
| `user` | — | — | (already `Existing`) |
| `placeholder` | — | — | (already `Placeholder`) |

> The built-in skill does not run web image search. Use generated images, user-provided images, or `Needs-Manual`.

---

## 4. Analysis Phase

Before processing any row:

1. `read_file <project_path>/design_spec.md` — extract color scheme, canvas format, target audience
2. Extract rows where `Acquire Via: ai`
3. Confirm `project/images/` exists

---

## 5. Verification Phase

After all rows reach terminal status:

- Every non-skipped row has a file at `project/images/<filename>`, or is marked `Needs-Manual`
- No `Pending` rows remain
- `image_prompts.json` exists when ≥1 ai row processed; every entry has `status ∈ {Generated, Failed, Needs-Manual}` (no `Pending` remaining), and `image_prompts.md` is refreshed with `image_prompts.py --render-md`

> `Needs-Manual` is a legitimate terminal state for ai rows — Step 7 entry waits for the user to place the file. See [`image-generator.md`](./image-generator.md) §3.2 Offline Manual Mode.

---

## 6. Failure Handling

**Hard rule**: acquisition failures MUST NOT halt the pipeline.

1. Try once
2. On recoverable failure (system image tool unavailable, rate limit, invalid output), retry once with adjusted prompt/parameters
3. On second failure, set `Status: Needs-Manual`, log the reason in conversation, continue
4. After the phase completes, summarize all `Needs-Manual` rows for the user — list filenames, where prompts live (`images/image_prompts.md` paste-ready blocks for ai rows; refresh via `image_prompts.py --render-md` if stale), and where to place generated files (`project/images/<filename>`)

`Needs-Manual` is also the entry status for **Offline Manual Mode** (no host-native image tool is available). Affected ai rows are marked `Needs-Manual` from the start without a failed attempt — see [`image-generator.md`](./image-generator.md) §7.

Path-specific retry policies live in `image-generator.md`.

---

## 8. Handoff with Strategist

The `Reference` field is **intent**, not a query. Strategist writes free-form intent; the receiving role translates.

| ✅ Intent | ❌ Pre-processed |
|---|---|
| `"Diverse engineering team in modern office, natural light"` | `"team office light"` |
| `"Abstract digital waves, deep navy gradient #0A2540"` | `"use openverse, search 'waves'"` |

---

## 9. Handoff with Executor

Executor consumes the resource list plus:

| Artifact | Path | Purpose |
|---|---|---|
| Image files | `project/images/*.{jpg,png,webp}` | `<image>` references |
| Prompt manifest | `project/images/image_prompts.json` | prompt/status per Generated or Needs-Manual AI image |

Executor does NOT invoke `image_prompts.py`.

---

## 10. Task Completion Checkpoint

```markdown
## ✅ Image Acquisition Phase Complete
- [x] {N} ai image rows processed
- [x] {a} `Generated`, {c} `Needs-Manual`
- [x] image_prompts.json written
- [ ] **Next**: Auto-proceed to Executor phase
```
