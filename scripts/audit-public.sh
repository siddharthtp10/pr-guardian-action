#!/usr/bin/env bash
# audit-public.sh - check whether this repository is safe to make (or keep) PUBLIC.
#
# It scans the tracked files AND the full history of every ref: a secret that was
# deleted in a later commit is still public once the repository is. It is
# read-only and never prints a matched value, only "file:line" or
# "commit + pattern name", so its own output is safe to paste anywhere.
#
# Usage:
#   scripts/audit-public.sh [--strict]
#
#   AUDIT_EXTRA_TERMS="acme,acme-internal" scripts/audit-public.sh
#
# AUDIT_EXTRA_TERMS: comma-separated, case-insensitive terms that must not appear
# anywhere (file names, file contents, commit messages, author names). Use it for
# names that belong to a current or former employer or client. It is read from the
# environment on purpose, so those names never have to be written into the repo.
#
# Exit status: 0 = nothing blocking (REVIEW notes may still be printed),
#              1 = blocking findings (or any finding with --strict), 2 = bad usage.
#
# Limits: this is a pattern scan, not a guarantee. It complements, and does not
# replace, a real secret scanner (gitleaks, trufflehog, GitHub secret scanning).

set -euo pipefail

STRICT=0
case "${1:-}" in
  "") ;;
  --strict) STRICT=1 ;;
  -h | --help) sed -n '2,/^$/p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
  *) echo "usage: $0 [--strict]" >&2; exit 2 ;;
esac

git rev-parse --git-dir >/dev/null 2>&1 || { echo "error: not inside a git repository" >&2; exit 2; }
cd "$(git rev-parse --show-toplevel)"

BLOCKING=0
REVIEW=0
blocking() { BLOCKING=$((BLOCKING + 1)); echo "BLOCKING  $*"; }
review() { REVIEW=$((REVIEW + 1)); echo "REVIEW    $*"; }

# name|extended regex. Provider-specific token formats, so false positives are rare.
SECRET_PATTERNS=(
  "aws-access-key|(AKIA|ASIA)[0-9A-Z]{16}"
  "github-token|gh[pousr]_[A-Za-z0-9]{30,}"
  "github-fine-grained-token|github_pat_[A-Za-z0-9_]{50,}"
  "anthropic-key|sk-ant-[A-Za-z0-9_-]{10,}"
  "api-key-sk|sk-[A-Za-z0-9]{32,}"
  "slack-token|xox[abposr]-[A-Za-z0-9-]{10,}"
  "google-api-key|AIza[0-9A-Za-z_-]{35}"
  "private-key|-----BEGIN [A-Z ]*PRIVATE KEY-----"
  "jwt|eyJ[A-Za-z0-9_-]{10,}[.]eyJ[A-Za-z0-9_-]{10,}[.][A-Za-z0-9_-]{10,}"
  "aws-arn-with-account-id|arn:aws[a-z-]*:[a-z0-9-]+:[a-z0-9-]*:[0-9]{12}:"
)
# Documented AWS example values are not findings.
BENIGN='EXAMPLE|123456789012|000000000000'

FORBIDDEN_FILES='(^|/)(\.env(\..*)?|id_rsa[^/]*|id_ed25519[^/]*|[^/]*\.(pem|key|p12|pfx|jks|keystore)|[^/]*\.tfstate(\.backup)?|\.terraform/.*|credentials|[^/]*\.kubeconfig)$'

COMMITS="$(git rev-list --all)"
N_COMMITS="$(printf '%s\n' "$COMMITS" | grep -c . || true)"
echo "Auditing $N_COMMITS commit(s) across all refs, and the tracked files at HEAD."
echo

# --- 1. files that should never be committed (HEAD and history) ------------------
echo "== Sensitive file names"
found=0
while IFS= read -r path; do
  [ -z "$path" ] && continue
  case "$path" in *.example | *.sample | *.template) continue ;; esac
  if [[ "$path" =~ $FORBIDDEN_FILES ]]; then
    blocking "file name looks sensitive and is in the history: $path"
    found=1
  fi
done < <(git log --all --name-only --format= | sort -u)
[ "$found" -eq 0 ] && echo "ok"

# --- 2. secret-shaped strings: working tree, then every commit --------------------
echo "== Secret-shaped strings (tracked files, then history)"
found=0
for entry in "${SECRET_PATTERNS[@]}"; do
  name="${entry%%|*}"
  pattern="${entry#*|}"
  # -I skips binaries; cut keeps only file:line so the match itself is never printed.
  hits="$(git grep -nIE -e "$pattern" -- . 2>/dev/null | grep -Ev "$BENIGN" | cut -d: -f1,2 || true)"
  if [ -n "$hits" ]; then
    while IFS= read -r hit; do
      blocking "$name in tracked file at $hit"
      found=1
    done <<<"$hits"
  fi
done
while IFS= read -r commit; do
  [ -z "$commit" ] && continue
  diff="$(git show --no-color --format= "$commit" 2>/dev/null || true)"
  for entry in "${SECRET_PATTERNS[@]}"; do
    name="${entry%%|*}"
    pattern="${entry#*|}"
    # No `grep -q` inside a pipeline: with `set -o pipefail` its early exit makes the
    # upstream command die of SIGPIPE, the pipeline "fail", and a real hit read as "none".
    matches="$(printf '%s\n' "$diff" | grep -E -e "$pattern" | grep -Ev "$BENIGN" || true)"
    if [ -n "$matches" ]; then
      blocking "$name appears in the history, commit $(git rev-parse --short "$commit")"
      found=1
    fi
  done
done <<<"$COMMITS"
[ "$found" -eq 0 ] && echo "ok"

# --- 3. caller-supplied terms (employer / client names, internal project names) -----
echo "== Extra terms (AUDIT_EXTRA_TERMS)"
if [ -z "${AUDIT_EXTRA_TERMS:-}" ]; then
  echo "skipped: AUDIT_EXTRA_TERMS is not set. Set it to your employer/client names to check them."
  review "no extra terms were checked"
else
  found=0
  IFS=',' read -r -a TERMS <<<"$AUDIT_EXTRA_TERMS"
  for term in "${TERMS[@]}"; do
    term="$(printf '%s' "$term" | tr -d '[:space:]')"
    [ -z "$term" ] && continue
    if git grep -qIiF -e "$term" -- . 2>/dev/null; then
      blocking "a configured term appears in tracked files (locate it with: git grep -iIF <term>)"
      found=1
    fi
    while IFS= read -r commit; do
      [ -z "$commit" ] && continue
      # grep -c reads all input (see the SIGPIPE note above) and prints a count, never the match.
      count="$({
        git log -1 --format='%an%n%ae%n%cn%n%ce%n%B' "$commit"
        git show --no-color --format= --name-only "$commit"
        git show --no-color --format= "$commit"
      } | grep -ciF -e "$term" || true)"
      if [ "${count:-0}" -gt 0 ]; then
        blocking "a configured term appears in the history, commit $(git rev-parse --short "$commit")"
        found=1
      fi
    done <<<"$COMMITS"
  done
  [ "$found" -eq 0 ] && echo "ok"
fi

# --- 4. personal machine paths ---------------------------------------------------------
echo "== Personal machine paths (tracked files)"
hits="$(git grep -nIE -e '/Users/[A-Za-z]|/home/[a-z]|[A-Z]:[\\]Users[\\]' -- . 2>/dev/null |
  grep -Ev '/home/runner/|/home/user/' | cut -d: -f1,2 || true)"
if [ -n "$hits" ]; then
  while IFS= read -r hit; do blocking "absolute home path at $hit"; done <<<"$hits"
else
  echo "ok"
fi

# --- 5. things a human should decide about (non-blocking) ---------------------------------
echo "== Commit identities (publicly visible once the repository is public)"
found=0
while IFS= read -r identity; do
  [ -z "$identity" ] && continue
  review "real e-mail address in history: $identity"
  found=1
done < <(git log --all --format='%an <%ae>%n%cn <%ce>' | sort -u |
  grep -Ev '<(noreply@[a-z.]+|[^@]+@users\.noreply\.github\.com)>$' || true)
[ "$found" -eq 0 ] && echo "ok"

echo "== Attribution trailers in commit messages"
trailers="$(git log --all --format=%B | grep -Eic '^(Co-Authored-By|Claude-Session):' || true)"
if [ "${trailers:-0}" -gt 0 ]; then
  review "$trailers trailer line(s) (Co-Authored-By / Claude-Session) in commit messages; decide whether to keep them BEFORE tagging a release"
else
  echo "ok"
fi

echo "== E-mail addresses in tracked files (URL user:pass@host forms are ignored)"
# Regex safety: a cheap literal ('@') must be on the line before the real pattern runs,
# and every quantifier is bounded. An unbounded leading `[a-z]+@` rescans a long line
# from every start position, so a single minified 1 MB line would hang the audit.
hits="$(git grep -nIE -e '@' --and -e '[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,253}[.][A-Za-z]{2,24}' -- . 2>/dev/null |
  grep -Eiv 'noreply@|@example\.(com|org|net)|@[a-z0-9.-]*\.(invalid|example|test|localhost)|users\.noreply\.github\.com|@sha256|@v[0-9]|@[0-9a-f]{7,}|://[^[:space:]@]*@' | cut -d: -f1,2 || true)"
if [ -n "$hits" ]; then
  while IFS= read -r hit; do review "e-mail-like text at $hit"; done <<<"$hits"
else
  echo "ok"
fi

echo "== Internal-looking hostnames and private IPs (outside tests/ and examples/)"
hits="$(git grep -nIE -e '[.](corp|internal|intranet|lan)|(10|192|172)[.][0-9]' --and \
  -e '[A-Za-z0-9-]{1,63}[.](corp|internal|intranet|lan)\b|(^|[^0-9.])(10[.][0-9]{1,3}[.][0-9]{1,3}[.][0-9]{1,3}|192[.]168[.][0-9]{1,3}[.][0-9]{1,3}|172[.](1[6-9]|2[0-9]|3[01])[.][0-9]{1,3}[.][0-9]{1,3})' \
  -- . ':!tests' ':!examples' 2>/dev/null | cut -d: -f1,2 || true)"
if [ -n "$hits" ]; then
  while IFS= read -r hit; do review "internal-looking host or IP at $hit"; done <<<"$hits"
else
  echo "ok"
fi

echo "== Large files and leftover markers"
large=""
while IFS= read -r -d '' f; do
  [ -f "$f" ] || continue
  if [ "$(wc -c <"$f")" -gt 1000000 ]; then large="$large$f"$'\n'; fi
done < <(git ls-files -z)
if [ -n "$large" ]; then
  while IFS= read -r f; do [ -n "$f" ] && review "file over 1 MB: $f"; done <<<"$large"
fi
# This script is excluded: its own search pattern would match itself.
markers="$(git grep -nIE -e '\b(TODO|FIXME|XXX)\b' -- src action.yml scripts ':!scripts/audit-public.sh' 2>/dev/null |
  cut -d: -f1,2 || true)"
if [ -n "$markers" ]; then
  while IFS= read -r hit; do review "unfinished-work marker at $hit"; done <<<"$markers"
fi
[ -z "$large" ] && [ -z "$markers" ] && echo "ok"

echo
echo "Summary: $BLOCKING blocking, $REVIEW to review."
if [ "$BLOCKING" -gt 0 ]; then
  echo "NOT safe to publish: fix the BLOCKING items (if a real secret was committed, ROTATE it first;"
  echo "removing it from history is not enough)."
  exit 1
fi
if [ "$STRICT" -eq 1 ] && [ "$REVIEW" -gt 0 ]; then
  echo "--strict: REVIEW items count as failures."
  exit 1
fi
echo "Nothing blocking. Read the REVIEW items and decide."
