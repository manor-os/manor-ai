# Integration brand directory

`catalog.ts` is the only provider-to-logo/color/alias registry. `marks.ts`
contains locally vendored paths and embedded provider favicons; full SVG assets
live alongside them in `assets/`. Import SVGs in `marks.ts` with `?no-inline`
so Vite emits content-hashed URLs. Do not place unversioned logos under public
`/assets/`: production caches that path for one year with `immutable`.
Render them with `components/IntegrationLogo`.
Chat replies, composer connectors, Integrations cards/drawers and workflow
nodes all use this directory. No runtime favicon service or external image
request is needed.

Add a verified mark and its entry in `sources.json`, then register its provider
and visual aliases in `catalog.ts`. Aliases never change setup URLs, provider
IDs, credentials, authorization, catalog availability or composer eligibility.
Generic Email/IMAP/SMTP and Webhook are protocols, not Gmail/vendor brands.
Unknown/custom MCPs get a neutral connection glyph rather than a guessed logo.

Run `node --test scripts/integration-brand*.test.mjs` from `apps/web` to check
managed-provider coverage, aliases, local assets and the shared consumers.
The asset regression builds disposable copies through Vite, checking that
unchanged artwork keeps its URL and changed artwork receives a new hash.

## Asset provenance

See `sources.json` for upstream URLs, pinned revisions and per-asset license
links where supplied. Existing marks came from Simple Icons; additional marks
use the installed Simple Icons version, its pinned 11.15.0 archive, provider
websites, provider repositories and Lobe Icons. The Simple Icons collection is
CC0, but brand trademarks and individual asset guidelines still apply; these
marks identify third-party integrations, not Manor ownership or endorsement.
See https://github.com/simple-icons/simple-icons/blob/develop/DISCLAIMER.md.
Lobe Icons' required MIT notice is retained in `LOBE-ICONS-LICENSE`.

The additional Agile CRM workflow mark comes from the n8n connector catalog.
