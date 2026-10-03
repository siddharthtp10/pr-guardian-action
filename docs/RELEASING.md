# Releasing

Everything in this document is run **by the maintainer**. Nothing here is run
automatically, and nothing here needs a secret that is not already yours.

Rules of the road:

- A tag is a public promise. Tag only a commit that passed CI, the tests and the
  audit, and (for the first release) the live checks below.
- The only force-push in this project is moving the floating major tag `v1`.
  Never force-push a branch, and never move or delete a full version tag
  (`v1.0.0`) once people can have pinned it.
- Run each command yourself and read what it prints before the next one.

## 1. Repository metadata (single source of truth)

The values below are what the commands in section 2 use. A test keeps them
valid (length, topic format, count) and keeps the commands in sync.

```text
description: Review Terraform, Kubernetes, GitHub Actions and Dockerfile pull requests with deterministic rules and optional, clearly-labelled advisory AI feedback. Least-privilege, fork-safe, SHA-pinned and hash-locked.
topics: github-actions,github-action,pull-request,code-review,infrastructure-as-code,terraform,kubernetes,dockerfile,devsecops,security,static-analysis,policy-as-code,llm,anthropic,claude,supply-chain-security,python
```

GitHub allows up to 350 characters of description and 20 topics (lowercase
letters, digits and hyphens).

## 2. One-time repository setup

Run once, from any directory, with an authenticated `gh`. Every endpoint and flag
below was checked against GitHub's published OpenAPI description and
`gh repo edit --help`; if GitHub has since changed one, the command fails loudly
rather than doing something else.

```bash
REPO=siddharthtp10/pr-guardian-action

# Description and topics
gh repo edit "$REPO" \
  --description "Review Terraform, Kubernetes, GitHub Actions and Dockerfile pull requests with deterministic rules and optional, clearly-labelled advisory AI feedback. Least-privilege, fork-safe, SHA-pinned and hash-locked." \
  --add-topic github-actions --add-topic github-action --add-topic pull-request \
  --add-topic code-review --add-topic infrastructure-as-code --add-topic terraform \
  --add-topic kubernetes --add-topic dockerfile --add-topic devsecops \
  --add-topic security --add-topic static-analysis --add-topic policy-as-code \
  --add-topic llm --add-topic anthropic --add-topic claude \
  --add-topic supply-chain-security --add-topic python

# Secret scanning + push protection (a public repository gets these for free)
gh repo edit "$REPO" --enable-secret-scanning --enable-secret-scanning-push-protection

# Private vulnerability reporting (what SECURITY.md tells people to use)
gh api -X PUT "repos/$REPO/private-vulnerability-reporting"

# Dependabot alerts and security updates
gh api -X PUT "repos/$REPO/vulnerability-alerts"
gh api -X PUT "repos/$REPO/automated-security-fixes"

# Actions: read-only default token, and a human must approve fork PR workflow runs
gh api -X PUT "repos/$REPO/actions/permissions/workflow" \
  -f default_workflow_permissions=read -F can_approve_pull_request_reviews=false
gh api -X PUT "repos/$REPO/actions/permissions/fork-pr-contributor-approval" \
  -f approval_policy=all_external_contributors
```

Also in **Settings > Rules > Rulesets** (easiest in the web UI):

1. **Branch ruleset for `main`**: require a pull request and the status checks
   `lint`, `test (3.11)`, `test (3.12)`, `test (3.13)` and `action-smoke`; block
   force pushes and deletion.
2. **Tag ruleset for `v*`**: enable *Restrict deletions* and *Restrict updates* so
   a full version tag (`v1.0.0`) can never be moved or removed by accident, and add
   yourself (repository admin) to the bypass list so you can still move the
   floating `v1` tag. (Rule names in the API are `deletion`, `non_fast_forward`
   and `update`; the API payload for bypass actors was not tested, so use the UI.)

Finally, GitHub's per-user setting **Settings > Emails > Keep my email addresses
private** (and *Block command line pushes that expose my email*) stops your real
address from being written into future commits. It does not change commits that
already exist.

## 3. Pre-release gate (before every release)

On a clean checkout of `main`:

```bash
git switch main && git pull --ff-only
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
pytest -q && ruff check . && ruff format --check .

# Pre-publication audit: tracked files AND the full history.
# Put your employer/client names here; they are never written to the repo.
AUDIT_EXTRA_TERMS="your-employer,your-client" scripts/audit-public.sh
```

The audit must exit 0. If it reports a real secret, **rotate the secret first**;
removing it from history does not un-leak it.

Confirm the CI run for that exact commit on `main` is green in the Actions tab.

### Live checks (required for the first release and whenever posting or AI code changes)

Until these pass, the release is tested against fakes and one live read-only run.
Use the demo PR (it only touches a scratch branch):

```bash
scripts/open-demo-pr.sh --dry-run
scripts/open-demo-pr.sh                       # prints the PR URL and the cleanup command
```

| # | Do this | Expect | If not |
|---|---|---|---|
| 1 | Open the demo PR. | One review from `github-actions[bot]` with inline comments on the example lines, a red check, a job summary. Log line `Review created: N comment(s) added`. | Read the log: `Could not post review` means a permissions problem (see the README). Do not release. |
| 2 | Push a commit that **inserts a comment line at the top** of `demo-pr/kubernetes/deployment.yaml`. | `Review updated: 0 comment(s) added, N kept`, no duplicate comments, the review body now shows the new commit. This proves GitHub keeps comment lines aligned, which the re-run logic assumes. | Duplicates mean that assumption is wrong. Fix before releasing. |
| 3 | Push a commit that deletes the `privileged: true` line. | That comment is removed (or marked *Resolved* if someone replied), the review body updates. | Do not release. |
| 4 | Optional, costs a few cents: create the `ANTHROPIC_API_KEY` repository secret yourself (`gh secret set ANTHROPIC_API_KEY --repo "$REPO"`), push any commit to the demo branch. | One *AI-generated advisory* comment, a log line with real tokens and cost, **no change** to the check result. | If you skip this, say "AI path not verified live" in the release notes. |
| 5 | Optional: open a PR from a fork (a second account). | `NOTICE: fork pull request`, nothing posted, findings as annotations. | |

Clean up: `scripts/open-demo-pr.sh --cleanup demo/pr-guardian-<timestamp>`.

Record what you verified in the release notes.

## 4. Release candidate

A tag that is **not** `v1` and not a full release, so consumers are unaffected:

```bash
git tag -a v1.0.0-rc.1 -m "PR Guardian 1.0.0 release candidate 1"
git push origin v1.0.0-rc.1
```

Use it from a scratch repository for a day (`uses: siddharthtp10/pr-guardian-action@v1.0.0-rc.1`),
ideally pinned by its full SHA, and watch real PRs. If something is wrong, fix it,
and tag `v1.0.0-rc.2`.

## 5. Final release and the floating major tag

Tag the **exact commit** that passed the gate:

```bash
SHA=$(git rev-parse HEAD)            # check this is the commit you tested: git log -1 "$SHA"

# 1. The immutable full version
git tag -a v1.0.0 -m "PR Guardian 1.0.0" "$SHA"
git push origin v1.0.0

# 2. The floating major tag consumers use (@v1). Moving it needs a force push of
#    this ONE tag; the explicit refspec makes sure nothing else is pushed.
git tag -fa v1 -m "PR Guardian v1 (currently 1.0.0)" "$SHA"
git push --force origin refs/tags/v1
```

(Prefer signed tags if you have signing set up: use `-s` instead of `-a`.)

Verify, and do not proceed unless both lines match:

```bash
git ls-remote --tags origin 'v1*'
git rev-parse 'v1^{commit}' 'v1.0.0^{commit}'
```

### GitHub Release

```bash
awk '/^## \[1\.0\.0\]/{f=1;next} /^## \[/{f=0} f' CHANGELOG.md > /tmp/release-notes.md
less /tmp/release-notes.md            # add a "Verified live:" section from your gate results
gh release create v1.0.0 --verify-tag --title "v1.0.0" --notes-file /tmp/release-notes.md
```

Publishing to the Marketplace is optional and is a checkbox in the release form;
check that the name in `action.yml` is not already taken first.

### Verify as a consumer

In a scratch repository add a workflow that uses `@v1` (and a second that uses
the full SHA from `git rev-parse v1.0.0`), open a PR, and confirm both run.

## 6. Later releases

1. Update `CHANGELOG.md` (move items out of *Unreleased*) and bump the version in
   **both** `pyproject.toml` and `src/pr_guardian/__init__.py` (a test checks
   they agree with the changelog).
2. Merge through a pull request; run section 3.
3. Tag `v1.x.y` on the merge commit, **then move `v1`** to the same commit
   (section 5). Forgetting the second step means consumers on `@v1` never get
   the fix.
4. A breaking change is a new major: `v2`, with a new floating tag. Never change
   behaviour under `v1` in a way that breaks existing workflows.

### What counts as breaking for a tool that gates merges

A review bot has an unusual compatibility problem: **a new finding can turn
yesterday's green PR red** without any workflow changing. So:

| Change | Version bump |
|---|---|
| Bug fix, docs, performance, a rule's message wording | patch |
| New input or output with a backwards-compatible default; a **new `medium` or `low` rule** (the default `fail-on: high` ignores it); better detection that reduces false positives | minor, with a "may produce new findings" note in the changelog |
| Removing or renaming an input/output, changing a default, changing an exit code | **major** |
| Raising a rule's severity to or above the default `fail-on`, or adding a new `high`/`critical` rule | **major** (it can fail existing PRs) |
| Dropping Python/runner support the Action depends on | major |

## 7. A bad release

- **Do not delete or move a full version tag** that may be pinned.
- Fix forward: publish `v1.0.1` and move `v1` to it.
- If the problem is dangerous and a fix will take time, move `v1` back to the
  last good commit (`git tag -fa v1 -m "..." <good-sha>` then
  `git push --force origin refs/tags/v1`), and edit the bad GitHub Release to say
  so (mark it as a pre-release).
- If a **secret** was exposed, rotate it first. Everything else is secondary.

## 8. Why a floating tag is a trade-off

`@v1` is convenient and receives fixes automatically, but a tag is mutable: whoever
can push tags can change what every consumer runs. That is why the tag ruleset in
section 2 matters, and why consumers who want immutability should pin the full
commit SHA (and let Dependabot propose updates):

```yaml
- uses: siddharthtp10/pr-guardian-action@<full-40-char-sha>  # v1.0.0
```
