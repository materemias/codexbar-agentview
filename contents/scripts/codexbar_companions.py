#!/usr/bin/env python3
"""Which of the tools that work well with CodexBar AgentView this machine has.

Prints JSON for the About pages of the Tern plugin and the Plasma applet:

    {"companions": [{"id", "name", "url", "benefit", "installed", "status"}, ...]}

`installed` is true when every part of the tool was found: its omp plugin or
extension, and its Tern plugin where it has one. `status` says the same for
the page: "Installed", "Not found", or which part is missing. It reports
files on disk, never whether omp or Tern loaded them.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

from install_integration import CONFIG_DIR, DATA_DIR, omp_agent_dir

COMPANIONS = (
    {
        "id": "tern-awui", "name": "tern-awui",
        "url": "https://github.com/materemias/tern-awui#install",
        "benefit": "The Waiting list, snooze, seen, stop and push notifications on your phone.",
    },
    {
        "id": "omp-stretch", "name": "omp-stretch",
        "url": "https://github.com/materemias/omp-stretch#install",
        "benefit": "omp replies arrive as their gist, and session previews here show the gist, not the markup.",
    },
    {
        "id": "omp-compact", "name": "omp-compact",
        "url": "https://github.com/arksdev/omp-compact#install-from-the-marketplace",
        "benefit": "Folds omp's tool log to one line per action; with omp-stretch a long run reads in a few lines.",
    },
)


def tern_plugins_dir() -> Path:
    """Tern's plugin folder: `tern plugin dir` when Tern is installed, else its default."""
    tern = os.environ.get("TERN_EXE") or shutil.which("tern")
    if tern:
        try:
            result = subprocess.run([tern, "plugin", "dir"], capture_output=True, text=True,
                                    timeout=5, check=False)
            found = result.stdout.strip()
            if result.returncode == 0 and found and os.path.isabs(found):
                return Path(found)
        except (OSError, subprocess.SubprocessError):
            pass
    configured = os.environ.get("TERN_CONFIG_DIR", "")
    if os.path.isabs(configured):
        return Path(configured) / "plugins"
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA", "")
        root = Path(appdata) if os.path.isabs(appdata) else Path.home() / "AppData" / "Roaming"
        return root / "Tern" / "plugins"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Tern" / "plugins"
    return CONFIG_DIR / "tern" / "plugins"


def has_tern_plugin(plugins: Path, plugin_id: str) -> bool:
    """Linked (`<id>.path` naming an existing folder) or installed (`<id>/plugin.toml`)."""
    try:
        linked = (plugins / f"{plugin_id}.path").read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        linked = ""
    if linked and Path(linked).expanduser().is_dir():
        return True
    return (plugins / plugin_id / "plugin.toml").is_file()


def omp_plugin_dirs() -> list[Path]:
    """omp's plugin folders: ~/.omp/plugins, or $XDG_DATA_HOME/omp/plugins once omp uses XDG."""
    dirs = [Path.home() / (os.environ.get("PI_CONFIG_DIR") or ".omp") / "plugins"]
    if sys.platform != "win32" and (DATA_DIR / "omp").is_dir():
        dirs.append(DATA_DIR / "omp" / "plugins")
    return dirs


_YAML_COMMENT = re.compile(r"\s+#.*$")


def omp_extension_entries(agent_dir: Path) -> list[Path]:
    """The `extensions` paths of omp's settings.json and config.yml that exist on disk."""
    entries: list[str] = []
    try:
        settings = json.loads((agent_dir / "settings.json").read_text(encoding="utf-8"))
        listed = settings.get("extensions") if isinstance(settings, dict) else None
        entries += [entry for entry in listed if isinstance(entry, str)] if isinstance(listed, list) else []
    except (OSError, ValueError):
        pass
    try:
        lines = (agent_dir / "config.yml").read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        lines = []
    inside = False
    for line in lines:
        line = _YAML_COMMENT.sub("", line)
        if line.startswith("extensions:"):
            rest = line[len("extensions:"):].strip()
            if rest.startswith("[") and rest.endswith("]"):
                entries += [item.strip().strip("\"'") for item in rest[1:-1].split(",") if item.strip()]
            inside = rest == ""
        elif inside and line.lstrip().startswith("- "):
            entries.append(line.lstrip()[2:].strip().strip("\"'"))
        elif inside and line.strip() and not line[0].isspace():
            inside = False
    found = []
    for entry in entries:
        path = Path(entry).expanduser()
        path = path if path.is_absolute() else agent_dir / path
        if path.exists():
            found.append(path)
    return found


def has_omp_plugin(plugin_dirs: list[Path], entries: list[Path], key: str, package: str,
                   matches: Callable[[Path], bool]) -> bool:
    for plugins in plugin_dirs:
        try:
            installed = json.loads((plugins / "installed_plugins.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            installed = None
        listed = installed.get("plugins") if isinstance(installed, dict) else None
        if isinstance(listed, dict) and key in listed:
            return True
        if (plugins / "node_modules" / package).is_dir():
            return True
    return any(matches(entry) for entry in entries)


# omp-stretch's omp half: its marketplace key, its package, and the entry file of a checkout.
_OMP_STRETCH = ("omp-stretch@omp-stretch", "omp-stretch",
                lambda path: path.parts[-2:] == ("omp-stretch", "stretch.ts"))


def omp_stretch_installed() -> bool:
    """Whether omp's half of omp-stretch is here, the half that has the model write stretches."""
    return has_omp_plugin(omp_plugin_dirs(), omp_extension_entries(omp_agent_dir()), *_OMP_STRETCH)


def statuses() -> list[dict]:
    agent_dir = omp_agent_dir()
    plugins = tern_plugins_dir()
    omp_dirs = omp_plugin_dirs()
    entries = omp_extension_entries(agent_dir)
    found = {
        "tern-awui": {
            "omp extension": (agent_dir / "extensions" / "tern-mobile.ts").exists(),
            "Tern plugin": has_tern_plugin(plugins, "tern-awui"),
        },
        "omp-stretch": {
            "omp plugin": has_omp_plugin(omp_dirs, entries, *_OMP_STRETCH),
            "Tern plugin": has_tern_plugin(plugins, "omp-stretch"),
        },
        "omp-compact": {
            "omp plugin": has_omp_plugin(omp_dirs, entries, "omp-compact@arksdev", "omp-compact",
                                lambda path: "omp-compact" in path.parts),
        },
    }
    rows = []
    for companion in COMPANIONS:
        parts = found[companion["id"]]
        missing = [part for part, here in parts.items() if not here]
        status = ("Installed" if not missing else "Not found" if len(missing) == len(parts)
                  else f"{' and '.join(missing)} not found")
        # Installed: the button opens the project; otherwise its install steps.
        url = companion["url"].split("#")[0] if not missing else companion["url"]
        rows.append({**companion, "url": url, "installed": not missing, "status": status})
    return rows


def main() -> int:
    print(json.dumps({"companions": statuses()}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
