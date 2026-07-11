# Web deployment caching and version-refresh strategy

This project now ships a small version manifest at `/version.json` and exposes the current build version to the frontend via `__APP_VERSION__`.

## What is implemented

The web app polls `/version.json` every 60 seconds and whenever the tab becomes visible again. If the deployed version differs from the version baked into the running bundle, the app shows a persistent toast prompting the user to refresh.

The app also listens for chunk-loading failures such as dynamic import fetch errors after a deploy. On the first such failure in a short window, it automatically tries one `location.reload()` to self-heal. If the app still hits the same class of error after that reload, it falls back to a persistent refresh prompt instead of leaving the user with a confusing runtime error.

This reduces deploy-time breakage for long-lived SPA sessions by turning silent bundle drift into an explicit refresh path.

## Build inputs

`apps/web/vite.config.ts` now uses:

- `BUILD_VERSION` environment variable when provided
- otherwise an ISO timestamp generated at build time

Each build emits:

- hashed JS/CSS assets under `/assets/`
- `/version.json` with `{ "version": "..." }`

For reproducible deploys, set `BUILD_VERSION` in CI to something stable like a git SHA or release ID.

Example:

```bash
BUILD_VERSION=$(git rev-parse --short HEAD) npm run build
```

GitHub Actions in this repo now pass `BUILD_VERSION=${GITHUB_SHA}` for both the frontend CI build and the deploy image build.

## Cache policy

Recommended policy, which is now reflected in `docker/nginx.conf`:

`/index.html`, `/admin.html`, and `/version.json`
should be served with:

```text
Cache-Control: no-store, no-cache, must-revalidate
```

`/assets/*`
should be served with:

```text
Cache-Control: public, max-age=31536000, immutable
```

This gives new sessions the latest entrypoint while allowing already-open pages to keep using their existing hashed assets.

## Deployment notes

For the smoothest upgrades:

Keep old hashed assets available during rollout instead of deleting them immediately. The refresh prompt helps users move forward, but preserving old chunks during the deployment window avoids chunk-load failures for already-open tabs.

At the moment, this repo's deploy flow rebuilds the `manor-web` image and recreates the `web` container in place. The old image may still exist on the host until image pruning, but the old container filesystem is replaced during `docker compose up --force-recreate`. That means old frontend assets are not intentionally retained and served side-by-side today. If you want true zero-drift asset retention during rollout, you still need an additional release strategy, such as keeping prior static bundles in object storage/CDN or versioned web roots during the cutover window.

If your backend and frontend are deployed together, prefer backend changes that remain compatible with the previous frontend for at least one rollout window.

## User experience

When a new version is detected, users see a non-blocking toast with a refresh action. Dismissing it suppresses repeat prompts for that version for the rest of the tab session.

When a dynamic import or chunk load fails after a deploy, the app first attempts one automatic reload. If the problem persists, users then see a persistent warning toast with the same refresh action so they can recover without needing to understand the technical error.
