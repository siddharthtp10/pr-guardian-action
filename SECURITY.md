# Security policy

PR Guardian runs in your CI with a write token and reads untrusted input, so
security reports are taken seriously. This is a small, single-maintainer
project: reports are handled on a best-effort basis, with no guaranteed response
time.

## Supported versions

Only the latest `1.x` release (the `v1` tag) receives fixes.

## Reporting a vulnerability

**Please do not open a public issue for a suspected vulnerability.**

Use GitHub's private vulnerability reporting: open the repository's **Security**
tab and choose **Report a vulnerability**. Include what you found, how to
reproduce it, and the impact you expect.

## What is in scope

- Ways a malicious pull request could execute code, exfiltrate the token or API
  key, forge workflow commands, or post attacker-controlled content as the bot.
- Ways to make the check pass or fail wrongly through the AI layer (it must never
  affect the verdict).
- Supply-chain issues in this repository's workflows, pins or lock files.
- Secrets or personal data committed to the repository or its history.

Out of scope: the contents of the rule set (missing or noisy rules are ordinary
bugs; open a normal issue), and vulnerabilities in GitHub or Anthropic themselves.

## Hardening tips for users

- Reference the Action by a full commit SHA, not only `@v1`.
- Keep `permissions:` to `contents: read` and `pull-requests: write`.
- Trigger it with `pull_request`. It refuses `pull_request_target` on purpose.
- Pass the API key only from a repository secret, never as a literal.
