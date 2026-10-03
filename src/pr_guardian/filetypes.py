"""Decide which changed files we know how to review, by path alone.

WHY path-only: the API gives us the patch, not the whole file. Fetching full
contents would cost an API call per file and means handling more untrusted
data. The trade-off is a heuristic for Kubernetes (see classify()).
"""

from __future__ import annotations

import re
from enum import StrEnum
from pathlib import PurePosixPath


class FileKind(StrEnum):
    TERRAFORM = "terraform"
    KUBERNETES = "kubernetes"
    GITHUB_ACTIONS = "github-actions"
    DOCKERFILE = "dockerfile"


_YAML = (".yaml", ".yml")
# YAML that is definitely not a Kubernetes manifest. Everything else YAML is
# assumed to be one; rules (Stage 3) also match on manifest content, so a stray
# non-k8s YAML file simply produces no findings.
_DOC_SUFFIXES = (".md", ".txt", ".rst")
_NOT_KUBERNETES_PREFIXES = ("docker-compose", "compose.", ".pre-commit-config")


def classify(path: str) -> FileKind | None:
    p = PurePosixPath(path)
    name = p.name
    lower = name.lower()
    parts = p.parts

    if lower.endswith((".tf", ".tfvars")):
        return FileKind.TERRAFORM

    # "Dockerfile.dev" / "Dockerfile.prod" are common; "Dockerfile.md" is docs.
    is_variant = name.startswith("Dockerfile.") and not lower.endswith(_DOC_SUFFIXES)
    if name == "Dockerfile" or is_variant or lower.endswith(".dockerfile"):
        return FileKind.DOCKERFILE

    # Workflows must sit directly in .github/workflows/ to be executed by GitHub.
    if len(parts) == 3 and parts[:2] == (".github", "workflows") and lower.endswith(_YAML):
        return FileKind.GITHUB_ACTIONS
    # Composite/Docker/JS action manifests pin and run third-party code too.
    if lower in ("action.yml", "action.yaml"):
        return FileKind.GITHUB_ACTIONS

    if lower.endswith(_YAML):
        if parts and parts[0] == ".github":  # dependabot.yml, issue templates, ...
            return None
        if lower.startswith(_NOT_KUBERNETES_PREFIXES):
            return None
        return FileKind.KUBERNETES

    return None


def glob_match(pattern: str, path: str) -> bool:
    """gitignore-flavoured glob: ``*`` stays inside a directory, ``**`` crosses them.

    A pattern with no ``/`` (e.g. ``*.tf``) matches the file name at any depth,
    because that is what people expect from ``paths: "*.tf"``.
    """
    if "/" not in pattern:
        path = PurePosixPath(path).name
    return re.fullmatch(_glob_to_regex(pattern), path) is not None


def _glob_to_regex(pattern: str) -> str:
    out: list[str] = []
    i = 0
    while i < len(pattern):
        c = pattern[i]
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
            continue
        if pattern.startswith("**", i):
            out.append(".*")
            i += 2
            continue
        if c == "*":
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        else:
            out.append(re.escape(c))
        i += 1
    return "".join(out)
