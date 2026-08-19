# Repository Workflow

## Dual-Remote Push Policy

This repository contains both the Cangbaoge application and the Merchandise
Monitoring Center. Every committed update for either application must be pushed
to both configured remotes:

- `origin`: `https://git.lu9.com/lu9/RachelCode.git`
- `github`: `git@github.com:rvvp/RachelCode.git`

After pushing, verify that the current branch points to the same commit on both
remotes. Never commit or push local databases, exported reports, credentials,
tokens, environment files, browser profiles, or runtime logs.
