# apps/web — UI guidance

**All UI changes must follow the design system:** [`docs/UI_DESIGN_SYSTEM.md`](../../docs/UI_DESIGN_SYSTEM.md).
Read it before any styling/visual work.

Quick rules (see the doc for detail):

- **Tokens first.** Use the CSS variables in `src/index.css` (`:root`) and the
  Tailwind scales (`manor.*` accent, `stone.*` neutrals, `font-sans` = Inter,
  `font-mono` = JetBrains Mono). Don't hard-code hex for roles a token covers.
- **Neutrals = warm `stone`.** Never `slate`/`gray`, never vivid Tailwind
  colours (`teal-*`, `emerald-500`, …) for chrome.
- **Borderless.** No `1px solid` outlines on cards/panels/forms/dialogs.
  Separate with the soft shadow + frosted fill + spacing. Dividers, only where
  functionally needed, are the faint hairline `rgba(28,25,23,0.06)`.
- **Brand teal only where it matters:** primary action, active nav, logo,
  focus ring, direct form-control state. Everything else neutral / ink.
- **Reuse primitives** in `src/components/ui/` (`Card`, `Button`, `Input`,
  `Select`, `Chip`, `StatusPill`, `TabSwitcher`, `IconTile`, …). All card
  surfaces (agent/skill/app/integration/team) go through `Card`.
- **Mono** (`.mono` / `font-mono`) for numerals, metrics, IDs, timestamps.
- **Detail pop-up is global.** Prefer compact clickable cards (`CompactCard`)
  that open the single global `DetailDrawer` via `openDetail(...)` from
  `stores/detail` — don't add per-page drawers, and put actions in the
  drawer, not in card footers.

If a change must deviate, update `docs/UI_DESIGN_SYSTEM.md` in the same change
so the doc stays the source of truth.
