"""Windows process introspection through kernel32 and ntdll, mirroring the
/proc reads codexbar_agents.py makes on Linux and proc_darwin.py's functions.

A process's command line, folder and environment live in its PEB, which is
read the way psutil does: NtQueryInformationProcess finds it, and
ReadProcessMemory copies the RTL_USER_PROCESS_PARAMETERS it points to. Struct
offsets follow phnt's ntpebteb.h and ntrtl.h. Every read returns an empty
value when the process is gone, belongs to another user or runs elevated.

Windows keeps no list of open files a process can cheaply read, so
`open_paths` finds nothing; callers resolve sessions by other means."""

from __future__ import annotations

import ctypes
import struct
from ctypes import wintypes

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_ntdll = ctypes.WinDLL("ntdll")
_shell32 = ctypes.WinDLL("shell32", use_last_error=True)

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_PROCESS_VM_READ = 0x0010
_SYNCHRONIZE = 0x00100000
_ERROR_ACCESS_DENIED = 5
_ERROR_INVALID_PARAMETER = 87
_WAIT_TIMEOUT = 0x102
_ProcessBasicInformation = 0
_ProcessWow64Information = 26
# FILETIME counts 100 ns ticks since 1601; the Unix epoch is this many later.
_EPOCH_TICKS = 116444736000000000
# Cap on an environment block read in search of its terminator.
_ENV_MAX = 4 << 20
_PAGE = 4096


class _BasicInfo(ctypes.Structure):
    _fields_ = [
        ("ExitStatus", ctypes.c_long),
        ("PebBaseAddress", ctypes.c_void_p),
        ("AffinityMask", ctypes.c_size_t),
        ("BasePriority", ctypes.c_long),
        ("UniqueProcessId", ctypes.c_size_t),
        ("InheritedFromUniqueProcessId", ctypes.c_size_t),
    ]


class _BasicInfo64(ctypes.Structure):
    """PROCESS_BASIC_INFORMATION as a 64-bit process sees it, for the WOW64
    calls a 32-bit Python makes to read one."""
    _fields_ = [
        ("ExitStatus", ctypes.c_long),
        ("PebBaseAddress", ctypes.c_uint64),
        ("AffinityMask", ctypes.c_uint64),
        ("BasePriority", ctypes.c_long),
        ("UniqueProcessId", ctypes.c_uint64),
        ("InheritedFromUniqueProcessId", ctypes.c_uint64),
    ]


_kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
_kernel32.OpenProcess.restype = wintypes.HANDLE
_kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
_kernel32.ReadProcessMemory.argtypes = (
    wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t,
    ctypes.POINTER(ctypes.c_size_t),
)
_kernel32.QueryFullProcessImageNameW.argtypes = (
    wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD),
)
_kernel32.GetProcessTimes.argtypes = (wintypes.HANDLE,) + (ctypes.POINTER(wintypes.FILETIME),) * 4
_kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
_kernel32.WaitForSingleObject.restype = wintypes.DWORD
_kernel32.IsWow64Process.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL))
_kernel32.GetCurrentProcess.restype = wintypes.HANDLE
_kernel32.K32EnumProcesses.argtypes = (ctypes.POINTER(wintypes.DWORD), wintypes.DWORD, ctypes.POINTER(wintypes.DWORD))
_kernel32.LocalFree.argtypes = (ctypes.c_void_p,)
_ntdll.NtQueryInformationProcess.argtypes = (
    wintypes.HANDLE, ctypes.c_ulong, ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulong),
)
_ntdll.NtQueryInformationProcess.restype = ctypes.c_long
_shell32.CommandLineToArgvW.argtypes = (wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int))
_shell32.CommandLineToArgvW.restype = ctypes.POINTER(wintypes.LPWSTR)

_PYTHON_64 = struct.calcsize("P") == 8
# A 32-bit Python reaches 64-bit processes only through these WOW64 entry
# points, which exist in a WOW64 process's ntdll alone.
_wow64_query = getattr(_ntdll, "NtWow64QueryInformationProcess64", None)
_wow64_read = getattr(_ntdll, "NtWow64ReadVirtualMemory64", None)
if _wow64_query is not None and _wow64_read is not None:
    _wow64_query.argtypes = (wintypes.HANDLE, ctypes.c_ulong, ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulong))
    _wow64_query.restype = ctypes.c_long
    _wow64_read.argtypes = (wintypes.HANDLE, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_uint64, ctypes.POINTER(ctypes.c_uint64))
    _wow64_read.restype = ctypes.c_long


class _Handle:
    """An OpenProcess handle, closed on leaving the `with` block; falsy
    when the process could not be opened."""

    def __init__(self, pid: int, access: int):
        self.value = None
        self.error = _ERROR_INVALID_PARAMETER  # what OpenProcess says of no such pid
        if 0 < pid < 1 << 32:
            self.value = _kernel32.OpenProcess(access, False, pid)
            self.error = 0 if self.value else ctypes.get_last_error()

    def __bool__(self) -> bool:
        return bool(self.value)

    def __enter__(self) -> "_Handle":
        return self

    def __exit__(self, *_) -> None:
        if self.value:
            _kernel32.CloseHandle(self.value)
            self.value = None


def _basic_info(handle) -> _BasicInfo | None:
    info = _BasicInfo()
    if _ntdll.NtQueryInformationProcess(handle, _ProcessBasicInformation, ctypes.byref(info), ctypes.sizeof(info), None) != 0:
        return None
    return info


def _filetime_ms(ft: wintypes.FILETIME) -> int:
    ticks = (ft.dwHighDateTime << 32) | ft.dwLowDateTime
    return max(ticks - _EPOCH_TICKS, 0) // 10_000


def _created_ms(handle) -> int:
    times = [wintypes.FILETIME() for _ in range(4)]
    if not _kernel32.GetProcessTimes(handle, *map(ctypes.byref, times)):
        return 0
    return _filetime_ms(times[0])


def all_pids() -> list[int]:
    size = 1024
    while True:
        buf = (wintypes.DWORD * size)()
        used = wintypes.DWORD()
        if not _kernel32.K32EnumProcesses(buf, ctypes.sizeof(buf), ctypes.byref(used)):
            return []
        count = used.value // ctypes.sizeof(wintypes.DWORD)
        # A full buffer may have cut the list short.
        if count < size:
            return [pid for pid in buf[:count] if pid > 0]
        size *= 2


def ppid_of(pid: int) -> int:
    """The parent pid, or 0. Windows keeps the pid of a parent that has
    exited, which a newer process may reuse: a parent created after its
    child is not its parent."""
    with _Handle(pid, _PROCESS_QUERY_LIMITED_INFORMATION) as handle:
        info = _basic_info(handle.value) if handle else None
        if info is None:
            return 0
        parent = int(info.InheritedFromUniqueProcessId)
        child_start = _created_ms(handle.value)
    with _Handle(parent, _PROCESS_QUERY_LIMITED_INFORMATION) as handle:
        if handle and child_start and _created_ms(handle.value) > child_start:
            return 0
    return parent


def exe_of(pid: int) -> str:
    with _Handle(pid, _PROCESS_QUERY_LIMITED_INFORMATION) as handle:
        if not handle:
            return ""
        size = wintypes.DWORD(32768)
        buf = ctypes.create_unicode_buffer(size.value)
        if not _kernel32.QueryFullProcessImageNameW(handle.value, 0, buf, ctypes.byref(size)):
            return ""
        return buf.value


def comm_of(pid: int) -> str:
    """The executable's name without `.exe`, in lower case: Windows matches
    file names case-insensitively, so `Claude.exe` is `claude`."""
    name = exe_of(pid).rsplit("\\", 1)[-1].lower()
    return name.removesuffix(".exe")


def start_ms(pid: int) -> int:
    with _Handle(pid, _PROCESS_QUERY_LIMITED_INFORMATION) as handle:
        return _created_ms(handle.value) if handle else 0


def exited(pid: int) -> bool:
    """True when `pid` is gone. A process we may not query still runs."""
    with _Handle(pid, _SYNCHRONIZE) as handle:
        if not handle:
            return handle.error != _ERROR_ACCESS_DENIED
        return _kernel32.WaitForSingleObject(handle.value, 0) != _WAIT_TIMEOUT


def open_paths(pid: int) -> list[str]:
    """Nothing: listing another process's open files needs a system-wide
    handle scan, which Windows makes slow and unreliable."""
    return []


class _Memory:
    """Reads one process's PEB fields in its own pointer width."""

    def __init__(self, handle):
        self.handle = handle
        self.peb = 0
        self.pointer = 8
        self.read = self._read_native
        if _PYTHON_64:
            wow = ctypes.c_void_p()
            status = _ntdll.NtQueryInformationProcess(handle, _ProcessWow64Information, ctypes.byref(wow), ctypes.sizeof(wow), None)
            if status == 0 and wow.value:
                self.peb, self.pointer = wow.value, 4
            else:
                info = _basic_info(handle)
                self.peb = (info.PebBaseAddress or 0) if info else 0
            return
        self.pointer = 4
        if _target_is_64(handle):
            info = _BasicInfo64()
            if _wow64_query is None or _wow64_query(handle, _ProcessBasicInformation, ctypes.byref(info), ctypes.sizeof(info), None) != 0:
                return
            self.peb, self.pointer, self.read = info.PebBaseAddress, 8, self._read_wow64
        else:
            info = _basic_info(handle)
            self.peb = (info.PebBaseAddress or 0) if info else 0

    def _read_native(self, address: int, size: int) -> bytes:
        buf = ctypes.create_string_buffer(size)
        done = ctypes.c_size_t()
        if not _kernel32.ReadProcessMemory(self.handle, ctypes.c_void_p(address), buf, size, ctypes.byref(done)) or done.value != size:
            return b""
        return buf.raw

    def _read_wow64(self, address: int, size: int) -> bytes:
        buf = ctypes.create_string_buffer(size)
        done = ctypes.c_uint64()
        if _wow64_read(self.handle, address, buf, size, ctypes.byref(done)) != 0 or done.value != size:
            return b""
        return buf.raw

    def pointer_at(self, address: int) -> int:
        raw = self.read(address, self.pointer) if address else b""
        if len(raw) != self.pointer:
            return 0
        return struct.unpack("<Q" if self.pointer == 8 else "<I", raw)[0]

    def unicode_string(self, address: int) -> str:
        """A UNICODE_STRING: USHORT Length, USHORT MaximumLength, then the
        buffer pointer at the next pointer boundary."""
        raw = self.read(address, 4) if address else b""
        if len(raw) != 4:
            return ""
        length = struct.unpack("<H", raw[:2])[0]
        buffer = self.pointer_at(address + self.pointer)
        if not length or not buffer:
            return ""
        text = self.read(buffer, length)
        return text.decode("utf-16-le", "replace") if text else ""

    def parameters(self) -> int:
        """RTL_USER_PROCESS_PARAMETERS, at PEB+0x20 (64-bit) or +0x10."""
        return self.pointer_at(self.peb + (0x20 if self.pointer == 8 else 0x10)) if self.peb else 0


def _target_is_64(handle) -> bool:
    """For a 32-bit Python: True when the target is a native process of a
    64-bit Windows, where Python itself runs under WOW64."""
    mine, theirs = wintypes.BOOL(), wintypes.BOOL()
    if not _kernel32.IsWow64Process(_kernel32.GetCurrentProcess(), ctypes.byref(mine)):
        return False
    if not mine.value:
        return False  # 32-bit Windows: every process is 32-bit.
    return bool(_kernel32.IsWow64Process(handle, ctypes.byref(theirs))) and not theirs.value


# RTL_USER_PROCESS_PARAMETERS offsets by pointer width.
_CWD = {8: 0x38, 4: 0x24}
_COMMAND_LINE = {8: 0x70, 4: 0x40}
_ENVIRONMENT = {8: 0x80, 4: 0x48}


def _process_parameters(pid: int, field: str) -> str:
    """The folder, command line or environment block (NUL-separated) of
    `pid`, or "" when it cannot be read."""
    with _Handle(pid, _PROCESS_QUERY_LIMITED_INFORMATION | _PROCESS_VM_READ) as handle:
        if not handle:
            return ""
        memory = _Memory(handle.value)
        params = memory.parameters()
        if not params:
            return ""
        width = memory.pointer
        if field == "cwd":
            return memory.unicode_string(params + _CWD[width])
        if field == "cmdline":
            return memory.unicode_string(params + _COMMAND_LINE[width])
        block = memory.pointer_at(params + _ENVIRONMENT[width])
        return _environment_block(memory, block) if block else ""


def _environment_block(memory: _Memory, block: int) -> str:
    """The entries of the environment block at `block`, up to the empty one
    that ends it, read a page at a time: the block's size is not kept
    reliably, and a read past its last page fails."""
    raw = bytearray()
    address = block
    while address - block < _ENV_MAX:
        chunk = memory.read(address, _PAGE - address % _PAGE)
        if not chunk:
            return ""
        raw += chunk
        address += len(chunk)
        text = bytes(raw[:len(raw) - len(raw) % 2]).decode("utf-16-le", "replace")
        if "\0\0" in text:
            return text.split("\0\0", 1)[0]
    return ""


def cwd_of(pid: int) -> str:
    """The process's folder, without the trailing backslash Windows keeps
    on every one but a drive root such as `C:\\`."""
    cwd = _process_parameters(pid, "cwd")
    if len(cwd) > 3 and cwd.endswith("\\"):
        cwd = cwd[:-1]
    return cwd


def _split_command_line(line: str) -> list[str]:
    """Split a command line as CommandLineToArgvW does, which is how the
    C runtime splits it apart from a quoted program path's backslashes."""
    if not line.strip():
        return []
    count = ctypes.c_int()
    argv = _shell32.CommandLineToArgvW(line, ctypes.byref(count))
    if not argv:
        return []
    try:
        return [argv[i] for i in range(count.value)]
    finally:
        _kernel32.LocalFree(argv)


def argv_of(pid: int) -> list[str]:
    return _split_command_line(_process_parameters(pid, "cmdline"))


def environ_value(pid: int, name: str) -> str:
    """One variable from a process's environment, or "". Windows names are
    case-insensitive; entries such as `=C:=C:\\work` hold drive folders."""
    wanted = name.upper()
    for entry in _process_parameters(pid, "environ").split("\0"):
        key, sep, value = entry[1:].partition("=")
        if sep and (entry[:1] + key).upper() == wanted:
            return value
    return ""


def boot_id() -> str:
    """Windows counts boots in the registry; the count changes on every
    boot. Fast startup resumes the kernel instead, so it keeps the count."""
    import winreg

    try:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SYSTEM\CurrentControlSet\Control\Session Manager\Memory Management\PrefetchParameters",
        ) as key:
            value, kind = winreg.QueryValueEx(key, "BootId")
    except OSError:
        return ""
    return str(value) if kind == winreg.REG_DWORD else ""
