"""macOS process introspection through libproc and sysctl, mirroring the
/proc reads codexbar_agents.py makes on Linux. Struct offsets follow
<sys/proc_info.h>; every read returns an empty value when the process is
gone or belongs to another user."""

from __future__ import annotations

import ctypes
import ctypes.util
import struct

_libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)

_PROC_PIDLISTFDS = 1
_PROC_PIDTBSDINFO = 3
_PROC_PIDVNODEPATHINFO = 9
_PROC_PIDT_SHORTBSDINFO = 13
_PROC_PIDFDVNODEPATHINFO = 2
_PROX_FDTYPE_VNODE = 1
_SZOMB = 5

# struct proc_bsdinfo; only the owner may read it.
_BSDINFO_SIZE = 136
_BSDINFO = struct.Struct("=5I6II16s32s5IiQQ")
# struct proc_bsdshortinfo (pid, ppid, pgid, status, comm, ...), readable for
# every process, so walks pass root-owned ancestors such as login.
_SHORTINFO_SIZE = 64
_SHORTINFO = struct.Struct("=4I16s")
# struct vnode_info is 152 bytes; vnode_info_path appends a 1024-byte path.
_VNODE_INFO_SIZE = 152
_PATH_MAX = 1024
_VNODE_INFO_PATH_SIZE = _VNODE_INFO_SIZE + _PATH_MAX
# struct vnode_fdinfowithpath: a 24-byte proc_fileinfo, then vnode_info_path.
_FILEINFO_SIZE = 24

_CTL_KERN = 1
_KERN_ARGMAX = 8
_KERN_PROCARGS2 = 49


def _cstr(raw: bytes) -> str:
    return raw.split(b"\0", 1)[0].decode("utf-8", "replace")


def _bsdinfo(pid: int) -> tuple | None:
    buf = ctypes.create_string_buffer(_BSDINFO_SIZE)
    if _libc.proc_pidinfo(pid, _PROC_PIDTBSDINFO, 0, buf, _BSDINFO_SIZE) != _BSDINFO_SIZE:
        return None
    return _BSDINFO.unpack_from(buf.raw)


def _shortinfo(pid: int) -> tuple | None:
    buf = ctypes.create_string_buffer(_SHORTINFO_SIZE)
    if _libc.proc_pidinfo(pid, _PROC_PIDT_SHORTBSDINFO, 0, buf, _SHORTINFO_SIZE) != _SHORTINFO_SIZE:
        return None
    return _SHORTINFO.unpack_from(buf.raw)


def all_pids() -> list[int]:
    count = _libc.proc_listallpids(None, 0)
    if count <= 0:
        return []
    # Room for processes started between the two calls.
    count += 64
    buf = (ctypes.c_int * count)()
    n = _libc.proc_listallpids(buf, ctypes.sizeof(buf))
    return [pid for pid in buf[:max(n, 0)] if pid > 0]


def ppid_of(pid: int) -> int:
    info = _shortinfo(pid)
    return info[1] if info else 0


def comm_of(pid: int) -> str:
    """The name `ps` and `pgrep -x` show: argv[0]'s basename, else the
    executable's. The kernel name is the binary's, which for Claude Code
    is its version (`~/.local/share/claude/versions/2.1.288`)."""
    argv = argv_of(pid)
    name = (argv[0] if argv else exe_of(pid)).rsplit("/", 1)[-1].lstrip("-")
    if name:
        return name
    info = _shortinfo(pid)
    return _cstr(info[4]) if info else ""


def start_ms(pid: int) -> int:
    info = _bsdinfo(pid)
    if not info:
        return 0
    return info[20] * 1000 + info[21] // 1000


def exited(pid: int) -> bool:
    info = _shortinfo(pid)
    return info is None or info[3] == _SZOMB


def exe_of(pid: int) -> str:
    buf = ctypes.create_string_buffer(4096)
    if _libc.proc_pidpath(pid, buf, len(buf)) <= 0:
        return ""
    return _cstr(buf.raw)


def cwd_of(pid: int) -> str:
    size = 2 * _VNODE_INFO_PATH_SIZE
    buf = ctypes.create_string_buffer(size)
    if _libc.proc_pidinfo(pid, _PROC_PIDVNODEPATHINFO, 0, buf, size) != size:
        return ""
    return _cstr(buf.raw[_VNODE_INFO_SIZE:_VNODE_INFO_PATH_SIZE])


def open_paths(pid: int) -> list[str]:
    """Paths of the files `pid` holds open, like /proc/<pid>/fd targets."""
    size = _libc.proc_pidinfo(pid, _PROC_PIDLISTFDS, 0, None, 0)
    if size <= 0:
        return []
    buf = ctypes.create_string_buffer(size)
    size = _libc.proc_pidinfo(pid, _PROC_PIDLISTFDS, 0, buf, size)
    if size <= 0:
        return []
    out = []
    info_size = _FILEINFO_SIZE + _VNODE_INFO_PATH_SIZE
    info = ctypes.create_string_buffer(info_size)
    for fd, fdtype in struct.iter_unpack("=iI", buf.raw[:size - size % 8]):
        if fdtype != _PROX_FDTYPE_VNODE:
            continue
        if _libc.proc_pidfdinfo(pid, fd, _PROC_PIDFDVNODEPATHINFO, info, info_size) != info_size:
            continue
        path = _cstr(info.raw[_FILEINFO_SIZE + _VNODE_INFO_SIZE:info_size])
        if path:
            out.append(path)
    return out


def _sysctl(mib: list[int], size: int) -> bytes:
    arr = (ctypes.c_int * len(mib))(*mib)
    buf = ctypes.create_string_buffer(size)
    length = ctypes.c_size_t(size)
    if _libc.sysctl(arr, len(mib), buf, ctypes.byref(length), None, 0) != 0:
        return b""
    return buf.raw[:length.value]


def _procargs(pid: int) -> tuple[list[str], list[bytes]]:
    """(argv, environment entries) from KERN_PROCARGS2: argc, the exec
    path, NUL padding, argc arguments, then the environment."""
    argmax = struct.unpack("=i", _sysctl([_CTL_KERN, _KERN_ARGMAX], 4) or b"\0\0\0\0")[0]
    raw = _sysctl([_CTL_KERN, _KERN_PROCARGS2, pid], argmax or 1 << 20)
    if len(raw) < 4:
        return [], []
    argc = struct.unpack_from("=i", raw)[0]
    pos = raw.find(b"\0", 4)
    if pos < 0:
        return [], []
    while pos < len(raw) and raw[pos] == 0:
        pos += 1
    parts = raw[pos:].split(b"\0")
    argv = [a.decode("utf-8", "replace") for a in parts[:argc]]
    env = []
    for entry in parts[argc:]:
        if not entry:
            break
        env.append(entry)
    return argv, env


def argv_of(pid: int) -> list[str]:
    return _procargs(pid)[0]


def environ_value(pid: int, name: str) -> str:
    prefix = name.encode() + b"="
    for entry in _procargs(pid)[1]:
        if entry.startswith(prefix):
            return entry[len(prefix):].decode("utf-8", "replace")
    return ""


def boot_id() -> str:
    """kern.bootsessionuuid, which changes on every boot."""
    size = ctypes.c_size_t(64)
    buf = ctypes.create_string_buffer(64)
    if _libc.sysctlbyname(b"kern.bootsessionuuid", buf, ctypes.byref(size), None, 0) != 0:
        return ""
    return _cstr(buf.raw[:size.value])
