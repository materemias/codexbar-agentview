#!/usr/bin/env python3
"""Runs the test suite in parallel: one test at a time per worker process.

    python tests/run.py              every test, a worker per CPU
    python tests/run.py -j 4 -v      four workers, one line per test
    python tests/run.py test_codexbar_waiting test_codexbar_fetch.FetchTests

Workers are long-lived, so each imports a module once, and they pull tests one
by one, so a slow one (a QML test runs `qml` for over a second) never holds
up a queue of others. Tests already isolate their state in temporary
directories, so they may run side by side; a module's or class's fixtures run
around each test. Same names and output shape as `python -m unittest discover
-s tests`, which still works.
"""

from __future__ import annotations

import argparse
import multiprocessing
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
# A result's lists, worst first, with the summary's names for their counts.
OUTCOMES = {"failures": "failures", "errors": "errors", "unexpectedSuccesses": "unexpected successes",
            "skipped": "skipped", "expectedFailures": "expected failures"}
LETTERS = {"failures": "F", "errors": "E", "unexpectedSuccesses": "u", "skipped": "s", "expectedFailures": "x"}
WORDS = {"failures": "FAIL", "errors": "ERROR", "unexpectedSuccesses": "unexpected success", "skipped": "skipped",
         "expectedFailures": "expected failure"}
FAILING = ("failures", "errors", "unexpectedSuccesses")


def _tests(suite: unittest.TestSuite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from _tests(item)
        else:
            yield item


def _run(name: str) -> tuple[str, str, str, float]:
    """In a worker: runs one test by name; (name, outcome, detail, seconds). The
    detail is the traceback with what the test printed, or a skip's reason."""
    started = time.monotonic()
    result = unittest.TestResult()
    # A test's prints and its child processes' (qml, bun) go with its failure,
    # not across other workers' output: fds 1 and 2 point at a file meanwhile.
    with tempfile.TemporaryFile() as output:
        sys.stdout.flush()
        sys.stderr.flush()
        saved = os.dup(1), os.dup(2)
        os.dup2(output.fileno(), 1)
        os.dup2(output.fileno(), 2)
        try:
            unittest.TestLoader().loadTestsFromName(name).run(result)
        finally:
            sys.stdout.flush()
            sys.stderr.flush()
            for fd, copy in zip((1, 2), saved):
                os.dup2(copy, fd)
                os.close(copy)
        output.seek(0)
        printed = output.read().decode(errors="replace").strip()
    seconds = time.monotonic() - started
    for outcome in OUTCOMES:
        for entry in getattr(result, outcome):
            test, detail = entry if isinstance(entry, tuple) else (entry, "")
            if outcome in FAILING:
                # A fixture's error names its class or module instead of the test.
                if test.id() != name:
                    detail = f"in {test}\n{detail}"
                if printed:
                    detail = f"{detail}\nOutput:\n{printed}\n"
            return name, outcome, detail, seconds
    return name, "ok", "", seconds


def _selected(names: list[str]) -> tuple[list[str], list[str]]:
    """Test names to run, and errors from loading them."""
    loader = unittest.TestLoader()
    suite = (loader.loadTestsFromNames(names) if names
             else loader.discover(str(TESTS), top_level_dir=str(TESTS)))
    found, errors = [], list(loader.errors)
    for test in _tests(suite):
        # An import that failed comes back as a stand-in test that raises it;
        # loader.errors already holds its traceback.
        if type(test).__name__ != "_FailedTest":
            found.append(test.id())
    return found, errors


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("names", nargs="*", help="modules, classes or tests (default: discover tests/)")
    parser.add_argument("-j", "--jobs", type=int, default=os.cpu_count() or 1, help="worker processes")
    parser.add_argument("-v", "--verbose", action="store_true", help="a line per test")
    args = parser.parse_args(argv)
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    # A test's output may hold what the console's code page lacks (Windows' cp1252).
    sys.stdout.reconfigure(errors="backslashreplace")
    sys.path.insert(0, str(TESTS))

    started = time.monotonic()
    names, load_errors = _selected(args.names)
    counts = dict.fromkeys(OUTCOMES, 0)
    reports = [("ERROR", "loading tests", detail) for detail in load_errors]
    counts["errors"] = len(load_errors)
    jobs = max(1, min(args.jobs, len(names)))
    # spawn, not fork: a worker starts clean of the parent's threads and imports.
    with multiprocessing.get_context("spawn").Pool(jobs) as pool:
        for name, outcome, detail, seconds in pool.imap_unordered(_run, names):
            if outcome != "ok":
                counts[outcome] += 1
            if outcome in ("failures", "errors", "unexpectedSuccesses"):
                reports.append((WORDS[outcome], name, detail))
            if args.verbose:
                note = f": {detail}" if outcome == "skipped" and detail else ""
                print(f"{name} ... {WORDS.get(outcome, 'ok')}{note} ({seconds:.2f}s)", flush=True)
            else:
                print(LETTERS.get(outcome, "."), end="", flush=True)
    if not args.verbose:
        print()
    for word, name, detail in reports:
        print(f"{'=' * 70}\n{word}: {name}\n{'-' * 70}\n{detail}")
    print(f"{'-' * 70}\nRan {len(names)} tests in {time.monotonic() - started:.3f}s with {jobs} workers\n")
    failed = sum(counts[key] for key in FAILING)
    extras = ", ".join(f"{OUTCOMES[key]}={count}" for key, count in counts.items() if count)
    print(("FAILED" if failed else "OK") + (f" ({extras})" if extras else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
