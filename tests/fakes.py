"""A stateful in-memory stand-in for the GitHub API.

It remembers what was posted, so a second run sees the first run's review and
comments - which is exactly what the "don't duplicate on re-run" tests need.

Compatibility: ``reviews`` lists the create_review CALLS (dicts), as the older
CLI tests expect; the review/comment OBJECTS GitHub would hold live in
``stored_reviews`` / ``stored_comments``.
"""

from __future__ import annotations

from pr_guardian.github_api import GitHubAPIError, PullFiles, Review, ReviewComment

MARKER_RE = r"<!-- pr-guardian:([A-Z][A-Z0-9]{1,5}-\d{3}) -->"


class FakeClient:
    def __init__(self, result=None, existing=(), post_errors=(), stored_reviews=()):
        self.result = result if result is not None else PullFiles([], False)
        self.stored_comments: list[ReviewComment] = list(existing)
        self.stored_reviews: list[Review] = list(stored_reviews)
        self.post_errors = list(post_errors)  # raised by successive create_review calls
        self.reviews: list[dict] = []  # create_review calls, in order
        self.calls: list[tuple[str, dict]] = []
        self.fail: dict[str, GitHubAPIError] = {}  # method name -> error to raise
        self.reject_comment_lines = False  # make create_review_comment 422
        self._next_id = 1000

    # --- helpers -----------------------------------------------------------
    @property
    def existing(self) -> list[ReviewComment]:
        return self.stored_comments

    def names(self) -> list[str]:
        return [n for n, _ in self.calls]

    def writes(self) -> list[str]:
        return [n for n in self.names() if n.startswith(("create_", "update_", "delete_"))]

    def open_comments(self) -> list[ReviewComment]:
        import re

        return [c for c in self.stored_comments if re.search(MARKER_RE, c.body)]

    def _record(self, name: str, **kw) -> None:
        self.calls.append((name, kw))
        if name in self.fail:
            raise self.fail[name]

    def _id(self) -> int:
        self._next_id += 1
        return self._next_id

    # --- reads -------------------------------------------------------------
    def list_pull_files(self, owner, repo, number):
        self._record("list_pull_files")
        if isinstance(self.result, Exception):
            raise self.result
        return self.result

    def list_reviews(self, owner, repo, number):
        self._record("list_reviews")
        return list(self.stored_reviews)

    def list_review_comments(self, owner, repo, number):
        self._record("list_review_comments")
        return list(self.stored_comments)

    # --- writes ------------------------------------------------------------
    def create_review(self, owner, repo, number, *, commit_id, body, comments):
        self._record("create_review")
        if self.post_errors:
            raise self.post_errors.pop(0)
        self.reviews.append({"commit_id": commit_id, "body": body, "comments": comments})
        self.stored_reviews.append(Review(self._id(), body, True))
        for c in comments:
            self.stored_comments.append(
                ReviewComment(c["path"], c["line"], c["body"], True, id=self._id())
            )

    def update_review(self, owner, repo, number, review_id, body):
        self._record("update_review", review_id=review_id, body=body)
        self.stored_reviews = [
            Review(r.id, body, r.author_is_bot) if r.id == review_id else r
            for r in self.stored_reviews
        ]

    def create_review_comment(self, owner, repo, number, *, commit_id, path, line, body):
        self._record("create_review_comment", path=path, line=line)
        if self.reject_comment_lines:
            raise GitHubAPIError("GitHub rejected the request as invalid (422)", 422)
        self.stored_comments.append(ReviewComment(path, line, body, True, id=self._id()))

    def update_review_comment(self, owner, repo, comment_id, body):
        self._record("update_review_comment", comment_id=comment_id, body=body)
        self.stored_comments = [
            ReviewComment(c.path, c.line, body, c.author_is_bot, c.id, c.in_reply_to_id)
            if c.id == comment_id
            else c
            for c in self.stored_comments
        ]

    def delete_review_comment(self, owner, repo, comment_id):
        self._record("delete_review_comment", comment_id=comment_id)
        self.stored_comments = [c for c in self.stored_comments if c.id != comment_id]
