# Manor AI CASA AL1 remediation record

Date: 2026-08-13
Scope: repository implementation and local verification only
Source assessment: `manor-ai-casa-al1-preassessment-2026-08-13.md`

## Outcome

The highest-risk application findings have been remediated and verified on an isolated repository branch. This is not yet a production attestation: the changes have not been deployed or submitted to a CASA assessor.

Do not pay for the official review until the deployment and evidence items in the final section are complete.

## Original failed controls

| Control | Implementation status | Remediation |
|---|---|---|
| 1.1.1 | Implemented | Redis-backed account and IP login failure budgets; cloud mode cannot disable the limiter. Passwords require at least 12 characters and reject common breached examples. |
| 1.1.2 | Implemented | Email activation codes are now eight-character alphanumeric values containing at least one letter and one digit. Web and mobile inputs were updated. |
| 2.2.1 | Implemented | Logout calls the server, increments `token_version`, and uniformly clears browser auth material. |
| 2.2.2 | Implemented | Password change/reset and account deactivation increment `token_version`, invalidating previously issued access tokens. |
| 2.2.3 | Implemented | Access tokens are capped at 60 minutes even when `remember_me` is supplied; JWTs include `jti`, `iat`, `iss`, `aud`, and `token_version`. |
| 3.3.1 | Implemented | Every active platform administrator must have TOTP enabled and present an MFA-authenticated JWT session. Password login records verified TOTP in the JWT `amr` claim, and a dedicated TOTP step-up endpoint can mint a new MFA-bound token. |
| 4.1.1 | Repository complete; edge deployment pending | Caddy accepts only TLS 1.2/1.3 and emits one-year HSTS. Cloudflare's edge minimum TLS/cipher policy must still be changed and retested. |
| 4.1.3 | Partially complete | TLS/HSTS repository settings are present. New OAuth tokens use the credential service, but the production plaintext backfill must be run against Vault. |
| 5.1.3 | Implemented for API host | Entity-root directory validation is enforced and the API-host command monitor is disabled. User/model commands must continue through the sandbox service. |
| 5.1.6 | Production paths implemented; sandbox audit pending | Untrusted XML in production WeChat, RSS, and Office fallback paths uses `defusedxml`. Artifact skill scripts execute in the sandbox but should be tracked separately in the final Bandit evidence scope. |
| 5.1.9 | Implemented for production runtime | No `shell=True` call remains in the API/core production path used by the vulnerable monitor. |
| 5.2.1 | Repository complete; AV deployment pending | Upload allowlist, executable signatures, magic-byte checks, cloud fail-closed ClamAV scanning, and forced attachment delivery for active content were added. Cloud Compose now includes a health-gated ClamAV service. |
| 6.1.1 | Implemented | Production Python and Web dependency audits report zero known vulnerabilities. A bounded vendored `image-size` build replaces the vulnerable transitive version. |
| 6.5.1 | Implemented | Raw verification-code logging was removed and global log redaction now covers verification codes, tokens, client secrets, and API keys. |
| 6.6.1 | Risk reduced; design item remains | Logout/expiry/delete now clear all known application, portal, public-chat, OAuth, and impersonation browser state. The short-lived access token still uses `localStorage`; moving the primary session to Secure HttpOnly cookies remains the strongest closure if the assessor requires it. |
| 6.7.1 | New writes implemented; backfill pending | OAuth access/refresh tokens are stored through the credential service and legacy columns are cleared. The production backfill and zero-plaintext evidence remain required. |

## Additional hardening

- CORS is an explicit first-party allowlist in cloud mode; methods and headers are narrowed.
- Configured CORS origins reject wildcards, malformed origins, credentials in the authority, paths, queries, fragments, and non-HTTPS cloud origins.
- Redis-backed login throttling fails over to bounded, expiring in-process LRU buckets instead of allowing unbounded memory growth.
- Cloud OpenAPI, Swagger UI, and ReDoc endpoints are disabled.
- API, Nginx, and Caddy emit `nosniff`, anti-framing, referrer, permissions, and CSP headers; HSTS is enabled at the HTTPS edge.
- Cloud startup rejects missing, short, and shipped-placeholder JWT signing secrets.
- Active HTML, SVG, XML, JavaScript, and CSS files are delivered as attachments under a sandbox CSP.
- OAuth credential migration is dry-run by default and refuses plaintext credential backends when `--apply` is used.

## Verification performed

- Focused CASA security, OAuth, refresh, and migration graph suite: `54 passed`.
- TOTP/login and access-log redaction regression suite: `8 passed`.
- Web production build: passed (`2300` modules transformed).
- Full Web source-contract suite under CI's Node 20 runtime: `615 passed`, including DOCX HTML sanitization, malformed ICNS bounded parsing, and PDF.js 6 lifecycle coverage.
- Full Mobile Jest suite: `343 passed`; Mobile TypeScript check passed.
- `npm audit --omit=dev`: zero vulnerabilities.
- `pip-audit` against exported production dependencies: no known vulnerabilities.
- Bandit on production runtime paths (excluding sandboxed artifact skill bundles): zero high-severity findings.
- Ruff on the newly added Python security modules, migration, script, and baseline tests: passed.
- Caddy validation: valid configuration.
- Nginx validation: syntax successful.
- Base and cloud Compose validation: passed; cloud CORS, 60-minute JWT, mandatory AV, and ClamAV health dependency verified from the rendered model.

## Required before official CASA submission

1. Rotate every credential currently present in the local `.env` (OAuth clients, SMTP, API keys, Vault access, and signing material) before deployment. A local Compose validation expanded environment values into this task's tool output; no secret was written to tracked files, but the existing values must be treated as exposed.
2. Deploy the changes and run `alembic upgrade head`.
3. Verify Vault readiness, then run:

   ```bash
   uv run python scripts/migrate_oauth_account_credentials.py
   uv run python scripts/migrate_oauth_account_credentials.py --apply
   ```

   Preserve evidence that `plaintext_rows_after=0`.
4. Confirm all active platform administrators have `totp_enabled=true`, then exercise real admin login and recovery procedures.
5. Provision sufficient ClamAV memory, wait for current signatures and healthy status, and test a benign file plus the EICAR test signature in a non-production test environment.
6. In Cloudflare, set Minimum TLS Version to 1.2 or later, use a current cipher policy, enable HSTS deliberately, and rerun SSLyze against the public hostname.
7. Redeploy the frontend/API, then rerun ZAP and the required authenticated Burp configuration, including IDOR, SSRF, HPP, template injection, XSS, SQL injection, and LFI/RFI cases.
8. Decide with the assessor whether the remaining short-lived `localStorage` access token plus CSP/revocation evidence is acceptable. If not, implement a Secure HttpOnly SameSite session/refresh cookie with CSRF controls before payment.
9. Re-run the 48-control matrix from production evidence and only then purchase the official assessment.
