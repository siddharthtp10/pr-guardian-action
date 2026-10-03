# pr-guardian-action

A GitHub Action that reviews pull requests touching infrastructure and config
files (Terraform, Kubernetes YAML, GitHub Actions workflows, Dockerfiles) and
posts inline review comments.

It combines a fast, deterministic **rules engine** with an optional **AI review
layer**. Rules decide whether the check fails; AI is advisory only. With no API
key it runs in rules-only mode.

> **Status: Stage 5 of 7 (optional AI review).** Rules decide pass/fail and post
> inline comments; an optional AI layer adds clearly-labelled advisory comments.
> The demo, full security model and release docs land in the last two stages.

## Usage

```yaml
name: PR Guardian
on: pull_request            # never pull_request_target - see Security model

permissions:
  contents: read
  pull-requests: write

# A new push supersedes the previous run, so a stale run cannot post against an
# old commit (and cannot spend an AI call on it).
concurrency:
  group: pr-guardian-${{ github.event.pull_request.number }}
  cancel-in-progress: true

jobs:
  review:
    runs-on: ubuntu-latest
    steps:
      - uses: siddharthtp10/pr-guardian-action@v1   # pin a full SHA in production
        with:
          fail-on: high
          paths: "!examples/**"                                # optional exclusions
          anthropic-api-key: ${{ secrets.ANTHROPIC_API_KEY }}  # optional: enables AI advice
```

The Action never checks out your code; it reads the diff through the API, so no
`actions/checkout` step is needed.

## Inputs

| Input | Required | Default | Description |
|---|---|---|---|
| `github-token` | no | `github.token` | Reads the diff, posts the review. |
| `anthropic-api-key` | no | `""` | Enables the AI layer. Pass it from a GitHub secret; empty = rules-only. |
| `model` | no | `claude-sonnet-5-5` | Model for the AI layer. |
| `fail-on` | no | `high` | `low`, `medium`, `high`, `critical` or `none`. Rules only. |
| `paths` | no | all supported | Newline/comma separated globs to restrict review. Prefix with `!` to exclude (e.g. `!examples/**`). |
| `max-files` | no | `50` | Max files reviewed per run (1-500). |
| `dry-run` | no | `false` | Run everything except posting the review. The exit code still reflects `fail-on`. |

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
| PR review | yes | One review (`COMMENT`, never "request changes") with up to 30 inline comments, most severe first; the rest go in the review body. Skipped in `dry-run` and on fork PRs. |
| Step outputs | no | `conclusion` (`success`/`failure`) and `findings-count`, for later workflow steps. |

Re-runs update the review instead of adding to it. The review body is edited in
place to describe the current state, a comment that still applies is kept, new
findings get new comments, and comments for fixed findings are deleted (or
edited to "Resolved" when someone replied, so the conversation survives). An
identical re-run makes no writes at all. GitHub marks a comment outdated when
its code changes, and then the finding is posted again at its new location. The
check result never depends on existing comments. Only comments and reviews
authored by a bot account are ever trusted or touched, so the token must be
`GITHUB_TOKEN` or a GitHub App token, not a personal access token.

## Rules

17 generic rules ship in [`policies/default.yaml`](src/pr_guardian/policies/default.yaml);
see [docs/rules.md](docs/rules.md) for the table and the known blind spots.
Rules are data (id, severity, file types, regex, message), validated strictly at
load time, and only ever flag **added** lines. Messages are static text and
never echo the matched line, so a secret in a diff is not re-published.

## AI review (optional, advisory)

Set `anthropic-api-key` to add an AI pass for what regex rules cannot see (risky
combinations, missing context, design problems). It is built so that it cannot
hurt you:

- **Advisory only.** AI findings are a different type from rule findings and
  never reach the pass/fail logic: they cannot fail the check, whatever they say.
  Each AI comment is labelled *AI-generated advisory* and says so.
- **Off by default, and off where it is unsafe.** No key = rules-only. Fork PRs
  never use the key, even if one is present: the PR author controls the input.
- **Minimal, redacted input.** Only the added and context lines of reviewed
  files (never the whole repo) plus rule finding IDs are sent, after secret
  redaction (known token formats, private keys, `password = ...` values,
  high-entropy strings, and the Action's own key and token). Removed lines are
  not sent.
- **The diff is data, not instructions.** It goes only in the user turn, inside
  a random per-run delimiter, and the system prompt says to ignore any
  instructions found in it. Because prompts are not a security boundary, the
  real defences come after the model: structured JSON output, strict
  validation, discarding anything that does not point at a line the PR added,
  and stripping links, HTML and @-mentions before anything is posted.
- **Bounded cost.** One request per run, at most 4,000 output tokens, about
  30,000 input tokens (estimated pessimistically) and a pre-flight worst-case
  cost check against a $0.25 per-run cap. On the default model the worst case is
  about $0.10 per run; typical PRs cost a few cents.
- **Fails soft.** Any API error becomes a warning and the run continues with
  rule results.

The `model` input defaults to `claude-sonnet-5-5`. The SDK is installed only when
a key is supplied, from a hash-locked file (`pip install --require-hashes`), so
rules-only runs download nothing extra.

Re-runs are stable: an AI comment is posted once per line and is not re-written
when the model words things differently; it is retired only when GitHub marks it
outdated. Rules-only runs (a fork, an outage) never delete earlier AI comments.

## Development

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
pre-commit install
pytest && ruff check . && ruff format --check .
```

Runtime dependencies are hash-locked. To change one, edit `requirements.in` or
`requirements-ai.in` (and the matching pin in `pyproject.toml` and
`requirements-dev.txt`; a test checks they agree), then regenerate with:

```bash
uv pip compile --python-version 3.12 --python-platform linux --generate-hashes \
  --no-header requirements.in -o requirements.txt
uv pip compile --python-version 3.12 --python-platform linux --generate-hashes \
  --no-header requirements-ai.in -o requirements-ai.txt
```

## Roadmap

Diff handling -> rules engine -> posting -> AI layer -> demo & docs -> release.
Security model, cost estimate, limitations and architecture diagram arrive in Stage 6.

## License

MIT
