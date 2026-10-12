# Typography profiles

Select one profile from `templates/layouts/typography_profiles.json` before
writing `design_spec.md` and `spec_lock.md`. User-specified or explicit
template fonts take precedence. A profile provides title, body and code font
stacks; use its first installed font in the sandbox, then its listed fallbacks.
For mixed-language slides, keep one consistent CJK fallback across the deck.

Record the selected profile and resolved title/body fonts in the spec lock.
Do not mix more than two text families on one slide (a code face may be a third
when code is real content). Use the text-role system for scale and hierarchy;
the profile does not override role minimum sizes, clipping checks, or visual
inspection of the rendered `.pptx`.
