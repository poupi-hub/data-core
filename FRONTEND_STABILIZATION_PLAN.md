# Frontend Stabilization Plan

Last verified: 2026-05-26

## Scope

This document captures the current operational risks in `poupi-frontend` and the safe path to make frontend deploys reproducible without reintroducing notebook runtime dependency.

The local frontend workspace appears to be a pnpm/turbo monorepo with apps under `apps/*` and shared packages under `packages/*`.

## Current Findings

- No `.git` directory was found at `C:\Users\dev\Documents\Projetos\poupi-frontend`.
- Real `.env.local` files exist under multiple frontend apps:
  - `apps/crypto-dashboard/.env.local`
  - `apps/poupi-baby/.env.local`
  - `apps/quant-dashboard/.env.local`
  - `apps/real-estate-dashboard/.env.local`
  - `apps/sports-dashboard/.env.local`
- Multiple code paths still fall back to localhost endpoints:
  - `http://localhost:8000`
  - `http://localhost:3001`
- `packages/api-client/src/index.ts` defaults `NEXT_PUBLIC_API_URL` to `http://localhost:8000`.
- `apps/poupi-baby` has many server routes/pages that default `BACKEND_URL` to localhost.
- `README.md` documents per-app `.env.local` usage, but there is no verified production env contract or CI/CD flow in this local copy.
- Safe env examples were added to the local frontend workspace:
  - root `.env.example`;
  - per-app `.env.local.example` files.
- A production guardrail script was added:
  - `scripts/check-production-localhost.mjs`;
  - root package script `check:prod-env`.
- `pnpm check:prod-env` currently fails by design because localhost references still exist in app `.env.local` files, `apps/poupi-baby`, and `packages/api-client`.

## Operational Risk

Classification: `PARTIAL`

The frontend can likely be developed locally, but it is not yet production-operationally mature because:

- Deploy reproducibility cannot be proven without a Git root or remote origin.
- Runtime endpoints can silently point to localhost if env vars are missing.
- Secrets may remain scattered in local `.env.local` files.
- Different apps may build against different implicit API targets.

## Target State

- `poupi-frontend` is a normal Git repository with a known remote origin.
- Production builds run in CI/CD or on the server, never from an implicit notebook state.
- All production API targets are explicit and injected by the deployment platform.
- Local `.env.local` files contain only non-sensitive local development values.
- Safe examples exist as `.env.example` or `.env.local.example`.
- No production build can silently fallback to localhost.

## Required Environment Contract

Minimum expected variables:

```env
NEXT_PUBLIC_API_URL=https://<public-api-host>
BACKEND_URL=http://<internal-backend-service>:<port>
NEXT_PUBLIC_SITE_URL=https://<public-frontend-host>
NEXT_PUBLIC_SENTRY_DSN=
SENTRY_DSN=
```

Rules:

- `NEXT_PUBLIC_*` values are browser-visible and must not contain secrets.
- `BACKEND_URL` is server-side only and should point to an internal service URL in production.
- Missing production endpoint variables should fail fast during build/startup.
- Localhost fallback is acceptable only under explicit local development mode.

## Migration Plan

### Phase 1 - Version Control

1. Confirm whether an upstream GitHub repository already exists.
2. If it exists, reclone it cleanly and compare with this local folder.
3. If it does not exist, initialize Git only after owner approval.
4. Add `.env.local`, `.env.production`, `.next`, `node_modules`, and build artifacts to `.gitignore`.

### Phase 2 - Env Hygiene

1. Move any real local secrets out of the repository tree.
2. Create per-app `.env.local.example` files with safe placeholders.
3. Document production variables in one root `.env.example`.
4. Validate that no secret values are committed.

### Phase 3 - Remove Silent Localhost Fallbacks

1. Introduce a shared config helper that:
   - allows localhost defaults only in development;
   - requires explicit URLs in production;
   - rejects malformed URLs.
2. Replace repeated `process.env.BACKEND_URL || 'http://localhost:3001'` and `process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000'` patterns.
3. Add tests or static checks for prohibited production localhost fallback.

### Phase 4 - Reproducible Build

1. Standardize package manager on pnpm.
2. Verify `pnpm install --frozen-lockfile`.
3. Verify `pnpm build`.
4. Add CI checks for lint, typecheck, and build.
5. Build/deploy from CI or server, not from notebook-only state.

### Phase 5 - Deployment

1. Define one deploy target per app.
2. Inject env vars through Coolify/CI secrets.
3. Confirm Traefik routes and TLS for each frontend.
4. Validate `/`, health route if available, and API proxy routes.

## Immediate Safe Next Actions

Run these before changing frontend code:

```powershell
Get-ChildItem -Force C:\Users\dev\Documents\Projetos\poupi-frontend
Get-ChildItem -Recurse -Force C:\Users\dev\Documents\Projetos\poupi-frontend -Filter .git
rg -n "localhost|127\.0\.0\.1|BACKEND_URL|NEXT_PUBLIC_API_URL" C:\Users\dev\Documents\Projetos\poupi-frontend
cd C:\Users\dev\Documents\Projetos\poupi-frontend
pnpm check:prod-env
```

Then decide:

- If a GitHub repo exists: reclone and migrate changes through Git.
- If no GitHub repo exists: initialize repository and create first baseline commit after approval.

## Do Not Do Yet

- Do not delete local `.env.local` files until safe examples exist and secrets are moved.
- Do not mass-replace localhost fallbacks before identifying production endpoint names.
- Do not deploy a frontend build from this local folder until Git/CI state is clarified.
- Do not expose backend-only URLs as `NEXT_PUBLIC_*`.
