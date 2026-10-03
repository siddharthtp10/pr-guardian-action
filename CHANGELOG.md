# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project follows
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

The major tag (`v1`) always points at the newest `1.x.y` release. To pin exactly,
use a full commit SHA (see [docs/RELEASING.md](docs/RELEASING.md)).

## [Unreleased]

## [1.0.0] - 2026-10-03

First release.

### Added

- Composite GitHub Action that reviews pull requests changing Terraform,
  Kubernetes YAML, GitHub Actions workflows and Dockerfiles. Inputs:
  `github-token`, `anthropic-api-key`, `model`, `fail-on`, `paths`, `max-files`,
  `dry-run`. Outputs: `conclusion`, `findings-count`, `ai-findings-count`.
- Diff handling through the GitHub API: pagination, size and file-count caps, a
  fail-closed unified-diff parser that maps every finding to a valid comment
  line, and a visible "NOT reviewed" report for anything skipped.
- Rules engine with 17 generic rules (unpinned Actions, script injection,
  internet-open ingress, public S3, wildcard IAM, hard-coded secrets, missing
  resource limits, privileged or root containers, mutable image tags). Rules are
  validated YAML, flag added lines only, and never echo the matched text.
- One inline review per pull request, reconciled on re-runs: the body is updated
  in place, still-valid comments are kept, fixed findings are removed or marked
  resolved, and an identical re-run makes no writes. Job summary, annotations and
  step outputs. The job's exit code is the check (`fail-on`).
- Optional AI review layer: secret redaction before sending, the diff treated as
  untrusted data, structured output with strict validation to real added lines,
  sanitised text, a per-run cost cap, and clearly labelled advisory comments.
  AI findings can never affect the check.
- `paths` exclusions (`!pattern`).
- Read-only handling for fork pull requests and Dependabot runs: no writes, no
  AI call, findings shown as annotations and in the job summary.
- Demo: intentionally insecure `examples/` and `scripts/open-demo-pr.sh`, which
  opens a draft demo pull request from a scratch branch.
- Documentation: security model, threat model, cost estimate, limitations,
  architecture diagram, `SECURITY.md`, release process, and per-stage interview
  notes.
- `scripts/audit-public.sh`: a pre-publication audit of the working tree and the
  full git history.

### Security

- Refuses the `pull_request_target` event; never checks out or runs pull request
  code.
- Least privilege: `contents: read` and `pull-requests: write` only.
- Third-party Actions and pre-commit hooks pinned to full commit SHAs; every
  Python dependency, including transitive ones, hash-locked and installed with
  `pip --require-hashes`; the Anthropic SDK is installed only when a key is
  supplied.
- API key and token never printed, never placed in files, and redacted from
  anything sent to the AI provider.

### Known limitations

See [Limitations](README.md#limitations). Notably: the diff-only view, regex
rules, heuristic secret detection, and that releases have so far been verified
against fakes and one live read-only run, not against a live review post or a
live AI call.

[Unreleased]: https://github.com/siddharthtp10/pr-guardian-action/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/siddharthtp10/pr-guardian-action/releases/tag/v1.0.0
