from __future__ import annotations

import contextlib
import errno
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
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
        self.assertIn(str(other), self.run_main("--status")[1])
        with mock.patch.object(self.installer, "OMP_EXTENSION_SOURCE", self.home / "package/omp-extension/waitingfor.ts"):
            self.assertIn("omp extension:               yes", self.run_main("--status")[1])

    def test_omp_extension_reports_a_link_it_cannot_make(self) -> None:
        self.omp_link.parent.parent.mkdir(parents=True)
        self.omp_link.parent.write_text("a file where the directory belongs\n")
        code, output = self.run_main("--omp-extension")
        self.assertEqual(code, 1)
        self.assertIn("can't link", output)


class MimeappsReplacementTests(unittest.TestCase):
    setUp = IntegrationPlatformTests.setUp

    def test_updates_preserve_symlink_and_target_mode(self) -> None:
        target = self.home / "associations.list"
        target.write_text("[Default Applications]\ntext/plain=editor.desktop;\n")
        target.chmod(0o640)
        config = self.installer.MIMEAPPS_FILE
        config.parent.mkdir(exist_ok=True)
        config.symlink_to(Path("..") / target.name)
        original_link = os.readlink(config)
        self.installer._set_mimeapps_default(self.installer.SCHEME_KEY, "new.desktop")
        self.assertIn(f"{self.installer.SCHEME_KEY}=new.desktop;", target.read_text())
        self.assertTrue(self.installer._drop_mimeapps_key(self.installer.SCHEME_KEY))
        self.assertEqual(target.read_text(), "[Default Applications]\ntext/plain=editor.desktop;\n")
        self.assertEqual(target.stat().st_mode & 0o7777, 0o640)
        self.assertTrue(config.is_symlink())
        self.assertEqual(os.readlink(config), original_link)
        self.assertEqual(list(target.parent.glob(".associations.list.*.tmp")), [])

    def test_failed_replace_preserves_bytes_and_removes_temporary(self) -> None:
        config = self.installer.MIMEAPPS_FILE
        config.parent.mkdir(exist_ok=True)
        original = b"[Default Applications]\nx-scheme-handler/codexbar-agentview=old.desktop;\n"
        for operation in ("set", "drop"):
            with self.subTest(operation=operation):
                config.write_bytes(original)
                with mock.patch.object(self.installer.os, "replace", side_effect=OSError(errno.EACCES, "denied")):
                    with self.assertRaises(OSError):
                        if operation == "set":
                            self.installer._set_mimeapps_default(self.installer.SCHEME_KEY, "new.desktop")
                        else:
                            self.installer._drop_mimeapps_key(self.installer.SCHEME_KEY)
                self.assertEqual(config.read_bytes(), original)
                self.assertEqual(list(config.parent.glob(".mimeapps.list.*.tmp")), [])


def _isolated_environment(home: Path) -> dict[str, str]:
    return {
        "HOME": str(home),
        "PATH": os.defpath,
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_CACHE_HOME": str(home / ".cache"),
        "XDG_DATA_HOME": str(home / ".local/share"),
        "XDG_STATE_HOME": str(home / ".local/state"),
        "XDG_RUNTIME_DIR": str(home / "run"),
        "OMP_AGENT_DIR": str(home / ".omp/agent"),
        "PI_CODING_AGENT_DIR": str(home / ".omp/agent"),
        "PYTHONDONTWRITEBYTECODE": "1",
    }


class MimeappsEncodingTests(unittest.TestCase):
    def assert_utf8_associations_survive_ascii_locale(self, operation: str) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            mimeapps = home / ".config/mimeapps.list"
            mimeapps.parent.mkdir()
            unrelated = "text/plain=éditeur.desktop;日本語.desktop;\n".encode("utf-8")
            scheme = b"x-scheme-handler/codexbar-agentview="
            original = (
                b"[Default Applications]\n"
                + unrelated
                + scheme + b"old.desktop;\n"
                + b"\n[Added Associations]\n"
                + "application/pdf=lecteur-écran.desktop;\n".encode("utf-8")
            )
            mimeapps.write_bytes(original)
            child = textwrap.dedent("""
                import importlib.util
                import sys
                from pathlib import Path

                assert sys.flags.utf8_mode == 0, "requires Python UTF-8 mode disabled"
                script = Path(sys.argv[1])
                sys.path.insert(0, str(script.parent))
                spec = importlib.util.spec_from_file_location("ascii_installer", script)
                installer = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(installer)
                if sys.argv[2] == "set":
                    installer._set_mimeapps_default(installer.SCHEME_KEY, "new.desktop")
                else:
                    assert installer._drop_mimeapps_key(installer.SCHEME_KEY)
            """)
            environment = _isolated_environment(home)
            environment.update({
                "LC_ALL": "C",
                "PYTHONUTF8": "0",
                "PYTHONCOERCECLOCALE": "0",
            })
            result = subprocess.run(
                [sys.executable, "-c", child, str(SCRIPT_PATH), operation],
                cwd=home, env=environment,
                capture_output=True, timeout=10, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", errors="replace"))
            expected = original.replace(
                scheme + b"old.desktop;\n",
                scheme + b"new.desktop;\n" if operation == "set" else b"",
            )
            self.assertEqual(
                mimeapps.read_bytes(), expected,
                "MIME updates must preserve unrelated UTF-8 desktop IDs under an ASCII locale",
            )

    def test_set_preserves_utf8_associations_under_ascii_locale(self) -> None:
        self.assert_utf8_associations_survive_ascii_locale("set")

    def test_drop_preserves_utf8_associations_under_ascii_locale(self) -> None:
        self.assert_utf8_associations_survive_ascii_locale("drop")


@unittest.skipUnless(sys.platform == "linux", "requires Linux RLIMIT_FSIZE")
class MimeappsAtomicWriteTests(unittest.TestCase):
    def assert_failed_write_preserves_associations(self, operation: str) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            mimeapps = home / ".config/mimeapps.list"
            mimeapps.parent.mkdir()
            original = (
                b"# Keep unrelated user associations intact.\n"
                b"[Default Applications]\n"
                b"text/plain=editor.desktop;other-editor.desktop;\n"
                b"x-scheme-handler/codexbar-agentview=old.desktop;\n"
                b"x-scheme-handler/https=browser.desktop;\n"
                b"\n[Added Associations]\n"
                b"application/pdf=reader.desktop;browser.desktop;\n"
            )
            mimeapps.write_bytes(original)
            child = textwrap.dedent("""
                import importlib.util
                import json
                import resource
                import signal
                import sys
                from pathlib import Path

                script = Path(sys.argv[1])
                sys.path.insert(0, str(script.parent))
                spec = importlib.util.spec_from_file_location("limited_installer", script)
                installer = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(installer)
                signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
                resource.setrlimit(resource.RLIMIT_FSIZE, (32, 32))
                try:
                    if sys.argv[2] == "set":
                        installer._set_mimeapps_default(installer.SCHEME_KEY, "new.desktop")
                    else:
                        installer._drop_mimeapps_key(installer.SCHEME_KEY)
                except OSError as error:
                    print(json.dumps({"errno": error.errno}))
                else:
                    print(json.dumps({"errno": None}))
            """)
            result = subprocess.run(
                [sys.executable, "-c", child, str(SCRIPT_PATH), operation],
                cwd=home, env=_isolated_environment(home),
                capture_output=True, text=True, timeout=10, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["errno"], errno.EFBIG,
                             "the operation must encounter the real file-size write limit")
            self.assertEqual(
                mimeapps.read_bytes(), original,
                "a failed MIME update must preserve every original association byte",
            )

    def test_set_mimeapps_default_preserves_existing_bytes_on_write_failure(self) -> None:
        self.assert_failed_write_preserves_associations("set")

    def test_drop_mimeapps_key_preserves_existing_bytes_on_write_failure(self) -> None:
        self.assert_failed_write_preserves_associations("drop")


class PackagedIntegrationTests(unittest.TestCase):
    def test_staged_package_installs_a_self_contained_omp_extension(self) -> None:
        package = Path(os.environ.get(
            "CODEXBAR_TEST_PACKAGE_DIR", str(SCRIPT_PATH.parents[2] / "build/package"),
        )).resolve()
        if not (package / "contents/scripts/install_integration.py").is_file():
            self.skipTest(
                "requires the staged CMake package; build package or set "
                "CODEXBAR_TEST_PACKAGE_DIR to its directory"
            )
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            installed = home / "installed-package"
            shutil.copytree(package, installed)
            environment = _isolated_environment(home)
            result = subprocess.run(
                [sys.executable, str(installed / "contents/scripts/install_integration.py"),
                 "--omp-extension"],
                cwd=home, env=environment,
                capture_output=True, text=True, timeout=10, check=False,
            )
            self.assertEqual(
                result.returncode, 0,
                f"the shipped installer must link the omp extension: {result.stdout}{result.stderr}",
            )
            link = home / ".omp/agent/extensions/codexbar-waitingfor.ts"
            self.assertTrue(link.is_symlink(), "the packaged installer did not install its extension")
            extension = link.resolve(strict=True)
            self.assertTrue(extension.is_relative_to(installed),
                            "the extension must not depend on the source checkout")
            self.assertTrue(extension.is_file())
            scripts = extension.parent.parent / "contents/scripts"
            cli = scripts / "codexbar_waiting.py"
            menu = scripts / "waiting_menu.json"
            self.assertTrue(cli.is_file(), "the linked extension's CLI must be shipped")
            self.assertTrue(menu.is_file(), "the linked extension's menu must be shipped")
            response = subprocess.run(
                [sys.executable, str(cli), "menu"],
                cwd=home, env=environment,
                capture_output=True, text=True, timeout=10, check=False,
            )
            self.assertEqual(response.returncode, 0, response.stderr)
            answer = json.loads(response.stdout)
            self.assertTrue(answer["ok"], answer)
            self.assertTrue(any("session" in item["targets"] for item in answer["items"]),
                            "the packaged CLI must load a usable session menu")


if __name__ == "__main__":
    unittest.main()
