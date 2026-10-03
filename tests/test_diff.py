from pathlib import Path

import pytest

from pr_guardian.diff import PatchError, parse_patch

FIXTURES = Path(__file__).parent / "fixtures"


def test_multi_hunk_line_and_position_mapping():
    p = parse_patch((FIXTURES / "multi_hunk.patch").read_text())
    assert p.hunk_count == 2
    # New-file line numbers: hunk 1 covers 1..6, hunk 2 covers 21..24.
    assert sorted(p.lines) == [1, 2, 3, 4, 5, 6, 21, 22, 23]
    assert [pl.new_line for pl in p.added_lines()] == [3, 4, 22]
    # Position counts from the first @@ (= 0) through every later line, including
    # the removed line (pos 3) and the second @@ header (pos 8).
    assert p.position_for(1) == 1
    assert p.position_for(3) == 4  # '-description' is pos 3, '+description' pos 4
    assert p.position_for(4) == 5
    assert p.position_for(21) == 9  # first line after the second header at pos 8
    assert p.position_for(22) == 10
    assert p.position_for(23) == 11


def test_removed_lines_and_lines_outside_hunks_are_not_commentable():
    p = parse_patch((FIXTURES / "multi_hunk.patch").read_text())
    assert not p.is_commentable(10)  # between the hunks
    assert not p.is_commentable(0)
    assert p.is_commentable(22)


def test_header_without_counts_means_one_line():
    p = parse_patch("@@ -5 +5 @@\n-old\n+new")
    assert p.hunk_count == 1
    assert p.lines[5].text == "new" and p.lines[5].added


def test_new_file_patch():
    p = parse_patch("@@ -0,0 +1,2 @@\n+a\n+b")
    assert [pl.new_line for pl in p.added_lines()] == [1, 2]


def test_pure_deletion_has_no_commentable_lines():
    p = parse_patch("@@ -1,2 +0,0 @@\n-a\n-b")
    assert p.lines == {}


def test_blank_context_line_with_stripped_space():
    # Some tooling strips the single space of an empty context line.
    p = parse_patch("@@ -1,3 +1,3 @@\n a\n\n-b\n+c")
    assert p.lines[2].text == "" and not p.lines[2].added
    assert p.lines[3].text == "c"


def test_crlf_is_stripped_from_text_but_not_used_to_split_lines():
    p = parse_patch("@@ -1,1 +1,2 @@\r\n a\r\n+b\x0bc\r")
    # \x0b (vertical tab) must NOT start a new line - splitlines() would have.
    assert p.lines[2].text == "b\x0bc"


def test_trailing_newline_is_ignored():
    assert parse_patch("@@ -1 +1 @@\n-a\n+b\n").lines[1].text == "b"


@pytest.mark.parametrize(
    "patch",
    [
        "garbage before header\n@@ -1 +1 @@\n-a\n+b",
        "@@ -1,3 +1,3 @@\n a\n+b",  # hunk truncated
        "@@ -1,3 +1,3 @@\n a\n+b\n",  # truncated AND newline-terminated (the trailing "" trap)
        "@@ -1 +1 @@\n-a\n+b\n+c",  # more lines than declared
        "@@ -1 +1 @@\n?weird",  # unknown prefix
        "@@ -1,2 +1,2 @@\n a\n@@ -9 +9 @@\n-x\n+y",  # first hunk cut short
    ],
)
def test_malformed_patches_fail_closed(patch):
    with pytest.raises(PatchError):
        parse_patch(patch)


def test_empty_patch_has_no_hunks():
    assert parse_patch("").hunk_count == 0
