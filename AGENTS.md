# Repository Workflow

## Release Policy

Catalog (Cangbaoge), Planning, and Replenishment are THREE INDEPENDENT
APPLICATIONS in a monorepo, not a single deployable application. Read
`APPLICATION_BOUNDARIES.md` before changing release or deployment behavior.

Each requested application change normally follows a verified release pipeline:

1. Identify the explicitly authorized application(s); run the relevant local
   tests and code-format checks. Shared code requires dependency assessment.
2. Commit only source, tests and reviewed deployment/documentation changes;
   never include runtime data or credentials.
3. Publish the exact commit to `origin/main` ONLY after proving the server
   dispatcher isolates the authorized application(s). A repository push is
   itself a production action because the Webhook can trigger deployment.
4. Deploy, restart, verify, or roll back ONLY the explicitly targeted app.
   Verify that app's exact commit, source fingerprint and current workers.
   Other apps retain their existing revisions and must not be restarted.
5. After targeted public verification passes, back up the same commit to
   `github/main`. A backup commit does not mean every app runs that commit.

SAFETY HOLD (2026-10-27): the installed server Webhook still invokes a shared
deployment script that stops all three services and can restore their databases.
Do not push to origin, rerun that script, or bypass the local safety gates until
the server dispatcher and per-app activation/rollback isolation are installed
and safely verified. Do local implementation, tests, and read-only diagnostics
instead; report the release as BLOCKED, not completed. The legacy all-site
activation entrypoint is permanently disabled. The new
`publish_verified_release.sh --app planning ...` entrypoint attests the installed
server dispatcher before pushing; until installation succeeds it must block.
Removing the hold requires actual server evidence,
not merely updating a local script or toggling an environment variable.

## Independent release implementation

`scripts/isolated_release.py` is designed to be installed and pinned outside
application releases; installation is still pending. It requires an explicit single-app `deploy/release-plan.json`, a
matching `Release-App: <app>` commit trailer, the target's currently accepted
base commit, and reviewed shared/infrastructure paths. Missing/stale plans,
unapproved apps, schema changes and dependency-lock changes fail closed.

The first rollout enables ONLY Planning. Catalog and Replenishment keep their
existing processes and shared legacy runtime untouched. Do not enable or migrate
them without a separate scoped rollout. Future application source snapshots
and virtual environments are private per release; code rollback never restores
database backups. Installation changes only deployment infrastructure and may
briefly pause the webhook listener; it must not restart any business service.

Release invocation after installation and attestation:

`scripts/publish_verified_release.sh --app planning tests.test_planning tests.test_rotation`

Never call the retired all-site activator. Never bypass the installed-hash
attestation just to advance origin or GitHub. The server's acceptance record
and unchanged non-target service identities are required before code backup.

Never push to `github` before the public verification passes. A failed local
test, origin push, deployment, or public verification stops the pipeline and
leaves GitHub unchanged. For projects other than Cangbaoge and Merchandise
Planning Center, or for an explicit one-remote request, follow the scope given
by the user instead of this automatic flow.

The configured remotes are:

- `origin`: `https://git.lu9.com/lu9/RachelCode.git`
- `github`: `git@github.com:rvvp/RachelCode.git`

## Deployment Topology

The only production deployment path is:

`local workstation -> origin/main -> company server -> public site`

`origin/main` is the only deployment source. The Webhook must dispatch an
explicit per-application release plan; sharing a branch does NOT authorize
full-site activation. A Planning-only change must never stop Catalog or
Replenishment. API compatibility checks do not authorize provider deployment.
Investigate the automated path when propagation fails; do not rerun a shared
deployment or request an all-service restart as a shortcut.

The `github` remote is a code backup only: it must not connect to the company
server, trigger deployment, or run public-site verification. A push to
`github` is never evidence that a production release started or completed.

After a release, verify that `origin/main`, the TARGET APP's public `/healthz`,
and `github/main` identify the exact same commit. Other apps may legitimately
run different commits; record their last accepted releases separately.

Catalog's production data and uploads are especially critical. Never overwrite,
restore, migrate, clean, or delete another app's database or uploads during an
application release. Code rollback must not restore an older business database;
data recovery is a separate, explicitly authorized operation with write-loss
analysis. Never commit or push databases, exported reports, credentials, tokens,
environment files, browser profiles, or runtime logs.
