# Deployment — apichain-backend

## Status: staging deployment is DEFERRED (not yet live)

DigitalOcean staging (plan `apichain-v2/07` §9) is **intentionally not activated.**
Cost is deferred and deployment is not the current focus; **local `docker compose up`
is the dev and test target.** What lives in this repo today is the *shape* of the
deploy so that turning it on later is a checklist, not a design exercise:

| File | What it is | State |
|------|-----------|-------|
| `.do/app.yaml` | DigitalOcean App Platform spec (service, health check, DB/migration blocks) | Template — DB + secrets commented out, untested |
| `.github/workflows/deploy.yml` | CD workflow, push-to-`main` → DO deploy | Inert — deploy job gated on `vars.DEPLOY_ENABLED == 'true'` (unset) |

Both are safe to have committed: CI stays green with no secret set, and nothing
deploys. **Do not treat either file as working infrastructure** until the steps
below are done and verified.

## Why this is already deploy-ready (no rework needed to go live)

- **The image builds.** CI runs `docker build` on every push (`.github/workflows/ci.yml`
  `docker` job), so the backend image is proven before any deploy.
- **Config is externalized, not baked in.** The app reads `DATABASE_URL` (and Phase 1+
  secrets) from the environment — nothing hardcodes a host. The container `CMD`
  binds `0.0.0.0:8000`, which App Platform needs.
- **Migrations ship with the deploy.** The `.do/app.yaml` PRE_DEPLOY `migrate` job
  runs `alembic upgrade head`. It is a no-op until Phase 1 cuts the first revision,
  wired now so it is never an afterthought.

## Activation checklist (run when funded + deployment is in scope)

Estimated cost ~$12–20/mo: backend `basic-xxs` service ~$5 + dev-tier managed
Postgres ~$7–15. Client static site is free tier (see the client repo's DEPLOYMENT.md).

1. **DO account + project.** Create a DigitalOcean account and a project (e.g.
   `apichain-staging`). Region `fra` (Frankfurt) is set in `app.yaml` — closest to Kenya.
2. **Access token.** DO → API → Generate a Personal Access Token (read+write).
3. **GitHub secret + variable** (per repo — do the client repo too):
   ```bash
   gh secret set DIGITALOCEAN_ACCESS_TOKEN --repo Apichain-Kenya/apichain-backend
   gh variable set DEPLOY_ENABLED --body true --repo Apichain-Kenya/apichain-backend
   ```
   The variable is the master switch: with it unset, `deploy.yml` skips.
4. **Complete `.do/app.yaml`.** Uncomment the `databases:` (managed PostGIS) and
   `jobs:` (pre-deploy migrate) blocks. Add Phase 1+ runtime secrets (`SECRET_KEY`,
   object storage, mail) as DO-managed app-level secrets — **never commit them.**
5. **Verify the CD action.** `deploy.yml` uses `digitalocean/app_action/deploy@v2`;
   confirm its current input names (`token`, `app_spec_location`) against the action's
   README before the first run — this workflow has never executed.
6. **First deploy.** Merge a trivial change to `main` (or run the workflow via
   `workflow_dispatch`). Watch the Actions run create/update the App Platform app.
7. **Verify staging is up.**
   ```bash
   curl https://<staging-backend-url>/health   # -> {"status":"ok"} (see app/main.py)
   ```
   Then point the client's `VITE_API_BASE_URL` at this URL (client DEPLOYMENT.md).
8. **Uptime ping.** Add the `/health` URL to a free uptime monitor (UptimeRobot etc.)
   so a dead staging is noticed. Full observability is Phase 5.

## Rollback / teardown

- **Pause deploys without deleting anything:** `gh variable set DEPLOY_ENABLED --body false`.
- **Tear down to stop billing:** delete the App Platform app and the managed DB in
  the DO console; unset the secret/variable. The repo files stay inert, ready to
  re-activate.
