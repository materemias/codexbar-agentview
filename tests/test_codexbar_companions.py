from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "contents" / "scripts" / "codexbar_companions.py"
sys.path.insert(0, str(SCRIPT_PATH.parent))
SPEC = importlib.util.spec_from_file_location("codexbar_companions", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
companions = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(companions)


class CompanionStatusTests(unittest.TestCase):
    """What the About pages say about tern-awui, omp-stretch and omp-compact."""

    def setUp(self) -> None:
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.home = Path(scratch.name)
        self.agent = self.home / ".omp" / "agent"
        self.omp_plugins = self.home / ".omp" / "plugins"
        self.tern_plugins = self.home / "tern-config" / "plugins"
        for folder in (self.agent / "extensions", self.omp_plugins, self.tern_plugins):
            folder.mkdir(parents=True)
        env = {"PI_CODING_AGENT_DIR": str(self.agent), "TERN_CONFIG_DIR": str(self.home / "tern-config")}
        for patch in (
            mock.patch.dict(os.environ, env),
            mock.patch.object(companions.Path, "home", return_value=self.home),
            mock.patch.object(companions, "DATA_DIR", self.home / "data"),
            # Never the real Tern: its plugin folder is the user's.
            mock.patch.object(companions.shutil, "which", return_value=None),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        os.environ.pop("TERN_EXE", None)
        os.environ.pop("PI_CONFIG_DIR", None)
        os.environ.pop("OMP_AGENT_DIR", None)

    def status(self) -> dict[str, tuple[bool, str]]:
        return {row["id"]: (row["installed"], row["status"]) for row in companions.statuses()}

    def link_tern_plugin(self, plugin_id: str, target: Path) -> None:
        target.mkdir(parents=True, exist_ok=True)
        (self.tern_plugins / f"{plugin_id}.path").write_text(f"{target}\n")

    def test_nothing_installed_is_not_found(self) -> None:
        self.assertEqual(self.status(), {
            "tern-awui": (False, "Not found"),
            "omp-stretch": (False, "Not found"),
            "omp-compact": (False, "Not found"),
        })

    def test_every_install_route_counts(self) -> None:
        (self.agent / "extensions" / "tern-mobile.ts").write_text("export {}\n")
        self.link_tern_plugin("tern-awui", self.home / "code" / "tern-awui" / "tern-plugin")
        # omp-stretch from a checkout listed in settings.json, its Tern half installed by copy.
        stretch = self.home / "code" / "omp-stretch" / "stretch.ts"
        stretch.parent.mkdir(parents=True)
        stretch.write_text("export {}\n")
        (self.agent / "settings.json").write_text(json.dumps({"extensions": [str(stretch)]}))
        (self.tern_plugins / "omp-stretch").mkdir()
        (self.tern_plugins / "omp-stretch" / "plugin.toml").write_text("id = 'omp-stretch'\n")
        (self.omp_plugins / "installed_plugins.json").write_text(
            json.dumps({"version": 2, "plugins": {"omp-compact@arksdev": [{"scope": "user"}]}}))
        self.assertEqual(self.status(), {
            "tern-awui": (True, "Installed"),
            "omp-stretch": (True, "Installed"),
            "omp-compact": (True, "Installed"),
        })
        # Installed, the link opens the project rather than its install steps.
        self.assertTrue(all("#" not in row["url"] for row in companions.statuses()))

    def test_a_missing_half_is_named_and_a_stale_link_does_not_count(self) -> None:
        (self.omp_plugins / "node_modules" / "omp-stretch").mkdir(parents=True)
        (self.agent / "extensions" / "tern-mobile.ts").write_text("export {}\n")
        (self.tern_plugins / "tern-awui.path").write_text(str(self.home / "moved-away"))
        compact = self.home / "code" / "omp-compact" / ".omp-plugin" / "index.ts"
        compact.parent.mkdir(parents=True)
        compact.write_text("export {}\n")
        (self.agent / "config.yml").write_text(
            f"smol: x\nextensions:  # mine\n  - \"{compact}\"  # checkout\nmodel: y\n")
        self.assertEqual(self.status(), {
            "tern-awui": (False, "Tern plugin not found"),
            "omp-stretch": (False, "Tern plugin not found"),
            "omp-compact": (True, "Installed"),
        })

    def test_config_yml_inline_list_and_settings_noise(self) -> None:
        stretch = self.home / "omp-stretch" / "stretch.ts"
        stretch.parent.mkdir()
        stretch.write_text("export {}\n")
        (self.agent / "settings.json").write_text("{not json")
        (self.agent / "config.yml").write_text(f"extensions: [a.ts, '{stretch}']\n")
        self.assertEqual(self.status()["omp-stretch"], (False, "Tern plugin not found"))

    def test_an_extension_entry_whose_file_is_gone_does_not_count(self) -> None:
        gone = self.home / "deleted" / "omp-compact" / ".omp-plugin" / "index.ts"
        (self.agent / "settings.json").write_text(json.dumps({"extensions": [str(gone)]}))
        self.assertEqual(self.status()["omp-compact"], (False, "Not found"))

    @unittest.skipIf(sys.platform == "win32", "a POSIX shell script stands in for tern")
    def test_tern_names_its_own_plugin_folder(self) -> None:
        elsewhere = self.home / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "omp-stretch.path").write_text(str(self.home))
        (self.omp_plugins / "node_modules" / "omp-stretch").mkdir(parents=True)
        tern = self.home / "tern"
        tern.write_text(f"#!/bin/sh\n[ \"$1 $2\" = 'plugin dir' ] && echo '{elsewhere}'\n")
        tern.chmod(0o755)
        with mock.patch.dict(os.environ, {"TERN_EXE": str(tern)}):
            self.assertEqual(self.status()["omp-stretch"], (True, "Installed"))

    def test_prints_json(self) -> None:
        with mock.patch("builtins.print") as printed:
            self.assertEqual(companions.main(), 0)
        rows = json.loads(printed.call_args.args[0])["companions"]
        self.assertEqual([row["id"] for row in rows], ["tern-awui", "omp-stretch", "omp-compact"])
        self.assertTrue(all(row["url"].startswith("https://github.com/") and row["benefit"] for row in rows))


if __name__ == "__main__":
    unittest.main()
