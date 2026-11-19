"""Exercise config pages through the real QML and process plugin."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
QML = shutil.which("qml6") or shutil.which("qml") or "/usr/lib/qt6/bin/qml"
PLUGIN = ROOT / "build/package/contents/ui/process"
KCMUTILS = Path("/usr/lib/qt6/qml/org/kde/kcmutils/qmldir")


@unittest.skipUnless(Path(QML).is_file() and (PLUGIN / "libcodexbarprocess.so").is_file(),
                     "Build the native package and install the Qt 6 qml tool first")
class IntegrationActionsTest(unittest.TestCase):
    def test_bursts_serialize_but_later_click_runs(self):
        with tempfile.TemporaryDirectory(prefix="codexbar-actions-") as directory:
            root = Path(directory)
            (root / "ui").mkdir()
            (root / "scripts").mkdir()
            for name in ("configAgents.qml", "Command.js"):
                shutil.copy2(ROOT / "contents/ui" / name, root / "ui" / name)
            (root / "ui/process").symlink_to(PLUGIN, target_is_directory=True)
            # Replace only the external installer: never mutate real desktop settings.
            (root / "scripts/install_integration.py").write_text(
                "import pathlib,time\n"
                "p=pathlib.Path(__file__).with_name('calls')\n"
                "with p.open('a') as f: f.write('call\\n')\n"
                "time.sleep(0.2)\n"
                "print(len(p.read_text().splitlines()))\n"
            )
            (root / "test.qml").write_text('''import QtQuick
Item {
    id: root
    property int stage: 0
    Loader {
        id: page
        source: "ui/configAgents.qml"
        onLoaded: { item._runHookCommand("--status"); item._runHookCommand("--status") }
    }
    Timer {
        interval: 50; running: true; repeat: true
        onTriggered: {
            if (!page.item || page.item.hookLoading
                    || page.item.hookStatus === "checking…" || page.item.hookStatus === "") return
            var expected = root.stage + 1
            if (!page.item || page.item.hookStatus !== String(expected)) {
                console.error("FAIL integration commands overlapped; expected " + expected
                    + " invocation(s), got " + (page.item ? page.item.hookStatus : "no page"))
                Qt.exit(1); return
            }
            if (root.stage === 1) {
                console.log("PASS integration bursts serialize; subsequent click runs fresh")
                Qt.exit(0); return
            }
            root.stage = 1
            page.item.hookStatus = ""
            page.item._runHookCommand("--status")
            page.item._runHookCommand("--status")
        }
    }
}
''')
            # configAgents.qml reads agents.json from the state dir: keep it scratch.
            env = dict(os.environ, QT_QPA_PLATFORM="offscreen",
                       QT_FORCE_STDERR_LOGGING="1", QT_LOGGING_TO_CONSOLE="1",
                       XDG_STATE_HOME=str(root / "state"))
            result = subprocess.run([QML, str(root / "test.qml")], env=env,
                                    capture_output=True, text=True, timeout=15)
            output = result.stdout + result.stderr
            self.assertEqual(result.returncode, 0, output)
            self.assertIn("PASS integration bursts serialize", output)
            self.assertEqual((root / "scripts/calls").read_text().splitlines(),
                             ["call", "call"])


@unittest.skipUnless(Path(QML).is_file() and (PLUGIN / "libcodexbarprocess.so").is_file() and KCMUTILS.is_file(),
                     "Build the native package and install the Qt 6 qml tool and KCMUtils first")
class AboutCompanionsTest(unittest.TestCase):
    def test_about_lists_what_the_helper_found(self):
        with tempfile.TemporaryDirectory(prefix="codexbar-about-") as directory:
            root = Path(directory)
            (root / "ui").mkdir()
            (root / "scripts").mkdir()
            for name in ("configAbout.qml", "Command.js"):
                shutil.copy2(ROOT / "contents/ui" / name, root / "ui" / name)
            (root / "ui/process").symlink_to(PLUGIN, target_is_directory=True)
            (root / "scripts/codexbar_companions.py").write_text(
                "import json\n"
                "print(json.dumps({'companions': ["
                "{'id': 'tern-awui', 'name': 'tern-awui', 'url': 'https://example.com/a', 'benefit': 'b',"
                " 'installed': True, 'status': 'Installed'},"
                "{'id': 'omp-compact', 'name': 'omp-compact', 'url': 'https://example.com/c', 'benefit': 'c',"
                " 'installed': False, 'status': 'Not found'}]}))\n"
            )
            (root / "test.qml").write_text('''import QtQuick
Item {
    Loader { id: page; source: "ui/configAbout.qml" }
    Timer {
        interval: 50; running: true; repeat: true
        onTriggered: {
            if (!page.item || page.item.companions.length === 0) return
            var shown = page.item.companions.map(function(c) { return c.name + ": " + c.status })
            console.log("SHOWN " + shown.join("; "))
            Qt.exit(0)
        }
    }
    Timer { interval: 10000; running: true; onTriggered: { console.error("FAIL no companions"); Qt.exit(1) } }
}
''')
            env = dict(os.environ, QT_QPA_PLATFORM="offscreen",
                       QT_FORCE_STDERR_LOGGING="1", QT_LOGGING_TO_CONSOLE="1")
            result = subprocess.run([QML, str(root / "test.qml")], env=env,
                                    capture_output=True, text=True, timeout=15)
            output = result.stdout + result.stderr
            self.assertEqual(result.returncode, 0, output)
            self.assertIn("SHOWN tern-awui: Installed; omp-compact: Not found", output)


if __name__ == "__main__":
    unittest.main()
