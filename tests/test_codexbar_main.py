"""Run the actual applet root with isolated inputs and the native process plugin."""
import datetime as dt
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parents[1]
QML = shutil.which("qml6") or shutil.which("qml") or "/usr/lib/qt6/bin/qml"
PLUGIN = ROOT / "build/package/contents/ui/process"


@unittest.skipUnless(Path(QML).is_file() and (PLUGIN / "libcodexbarprocess.so").is_file(),
                     "Build the native package and install the Qt 6 qml tool first")
class MainLifecycleTest(unittest.TestCase):
    def applet(self, scenario, helper_case=None, views=False, real_fetch=False,
               scenario_seconds=6):
        with tempfile.TemporaryDirectory(prefix="codexbar-main-") as directory:
            root = Path(directory)

            def put(name, text):
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text)

            (root / "ui").mkdir()
            for name in ("main.qml", "Command.js", "Usage.js", "Notifications.js", "Filter.js"):
                shutil.copy2(ROOT / "contents/ui" / name,
                             root / "ui" / ("Main.qml" if name == "main.qml" else name))
            (root / "ui/process").symlink_to(PLUGIN, target_is_directory=True)
            if views:
                # The real panel item and Agents rows, hosted in a bare popup.
                (root / "icons").symlink_to(ROOT / "contents/icons", target_is_directory=True)
                for name in ("CompactRepresentation.qml", "AgentsSection.qml", "SectionCard.qml",
                             "AgentTitle.qml", "TurnBubble.qml"):
                    shutil.copy2(ROOT / "contents/ui" / name, root / "ui" / name)
                put("ui/FullRepresentation.qml", "import QtQuick\nItem {\n"
                    "property bool tooltipHovered: false\n"
                    "AgentsSection { objectName: 'agentsSection'; width: 600 }\n}\n")
            else:
                for name in ("FullRepresentation", "CompactRepresentation"):
                    put(f"ui/{name}.qml", "import QtQuick\nItem { property bool tooltipHovered: false }\n")
            if helper_case and "waitingView" in helper_case:
                put("state/codexbar-agentview/waiting_view.json", json.dumps(helper_case["waitingView"]))
            # Replace desktop integration only; root bindings, callbacks, reducers,
            # command construction, and native subprocess execution remain real.
            module = "imports/org/kde/plasma/plasmoid/"
            put(module + "qmldir", "module org.kde.plasma.plasmoid\n"
                "singleton Plasmoid 1.0 Plasmoid.qml\nPlasmoidItem 1.0 PlasmoidItem.qml\n")
            configuration = []
            schema = ET.parse(ROOT / "contents/config/main.xml")
            for entry in schema.findall(".//{*}entry"):
                name, kind = entry.attrib["name"], entry.attrib["type"]
                default = entry.findtext("{*}default", "")
                if kind == "Bool":
                    value = name in ("enableCodex", "ompModeNotifications") or (
                        name == "usageNotifications" and bool((helper_case or {}).get("usageNotifications")))
                elif kind == "Int":
                    value = int(default or 0)
                elif kind == "StringList":
                    value = default.split(",") if default else []
                else:
                    value = "/backend-a" if name == "cliPath" else default
                if real_fetch:
                    if kind == "Bool":
                        value = name == "enableKilo" or (
                            name == "usageNotifications"
                            and bool((helper_case or {}).get("usageNotifications")))
                    elif name == "cliPath":
                        value = str(root / "bin/codexbar")
                    elif name == "refreshSeconds":
                        value = 3600
                value = (helper_case or {}).get("configuration", {}).get(name, value)
                configuration.append(f"property var {name}: {json.dumps(value)}")
            put(module + "Plasmoid.qml", "pragma Singleton\nimport QtQuick\nQtObject {\n"
                "signal activated()\nproperty int formFactor: 2\nproperty string globalShortcut: ''\n"
                "property QtObject configuration: QtObject {\n"
                + "\n".join(configuration) + "\n}\n}\n")
            put(module + "PlasmoidItem.qml", """import QtQuick
Item {
    property bool hideOnWindowDeactivate: true
    property bool expanded: false
    property Component preferredRepresentation
    property Component fullRepresentation
    property Component compactRepresentation
    property Item compactRepresentationItem: null
    property string toolTipMainText
    property string toolTipSubText
    property int toolTipTextFormat
}
""")
            module = "imports/org/kde/plasma/core/"
            put(module + "qmldir", "module org.kde.plasma.core\nsingleton Types 1.0 Types.qml\n")
            put(module + "Types.qml", "pragma Singleton\nimport QtQuick\nQtObject { enum FormFactor { Planar } }\n")
            module = "imports/org/kde/taskmanager/"
            put(module + "qmldir", "module org.kde.taskmanager\n"
                "TasksModel 1.0 TasksModel.qml\nVirtualDesktopInfo 1.0 VirtualDesktopInfo.qml\n")
            put(module + "TasksModel.qml", "import QtQuick\nListModel {\n"
                "enum Mode { GroupDisabled }\nproperty int groupMode: 0\n}\n")
            put(module + "VirtualDesktopInfo.qml", "import QtQuick\nQtObject {\n"
                "property var desktopIds: []\nproperty var currentDesktop: 0\n}\n")
            put("scripts/scenario.json", json.dumps(helper_case))
            put("scripts/codexbar_fetch.py", '''import datetime, json, pathlib, sys, time
root = pathlib.Path(__file__).parent
args = sys.argv[1:]
backend = args[args.index("--cli-path") + 1]
cache = "--cache-only" in args
calls = root / "calls"
with calls.open("a") as output:
    output.write(json.dumps({"backend": backend, "cache": cache}) + "\\n")
history = [json.loads(line) for line in calls.read_text().splitlines()]
number = len(history)
normal = sum(call["backend"] == backend and not call["cache"] for call in history)
time.sleep(0.15)
test = json.loads((root / "scenario.json").read_text())
snapshots = (test or {}).get("freshSnapshots", {}).get(backend, [])
if snapshots and not cache:
    print(json.dumps(snapshots[min(normal - 1, len(snapshots) - 1)]))
    sys.exit(0)
if test and test.get("failure") is not None and normal >= 2:
    if cache:
        if test.get("recoveryFails"):
            print("cache validation failed", file=sys.stderr)
            sys.exit(1)
        print(json.dumps({"providers": test.get("recoveryProviders", []),
            "cacheOnly": True, "fatal": None, "codexRotation": None}))
    elif test["failure"] == "fatal":
        print(json.dumps({"providers": [], "cacheOnly": False,
            "fatal": {"message": "credentials changed"}, "codexRotation": None}))
        sys.exit(1)
    else:
        print(test["failure"])
        print("transport exploded", file=sys.stderr)
        sys.exit(1)
    sys.exit(0)
if backend == "/missing":
    print("selected backend is unavailable", file=sys.stderr)
    sys.exit(1)
if not cache and normal == 6:
    print("temporary fetch failure", file=sys.stderr)
    sys.exit(1)
mode = {1: "FILL", 2: "TARGET", 4: "BURN", 5: "BURN", 7: "IDLE", 8: "IDLE"}.get(normal)
rotation = None if cache or mode is None else {
    "mode": mode, "operatingMode": "confirm" if normal == 8 else "auto",
    "stalled": normal == 5}
now = datetime.datetime.now(datetime.timezone.utc).isoformat()
scope = "b" if test and test.get("switchScope") and normal >= 2 else "a"
percent = 80 if test and test.get("usageNotifications") else number
print(json.dumps({"updatedAt": now, "fatal": None, "cacheOnly": cache,
    "providers": [{"id": "codex", "ok": True, "sourceScope": scope * 64,
        "stale": cache, "primary": {"usedPercent": percent}, "updatedAt": now}],
    "codexRotation": rotation, "cliVersion": backend}))
''')
            if real_fetch:
                # Keep the real helper, normalization, cache and flock claim path.
                # Kilo has no Meridian/Claude or forecast HTTP fallback.
                for name in ("codexbar_fetch.py", "state_paths.py"):
                    shutil.copy2(ROOT / "contents/scripts" / name, root / "scripts" / name)
                put("bin/codexbar", '''#!/usr/bin/env python3
import datetime, json, pathlib, sys
root = pathlib.Path(__file__).parent.parent
if sys.argv[1:] == ["--version"]:
    print("CodexBar 0.56.3")
else:
    assert sys.argv[1:4] == ["usage", "--json", "--provider"], sys.argv
    with (root / "scripts/calls").open("a") as output:
        output.write(json.dumps({"provider": sys.argv[4]}) + "\\n")
    print(json.dumps([{"usage": {
        "updatedAt": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "primary": {"usedPercent": int((root / (
            "replacement-percent" if pathlib.Path(__file__).name.endswith("-b")
            else "provider-percent")).read_text()),
                    "windowMinutes": 300}}}]))
''')
                (root / "bin/codexbar").chmod(0o755)
                shutil.copy2(root / "bin/codexbar", root / "bin/codexbar-b")
                put("replacement-percent", "67")
                put("expire-claim.py", '''import os, pathlib
state = pathlib.Path(os.environ["XDG_STATE_HOME"]) / "codexbar-agentview"
(state / "fetch_claim").write_text("0\\n")
''')
            put("bin/notify-send", "#!/usr/bin/env python3\n"
                "import json, pathlib, sys, time\n"
                "time.sleep(0.8)\n"
                "with pathlib.Path(__file__).with_name('notifications').open('a') as output:\n"
                "    output.write(json.dumps(sys.argv[1:]) + '\\n')\n"
                "config = json.loads((pathlib.Path(__file__).parent.parent / 'scripts/scenario.json').read_text()) or {}\n"
                "if config.get('notifyFails'):\n"
                "    print('synthetic service failure', file=sys.stderr)\n"
                "    sys.exit(1)\n")
            (root / "bin/notify-send").chmod(0o755)
            put("test.qml", '''import QtQuick
import org.kde.plasma.plasmoid
import "ui/process" as Process
Item {
    id: fixture
    property int stage: 0
    property int ticks: 0
    property double deadlineMs: Date.now() + ''' + str(scenario_seconds * 1000) + '''
    property string oldCommand: ""
    property var compact: null
    property var rows: []
    property var tip: null
    property var section: null
    property var popup: null
    property int since: 0
    property int freshFetches: 0
    property bool controlFinished: false
    property string expireCommand: ''' + json.dumps(
                "python3 " + json.dumps(str(root / "expire-claim.py"))) + '''
    Connections {
        target: applet.item
        function onFetchKindChanged() {
            if (applet.item.fetchKind === "fresh") fixture.freshFetches++
        }
    }
    Process.CommandRunner {
        id: controlRunner
        onFinished: function(command, exitCode, standardOutput, standardError) {
            if (exitCode !== 0) {
                console.error("FAIL claim expiry: " + standardError)
                Qt.exit(1)
            }
            fixture.controlFinished = true
        }
    }
    function check(condition, message) {
        if (!condition) throw new Error(message)
    }
    Loader {
        id: applet
        source: "ui/Main.qml"
        onStatusChanged: if (status === Loader.Error) Qt.exit(1)
    }
    Timer {
        interval: 20; running: true; repeat: true
        onTriggered: {
            try {
                fixture.ticks++
                if (Date.now() > fixture.deadlineMs)
                    throw new Error("applet scenario timed out at stage " + fixture.stage)
                var app = applet.item
                if (!app) return
''' + scenario + '''
            } catch (error) {
                console.error("FAIL " + error.message)
                Qt.exit(1)
            }
        }
    }
}
''')
            env = dict(os.environ, QT_QPA_PLATFORM="offscreen",
                       QT_FORCE_STDERR_LOGGING="1", QT_LOGGING_TO_CONSOLE="1",
                       QML_IMPORT_PATH=str(root / "imports"),
                       QML_XHR_ALLOW_FILE_READ="1", XDG_STATE_HOME=str(root / "state"),
                       HOME=str(root), XDG_CONFIG_HOME=str(root / "config"),
                       XDG_CACHE_HOME=str(root / "cache"), XDG_DATA_HOME=str(root / "data"),
                       CODEX_HOME=str(root / ".codex"), CLAUDE_CONFIG_DIR=str(root / ".claude"),
                       OMP_AGENT_DIR=str(root / ".omp/agent"),
                       PATH=str(root / "bin") + os.pathsep + os.environ["PATH"])
            if real_fetch:
                seed_percent = (helper_case or {}).get("seedPercent", 42)
                put("provider-percent", str(seed_percent))
                seed = subprocess.run(
                    [sys.executable, str(root / "scripts/codexbar_fetch.py"),
                     "--cli-path", str(root / "bin/codexbar"),
                     "--providers", "kilo", "--min-interval", "3600"],
                    env=env, capture_output=True, text=True, timeout=5)
                self.assertEqual(seed.returncode, 0, seed.stdout + seed.stderr)
                self.assertEqual(json.loads(seed.stdout)["providers"][0]["primary"]["usedPercent"],
                                 seed_percent)
                cache_case = (helper_case or {}).get("seedCache", {})
                if cache_case:
                    cache_path = root / "state/codexbar-agentview/usage_cache.json"
                    payload = json.loads(cache_path.read_text())
                    record = payload["providers"]["kilo"]["records"][0]
                    record.update(cache_case.get("record", {}))
                    if "ageSeconds" in cache_case:
                        measured = (dt.datetime.now(dt.timezone.utc)
                                    - dt.timedelta(seconds=cache_case["ageSeconds"])).isoformat()
                        record.update(updatedAt=measured, cachedAt=measured)
                    cache_path.write_text(json.dumps(payload))
                # The preceding poller has published a reading and a recent claim.
                # Only requests made by the actual applet count below.
                (root / "scripts/calls").unlink()
                put("provider-percent", "81")
                if (helper_case or {}).get("expireInitialClaim"):
                    put("state/codexbar-agentview/fetch_claim", "0\n")
                if (helper_case or {}).get("delayFirstFresh"):
                    # Delay process startup before the real helper stamps its claim.
                    # The poll Timer still starts naturally with the applet.
                    shutil.copy2(root / "scripts/codexbar_fetch.py",
                                 root / "scripts/real_codexbar_fetch.py")
                    put("scripts/codexbar_fetch.py", '''import pathlib, runpy, sys, time
root = pathlib.Path(__file__).parent
first = root / "first-fresh-started"
if "--cache-only" not in sys.argv and not first.exists():
    first.touch()
    time.sleep(1.2)
runpy.run_path(str(root / "real_codexbar_fetch.py"), run_name="__main__")
''')
            result = subprocess.run([QML, str(root / "test.qml")], env=env,
                                    capture_output=True, text=True, timeout=scenario_seconds + 9)
            output = result.stdout + result.stderr
            self.assertEqual(result.returncode, 0, output)
            self.assertIn("PASS actual main", output)
            call_path = root / "scripts/calls"
            calls = [json.loads(line) for line in call_path.read_text().splitlines()] if call_path.exists() else []
            notifications = root / "bin/notifications"
            return calls, ([json.loads(line) for line in notifications.read_text().splitlines()]
                           if notifications.exists() else [])

    def test_automatic_refresh_respects_shared_fetch_claim_and_expiry(self):
        calls, notifications = self.applet('''
                if (fixture.stage === 0) {
                    // Startup restores cache, then schedules the real automatic refresh.
                    if (fixture.freshFetches < 1 || app.cacheRestoring || app.loading) return
                    fixture.check(app.snapshot.providers.length === 1, "cached reading was lost")
                    fixture.check(app.snapshot.providers[0].primary.usedPercent === 42,
                        "automatic refresh executed provider despite a fresh shared claim")
                    fixture.check(app.snapshot.cacheOnly === true, "recent claim did not serve cache")
                    fixture.check(app.lastError === "", "cache-only response became a UI error")
                    fixture.check(app.snapshot.codexRotation === null
                        && app.snapshot.claudeRotation === null, "cached response exposed rotation status")
                    fixture.stage = 1
                    controlRunner.run(fixture.expireCommand)
                } else if (fixture.stage === 1) {
                    if (!fixture.controlFinished) return
                    fixture.stage = 2
                    app.refresh(true)
                } else if (fixture.stage === 2) {
                    if (app.loading) return
                    fixture.check(fixture.freshFetches === 2, "expired claim refresh did not complete")
                    fixture.check(app.snapshot.providers[0].primary.usedPercent === 81,
                        "expired claim did not allow a provider fetch")
                    fixture.check(app.snapshot.cacheOnly !== true, "expired claim still served cache only")
                    fixture.check(app.lastError === "", "expired claim refresh failed")
                    fixture.stage = 3
                    app.refresh(true)
                } else if (fixture.stage === 3) {
                    if (app.loading) return
                    fixture.check(app.snapshot.cacheOnly === true, "new claim allowed a duplicate fetch")
                    fixture.check(app.snapshot.providers[0].primary.usedPercent === 81,
                        "newly cached reading was lost")
                    fixture.check(app.lastError === "", "new cache-only response became a UI error")
                    console.log("PASS actual main")
                    Qt.quit()
                }
''', real_fetch=True)
        self.assertEqual(calls, [{"provider": "kilo"}],
                         "only the refresh after shared claim expiry may execute the provider")
        self.assertEqual(notifications, [])

    def test_recent_shared_reading_is_current_and_notifies_critical_usage_once(self):
        calls, notifications = self.applet('''
                if (fixture.freshFetches < 1 || app.cacheRestoring || app.loading) return
                fixture.check(app.snapshot.providers.length === 1
                    && app.snapshot.providers[0].primary.usedPercent === 95,
                    "recent shared 95% reading must remain usable")
                fixture.check(app.snapshot.cacheOnly === true, "shared claim must refuse provider execution")
                fixture.check(app.snapshot.providers[0].stale !== true,
                    "fresh shared reading was incorrectly presented as last known")
                fixture.check(app.lastError === "", "shared current reading became a fetch error")
                if (Object.keys(app.pendingNotifications).length) return
                if (fixture.stage === 0) {
                    fixture.stage = 1
                    app.refresh(true)
                } else {
                    fixture.check(fixture.freshFetches === 2, "second automatic refresh did not complete")
                    console.log("PASS actual main shared current usage alert")
                    Qt.quit()
                }
''', {"usageNotifications": True, "seedPercent": 95}, real_fetch=True)
        self.assertEqual(calls, [], "adopting current shared usage must not execute the provider")
        self.assertEqual(len(notifications), 1,
                         "current shared critical usage must notify once across repeated claim refusals")
        self.assertIn("CodexBar AgentView usage critical", notifications[0])
        self.assertTrue(any("Kilo" in part and "95%" in part for part in notifications[0]),
                        notifications)

    def test_shared_old_failed_or_source_stale_usage_stays_last_known_and_silent(self):
        for cache_case in (
            {"ageSeconds": 10_800},
            {"record": {"stale": True, "refreshError": "controlled upstream failure"}},
            {"record": {"stale": True, "sourceStale": True, "refreshError": ""}},
        ):
            with self.subTest(cache_case=cache_case):
                calls, notifications = self.applet('''
                if (fixture.freshFetches < 1 || app.cacheRestoring || app.loading) return
                fixture.check(app.snapshot.providers.length === 1
                    && app.snapshot.providers[0].primary.usedPercent === 95,
                    "last-known shared 95% reading must remain visible")
                fixture.check(app.snapshot.cacheOnly === true, "recent claim must still suppress a fetch")
                fixture.check(app.snapshot.providers[0].stale === true,
                    "old, failed or source-stale shared reading was presented as current")
                fixture.check(app.lastError === "", "valid last-known response became a fetch error")
                if (Object.keys(app.pendingNotifications).length) return
                if (fixture.stage === 0) {
                    fixture.stage = 1
                    app.refresh(true)
                } else {
                    fixture.check(fixture.freshFetches === 2, "second claim refusal did not complete")
                    console.log("PASS actual main shared last-known usage stays silent")
                    Qt.quit()
                }
''', {"usageNotifications": True, "seedPercent": 95, "seedCache": cache_case},
                    real_fetch=True)
                self.assertEqual(calls, [], "last-known cache must respect the shared claim")
                self.assertEqual(notifications, [],
                                 "old, failed or source-stale usage must not notify")

    def test_manual_refresh_bypasses_fresh_shared_claim(self):
        calls, notifications = self.applet('''
                if (fixture.freshFetches < 1 || app.cacheRestoring || app.loading) return
                if (fixture.stage === 0) {
                    check(app.snapshot.cacheOnly === true
                        && app.snapshot.providers[0].primary.usedPercent === 42,
                        "startup must adopt the preceding poller's reading")
                    fixture.stage = 1
                    app.refresh()
                } else {
                    check(fixture.freshFetches === 2, "manual refresh did not complete")
                    check(app.snapshot.providers.length === 1
                        && app.snapshot.providers[0].primary.usedPercent === 81,
                        "Refresh now must execute the provider despite a fresh shared claim")
                    check(app.snapshot.cacheOnly !== true && app.lastError === "",
                        "manual refresh must publish a fresh successful reading")
                    console.log("PASS actual main manual refresh bypasses shared claim")
                    Qt.quit()
                }
''', real_fetch=True)
        self.assertEqual(calls, [{"provider": "kilo"}],
                         "manual refresh must execute the actual helper's provider path")
        self.assertEqual(notifications, [])

    def test_backend_change_bypasses_fresh_shared_claim(self):
        calls, notifications = self.applet('''
                if (fixture.freshFetches < 1 || app.cacheRestoring || app.loading) return
                if (fixture.stage === 0) {
                    check(app.snapshot.providers.length === 1
                        && app.snapshot.providers[0].primary.usedPercent === 42,
                        "the old backend starts with its scoped shared reading")
                    fixture.stage = 1
                    Plasmoid.configuration.cliPath = app.cliPath + "-b"
                    check(app.snapshot.providers.length === 0,
                        "replacement backend must immediately retire the old scoped reading")
                } else {
                    if (fixture.freshFetches < 2) return
                    check(app.snapshot.providers.length === 1
                        && app.snapshot.providers[0].primary.usedPercent === 67,
                        "new backend must fetch its own reading, not an empty claim-refused cache")
                    check(app.snapshot.cacheOnly !== true && app.lastError === "",
                        "backend change must complete a fresh successful fetch")
                    console.log("PASS actual main backend change bypasses shared claim")
                    Qt.quit()
                }
''', real_fetch=True)
        self.assertEqual(calls, [{"provider": "kilo"}],
                         "the replacement executable must run despite the old backend's claim")
        self.assertEqual(notifications, [])

    def test_first_natural_poll_fetches_after_delayed_initial_helper(self):
        calls, notifications = self.applet('''
                if (fixture.freshFetches < 1 || app.cacheRestoring || app.loading) return
                if (fixture.stage === 0) {
                    check(app.snapshot.cacheOnly !== true
                        && app.snapshot.providers[0].primary.usedPercent === 81,
                        "expired initial claim must allow the delayed first provider reading")
                    fixture.oldCommand = app.snapshot.providers[0].updatedAt
                    fixture.stage = 1
                } else {
                    // Do not drive refresh: wait for the applet's own first poll Timer.
                    if (fixture.freshFetches < 2) return
                    check(fixture.freshFetches === 2, "missed the first natural polling result")
                    check(app.snapshot.cacheOnly !== true,
                        "first natural poll was refused by the applet's own fresh claim")
                    check(app.snapshot.providers[0].updatedAt !== fixture.oldCommand,
                        "first natural poll must publish a second provider measurement")
                    console.log("PASS actual main natural polling follows helper completion")
                    Qt.quit()
                }
''', {"configuration": {"refreshSeconds": 10}, "expireInitialClaim": True,
      "delayFirstFresh": True}, real_fetch=True, scenario_seconds=16)
        self.assertEqual(calls, [{"provider": "kilo"}, {"provider": "kilo"}],
                         "both startup and the first natural poll must execute the provider")
        self.assertEqual(notifications, [])

    def test_shared_cache_preserves_forecast_and_cli_version_for_same_backend(self):
        initial = {
            "providers": [{"id": "codex", "ok": True, "sourceScope": "a" * 64,
                           "primary": {"usedPercent": 42}}],
            "forecast": {"ok": True, "signal": {
                "deadlineAt": "2099-01-01T12:00:00Z", "percent": 75}},
            "cliVersion": "CodexBar 0.56.3",
        }
        shared = dict(initial, cacheOnly=True, sharedCache=True, forecast=None, cliVersion=None,
                      providers=[{"id": "codex", "ok": True, "sourceScope": "a" * 64,
                                  "primary": {"usedPercent": 83}}])
        shared_without_metadata = {"providers": shared["providers"],
                                   "cacheOnly": True, "sharedCache": True}
        calls, notifications = self.applet('''
                if (fixture.freshFetches < 1 || app.cacheRestoring || app.loading) return
                if (fixture.stage === 0) {
                    check(app.codexForecastState(app.snapshot.forecast) === "announced"
                        && app.snapshot.cliVersion === "CodexBar 0.56.3",
                        "initial fresh response must establish usable metadata")
                    fixture.stage = 1
                    app.refresh(true)
                } else {
                    check(app.snapshot.sharedCache === true
                        && app.snapshot.providers[0].primary.usedPercent === 83,
                        "the new shared usage response must replace the old provider reading")
                    check(app.codexForecastState(app.snapshot.forecast) === "announced"
                        && app.snapshot.forecast.signal.percent === 75,
                        "shared-cache adoption must retain the existing valid forecast")
                    check(app.snapshot.cliVersion === "CodexBar 0.56.3",
                        "shared-cache adoption must retain the current backend's CLI version")
                    if (fixture.stage === 1) {
                        fixture.stage = 2
                        app.refresh(true)
                    } else {
                        console.log("PASS actual main shared cache retains current backend metadata")
                        Qt.quit()
                    }
                }
''', {"freshSnapshots": {"/backend-a": [initial, shared, shared_without_metadata]}})
        self.assertEqual([call["cache"] for call in calls], [True, False, False, False])
        self.assertEqual(notifications, [])

    def test_shared_cache_uses_current_local_rotation_badges_and_clears_deleted_status(self):
        records = [{"id": provider, "ok": True, "sourceScope": provider[0] * 64,
                    "accountEmail": provider + "@example.test", "primary": {"usedPercent": 42}}
                   for provider in ("claude", "codex")]
        initial = {"providers": records}
        shared = {"providers": records, "cacheOnly": True, "sharedCache": True}
        deleted = dict(shared, claudeRotation=None, codexRotation=None)
        for provider in ("claude", "codex"):
            initial[provider + "Rotation"] = {
                "mode": "FILL", "operatingMode": "auto",
                "accountAvailability": {provider + "@example.test": "open"}}
            shared[provider + "Rotation"] = {
                "mode": "TARGET", "operatingMode": "confirm",
                "accountAvailability": {provider + "@example.test": "blocked"}}
        calls, notifications = self.applet('''
                if (fixture.freshFetches < 1 || app.cacheRestoring || app.loading) return
                for (var index = 0; index < app.snapshot.providers.length; index++) {
                    var record = app.snapshot.providers[index]
                    var rotation = app.providerRotation(record.id)
                    if (fixture.stage === 0) {
                        check(rotation && rotation.mode === "FILL"
                            && app.accountAvailabilityIndicator(record) === "\\uD83D\\uDFE2",
                            "initial fresh response establishes local rotation badges")
                    } else if (fixture.stage === 1) {
                        check(rotation && rotation.mode === "TARGET"
                            && rotation.operatingMode === "confirm",
                            "shared-cache response must use its current local rotation, not clear or copy old status")
                        check(app.accountAvailabilityIndicator(record) === "\\uD83D\\uDD12",
                            "shared-cache response must display the newly blocked account badge")
                    } else {
                        check(rotation === null && app.accountAvailabilityIndicator(record) === "",
                            "deleted local rotation must clear the old badge, never resurrect it from memory")
                    }
                }
                check(app.snapshot.providers.length === 2, "both providers must remain represented")
                if (fixture.stage === 2) {
                    console.log("PASS actual main shared cache adopts only current local rotation badges")
                    Qt.quit()
                } else {
                    fixture.stage++
                    app.refresh(true)
                }
''', {"configuration": {"enableClaude": True},
      "freshSnapshots": {"/backend-a": [initial, shared, deleted]}})
        self.assertEqual([call["cache"] for call in calls], [True, False, False, False])
        self.assertEqual(notifications, [],
                         "adopting local cache badges is not a fresh mode-transition notification")

    def test_shared_cache_does_not_carry_metadata_across_backend_generations(self):
        initial = {
            "providers": [{"id": "codex", "ok": True, "sourceScope": "a" * 64,
                           "primary": {"usedPercent": 42}}],
            "forecast": {"ok": True, "signal": {
                "deadlineAt": "2099-01-01T12:00:00Z", "percent": 75}},
            "cliVersion": "CodexBar 0.56.3",
            "codexRotation": {"mode": "FILL", "operatingMode": "auto"},
        }
        replacement = {
            "providers": [{"id": "codex", "ok": True, "sourceScope": "b" * 64,
                           "primary": {"usedPercent": 83}}],
            "cacheOnly": True, "sharedCache": True,
            "forecast": None, "cliVersion": None, "codexRotation": None,
        }
        calls, notifications = self.applet('''
                if (fixture.freshFetches < 1 || app.cacheRestoring || app.loading) return
                if (fixture.stage === 0) {
                    check(app.snapshot.forecast && app.snapshot.cliVersion
                        && app.snapshot.codexRotation, "old backend must start with metadata")
                    fixture.stage = 1
                    Plasmoid.configuration.cliPath = "/backend-b"
                    check(app.snapshot.forecast === null && app.snapshot.cliVersion === null
                        && app.snapshot.codexRotation === null,
                        "backend change must synchronously clear its old metadata")
                } else {
                    if (fixture.freshFetches < 2) return
                    check(app.snapshot.sharedCache === true
                        && app.snapshot.providers.length === 1
                        && app.snapshot.providers[0].sourceScope === "b".repeat(64)
                        && app.snapshot.providers[0].primary.usedPercent === 83,
                        "replacement backend may adopt only its current credential scope")
                    check(app.snapshot.forecast === null && app.snapshot.cliVersion === null
                        && app.snapshot.codexRotation === null,
                        "shared cache must not recover another backend generation's metadata")
                    console.log("PASS actual main shared metadata remains backend scoped")
                    Qt.quit()
                }
''', {"freshSnapshots": {"/backend-a": [initial], "/backend-b": [replacement]}})
        self.assertEqual([call["backend"] for call in calls],
                         ["/backend-a", "/backend-a", "/backend-b"])
        self.assertEqual(notifications, [])

    def test_authoritative_empty_fatal_snapshot_does_not_resurrect_memory(self):
        calls, notifications = self.applet('''
                if (app.cacheRestoring || app.loading) return
                if (fixture.stage === 0) {
                    check(app.snapshot.providers.length === 1, "initial scoped usage")
                    app.refresh()
                    fixture.stage = 1
                } else {
                    check(app.snapshot.providers.length === 0,
                        "an authoritative empty fatal snapshot must retire old credentials")
                    check(app.lastError === "credentials changed" && app.snapshot.codexRotation === null,
                        "fatal snapshot must preserve the current error and clear mode")
                    console.log("PASS actual main authoritative empty snapshot")
                    Qt.exit(0)
                }
''', {"failure": "fatal"})
        self.assertEqual([call["cache"] for call in calls], [True, False, False])
        self.assertEqual(notifications, [])

    def test_transport_failure_revalidates_cache_and_preserves_empty_marker(self):
        for failure in ("", "not json", '{"providers": [null]}'):
            with self.subTest(failure=failure):
                calls, notifications = self.applet('''
                if (app.cacheRestoring || app.loading) return
                if (fixture.stage === 0) {
                    app.refresh()
                    fixture.stage = 1
                } else {
                    check(app.snapshot.providers.length === 0,
                        "transport failure cannot trust old in-memory credentials")
                    check(app.snapshot.cacheOnly === true,
                        "empty validated cache result must retain its empty-state marker")
                    check(!!app.lastError && app.snapshot.codexRotation === null
                        && app.ompModeNotificationState.mode === null,
                        "cache recovery preserves fetch failure and clears mode continuity")
                    console.log("PASS actual main scoped empty recovery")
                    Qt.exit(0)
                }
''', {"failure": failure})
                self.assertEqual([call["cache"] for call in calls], [True, False, False, True])
                self.assertEqual(notifications, [])

    def test_recovery_cache_replaces_memory_with_current_scope_only(self):
        current = {"id": "codex", "ok": True, "stale": True, "sourceScope": "b" * 64,
                   "primary": {"usedPercent": 83}, "cachedAt": "2026-09-29T12:00:00Z"}
        calls, notifications = self.applet('''
                if (app.cacheRestoring || app.loading) return
                if (fixture.stage === 0) {
                    app.refresh()
                    fixture.stage = 1
                } else {
                    var record = app.snapshot.providers[0]
                    check(app.snapshot.providers.length === 1 && record.sourceScope === "b".repeat(64)
                        && record.primary.usedPercent === 83 && record.stale === true,
                        "only helper-confirmed current credentials may survive recovery")
                    check(app.lastError === "transport exploded"
                        && app.snapshot.codexRotation === null,
                        "cache success cannot hide the failed refresh")
                    console.log("PASS actual main scoped recovery")
                    Qt.exit(0)
                }
''', {"failure": "", "recoveryProviders": [current]})
        self.assertEqual([call["cache"] for call in calls], [True, False, False, True])
        self.assertEqual(notifications, [])

    def test_failed_cache_recovery_clears_unverified_usage_without_retry_loop(self):
        calls, notifications = self.applet('''
                if (app.cacheRestoring || app.loading) return
                if (fixture.stage === 0) {
                    app.refresh()
                    fixture.stage = 1
                } else if (fixture.stage === 1) {
                    check(app.snapshot.providers.length === 0 && app.snapshot.codexRotation === null,
                        "failed validation must clear unverified usage and mode")
                    check(app.lastError === "transport exploded", "keep original refresh failure")
                    fixture.stage = 2
                } else if (++fixture.stage >= 12) {
                    check(!app.loading, "recovery must not trigger a fresh/recovery loop")
                    console.log("PASS actual main failed scoped recovery")
                    Qt.exit(0)
                }
''', {"failure": "", "recoveryFails": True})
        self.assertEqual([call["cache"] for call in calls], [True, False, False, True])
        self.assertEqual(notifications, [])

    def test_late_usage_delivery_cannot_settle_replacement_credential_scope(self):
        calls, notifications = self.applet('''
                if (app.cacheRestoring || app.loading) return
                if (fixture.stage === 0) {
                    Plasmoid.configuration.ompModeNotifications = false
                    fixture.oldCommand = Object.keys(app.pendingNotifications)[0]
                    check(!!fixture.oldCommand, "first credential alert is in flight")
                    app.refresh()
                    fixture.stage = 1
                } else if (fixture.stage === 1) {
                    check(app.snapshot.providers[0].sourceScope === "b".repeat(64), "new credentials loaded")
                    if (app.pendingNotifications[fixture.oldCommand]) return
                    var states = Object.values(app.usageNotificationState)
                    check(states.length === 1 && states[0].pending.length === 1 && states[0].level === 0,
                        "old credential callback cannot acknowledge the replacement credential alert")
                    fixture.stage = 2
                } else if (Object.keys(app.pendingNotifications).length === 0) {
                    check(Object.values(app.usageNotificationState)[0].level === 2,
                        "current credential callback acknowledges only its own alert")
                    console.log("PASS actual main scopes usage acknowledgements")
                    Qt.exit(0)
                }
''', {"switchScope": True, "usageNotifications": True})
        self.assertEqual(len(calls), 3)
        self.assertEqual(len(notifications), 2)

    def test_successful_fetch_does_not_clear_notification_service_failure(self):
        calls, notifications = self.applet('''
                if (app.cacheRestoring || app.loading || Object.keys(app.pendingNotifications).length) return
                if (fixture.stage === 0) {
                    app.refresh()
                    fixture.stage = 1
                } else if (fixture.stage === 1) {
                    check(app.notificationError.indexOf("synthetic service failure") >= 0,
                        "real notification subprocess failure is shown")
                    fixture.oldCommand = app.notificationError
                    app.refresh()
                    fixture.stage = 2
                } else {
                    check(app.lastError === "" && app.notificationError === fixture.oldCommand,
                        "successful usage refresh cannot clear an unrelated notification failure")
                    console.log("PASS actual main preserves notification service failures")
                    Qt.exit(0)
                }
''', {"notifyFails": True})
        self.assertEqual(len(calls), 4)
        self.assertEqual(len(notifications), 1)

    def test_backend_change_clears_old_usage_before_missing_backend_fails(self):
        calls, notifications = self.applet('''
                if (fixture.stage === 0 && !app.cacheRestoring && app.snapshot.providers.length) {
                    check(app.snapshot.providers[0].primary.usedPercent === 1,
                        "cache must be shown before the fresh fetch completes")
                    fixture.stage = 1
                } else if (fixture.stage === 1 && !app.loading) {
                    check(app.snapshot.codexRotation.mode === "FILL", "fresh initial status")
                    Plasmoid.configuration.cliPath = "/missing"
                    check(app.snapshot.providers.length === 0 && app.snapshot.codexRotation === null,
                        "changing backend must immediately discard previous usage and rotation")
                    fixture.stage = 2
                } else if (fixture.stage === 2 && !app.loading && app.lastError) {
                    check(app.snapshot.providers.length === 0 && app.snapshot.codexRotation === null,
                        "failed new backend must not retain another backend's usage")
                    console.log("PASS actual main backend change clears old readings")
                    Qt.exit(0)
                }
''')
        self.assertEqual(calls, [{"backend": "/backend-a", "cache": True},
                                 {"backend": "/backend-a", "cache": False},
                                 {"backend": "/missing", "cache": False},
                                 {"backend": "/missing", "cache": True}])
        self.assertEqual(notifications, [])

    def test_inflight_backend_round_trip_rejects_old_completion(self):
        calls, notifications = self.applet('''
                if (fixture.stage === 0 && !app.cacheRestoring && !app.loading) {
                    app.refresh()
                    check(app.loading, "start the old backend request")
                    Plasmoid.configuration.cliPath = "/missing"
                    Plasmoid.configuration.cliPath = "/backend-a"
                    fixture.stage = 1
                } else if (fixture.stage === 1) {
                    if (app.snapshot.providers.length) {
                        check(app.snapshot.providers[0].primary.usedPercent === 4,
                            "A to B to A must reject the original A completion")
                        check(!app.loading, "only the replacement request can publish usage")
                        fixture.stage = 2
                    }
                } else if (fixture.stage === 2 && Object.keys(app.pendingNotifications).length === 0) {
                    console.log("PASS actual main rejects stale inflight backend completion")
                    Qt.exit(0)
                }
''')
        self.assertEqual([call["backend"] for call in calls], ["/backend-a"] * 4)
        self.assertEqual(notifications, [])

    def test_mode_dispatch_uses_fresh_status_and_clears_gaps(self):
        calls, notifications = self.applet('''
                if (app.cacheRestoring || app.loading
                        || Object.keys(app.pendingNotifications).length) return
                if (fixture.stage === 0) {
                    check(app.ompModeNotificationState.mode === "auto · FILL",
                        "first fresh status must establish the mode baseline")
                } else if (fixture.stage === 2) {
                    check(app.snapshot.codexRotation === null
                            && app.ompModeNotificationState.mode === null,
                        "missing status must break comparison continuity")
                } else if (fixture.stage === 5) {
                    check(app.lastError === "temporary fetch failure"
                            && app.snapshot.providers[0].stale === true
                            && app.snapshot.codexRotation === null
                            && app.ompModeNotificationState.mode === null,
                        "fetch failure must retain usage but not mode status or its baseline")
                } else if (fixture.stage === 7) {
                    console.log("PASS actual main sends only available mode transitions")
                    Qt.exit(0)
                    return
                }
                fixture.stage++
                app.refresh()
''')
        self.assertEqual([call["cache"] for call in calls],
                         [True] + [False] * 6 + [True, False, False])
        self.assertEqual([args[-1] for args in notifications], [
            "auto · FILL → auto · TARGET",
            "auto · BURN → auto · STALLED (BURN)",
            "auto · IDLE → confirm · IDLE",
        ])

    def test_disabling_codex_clears_status_and_reenabling_is_silent(self):
        calls, notifications = self.applet('''
                if (app.cacheRestoring || app.loading) return
                if (fixture.stage === 0) {
                    Plasmoid.configuration.enableCodex = false
                    check(app.snapshot.codexRotation === null
                            && app.ompModeNotificationState.mode === null,
                        "disabling Codex must immediately clear status and comparison baseline")
                    app.refresh()
                    check(!app.loading && app.snapshot.providers.length === 0,
                        "empty provider selection must not launch a fetch")
                    Plasmoid.configuration.enableCodex = true
                    app.refresh()
                    fixture.stage = 1
                } else if (fixture.stage === 1) {
                    check(app.snapshot.codexRotation.mode === "TARGET"
                            && app.ompModeNotificationState.mode === "auto · TARGET"
                            && Object.keys(app.pendingNotifications).length === 0,
                        "reenabling Codex establishes a silent fresh baseline")
                    console.log("PASS actual main clears disabled-provider mode baseline")
                    Qt.exit(0)
                }
''')
        self.assertEqual(len(calls), 3)
        self.assertEqual(notifications, [])

    def test_old_delivery_cannot_settle_new_backend_mode_transition(self):
        calls, notifications = self.applet('''
                if (app.cacheRestoring || app.loading) return
                if (fixture.stage === 0) {
                    app.refresh()
                    fixture.stage = 1
                } else if (fixture.stage === 1) {
                    fixture.oldCommand = Object.keys(app.pendingNotifications)[0]
                    check(!!fixture.oldCommand, "old backend transition must be in flight")
                    Plasmoid.configuration.cliPath = "/backend-b"
                    fixture.stage = 2
                } else if (fixture.stage === 2) {
                    check(app.ompModeNotificationState.mode === "auto · FILL",
                        "new backend starts with its own silent baseline")
                    app.refresh()
                    fixture.stage = 3
                } else if (fixture.stage === 3) {
                    if (app.pendingNotifications[fixture.oldCommand]) return
                    check(app.ompModeNotificationState.pending
                            && app.ompModeNotificationState.transition !== null,
                        "late old-backend acknowledgement must not settle the new backend event")
                    fixture.stage = 4
                } else if (fixture.stage === 4 && Object.keys(app.pendingNotifications).length === 0) {
                    check(!app.ompModeNotificationState.pending
                            && app.ompModeNotificationState.transition === null,
                        "the new backend acknowledgement must settle its own event")
                    console.log("PASS actual main isolates late notification acknowledgements")
                    Qt.exit(0)
                }
''')
        self.assertEqual([call["backend"] for call in calls],
                         ["/backend-a"] * 3 + ["/backend-b"] * 2)
        self.assertEqual([args[-1] for args in notifications],
                         ["auto · FILL → auto · TARGET"] * 2)

    def test_focused_session_needs_one_candidate_in_the_focused_window(self):
        self.applet('''
                if (app.cacheRestoring || app.loading) return
                function win(pid, caption) { return { info: { pid: pid, caption: caption } } }
                function key(w, agents) {
                    return app.focusedAgentKeyFor(w, { agents: agents })
                }
                var code = [win(5, "a.cs - alpha - Visual Studio Code"), win(5, "b.js - beta - Visual Studio Code")]
                app.taskWindows = code.concat([win(7, "π > Fix login"), win(9, "work")])
                var alpha = { provider: "omp", sessionId: "a", cwd: "/x/alpha", ancestorPids: [11, 5] }
                check(key(code[0], [alpha]) === '["omp","a"]', "VS Code window of the session's folder")
                check(key(code[1], [alpha]) === "", "another window of the same VS Code process")
                var login = { provider: "omp", sessionId: "l", windowTitle: "Fix login", cwd: "/w", ancestorPids: [12, 7] }
                var other = { provider: "pi", sessionId: "o", windowTitle: "Docs", cwd: "/w", ancestorPids: [13, 7] }
                check(key(app.taskWindows[2], [other, login]) === '["omp","l"]', "kitty tab named by caption")
                var shown = { provider: "omp", sessionId: "s", host: "tern", cwd: "/w", ternSession: "work", ancestorPids: [14, 9] }
                var hidden = { provider: "omp", sessionId: "h", host: "tern", cwd: "/w", ancestorPids: [15, 9] }
                var elsewhere = { provider: "omp", sessionId: "e", host: "tern", cwd: "/w", ternSession: "Default", ancestorPids: [16, 9] }
                check(key(app.taskWindows[3], [hidden, elsewhere, shown]) === '["omp","s"]',
                    "Tern pane visible in the session the window shows")
                check(key(app.taskWindows[3], [elsewhere]) === "",
                    "a visible pane of a session the window does not show")
                check(key(app.taskWindows[3], [hidden]) === "", "a lone hidden Tern pane")
                check(key(null, [alpha]) === "", "no focused task window")
                console.log("PASS actual main focused session")
                Qt.exit(0)
''')

    def test_tern_integration_shows_waits_only_while_switched_on(self):
        view = {"ok": True, "now": "2026-11-05T16:00:00+01:00",
                "counts": {"on_me": 2, "on_machine": 1, "on_someone": 0, "on_my_time": 0},
                "sessions": [
                    {"session_id": "back", "ball": "on_me", "status": "● ⏳ PR#7 · 2d",
                     "line": "● ⏳ PR#7 · Q&A <b>on</b> head · 2 days"},
                    {"session_id": "away", "ball": "on_machine", "status": "⏳ 16:00",
                     "line": "⏳ 16:00 · \"check the deploy\""}],
                "panes": []}
        self.applet('''
                if (app.cacheRestoring || app.loading) return
                function find(item, name, out) {
                    out = out || []
                    if (item.objectName === name) out.push(item)
                    var kids = item.children || []
                    for (var i = 0; i < kids.length; i++) find(kids[i], name, out)
                    return out
                }
                function markers() {
                    var out = {}
                    find(fixture.popup, "waitingMarker").forEach(function(m) {
                        if (m.visible) out[m.line] = m.text
                    })
                    return out
                }
                if (fixture.stage === 0) {
                    app.agentSnapshot = { counts: { working: 0, blocked: 0, idle: 3, untracked: 0, total: 3 },
                        agents: ["back", "away", "plain"].map(function(id) {
                            return { provider: "omp", sessionId: id, state: "idle", cwd: "/w/" + id }
                        }) }
                    fixture.compact = app.compactRepresentation.createObject(fixture, { width: 400, height: 24 })
                    fixture.popup = app.fullRepresentation.createObject(fixture, { width: 600, height: 400 })
                    fixture.stage = 1
                    fixture.since = fixture.ticks
                } else if (fixture.stage === 1) {
                    if (fixture.ticks - fixture.since < 25) return
                    var count = find(fixture.compact, "waitingOnMeCount")[0]
                    check(count && !count.visible, "switch off: no panel count")
                    check(JSON.stringify(markers()) === "{}", "switch off: no row markers")
                    check(app.waitingRequest === null && app._waitingViewKey === ""
                        && app.waitingOnMeCount === 0, "switch off: no waiting view loaded")
                    Plasmoid.configuration.ternIntegration = true
                    fixture.stage = 2
                } else if (fixture.stage === 2) {
                    if (app.waitingOnMeCount === 0) return
                    var shown = find(fixture.compact, "waitingOnMeCount")[0]
                    check(shown.visible && find(shown, "waitingOnMeCountLabel")[0].text === "2", "switch on: panel shows on_me")
                    var marks = markers()
                    var line = "● ⏳ PR#7 · Q&A <b>on</b> head · 2 days"
                    check(marks[line] === "●", "on_me session gets ●")
                    check(marks["⏳ 16:00 · \\"check the deploy\\""] === "⏳", "waiting session gets ⏳")
                    check(Object.keys(marks).length === 2, "a session without a wait stays unmarked")
                    var section = find(fixture.popup, "agentsSection")[0]
                    var tip = section.data.filter(function(o) { return o.objectName === "rowTip" })[0]
                    var rows = []
                    ;(function walk(item) {
                        if (item.waitingHoverText !== undefined) rows.push(item)
                        ;(item.children || []).forEach(walk)
                    })(fixture.popup)
                    section.hoveredRow = rows.filter(function(r) { return r.waiting && r.waiting.onMe })[0]
                    check(tip.textFormat === Text.PlainText
                        && tip.text === line.replace("&", "&\\u200a") + "\\n/w/back"
                            + "\\nclick to focus · peek button or Space for recent messages",
                        "row tooltip leads with the line, as plain text with its ampersand: " + tip.text)
                    section.hoveredRow = rows.filter(function(r) { return !r.waiting })[0]
                    check(section.hoveredRow && tip.text === "",
                        "a row without a wait keeps its attached cwd tooltip")
                    section.hoveredRow = null
                    fixture.rows = rows
                    fixture.tip = tip
                    fixture.section = section
                    Plasmoid.configuration.ternIntegration = false
                    fixture.stage = 3
                } else {
                    check(!find(fixture.compact, "waitingOnMeCount")[0].visible
                        && JSON.stringify(markers()) === "{}", "switching off clears the markers")
                    fixture.section.hoveredRow = fixture.rows[0]
                    check(fixture.tip.text === "" && fixture.rows.every(function(r) { return !r.waiting }),
                        "switch off: every row keeps its attached cwd tooltip")
                    console.log("PASS actual main tern integration")
                    Qt.exit(0)
                }
''', {"waitingView": view}, views=True)

    def test_waiting_health_warns_and_marks_the_panel_count_uncertain(self):
        # A fixed past day: never today, whatever the clock reads.
        checked = dt.datetime(2026, 1, 5, 12, 0).astimezone()
        since = "Jan 5, 12:00"
        warning = f"⚠ Waits not checked since {since}: waiting.json: unknown version 9"
        view = {"ok": True, "counts": {"on_me": 2},
                "health": {"checked_at": checked.isoformat(),
                           "error": "waiting.json:\n unknown version 9",
                           "error_since": checked.isoformat(), "agents_stale_since": None},
                "sessions": [{"session_id": "back", "ball": "on_me", "line": "● PR#7 merged"}]}
        self.applet('''
                if (app.cacheRestoring || app.loading) return
                function find(item, name, out) {
                    out = out || []
                    if (item.objectName === name) out.push(item)
                    var kids = item.children || []
                    for (var i = 0; i < kids.length; i++) find(kids[i], name, out)
                    return out
                }
                if (fixture.stage === 0) {
                    app.agentSnapshot = { counts: { working: 0, blocked: 0, idle: 1, untracked: 0, total: 1 },
                        agents: [{ provider: "omp", sessionId: "back", state: "idle", cwd: "/w/back" }] }
                    fixture.compact = app.compactRepresentation.createObject(fixture, { width: 400, height: 24 })
                    fixture.popup = app.fullRepresentation.createObject(fixture, { width: 600, height: 400 })
                    Plasmoid.configuration.ternIntegration = true
                    fixture.stage = 1
                    return
                }
                if (app.waitingOnMeCount === 0) return
                var expected = ''' + json.dumps(warning) + '''
                var count = find(fixture.compact, "waitingOnMeCount")[0]
                var countText = find(count, "waitingOnMeCountLabel")[0]
                var label = find(fixture.popup, "waitingWarning")[0]
                var row = null
                ;(function walk(item) {
                    if (item.waitingHoverText !== undefined && item.waiting) row = item
                    ;(item.children || []).forEach(walk)
                })(fixture.popup)
                check(app.waitingWarning === expected, "failed tick read from the file: " + app.waitingWarning)
                check(label.visible && label.text === expected, "Agents section shows the warning")
                check(count.visible && countText.text === "2?" && count.opacity < 1,
                    "stale panel count reads as uncertain")
                check(row.waitingHoverText === "● PR#7 merged\\n" + expected + "\\n/w/back"
                    + "\\nclick to focus · peek button or Space for recent messages",
                    "row tooltip follows the line with the warning: " + row.waitingHoverText)
                check(app.toolTipSubText.indexOf("unknown version 9") >= 0, "panel tooltip carries the warning")

                var fresh = ''' + json.dumps(view) + '''
                fresh.health = { checked_at: new Date().toISOString(), error: null,
                                 error_since: null, agents_stale_since: null }
                app.applyWaitingView(fresh)
                check(app.waitingWarning === "" && !label.visible, "a successful tick clears the warning")
                check(countText.text === "2" && count.opacity === 1, "fresh count is plain")
                check(row.waitingHoverText.indexOf("⚠") < 0, "fresh row tooltip has no warning")
                check(app.toolTipSubText.indexOf("⚠") < 0, "fresh panel tooltip has no warning")
                var partial = JSON.parse(JSON.stringify(fresh))
                partial.health.error = "session abcd can't be shown:\\n gone"
                partial.health.error_kind = "partial"
                app.applyWaitingView(partial)
                var lastCheck = "⚠ Last check: session abcd can't be shown: gone"
                check(app.waitingWarning === lastCheck && label.visible && label.text === lastCheck,
                    "a fresh check's error is the last check's: " + app.waitingWarning)
                check(count.visible && countText.text === "2" && count.opacity === 1,
                    "a fresh check's error leaves the count plain: " + countText.text)
                check(row.waitingHoverText.indexOf(lastCheck) >= 0
                    && app.toolTipSubText.indexOf("⚠ Last check: session abcd") >= 0,
                    "both tooltips carry the last check's error")
                var failed = JSON.parse(JSON.stringify(partial))
                failed.health.error = "tern: no daemon"
                failed.health.error_kind = "failed"
                app.applyWaitingView(failed)
                check(app.waitingWarning.indexOf("⚠ Waits not checked since ") === 0
                    && app.waitingWarning.indexOf(": tern: no daemon") > 0 && countText.text === "2?",
                    "a failed tick with a fresh checked_at keeps the old wording and N?: " + app.waitingWarning)
                app.applyWaitingView({ health: { checked_at: null, error: "tern: no daemon" } })
                check(count.visible && countText.text === "0?",
                    "a failed first tick still marks the panel at zero")
                fresh.counts.on_me = 0
                app.applyWaitingView(fresh)
                check(!count.visible, "a fresh zero count stays hidden")
                var unticked = { counts: { on_me: 0 }, health: { checked_at: null, error: null } }
                app.applyWaitingView(unticked)
                check(app.waitingWarning === "" && !count.visible && app._waitingUncheckedSince > 0,
                    "a view no tick has checked yet warns of nothing at first")
                app._waitingUncheckedSince -= 15 * 60 * 1000 + 1000
                app.applyWaitingView(unticked)
                check(app.waitingWarning === "⚠ Waits not checked since never" && countText.text === "0?",
                    "still unchecked 15 minutes later: " + app.waitingWarning)
                app.applyWaitingView(fresh)
                check(app.waitingWarning === "" && app._waitingUncheckedSince === 0,
                    "a tick ends the unchecked wait")

                function local(d, h, m, s) { return new Date(2026, 10, d, h, m, s || 0) }
                var now = local(12, 9, 20).getTime()
                function health(h, unchecked) { return app.waitingHealth(h, now, unchecked) }
                function warn(h, unchecked) { return health(h, unchecked).text }
                function iso(d) { return d.toISOString() }
                check(warn(null) === "" && !health(null).uncertain, "no health (an older CLI): no warning")
                check(warn({ checked_at: iso(local(12, 9, 5, 1)) }) === "", "checked 14:59 ago is current")
                check(warn({ checked_at: iso(local(12, 9, 4, 59)) }) === "⚠ Waits not checked since 09:04",
                    "checked over 15 min ago: " + warn({ checked_at: iso(local(12, 9, 4, 59)) }))
                check(health({ checked_at: iso(local(12, 9, 4, 59)) }).uncertain, "a stale check is uncertain")
                check(warn({ checked_at: iso(local(11, 23, 50)) }) === "⚠ Waits not checked since Nov 11, 23:50",
                    "an older day shows the date")
                check(warn({ error: "boom" }) === "⚠ Waits not checked since never: boom"
                    && health({ error: "boom" }).uncertain, "a failure before any successful tick")
                var partialAt = function(at, extra) {
                    return health(Object.assign({ checked_at: iso(at), error: "PR checks: 502", error_kind: "partial" },
                                                extra || {}))
                }
                var recent = partialAt(local(12, 9, 5, 1))
                check(recent.text === "⚠ Last check: PR checks: 502" && !recent.uncertain,
                    "a partial error from a check 14:59 ago is the last check's, count certain: " + recent.text)
                var old = partialAt(local(12, 9, 4, 59))
                check(old.text === "⚠ Waits not checked since 09:04: PR checks: 502" && old.uncertain,
                    "a partial error from a check over 15 min ago keeps the old wording: " + old.text)
                var failedTick = partialAt(local(12, 9, 18), { error_kind: "failed" })
                check(failedTick.text === "⚠ Waits not checked since 09:18: PR checks: 502" && failedTick.uncertain,
                    "a failed tick is not a last check: " + failedTick.text)
                var olderCli = partialAt(local(12, 9, 18), { error_kind: undefined })
                check(olderCli.text === "⚠ Waits not checked since 09:18: PR checks: 502" && olderCli.uncertain,
                    "no error_kind (an older CLI) reads as a failed tick: " + olderCli.text)
                var scanned = partialAt(local(12, 9, 18), { agents_stale_since: iso(local(12, 9, 0)) })
                check(scanned.text === "⚠ Last check: PR checks: 502 · session scan stale since 09:00"
                    && scanned.uncertain, "a stale scan rides along a last check and keeps N?: " + scanned.text)
                var failedScan = partialAt(local(12, 9, 18), { error_kind: "failed",
                                                               agents_stale_since: iso(local(12, 9, 0)) })
                check(failedScan.text === "⚠ Waits not checked since 09:18: PR checks: 502" && failedScan.uncertain,
                    "a failed tick outranks a stale scan")
                var scanOnly = health({ checked_at: iso(local(12, 9, 18)), agents_stale_since: iso(local(12, 9, 0)) })
                check(scanOnly.text === "⚠ Session scan stale since 09:00" && scanOnly.uncertain,
                    "fresh ticks over a stale session scan")
                check(warn({ checked_at: null }, local(12, 9, 5, 1).getTime()) === "",
                    "unchecked for 14:59 is not yet a warning")
                check(warn({ error: " \\n " }, local(12, 9, 4, 59).getTime()) === "⚠ Waits not checked since never",
                    "a blank error is no error; unchecked for over 15 min warns")
                check(warn({ checked_at: null }) === "", "unchecked since unknown: no warning")
                var long = warn({ checked_at: iso(local(12, 9, 18)), error: new Array(201).join("x"),
                                  error_kind: "partial" })
                check(long === "⚠ Last check: " + new Array(120).join("x") + "…",
                    "a long error is cut to 120 characters: " + long.length)
                var emoji = warn({ checked_at: iso(local(12, 9, 4)), error: new Array(201).join("🙂") })
                check(emoji === "⚠ Waits not checked since 09:04: " + new Array(120).join("🙂") + "…",
                    "the cap counts characters, not halves of an emoji: " + emoji.length)
                console.log("PASS actual main waiting health")
                Qt.exit(0)
''', {"waitingView": view}, views=True)

    def test_waiting_view_that_reads_empty_keeps_the_last_view(self):
        # No waiting_view.json here. The CLI never deletes it, so once a view
        # was read, an empty read is a broken state dir, not "nothing waiting".
        self.applet('''
                if (app.cacheRestoring || app.loading) return
                if (fixture.stage === 0) {
                    Plasmoid.configuration.ternIntegration = true
                    app.refreshWaitingView()
                    fixture.stage = 1
                    return
                }
                if (app.waitingRequest) return
                if (fixture.stage === 1) {
                    check(app._waitingView === null && app.waitingOnMeCount === 0,
                        "no file before the first read: nothing waiting")
                    app.applyWaitingView({ counts: { on_me: 2 }, sessions: [],
                        health: { checked_at: new Date(Date.now() - 20 * 60 * 1000).toISOString() } })
                    app.refreshWaitingView()
                    fixture.stage = 2
                    return
                }
                check(app.waitingOnMeCount === 2, "an empty read keeps the last count")
                check(app.waitingWarning.indexOf("⚠ Waits not checked since ") === 0,
                    "the kept view turns stale by the clock: " + app.waitingWarning)
                console.log("PASS actual main waiting empty read")
                Qt.exit(0)
''')


if __name__ == "__main__":
    unittest.main()
