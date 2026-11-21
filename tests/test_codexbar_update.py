from __future__ import annotations

import http.server
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "contents" / "scripts" / "codexbar_update.py"
sys.path.insert(0, str(SCRIPT_PATH.parent))
SPEC = importlib.util.spec_from_file_location("codexbar_update", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
update = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(update)

RELEASE = {"tag_name": "v1.1.0", "html_url": "https://github.com/materemias/codexbar-agentview/releases/tag/v1.1.0"}


class Scratch(unittest.TestCase):
    """A state directory, a git without the user's configuration, and no request to GitHub."""

    def setUp(self) -> None:
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        (self.root / "gitconfig").write_text("[user]\n\tname = t\n\temail = t@example.invalid\n")
        self.answer = self.root / "latest.json"
        self.answer.write_text(json.dumps(RELEASE))
        env = {
            "XDG_STATE_HOME": str(self.root / "state"), "GIT_CONFIG_GLOBAL": str(self.root / "gitconfig"),
            "GIT_CONFIG_NOSYSTEM": "1", update.URL_ENV: self.answer.as_uri(),
        }
        patch = mock.patch.dict(os.environ, env)
        patch.start()
        self.addCleanup(patch.stop)
        self.state = self.root / "state" / "codexbar-agentview"


class VersionTests(Scratch):
    def test_versions_compare_as_numbers(self) -> None:
        self.assertTrue(update.is_newer("1.10.0", "1.9.9"))
        self.assertTrue(update.is_newer("2.0.0", "1.99.99"))
        self.assertFalse(update.is_newer("1.0.0", "1.0.0"))
        # A development checkout ahead of the release isn't behind it.
        self.assertFalse(update.is_newer("1.0.0", "1.1.0"))
        self.assertFalse(update.is_newer("1.1.0-rc1", "1.0.0"))
        self.assertFalse(update.is_newer("1.1.0", ""))
        self.assertEqual(update.parse_version("v1.2.3"), (1, 2, 3))
        self.assertIsNone(update.parse_version(None))

    def test_the_installed_version_is_the_manifests_top_one(self) -> None:
        (self.root / "tern").mkdir()
        (self.root / "tern" / "plugin.toml").write_text(
            'id = "x"\nversion = "1.2.3"\n\n[[blocks]]\nversion = "9.9.9"\n')
        self.assertEqual(update.installed_version(self.root), "1.2.3")
        (self.root / "tern" / "plugin.toml").write_text('id = "x"\n[[blocks]]\nversion = "9.9.9"\n')
        self.assertEqual(update.installed_version(self.root), "")

    def test_drafts_prereleases_and_odd_pages_are_refused(self) -> None:
        for body, message in (
            ({**RELEASE, "prerelease": True}, "no published release"),
            ({**RELEASE, "tag_name": "nightly"}, "isn't a version"),
        ):
            self.answer.write_text(json.dumps(body))
            with self.assertRaisesRegex(ValueError, message):
                update.fetch_latest(self.answer.as_uri())
        self.answer.write_text(json.dumps({**RELEASE, "html_url": "javascript:alert(1)"}))
        self.assertEqual(update.fetch_latest(self.answer.as_uri())["url"], update.RELEASES_URL)

    def test_the_request_names_no_user(self) -> None:
        seen: dict = {}

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - the stdlib's name
                if self.path != "/latest":
                    self.send_error(404, "Not Found")
                    return
                seen.update(self.headers.items())
                body = json.dumps(RELEASE).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args) -> None:
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        url = f"http://127.0.0.1:{server.server_address[1]}/latest"
        no_proxy = mock.patch.dict(os.environ, {"no_proxy": "*", "NO_PROXY": "*"})
        no_proxy.start()
        self.addCleanup(no_proxy.stop)
        self.assertEqual(update.fetch_latest(url)["latest"], "1.1.0")
        self.assertEqual(seen["User-Agent"], "codexbar-agentview")
        self.assertEqual(seen["Accept"], "application/vnd.github+json")
        self.assertLessEqual(set(seen), {"Host", "User-Agent", "Accept", "Accept-Encoding", "Connection"})
        # GitHub answered: no "couldn't reach" for a missing release or a rate limit.
        with mock.patch.dict(os.environ, {update.URL_ENV: url + "-missing"}), update.held_lock():
            self.assertEqual(update.latest_release()[0]["error"], "GitHub answered 404 Not Found")


class CacheTests(Scratch):
    def release(self, now: float, max_age: float = update.SUCCESS_TTL) -> dict:
        with update.held_lock():
            return update.latest_release(max_age, now=now)[0]

    def test_a_success_is_reused_for_a_day(self) -> None:
        with mock.patch.object(update, "fetch_latest", wraps=update.fetch_latest) as fetch:
            self.assertEqual(self.release(1000)["latest"], "1.1.0")
            self.answer.write_text(json.dumps({**RELEASE, "tag_name": "v1.2.0"}))
            self.assertEqual(self.release(1000 + update.SUCCESS_TTL - 1)["latest"], "1.1.0")
            self.assertEqual(fetch.call_count, 1)
            self.assertEqual(self.release(1000 + update.SUCCESS_TTL)["latest"], "1.2.0")
            # --max-age 0 asks again at once.
            self.assertEqual(self.release(1000 + update.SUCCESS_TTL, max_age=0)["latest"], "1.2.0")
            self.assertEqual(fetch.call_count, 3)

    def test_a_failure_waits_an_hour_and_keeps_the_release_known(self) -> None:
        self.release(1000)
        self.answer.unlink()
        failed = self.release(1000 + update.SUCCESS_TTL)
        self.assertIn("Couldn't reach GitHub", failed["error"])
        self.assertEqual(failed["latest"], "1.1.0")
        self.answer.write_text(json.dumps({**RELEASE, "tag_name": "v1.2.0"}))
        with mock.patch.object(update, "fetch_latest", wraps=update.fetch_latest) as fetch:
            # Even a forced check waits out the failure's hour.
            self.assertIn("error", self.release(1000 + update.SUCCESS_TTL + update.FAILURE_TTL - 1, max_age=0))
            self.assertEqual(fetch.call_count, 0)
            again = self.release(1000 + update.SUCCESS_TTL + update.FAILURE_TTL)
        self.assertEqual((again["latest"], "error" in again), ("1.2.0", False))

    def test_a_clock_set_back_asks_again(self) -> None:
        self.release(5000)
        with mock.patch.object(update, "fetch_latest", wraps=update.fetch_latest) as fetch:
            self.release(4000)
        self.assertEqual(fetch.call_count, 1)

    def test_concurrent_checks_make_one_request(self) -> None:
        # Another poller holds the lock while it asks: this check waits, then reuses its answer.
        holding, release = threading.Event(), threading.Event()

        def other() -> None:
            with update.held_lock():
                holding.set()
                release.wait(5)
                update.latest_release()

        thread = threading.Thread(target=other)
        thread.start()
        self.addCleanup(thread.join)
        holding.wait(5)
        with mock.patch.object(update, "fetch_latest", wraps=update.fetch_latest) as fetch:
            threading.Timer(0.2, release.set).start()
            started = time.monotonic()
            answer = update.status(checkout=self.root)
            waited = time.monotonic() - started
        self.assertGreaterEqual(waited, 0.15)
        self.assertEqual(answer["latest"], "1.1.0")
        # One request, the other poller's (the patch counts every thread's); this check made none.
        self.assertEqual(fetch.call_count, 1)


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True).stdout.strip()


class UpdateTests(Scratch):
    """A clone at v1.0.0 of an origin that has v1.1.0."""

    def setUp(self) -> None:
        super().setUp()
        origin, work = self.root / "origin.git", self.root / "work"
        git(self.root, "init", "-q", "--bare", "-b", "master", str(origin))
        git(self.root, "init", "-q", "-b", "master", str(work))
        for version in ("1.0.0", "1.1.0"):
            self.manifest(work, version)
            git(work, "add", "-A")
            git(work, "commit", "-q", "-m", version)
            git(work, "tag", f"v{version}")
        git(work, "push", "-q", "--tags", str(origin), "master")
        self.clone = self.root / "clone"
        git(self.root, "clone", "-q", str(origin), str(self.clone))
        git(self.clone, "reset", "-q", "--hard", "v1.0.0")
        # As a clone made before the release: the tag comes with the update's fetch.
        git(self.clone, "tag", "-d", "v1.1.0")
        self.work, self.origin = work, origin

    @staticmethod
    def manifest(folder: Path, version: str) -> None:
        (folder / "tern").mkdir(parents=True, exist_ok=True)
        (folder / "tern" / "plugin.toml").write_text(f'id = "codexbar-agentview"\nversion = "{version}"\n')

    def plan(self) -> dict:
        return update.status(checkout=self.clone)["update"]

    def apply(self) -> tuple[int, dict]:
        return update.apply_update(self.clone)

    def test_a_clean_branch_fast_forwards_to_the_release(self) -> None:
        found = update.status(checkout=self.clone)
        self.assertEqual((found["current"], found["latest"], found["newer"]), ("1.0.0", "1.1.0", True))
        self.assertEqual(found["update"]["possible"], True, found)
        self.assertIn("merge --ff-only v1.1.0", found["update"]["command"])
        (self.clone / "notes.txt").write_text("untracked stays\n")
        code, answer = self.apply()
        self.assertEqual((code, answer), (0, {"ok": True, "updated": True, "from": "1.0.0", "to": "1.1.0", "branch": "master"}))
        self.assertEqual(update.installed_version(self.clone), "1.1.0")
        self.assertEqual(git(self.clone, "rev-parse", "HEAD"), git(self.work, "rev-parse", "v1.1.0"))
        self.assertTrue((self.clone / "notes.txt").exists())
        self.assertEqual(update.status(checkout=self.clone)["newer"], False)

    def test_uncommitted_changes_refuse(self) -> None:
        self.manifest(self.clone, "1.0.1")
        self.assertIn("uncommitted changes", self.plan()["reason"])
        code, answer = self.apply()
        self.assertEqual((code, answer["ok"]), (1, False))
        # PowerShell 5.1, Windows' default shell, has no `&&`.
        chain = "; if ($?) { git -C" if os.name == "nt" else " && git -C"
        self.assertIn("fetch --tags origin" + chain, answer["command"])
        self.assertIn("1.0.1", (self.clone / "tern" / "plugin.toml").read_text())

    def test_a_detached_head_refuses(self) -> None:
        git(self.clone, "checkout", "-q", "--detach")
        plan = self.plan()
        self.assertEqual((plan["possible"], plan["reason"]), (False, "HEAD is detached, so there is no branch to move forward."))
        self.assertIn("checkout v1.1.0", plan["command"])
        self.assertEqual(self.apply()[0], 1)

    def test_a_diverged_branch_refuses(self) -> None:
        (self.clone / "mine.txt").write_text("local work\n")
        git(self.clone, "add", "mine.txt")
        git(self.clone, "commit", "-q", "-m", "local")
        head = git(self.clone, "rev-parse", "HEAD")
        plan = self.plan()
        self.assertEqual(plan["possible"], False)
        self.assertIn("can't fast-forward", plan["reason"])
        self.assertTrue(plan["command"].endswith("merge v1.1.0"), plan)
        self.assertEqual(self.apply()[0], 1)
        self.assertEqual(git(self.clone, "rev-parse", "HEAD"), head)

    def test_a_folder_that_isnt_its_own_checkout_refuses(self) -> None:
        plain = self.root / "plain"
        self.manifest(plain, "1.0.0")
        self.assertIn("isn't a git checkout", update.status(checkout=plain)["update"]["reason"])
        # Nor does a folder inside another repository count.
        nested = self.clone / "nested"
        self.manifest(nested, "1.0.0")
        self.assertIn("isn't a git checkout", update.status(checkout=nested)["update"]["reason"])
        self.assertEqual(update.apply_update(plain)[0], 1)

    def test_a_tag_origin_lacks_refuses_and_is_fetched_once_a_day(self) -> None:
        self.answer.write_text(json.dumps({**RELEASE, "tag_name": "v1.2.0"}))
        plan = self.plan()
        self.assertEqual((plan["possible"], plan["reason"]), (False, "origin has no tag v1.2.0."))
        # The next checks reuse the day's answer and fetch nothing: Update is offered, and
        # --apply fetches and judges.
        with mock.patch.object(update, "_git", wraps=update._git) as run:
            self.assertEqual(self.plan()["possible"], True)
        self.assertNotIn("fetch", [call.args[1] for call in run.call_args_list])
        code, answer = self.apply()
        self.assertEqual((code, answer["error"]), (1, "origin has no tag v1.2.0."))

    def test_up_to_date_has_nothing_to_apply(self) -> None:
        self.answer.write_text(json.dumps({**RELEASE, "tag_name": "v1.0.0"}))
        found = update.status(checkout=self.clone)
        self.assertEqual((found["newer"], found["update"]["possible"]), (False, False))
        code, answer = self.apply()
        self.assertEqual((code, answer["error"]), (1, "1.0.0 is the latest release."))


class CommandLineTests(Scratch):
    def test_check_prints_json(self) -> None:
        # Not newer than this checkout, so no git command reaches its real origin.
        self.answer.write_text(json.dumps({**RELEASE, "tag_name": "v0.0.1"}))
        env = dict(os.environ, HOME=str(self.root))
        done = subprocess.run([sys.executable, str(SCRIPT_PATH), "check"], capture_output=True, text=True,
                              env=env, check=False, timeout=30)
        self.assertEqual(done.returncode, 0, done.stderr)
        answer = json.loads(done.stdout)
        self.assertEqual((answer["latest"], answer["newer"]), ("0.0.1", False))
        self.assertEqual(set(answer) - {"error"}, {"current", "latest", "newer", "url", "checkedAt", "update"})
        self.assertTrue((self.state / update.CACHE_NAME).is_file())


if __name__ == "__main__":
    unittest.main()
