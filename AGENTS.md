# Repository Workflow

## Push Policy

Do not push automatically after code changes or commits. Push only when the
user gives an explicit push instruction. The instruction defines the scope:

- "双仓库推送" means push to both configured remotes.
- "origin 单仓库推送" means push only to `origin`.
- "github 单仓库推送" means push only to `github`.

If the user does not specify a push scope, leave the changes unpushed and ask
for clarification when a push is needed. Do not infer a dual-remote push from
the repository layout. The dual-remote requirement currently applies only to
code in the “应用开发” project; it does not apply automatically to every
project or code area in this repository.

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

After an explicitly requested push, verify the requested remote branch or
branches. For a dual-remote push, verify that both remotes point to the same
commit. Never commit or push local databases, exported reports, credentials,
tokens, environment files, browser profiles, or runtime logs.
