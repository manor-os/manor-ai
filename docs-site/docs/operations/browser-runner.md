---
title: Browser Runner
---

# Browser Runner

`browser-runner` is a sidecar container for browser-backed automation. It runs
Chromium and Playwright outside the API and worker processes.

## Why It Exists

Some useful workflows depend on sites that do not provide complete APIs or need
interactive login. Running those flows in a dedicated sidecar keeps browser
dependencies, display services, and automation failures isolated from core
application services.

## How It Is Used

The API and worker call the runner over the Docker network. The runner executes
provider-specific browser flows and returns structured results or captured
artifacts.

## Configuration

| Variable | Purpose |
| --- | --- |
| `BROWSER_RUNNER_TOKEN` | Optional bearer token shared between Manor services and the runner. |
| `BROWSER_RUNNER_TIMEOUT_MS` | Upper bound for browser operations. |

Keep `BROWSER_RUNNER_TOKEN` set for shared environments.

## Troubleshooting

First build can take several minutes because the image includes Chromium and an
X11 stack.

Useful commands:

```bash
docker compose logs browser-runner --tail=200
docker compose build browser-runner
docker compose up -d browser-runner
```

If browser-backed integrations fail, check:

- The runner container is healthy.
- The API and worker can reach `http://browser-runner:5200`.
- Any required cookies or user sessions are still valid.
- The site being automated has not changed its UI.
