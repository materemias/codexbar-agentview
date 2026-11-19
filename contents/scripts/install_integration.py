#!/usr/bin/env python3
"""Set up CodexBar AgentView's Linux Plasma integration and its omp extension.

Hookless — the widget itself runs the aggregator every poll tick. This
script handles the bits the widget can't:

  * QML XHR env: drops env files so plasmashell gets QML_XHR_ALLOW_FILE_READ=1
    on every login, no matter how it's restarted.
  * codexbar-agentview:// URL scheme: registers a desktop entry so the
    widget's click-to-focus action can launch codexbar_focus.py.
  * Legacy identity cleanup: removes the codexbar:// handler and
    codexbar-xhr env files earlier versions installed.
  * omp extension: symlinks omp-extension/waitingfor.ts into omp's
    extensions directory (on Windows, where a symlink needs extra rights, a
    one-line loader file that imports it). This part also runs on macOS and
    Windows, on its own, with --omp-extension.

Usage:
  install_integration.py                  install everything
  install_integration.py --uninstall      remove everything we installed
  install_integration.py --status         report what's currently installed
  install_integration.py --omp-extension [--uninstall]
                                          install or remove only the omp extension
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path

import state_paths

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).resolve().parent
AGGREGATOR_PATH = SCRIPT_DIR / "codexbar_agents.py"
FOCUS_SCRIPT_PATH = SCRIPT_DIR / "codexbar_focus.py"


def _xdg_dir(variable: str, default: str) -> Path:
    # The spec ignores a relative value.
    value = os.environ.get(variable, "")
    return Path(value) if os.path.isabs(value) else Path.home() / default


CONFIG_DIR = _xdg_dir("XDG_CONFIG_HOME", ".config")
DATA_DIR = _xdg_dir("XDG_DATA_HOME", ".local/share")

# Sourced by plasmashell at session start (KDE-specific path).
PLASMA_ENV_DIR = CONFIG_DIR / "plasma-workspace" / "env"
PLASMA_ENV_FILE = PLASMA_ENV_DIR / "codexbar-agentview-xhr.sh"
PLASMA_ENV_CONTENTS = (
    "#!/bin/sh\n"
    "# Installed by CodexBar AgentView. Enables file:// XHR reads in plasmashell\n"
    "# so the widget can read agents.json from ~/.local/state/codexbar-agentview\n"
    "# (or $XDG_STATE_HOME/codexbar-agentview) directly.\n"
    "export QML_XHR_ALLOW_FILE_READ=1\n"
)

# Read by systemd-user at login via pam_systemd → propagates to every shell
# and user service. Covers manual restart paths like `kstart plasmashell`
# or `plasmashell --replace` from an existing terminal that the plasma-
# workspace/env script doesn't reach.
SYSTEMD_ENV_DIR = CONFIG_DIR / "environment.d"
SYSTEMD_ENV_FILE = SYSTEMD_ENV_DIR / "codexbar-agentview-xhr.conf"
SYSTEMD_ENV_CONTENTS = (
    "# Installed by CodexBar AgentView. Keeps QML_XHR_ALLOW_FILE_READ in the\n"
    "# systemd-user environment so plasmashell picks it up no matter how\n"
    "# it's launched, letting the widget read agents.json from\n"
    "# ~/.local/state/codexbar-agentview (or $XDG_STATE_HOME/codexbar-agentview).\n"
    "QML_XHR_ALLOW_FILE_READ=1\n"
)

# URL handler for the focus-on-click action.
DESKTOP_DIR = DATA_DIR / "applications"
DESKTOP_FILE = DESKTOP_DIR / "codexbar-agentview-focus.desktop"
MIMEAPPS_FILE = CONFIG_DIR / "mimeapps.list"
SCHEME = "codexbar-agentview"
SCHEME_KEY = f"x-scheme-handler/{SCHEME}"

# Legacy: names used before the CodexBar AgentView rename.
LEGACY_DESKTOP_FILE = DESKTOP_DIR / "org.codexbar.focus.desktop"
LEGACY_SCHEME_KEY = "x-scheme-handler/codexbar"
LEGACY_ENV_FILES = (PLASMA_ENV_DIR / "codexbar-xhr.sh", SYSTEMD_ENV_DIR / "codexbar-xhr.conf")

# Symlinked into omp's extensions directory from a checkout or applet package;
# on Windows a loader file (OMP_LOADER_MARKER, then re-exports of its file URL).
OMP_EXTENSION_SOURCE = SCRIPT_DIR.parents[1] / "omp-extension" / "waitingfor.ts"
OMP_EXTENSION_NAME = "codexbar-waitingfor.ts"
OMP_LOADER_MARKER = "// codexbar-agentview: loads its omp extension from the checkout (a Windows symlink needs extra rights).\n"
DEFAULT_OMP_AGENT_DIR = Path.home() / ".omp" / "agent"


# ---------------------------------------------------------------------------
# Env script
# ---------------------------------------------------------------------------

def install_env_script() -> str:
    msgs: list[str] = []

    # 1. Plasma-workspace env (sourced by startplasma).
    PLASMA_ENV_DIR.mkdir(parents=True, exist_ok=True)
    if not (PLASMA_ENV_FILE.is_file()
            and _try_read(PLASMA_ENV_FILE) == PLASMA_ENV_CONTENTS):
        PLASMA_ENV_FILE.write_text(PLASMA_ENV_CONTENTS)
        try:
            os.chmod(PLASMA_ENV_FILE, 0o755)
        except OSError:
            pass
        msgs.append(f"installed {PLASMA_ENV_FILE.name}")

    # 2. systemd-user environment (read by pam_systemd at login).
    SYSTEMD_ENV_DIR.mkdir(parents=True, exist_ok=True)
    if not (SYSTEMD_ENV_FILE.is_file()
            and _try_read(SYSTEMD_ENV_FILE) == SYSTEMD_ENV_CONTENTS):
        SYSTEMD_ENV_FILE.write_text(SYSTEMD_ENV_CONTENTS)
        msgs.append(f"installed {SYSTEMD_ENV_FILE.name}")

    # 3. Push into the running systemd-user manager so subsequent restarts
    # (kquitapp6/kstart, plasmashell --replace, etc) from the current session
    # see the var without waiting for next login.
    if _systemctl("set-environment", "QML_XHR_ALLOW_FILE_READ=1")[0] == 0:
        msgs.append("set in current systemd-user environment")

    return "env: " + ("; ".join(msgs) if msgs else "already current")


def _try_read(path: Path) -> str:
    try:
        return path.read_text()
    except OSError:
        return ""


def remove_env_script() -> str:
    removed: list[str] = []
    for f in (PLASMA_ENV_FILE, SYSTEMD_ENV_FILE):
        if f.is_file():
            try:
                f.unlink()
                removed.append(f.name)
            except OSError:
                pass
    _systemctl("unset-environment", "QML_XHR_ALLOW_FILE_READ")
    return "env: removed " + (", ".join(removed) if removed else "nothing")


# ---------------------------------------------------------------------------
# URL scheme handler
# ---------------------------------------------------------------------------

def _desktop_exec_arg(path: Path) -> str:
    """Quote an Exec argument per the Desktop Entry spec: backslash-escape
    the quoting's reserved characters, then the string level's backslashes;
    a literal % is %%."""
    quoted = re.sub(r'(["`$\\])', r"\\\1", str(path)).replace("%", "%%")
    return '"' + quoted.replace("\\", "\\\\") + '"'


def install_focus_url_handler() -> str:
    if not FOCUS_SCRIPT_PATH.is_file():
        return f"skipped url handler (focus script missing: {FOCUS_SCRIPT_PATH})"
    try:
        os.chmod(FOCUS_SCRIPT_PATH, 0o755)
    except OSError:
        pass
    desktop_contents = (
        "[Desktop Entry]\n"
        "Name=CodexBar AgentView Focus Agent\n"
        f"Exec={_desktop_exec_arg(FOCUS_SCRIPT_PATH)} %u\n"
        "Type=Application\n"
        "NoDisplay=true\n"
        f"MimeType={SCHEME_KEY};\n"
        f"X-KDE-Protocols={SCHEME}\n"
    )
    DESKTOP_DIR.mkdir(parents=True, exist_ok=True)
    DESKTOP_FILE.write_text(desktop_contents)
    _set_mimeapps_default(SCHEME_KEY, DESKTOP_FILE.name)
    try:
        subprocess.run(
            ["update-desktop-database", str(DESKTOP_DIR)],
            capture_output=True, timeout=5.0, check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return f"registered {SCHEME}:// → {DESKTOP_FILE.name}"


def remove_focus_url_handler() -> str:
    if DESKTOP_FILE.is_file():
        DESKTOP_FILE.unlink()
    _drop_mimeapps_key(SCHEME_KEY)
    return f"removed {DESKTOP_FILE.name} and unregistered {SCHEME}://"


def _write_mimeapps(text: str) -> None:
    """Replace the config atomically, following any user-owned symlink. A
    new file gets the mode any program creating it would: 0666 less the
    umask, so 0644 usually."""
    target = MIMEAPPS_FILE.resolve()
    try:
        mode = target.stat().st_mode & 0o7777
    except FileNotFoundError:
        umask = os.umask(0)
        os.umask(umask)
        mode = 0o666 & ~umask
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=target.parent,
            prefix=f".{target.name}.", suffix=".tmp", delete=False,
        ) as output:
            temporary = Path(output.name)
            os.fchmod(output.fileno(), mode)
            output.write(text)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _drop_mimeapps_key(scheme: str) -> bool:
    """Remove every mimeapps.list line for scheme; True when one went."""
    try:
        text = MIMEAPPS_FILE.read_text(encoding="utf-8")
    except OSError:
        return False
    lines = text.splitlines()
    kept = [ln for ln in lines if ln.split("=", 1)[0].strip() != scheme]
    if len(kept) == len(lines):
        return False
    _write_mimeapps("\n".join(kept) + "\n")
    return True


def remove_legacy_identity() -> str:
    """Earlier versions registered codexbar:// and installed codexbar-xhr env
    files. Remove them so a stale handler never answers old links."""
    removed: list[str] = []
    for f in (LEGACY_DESKTOP_FILE, *LEGACY_ENV_FILES):
        if f.is_file():
            try:
                f.unlink()
                removed.append(f.name)
            except OSError:
                pass
    if _drop_mimeapps_key(LEGACY_SCHEME_KEY):
        removed.append("codexbar://")
    return "removed legacy " + ", ".join(removed) if removed else ""


def _set_mimeapps_default(scheme: str, desktop_name: str) -> None:
    MIMEAPPS_FILE.parent.mkdir(parents=True, exist_ok=True)
    text = MIMEAPPS_FILE.read_text(encoding="utf-8") if MIMEAPPS_FILE.is_file() else ""
    out: list[str] = []
    in_section = False
    inserted = False
    has_section = False
    for ln in text.splitlines():
        stripped = ln.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            if in_section and not inserted:
                out.append(f"{scheme}={desktop_name};")
                inserted = True
            in_section = (stripped == "[Default Applications]")
            if in_section:
                has_section = True
            out.append(ln)
            continue
        if in_section and "=" in ln and ln.split("=", 1)[0].strip() == scheme:
            out.append(f"{scheme}={desktop_name};")
            inserted = True
            continue
        out.append(ln)
    if in_section and not inserted:
        out.append(f"{scheme}={desktop_name};")
        inserted = True
    if not has_section:
        if out and out[-1].strip():
            out.append("")
        out.append("[Default Applications]")
        out.append(f"{scheme}={desktop_name};")
    _write_mimeapps("\n".join(out) + "\n")


# ---------------------------------------------------------------------------
# systemctl helper (used for set-environment / unset-environment only)
# ---------------------------------------------------------------------------

def _systemctl(*args: str) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            ["systemctl", "--user", *args],
            capture_output=True, text=True, timeout=8.0, check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return 1, str(exc)
    out = (proc.stdout + proc.stderr).strip()
    return proc.returncode, out


# ---------------------------------------------------------------------------
# omp extension
# ---------------------------------------------------------------------------

def omp_agent_dir() -> Path:
    # omp itself reads PI_CODING_AGENT_DIR; codexbar_waiting.py checks the same order.
    agent_dir = os.environ.get("PI_CODING_AGENT_DIR") or os.environ.get("OMP_AGENT_DIR")
    return Path(agent_dir) if agent_dir else DEFAULT_OMP_AGENT_DIR


def _omp_extension_link() -> Path:
    return omp_agent_dir() / "extensions" / OMP_EXTENSION_NAME


def _omp_loader(source: Path) -> str:
    url = json.dumps(source.as_uri())
    return f'{OMP_LOADER_MARKER}export * from {url};\nexport {{ default }} from {url};\n'


def _omp_extension_target(link: Path) -> Path | None:
    """Where our link or loader file points, any checkout's (a moved repo's
    dangling one is still ours); None when the entry isn't ours."""
    if link.is_symlink():
        target = link.parent / os.readlink(link)
    elif link.is_file():
        try:
            text = link.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None
        loader = re.fullmatch(
            re.escape(OMP_LOADER_MARKER) + r'export \* from ("file:[^"\n]*");\nexport \{ default \} from \1;\n', text,
        )
        if loader is None:
            return None
        try:
            url = json.loads(loader[1])
        except ValueError:
            return None
        target = Path(urllib.request.url2pathname(urllib.parse.urlparse(url).path))
    else:
        return None
    return target if target.parts[-2:] == OMP_EXTENSION_SOURCE.parts[-2:] else None


def _omp_extension_destination(link: Path) -> Path | None:
    """The file our link or loader loads, resolved; None when it isn't ours.
    A symlink resolves through the link: Windows' readlink adds a \\\\?\\ prefix."""
    target = _omp_extension_target(link)
    return None if target is None else (link if link.is_symlink() else target).resolve()


def _omp_extension_current(link: Path) -> bool:
    return _omp_extension_destination(link) == OMP_EXTENSION_SOURCE


def install_omp_extension() -> str:
    if not OMP_EXTENSION_SOURCE.is_file():
        raise RuntimeError("not in this copy; use a complete checkout or applet package")
    link = _omp_extension_link()
    if (link.exists() or link.is_symlink()) and _omp_extension_target(link) is None:
        raise RuntimeError(f"{link} exists and isn't ours; left alone")
    if _omp_extension_current(link):
        return "omp extension: already current"
    # The name ends outside .ts, so a starting omp can't discover it half-made.
    temporary = link.with_name(f"{link.name}.{os.getpid()}.tmp")
    try:
        link.parent.mkdir(parents=True, exist_ok=True)
        temporary.unlink(missing_ok=True)
        if sys.platform == "win32":
            temporary.write_text(_omp_loader(OMP_EXTENSION_SOURCE), encoding="utf-8")
        else:
            temporary.symlink_to(OMP_EXTENSION_SOURCE)
        state_paths.replace(temporary, link)
    except OSError as exc:
        raise RuntimeError(f"can't link {link}: {exc}") from exc
    return f"omp extension: linked {link}"


def remove_omp_extension() -> str:
    link = _omp_extension_link()
    if not link.exists() and not link.is_symlink():
        return "omp extension: removed nothing"
    if _omp_extension_target(link) is None:
        return f"omp extension: left {link}, it isn't ours"
    try:
        link.unlink()
    except OSError as exc:
        return f"omp extension: can't remove {link}: {exc}"
    return f"omp extension: removed {link}"


def _omp_extension_status() -> str:
    link = _omp_extension_link()
    target = _omp_extension_target(link)
    if target is not None:
        if not target.exists():
            return "broken link"
        destination = _omp_extension_destination(link)
        current = destination == OMP_EXTENSION_SOURCE or not OMP_EXTENSION_SOURCE.is_file()
        return "yes" if current else f"other copy: {destination}"
    return "not ours" if link.exists() or link.is_symlink() else "no"


# ---------------------------------------------------------------------------
# Top-level
# ---------------------------------------------------------------------------

def _require_linux() -> None:
    if sys.platform != "linux":
        raise RuntimeError("Plasma integration is Linux-only; Tern needs no integration setup.")


def install_all() -> str:
    """Install: env scripts so plasmashell gets QML_XHR_ALLOW_FILE_READ on
    every launch, URL scheme handler for click-to-focus, and cleanup of the
    pre-rename codexbar:// handler and env files."""
    _require_linux()
    parts = [
        remove_legacy_identity(),
        install_env_script(),
        install_focus_url_handler(),
    ]
    # Seed the aggregate so the widget has something to read on first open.
    # An existing one is left alone: this sweep would trim History to the
    # default limit instead of the configured one.
    if not (state_paths.migrate() / "agents.json").exists():
        try:
            subprocess.run(
                [sys.executable or "python3", str(AGGREGATOR_PATH), "--once"],
                capture_output=True, timeout=10.0, check=False,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
    # Only where omp is set up: the Install button shouldn't create an ~/.omp tree.
    if _omp_extension_link().parent.parent.is_dir():
        try:
            parts.append(install_omp_extension())
        except RuntimeError as exc:
            parts.append(f"omp extension: {exc}")
    return "\n".join(p for p in parts if p)


def uninstall_all() -> str:
    _require_linux()
    parts = [
        remove_legacy_identity(),
        remove_focus_url_handler(),
        remove_env_script(),
        remove_omp_extension(),
    ]
    return "\n".join(p for p in parts if p)


def status_all() -> str:
    omp_extension = f"omp extension:               {_omp_extension_status()}"
    if sys.platform != "linux":
        return (
            "Plasma integration is Linux-only.\n"
            "Tern needs no Plasma integration; it calls the helpers directly.\n"
            f"{omp_extension}"
        )
    plasma_env = "yes" if PLASMA_ENV_FILE.is_file() else "no"
    systemd_env = "yes" if SYSTEMD_ENV_FILE.is_file() else "no"
    url_ok = "yes" if DESKTOP_FILE.is_file() else "no"
    return (
        f"plasma-workspace env script: {plasma_env}\n"
        f"systemd-user env file:       {systemd_env}\n"
        f"focus url handler:           {url_ok}\n"
        f"{omp_extension}"
    )


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--uninstall", action="store_true")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--omp-extension", action="store_true",
                        help="install or remove only the omp extension (Linux, macOS and Windows)")
    args = parser.parse_args(argv)
    if args.status:
        print(status_all())
        return 0
    try:
        if args.omp_extension:
            result = remove_omp_extension() if args.uninstall else install_omp_extension()
        else:
            result = uninstall_all() if args.uninstall else install_all()
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(result)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
