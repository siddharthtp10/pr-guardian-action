#!/usr/bin/env bash
# open-demo-pr.sh - open a DRAFT pull request containing the intentionally bad
# files from examples/, so you can watch PR Guardian review a real PR.
#
# What it does (and nothing else):
#   1. creates a scratch branch  demo/pr-guardian-<UTC timestamp>  from origin/<base>
#   2. copies the files listed in examples/manifest.tsv onto that branch
#   3. commits, pushes the scratch branch (plain push, never forced)
#   4. opens a DRAFT PR titled "DEMO: ... (do not merge)" with `gh`
#   5. switches you back to the branch you started on
#
# What it never does: touch your current branch or working tree, force-push,
# merge anything, edit repository settings, or create/read secrets.
#
# Usage:
#   scripts/open-demo-pr.sh [--base BRANCH] [--dry-run]
#   scripts/open-demo-pr.sh --cleanup demo/pr-guardian-YYYYMMDD-HHMMSS
#
# Needs: git, and `gh` (authenticated) unless --dry-run.
# Note: if the repository has an ANTHROPIC_API_KEY secret, the demo PR will make
# one AI call (about 10 cents at the very most on the default model).

set -euo pipefail

BASE="main"
DRY_RUN=0
MODE="open"
CLEANUP=""
BRANCH_PREFIX="demo/pr-guardian-"
BRANCH_PATTERN='^demo/pr-guardian-[0-9]{8}-[0-9]{6}$'
WORKFLOW=".github/workflows/pr-guardian.yml"
MANIFEST="examples/manifest.tsv"

usage() {
  sed -n '2,/^$/p' "$0" | sed 's/^# \{0,1\}//'
}

die() {
  echo "error: $*" >&2
  exit 1
}

while [ $# -gt 0 ]; do
  case "$1" in
    --base) [ $# -ge 2 ] || die "--base needs a branch name"; BASE="$2"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    --cleanup) [ $# -ge 2 ] || die "--cleanup needs a branch name"; MODE="cleanup"; CLEANUP="$2"; shift 2 ;;
    -h | --help) usage; exit 0 ;;
    *) die "unknown argument: $1 (try --help)" ;;
  esac
done

ROOT="$(git rev-parse --show-toplevel 2>/dev/null)" || die "run this inside the repository"
cd "$ROOT"

# --- cleanup mode -------------------------------------------------------------
# Decided by the flag, not by the value: `--cleanup ""` must be refused below, never
# silently turn into "open a PR".
if [ "$MODE" = "cleanup" ]; then
  # Refuse anything that is not a branch THIS script made. A typo here must never
  # be able to delete main or somebody's real work.
  [[ "$CLEANUP" =~ $BRANCH_PATTERN ]] || die "refusing to clean up '$CLEANUP': not a demo branch"
  command -v gh >/dev/null || die "gh is required for --cleanup"
  echo "Closing the demo PR for $CLEANUP and deleting the branch..."
  if ! gh pr close "$CLEANUP" --delete-branch --comment "Closing the PR Guardian demo."; then
    echo "No open PR found; deleting the scratch branch directly."
    git push origin --delete "$CLEANUP"
  fi
  exit 0
fi

# --- open mode ------------------------------------------------------------------
[ -f "$MANIFEST" ] || die "$MANIFEST not found; run from a checkout that has examples/"

# Read "source<TAB>destination<TAB>expected rules"; skip comments and blanks.
SOURCES=()
DESTS=()
while IFS=$'\t' read -r src dest _; do
  [ -z "${src:-}" ] && continue
  case "$src" in \#*) continue ;; esac
  [ -f "examples/$src" ] || die "manifest lists examples/$src but it does not exist"
  case "$dest" in /* | *..*) die "unsafe destination in manifest: $dest" ;; esac
  SOURCES+=("$src")
  DESTS+=("$dest")
done <"$MANIFEST"
[ "${#SOURCES[@]}" -gt 0 ] || die "manifest is empty"

STAMP="$(date -u +%Y%m%d-%H%M%S)"
BRANCH="${BRANCH_PREFIX}${STAMP}"
TITLE="DEMO: PR Guardian example findings (do not merge)"

if [ "$DRY_RUN" -eq 1 ]; then
  echo "DRY RUN - nothing will be created."
  echo "base branch : origin/$BASE"
  echo "new branch  : $BRANCH"
  echo "pr title    : $TITLE (draft)"
  echo "files:"
  for i in "${!SOURCES[@]}"; do
    echo "  examples/${SOURCES[$i]} -> ${DESTS[$i]}"
  done
  exit 0
fi

command -v gh >/dev/null || die "gh (GitHub CLI) is required; or use --dry-run"
if [ -n "$(git status --porcelain)" ]; then
  die "your working tree has uncommitted changes; commit or stash them first"
fi

ORIGINAL="$(git rev-parse --abbrev-ref HEAD)"
restore() { git switch -q "$ORIGINAL" 2>/dev/null || true; }
trap restore EXIT

git fetch -q origin "$BASE" || die "could not fetch origin/$BASE"
git rev-parse -q --verify "refs/remotes/origin/$BASE" >/dev/null || die "origin/$BASE not found"
# The demo only shows comments if the base branch already has the review workflow.
if ! git cat-file -e "origin/$BASE:$WORKFLOW" 2>/dev/null; then
  die "origin/$BASE has no $WORKFLOW, so the demo PR would not be reviewed. Merge it first, or pass --base <branch that has it>."
fi
git rev-parse -q --verify "refs/heads/$BRANCH" >/dev/null && die "branch $BRANCH already exists locally"

git switch -q -c "$BRANCH" "origin/$BASE"
for i in "${!SOURCES[@]}"; do
  mkdir -p "$(dirname "${DESTS[$i]}")"
  cp "examples/${SOURCES[$i]}" "${DESTS[$i]}"
  git add -- "${DESTS[$i]}"   # explicit paths only, never `git add -A`
done
git commit -q -m "demo: add intentionally insecure files for PR Guardian"

# A plain push of a brand-new scratch branch. No --force, ever.
git push -q -u origin "$BRANCH"

BODY="$(mktemp)"
trap 'rm -f "$BODY"; restore' EXIT
cat >"$BODY" <<'MD'
This is a **demo**. It adds intentionally insecure Terraform, Kubernetes, Dockerfile and
workflow files (copied from `examples/`) so PR Guardian has something to review.

* Do **not** merge. The workflow file it adds is inert (manual trigger only, job disabled).
* Expect inline comments, a failing check and a job summary. That is the point.
* Close it with `scripts/open-demo-pr.sh --cleanup <this branch>`.
MD
URL="$(gh pr create --draft --base "$BASE" --head "$BRANCH" --title "$TITLE" --body-file "$BODY")"

echo
echo "Opened: $URL"
echo "Clean up when you are done:"
echo "  scripts/open-demo-pr.sh --cleanup $BRANCH"
