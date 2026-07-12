---
sidebar_position: 10
title: API Reference
---

# API Reference

The API exposes OpenAPI documentation from the running backend.

## Local URLs

When running the API directly:

```text
http://localhost:8000/api/docs
http://localhost:8000/api/redoc
```

When running through Docker Compose:

```text
http://localhost:18080/api/docs
http://localhost:18080/api/redoc
```

## Generate OpenAPI JSON

```bash
make openapi
```

This writes the OpenAPI document to `docs/openapi.json` in a development tree.
The public documentation site focuses on human-authored operator docs; API
schema publication can be layered into the site as a later release.

## Authentication

Most API routes require an authenticated session or API key. Create API keys in
the application UI and scope them to the minimum required access.
