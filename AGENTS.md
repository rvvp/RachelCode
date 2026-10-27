# Repository Workflow

## Server SSH Access — Read Before Connecting

Routine application releases MUST NOT use SSH, request a server password, or
read server files directly. Use the origin commit-status receipts described
below. SSH is reserved for an explicitly scoped infrastructure installation
or an incident that cannot be resolved through the automated release evidence.
Before proposing server access, exhaust the relevant local code/tests, origin
branch and commit statuses, Webhook/CI evidence available through the repository,
and bounded public target health checks. Explain the concrete missing evidence
or infrastructure change and obtain the user's authorization for that exceptional
access. Do not use server login as a routine diagnostic shortcut or repeatedly
retry authentication. A failed login does not authorize changing SSH/PAM policy.

Verified on 2026-10-27: server `203.205.90.232`, SSH port `22`, account `root`.
User preference: before starting server access for each new task/session, ask
the user for the current password. Do not retrieve passwords from earlier
conversations or stored files, and do not persist the supplied password. Within
that same authorized task, reuse its authenticated connection instead of
asking again for every command.
The successful method is an interactive PTY with password authentication, not
an assumed SSH key or jump host. Use `exec_command` with `tty: true`, wait for
the password prompt, and enter the authorized credential through terminal
input. Never put passwords in commands, files, logs, or Git.

First check and reuse any authenticated SSH ControlMaster from the current
task. For a fresh connection, create a private temporary directory with
`mktemp -d /tmp/rachel-ssh.XXXXXX`; use its `control` path with
`ssh -tt -F /dev/null -o PreferredAuthentications=password -o PubkeyAuthentication=no -o NumberOfPasswordPrompts=1 -o StrictHostKeyChecking=yes -o ControlMaster=yes -o ControlPersist=1800 -o ControlPath=<private-directory>/control root@203.205.90.232`.
Replace the path placeholder; do not reuse an expired historical socket path.
Keep the authenticated connection until that exceptional task is finished.
Routine release scripts do not use `SSH_CONTROL_PATH` or a root connection.

The verified PAM policy is `pam_faillock deny=3 even_deny_root unlock_time=60`:
three failed authentications can lock root for 60 seconds. External failed
attempts have also been observed. A single `Permission denied` is NOT evidence
that the supplied password is invalid. Do not rapidly retry; allow at least
60 seconds before one controlled retry. Prefer existing-session diagnostics
when available. Do not disable host-key checks, change SSH/PAM policy, reset
passwords, or install root keys just to work around a temporary lockout.

## Release Policy

Catalog (Cangbaoge), Planning, and Replenishment are THREE INDEPENDENT
APPLICATIONS in a monorepo, not a single deployable application. Read
`APPLICATION_BOUNDARIES.md` before changing release or deployment behavior.

Each requested application change normally follows a verified release pipeline:

1. Identify the explicitly authorized application(s); run the relevant local
   tests and code-format checks. Shared code requires dependency assessment.
2. Commit only source, tests and reviewed deployment/documentation changes;
   never include runtime data or credentials.
3. Publish the exact commit to `origin/main` ONLY after checking the installed
   dispatcher readiness receipt on origin matches the reviewed manifest.
   The SERVER performs isolation checks automatically. A repository push is
   itself a production action because the Webhook can trigger deployment.
4. Deploy, restart, verify, or roll back ONLY the explicitly targeted app.
   Verify that app's exact commit, source fingerprint and current workers.
   Other apps retain their existing revisions and must not be restarted.
5. After targeted public verification passes, back up the same commit to
   `github/main`. A backup commit does not mean every app runs that commit.

SERVER ISOLATION (2026-10-27): the reviewed isolated-v1 dispatcher is installed.
Catalog and Replenishment were subsequently enrolled one at a time, after
consistent database backups and initialization rehearsals on private copies.
All THREE apps now have private source snapshots, venvs, config files, offline
dependency caches, acceptance records and runtime pointers. The old all-site
activator remains permanently disabled. Every
release still requires installed-file hash attestation before an origin push,
obtained through authenticated origin commit statuses, not routine SSH.
Missing/mismatched proof blocks publication. Do not bypass this gate or restore
the retired shared deployer. Installation evidence is retained under
`/opt/rachelcode/automation/isolation-backups/20261027-145045`.

## Independent release implementation

`scripts/isolated_release.py` is designed to be installed and pinned outside
application releases; that installation has been verified. It requires an explicit single-app `deploy/release-plan.json`, a
matching `Release-App: <app>` commit trailer, the target's currently accepted
base commit, and reviewed shared/infrastructure paths. Missing/stale plans,
unapproved apps, schema changes and dependency-lock changes fail closed.

All three applications are now enabled, but every release still targets exactly
ONE application. Enabling all apps is never authorization for a combined restart.
Future application source snapshots
and virtual environments are private per release; code rollback never restores
database backups. Installation changes only deployment infrastructure and may
briefly pause the webhook listener; it must not restart any business service.
App migration is a separate authorized operation via
`scripts/migrate_isolated_app.py --app <catalog|replenishment> prepare|activate`.
Do not rerun migration against an already enrolled app. Existing DB, upload,
credential-key, browser-profile and report paths remain unchanged.
Each app's systemd sandbox permits persistent writes only to its own data root.

Release invocation after installation and attestation:

`scripts/publish_verified_release.sh --app planning tests.test_planning tests.test_rotation`

Use `--app catalog` or `--app replenishment` for those websites. A release plan
must use the target app's accepted `base_commit`; `repository_base_commit` is the
last origin/main commit before this push. A linear range of up to 50 local
commits is allowed only when EVERY commit names the same Release-App and ALL
changed paths in that range are reviewed (including subsequently reverted files).
The latter scopes this push independently of intervening other-app releases;
the target's full runtime delta is still checked for schema/dependency changes
and explicitly reviewed bundled/shared dependencies. Do not silently drop
intervening runtime changes from review.

Never call the retired all-site activator. Never bypass the installed-hash
attestation just to advance origin or GitHub. The server's acceptance record
and unchanged non-target service identities are required before code backup.

## Automatic server isolation and acceptance receipts

Daily flow: local tests -> origin/main -> automatic target-only server deploy
and isolation checks -> authenticated origin receipt + target public /healthz
verification -> same SHA to github/main. No routine server login.

The fixed dispatcher compares its files to a root-owned installed manifest,
rejects cross-app stop/restart dependencies and stale or out-of-scope plans,
and verifies the target's complete worker set and unchanged non-target process
identities. Failure rolls back only target code, never a business database.

The server writes `isolated-release/dispatcher` readiness and
`isolated-release/<app>` acceptance statuses to the exact origin commit.
Acceptance binds the SHA, generation, source fingerprint, worker count,
two unchanged protected apps, and dispatcher manifest digest. The local script
requires the latest successful receipt AND public target health before GitHub
backup. Missing, pending, failed, mismatched or timed-out evidence blocks backup.
GitHub neither deploys nor verifies production. Do not expose a root-command
HTTP endpoint or give ordinary code pushes permission to replace the dispatcher.

Changing the pinned dispatcher requires a separate, reviewed one-time installer
upgrade. That operation may briefly pause only the Webhook listener; it must
leave all three business services, app configs, databases and uploads untouched.
Until that upgrade and its readiness receipt are verified, the new local
publisher must stop before origin, not silently fall back to SSH or old deployers.

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
