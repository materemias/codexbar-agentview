from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "contents/scripts/install_integration.py"
sys.path.insert(0, str(SCRIPT_PATH.parent))


class IntegrationPlatformTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.home = Path(directory.name)
        spec = importlib.util.spec_from_file_location("isolated_install_integration", SCRIPT_PATH)
        assert spec is not None and spec.loader is not None
        self.installer = importlib.util.module_from_spec(spec)
        with mock.patch.object(Path, "home", return_value=self.home):
            spec.loader.exec_module(self.installer)
        commands = mock.patch.object(self.installer.subprocess, "run")
        self.commands = commands.start()
        self.addCleanup(commands.stop)
        environment = mock.patch.dict(os.environ)
        environment.start()
        self.addCleanup(environment.stop)
        os.environ.pop("OMP_AGENT_DIR", None)
        os.environ.pop("PI_CODING_AGENT_DIR", None)
        self.omp_link = self.home / ".omp/agent/extensions/codexbar-waitingfor.ts"
        self.installer.CLAUDE_SETTINGS.parent.mkdir(parents=True)
        self.installer.CLAUDE_SETTINGS.write_text(json.dumps({
            "hooks": {"Stop": [{"hooks": [{"command": "codexbar_agent_hook"}]}]},
            "model": "keep-this-setting",
        }))

    def snapshot(self) -> dict:
        return {
            str(path.relative_to(self.home)): (
                path.stat().st_mode,
                path.read_bytes() if path.is_file() else None,
            )
            for path in self.home.rglob("*")
        }

    def seed_artifacts(self) -> None:
        for path in (
            self.installer.PLASMA_ENV_FILE,
            self.installer.SYSTEMD_ENV_FILE,
            self.installer.DESKTOP_FILE,
            self.installer.LEGACY_DESKTOP_FILE,
            self.installer.LEGACY_SYSTEMD_FILE,
            *self.installer.LEGACY_ENV_FILES,
        ):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("existing artifact\n")
        self.installer.MIMEAPPS_FILE.write_text(
            "[Default Applications]\nx-scheme-handler/codexbar=legacy.desktop;\n"
            "x-scheme-handler/codexbar-agentview=current.desktop;\n"
        )

    def test_unsupported_actions_leave_home_and_services_untouched(self) -> None:
        for platform in ("darwin", "win32"):
            for existing in (False, True):
                if existing:
                    self.seed_artifacts()
                before = self.snapshot()
                with mock.patch.object(self.installer.sys, "platform", platform), \
                        mock.patch.object(self.installer.state_paths, "migrate") as migrate:
                    for arguments in ([], ["--uninstall"]):
                        with self.subTest(platform=platform, existing=existing, arguments=arguments):
                            error = io.StringIO()
                            with contextlib.redirect_stderr(error):
                                self.assertEqual(self.installer.main(arguments), 1)
                            self.assertIn("Linux-only", error.getvalue())
                            self.assertEqual(self.snapshot(), before)
                    for action in (self.installer.install_all, self.installer.uninstall_all):
                        with self.assertRaises(RuntimeError):
                            action()
                    migrate.assert_not_called()
                self.commands.assert_not_called()

    def test_macos_status_explains_direct_tern_integration_without_mutation(self) -> None:
        self.seed_artifacts()
        before = self.snapshot()
        output = io.StringIO()
        with mock.patch.object(self.installer.sys, "platform", "darwin"), \
                contextlib.redirect_stdout(output):
            self.assertEqual(self.installer.main(["--status"]), 0)
        self.assertIn("Linux-only", output.getvalue())
        self.assertIn("Tern needs no Plasma integration", output.getvalue())
        self.assertEqual(self.snapshot(), before)
        self.commands.assert_not_called()

    def test_linux_install_and_uninstall_keep_plasma_paths(self) -> None:
        self.seed_artifacts()
        state = self.home / "state"
        state.mkdir()
        (state / "agents.json").write_text("{}")
        with mock.patch.object(self.installer.sys, "platform", "linux"), \
                mock.patch.object(self.installer.state_paths, "migrate", return_value=state), \
                mock.patch.object(self.installer.os, "chmod"), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.installer.main([]), 0)
            self.assertEqual(self.installer.PLASMA_ENV_FILE.read_text(),
                             self.installer.PLASMA_ENV_CONTENTS)
            self.assertEqual(self.installer.SYSTEMD_ENV_FILE.read_text(),
                             self.installer.SYSTEMD_ENV_CONTENTS)
            self.assertIn("focus url handler:           yes", self.installer.status_all())
            desktop = self.installer.DESKTOP_FILE.read_text()
            self.assertIn(f"Exec={self.installer.FOCUS_SCRIPT_PATH} %u", desktop)
            self.assertIn("x-scheme-handler/codexbar-agentview", desktop)
            self.assertIn("x-scheme-handler/codexbar-agentview=codexbar-agentview-focus.desktop;",
                          self.installer.MIMEAPPS_FILE.read_text())
            self.assertFalse(self.installer.LEGACY_DESKTOP_FILE.exists())
            self.assertFalse(self.installer.LEGACY_SYSTEMD_FILE.exists())
            settings = json.loads(self.installer.CLAUDE_SETTINGS.read_text())
            self.assertEqual(settings, {"model": "keep-this-setting"})
            self.assertEqual(self.installer.main(["--uninstall"]), 0)
        self.assertFalse(self.installer.PLASMA_ENV_FILE.exists())
        self.assertFalse(self.installer.SYSTEMD_ENV_FILE.exists())
        self.assertFalse(self.installer.DESKTOP_FILE.exists())
        self.assertNotIn("x-scheme-handler/codexbar-agentview=",
                         self.installer.MIMEAPPS_FILE.read_text())
        commands = [call.args[0] for call in self.commands.call_args_list]
        self.assertIn(["systemctl", "--user", "set-environment", "QML_XHR_ALLOW_FILE_READ=1"], commands)
        self.assertIn(["systemctl", "--user", "unset-environment", "QML_XHR_ALLOW_FILE_READ"], commands)

    def run_main(self, *arguments: str) -> tuple[int, str]:
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            code = self.installer.main(list(arguments))
        return code, output.getvalue()

    def test_omp_extension_installs_and_removes_only_its_symlink_on_macos(self) -> None:
        with mock.patch.object(self.installer.sys, "platform", "darwin"):
            self.assertEqual(self.run_main("--omp-extension")[0], 0)
            self.assertTrue(self.omp_link.is_symlink())
            self.assertEqual(self.omp_link.resolve(), self.installer.OMP_EXTENSION_SOURCE)
            self.assertEqual(list(self.omp_link.parent.iterdir()), [self.omp_link])
            self.assertIn("already current", self.run_main("--omp-extension")[1])
            self.assertIn("omp extension:               yes", self.run_main("--status")[1])
            self.assertEqual(self.run_main("--omp-extension", "--uninstall")[0], 0)
            self.assertFalse(self.omp_link.is_symlink())
            self.assertIn("omp extension:               no", self.run_main("--status")[1])
        self.assertFalse(self.installer.PLASMA_ENV_FILE.exists())
        self.commands.assert_not_called()

    def test_omp_extension_respects_omp_agent_dir(self) -> None:
        agent_dir = self.home / "elsewhere"
        with mock.patch.dict(os.environ, {"OMP_AGENT_DIR": str(agent_dir)}):
            self.assertEqual(self.run_main("--omp-extension")[0], 0)
        self.assertTrue((agent_dir / "extensions/codexbar-waitingfor.ts").is_symlink())
        self.assertFalse(self.omp_link.is_symlink())

    def test_omp_extension_prefers_omps_own_agent_dir(self) -> None:
        ours, omps = self.home / "ours", self.home / "omps"
        with mock.patch.dict(os.environ, {"OMP_AGENT_DIR": str(ours), "PI_CODING_AGENT_DIR": str(omps)}):
            self.assertEqual(self.run_main("--omp-extension")[0], 0)
        self.assertTrue((omps / "extensions/codexbar-waitingfor.ts").is_symlink())
        self.assertFalse((ours / "extensions/codexbar-waitingfor.ts").exists())

    def test_omp_extension_replaces_a_moved_checkouts_dangling_link(self) -> None:
        self.omp_link.parent.mkdir(parents=True)
        self.omp_link.symlink_to("/gone/repo/omp-extension/waitingfor.ts")
        self.assertIn("broken link", self.run_main("--status")[1])
        self.assertEqual(self.run_main("--omp-extension")[0], 0)
        self.assertEqual(self.omp_link.resolve(), self.installer.OMP_EXTENSION_SOURCE)

    def test_omp_extension_leaves_what_is_not_ours(self) -> None:
        self.omp_link.parent.mkdir(parents=True)
        foreign = self.home / "foreign.ts"
        foreign.write_text("export default () => {};\n")
        for make in (lambda: self.omp_link.write_text("mine\n"), lambda: self.omp_link.symlink_to(foreign)):
            make()
            before = os.readlink(self.omp_link) if self.omp_link.is_symlink() else self.omp_link.read_text()
            code, output = self.run_main("--omp-extension")
            self.assertEqual(code, 1)
            self.assertIn("isn't ours", output)
            self.assertIn("not ours", self.run_main("--status")[1])
            self.assertEqual(self.run_main("--omp-extension", "--uninstall")[0], 0)
            after = os.readlink(self.omp_link) if self.omp_link.is_symlink() else self.omp_link.read_text()
            self.assertEqual(after, before)
            self.omp_link.unlink()

    def test_omp_extension_without_its_source_fails_alone(self) -> None:
        missing = self.home / "package/omp-extension/waitingfor.ts"
        state = self.home / "state"
        state.mkdir()
        (state / "agents.json").write_text("{}")
        with mock.patch.object(self.installer, "OMP_EXTENSION_SOURCE", missing), \
                mock.patch.object(self.installer.sys, "platform", "linux"), \
                mock.patch.object(self.installer.state_paths, "migrate", return_value=state), \
                mock.patch.object(self.installer.os, "chmod"):
            self.assertEqual(self.run_main("--omp-extension")[0], 1)
            code, output = self.run_main()
            self.assertEqual(code, 0)
            self.assertIn("omp extension: not in this copy", output)
        self.assertFalse(self.omp_link.is_symlink())
        self.assertTrue(self.installer.PLASMA_ENV_FILE.is_file())

    def test_linux_install_and_uninstall_cover_the_omp_extension(self) -> None:
        state = self.home / "state"
        state.mkdir()
        (state / "agents.json").write_text("{}")
        with mock.patch.object(self.installer.sys, "platform", "linux"), \
                mock.patch.object(self.installer.state_paths, "migrate", return_value=state), \
                mock.patch.object(self.installer.os, "chmod"):
            self.assertEqual(self.run_main()[0], 0)
            self.assertTrue(self.omp_link.is_symlink())
            self.assertIn("omp extension:               yes", self.installer.status_all())
            self.assertEqual(self.run_main("--uninstall")[0], 0)
        self.assertFalse(self.omp_link.is_symlink())

    def test_omp_extension_status_names_another_checkout(self) -> None:
        other = self.home / "other/omp-extension/waitingfor.ts"
        other.parent.mkdir(parents=True)
        other.write_text("export default () => {};\n")
        self.omp_link.parent.mkdir(parents=True)
        self.omp_link.symlink_to(other)
        self.assertIn(f"other checkout: {other}", self.run_main("--status")[1])
        with mock.patch.object(self.installer, "OMP_EXTENSION_SOURCE", self.home / "package/omp-extension/waitingfor.ts"):
            self.assertIn("omp extension:               yes", self.run_main("--status")[1])

    def test_omp_extension_reports_a_link_it_cannot_make(self) -> None:
        self.omp_link.parent.parent.mkdir(parents=True)
        self.omp_link.parent.write_text("a file where the directory belongs\n")
        code, output = self.run_main("--omp-extension")
        self.assertEqual(code, 1)
        self.assertIn("can't link", output)


if __name__ == "__main__":
    unittest.main()
