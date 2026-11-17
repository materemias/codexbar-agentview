from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import unittest
from pathlib import Path
from typing import Any


SCRIPT_PATH = (
    Path(__file__).resolve().parents[1]
    / "contents"
    / "scripts"
    / "waiting_github.py"
)
sys.path.insert(0, str(SCRIPT_PATH.parent))
SPEC = importlib.util.spec_from_file_location("waiting_github", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
github = importlib.util.module_from_spec(SPEC)
sys.modules["waiting_github"] = github
SPEC.loader.exec_module(github)

REPO = "acme/widget"
HEAD = "a" * 40
OLD_HEAD = "b" * 40
NEW_HEAD = "c" * 40
MERGE_SHA = "d" * 40
MERGED_AT = "2026-11-01T10:00:00Z"


def bot_review(oid: str = HEAD, login: str = "greptile-apps", rid: str = "PRR_bot") -> dict:
    return {
        "id": rid,
        "state": "COMMENTED",
        "body": "",
        "commit": {"oid": oid},
        "author": {"login": login, "__typename": "Bot"},
    }


def comment(cid: str, login: str, body: str = "hello", typename: str = "User", created: str | None = None) -> dict:
    node = {"id": cid, "body": body, "author": {"login": login, "__typename": typename}}
    return {**node, "createdAt": created} if created else node


def thread(
    tid: str = "T1",
    resolved: bool = False,
    comments: int = 1,
    author: str = "greptile-apps",
    line: int | None = 12,
) -> dict:
    return {
        "id": tid,
        "isResolved": resolved,
        "path": "src/a.py",
        "line": line,
        "comments": {
            "nodes": [
                {"id": f"{tid}-{i}", "body": "nit", "author": {"login": author, "__typename": "Bot"}}
                for i in range(comments)
            ]
        },
    }


def check(
    name: str,
    conclusion: str | None = "SUCCESS",
    required: bool = False,
    status: str = "COMPLETED",
    app: str | None = None,
) -> dict:
    node = {"name": name, "status": status, "conclusion": conclusion, "isRequired": required}
    return {**node, "checkSuite": {"app": {"slug": app}}} if app else node


def threads_page(nodes: list, older_cursor: str | None = None) -> dict:
    return {
        "pageInfo": {"hasPreviousPage": older_cursor is not None, "startCursor": older_cursor},
        "nodes": nodes,
    }


def pr_answer(
    state: str = "OPEN",
    head: str = HEAD,
    merge_state: str = "CLEAN",
    reviews: list | None = None,
    comments: list | None = None,
    threads: list | None = None,
    checks: list | None = None,
    author: str = "me",
    older_cursor: str | None = None,
    more_checks: str | None = None,
    merge_commit: bool = True,
) -> dict:
    contexts = {
        "pageInfo": {"hasNextPage": more_checks is not None, "endCursor": more_checks},
        "nodes": checks or [],
    }
    return {
        "data": {
            "viewer": {"login": "me"},
            "repository": {
                "pullRequest": {
                    "state": state,
                    "headRefOid": head,
                    "baseRefName": "main",
                    "mergeStateStatus": merge_state,
                    "mergedAt": MERGED_AT if state == "MERGED" else None,
                    "closedAt": MERGED_AT if state != "OPEN" else None,
                    "mergeCommit": {"oid": MERGE_SHA} if state == "MERGED" and merge_commit else None,
                    "author": {"login": author},
                    "baseRef": {"target": {"oid": "e" * 40}},
                    "comments": {"nodes": comments or []},
                    "reviews": {"nodes": reviews or []},
                    "reviewThreads": threads_page(threads or [], older_cursor),
                    "commits": {"nodes": [{"commit": {"statusCheckRollup": {"contexts": contexts}}}]},
                }
            },
        }
    }


def ok(stdout: str = "", code: int = 0, stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], code, stdout, stderr)


NOT_FOUND = ok(code=1, stderr="gh: Not Found (HTTP 404)\n")


class FakeGh:
    """Answers gh by what it was asked, and records every call."""

    def __init__(self, pr: dict | None = None, remaining: Any = 5000, **answers: Any) -> None:
        self.pr: Any = pr or pr_answer()
        self.remaining: Any = remaining
        self.calls: list[list[str]] = []
        self.rules: Any = []
        self.releases: list = []
        self.older_threads: dict[str, dict] = {}
        # Later pages of check contexts and of one thread's comments, by cursor.
        self.more_checks: dict[str, list] = {}
        self.more_comments: dict[str, list] = {}
        self.compare: dict[str, Any] = {}
        self.rest_pr: dict = {}
        self.rest_reviews: list = []
        self.user: Any = ok("me\n")
        # The head's workflow runs, newest first: (name, status, conclusion[, workflow]),
        # or a gh answer.
        self.runs: Any = []
        # Any of the answers above, by name.
        for name, answer in answers.items():
            assert hasattr(self, name), name
            setattr(self, name, answer)

    def count(self, needle: str) -> int:
        return sum(1 for call in self.calls if any(needle in arg for arg in call))

    def __call__(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        assert argv[0] == "gh"
        assert kwargs["timeout"] >= 30
        args = argv[1:]
        self.calls.append(args)
        names = ("repos/", "query=", "rate_limit", "user")
        target = next((arg for arg in args[1:] if arg.startswith(names)), "")
        if target == "rate_limit":
            if isinstance(self.remaining, subprocess.CompletedProcess):
                return self.remaining
            return ok(f"{self.remaining}\n")
        if target == "user":
            return self.user
        if target.startswith("query="):
            variables = dict(arg.split("=", 1) for arg in args if "=" in arg and not arg.startswith("query="))
            if "before: $before" in target:
                page = {"reviewThreads": self.older_threads[variables["before"]]}
                return ok(json.dumps({"data": {"repository": {"pullRequest": page}}}))
            if "contexts(first: 100, after: $after)" in target:
                contexts = {"pageInfo": {"hasNextPage": False}, "nodes": self.more_checks[variables["after"]]}
                commit = {"statusCheckRollup": {"contexts": contexts}}
                pull = {"commits": {"nodes": [{"commit": commit}]}}
                return ok(json.dumps({"data": {"repository": {"pullRequest": pull}}}))
            if "node(id: $id)" in target:
                assert "owner" not in variables, "a node query declares no $owner"
                comments = {"pageInfo": {"hasNextPage": False}, "nodes": self.more_comments[variables["after"]]}
                return ok(json.dumps({"data": {"node": {"comments": comments}}}))
            if isinstance(self.pr, subprocess.CompletedProcess):
                return self.pr
            return ok(json.dumps(self.pr))
        if "/rules/branches/" in target:
            if isinstance(self.rules, subprocess.CompletedProcess):
                return self.rules
            return ok("".join(f"{name}\n" for name in self.rules))
        if "/protection/" in target:
            return NOT_FOUND
        if target.endswith("/pulls/7"):
            return ok(json.dumps(self.rest_pr))
        if "/reviews" in target:
            return ok("".join(json.dumps(review) + "\n" for review in self.rest_reviews))
        if "/releases" in target:
            page = int(target.rsplit("page=", 1)[1])
            size = github.RELEASE_PAGE
            return ok(json.dumps(self.releases[(page - 1) * size : page * size]))
        if "/actions/runs" in target:
            if isinstance(self.runs, subprocess.CompletedProcess):
                return self.runs
            return ok("".join("\t".join((*run, "")[:4]) + "\n" for run in self.runs))
        if "/compare/" in target:
            tag = target.split("...", 1)[1]
            answer = self.compare.get(tag, NOT_FOUND)
            return answer if isinstance(answer, subprocess.CompletedProcess) else ok(f"{answer}\n")
        raise AssertionError(f"unexpected gh call: {args}")


def release(tag: str, published: str, body: str = "", draft: bool = False,
            prerelease: bool = False) -> dict:
    return {"tag_name": tag, "published_at": published, "body": body, "draft": draft,
            "prerelease": prerelease}


def rest_pr(state: str = "open", merged: bool = False, **fields: Any) -> dict:
    """The REST answer for PR 7, which the poller reads when GraphQL's budget is low."""
    return {
        "state": state,
        "merged": merged,
        "merged_at": MERGED_AT if merged else None,
        "closed_at": MERGED_AT if state == "closed" else None,
        "merge_commit_sha": MERGE_SHA,
        "mergeable_state": "clean",
        "head": {"sha": HEAD},
        "base": {"ref": "main", "sha": "e" * 40},
        "user": {"login": "me"},
        **fields,
    }


def rest_review(
    oid: str = HEAD, login: str = "greptile-apps[bot]", kind: str = "Bot", state: str = "COMMENTED",
    body: str = "", rid: str = "PRR_bot",
) -> dict:
    return {"node_id": rid, "state": state, "body": body, "commit_id": oid, "user": {"login": login, "type": kind}}


class PollTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.memo: dict = {}
        self.clock = 1000.0

    def poll(self, gh: FakeGh, until: str = "review_on_head", **cond: Any) -> dict:
        self.clock += 180
        result = github.poll(
            {"repo": REPO, "number": 7, "until": until, **cond}, self.memo, run=gh, now=self.clock
        )
        # Core stores the memo as JSON between polls.
        self.memo = json.loads(json.dumps(result["memo"]))
        return result


class ParsePrTest(unittest.TestCase):
    def test_accepts_url_and_short_form(self) -> None:
        self.assertEqual(github.parse_pr("https://github.com/acme/widget/pull/42"), (REPO, 42))
        self.assertEqual(github.parse_pr(" https://github.com/acme/widget/pull/42/files?x=1 "), (REPO, 42))
        self.assertEqual(github.parse_pr("acme/widget#42"), (REPO, 42))

    def test_rejects_the_rest(self) -> None:
        for text in (
            "42", "#42", "acme/widget", "https://github.com/acme/widget/issues/42", "acme/widget#0",
            "ö/ä#1", "../..#1", "./x#1", "x/..#1", "https://github.com/../x/pull/1",
        ):
            with self.subTest(text=text):
                self.assertIsNone(github.parse_pr(text))

    def test_repo_names_may_hold_dots_but_not_be_dots(self) -> None:
        self.assertIsNone(github.REPO_RE.fullmatch("a/.."))
        self.assertIsNotNone(github.REPO_RE.fullmatch("a.b/.github"))


class ReviewOnHeadTest(PollTestCase):
    def test_baseline_then_new_review_on_head_fires(self) -> None:
        gh = FakeGh(pr_answer(comments=[comment("C1", "someone")]))
        first = self.poll(gh)
        self.assertEqual(first["events"], [])
        self.assertFalse(first["fired"])
        self.assertEqual(first["next_poll_s"], 180)

        gh.pr = pr_answer(comments=[comment("C1", "someone")], reviews=[bot_review()], threads=[thread()])
        second = self.poll(gh)
        self.assertTrue(second["fired"])
        self.assertIn("greptile reviewed aaaaaaa", second["fire_text"])
        self.assertIn("1 open threads", second["fire_text"])
        self.assertNotIn(f"bot:greptile:{HEAD}", [event["key"] for event in second["events"]])

    def test_review_on_old_head_does_not_fire(self) -> None:
        gh = FakeGh(pr_answer(reviews=[bot_review(OLD_HEAD)]))
        self.poll(gh)
        result = self.poll(gh)
        self.assertFalse(result["fired"])
        self.assertIsNone(result["fire_text"])

    def test_review_already_on_head_fires_on_first_poll_without_events(self) -> None:
        result = self.poll(FakeGh(pr_answer(reviews=[bot_review()])))
        self.assertTrue(result["fired"])
        self.assertEqual(result["events"], [])

    def test_push_after_review_needs_a_new_review(self) -> None:
        gh = FakeGh(pr_answer(reviews=[bot_review(OLD_HEAD)], head=OLD_HEAD))
        self.assertTrue(self.poll(gh)["fired"])
        gh.pr = pr_answer(reviews=[bot_review(OLD_HEAD)], head=NEW_HEAD)
        result = self.poll(gh)
        self.assertFalse(result["fired"])
        self.assertEqual([event["key"] for event in result["events"]], [f"head:{NEW_HEAD}"])

    def test_custom_bots_replace_the_default(self) -> None:
        gh = FakeGh(pr_answer(reviews=[bot_review(login="otherbot")]))
        self.assertFalse(self.poll(gh)["fired"])
        self.assertTrue(self.poll(gh, bots=["otherbot"])["fired"])
        self.assertTrue(self.poll(gh, bots="otherbot, another")["fired"])

    def test_bot_with_checks_is_done_when_they_finish(self) -> None:
        gh = FakeGh(pr_answer(checks=[check("Macroscope - Correctness", status="IN_PROGRESS", conclusion=None)]))
        self.assertFalse(self.poll(gh)["fired"])
        gh.pr = pr_answer(checks=[check("Macroscope - Correctness")])
        self.assertTrue(self.poll(gh)["fired"])

    def test_summary_naming_the_head_counts(self) -> None:
        summary = comment("C9", "macroscopeapp", "**Verdict:** Approved at `aaaaaaa`", "Bot")
        self.assertTrue(self.poll(FakeGh(pr_answer(comments=[summary])))["fired"])

    def test_failed_bot_check_without_a_review_is_not_a_review(self) -> None:
        gh = FakeGh(pr_answer(checks=[check("Greptile Review", "FAILURE")]))
        self.assertFalse(self.poll(gh)["fired"])
        gh.pr = pr_answer(checks=[check("Greptile Review", "CANCELLED")])
        self.assertFalse(self.poll(gh)["fired"])

    def test_failed_bot_check_with_a_review_on_the_head_is_done(self) -> None:
        gh = FakeGh(pr_answer(checks=[check("Greptile Review", "FAILURE")], reviews=[bot_review()]))
        self.assertTrue(self.poll(gh)["fired"])

    def test_verdict_of_an_older_head_is_not_reported(self) -> None:
        stale = comment("C9", "greptile-apps", "**Verdict:** Approved at `bbbbbbb`", "Bot")
        gh = FakeGh(pr_answer(comments=[stale]))
        self.poll(gh, until="merged")
        gh.pr = pr_answer(comments=[stale], reviews=[bot_review()])
        self.assertEqual(
            self.poll(gh, until="merged")["events"][0]["text"], "greptile finished on aaaaaaa"
        )

    def test_every_default_bot_counts_by_its_real_login(self) -> None:
        logins = (
            "coderabbitai",
            "chatgpt-codex-connector",
            "copilot-pull-request-reviewer",
            "gemini-code-assist",
            "cursor",
            "sourcery-ai",
            "roboomp",
        )
        for login in logins:
            self.memo = {}
            self.assertTrue(self.poll(FakeGh(pr_answer(reviews=[bot_review(login=login)])))["fired"], login)

    def test_review_on_old_head_does_not_block_another_bots_review(self) -> None:
        reviews = [
            bot_review(OLD_HEAD, "copilot-pull-request-reviewer", "PRR_c"),
            bot_review(HEAD, "greptile-apps", "PRR_g"),
        ]
        result = self.poll(FakeGh(pr_answer(reviews=reviews)))
        self.assertTrue(result["fired"])
        self.assertIn("greptile", result["fire_text"])
        self.assertNotIn("copilot", result["fire_text"])

    def test_gating_bots_pending_check_blocks_then_all_reviewers_are_listed(self) -> None:
        reviews = [bot_review()]
        pending = check("CodeRabbit", conclusion=None, status="IN_PROGRESS")
        gh = FakeGh(pr_answer(reviews=reviews, checks=[pending]))
        self.assertFalse(self.poll(gh)["fired"])
        gh.pr = pr_answer(reviews=reviews, checks=[check("CodeRabbit")])
        result = self.poll(gh)
        self.assertTrue(result["fired"])
        self.assertIn("greptile, coderabbit reviewed", result["fire_text"])

    def test_pr_author_is_not_a_bot(self) -> None:
        review = bot_review(login="Copilot")
        summary = comment("C9", "Copilot", "Finished work on `aaaaaaa`", "Bot")
        gh = FakeGh(pr_answer(author="Copilot", reviews=[review], comments=[summary]))
        self.assertFalse(self.poll(gh)["fired"])
        gh.pr = pr_answer(
            author="Copilot",
            reviews=[review, bot_review(login="copilot-pull-request-reviewer", rid="PRR_c")],
        )
        self.assertTrue(self.poll(gh)["fired"])

    def test_codex_clean_receipt_names_the_head(self) -> None:
        receipt = (
            "<!-- codex-review-receipt:v1 -->\n\nCodex Review: Didn't find any major issues.\n\n"
            f"**Reviewed commit:** `{HEAD[:10]}`"
        )
        gh = FakeGh(pr_answer(comments=[comment("C9", "chatgpt-codex-connector", receipt, "Bot")]))
        self.assertTrue(self.poll(gh)["fired"])

    def test_codex_summary_counts_only_when_completed(self) -> None:
        state = '<!-- codex-review-state:v1 {"code_review": {"commit_sha": "%s", "status": "%s"}} -->'
        summary = comment("C9", "chatgpt-codex-connector", state % (HEAD, "failed"), "Bot")
        gh = FakeGh(pr_answer(comments=[summary]))
        self.assertFalse(self.poll(gh)["fired"])
        summary = comment("C9", "chatgpt-codex-connector", state % (HEAD, "completed"), "Bot")
        gh.pr = pr_answer(comments=[summary])
        self.assertTrue(self.poll(gh)["fired"])

    def test_ci_job_named_after_a_bot_is_not_that_bot(self) -> None:
        running = check("codex-rs tests", None, status="IN_PROGRESS", app="github-actions")
        gh = FakeGh(pr_answer(reviews=[bot_review()], checks=[running]))
        self.assertTrue(self.poll(gh)["fired"])
        self.memo = {}
        green = check("codex-rs tests", app="github-actions")
        self.assertFalse(self.poll(FakeGh(pr_answer(checks=[green])))["fired"])

    def test_ci_job_named_after_a_bot_still_gates_ready(self) -> None:
        failed = check("cursor-adapter", "FAILURE", app="github-actions")
        gh = FakeGh(pr_answer(reviews=[bot_review()], checks=[failed]))
        self.assertFalse(self.poll(gh, until="ready")["fired"])

    def test_authors_own_check_is_not_a_bot_check(self) -> None:
        running = check("Copilot", None, status="IN_PROGRESS", app="copilot-swe-agent")
        gh = FakeGh(pr_answer(author="copilot-swe-agent", reviews=[bot_review()], checks=[running]))
        self.assertTrue(self.poll(gh)["fired"])

    def test_person_whose_login_contains_a_bot_name_is_a_person(self) -> None:
        review = bot_review(login="cursorfan")
        review["author"]["__typename"] = "User"
        self.assertFalse(self.poll(FakeGh(pr_answer(reviews=[review])))["fired"])
        self.memo = {}
        gh = FakeGh()
        self.poll(gh, until="merged")
        gh.pr = pr_answer(comments=[comment("C2", "geminidev", "another")])
        self.assertEqual(
            [event["text"] for event in self.poll(gh, until="merged")["events"]],
            ["comment from geminidev: another"],
        )

    def test_roboomp_posts_as_a_user(self) -> None:
        review = bot_review(login="roboomp")
        review["author"]["__typename"] = "User"
        self.assertTrue(self.poll(FakeGh(pr_answer(reviews=[review])))["fired"])

    def test_merged_pr_reads_no_branch_rules(self) -> None:
        gh = FakeGh(pr_answer(state="MERGED", reviews=[bot_review()]))
        self.poll(gh)
        self.assertEqual(gh.count("/rules/"), 0)

    def test_copilot_that_could_not_run_did_not_review(self) -> None:
        review = bot_review(login="copilot-pull-request-reviewer")
        review["body"] = "Copilot was unable to review this pull request because ..."
        self.assertFalse(self.poll(FakeGh(pr_answer(reviews=[review])))["fired"])

    def test_pending_bot_check_blocks_on_a_merged_pr_too(self) -> None:
        pending = check("CodeRabbit", None, status="IN_PROGRESS", app="coderabbitai")
        gh = FakeGh(pr_answer(state="MERGED", reviews=[bot_review()], checks=[pending]))
        self.assertFalse(self.poll(gh)["fired"])


class EventsTest(PollTestCase):
    def test_new_comment_reports_once_across_polls(self) -> None:
        gh = FakeGh()
        self.poll(gh)
        gh.pr = pr_answer(comments=[comment("C1", "someone", "<!-- x --> please <b>look</b>")])
        first = self.poll(gh)
        self.assertEqual(
            first["events"],
            [{"key": "c:C1", "text": "comment from someone: please look", "on_me": False}],
        )
        self.assertEqual(self.poll(gh)["events"], [])

    def test_own_status_and_gating_bot_comments_are_skipped(self) -> None:
        gh = FakeGh()
        self.poll(gh)
        gh.pr = pr_answer(
            comments=[
                comment("C1", "me"),
                comment("C2", "github-actions", typename="Bot"),
                comment("C3", "vercel", typename="Bot"),
                comment("C4", "greptile-apps", typename="Bot"),
            ]
        )
        self.assertEqual(self.poll(gh)["events"], [])

    def test_human_review_and_thread_events(self) -> None:
        gh = FakeGh()
        self.poll(gh)
        review = {
            "id": "R1",
            "state": "CHANGES_REQUESTED",
            "body": "no",
            "commit": {"oid": HEAD},
            "author": {"login": "alice", "__typename": "User"},
        }
        gh.pr = pr_answer(
            reviews=[review],
            threads=[thread(comments=2, author="alice"), thread("T2", author="bob", line=None)],
        )
        texts = [event["text"] for event in self.poll(gh)["events"]]
        self.assertEqual(
            texts,
            [
                "review from alice (changes requested): no",
                "new thread from alice on src/a.py:12: nit",
                "thread reply from alice on src/a.py:12: nit",
                "new thread from bob on src/a.py: nit",
            ],
        )

    def test_review_bot_findings_count_in_the_bot_event_only(self) -> None:
        gh = FakeGh()
        self.poll(gh, until="merged")
        gh.pr = pr_answer(reviews=[bot_review()], threads=[thread("T1"), thread("T2")])
        self.assertEqual(
            [event["text"] for event in self.poll(gh, until="merged")["events"]],
            ["greptile finished on aaaaaaa: 2 open threads"],
        )

    def test_new_bot_feedback_on_a_reported_head_is_not_lost(self) -> None:
        gh = FakeGh(pr_answer(reviews=[bot_review()]))
        self.assertEqual(self.poll(gh, until="merged")["events"], [])

        gh.pr = pr_answer(reviews=[bot_review()], threads=[thread("T1", comments=2), thread("T2")])
        first = self.poll(gh, until="merged")
        self.assertEqual(
            first["events"],
            [
                {
                    "key": "bot-comments:c:T1-0",
                    "text": "new greptile review comments on aaaaaaa: 3",
                    "on_me": False,
                }
            ],
        )
        self.assertEqual(self.poll(gh, until="merged")["events"], [])

        gh.pr = pr_answer(reviews=[bot_review()], threads=[thread("T1", comments=3), thread("T2")])
        self.assertEqual(
            [event["text"] for event in self.poll(gh, until="merged")["events"]],
            ["new greptile review comments on aaaaaaa: 1"],
        )

    def test_bot_comments_at_baseline_stay_silent(self) -> None:
        gh = FakeGh(pr_answer(reviews=[bot_review()], threads=[thread()]))
        self.poll(gh, until="merged")
        self.assertEqual(self.poll(gh, until="merged")["events"], [])

    def test_failed_status_context_counts_as_a_failed_check(self) -> None:
        gh = FakeGh()
        self.poll(gh)
        status = {"context": "ci/legacy", "state": "ERROR", "isRequired": True}
        gh.pr = pr_answer(checks=[status])
        self.assertEqual(self.poll(gh)["events"][0]["key"], f"fail:{HEAD}:ci/legacy")

    def test_failed_required_check_reports_once_per_sha(self) -> None:
        gh = FakeGh(pr_answer(checks=[check("ci", required=True)]))
        self.poll(gh)
        gh.pr = pr_answer(checks=[check("ci", "FAILURE", required=True), check("lint", "FAILURE")])
        first = self.poll(gh)
        self.assertEqual(
            first["events"],
            [{"key": f"fail:{HEAD}:ci", "text": "required check failed: ci", "on_me": True}],
        )
        self.assertEqual(self.poll(gh)["events"], [])

        gh.pr = pr_answer(head=NEW_HEAD, checks=[check("ci", "FAILURE", required=True)])
        keys = [event["key"] for event in self.poll(gh)["events"]]
        self.assertIn(f"fail:{NEW_HEAD}:ci", keys)

    def test_every_ci_check_gates_when_none_is_required(self) -> None:
        gh = FakeGh()
        self.poll(gh)
        gh.pr = pr_answer(checks=[check("lint", "TIMED_OUT"), check("Greptile Review", "FAILURE")])
        self.assertEqual(
            self.poll(gh)["events"][-1],
            {"key": f"fail:{HEAD}:lint", "text": "check failed: lint", "on_me": True},
        )

    def test_failure_at_baseline_is_reported_once(self) -> None:
        # A state that needs you: a wait created on a red PR says why it can't fire.
        gh = FakeGh(pr_answer(checks=[check("ci", "FAILURE", required=True)], merge_state="DIRTY"))
        self.assertEqual(
            [event["key"] for event in self.poll(gh)["events"]],
            [f"fail:{HEAD}:ci", f"merge:DIRTY:{HEAD}:{'e' * 40}"],
        )
        self.assertEqual(self.poll(gh)["events"], [])

    def test_duplicate_check_names_take_the_worst_result(self) -> None:
        gh = FakeGh()
        self.poll(gh)
        gh.pr = pr_answer(
            checks=[check("ci", "FAILURE", required=True), check("ci", "SUCCESS", required=True)]
        )
        self.assertEqual(len(self.poll(gh)["events"]), 1)

    def test_conflict_that_survives_a_push_is_reported_again(self) -> None:
        gh = FakeGh(pr_answer(merge_state="DIRTY"))
        self.assertEqual(len(self.poll(gh)["events"]), 1)
        gh.pr = pr_answer(head=NEW_HEAD, merge_state="DIRTY")
        self.assertIn(
            f"merge:DIRTY:{NEW_HEAD}:{'e' * 40}", [event["key"] for event in self.poll(gh)["events"]]
        )

    def test_behind_and_dirty_report_once_each(self) -> None:
        gh = FakeGh()
        self.poll(gh)
        gh.pr = pr_answer(merge_state="BEHIND")
        behind = self.poll(gh)
        self.assertEqual(behind["events"][0]["text"], "behind main, update the branch")
        self.assertTrue(behind["events"][0]["on_me"])
        self.assertEqual(self.poll(gh)["events"], [])
        gh.pr = pr_answer(merge_state="DIRTY")
        self.assertEqual(self.poll(gh)["events"][0]["text"], "conflicts with main, rebase it")
        gh.pr = pr_answer(merge_state="UNKNOWN")
        self.assertEqual(self.poll(gh)["events"], [])

    def test_closed_without_merge_reports_even_on_the_first_poll(self) -> None:
        gh = FakeGh(pr_answer(state="CLOSED"))
        result = self.poll(gh, until="merged")
        self.assertFalse(result["fired"])
        self.assertEqual(
            result["events"],
            [{"key": f"closed:{MERGED_AT}", "text": "PR#7 closed without merging", "on_me": True}],
        )
        self.assertEqual(result["next_poll_s"], 3600)
        self.assertEqual(self.poll(gh, until="merged")["events"], [])

    def test_reopened_after_a_first_poll_on_a_closed_pr_keeps_the_baseline(self) -> None:
        old = [comment("C1", "someone")]
        gh = FakeGh(pr_answer(state="CLOSED", comments=old))
        self.poll(gh, until="merged")
        gh.pr = pr_answer(comments=[*old, comment("C2", "someone", "new")])
        self.assertEqual(
            [event["key"] for event in self.poll(gh, until="merged")["events"]], ["c:C2"]
        )


class SeenCapTest(PollTestCase):
    def test_keeps_the_newest_keys(self) -> None:
        old = [f"old:{index}" for index in range(github.SEEN_KEPT + 100)]
        self.memo = {"seen": old, "baselined": True, "rest_baselined": True}
        gh = FakeGh(pr_answer(comments=[comment("C1", "someone")]))
        self.assertEqual([event["key"] for event in self.poll(gh)["events"]], [f"head:{HEAD}", "c:C1"])
        seen = self.memo["seen"]
        self.assertEqual(len(seen), github.SEEN_KEPT)
        self.assertEqual(seen[-2:], [f"head:{HEAD}", "c:C1"])
        self.assertEqual(seen[0], "old:102")

    def test_a_key_met_again_is_newest(self) -> None:
        old = [f"old:{index}" for index in range(github.SEEN_KEPT)]
        self.memo = {"seen": ["c:C1", *old], "baselined": True, "rest_baselined": True}
        gh = FakeGh(pr_answer(comments=[comment("C1", "someone")]))
        self.assertEqual([event["key"] for event in self.poll(gh)["events"]], [f"head:{HEAD}"])
        self.assertIn("c:C1", self.memo["seen"])
        self.assertNotIn("old:0", self.memo["seen"])

    def test_more_talk_than_the_cap_is_not_reported_again(self) -> None:
        many = github.SEEN_KEPT + 100
        gh = FakeGh(pr_answer(threads=[thread("T1", comments=many, author="someone")]))
        self.poll(gh)
        gh.pr = pr_answer(threads=[thread("T1", comments=many + 1, author="someone")])
        self.assertEqual([event["key"] for event in self.poll(gh)["events"]], [f"c:T1-{many}"])
        self.assertEqual(self.poll(gh)["events"], [])

    def test_more_bot_findings_than_the_cap_are_not_reported_again(self) -> None:
        threads = [thread("T1", comments=github.SEEN_KEPT + 100)]
        gh = FakeGh(pr_answer(reviews=[bot_review()], threads=threads))
        self.poll(gh, until="merged")
        self.assertEqual(self.poll(gh, until="merged")["events"], [])
        self.assertEqual(self.poll(gh, until="merged")["events"], [])

    def test_a_failed_poll_trims_nothing(self) -> None:
        old = [f"old:{index}" for index in range(github.SEEN_KEPT + 100)]
        self.memo = {"seen": old, "baselined": True, "rest_baselined": True}
        self.poll(FakeGh(ok(code=1, stderr="gh: Bad Gateway (HTTP 502)\n")))
        self.assertEqual(self.memo["seen"], old)


class ReadyTest(PollTestCase):
    def assert_ready(self, gh: FakeGh, ready: bool) -> dict:
        result = self.poll(gh, until="ready")
        self.assertEqual(result["fired"], ready, result)
        return result

    def first_polls(self, ready: bool, cases: dict[str, FakeGh]) -> None:
        """Each case as a new wait's first poll."""
        for name, gh in cases.items():
            with self.subTest(name):
                self.memo, self.clock = {}, 1000.0
                self.assert_ready(gh, ready)

    def test_green_reviewed_and_no_threads_fires(self) -> None:
        gh = FakeGh(
            pr_answer(
                reviews=[bot_review()],
                checks=[check("ci", required=True), check("skip", "SKIPPED", required=True)],
                threads=[thread(resolved=True)],
            )
        )
        result = self.assert_ready(gh, True)
        self.assertEqual(
            result["fire_text"], "ready on aaaaaaa: 2 checks green, greptile reviewed, no open threads"
        )

    def test_one_unmet_condition_blocks(self) -> None:
        reviewed = [bot_review()]
        self.first_polls(False, {
            "an open thread": FakeGh(pr_answer(reviews=reviewed, threads=[thread()])),
            "an open thread on an older page": FakeGh(
                pr_answer(reviews=reviewed, threads=[thread(resolved=True)], older_cursor="c1"),
                older_threads={"c1": threads_page([thread("T0")])},
            ),
            "a pending required check": FakeGh(
                pr_answer(reviews=reviewed, checks=[check("ci", conclusion=None, status="QUEUED", required=True)])
            ),
            "a required check that has not started": FakeGh(
                pr_answer(reviews=reviewed, checks=[check("other")]), rules=["ci"]
            ),
            "a gating bot's pending check": FakeGh(pr_answer(
                reviews=reviewed,
                checks=[check("ci", required=True), check("Cursor Bugbot", conclusion=None, status="IN_PROGRESS")],
            )),
            "behind the base": FakeGh(pr_answer(reviews=reviewed, merge_state="BEHIND")),
            "merged, with a review on its head": FakeGh(pr_answer(state="MERGED", reviews=reviewed)),
            "a workflow still running, no required checks": FakeGh(
                pr_answer(reviews=reviewed), runs=[("CI", "queued", "")]
            ),
        })

    def test_what_does_not_block(self) -> None:
        reviewed = [bot_review()]
        self.first_polls(True, {
            "a failed optional check": FakeGh(
                pr_answer(reviews=reviewed, checks=[check("ci", required=True), check("lint", "FAILURE")])
            ),
            "a run cancelled for a newer one": FakeGh(pr_answer(reviews=reviewed), runs=[
                ("CI", "completed", "success", "1/pull_request"), ("CI", "completed", "cancelled", "1/pull_request"),
            ]),
            # The rollup is then in charge.
            "unreadable workflow runs": FakeGh(pr_answer(reviews=reviewed), runs=NOT_FOUND),
        })

    def test_unknown_required_checks_never_read_as_none(self) -> None:
        gh = FakeGh(pr_answer(reviews=[bot_review()]))
        gh.rules = ok(code=1, stderr="gh: Bad Gateway (HTTP 502)\n")
        result = self.assert_ready(gh, False)
        self.assertTrue(result["transient"])
        self.assertEqual(result["events"], [])

    def test_head_without_a_bot_gets_the_settle_time(self) -> None:
        gh = FakeGh()
        self.assert_ready(gh, False)  # head first seen at 1180
        self.clock = 1180 + github.BOT_SETTLE_S - 180
        result = self.assert_ready(gh, True)
        self.assertEqual(
            result["fire_text"], "ready on aaaaaaa: no checks, no bot reviewed aaaaaaa in 15 min, no open threads"
        )

    def test_review_on_an_earlier_head_gets_the_settle_time_and_is_named(self) -> None:
        review = bot_review(OLD_HEAD, "copilot-pull-request-reviewer")
        gh = FakeGh(pr_answer(reviews=[review]))
        self.assert_ready(gh, False)
        self.clock = 1180 + github.BOT_SETTLE_S - 180
        result = self.assert_ready(gh, True)
        self.assertIn("no bot reviewed aaaaaaa in 15 min (copilot reviewed an earlier head)", result["fire_text"])

    def test_settle_time_runs_from_the_push_not_the_commit(self) -> None:
        # The poller can't see when a commit was pushed, only when it first saw it.
        gh = FakeGh()
        self.assert_ready(gh, False)
        self.clock += github.BOT_SETTLE_S
        gh.pr = pr_answer(head=NEW_HEAD)
        self.assert_ready(gh, False)
        self.clock += github.BOT_SETTLE_S - 180
        self.assert_ready(gh, True)

    def test_check_past_the_first_hundred_still_gates(self) -> None:
        gh = FakeGh(pr_answer(reviews=[bot_review()], checks=[check("ci")], more_checks="p2"))
        gh.more_checks = {"p2": [check("slow", "FAILURE")]}
        result = self.assert_ready(gh, False)
        self.assertIn(f"fail:{HEAD}:slow", [event["key"] for event in result["events"]])

    def test_settle_time_does_not_apply_to_review_on_head(self) -> None:
        gh = FakeGh()
        self.poll(gh)
        self.clock += github.BOT_SETTLE_S
        self.assertFalse(self.poll(gh)["fired"])

    def test_check_pending_over_an_hour_is_reported_once_per_head(self) -> None:
        pending = check("CodeRabbit", None, status="IN_PROGRESS", app="coderabbitai")
        gh = FakeGh(pr_answer(checks=[pending]))
        self.assertEqual(self.assert_ready(gh, False)["events"], [])
        self.clock += github.BOT_STUCK_S
        expected = {
            "key": f"stuck:{HEAD}:CodeRabbit",
            "text": "CodeRabbit pending for over an hour",
            "on_me": True,
        }
        self.assertEqual(self.assert_ready(gh, False)["events"], [expected])
        self.assertEqual(self.assert_ready(gh, False)["events"], [])

        gh.pr = pr_answer(head=NEW_HEAD, checks=[pending])
        self.assertEqual(
            [event["key"] for event in self.assert_ready(gh, False)["events"]], [f"head:{NEW_HEAD}"]
        )

    def test_poll_with_an_empty_memo_fires_on_a_settled_head(self) -> None:
        # The core verifies a `pr` postcondition this way.
        gh = FakeGh(pr_answer(reviews=[bot_review()]))
        result = github.poll({"repo": REPO, "number": 7, "until": "ready"}, {}, run=gh, now=9999.0)
        self.assertTrue(result["fired"])

    def test_coderabbit_satisfies_the_review(self) -> None:
        result = self.assert_ready(FakeGh(pr_answer(reviews=[bot_review(login="coderabbitai")])), True)
        self.assertIn("coderabbit reviewed", result["fire_text"])

    def test_draft_blocks_and_says_so_once_per_head(self) -> None:
        gh = FakeGh(pr_answer(merge_state="DRAFT", reviews=[bot_review()]))
        result = self.assert_ready(gh, False)
        self.assertEqual([event["key"] for event in result["events"]], [f"draft:{HEAD}"])
        self.assertEqual(self.assert_ready(gh, False)["events"], [])

    def test_merged_before_ready_says_so_and_does_not_fire(self) -> None:
        gh = FakeGh(pr_answer(state="MERGED", reviews=[bot_review(OLD_HEAD)]))
        result = self.assert_ready(gh, False)
        self.assertTrue(result["merged"])
        self.assertEqual(result["events"], [])

    def test_ci_waiting_for_a_maintainers_approval_blocks_and_says_so_once(self) -> None:
        # A fork's CI: no checks in the rollup until a maintainer approves the run.
        gh = FakeGh(pr_answer(reviews=[bot_review()]))
        gh.runs = [("CI", "completed", "action_required")]
        result = self.assert_ready(gh, False)
        self.assertEqual(
            [event["text"] for event in result["events"]],
            ["CI on aaaaaaa waits for a maintainer's approval (CI)"],
        )
        self.assertEqual(self.assert_ready(gh, False)["events"], [])
        gh.runs = [("CI", "completed", "success")]
        self.assert_ready(gh, True)

    def test_a_failed_ci_run_blocks_and_needs_me(self) -> None:
        # Beside green checks, or failed before any job reached the rollup.
        for checks in ([check("lint")], []):
            with self.subTest(checks=checks):
                self.memo, self.clock = {}, 1000.0
                gh = FakeGh(pr_answer(reviews=[bot_review()], checks=checks), runs=[("CI", "completed", "failure")])
                result = self.assert_ready(gh, False)
                self.assertEqual(
                    [(event["text"], event["on_me"]) for event in result["events"]],
                    [("CI failed on aaaaaaa (CI)", True)],
                )

    def test_a_once_bot_on_the_head_does_not_stand_in_for_an_every_push_bot(self) -> None:
        reviews = [bot_review(OLD_HEAD, "roboomp", "PRR_old"), bot_review(login="copilot-pull-request-reviewer")]
        gh = FakeGh(pr_answer(reviews=reviews))
        self.assert_ready(gh, False)
        self.clock = 1180 + github.BOT_SETTLE_S
        self.assert_ready(gh, False)
        gh.pr = pr_answer(reviews=[*reviews, bot_review(login="roboomp", rid="PRR_new")])
        self.assertIn("reviewed", self.assert_ready(gh, True)["fire_text"])

    def test_a_bot_that_reviews_every_push_is_waited_for_and_reported_after_an_hour(self) -> None:
        review = bot_review(OLD_HEAD, "roboomp")
        gh = FakeGh(pr_answer(reviews=[review]))
        self.assert_ready(gh, False)  # head first seen at 1180
        self.clock = 1180 + github.BOT_SETTLE_S
        self.assertEqual(self.assert_ready(gh, False)["events"], [])
        self.clock = 1180 + github.BOT_STUCK_S - 179
        self.assertEqual(
            [(event["text"], event["on_me"]) for event in self.assert_ready(gh, False)["events"]],
            [("roboomp hasn't reviewed aaaaaaa in an hour", True)],
        )
        self.assertEqual(self.assert_ready(gh, False)["events"], [])


class MergedTest(PollTestCase):
    def test_open_then_merged(self) -> None:
        gh = FakeGh()
        self.assertFalse(self.poll(gh, until="merged")["fired"])
        gh.pr = pr_answer(state="MERGED")
        result = self.poll(gh, until="merged")
        self.assertTrue(result["fired"])
        self.assertEqual(result["fire_text"], "PR#7 merged")
        self.assertTrue(result["merged"])

    def test_merged_at_first_poll_fires(self) -> None:
        self.assertTrue(self.poll(FakeGh(pr_answer(state="MERGED")), until="merged")["fired"])

    def test_every_wait_says_the_pr_is_merged(self) -> None:
        for until in github.UNTILS:
            with self.subTest(until=until):
                self.memo = {}
                self.assertTrue(self.poll(FakeGh(pr_answer(state="MERGED")), until=until)["merged"])

    def test_a_release_wait_says_so_on_later_polls_too(self) -> None:
        gh = FakeGh(pr_answer(state="MERGED"))
        self.poll(gh, until="released")
        self.assertTrue(self.poll(gh, until="released")["merged"])

    def test_an_open_or_closed_pr_is_not(self) -> None:
        self.assertFalse(self.poll(FakeGh())["merged"])
        self.assertFalse(self.poll(FakeGh(pr_answer(state="CLOSED")))["merged"])


class ActivityTest(PollTestCase):
    def test_existing_talk_never_fires_and_a_new_comment_does_once(self) -> None:
        old = [comment("C1", "someone")]
        gh = FakeGh(pr_answer(comments=old))
        self.assertFalse(self.poll(gh, until="activity")["fired"])
        self.assertFalse(self.poll(gh, until="activity")["fired"])
        gh.pr = pr_answer(comments=[*old, comment("C2", "maintainer", "needs a test")])
        result = self.poll(gh, until="activity")
        self.assertTrue(result["fired"])
        self.assertEqual(result["fire_text"], "comment from maintainer: needs a test")
        self.assertEqual(result["events"], [])
        self.assertFalse(self.poll(gh, until="activity")["fired"])

    def test_your_own_comments_and_pushes_do_not_count(self) -> None:
        gh = FakeGh()
        self.poll(gh, until="activity")
        gh.pr = pr_answer(head="b" * 40, comments=[comment("C1", "me", "pushed a fix")])
        result = self.poll(gh, until="activity")
        self.assertFalse(result["fired"])
        self.assertEqual([event["key"] for event in result["events"]], [f"head:{'b' * 40}"])

    def test_another_authors_comment_and_push_count(self) -> None:
        gh = FakeGh(pr_answer(author="someone"))
        self.poll(gh, until="activity")
        gh.pr = pr_answer(author="someone", head="b" * 40, comments=[comment("C1", "someone", "rebased")])
        result = self.poll(gh, until="activity")
        self.assertTrue(result["fired"])
        self.assertEqual(result["fire_text"], "2 new, first new head bbbbbbb")

    def test_a_bot_review_counts(self) -> None:
        gh = FakeGh()
        self.poll(gh, until="activity")
        gh.pr = pr_answer(reviews=[bot_review()])
        self.assertTrue(self.poll(gh, until="activity")["fired"])

    def test_merged_fires(self) -> None:
        gh = FakeGh()
        self.poll(gh, until="activity")
        gh.pr = pr_answer(state="MERGED")
        result = self.poll(gh, until="activity")
        self.assertTrue(result["fired"])
        self.assertEqual(result["fire_text"], "PR#7 merged")

    def test_a_gating_bots_user_account_review_counts_once(self) -> None:
        review = bot_review(login="roboomp")
        review["author"]["__typename"] = "User"
        review["body"] = "found two issues"
        gh = FakeGh()
        self.poll(gh, until="activity")
        gh.pr = pr_answer(reviews=[review])
        result = self.poll(gh, until="activity")
        self.assertEqual(result["fire_text"], "roboomp finished on aaaaaaa")

    def test_closed_fires_even_on_the_first_poll(self) -> None:
        result = self.poll(FakeGh(pr_answer(state="CLOSED")), until="activity")
        self.assertTrue(result["fired"])
        self.assertEqual(result["fire_text"], "PR#7 closed without merging")

    def test_a_comment_before_the_graphql_baseline_is_reported(self) -> None:
        gh = FakeGh(remaining=100, rest_pr=rest_pr())
        self.assertFalse(self.poll(gh, until="activity")["fired"])
        gh.remaining = 4000
        self.clock += github.RATE_CHECK_S
        gh.pr = pr_answer(
            comments=[
                comment("C1", "old", created="1970-01-01T00:00:00Z"),
                comment("C2", "maintainer", "needs a test", created="2026-11-17T10:00:00Z"),
            ]
        )
        result = self.poll(gh, until="activity")
        self.assertTrue(result["fired"])
        self.assertEqual(result["fire_text"], "comment from maintainer: needs a test")


class ReleasedTest(PollTestCase):
    def merged(self, *releases: dict, compare: dict | None = None, **pr: Any) -> FakeGh:
        """A merged PR beside its repo's releases, newest first; compare: tag -> status."""
        return FakeGh(pr_answer(state="MERGED", **pr), releases=list(releases), compare=compare or {})

    def test_open_pr_does_not_look_at_releases(self) -> None:
        gh = FakeGh()
        self.assertFalse(self.poll(gh, until="released")["fired"])
        self.assertEqual(gh.count("/releases"), 0)

    def test_compare_ahead_fires(self) -> None:
        gh = self.merged(
            release("v2", "2026-11-03T00:00:00Z"), release("v1", "2026-11-02T00:00:00Z"),
            compare={"v1": "ahead", "v2": "ahead"},
        )
        result = self.poll(gh, until="released")
        self.assertTrue(result["fired"])
        self.assertFalse(result["confirm"])
        self.assertEqual(result["fire_text"], "PR#7 released in v1")
        self.assertEqual(gh.count(f"compare/{MERGE_SHA}...v1"), 1)
        self.assertEqual(gh.count(f"compare/{MERGE_SHA}...v2"), 0)

    def test_identical_fires(self) -> None:
        gh = self.merged(release("v1", "2026-11-02T00:00:00Z"), compare={"v1": "identical"})
        self.assertTrue(self.poll(gh, until="released")["fired"])

    def test_diverged_with_notes_reference_asks_to_confirm(self) -> None:
        gh = self.merged(release("v1.1", "2026-11-02T00:00:00Z", "Fixes (#7), thanks"), compare={"v1.1": "diverged"})
        first = self.poll(gh, until="released")
        self.assertFalse(first["fired"])
        self.assertTrue(first["confirm"])
        self.assertEqual(first["events"][0]["key"], "confirm:v1.1")
        self.assertTrue(first["events"][0]["on_me"])

        second = self.poll(gh, until="released")
        self.assertTrue(second["confirm"])
        self.assertEqual(second["events"], [])
        self.assertEqual(gh.count("compare/"), 1)
        self.assertEqual(gh.count("query="), 1)

    def test_diverged_without_reference_keeps_waiting(self) -> None:
        gh = self.merged(release("v1", "2026-11-02T00:00:00Z", "See #70 and other/repo#7"), compare={"v1": "behind"})
        result = self.poll(gh, until="released")
        self.assertFalse(result["fired"])
        self.assertFalse(result["confirm"])
        self.assertEqual(result["events"], [])

    def test_later_release_containing_the_merge_wins_over_a_reference(self) -> None:
        gh = self.merged(
            release("v2", "2026-11-03T00:00:00Z"), release("v1.1", "2026-11-02T00:00:00Z", "backport of #7"),
            compare={"v1.1": "diverged", "v2": "ahead"},
        )
        result = self.poll(gh, until="released")
        self.assertTrue(result["fired"])
        self.assertFalse(result["confirm"])

    def test_waiting_for_a_release_polls_slowly(self) -> None:
        self.assertEqual(self.poll(self.merged(), until="released")["next_poll_s"], github.RELEASE_POLL_S)

    def test_release_past_the_first_page_is_found(self) -> None:
        maintenance = [
            release(f"maint-{i}", f"2026-11-20T00:{i // 60:02d}:{i % 60:02d}Z")
            for i in range(github.RELEASE_PAGE)
        ]
        gh = self.merged(
            *maintenance, release("v1", "2026-11-02T00:00:00Z"),
            compare={"v1": "ahead", **{item["tag_name"]: "diverged" for item in maintenance}},
        )
        result = self.poll(gh, until="released")
        self.assertEqual(result["fire_text"], "PR#7 released in v1")
        self.assertEqual(gh.count("page=2"), 1)

    def test_paging_stops_at_the_first_release_older_than_the_merge(self) -> None:
        gh = self.merged(*(release(f"old-{i}", "2026-10-01T00:00:00Z") for i in range(github.RELEASE_PAGE + 1)))
        self.poll(gh, until="released")
        self.assertEqual(gh.count("/releases"), 1)

    def test_only_a_link_to_this_repo_is_a_reference(self) -> None:
        for link, confirm in (
            ("https://github.com/other/project/pull/7", False),
            ("https://github.com/acme/widget/pull/7", True),
        ):
            with self.subTest(link=link):
                self.memo = {}
                gh = self.merged(release("v1", "2026-11-02T00:00:00Z", link), compare={"v1": "diverged"})
                self.assertEqual(self.poll(gh, until="released")["confirm"], confirm)

    def test_old_and_draft_releases_are_skipped(self) -> None:
        gh = self.merged(
            release("draft", "2026-11-02T00:00:00Z", draft=True), release("old", "2026-10-01T00:00:00Z"),
        )
        self.assertFalse(self.poll(gh, until="released")["fired"])
        self.assertEqual(gh.count("compare/"), 0)

    def test_tag_missing_from_compare_is_not_an_error(self) -> None:
        result = self.poll(self.merged(release("v1", "2026-11-02T00:00:00Z", "#7")), until="released")
        self.assertIsNone(result["error"])
        self.assertTrue(result["confirm"])

    def test_compare_404_is_asked_again(self) -> None:
        gh = self.merged(release("v1", "2026-11-02T00:00:00Z"))
        self.assertFalse(self.poll(gh, until="released")["fired"])
        gh.compare = {"v1": "ahead"}
        self.assertTrue(self.poll(gh, until="released")["fired"])

    def test_merged_without_a_merge_commit_compares_the_head(self) -> None:
        gh = self.merged(release("v1", "2026-11-02T00:00:00Z"), compare={"v1": "ahead"}, merge_commit=False)
        result = self.poll(gh, until="released")
        self.assertIsNone(result["error"])
        self.assertTrue(result["fired"])
        self.assertEqual(gh.count(f"compare/{HEAD}...v1"), 1)

    def test_unknown_merge_time_reads_one_page(self) -> None:
        self.memo = {"merged": {"sha": MERGE_SHA, "at": None}}
        releases = [release(f"r{i}", "2026-11-02T00:00:00Z") for i in range(github.RELEASE_PAGE + 1)]
        gh = FakeGh(releases=releases, compare={item["tag_name"]: "behind" for item in releases})
        self.poll(gh, until="released")
        self.assertEqual(gh.count("/releases"), 1)

    def test_transient_release_failure_retries_soon(self) -> None:
        gh = self.merged(
            release("v1", "2026-11-02T00:00:00Z"), compare={"v1": ok(code=1, stderr="gh: Bad Gateway (HTTP 502)\n")},
        )
        result = self.poll(gh, until="released")
        self.assertTrue(result["transient"])
        self.assertEqual(result["next_poll_s"], github.RETRY_POLL_S)

    def test_tag_is_escaped_in_the_compare_path(self) -> None:
        gh = self.merged(release("v1+build/x", "2026-11-02T00:00:00Z"), compare={"v1%2Bbuild/x": "ahead"})
        self.assertTrue(self.poll(gh, until="released")["fired"])

    def test_a_prerelease_with_the_merge_is_news_not_the_release(self) -> None:
        gh = self.merged(
            release("v2-rc2", "2026-11-03T00:00:00Z", prerelease=True),
            release("v2-rc1", "2026-11-02T00:00:00Z", "#7", prerelease=True),
            compare={"v2-rc1": "ahead", "v2-rc2": "ahead"},
        )
        first = self.poll(gh, until="released")
        self.assertFalse(first["fired"])
        self.assertFalse(first["confirm"])
        self.assertEqual(
            [(event["key"], event["text"], event["on_me"]) for event in first["events"]],
            [("prerelease:v2-rc1", "PR#7 is in prerelease v2-rc1; waiting for a release", False)],
        )
        second = self.poll(gh, until="released")
        self.assertFalse(second["fired"])
        self.assertEqual(second["events"], [])

        gh.releases.insert(0, release("v2", "2026-11-04T00:00:00Z"))
        gh.compare["v2"] = "ahead"
        third = self.poll(gh, until="released")
        self.assertTrue(third["fired"])
        self.assertEqual(third["fire_text"], "PR#7 released in v2")

    def test_a_prerelease_without_the_merge_says_nothing(self) -> None:
        gh = self.merged(release("v2-rc1", "2026-11-02T00:00:00Z", prerelease=True), compare={"v2-rc1": "behind"})
        result = self.poll(gh, until="released")
        self.assertFalse(result["fired"])
        self.assertEqual(result["events"], [])


class ErrorsTest(PollTestCase):
    def test_gateway_error_is_transient(self) -> None:
        result = self.poll(FakeGh(ok(code=1, stderr="gh: Bad Gateway (HTTP 502)\n")))
        self.assertEqual(result["error"], "gh: Bad Gateway (HTTP 502)")
        self.assertTrue(result["transient"])
        self.assertEqual(result["next_poll_s"], 60)
        self.assertFalse(result["fired"])

    def test_unknown_pr_is_fatal(self) -> None:
        result = self.poll(FakeGh(ok(code=1, stderr="gh: Could not resolve to a PullRequest\n")))
        self.assertIn("Could not resolve", result["error"])
        self.assertFalse(result["transient"])

    def test_null_pull_request_is_fatal(self) -> None:
        result = self.poll(FakeGh({"data": {"repository": {"pullRequest": None}}}))
        self.assertIn("no such pull request", result["error"])
        self.assertFalse(result["transient"])

    def test_malformed_json_is_transient(self) -> None:
        result = self.poll(FakeGh(ok("<html>oops</html>")))
        self.assertTrue(result["transient"])

    def test_network_server_and_rate_limit_failures_are_transient(self) -> None:
        for text in (
            "error connecting to api.github.com\ncheck your internet connection or https://githubstatus.com",
            'Post "https://api.github.com/graphql": dial tcp: lookup api.github.com: no such host',
            'Post "https://api.github.com/graphql": dial tcp: lookup api.github.com on 127.0.0.53:53: '
            "server misbehaving",
            'Get "https://api.github.com/user": dial tcp 140.82.121.6:443: connect: no route to host',
            'Post "https://api.github.com/graphql": net/http: TLS handshake timeout',
            'Post "https://api.github.com/graphql": context deadline exceeded',
            'Post "https://api.github.com/graphql": unexpected EOF',
            "gh: Gateway Timeout (HTTP 504)",
            "gh: Too Many Requests (HTTP 429)",
            "gh: You have exceeded a secondary rate limit. (HTTP 403)",
            "GraphQL: Something went wrong while executing your query. This may be the result of a timeout",
            "gh: You've exceeded a secondary rate limit; don't retry yet (HTTP 403)",
            "GitHub's servers timed out for 'acme/widget'",
            'Get "https://api.github.com/user": read tcp 10.0.0.2:51234->140.82.121.6:443: i/o timeout',
            '{"documentation_url":"https://docs.github.com/rest","message":"timeout"}',
        ):
            with self.subTest(text=text):
                self.assertTrue(github.is_transient(text))

    def test_other_failures_are_not_transient(self) -> None:
        for text in (
            "gh: Bad credentials (HTTP 401)",
            "gh: Resource not accessible by integration (HTTP 403)",
            "gh: Not Found (HTTP 404)",
            "gh: Validation Failed (HTTP 422)",
            "To get started with GitHub CLI, please run:  gh auth login",
            # Where it failed says nothing about why.
            "gh: Not Found (HTTP 404) (https://api.github.com/repos/acme/timeout-tool/releases)",
            "GraphQL: Could not resolve to a Repository with the name 'acme/temporary-eof'. (repository)",
            "release not found in acme/eof-parser",
            "",
        ):
            with self.subTest(text=text):
                self.assertFalse(github.is_transient(text))

    def test_a_missing_repo_named_like_a_network_error_is_fatal(self) -> None:
        stderr = "GraphQL: Could not resolve to a Repository with the name 'acme/timeout'. (repository)\n"
        result = self.poll(FakeGh(ok(code=1, stderr=stderr)))
        self.assertFalse(result["transient"])

    def test_timeout_is_transient(self) -> None:
        def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

        result = self.poll(run)  # type: ignore[arg-type]
        self.assertTrue(result["transient"])

    def test_logged_out_gh_is_transient_and_says_how_to_fix_it(self) -> None:
        def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
            return ok(code=4, stderr="To get started with GitHub CLI, please run:  gh auth login\n")

        with self.assertRaises(github.GhError) as caught:
            github._gh(run, ["api", "user"])
        self.assertTrue(caught.exception.transient)
        self.assertTrue(caught.exception.auth)
        result = self.poll(run)  # type: ignore[arg-type]
        self.assertTrue(result["transient"])
        self.assertEqual(result["error"], "gh is not logged in: run `gh auth login`")
        self.assertTrue(result["auth"])

    def test_stale_check_is_a_cancel(self) -> None:
        self.assertEqual(github._bucket({"status": "COMPLETED", "conclusion": "STALE"}), "cancel")

    def test_missing_gh_is_fatal(self) -> None:
        def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
            raise FileNotFoundError("gh")

        result = self.poll(run)  # type: ignore[arg-type]
        self.assertIn("cannot run gh", result["error"])
        self.assertFalse(result["transient"])

    def test_bad_condition_is_fatal_and_runs_nothing(self) -> None:
        gh = FakeGh()
        for cond in (
            {"repo": REPO, "number": 7, "until": "soon"},
            {"repo": "--evil", "number": 7, "until": "merged"},
            {"repo": REPO, "number": "7", "until": "merged"},
            {"repo": REPO, "number": 7, "until": "merged", "bots": ["greptile", 1]},
        ):
            result = github.poll(cond, {}, run=gh)
            self.assertIn("bad", result["error"])
            self.assertFalse(result["transient"])
        self.assertEqual(gh.calls, [])

    def test_failed_poll_keeps_the_baseline_pending(self) -> None:
        gh = FakeGh(ok(code=1, stderr="gh: Bad Gateway (HTTP 502)\n"))
        self.poll(gh)
        gh.pr = pr_answer(comments=[comment("C1", "someone")])
        self.assertEqual(self.poll(gh)["events"], [])


class RateLimitTest(PollTestCase):
    def test_a_rest_poll_forgets_none_of_a_big_prs_talk(self) -> None:
        talk = [thread("T1", comments=github.SEEN_KEPT + 100, author="someone")]
        gh = FakeGh(pr_answer(threads=talk))
        self.poll(gh, until="activity")
        gh.remaining = 100
        self.clock += github.RATE_CHECK_S
        gh.rest_pr = rest_pr()
        self.assertFalse(self.poll(gh, until="activity")["fired"])
        gh.remaining = 5000
        self.clock += github.RATE_CHECK_S
        result = self.poll(gh, until="activity")
        self.assertEqual((result["fired"], result["events"]), (False, []))

    def test_review_on_rest_waits_for_the_checks_graphql_shows(self) -> None:
        gh = FakeGh(remaining=100, rest_pr=rest_pr(), rest_reviews=[rest_review()])
        result = self.poll(gh)
        self.assertFalse(result["fired"])
        self.assertEqual(result["next_poll_s"], github.RATE_LIMITED_POLL_S)
        self.assertEqual(gh.count("query="), 0)

    def test_review_on_old_head_does_not_fire_on_rest(self) -> None:
        gh = FakeGh(remaining=100, rest_pr=rest_pr(), rest_reviews=[rest_review(OLD_HEAD)])
        self.assertFalse(self.poll(gh)["fired"])

    def test_ready_cannot_fire_on_rest(self) -> None:
        gh = FakeGh(remaining=100, rest_pr=rest_pr(), rest_reviews=[rest_review()])
        self.assertFalse(self.poll(gh, until="ready")["fired"])

    def test_merged_and_released_work_on_rest(self) -> None:
        gh = FakeGh(remaining=100, rest_pr=rest_pr("closed", merged=True))
        self.assertTrue(self.poll(gh, until="merged")["fired"])

        self.memo = {}
        gh.releases = [release("v1", "2026-11-02T00:00:00Z")]
        gh.compare = {"v1": "ahead"}
        self.assertTrue(self.poll(gh, until="released")["fired"])

    def test_closed_on_rest(self) -> None:
        result = self.poll(FakeGh(remaining=100, rest_pr=rest_pr("closed")), until="merged")
        self.assertEqual(result["events"][0]["text"], "PR#7 closed without merging")

    def test_a_healthy_budget_is_not_read_again(self) -> None:
        gh = FakeGh()
        for _ in range(8):
            self.poll(gh)
        self.assertEqual(gh.count("rate_limit"), 1)

    def test_a_low_budget_is_read_at_most_every_ten_minutes(self) -> None:
        gh = FakeGh(remaining=100, rest_pr=rest_pr())
        self.poll(gh)  # 1180
        self.poll(gh)  # 1360
        self.poll(gh)  # 1540
        self.assertEqual(gh.count("rate_limit"), 1)
        self.poll(gh)  # 1720: 540 s after the first read
        self.assertEqual(gh.count("rate_limit"), 1)
        self.poll(gh)  # 1900: 720 s
        self.assertEqual(gh.count("rate_limit"), 2)

    def test_budget_back_returns_to_graphql(self) -> None:
        gh = FakeGh(remaining=100, rest_pr=rest_pr())
        self.poll(gh)
        gh.remaining = 4000
        self.clock += github.RATE_CHECK_S
        result = self.poll(gh)
        self.assertEqual(result["next_poll_s"], 180)
        self.assertEqual(gh.count("query="), 1)

    def test_rest_polls_leave_the_rest_of_the_baseline_to_graphql(self) -> None:
        gh = FakeGh(remaining=100, rest_pr=rest_pr())
        self.poll(gh)
        gh.remaining = 4000
        self.clock += github.RATE_CHECK_S
        gh.pr = pr_answer(comments=[comment("C1", "someone")], checks=[check("ci", required=True)])
        self.assertEqual(self.poll(gh)["events"], [])

    def test_viewer_is_read_once_on_a_rest_only_start(self) -> None:
        gh = FakeGh(
            remaining=100, rest_pr=rest_pr(user={"login": "someone-else"}),
            rest_reviews=[rest_review(login="me", kind="User", state="APPROVED", rid="R1")],
        )
        self.memo["baselined"] = True
        self.assertEqual(self.poll(gh)["events"][0]["key"], f"head:{HEAD}")
        self.assertEqual(self.poll(gh)["events"], [])
        self.assertEqual(gh.count("user"), 1)

    def test_failed_rate_limit_read_is_not_retried_every_poll(self) -> None:
        gh = FakeGh(remaining=ok(code=1, stderr="gh: Bad Gateway (HTTP 502)\n"))
        self.poll(gh)
        self.poll(gh)
        self.assertEqual(gh.count("rate_limit"), 1)

    def test_rate_limit_error_from_graphql_falls_back_at_once(self) -> None:
        gh = FakeGh(ok(code=1, stderr="gh: API rate limit already exceeded for user ID 1.\n"), rest_pr=rest_pr())
        result = self.poll(gh)
        self.assertIsNone(result["error"])
        self.assertEqual(result["next_poll_s"], github.RATE_LIMITED_POLL_S)
        self.assertEqual(self.memo["rate"]["remaining"], 0)

    def test_transient_failure_on_rest_retries_soon(self) -> None:
        result = self.poll(FakeGh(remaining=100, rest_pr={}))
        self.assertTrue(result["transient"])
        self.assertEqual(result["next_poll_s"], github.RETRY_POLL_S)

    def test_rate_limited_rest_keeps_the_long_interval(self) -> None:
        gh = FakeGh(
            remaining=100, rest_pr={"head": {"sha": HEAD}}, rest_reviews=[],
            user=ok(code=1, stderr="gh: API rate limit exceeded (HTTP 403)\n"),
        )
        result = self.poll(gh)
        self.assertTrue(result["transient"])
        self.assertEqual(result["next_poll_s"], github.RATE_LIMITED_POLL_S)

    def test_unknown_viewer_on_rest_is_an_error_not_activity(self) -> None:
        gh = FakeGh(pr_answer(author="someone"))
        self.poll(gh, until="activity")
        gh.remaining = 100
        self.memo["rate"]["remaining"] = 100  # a low reading, so the budget is read again
        self.clock += github.RATE_CHECK_S
        gh.rest_pr = rest_pr(user={"login": "someone"}, head={"sha": NEW_HEAD})
        self.memo.pop("viewer")
        gh.user = ok(code=1, stderr="gh: Bad Gateway (HTTP 502)\n")
        result = self.poll(gh, until="activity")
        self.assertFalse(result["fired"])
        self.assertTrue(result["transient"])

    def test_a_review_during_a_rest_only_start_is_not_lost(self) -> None:
        gh = FakeGh(remaining=100, rest_pr=rest_pr(user={"login": "someone"}))
        self.assertFalse(self.poll(gh, until="activity")["fired"])
        gh.rest_reviews = [rest_review(login="alice", kind="User", state="CHANGES_REQUESTED", body="no", rid="R1")]
        result = self.poll(gh, until="activity")
        self.assertTrue(result["fired"])
        self.assertEqual(result["fire_text"], "review from alice (changes requested): no")

    def test_rest_ignores_reviews_older_than_graphqls_window(self) -> None:
        reviews = [
            rest_review(login="alice", kind="User", body="looks fine", rid=f"R{index}") for index in range(101)
        ]
        # A whole GraphQL poll saw R1..R100 and has since forgotten R0.
        self.memo = {
            "seen": [f"head:{HEAD}", *(f"c:R{index}" for index in range(1, 101))],
            "baselined": True,
            "rest_baselined": True,
            "viewer": "me",
        }
        gh = FakeGh(remaining=100, rest_pr=rest_pr(), rest_reviews=reviews)
        result = self.poll(gh, until="activity")
        self.assertEqual((result["fired"], result["events"]), (False, []))


def answered(tid: str = "T1", login: str = "me") -> dict:
    """A bot's thread with one reply by `login`."""
    node = thread(tid)
    node["comments"]["nodes"].append(comment(f"{tid}-reply", login, "fixed"))
    return node


class VerifyFixTest(unittest.TestCase):
    def test_snapshot_shape(self) -> None:
        gh = FakeGh(pr_answer(threads=[answered("T1"), thread("T2", resolved=True)]))
        self.assertEqual(
            github.snapshot(REPO, 7, run=gh),
            {
                "head": HEAD,
                "threads": [
                    {"id": "T1", "resolved": False, "replies": 1},
                    {"id": "T2", "resolved": True, "replies": 0},
                ],
            },
        )

    def test_a_bots_own_follow_up_is_not_an_answer(self) -> None:
        before = github.snapshot(REPO, 7, run=FakeGh(pr_answer(threads=[thread("T1")])))
        after = FakeGh(pr_answer(threads=[thread("T1", comments=2)]))
        self.assertFalse(github.verify_fix(REPO, 7, before, run=after)[0])
        self.assertTrue(github.verify_fix(REPO, 7, before, run=FakeGh(pr_answer(threads=[answered("T1")])))[0])

    def test_a_fired_fix_poll_carries_the_snapshot(self) -> None:
        gh = FakeGh(pr_answer(reviews=[bot_review()], threads=[answered("T1")]))
        result = github.poll(
            {"repo": REPO, "number": 7, "until": "review_on_head"}, {}, run=gh, now=1000.0, snapshot=True
        )
        self.assertTrue(result["fired"])
        self.assertEqual(
            result["snapshot"], {"head": HEAD, "threads": [{"id": "T1", "resolved": False, "replies": 1}]}
        )
        self.assertEqual(gh.count("query="), 1)

    def test_snapshot_raises_on_failure(self) -> None:
        with self.assertRaises(github.GhError):
            github.snapshot(REPO, 7, run=FakeGh(ok(code=1, stderr="gh: boom\n")))

    def test_snapshot_reads_every_thread_page(self) -> None:
        gh = FakeGh(pr_answer(threads=[thread("T3")], older_cursor="c2"))
        gh.older_threads = {
            "c2": threads_page([thread("T2")], "c1"),
            "c1": threads_page([thread("T1")]),
        }
        ids = [item["id"] for item in github.snapshot(REPO, 7, run=gh)["threads"]]
        self.assertEqual(ids, ["T1", "T2", "T3"])

    def test_new_head_passes(self) -> None:
        before = github.snapshot(REPO, 7, run=FakeGh(pr_answer(threads=[thread()])))
        passed, reason = github.verify_fix(
            REPO, 7, before, run=FakeGh(pr_answer(head=NEW_HEAD, threads=[thread()]))
        )
        self.assertTrue(passed)
        self.assertEqual(reason, "new head ccccccc")

    def test_every_open_thread_answered_or_resolved_passes(self) -> None:
        before = github.snapshot(
            REPO, 7, run=FakeGh(pr_answer(threads=[thread("T1"), thread("T2"), thread("T3", resolved=True)]))
        )
        after = FakeGh(pr_answer(threads=[answered("T1"), thread("T2", resolved=True)]))
        passed, reason = github.verify_fix(REPO, 7, before, run=after)
        self.assertTrue(passed)
        self.assertEqual(reason, "all 2 open threads answered or resolved")

    def test_unanswered_thread_fails(self) -> None:
        before = github.snapshot(REPO, 7, run=FakeGh(pr_answer(threads=[thread("T1"), thread("T2")])))
        after = FakeGh(pr_answer(threads=[answered("T1"), thread("T2")]))
        passed, reason = github.verify_fix(REPO, 7, before, run=after)
        self.assertFalse(passed)
        self.assertEqual(reason, "head unchanged at aaaaaaa, 1 of 2 open threads unanswered")

    def test_no_open_threads_passes(self) -> None:
        before = github.snapshot(REPO, 7, run=FakeGh())
        self.assertEqual(github.verify_fix(REPO, 7, before, run=FakeGh()), (True, "no open threads"))

    def test_gh_failure_raises(self) -> None:
        # The caller tells a check that couldn't run from a fix that failed.
        with self.assertRaises(github.GhError) as caught:
            github.verify_fix(
                REPO, 7, {"head": HEAD, "threads": []}, run=FakeGh(ok(code=1, stderr="gh: boom\n"))
            )
        self.assertIn("gh: boom", str(caught.exception))

    def test_reply_past_the_first_hundred_comments_counts(self) -> None:
        long_thread = thread("T1", comments=100)
        long_thread["comments"]["pageInfo"] = {"hasNextPage": True, "endCursor": "k1"}
        gh = FakeGh(pr_answer(threads=[long_thread]))
        gh.more_comments = {"k1": []}
        before = github.snapshot(REPO, 7, run=gh)
        self.assertEqual(before["threads"][0]["replies"], 0)  # the bot's own comments
        gh.more_comments = {"k1": [comment("T1-100", "me", "fixed")]}
        self.assertEqual(
            github.verify_fix(REPO, 7, before, run=gh), (True, "all 1 open threads answered or resolved")
        )


class PrStatusTest(unittest.TestCase):
    def status(self, **fields: Any) -> dict:
        self.argv: list[str] = []

        def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
            self.argv = argv
            return ok(json.dumps({"title": "T", "state": "OPEN", "statusCheckRollup": [], **fields}))

        return github.pr_status(REPO, 7, run=run)

    def test_asks_gh_once_with_the_contract_fields(self) -> None:
        self.status()
        self.assertEqual(
            self.argv,
            ["gh", "pr", "view", "7", "-R", REPO, "--json", "title,state,isDraft,reviewDecision,statusCheckRollup"],
        )

    def test_state(self) -> None:
        cases = (
            ({"state": "OPEN", "isDraft": False}, "open"),
            ({"state": "OPEN", "isDraft": True}, "draft"),
            ({"state": "MERGED", "isDraft": True}, "merged"),
            ({"state": "CLOSED"}, "closed"),
        )
        for fields, state in cases:
            self.assertEqual(self.status(**fields)["state"], state)

    def test_review(self) -> None:
        self.assertEqual(self.status(reviewDecision="CHANGES_REQUESTED")["review"], "changes_requested")
        self.assertEqual(self.status(reviewDecision="REVIEW_REQUIRED")["review"], "review_required")
        self.assertEqual(self.status(reviewDecision="APPROVED")["review"], "approved")
        self.assertIsNone(self.status(reviewDecision="")["review"])

    def test_checks_take_the_worst_bucket(self) -> None:
        passed = check("a")
        skipped = check("b", "SKIPPED")
        running = check("c", conclusion=None, status="IN_PROGRESS")
        failed = check("d", "FAILURE")
        cases = (
            ([], None),
            ([passed, skipped], "passing"),
            ([passed, {"context": "ci", "state": "SUCCESS"}], "passing"),
            ([passed, running], "pending"),
            ([{"context": "ci", "state": "PENDING"}], "pending"),
            ([running, failed], "failing"),
            ([running, check("e", "CANCELLED")], "failing"),
            ([{"context": "ci", "state": "ERROR"}], "failing"),
        )
        for rollup, checks in cases:
            self.assertEqual(self.status(statusCheckRollup=rollup)["checks"], checks, rollup)

    def test_failure_raises(self) -> None:
        with self.assertRaises(github.GhError):
            github.pr_status(REPO, 7, run=lambda argv, **kwargs: NOT_FOUND)


class FixPromptTest(unittest.TestCase):
    def test_placeholders_format(self) -> None:
        text = github.FIX_PROMPT.format(repo=REPO, number=7, head=HEAD[:7])
        self.assertIn("acme/widget#7 at head aaaaaaa", text)
        self.assertIn("Never merge", text)


if __name__ == "__main__":
    unittest.main()
