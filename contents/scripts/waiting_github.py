"""Pull request conditions for codexbar_waiting.py, read with the gh CLI.

poll() answers one question per wait: did the PR reach `until`, and what
happened since the last poll. It follows tstack's pr-wait: one GraphQL query per
poll, event keys kept in the wait's memo, and a first poll that records what
exists and reports none of it.

`fired` and `confirm` describe the PR's state, so a condition that already holds
fires on the first poll. Only events are held back by the baseline, except the
ones that need you or end the wait's chance of firing (a failed check, a
conflict, CI awaiting approval, a close or merge, a release that needs
confirming): those are states, and no later poll would report them. A REST
poll baselines what REST shows (the head and reviews), so news there isn't lost
while GraphQL is starved. `merged` says the PR is merged, which settles any
wait on it: the caller decides how.

GraphQL has a separate budget from REST. When it runs low, polls read REST,
which has no review threads and no checks, so `ready` can't fire and
`review_on_head` waits on an open PR until GraphQL is back.
"""

from __future__ import annotations

import html
import itertools
import json
import re
import subprocess
import time
from typing import Any, Callable
from urllib.parse import quote

Json = dict[str, Any]
Run = Callable[..., subprocess.CompletedProcess]

UNTILS = ("review_on_head", "ready", "merged", "released", "activity")
# Event keys that count as someone else's activity for `activity`.
ACTIVITY = ("c:", "bot:", "bot-comments:", "closed:", "merged:")
# Lowercase substrings of each bot's login. Every one of them gates.
DEFAULT_BOTS = (
    "greptile",
    "macroscope",
    "coderabbit",
    "codex",  # chatgpt-codex-connector
    "copilot",  # copilot-pull-request-reviewer
    "gemini",  # gemini-code-assist
    "cursor",  # Cursor Bugbot
    "sourcery",
    "roboomp",
)
# Every default bot but roboomp, which posts from a user account.
APP_BOTS = frozenset(DEFAULT_BOTS) - {"roboomp"}
# Bots that review a PR once and skip later pushes unless asked again. A head no
# bot reviewed counts as reviewed after BOT_SETTLE_S only when every bot that
# reviewed an earlier head is one of these; the others review every push.
ONCE_BOTS = frozenset({"copilot", "codex", "gemini"})
# CI and deploy bots report through checks. Their PR comments are status reports.
STATUS_BOTS = ("github-actions", "vercel", "netlify", "railway", "cloudflare")

DEFAULT_POLL_S = 180
RETRY_POLL_S = 60
RATE_LIMITED_POLL_S = 900
# Closed, or merged while watching for activity: only a reopen can change that.
ENDED_POLL_S = 3600
RATE_FLOOR = 200
RATE_CHECK_S = 600
RULES_TTL_S = 3600
# How long `ready` gives the bots on a head before going on without a review.
BOT_SETTLE_S = 900
BOT_STUCK_S = 3600
RELEASE_PAGE = 30
RELEASE_POLL_S = 900
GH_TIMEOUT_S = 30

TRANSIENT = re.compile(
    r"HTTP (5\d\d|429)|rate limit|timed? ?out|connection (reset|refused)|\bEOF\b|"
    r"temporar|could not resolve host|no such host|error connecting|"
    r"network is unreachable|TLS handshake|Something went wrong",
    re.IGNORECASE,
)
RATE_LIMITED = re.compile(r"HTTP 429|rate limit", re.IGNORECASE)
REPO_RE = re.compile(r"[\w.-]+/[\w.-]+")
PR_URL_RE = re.compile(r"https?://(?:www\.)?github\.com/([\w.-]+/[\w.-]+)/pull/(\d+)(?:[/?#]\S*)?")
PR_SHORT_RE = re.compile(r"([\w.-]+/[\w.-]+)#(\d+)")

GREEN = ("pass", "skipping")
FAILED = ("fail", "cancel")
# Workflow run conclusions that mean the head's CI didn't pass.
CI_FAILED = ("failure", "cancelled", "timed_out", "startup_failure")
# gh's buckets, worst first. Duplicate names (push and pull_request runs of one
# job) collapse to the worst bucket.
RANK = {"fail": 4, "cancel": 3, "pending": 2, "pass": 1, "skipping": 0}
BUCKETS = {
    "SUCCESS": "pass",
    "SKIPPED": "skipping",
    "NEUTRAL": "skipping",
    "CANCELLED": "cancel",
    "ERROR": "fail",
    "FAILURE": "fail",
    "TIMED_OUT": "fail",
    "ACTION_REQUIRED": "fail",
    "STARTUP_FAILURE": "fail",
}
PR_STATES = {"OPEN": "open", "MERGED": "merged", "CLOSED": "closed"}
CODEX_STATE = re.compile(r"<!-- codex-review-state:v1 (.*?) -->", re.DOTALL)

# Fragments go last: a GraphQL document may define them after their use.
COMMENTS_FRAGMENT = """
fragment Comments on PullRequestReviewCommentConnection {
  pageInfo { hasNextPage endCursor }
  nodes { id body author { login __typename } }
}
"""

THREADS_FRAGMENT = """
fragment Threads on PullRequestReviewThreadConnection {
  pageInfo { hasPreviousPage startCursor }
  nodes {
    id isResolved path line
    comments(first: 100) { ...Comments }
  }
}
""" + COMMENTS_FRAGMENT

CONTEXTS_FRAGMENT = """
fragment Contexts on StatusCheckRollupContextConnection {
  pageInfo { hasNextPage endCursor }
  nodes {
    ... on CheckRun {
      name status conclusion
      checkSuite { app { slug } }
      isRequired(pullRequestNumber: $number)
    }
    ... on StatusContext {
      context state
      creator { login }
      isRequired(pullRequestNumber: $number)
    }
  }
}
"""

QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  viewer { login }
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      state headRefOid baseRefName mergeStateStatus mergedAt closedAt
      mergeCommit { oid }
      author { login }
      baseRef { target { oid } }
      comments(last: 100) { nodes { id body author { login __typename } } }
      reviews(last: 100) {
        nodes { id state body commit { oid } author { login __typename } }
      }
      reviewThreads(last: 100) { ...Threads }
      commits(last: 1) {
        nodes {
          commit {
            statusCheckRollup { contexts(first: 100) { ...Contexts } }
          }
        }
      }
    }
  }
}
""" + THREADS_FRAGMENT + CONTEXTS_FRAGMENT

OLDER_THREADS_QUERY = """
query($owner: String!, $name: String!, $number: Int!, $before: String!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      reviewThreads(last: 100, before: $before) { ...Threads }
    }
  }
}
""" + THREADS_FRAGMENT

MORE_CONTEXTS_QUERY = """
query($owner: String!, $name: String!, $number: Int!, $after: String!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      commits(last: 1) {
        nodes {
          commit {
            statusCheckRollup { contexts(first: 100, after: $after) { ...Contexts } }
          }
        }
      }
    }
  }
}
""" + CONTEXTS_FRAGMENT

# A node query has no repository, so it declares no $owner or $name.
MORE_COMMENTS_QUERY = """
query($id: ID!, $after: String!) {
  node(id: $id) {
    ... on PullRequestReviewThread { comments(first: 100, after: $after) { ...Comments } }
  }
}
""" + COMMENTS_FRAGMENT

FIX_PROMPT = """\
A review landed on {repo}#{number} at head {head}. Triage it:

- Handle only checks and comments newer than the latest push.
- Verify every finding against the source before changing code.
- Fix real findings and CI failures. Tell repository failures from infrastructure flakes.
- When a finding is wrong or not worth addressing, reply on its thread with a written reason and resolve the thread.
- Keep the PR at the size its goal needs. Prefer the fix that deletes or narrows code. Decline the rest with a written reason and list it as follow-ups: unneeded tests, guards for edge cases that don't matter, extra compatibility, small performance costs.
- Never merge. Push your fixes and stop.
"""


class GhError(Exception):
    def __init__(self, message: str, transient: bool) -> None:
        super().__init__(message)
        self.transient = transient


def _gh(run: Run, args: list[str]) -> str:
    try:
        proc = run(
            ["gh", *args],
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=GH_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        raise GhError(f"gh timed out after {GH_TIMEOUT_S} s", True) from None
    except OSError as error:
        raise GhError(f"cannot run gh: {error}", False) from error
    if proc.returncode == 0:
        return proc.stdout
    stderr = proc.stderr or ""
    message = " ".join(stderr.split())[:300] or f"gh exited {proc.returncode}"
    raise GhError(message, bool(TRANSIENT.search(stderr)))


def _json(text: str, expected: type) -> Any:
    """Parse a gh answer. A malformed one is a bad gateway page, so retry it."""
    try:
        value = json.loads(text)
    except ValueError:
        raise GhError("gh returned malformed JSON", True) from None
    if not isinstance(value, expected):
        raise GhError("gh returned an unexpected JSON shape", True)
    return value


def _graphql(run: Run, repo: str | None, query: str, **variables: int | str) -> Json:
    """`repo` fills the query's $owner and $name; a node query has neither."""
    args = ["api", "graphql", "-f", f"query={query}"]
    if repo:
        owner, name = repo.split("/")
        args += ["-f", f"owner={owner}", "-f", f"name={name}"]
    for key, value in variables.items():
        args += ["-f" if isinstance(value, str) else "-F", f"{key}={value}"]
    return _json(_gh(run, args), dict)


def _page(connection: Json | None) -> list[Json]:
    return [node for node in (connection or {}).get("nodes") or [] if node]


def _nodes(parent: Json | None, key: str) -> list[Json]:
    return _page((parent or {}).get(key))


def _login(node: Json, key: str = "author") -> str:
    login = (node.get(key) or {}).get("login") or "ghost"
    return login.removesuffix("[bot]")  # REST spells bots with it, GraphQL without



def _row(node: Json) -> Json:
    """A GraphQL comment or review: who wrote it and what it says."""
    return {
        "id": node.get("id"),
        "login": _login(node),
        "is_bot": (node.get("author") or {}).get("__typename") == "Bot",
        "body": node.get("body") or "",
    }


def _later_pages(connection: Json | None, fetch: Callable[[str], Json | None]) -> list[Json]:
    """The nodes of `connection` and of every page after it. `fetch(cursor)`
    answers the next page's connection. Only a long list costs another query."""
    nodes = _page(connection)
    page = (connection or {}).get("pageInfo") or {}
    while page.get("hasNextPage") and page.get("endCursor"):
        connection = fetch(page["endCursor"])
        nodes += _page(connection)
        page = (connection or {}).get("pageInfo") or {}
    return nodes


def _thread(thread: Json, run: Run) -> Json:
    """A review thread with every comment: a reply past the first 100 is still news."""

    def more(cursor: str) -> Json | None:
        answer = _graphql(run, None, MORE_COMMENTS_QUERY, id=thread.get("id") or "", after=cursor)
        return ((answer.get("data") or {}).get("node") or {}).get("comments")

    line = thread.get("line")
    return {
        "id": thread.get("id"),
        "resolved": bool(thread.get("isResolved")),
        "where": f"{thread.get('path')}:{line}" if line else str(thread.get("path")),
        "comments": [_row(comment) for comment in _later_pages(thread.get("comments"), more)],
    }


def _all_threads(repo: str, number: int, pr: Json, run: Run) -> list[Json]:
    """Every review thread: an open one past the first page still blocks `ready`."""
    threads = _nodes(pr, "reviewThreads")
    page = (pr.get("reviewThreads") or {}).get("pageInfo") or {}
    while page.get("hasPreviousPage") and page.get("startCursor"):
        answer = _graphql(run, repo, OLDER_THREADS_QUERY, number=number, before=page["startCursor"])
        older = ((answer.get("data") or {}).get("repository") or {}).get("pullRequest")
        threads = _nodes(older, "reviewThreads") + threads
        page = ((older or {}).get("reviewThreads") or {}).get("pageInfo") or {}
    return [_thread(thread, run) for thread in threads]


def _bucket(check: Json) -> str:
    if "context" in check:
        result = check.get("state")
    else:
        result = check.get("conclusion") if check.get("status") == "COMPLETED" else None
    return BUCKETS.get(result or "", "pending")


def _contexts(repo: str, number: int, pr: Json, run: Run) -> list[Json]:
    """The head commit's check runs and statuses, every page of them."""

    def rollup(pull: Json | None) -> Json | None:
        commits = _nodes(pull, "commits")
        commit = (commits[-1].get("commit") or {}) if commits else {}
        return (commit.get("statusCheckRollup") or {}).get("contexts")

    def more(cursor: str) -> Json | None:
        answer = _graphql(run, repo, MORE_CONTEXTS_QUERY, number=number, after=cursor)
        return rollup(((answer.get("data") or {}).get("repository") or {}).get("pullRequest"))

    return _later_pages(rollup(pr), more)


def _rollup(contexts: list[Json]) -> dict[str, Json]:
    """The head commit's checks by name, each with its worst bucket and the app
    or account that reports it."""
    checks: dict[str, Json] = {}
    for node in contexts:
        name = node.get("name") or node.get("context")
        if not name:
            continue
        bucket = _bucket(node)
        current = checks.get(name)
        required = bool(node.get("isRequired")) or bool(current and current["required"])
        worst = bucket if current is None or RANK[bucket] > RANK[current["bucket"]] else current["bucket"]
        app = (node.get("checkSuite") or {}).get("app") or {}
        owner = app.get("slug") or (node.get("creator") or {}).get("login") or ""
        checks[name] = {"bucket": worst, "required": required, "owner": owner.lower()}
    return checks


def _fetch_graphql(repo: str, number: int, run: Run) -> Json:
    data = _graphql(run, repo, QUERY, number=number).get("data") or {}
    pr = (data.get("repository") or {}).get("pullRequest")
    if not pr:
        raise GhError(f"{repo}#{number}: no such pull request", False)
    head = pr.get("headRefOid")
    if not head:
        raise GhError(f"{repo}#{number}: GitHub gave no head commit", True)
    return {
        "full": True,
        "state": pr.get("state"),
        "head": head,
        "base": pr.get("baseRefName") or "",
        "base_oid": ((pr.get("baseRef") or {}).get("target") or {}).get("oid"),
        "merge_state": pr.get("mergeStateStatus"),
        "merge_sha": (pr.get("mergeCommit") or {}).get("oid"),
        "merged_at": pr.get("mergedAt"),
        "closed_at": pr.get("closedAt"),
        "author": _login(pr),
        "viewer": _login(data, "viewer"),
        "reviews": [
            {
                **_row(review),
                "state": review.get("state"),
                "commit": (review.get("commit") or {}).get("oid"),
            }
            for review in _nodes(pr, "reviews")
        ],
        "comments": [_row(comment) for comment in _nodes(pr, "comments")],
        "threads": _all_threads(repo, number, pr, run),
        "checks": _rollup(_contexts(repo, number, pr, run)),
    }


def _fetch_rest(repo: str, number: int, run: Run, viewer: str) -> Json:
    pr = _json(_gh(run, ["api", f"repos/{repo}/pulls/{number}"]), dict)
    head = (pr.get("head") or {}).get("sha")
    if not head:
        raise GhError(f"{repo}#{number}: GitHub gave no head commit", True)
    merged = bool(pr.get("merged") or pr.get("merged_at"))
    lines = _gh(
        run, ["api", "--paginate", f"repos/{repo}/pulls/{number}/reviews?per_page=100", "--jq", ".[]"]
    ).splitlines()
    reviews = [_json(line, dict) for line in lines if line.strip()]
    return {
        "full": False,
        "state": "MERGED" if merged else str(pr.get("state") or "").upper(),
        "head": head,
        "base": (pr.get("base") or {}).get("ref") or "",
        "base_oid": (pr.get("base") or {}).get("sha"),
        "merge_state": str(pr.get("mergeable_state") or "unknown").upper(),
        "merge_sha": pr.get("merge_commit_sha") if merged else None,
        "merged_at": pr.get("merged_at"),
        "closed_at": pr.get("closed_at"),
        "author": _login(pr, "user"),
        "viewer": viewer,
        "reviews": [
            {
                "id": review.get("node_id"),
                "login": _login(review, "user"),
                "is_bot": (review.get("user") or {}).get("type") == "Bot",
                "state": review.get("state"),
                "body": review.get("body") or "",
                "commit": review.get("commit_id"),
            }
            for review in reviews
        ],
        "comments": [],
        "threads": [],
        "checks": {},
    }


def _graphql_starved(memo: Json, run: Run, now: float) -> bool:
    rate = memo.get("rate") or {}
    if now - rate.get("at", float("-inf")) >= RATE_CHECK_S:
        rate = {**rate, "at": now}
        memo["rate"] = rate
        try:
            text = _gh(run, ["api", "rate_limit", "--jq", ".resources.graphql.remaining"])
            rate["remaining"] = int(text.strip())
        except (GhError, ValueError):
            pass  # keep the last reading; a real outage surfaces in the PR query
    return rate.get("remaining", RATE_FLOOR) < RATE_FLOOR


def _rest_viewer(memo: Json, run: Run) -> str:
    """Your login. Unknown, your own pushes and reviews would read as someone else's."""
    if not memo.get("viewer"):
        login = _gh(run, ["api", "user", "--jq", ".login"]).strip().removesuffix("[bot]")
        if not login:
            raise GhError("gh gave no login for you", True)
        memo["viewer"] = login
    return memo["viewer"]


def _fetch(repo: str, number: int, memo: Json, result: Json, run: Run, now: float) -> Json:
    if not _graphql_starved(memo, run, now):
        try:
            pr = _fetch_graphql(repo, number, run)
        except GhError as error:
            if "rate limit" not in str(error).lower():
                raise
            memo["rate"] = {"at": now, "remaining": 0}
        else:
            memo["viewer"] = pr["viewer"]
            return pr
    result["next_poll_s"] = RATE_LIMITED_POLL_S
    return _fetch_rest(repo, number, run, _rest_viewer(memo, run))


def _required_names(repo: str, base: str, memo: Json, run: Run, now: float) -> list[str]:
    """Required checks from the base branch's rules. Unlike the rollup's
    isRequired, this also names a required check that hasn't started yet."""
    cached = memo.get("required") or {}
    fresh = now - cached.get("at", float("-inf")) < RULES_TTL_S
    if cached.get("base") == base and fresh:
        return cached.get("names") or []
    branch = quote(base, safe="/")
    sources = (
        (
            f"repos/{repo}/rules/branches/{branch}",
            '.[] | select(.type == "required_status_checks") '
            "| .parameters.required_status_checks[].context",
        ),
        (
            f"repos/{repo}/branches/{branch}/protection/required_status_checks",
            ".contexts[]?, .checks[]?.context",
        ),
    )
    names: set[str] = set()
    for path, jq in sources:
        try:
            names.update(line for line in _gh(run, ["api", path, "--jq", jq]).splitlines() if line)
        except GhError as error:
            if error.transient:
                raise  # unknown required checks must not read as none
            # No protection, or no admin rights to read it: the rollup still has isRequired.
    memo["required"] = {"at": now, "base": base, "names": sorted(names)}
    return memo["required"]["names"]


def _ci_runs(repo: str, head: str, run: Run) -> dict[str, list[str]]:
    """The head's workflow runs by what they mean for `ready`: `approval` (a
    maintainer has to approve them first), `running`, `failed`. A fork's CI
    shows no checks before approval, and a run that fails before its first job
    shows none at all, so the rollup can't tell either from a repo without CI.
    Only each workflow's newest run per event counts: a run that concurrency
    cancelled for a later one is not a failure."""
    runs: dict[str, list[str]] = {"approval": [], "running": [], "failed": []}
    try:
        lines = _gh(
            run,
            [
                "api", f"repos/{repo}/actions/runs?head_sha={head}&per_page=100",
                "--jq",
                '.workflow_runs[] | [.name, .status, .conclusion // "", "\\(.workflow_id)/\\(.event)"]'
                " | @tsv",
            ],
        ).splitlines()
    except GhError as error:
        if error.transient:
            raise
        return runs  # Actions off, or not readable: the rollup is all there is.
    newest: set[str] = set()
    for line in lines:  # GitHub lists the newest run first
        name, status, conclusion, workflow = (line.split("\t") + ["", "", "", ""])[:4]
        if (workflow or name) in newest:
            continue
        newest.add(workflow or name)
        if conclusion == "action_required" or status == "action_required":
            runs["approval"].append(name)
        elif status != "completed":
            runs["running"].append(name)
        elif conclusion in CI_FAILED:
            runs["failed"].append(name)
    return {key: sorted(set(names)) for key, names in runs.items()}


def _plain(body: str) -> str:
    body = re.sub(r"<!--.*?-->", " ", body or "", flags=re.DOTALL)
    return html.unescape(re.sub(r"<[^>]+>", " ", body))


def _snippet(body: str, limit: int = 100) -> str:
    text = " ".join(_plain(body).split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _verdict(body: str) -> str:
    """The verdict line of a bot summary (Macroscope, Greptile), if it has one."""
    for line in _plain(body).splitlines():
        if re.search(r"verdict|confidence score", line, re.IGNORECASE):
            return _snippet(line.replace("*", ""), 80)
    return ""


def _bots(cond: Json) -> tuple[str, ...]:
    bots = cond.get("bots") or DEFAULT_BOTS
    if isinstance(bots, str):
        bots = bots.split(",")
    return tuple(bot.strip().lower() for bot in bots if bot.strip()) or DEFAULT_BOTS


def _is_bot(row: Json, bot: str) -> bool:
    """A review or comment by `bot`. A person whose login contains an App bot's
    name isn't that bot."""
    return bot in row["login"].lower() and (row["is_bot"] or bot not in APP_BOTS)


def _bot_check(name: str, check: Json, bot: str) -> bool:
    """Matched on the reporting app when GitHub names one: a CI job called
    `codex-tests` runs on github-actions and is not the Codex bot."""
    return bot in (check["owner"] or name.lower())


def _codex_unfinished(body: str) -> bool:
    """A Codex summary names the head however its review went, also a failed one."""
    match = CODEX_STATE.search(body)
    if not match:
        return False
    try:
        state = json.loads(match.group(1))
    except ValueError:
        return False
    review = state.get("code_review") if isinstance(state, dict) else None
    return isinstance(review, dict) and review.get("status") != "completed"


def _bot_activity(pr: Json, bot: str) -> tuple[bool, bool, list[str], str, bool]:
    """(has reviewed an earlier head, finished on the head, its pending checks,
    verdict, left a review on the head).
    A bot with checks is done when they finish: clean, or failed with a review or
    summary on the head to show for it. Without checks it is done once that review
    or summary exists. A summary counts when it names the head: Macroscope and Codex
    leave no review on a clean PR. The PR's author is never the bot: Copilot's
    coding agent writes PRs that its code review bot then reviews."""
    head = pr["head"]
    author = pr["author"]
    checks = {
        name: check
        for name, check in pr["checks"].items()
        if _bot_check(name, check, bot) and check["owner"] != author.lower()
    }
    reviews = [
        review for review in pr["reviews"] if _is_bot(review, bot) and review["login"] != author
    ]
    summaries = [
        comment for comment in pr["comments"] if _is_bot(comment, bot) and comment["login"] != author
    ]
    on_head = [
        summary
        for summary in summaries
        if head[:7] in summary["body"] and not _codex_unfinished(summary["body"])
    ]
    # Copilot posts a review on the head when it couldn't run.
    by_review = any(
        review["commit"] == head and "unable to review" not in review["body"] for review in reviews
    )
    evidence = bool(on_head) or by_review
    pending = [name for name, check in checks.items() if check["bucket"] == "pending"]
    if checks:
        done = not pending and (
            evidence or all(check["bucket"] in GREEN for check in checks.values())
        )
    else:
        done = evidence
    verdict = next(filter(None, (_verdict(summary["body"]) for summary in on_head)), "")
    earlier = any(review["commit"] != head for review in reviews) or any(
        head[:7] not in summary["body"] for summary in summaries
    )
    return earlier, done, pending, verdict, by_review


class _Events:
    """Reports each key once, across polls. Holds reports back during a baseline.
    A REST poll baselines what REST shows, so an event `rest` can see is news
    once any poll has run, even before GraphQL has baselined the rest."""

    def __init__(self, memo: Json, out: list[Json]) -> None:
        self.memo = memo
        self.out = out
        self.seen = set(memo.get("seen") or [])
        self.baseline = not memo.get("baselined")
        self.rest_baseline = self.baseline and not memo.get("rest_baselined")

    def emit(
        self, key: str, text: str, on_me: bool = False, always: bool = False, rest: bool = False
    ) -> None:
        if key in self.seen:
            return
        self.seen.add(key)
        if always or not (self.rest_baseline if rest else self.baseline):
            self.out.append({"key": key, "text": text, "on_me": on_me})

    def drop(self, keys: set[str]) -> None:
        self.out[:] = [event for event in self.out if event["key"] not in keys]

    def save(self) -> None:
        self.memo["seen"] = sorted(self.seen)


def _talk_events(pr: Json, events: _Events, known_bots: tuple[str, ...], mine: set[str]) -> None:
    """Comments, reviews and thread replies from anyone but `mine` and the review
    bots, which _bot_comment_events groups. Edits are never events."""
    for comment in pr["comments"]:
        login = comment["login"]
        lowered = login.lower()
        if (
            login in mine
            or any(_is_bot(comment, bot) for bot in known_bots)
            or (comment["is_bot"] and any(bot in lowered for bot in STATUS_BOTS))
        ):
            continue
        events.emit(f"c:{comment['id']}", f"comment from {login}: {_snippet(comment['body'])}")
    for review in pr["reviews"]:
        login = review["login"]
        # A gating bot's review is its bot: event; roboomp posts from a user account.
        if login in mine or review["is_bot"] or any(_is_bot(review, bot) for bot in known_bots):
            continue
        if review["state"] in ("APPROVED", "CHANGES_REQUESTED") or review["body"].strip():
            state = str(review["state"]).lower().replace("_", " ")
            events.emit(
                f"c:{review['id']}",
                f"review from {login} ({state}): {_snippet(review['body'])}",
                rest=True,
            )
    for thread in pr["threads"]:
        for index, comment in enumerate(thread["comments"]):
            login = comment["login"]
            if login in mine or any(_is_bot(comment, bot) for bot in known_bots):
                continue
            kind = "new thread" if index == 0 else "thread reply"
            events.emit(
                f"c:{comment['id']}",
                f"{kind} from {login} on {thread['where']}: {_snippet(comment['body'])}",
            )


def _bot_comment_events(pr: Json, events: _Events, bot: str, counted: bool) -> None:
    """A review bot's new inline comments, as one event so its findings don't
    flood the history. `counted`: this poll's bot event already covers them."""
    mine = {pr["author"], pr["viewer"]}
    new = [
        f"c:{comment['id']}"
        for thread in pr["threads"]
        for comment in thread["comments"]
        if _is_bot(comment, bot)
        and comment["login"] not in mine
        and f"c:{comment['id']}" not in events.seen
    ]
    events.seen.update(new)
    if new and not counted:
        events.emit(f"bot-comments:{new[0]}", f"new {bot} review comments on {pr['head'][:7]}: {len(new)}")


def _reference(repo: str, number: int) -> re.Pattern[str]:
    return re.compile(
        rf"(?<![\w/#.-])#{number}\b|github\.com/{re.escape(repo)}/pull/{number}\b", re.IGNORECASE
    )


def _releases_since(repo: str, merged_at: str, run: Run) -> list[Json]:
    """Published releases from the merge on, oldest first. GitHub lists newest
    first, so paging stops at the first release older than the merge. Without a
    merge time only the first page is read."""
    found: list[Json] = []
    for page in itertools.count(1):
        path = f"repos/{repo}/releases?per_page={RELEASE_PAGE}&page={page}"
        batch = [release for release in _json(_gh(run, ["api", path]), list) if isinstance(release, dict)]
        published = [release for release in batch if release.get("published_at") and not release.get("draft")]
        found += [
            release
            for release in published
            if release.get("tag_name") and release["published_at"] >= merged_at
        ]
        if (
            not merged_at
            or len(batch) < RELEASE_PAGE
            or any(release["published_at"] < merged_at for release in published)
        ):
            break
    return sorted(found, key=lambda release: release["published_at"])


def _check_release(
    repo: str, number: int, merged: Json, memo: Json, result: Json, events: _Events, run: Run
) -> None:
    sha, merged_at = merged["sha"], merged.get("at") or ""
    result["next_poll_s"] = RELEASE_POLL_S
    candidates = _releases_since(repo, merged_at, run)
    # A tag's relation to the merge commit doesn't change, so each is asked once.
    compared = memo.setdefault("compared", {})
    reference = _reference(repo, number)
    to_confirm: tuple[str, str] | None = None
    for release in candidates:
        tag = release["tag_name"]
        if tag not in compared:
            path = f"repos/{repo}/compare/{sha}...{quote(tag, safe='/')}"
            try:
                compared[tag] = _gh(run, ["api", path, "--jq", ".status"]).strip()
            except GhError as error:
                if error.transient or "HTTP 404" not in str(error):
                    raise
                compared[tag] = "unrelated"
        status = compared[tag]
        if status in ("ahead", "identical"):
            result["fired"] = True
            result["fire_text"] = f"PR#{number} released in {tag}"
            return
        if to_confirm is None and reference.search(release.get("body") or ""):
            to_confirm = (tag, status)
    if to_confirm:
        tag, status = to_confirm
        result["confirm"] = True
        events.emit(
            f"confirm:{tag}",
            f"{tag} mentions #{number}, but its tag doesn't contain the merge commit "
            f"({status}): confirm by hand",
            on_me=True,
            always=True,
        )


def _gates_open(pr: Json, gate: set[str], open_threads: int) -> bool:
    """Everything `ready` needs except a bot's review. BEHIND and DIRTY block it,
    because neither can be merged."""
    if not pr["full"] or open_threads or pr["merge_state"] in ("BEHIND", "DIRTY"):
        return False
    return all(pr["checks"].get(name, {}).get("bucket", "pending") in GREEN for name in gate)


def _fire_on_activity(pr: Json, result: Json, events: _Events) -> None:
    """`activity` fires on this poll's first word from someone else: a comment,
    review or reply, a bot's finished review, a push by another author, a merge
    or a close. The baseline poll reports nothing old, except a close or merge:
    a wait created on a closed or merged PR fires at once, as no later poll would."""
    prefixes = ACTIVITY + (("head:",) if pr["author"] != pr["viewer"] else ())
    new = [event for event in events.out if event["key"].startswith(prefixes)]
    if not new:
        return
    result["fired"] = True
    if len(new) == 1:
        result["fire_text"] = new[0]["text"]
        events.drop({new[0]["key"]})
    else:
        result["fire_text"] = f"{len(new)} new, first {new[0]['text']}"


def _poll(cond: Json, memo: Json, result: Json, events: _Events, run: Run, now: float) -> None:
    repo, number, until = cond["repo"], cond["number"], cond["until"]
    merged = memo.get("merged") or {}
    if until == "released" and merged.get("sha"):
        result["merged"] = True
        _check_release(repo, number, merged, memo, result, events, run)
        return
    pr = _fetch(repo, number, memo, result, run, now)
    # What a fired wait is checked against later: a new head or a conflict takes it back.
    result.update(head=pr["head"], merge_state=pr["merge_state"], base=pr["base"])

    if pr["state"] == "MERGED":
        # A PR merged by pushing its commits to the base has no merge commit; its head is in the base.
        memo["merged"] = {"sha": pr["merge_sha"] or pr["head"], "at": pr["merged_at"]}
        result["merged"] = True
        if until == "merged":
            result["fired"] = True
            result["fire_text"] = f"PR#{number} merged"
            return
        if until == "released":
            _check_release(repo, number, memo["merged"], memo, result, events, run)
            return

    head = pr["head"]
    short = head[:7]
    is_open = pr["state"] == "OPEN"
    gating = _bots(cond)
    open_threads = sum(1 for thread in pr["threads"] if not thread["resolved"] and thread["comments"])
    # When the poller first saw the head: a commit can be pushed long after it was made.
    head_seen = (memo.get("head_seen") or {}).get(head, now)
    memo["head_seen"] = {head: head_seen}

    # Before any event, so a failure here doesn't report half a poll.
    required: set[str] = set()
    gate: set[str] = set()
    if pr["full"] and is_open:
        checks = pr["checks"]
        required = set(_required_names(repo, pr["base"], memo, run, now))
        required.update(name for name, check in checks.items() if check["required"])
        gate = required or {
            name
            for name, check in checks.items()
            if not any(_bot_check(name, check, bot) for bot in gating)
        }

    events.emit(f"head:{head}", f"new head {short}", rest=True)
    # `activity` counts the author's own words unless the author is you.
    _talk_events(pr, events, gating, {pr["viewer"]} if until == "activity" else {pr["author"], pr["viewer"]})

    reviewed: list[str] = []
    earlier_bots: list[str] = []
    pending_checks: list[str] = []
    # A bot that doesn't rerun on push (Copilot, Codex) has no check here. REST
    # shows no checks at all, so an open PR's are unknown.
    bot_pending = is_open and not pr["full"]
    for bot in gating:
        earlier, done, pending, verdict, by_review = _bot_activity(pr, bot)
        pending_checks.extend(pending)
        bot_pending = bot_pending or bool(pending)
        finished = f"bot:{bot}:{head}"
        first_report = done and finished not in events.seen
        if earlier and not done:
            earlier_bots.append(bot)
        if done:
            reviewed.append(bot)
            details = ", ".join(
                filter(None, (verdict, f"{open_threads} open threads" if open_threads else ""))
            )
            events.emit(
                finished,
                f"{bot} finished on {short}{': ' + details if details else ''}",
                rest=by_review,  # REST shows reviews, not checks or summaries
            )
        _bot_comment_events(pr, events, bot, counted=first_report)

    if pr["full"] and is_open:
        since = memo.get("pending_since") or {}
        memo["pending_since"] = {
            f"{head}:{name}": since.get(f"{head}:{name}", now) for name in pending_checks
        }
        for name in pending_checks:
            if now - memo["pending_since"][f"{head}:{name}"] > BOT_STUCK_S:
                events.emit(f"stuck:{head}:{name}", f"{name} pending for over an hour", on_me=True)
        label = "required check" if required else "check"
        for name in sorted(gate):
            if pr["checks"].get(name, {}).get("bucket") in FAILED:
                # A state that needs you: also on the first poll.
                events.emit(f"fail:{head}:{name}", f"{label} failed: {name}", on_me=True, always=True)

    merge_state = pr["merge_state"]
    if is_open and merge_state and merge_state != "UNKNOWN":
        # Once per head: a conflict a push didn't fix is news about the new head.
        if merge_state in ("BEHIND", "DIRTY") and memo.get("merge_state") != f"{head}:{merge_state}":
            text = (
                f"conflicts with {pr['base']}, rebase it"
                if merge_state == "DIRTY"
                else f"behind {pr['base']}, update the branch"
            )
            events.emit(
                f"merge:{merge_state}:{head}:{pr['base_oid']}", text, on_me=True, always=True
            )
        memo["merge_state"] = f"{head}:{merge_state}"

    memo["rest_baselined"] = True
    if pr["full"]:
        memo["baselined"] = True

    if pr["state"] == "CLOSED":
        events.emit(
            f"closed:{pr['closed_at']}",
            f"PR#{number} closed without merging",
            on_me=True,
            always=True,
        )
        result["next_poll_s"] = ENDED_POLL_S
        if until == "activity":
            _fire_on_activity(pr, result, events)
        return

    if until == "activity":
        if pr["state"] == "MERGED":
            events.emit(f"merged:{memo['merged']['sha']}", f"PR#{number} merged", always=True)
            result["next_poll_s"] = ENDED_POLL_S
        _fire_on_activity(pr, result, events)
        return

    fire_text = None
    if until == "review_on_head" and reviewed and not bot_pending:
        threads = f", {open_threads} open threads" if open_threads else ""
        fire_text = f"{', '.join(reviewed)} reviewed {short}{threads}"
    elif until == "ready" and not bot_pending and _gates_open(pr, gate, open_threads):
        bots = None
        age = now - head_seen
        # A bot that reviews every push and reviewed this PR before must review this
        # push too, whoever else did: Copilot on the head doesn't stand in for roboomp.
        every_push = [bot for bot in earlier_bots if bot not in ONCE_BOTS]
        if every_push:
            if is_open and age > BOT_STUCK_S:
                events.emit(
                    f"noreview:{head}",
                    f"{', '.join(every_push)} hasn't reviewed {short} in an hour",
                    on_me=True,
                )
        elif reviewed:
            bots = f"{', '.join(reviewed)} reviewed"
        elif is_open and age >= BOT_SETTLE_S:
            where = f" ({', '.join(earlier_bots)} reviewed an earlier head)" if earlier_bots else ""
            bots = f"no bot reviewed {short} in {BOT_SETTLE_S // 60} min{where}"
        if is_open:
            # Also while a bot is awaited: CI that can't pass is what you can act on first.
            runs = _ci_runs(repo, head, run)
            if runs["approval"]:
                events.emit(
                    f"approval:{head}",
                    f"CI on {short} waits for a maintainer's approval ({', '.join(runs['approval'])})",
                    always=True,  # A state, not news: the first poll must say why nothing fires.
                )
                bots = None
            elif not required and (runs["running"] or runs["failed"]):
                if runs["failed"]:
                    # The rollup is green, so nothing else reports it.
                    events.emit(
                        f"runfail:{head}",
                        f"CI failed on {short} ({', '.join(runs['failed'])})",
                        on_me=True,
                        always=True,
                    )
                bots = None
        if bots:
            green = f"{len(gate)} checks green" if gate else "no checks"
            fire_text = f"ready on {short}: {green}, {bots}, no open threads"
    if fire_text:
        result["fired"] = True
        result["fire_text"] = fire_text
        # The fire text says the same thing as the bots' own events.
        events.drop({f"bot:{bot}:{head}" for bot in reviewed})


def _invalid(cond: Json) -> str | None:
    repo, number = cond.get("repo"), cond.get("number")
    if not isinstance(repo, str) or not REPO_RE.fullmatch(repo):
        return f"bad repo: {repo!r}"
    if not isinstance(number, int) or isinstance(number, bool) or number <= 0:
        return f"bad PR number: {number!r}"
    if cond.get("until") not in UNTILS:
        return f"bad until: {cond.get('until')!r}"
    bots = cond.get("bots")
    if not (
        bots is None
        or isinstance(bots, str)
        or (isinstance(bots, list) and all(isinstance(bot, str) for bot in bots))
    ):
        return f"bad bots: {bots!r}"
    return None


def parse_pr(text: str) -> tuple[str, int] | None:
    """(repo, number) from a PR URL or O/R#N."""
    stripped = text.strip()
    match = PR_URL_RE.fullmatch(stripped) or PR_SHORT_RE.fullmatch(stripped)
    if not match or int(match.group(2)) <= 0:
        return None
    return match.group(1), int(match.group(2))


def pr_status(repo: str, number: int, *, run: Run = subprocess.run) -> Json:
    """One PR's title, state, review decision and overall checks. Raises GhError."""
    fields = "title,state,isDraft,reviewDecision,statusCheckRollup"
    pr = _json(_gh(run, ["pr", "view", str(number), "-R", repo, "--json", fields]), dict)
    state = PR_STATES.get(pr.get("state") or "")
    buckets = {_bucket(check) for check in pr.get("statusCheckRollup") or []}
    if buckets.intersection(FAILED):
        checks = "failing"
    elif "pending" in buckets:
        checks = "pending"
    else:
        checks = "passing" if buckets else None
    return {
        "title": pr.get("title"),
        "state": "draft" if state == "open" and pr.get("isDraft") else state,
        "review": (pr.get("reviewDecision") or "").lower() or None,
        "checks": checks,
    }


def poll(cond: Json, memo: Json, *, run: Run = subprocess.run, now: float | None = None) -> Json:
    """One poll of a PR condition. Never raises for gh failures: `error` and
    `transient` report them, and events found before the failure are kept."""
    now = time.time() if now is None else now
    result: Json = {
        "memo": memo,
        "events": [],
        "fired": False,
        "merged": False,
        "fire_text": None,
        "confirm": False,
        "error": None,
        "transient": False,
        "next_poll_s": DEFAULT_POLL_S,
    }
    invalid = _invalid(cond)
    if invalid:
        result["error"] = invalid
        return result
    events = _Events(memo, result["events"])
    try:
        _poll(cond, memo, result, events, run, now)
    except GhError as error:
        result["error"] = str(error)
        result["transient"] = error.transient
        # A hiccup on a slow path (REST fallback, a release check) still retries soon;
        # running out of budget doesn't.
        if error.transient and not RATE_LIMITED.search(str(error)):
            result["next_poll_s"] = min(result["next_poll_s"], RETRY_POLL_S)
    events.save()
    return result


def snapshot(repo: str, number: int, *, run: Run = subprocess.run) -> Json:
    """The PR's head and review threads, for verify_fix. Raises GhError."""
    pr = _fetch_graphql(repo, number, run)
    return {
        "head": pr["head"],
        "threads": [
            {
                "id": thread["id"],
                "resolved": thread["resolved"],
                "replies": max(len(thread["comments"]) - 1, 0),
            }
            for thread in pr["threads"]
        ],
    }


def verify_fix(repo: str, number: int, before: Json, *, run: Run = subprocess.run) -> tuple[bool, str]:
    """A fix turn succeeded when the head moved, or when every thread open before
    it got a new reply or was resolved. Raises GhError when the PR can't be read."""
    after = snapshot(repo, number, run=run)
    if after["head"] != before.get("head"):
        return True, f"new head {after['head'][:7]}"
    current = {thread["id"]: thread for thread in after["threads"]}
    was_open = [thread for thread in before.get("threads") or [] if not thread.get("resolved")]
    unanswered = 0
    for thread in was_open:
        now_thread = current.get(thread.get("id"))
        # A thread that disappeared was deleted, so nothing is left to answer.
        if now_thread is None or now_thread["resolved"]:
            continue
        if now_thread["replies"] <= thread.get("replies", 0):
            unanswered += 1
    if unanswered:
        return False, (
            f"head unchanged at {after['head'][:7]}, "
            f"{unanswered} of {len(was_open)} open threads unanswered"
        )
    if was_open:
        return True, f"all {len(was_open)} open threads answered or resolved"
    return True, "no open threads"
