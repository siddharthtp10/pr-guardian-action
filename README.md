# pr-guardian-action

A GitHub Action that reviews pull requests touching infrastructure and config
files (Terraform, Kubernetes YAML, GitHub Actions workflows, Dockerfiles) and
posts inline review comments.

It combines a fast, deterministic **rules engine** with an optional **AI review
layer**. Rules decide whether the check fails; AI is advisory only. With no API
key it runs in rules-only mode.

> **Status: Stage 4 of 7 (posting).** The Action fetches the PR's changed
> files, evaluates 17 rules, posts one review with inline comments, writes a job
> summary and annotations, and fails the check on findings at or above
> `fail-on`. The AI layer lands in Stage 5.

## Usage (target design)

```yaml
name: PR Guardian
on: pull_request            # never pull_request_target - see Security model

permissions:
  contents: read
  pull-requests: write

jobs:
  review:
    runs-on: ubuntu-latest
    steps:
      - uses: siddharthtp10/pr-guardian-action@v1   # pin a full SHA in production
        with:
          fail-on: high
          anthropic-api-key: ${{ secrets.ANTHROPIC_API_KEY }}  # optional
```

## Inputs

| Input | Required | Default | Description |
|---|---|---|---|
| `github-token` | no | `github.token` | Reads the diff, posts the review. |
| `anthropic-api-key` | no | `""` | Enables the AI layer. Empty = rules-only. |
| `model` | no | `claude-sonnet-5-5` | Model for the AI layer. |
| `fail-on` | no | `high` | `low`, `medium`, `high`, `critical` or `none`. Rules only. |
| `paths` | no | all supported | Newline/comma separated globs to restrict review. |
| `max-files` | no | `50` | Max files reviewed per run (1-500). |
| `dry-run` | no | `false` | Print findings instead of posting. |

## What gets reviewed

| Kind | Matched by path |
|---|---|
| Terraform | `*.tf`, `*.tfvars` |
| Kubernetes | any other `*.yaml` / `*.yml` outside `.github/` (heuristic - see below) |
| GitHub Actions | `.github/workflows/*.yml`, `action.yml` |
| Dockerfile | `Dockerfile`, `Dockerfile.*`, `*.dockerfile` |

Files are never silently dropped: anything supported but not reviewed (over
`max-files`, patch too large, no patch from GitHub, unparseable) is listed under
**"Skipped ... NOT reviewed"** in the log. Kubernetes detection is path-based
because the API returns only the patch, not the whole file; a non-Kubernetes
YAML file just produces no findings.

Exit codes: `0` ok, `1` findings at or above `fail-on`, `2` bad input or
unsupported event, `3` GitHub API error (including a review that could not be
posted). The Action runs only on `pull_request` and refuses `pull_request_target`.

## Where results appear

| Output | Needs write token | Notes |
|---|---|---|
| Check status (exit code) | no | Red when any rule finding is at or above `fail-on`. |
| Annotations | no | `error` for failing findings, `warning` for the rest. Shown on fork PRs too. |
| Job summary | no | Verdict, findings table, files that were not reviewed. |
| PR review | yes | One review (`COMMENT`, never "request changes") with up to 30 inline comments; the rest go in the review body. Skipped in `dry-run` and on fork PRs. |

Re-runs do not repeat themselves: a finding is not re-posted while an earlier
PR Guardian comment for the same rule still sits on the same line. GitHub marks
a comment outdated when its code changes, and then the finding is posted again.
The check result never depends on existing comments.

## Rules

17 generic rules ship in [`policies/default.yaml`](src/pr_guardian/policies/default.yaml);
see [docs/rules.md](docs/rules.md) for the table and the known blind spots.
Rules are data (id, severity, file types, regex, message), validated strictly at
load time, and only ever flag **added** lines. Messages are static text and
never echo the matched line, so a secret in a diff is not re-published.

## Development

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
pre-commit install
pytest && ruff check . && ruff format --check .
```

## Roadmap

Diff handling -> rules engine -> posting -> AI layer -> demo & docs -> release.
Security model, cost estimate, limitations and architecture diagram arrive in Stage 6.

## License

MIT
