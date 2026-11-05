"""Run the type-to-filter matcher in the same Qt JS engine as Plasma."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
QML = shutil.which("qml6") or shutil.which("qml") or "/usr/lib/qt6/bin/qml"


@unittest.skipUnless(Path(QML).is_file(), "Install the Qt 6 qml tool first")
class FilterTest(unittest.TestCase):
    def run_checks(self, checks):
        with tempfile.TemporaryDirectory(prefix="codexbar-filter-") as directory:
            root = Path(directory)
            shutil.copy2(ROOT / "contents/ui/Filter.js", root / "Filter.js")
            (root / "test.qml").write_text('''import QtQuick
import "Filter.js" as Filter
Item {
    function check(condition, message) {
        if (!condition) throw new Error(message)
    }
    function matches(query, fields, turns) {
        return Filter.matchesAll(fields, turns || [], Filter.words(query))
    }
    function marked(line) {
        var out = []
        var re = /<font color="#ffb300">([^<]*)<\\/font>/g
        var m
        while ((m = re.exec(line)) !== null) out.push(m[1])
        return out.join("|")
    }
    Component.onCompleted: {
        try {
''' + checks + '''
            console.log("PASS filter")
            Qt.exit(0)
        } catch (error) {
            console.error(error.message)
            Qt.exit(1)
        }
    }
}
''')
            env = dict(os.environ, QT_QPA_PLATFORM="offscreen",
                       QT_FORCE_STDERR_LOGGING="1", QT_LOGGING_TO_CONSOLE="1")
            result = subprocess.run([QML, str(root / "test.qml")], env=env,
                                    capture_output=True, text=True, timeout=15)
            output = result.stdout + result.stderr
            self.assertEqual(result.returncode, 0, output)
            self.assertIn("PASS filter", output)

    def test_every_word_must_match_somewhere_in_any_order(self):
        self.run_checks('''
            var prompt = "afaik the local version runs my pr already"
            var turns = ["The loop waits for a 20 s gap between requests"]
            check(matches("ai gap", [prompt], turns), "words split across a field and a turn")
            check(matches("gap ai", [prompt], turns), "word order is free")
            check(matches("  ai   gap ", [prompt], turns), "extra spaces are ignored")
            check(!matches("ai zebra", [prompt], turns), "an unmatched word rejects the session")
            check(!matches("aigap", [prompt], turns), "words typed together are one word")
        ''')

    def test_initials_need_consecutive_words(self):
        self.run_checks('''
            var prompt = "Az Auto-Research-csel eljutottunk egy jó eredményre, de itt az a bajom"
            check(!matches("aibajom", [prompt]), "word starts scattered across a sentence")
            check(!matches("aiba", [prompt]), "word starts scattered across a sentence")
            check(matches("bajom", [prompt]), "a substring still matches")
            check(matches("autor", [prompt]), "prefixes of a hyphenated word")
            check(matches("cmh", ["Check MCP history"]), "initials of consecutive words")
            check(matches("chemhis", ["Check MCP history"]), "prefixes of consecutive words")
            check(matches("cbav", ["CodexBarAgentView"]), "camelCase words")
            check(!matches("codexa", ["CodexBarAgentView"]), "a camelCase word skipped")
            check(!matches("chi", ["Check MCP history"]), "initials that skip a word")
            check(!matches("cmh", [], ["Check MCP history"]), "turns match substrings only")
        ''')

    def test_snippets_show_every_word_beyond_the_shown_label(self):
        self.run_checks('''
            var ws = Filter.words("ai gap")
            var fields = [
                { name: "windowTitle", label: "title", text: "STALE: Check usage fallback", shown: true },
                { name: "lastPrompt", label: "prompt", text: "afaik the local version runs", shown: false },
            ]
            var turns = [
                { user: false, text: "both waiting on the release" },
                { user: false, text: "it waits again" },
                { user: false, text: "The loop waits for a 20 s gap between requests" },
            ]
            var lines = Filter.snippets(fields, turns, ws)
            check(lines.length === 2, "prompt line and the gap turn only: " + lines.join(" / "))
            check(marked(lines[0]) === "ai", "prompt shows its word: " + lines[0])
            check(marked(lines[1]).split("|").indexOf("gap") !== -1,
                "the only turn holding gap is chosen: " + lines[1])

            var proto = Filter.snippets([], [{ user: false, text: "alpha" },
                { user: false, text: "constructor call" }], Filter.words("alpha constructor"))
            check(proto.length === 2 && marked(proto[1]) === "constructor",
                "a word named like an Object property still needs its turn: " + proto.join(" / "))

            var long = "A" + "x".repeat(250) + " Beta Charlie"
            var cut = Filter.snippets([{ name: "lastPrompt", label: "prompt", text: long, shown: false }],
                [], Filter.words("abc"))
            var plain = cut[0].replace(/<[^>]*>/g, "")
            check(marked(cut[0]) === "A|B|C" && plain.length < 200 && plain.indexOf(" … ") !== -1,
                "initials across a long word keep every letter in a short excerpt: " + plain)

            var far = "ai" + " filler".repeat(80) + " gap"
            var one = Filter.snippets([{ name: "lastPrompt", label: "prompt", text: far, shown: false }],
                [], ws)
            check(one.length === 1 && marked(one[0]) === "ai|gap",
                "far-apart words both stay in the excerpt: " + one[0])
            check(one[0].indexOf(" … ") !== -1, "the text between them is elided")

            var turn = Filter.snippets(
                [{ name: "windowTitle", label: "title", text: "ai work", shown: true }],
                [{ user: false, text: far }], ws)
            check(turn.length === 1 && marked(turn[0]) === "gap",
                "a turn shown for gap centers on gap, not the title's ai: " + turn[0])
        ''')


if __name__ == "__main__":
    unittest.main()
