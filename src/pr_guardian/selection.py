"""Turn "every file in the PR" into "the files we will actually review".

Every file that is dropped for a reason a human would care about is recorded
in ``skipped`` with that reason. WHY: silent truncation is the worst failure of
a review bot - people assume green means "everything was checked". Files that
are dropped for boring reasons (deleted, not an IaC file) are only counted.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from pr_guardian.config import Config
from pr_guardian.diff import ParsedPatch, PatchError, parse_patch
from pr_guardian.filetypes import FileKind, classify, glob_match
from pr_guardian.github_api import ChangedFile

# Per-file and total ceilings on patch size. They bound CPU/memory and keep one
# giant generated file from dominating the review. Constants, not inputs: the
# README should have few knobs. Tests shrink them via Limits.
MAX_PATCH_BYTES = 100_000
MAX_TOTAL_PATCH_BYTES = 500_000


@dataclass(frozen=True)
class Limits:
    max_patch_bytes: int = MAX_PATCH_BYTES
    max_total_patch_bytes: int = MAX_TOTAL_PATCH_BYTES


class SkipReason(StrEnum):
    NO_PATCH = "no patch from GitHub (binary file or diff too large)"
    PATCH_TOO_LARGE = "patch larger than the per-file size limit"
    UNPARSEABLE = "patch could not be parsed safely"
    OVER_MAX_FILES = "over the max-files limit"
    OVER_TOTAL_SIZE = "over the total patch size limit"


@dataclass(frozen=True)
class Skipped:
    path: str
    reason: SkipReason


@dataclass(frozen=True)
class ReviewTarget:
    path: str
    kind: FileKind
    status: str
    patch: ParsedPatch


@dataclass
class Selection:
    targets: list[ReviewTarget] = field(default_factory=list)
    skipped: list[Skipped] = field(default_factory=list)
    # Silent-but-counted buckets:
    removed: int = 0
    outside_paths: int = 0
    unsupported_type: int = 0
    content_unchanged: int = 0  # e.g. a pure rename
    total_changed: int = 0
    # True when GitHub's 3000-file ceiling means we cannot see every file.
    api_truncated: bool = False


def select_files(
    files: list[ChangedFile],
    config: Config,
    limits: Limits = Limits(),  # noqa: B008 - frozen dataclass, safe as a default
    api_truncated: bool = False,
) -> Selection:
    sel = Selection(total_changed=len(files), api_truncated=api_truncated)
    total_bytes = 0

    for f in files:
        if f.status == "removed":
            sel.removed += 1  # nothing in the new tree to comment on
            continue
        if config.paths and not any(glob_match(p, f.filename) for p in config.paths):
            sel.outside_paths += 1
            continue
        kind = classify(f.filename)
        if kind is None:
            sel.unsupported_type += 1
            continue
        if not f.patch:
            if f.additions == 0 and f.deletions == 0:
                sel.content_unchanged += 1
            else:
                sel.skipped.append(Skipped(f.filename, SkipReason.NO_PATCH))
            continue

        size = len(f.patch.encode("utf-8", errors="replace"))
        if size > limits.max_patch_bytes:
            sel.skipped.append(Skipped(f.filename, SkipReason.PATCH_TOO_LARGE))
            continue
        # Caps are checked *after* the cheap filters so that, e.g., 400 README
        # edits cannot use up the max-files budget meant for Terraform files.
        if len(sel.targets) >= config.max_files:
            sel.skipped.append(Skipped(f.filename, SkipReason.OVER_MAX_FILES))
            continue
        if total_bytes + size > limits.max_total_patch_bytes:
            sel.skipped.append(Skipped(f.filename, SkipReason.OVER_TOTAL_SIZE))
            continue
        try:
            parsed = parse_patch(f.patch)
        except PatchError:
            sel.skipped.append(Skipped(f.filename, SkipReason.UNPARSEABLE))
            continue

        total_bytes += size
        sel.targets.append(ReviewTarget(f.filename, kind, f.status, parsed))
    return sel
