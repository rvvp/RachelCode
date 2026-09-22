# Repository Workflow

## Release Policy

For Cangbaoge and Merchandise Planning Center application code, finish each
requested change through the verified release pipeline unless the user
explicitly asks to keep it local:

1. Run the relevant local tests and code-format checks successfully.
2. Commit only application code and tests; never include runtime data.
3. Run `scripts/publish_verified_release.sh` with the relevant unittest names.
4. The script pushes the exact commit to `origin/main`, waits for automatic
   deployment, verifies the public release across all workers, and only then
   pushes that same commit to `github/main` as a backup.

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

`origin/main` is the deployment source. Its existing Webhook automatically
notifies the company server to pull, activate, and verify every release; the
server administrator does not participate in routine deployments. After an
`origin/main` push, verify the public commit and build directly. If propagation
is delayed or fails, investigate the automated release path first instead of
asking the administrator to perform the deployment manually.

The `github` remote is a code backup only: it must not connect to the company
server, trigger deployment, or run public-site verification. A push to
`github` is never evidence that a production release started or completed.

After every release, verify that `origin/main`, the public `/healthz` response,
and `github/main` all identify the exact same commit. Never commit or push local
databases, exported reports, credentials, tokens, environment files, browser
profiles, or runtime logs.
