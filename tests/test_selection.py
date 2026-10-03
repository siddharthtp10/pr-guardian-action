from pr_guardian.config import Config
from pr_guardian.github_api import ChangedFile
from pr_guardian.report import render_selection, safe_name
from pr_guardian.selection import Limits, SkipReason, select_files

PATCH = "@@ -1 +1 @@\n-a\n+b"


def cfg(**kw):
    env = {"PRG_GITHUB_TOKEN": "t", **{f"PRG_{k.upper()}": str(v) for k, v in kw.items()}}
    return Config.from_env(env)


def f(name, patch=PATCH, status="modified", adds=1, dels=1):
    return ChangedFile(name, status, patch, adds, dels)


def test_selects_supported_and_counts_the_rest_quietly():
    files = [
        f("main.tf"),
        f("README.md"),
        f("old.tf", status="removed"),
        f("k8s/app.yaml"),
    ]
    sel = select_files(files, cfg())
    assert [t.path for t in sel.targets] == ["main.tf", "k8s/app.yaml"]
    assert sel.removed == 1 and sel.unsupported_type == 1
    assert sel.skipped == []  # boring drops are counted, not listed
    assert sel.total_changed == 4


def test_paths_filter():
    sel = select_files([f("a/main.tf"), f("b/main.tf")], cfg(paths="a/**"))
    assert [t.path for t in sel.targets] == ["a/main.tf"]
    assert sel.outside_paths == 1


def test_max_files_cap_is_reported_not_silent():
    files = [f(f"m{i}.tf") for i in range(5)]
    sel = select_files(files, cfg(max_files=2))
    assert len(sel.targets) == 2
    assert [s.reason for s in sel.skipped] == [SkipReason.OVER_MAX_FILES] * 3
    assert "NOT reviewed" in render_selection(sel)


def test_unsupported_files_do_not_consume_the_max_files_budget():
    files = [f(f"doc{i}.md") for i in range(10)] + [f("main.tf")]
    assert len(select_files(files, cfg(max_files=1)).targets) == 1


def test_missing_patch_is_a_visible_skip_but_pure_rename_is_not():
    files = [
        f("big.tf", patch=None, adds=5000, dels=0),
        f("moved.tf", patch=None, status="renamed", adds=0, dels=0),
    ]
    sel = select_files(files, cfg())
    assert [s.reason for s in sel.skipped] == [SkipReason.NO_PATCH]
    assert sel.content_unchanged == 1


def test_per_file_and_total_size_limits():
    big = "@@ -0,0 +1,1 @@\n+" + "x" * 100
    limits = Limits(max_patch_bytes=150, max_total_patch_bytes=200)
    huge = "@@ -0,0 +1,1 @@\n+" + "x" * 1000
    files = [f("a.tf", big), f("b.tf", big), f("c.tf", huge)]
    sel = select_files(files, cfg(), limits)
    assert [t.path for t in sel.targets] == ["a.tf"]
    assert [s.reason for s in sel.skipped] == [
        SkipReason.OVER_TOTAL_SIZE,
        SkipReason.PATCH_TOO_LARGE,
    ]


def test_unparseable_patch_is_skipped_loudly():
    sel = select_files([f("a.tf", "@@ -1,5 +1,5 @@\n a")], cfg())
    assert sel.targets == []
    assert sel.skipped[0].reason is SkipReason.UNPARSEABLE


def test_api_truncation_warning():
    sel = select_files([f("a.tf")], cfg(), api_truncated=True)
    assert "3000+" in render_selection(sel)


def test_hostile_file_names_cannot_forge_commands_or_markdown():
    evil = "a`b\n::error::pwned.tf"
    sel = select_files([f(evil)], cfg())
    text = render_selection(sel)
    assert "\n::" not in text
    assert "`" not in safe_name(evil)
    assert all(not line.startswith("::") for line in text.splitlines())
