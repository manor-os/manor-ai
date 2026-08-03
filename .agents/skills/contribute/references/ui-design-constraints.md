# Manor UI Design Constraints

Use this checklist for every public Manor web UI contribution. The live token and component implementations in `apps/web` remain authoritative.

## Visual direction

- Keep the interface calm, warm-neutral, low-saturation, and content-first.
- Use borderless frosted-glass surfaces with soft elevation and generous spacing.
- Create hierarchy with typography, weight, spacing, and elevation instead of decorative color.
- Keep primary content at the highest contrast and let application chrome recede.

## Tokens and color

- Use CSS variables from `apps/web/src/index.css` and Tailwind mappings from `apps/web/tailwind.config.ts`.
- Use warm `stone-*` neutrals. Do not introduce cool `slate-*` or `gray-*` chrome.
- Do not hard-code a color when an existing semantic token covers the role.
- Reserve Manor accent teal for primary actions, the active navigation item, the logo, focus rings, and direct form-control states.
- Keep secondary actions, tabs, filters, pagination, counts, badges, icon tiles, and decorative accents neutral or ink.
- Show status primarily with a small dot or icon on a neutral surface. Color text only when an alert state requires it.

## Surfaces and separation

- Keep cards, panels, forms, dialogs, dropdowns, and popovers borderless.
- Separate surfaces with translucent fills, blur, soft shadows, and spacing.
- Use a faint hairline only when dense tables, lists, or editors require a functional divider.
- Use the established panel, card, and control radii. Do not create one-off radius systems.

## Typography

- Use the configured sans-serif UI font for labels, body copy, and headings.
- Use the configured monospace font with tabular figures for metrics, IDs, timestamps, and code.
- Keep labels concise and preserve the existing type scale before adding a new size or weight.

## Components and interaction patterns

- Reuse primitives from `apps/web/src/components/ui/`, including `Card`, `CompactCard`, `Button`, `Input`, `Select`, `Textarea`, `Chip`, `StatusPill`, `TabSwitcher`, `SectionTabs`, `IconTile`, dialogs, dropdowns, and empty states.
- Route standard cards through the shared `Card` contract. Do not create bespoke card shells for individual pages.
- Prefer compact, clickable cards for summaries. Open full details and secondary actions in the single global `DetailDrawer` through the existing detail store.
- Keep the key action on a compact card only when it must be immediately available. Put remaining actions in the detail surface.
- Extend a shared primitive when a pattern will appear in more than one place.

## Accessibility and responsive behavior

- Use semantic HTML and accessible names for icon-only controls.
- Preserve logical keyboard order and visible `:focus-visible` treatment.
- Do not rely on color alone to communicate state.
- Maintain usable target sizes and readable contrast.
- Design narrow layouts intentionally: avoid clipped actions, hidden content, horizontal page overflow, and desktop-only interaction assumptions.
- Respect reduced-motion preferences for non-essential animation.

## Contribution review gate

Before considering a UI contribution complete, confirm:

- Existing tokens and shared primitives are reused.
- No unnecessary raw colors, borders, or page-local component systems were added.
- Accent teal is used only for an approved role.
- Default, hover, focus, disabled, loading, empty, and error states are coherent.
- Desktop and mobile layouts remain usable.
- Keyboard access, focus visibility, semantics, and contrast were checked.
- Any deliberate design-system change updates its public guidance in the same contribution.
- The change contains no sensitive data or non-public product information.
