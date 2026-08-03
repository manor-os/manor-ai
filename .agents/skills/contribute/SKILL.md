---
name: contribute
description: Apply Manor's public contribution standards when creating, editing, or reviewing web UI, React or TSX components, CSS, Tailwind classes, layouts, forms, cards, dialogs, navigation, responsive behavior, accessibility, or visual consistency in apps/web.
---

# Contribute to Manor

Use this skill to keep public Manor UI contributions consistent, reusable, accessible, and safe to publish.

## Required workflow

1. Read [references/ui-design-constraints.md](references/ui-design-constraints.md) before changing or reviewing UI.
2. Inspect the affected flow and nearby components before choosing a pattern.
3. Read the current repository sources of truth when they exist:
   - `docs/UI_DESIGN_SYSTEM.md`
   - `apps/web/src/index.css`, especially `:root`
   - `apps/web/tailwind.config.ts`
   - `apps/web/src/components/ui/`
4. Reuse or extend a shared primitive before creating page-local visual infrastructure.
5. Implement every relevant state: default, hover, focus, disabled, loading, empty, error, and responsive behavior.
6. Review the diff against the checklist in the reference.

If the reference and current code disagree, preserve the current token and component contracts. Update the public design guidance in the same contribution when intentionally changing the design system.

## Public contribution boundary

Treat this Skill and all contribution guidance as public. Use only public repository information. Omit credentials, customer or tenant data, internal URLs, deployment topology, and non-public product surfaces.

## Verification

Run checks proportional to the change:

```bash
npm --prefix apps/web run build
npm --prefix apps/web run test:source
git diff --check
```

For visual changes, also inspect the rendered UI at representative desktop and mobile widths. Verify keyboard navigation, visible focus, readable contrast, overflow, loading, empty, and error states. Report only checks actually run.
