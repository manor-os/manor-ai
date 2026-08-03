# Manor Skill Generation Contract

Use this contract when generating, reviewing, or substantially rewriting a Manor Skill specification.

## Goal

Create the smallest Skill that reliably captures the user's reusable workflow. Optimize for correct activation, clear execution, appropriate tool use, progressive disclosure, and verifiable output—not instruction length.

## Output envelope

Return one JSON object with exactly these required fields:

```json
{
  "name": "lowercase-hyphenated-name",
  "slug": "lowercase-hyphenated-name",
  "display_name": "Human-readable name",
  "description": "Semantic discovery description",
  "system_prompt": "Markdown operating instructions",
  "tools": [],
  "input_schema": {},
  "output_format": "text",
  "category": "category-name",
  "tags": [],
  "complexity": "worker"
}
```

The optional fields are:

```json
{
  "scripts": {
    "script_name.py": "complete standalone source"
  },
  "references": {
    "reference-name.md": "complete reference content"
  }
}
```

Do not add configuration, credentials, invocation policies, ownership fields, IDs, timestamps, or deployment metadata.

## Field rules

### Identity

- Use a short `name` with lowercase letters, digits, and hyphens.
- Use the same value for `slug` unless compatibility requires another URL-safe form.
- Use a concise, human-readable `display_name`.
- Preserve identity when updating an existing Skill unless the user explicitly requests a rename.

### Description

Treat `description` as the discovery contract. State:

- what the Skill enables;
- the semantic situations in which it should be used;
- adjacent or implicit requests for which it remains useful;
- important near-misses when confusion with another capability is likely.

Use natural semantic guidance rather than a literal keyword list. Keep the description concise enough to remain useful in the always-visible Skill catalog.

### Main instructions

Write `system_prompt` in imperative English. Include only sections required by the workflow. A useful structure may include:

- objective or outcome;
- inputs, assumptions, and defaults;
- ordered workflow;
- tool usage;
- failure handling and approval boundaries;
- output contract;
- quality checks.

Do not force a universal template when a simpler structure is clearer. Do not target an arbitrary size such as 200-300 lines. Keep the main instructions comfortably below 500 lines and move detailed static material into references.

Explain the reason behind consequential constraints. Use strict wording only where errors, side effects, security, or output compatibility demand it.

### Tools

- Declare only tools used by the instructions.
- Prefer `search_tools` when a workflow must discover an Integration dynamically.
- Use `invoke_skill` only when composition with another reusable Skill is intentional.
- Never invent a tool name or imply that a Skill can bypass authorization, approval, or runtime policy.
- Do not list sandbox lifecycle tools merely because scripts or references are present; Manor packaging supplies those tools.

### Inputs and output

- Use a valid JSON Schema object for `input_schema`; use `{}` for free-form input.
- Select `output_format` from `text`, `markdown`, or `json`.
- Define the observable output shape in the instructions when consumers depend on it.
- Document defaults only when they are safe and unsurprising.

### Complexity

- Use `worker` for bounded, routine, or repeatable tasks with limited judgment.
- Use `primary` for multi-step work requiring significant research, judgment, coordination, or many tool calls.

### Category and tags

- Use a short descriptive `category` label; Manor does not require a fixed category taxonomy.
- Use a small set of tags that improve browsing without duplicating the discovery description.

### Scripts

Add a script when deterministic behavior or repeated computation is more reliable than regenerating code on every run.

- Use a basename such as `build_report.py`; do not include directory traversal.
- Provide complete standalone source.
- Prefer the standard library unless the runtime contract explicitly provides another dependency.
- Define input, output, failure behavior, and validation in the main instructions.
- Do not create scripts that perform unrelated collection or hidden side effects.

### References

Move long-lived domain knowledge, schemas, policies, examples, and templates into references.

- Use one descriptive Markdown filename per topic.
- Link each reference directly from the main instructions and say when to read it.
- Avoid duplicating reference content in `system_prompt`.
- Include a table of contents when a reference is large enough to need navigation.

## Quality review

Review the complete specification against these questions:

1. Would the description activate for realistic requests without matching unrelated near-misses?
2. Can an agent execute the workflow without inventing missing steps, data, or tools?
3. Are instructions no longer than needed for reliability?
4. Is repeated deterministic work captured in a script?
5. Is detailed static knowledge moved to a directly linked reference?
6. Are external effects, approvals, authorization, and failure states handled safely?
7. Is the output observable and testable?
8. Does every declared tool appear in the workflow, and does every required tool exist?
9. Would any behavior surprise a user who read the Skill's name and description?

In review mode, return exactly `PASS` only when the Skill satisfies the contract. Otherwise return a complete corrected specification, not commentary alone.

## Output discipline

For generation or complete correction, output only the JSON object. Do not use Markdown fences or surrounding prose. The exact `PASS` token is the sole non-JSON exception, and only in review mode.
