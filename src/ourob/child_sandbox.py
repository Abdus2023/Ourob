"""Filesystem write confinement for subprocesses.

On Linux with Landlock available, child processes may create or modify file
contents and namespace entries only inside unprotected repository directories
and a per-process temporary tree. The repository's protected paths, ``.ourob``
state, and ``.git`` metadata receive no such write rules. The restriction is
applied in the child before ``exec`` and is inherited by its descendants. A
narrow seccomp filter also rejects selected ownership and xattr mutation
syscalls. File-mode and timestamp changes are not
confined by that filter.

This is deliberately *not* a general OS sandbox: it does not restrict reads,
network access, or process inspection. On platforms without the required Linux
Landlock and seccomp support, code-executing child processes fail closed.
"""

from __future__ import annotations

import contextlib
import ctypes
import os
import platform
import shutil
import signal
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

# Linux assigns the Landlock syscall numbers below on x86_64 and aarch64.
_SYSCALL_ARCHES = {"x86_64", "amd64", "aarch64", "arm64"}
_METADATA_SYSCALLS = {
    "x86_64": {
        92,
        93,
        94,
        260,  # ownership changes
        188,
        189,
        190,
        197,
        198,
        199,  # xattr changes
    },
    "aarch64": {
        5,
        6,
        7,
        14,
        15,
        16,  # xattr changes
        54,
        55,  # ownership changes
    },
}
_SYS_LANDLOCK_CREATE_RULESET = 444
_SYS_LANDLOCK_ADD_RULE = 445
_SYS_LANDLOCK_RESTRICT_SELF = 446
_LANDLOCK_CREATE_RULESET_VERSION = 1
_LANDLOCK_RULE_PATH_BENEATH = 1
_PR_SET_NO_NEW_PRIVS = 38

# Filesystem write rights. REFER is handled but intentionally not granted: a
# child cannot create a hard link to a protected file or move files across
# directories to smuggle a protected inode into a writable subtree.
_LANDLOCK_ACCESS_FS_WRITE_FILE = 1 << 1
_LANDLOCK_ACCESS_FS_REMOVE_DIR = 1 << 4
_LANDLOCK_ACCESS_FS_REMOVE_FILE = 1 << 5
_LANDLOCK_ACCESS_FS_MAKE_CHAR = 1 << 6
_LANDLOCK_ACCESS_FS_MAKE_DIR = 1 << 7
_LANDLOCK_ACCESS_FS_MAKE_REG = 1 << 8
_LANDLOCK_ACCESS_FS_MAKE_SOCK = 1 << 9
_LANDLOCK_ACCESS_FS_MAKE_FIFO = 1 << 10
_LANDLOCK_ACCESS_FS_MAKE_BLOCK = 1 << 11
_LANDLOCK_ACCESS_FS_MAKE_SYM = 1 << 12
_LANDLOCK_ACCESS_FS_REFER = 1 << 13  # ABI >= 2
_LANDLOCK_ACCESS_FS_TRUNCATE = 1 << 14  # ABI >= 3
_LANDLOCK_ACCESS_FS_IOCTL_DEV = 1 << 15  # ABI >= 5


@dataclass(frozen=True)
class LandlockStatus:
    available: bool
    abi: int = 0
    reason: str = ""


def _libc() -> ctypes.CDLL:
    library = ctypes.CDLL(None, use_errno=True)
    library.syscall.restype = ctypes.c_long
    library.prctl.restype = ctypes.c_int
    return library


@lru_cache(maxsize=1)
def landlock_status() -> LandlockStatus:
    """Return whether this host can apply the required filesystem boundary."""
    if not sys.platform.startswith("linux"):
        return LandlockStatus(False, reason="Landlock is Linux-only")
    if platform.machine().lower() not in _SYSCALL_ARCHES:
        return LandlockStatus(False, reason=f"unsupported Linux architecture {platform.machine()!r}")
    try:
        abi = int(
            _libc().syscall(
                _SYS_LANDLOCK_CREATE_RULESET,
                ctypes.c_void_p(),
                0,
                _LANDLOCK_CREATE_RULESET_VERSION,
            )
        )
    except (AttributeError, OSError) as exc:
        return LandlockStatus(False, reason=f"Landlock syscall unavailable: {exc}")
    if abi < 1:
        error = ctypes.get_errno()
        detail = os.strerror(error) if error else "kernel returned no Landlock ABI"
        return LandlockStatus(False, reason=f"Landlock unavailable: {detail}")
    return LandlockStatus(True, abi=abi)


def _write_rights(abi: int) -> int:
    rights = (
        _LANDLOCK_ACCESS_FS_WRITE_FILE
        | _LANDLOCK_ACCESS_FS_REMOVE_DIR
        | _LANDLOCK_ACCESS_FS_REMOVE_FILE
        | _LANDLOCK_ACCESS_FS_MAKE_CHAR
        | _LANDLOCK_ACCESS_FS_MAKE_DIR
        | _LANDLOCK_ACCESS_FS_MAKE_REG
        | _LANDLOCK_ACCESS_FS_MAKE_SOCK
        | _LANDLOCK_ACCESS_FS_MAKE_FIFO
        | _LANDLOCK_ACCESS_FS_MAKE_BLOCK
        | _LANDLOCK_ACCESS_FS_MAKE_SYM
    )
    if abi >= 2:
        rights |= _LANDLOCK_ACCESS_FS_REFER
    if abi >= 3:
        rights |= _LANDLOCK_ACCESS_FS_TRUNCATE
    if abi >= 5:
        rights |= _LANDLOCK_ACCESS_FS_IOCTL_DEV
    return rights


class FilesystemSandboxUnavailable(RuntimeError):
    """Raised when the child write boundary cannot be prepared."""


def _kill_group(proc: subprocess.Popen[str]) -> None:
    """Kill a child's entire process group, tolerating an already-exited group."""
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        os.killpg(proc.pid, signal.SIGKILL)


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _protected_roots(repo: Path, patterns: list[str]) -> list[Path]:
    # Resolve these roots before constructing allow-rules. Otherwise a top-level
    # `.ourob` or `.git` symlink into an otherwise writable directory (for
    # example `docs/`) would inherit that target directory's write permission.
    roots: list[Path] = [(repo / ".ourob").resolve(strict=False), (repo / ".git").resolve(strict=False)]
    for raw in patterns:
        pattern = str(raw).replace("\\", "/").strip()
        if not pattern or pattern.startswith("/") or ".." in pattern.split("/"):
            raise FilesystemSandboxUnavailable(f"invalid protected path pattern {raw!r}")
        target = (repo / pattern.rstrip("/")).resolve(strict=False)
        if not _is_within(target, repo):
            raise FilesystemSandboxUnavailable(f"protected path resolves outside repository: {raw!r}")
        roots.append(target)
    # A parent rule is enough to exclude all of its descendants.
    unique: list[Path] = []
    for root in sorted(set(roots), key=lambda item: (len(item.parts), item.as_posix())):
        if not any(_is_within(root, parent) for parent in unique):
            unique.append(root)
    return unique


def _writable_repository_directories(repo: Path, protected: list[str]) -> list[Path]:
    """Find existing real directories that neither contain nor enter protection."""
    root = Path(repo).resolve(strict=True)
    if not root.is_dir():
        raise FilesystemSandboxUnavailable(f"repository is not a directory: {root}")
    blocked = _protected_roots(root, protected)
    writable: list[Path] = []
    for current, dirs, _files in os.walk(root, topdown=True, followlinks=False):
        directory = Path(current).resolve(strict=True)
        if any(_is_within(directory, item) for item in blocked):
            dirs[:] = []
            continue
        contains_protected = any(_is_within(item, directory) for item in blocked)
        if not contains_protected:
            writable.append(directory)

        kept: list[str] = []
        for name in dirs:
            child = Path(current) / name
            if child.is_symlink():
                continue
            resolved = child.resolve(strict=False)
            if not _is_within(resolved, root):
                continue
            if any(_is_within(resolved, item) for item in blocked):
                continue
            kept.append(name)
        dirs[:] = kept
    return sorted(set(writable), key=lambda item: item.as_posix())


class ChildFilesystemSandbox:
    """Prepare and apply one child's restricted filesystem write domain."""

    def __init__(self, repo: Path, protected: list[str]) -> None:
        self.repo = Path(repo).resolve()
        self.protected = list(protected)
        self.status = landlock_status()
        self.temp_root: Path | None = None
        self.writable_roots: list[Path] = []

    def __enter__(self) -> ChildFilesystemSandbox:
        if not self.status.available:
            raise FilesystemSandboxUnavailable(self.status.reason)
        try:
            self.temp_root = Path(tempfile.mkdtemp(prefix="ourob-child-")).resolve(strict=True)
            for name in ("tmp", "home", "cache", "config", "data", "ruff-cache", "mypy-cache", "pycache"):
                (self.temp_root / name).mkdir()
            self.writable_roots = _writable_repository_directories(self.repo, self.protected)
            self.writable_roots.append(self.temp_root)
            # Pytest and other standard tools open os.devnull as a log sink.
            # Grant this single device node, not its containing directory.
            null_device = Path("/dev/null")
            if null_device.exists():
                self.writable_roots.append(null_device.resolve())
        except (OSError, RuntimeError) as exc:
            self.__exit__(None, None, None)
            raise FilesystemSandboxUnavailable(f"could not prepare child filesystem boundary: {exc}") from exc
        return self

    def environment(self, base: dict[str, str]) -> dict[str, str]:
        """Return a stripped child environment with all tool caches redirected."""
        if self.temp_root is None:
            raise FilesystemSandboxUnavailable("child filesystem boundary is not active")
        env = dict(base)
        temp = self.temp_root
        env.update(
            {
                "HOME": str(temp / "home"),
                "TMPDIR": str(temp / "tmp"),
                "TMP": str(temp / "tmp"),
                "TEMP": str(temp / "tmp"),
                "XDG_CACHE_HOME": str(temp / "cache"),
                "XDG_CONFIG_HOME": str(temp / "config"),
                "XDG_DATA_HOME": str(temp / "data"),
                "RUFF_CACHE_DIR": str(temp / "ruff-cache"),
                "MYPY_CACHE_DIR": str(temp / "mypy-cache"),
                "PYTHONPYCACHEPREFIX": str(temp / "pycache"),
                "PYTHONDONTWRITEBYTECODE": "1",
                "GIT_OPTIONAL_LOCKS": "0",
            }
        )
        return env

    def preexec_fn(self, limits: Callable[[], None] | None = None) -> Callable[[], None]:
        if self.temp_root is None:
            raise FilesystemSandboxUnavailable("child filesystem boundary is not active")
        roots = tuple(self.writable_roots)
        abi = self.status.abi
        rights = _write_rights(abi)
        allowed = rights & ~_LANDLOCK_ACCESS_FS_REFER

        def apply() -> None:
            if limits is not None:
                limits()
            _restrict_child(roots, rights, allowed)

        return apply

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if self.temp_root is not None:
            shutil.rmtree(self.temp_root, ignore_errors=True)
            self.temp_root = None


def _install_metadata_filter() -> None:
    """Deny selected ownership and xattr mutation syscalls in this process."""

    class SockFilter(ctypes.Structure):
        _fields_ = [
            ("code", ctypes.c_ushort),
            ("jt", ctypes.c_ubyte),
            ("jf", ctypes.c_ubyte),
            ("k", ctypes.c_uint),
        ]

    class SockFprog(ctypes.Structure):
        _fields_ = [("length", ctypes.c_ushort), ("filter", ctypes.POINTER(SockFilter))]

    machine = platform.machine().lower()
    if machine in {"amd64", "x86_64"}:
        key = "x86_64"
    elif machine in {"arm64", "aarch64"}:
        key = "aarch64"
    else:  # guarded by landlock_status; keep the child fail-closed if called alone
        raise OSError(f"no seccomp metadata syscall map for {machine!r}")

    # BPF: load seccomp_data.nr, return EPERM for any metadata mutation above,
    # and ALLOW everything else. Landlock handles content/create/remove/rename.
    program: list[tuple[int, int, int, int]] = [(0x20, 0, 0, 0)]
    for number in sorted(_METADATA_SYSCALLS[key]):
        program.append((0x15, 0, 1, number))
        program.append((0x06, 0, 0, 0x00050000 | 1))  # SECCOMP_RET_ERRNO | EPERM
    program.append((0x06, 0, 0, 0x7FFF0000))  # SECCOMP_RET_ALLOW
    entries = (SockFilter * len(program))(*(SockFilter(*item) for item in program))
    descriptor = SockFprog(len(program), entries)
    result = _libc().prctl(22, 2, ctypes.byref(descriptor), 0, 0)  # PR_SET_SECCOMP/FILTER
    if result < 0:
        error = ctypes.get_errno()
        raise OSError(error, f"prctl(PR_SET_SECCOMP): {os.strerror(error)}")


def _restrict_child(roots: tuple[Path, ...], handled: int, allowed: int) -> None:
    """Install the Landlock ruleset in the forked child before it execs."""

    class RulesetAttr(ctypes.Structure):
        _fields_ = [("handled_access_fs", ctypes.c_uint64)]

    class PathBeneathAttr(ctypes.Structure):
        _fields_ = [
            ("allowed_access", ctypes.c_uint64),
            ("parent_fd", ctypes.c_int),
            ("reserved", ctypes.c_uint32),
        ]

    libc = _libc()
    attr = RulesetAttr(handled)
    ruleset_fd = int(libc.syscall(_SYS_LANDLOCK_CREATE_RULESET, ctypes.byref(attr), ctypes.sizeof(attr), 0))
    if ruleset_fd < 0:
        error = ctypes.get_errno()
        raise OSError(error, f"landlock_create_ruleset: {os.strerror(error)}")

    try:
        for root in roots:
            path_fd = os.open(root, os.O_PATH | os.O_CLOEXEC)
            try:
                per_root_allowed = allowed
                if not root.is_dir():
                    per_root_allowed = _LANDLOCK_ACCESS_FS_WRITE_FILE | (
                        allowed & _LANDLOCK_ACCESS_FS_IOCTL_DEV
                    )
                beneath = PathBeneathAttr(per_root_allowed, path_fd, 0)
                result = libc.syscall(
                    _SYS_LANDLOCK_ADD_RULE,
                    ruleset_fd,
                    _LANDLOCK_RULE_PATH_BENEATH,
                    ctypes.byref(beneath),
                    0,
                )
                if result < 0:
                    error = ctypes.get_errno()
                    raise OSError(error, f"landlock_add_rule({root}): {os.strerror(error)}")
            finally:
                os.close(path_fd)
        if libc.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) < 0:
            error = ctypes.get_errno()
            raise OSError(error, f"prctl(PR_SET_NO_NEW_PRIVS): {os.strerror(error)}")
        result = libc.syscall(_SYS_LANDLOCK_RESTRICT_SELF, ruleset_fd, 0)
        if result < 0:
            error = ctypes.get_errno()
            raise OSError(error, f"landlock_restrict_self: {os.strerror(error)}")
        _install_metadata_filter()
    finally:
        os.close(ruleset_fd)


def run_child(
    argv: list[str],
    *,
    repo: Path,
    protected: list[str],
    env: dict[str, str],
    timeout: int,
    stdin: str = "",
    limits: Callable[[], None] | None = None,
) -> tuple[int, str, str]:
    """Run one command with filesystem write rules installed before exec."""
    try:
        with ChildFilesystemSandbox(repo, protected) as sandbox:
            child_env = sandbox.environment(env)
            try:
                proc = subprocess.Popen(
                    argv,
                    cwd=repo,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    env=child_env,
                    preexec_fn=sandbox.preexec_fn(limits),
                    start_new_session=True,
                    close_fds=True,
                )
            except FileNotFoundError:
                return 127, "", f"executable not found: {argv[0]!r}"
            except (OSError, subprocess.SubprocessError) as exc:
                return 126, "", f"could not start {argv[0]!r} under filesystem boundary: {exc}"
            try:
                out, err = proc.communicate(input=stdin, timeout=timeout)
            except subprocess.TimeoutExpired:
                _kill_group(proc)
                try:
                    out, err = proc.communicate(timeout=10)
                except subprocess.TimeoutExpired:
                    out, err = "", ""
                return 124, out, f"timed out after {timeout}s"
            return proc.returncode, out, err
    except FilesystemSandboxUnavailable as exc:
        return 126, "", f"child execution refused: {exc}"
    except OSError as exc:
        return 126, "", f"child filesystem boundary failed: {exc}"
