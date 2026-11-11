"""Local Git snapshots and ownership checks for agent patches."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
import tempfile
from collections.abc import Collection
from pathlib import Path, PurePosixPath


_GIT_OPTIONS = (
    "--no-optional-locks",
    "-c", f"core.hooksPath={os.devnull}",
    "-c", "core.fsmonitor=false",
    "-c", "core.protectHFS=true",
    "-c", "core.protectNTFS=true",
    "-c", "gc.auto=0",
)
_APPLE_GIT_SHIM = Path("/usr/bin/git")
_APPLE_CLT_GIT = Path("/Library/Developer/CommandLineTools/usr/bin/git")
_PACKAGE_MANAGER_GIT = (Path("/opt/homebrew/bin/git"), Path("/usr/local/bin/git"), Path("/opt/local/bin/git"))
_MACH_O_MAGICS = {
    b"\xfe\xed\xfa\xce", b"\xce\xfa\xed\xfe", b"\xfe\xed\xfa\xcf", b"\xcf\xfa\xed\xfe",
    b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca", b"\xca\xfe\xba\xbf", b"\xbf\xba\xfe\xca",
}


def _resolved_executable(path: str | Path) -> Path | None:
    try:
        executable = Path(path).resolve(strict=True)
        if executable.is_file() and os.access(executable, os.X_OK):
            return executable
    except (OSError, RuntimeError):
        pass
    return None


def _is_apple_shim(executable: Path) -> bool:
    if executable == _APPLE_GIT_SHIM:
        return True
    try:
        return executable.samefile(_APPLE_GIT_SHIM)
    except OSError:
        return False


def _native_git(candidate: Path) -> Path | None:
    executable = _resolved_executable(candidate)
    if executable is None or _is_apple_shim(executable):
        return None
    # Inspect the file without starting it. A PATH wrapper may call xcrun or Apple's Git shim.
    try:
        with executable.open("rb") as file:
            if file.read(4) in _MACH_O_MAGICS:
                return executable
    except OSError:
        pass
    return None


def resolve_git(executable: str | None = None) -> Path:
    """Choose an absolute Git path without starting Git or the macOS developer tools launcher."""
    if executable is not None:
        if not isinstance(executable, str) or "\x00" in executable or not Path(executable).is_absolute():
            raise RuntimeError("git_executable must be an absolute path to a Git executable.")
        resolved = _native_git(Path(executable)) if sys.platform == "darwin" else _resolved_executable(executable)
        if resolved is None:
            raise RuntimeError(f"Git executable is unavailable or is a launcher: {executable!r}")
        return resolved

    if sys.platform != "darwin":
        located = shutil.which("git")
        resolved = _resolved_executable(located) if located else None
        if resolved is None:
            raise RuntimeError("Git executable is unavailable in PATH.")
        return resolved

    clt_git = _native_git(_APPLE_CLT_GIT)
    candidates = (Path(directory) / "git" for directory in os.get_exec_path())
    for candidate in (*candidates, *_PACKAGE_MANAGER_GIT):
        resolved = _native_git(candidate)
        if resolved is not None and resolved != clt_git and ".app/Contents/Developer/" not in resolved.as_posix():
            return resolved
    if clt_git is not None:
        return clt_git
    raise RuntimeError("No standalone Git executable found. Install Git or configure its absolute path.")


def _git(
    git: Path,
    cwd: Path,
    *args: str,
    data: bytes | None = None,
    index: Path | None = None,
    fixed_worktree: bool = False,
    allowed_codes: tuple[int, ...] = (0,),
) -> bytes:
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(
        GIT_ALLOW_PROTOCOL="file",
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_NO_LAZY_FETCH="1",
        GIT_TERMINAL_PROMPT="0",
        LC_ALL="C",
        PATH=os.pathsep.join((str(git.parent), env.get("PATH", os.defpath))),
    )
    if index is not None:
        env["GIT_INDEX_FILE"] = str(index)
    options = _GIT_OPTIONS
    if fixed_worktree:
        git_dir = str(cwd / ".git")
        env.update(GIT_DIR=git_dir, GIT_COMMON_DIR=git_dir, GIT_WORK_TREE=str(cwd))
        options += (
            "-c", f"core.attributesFile={os.devnull}",
            "-c", f"core.excludesFile={os.devnull}",
            "-c", "core.sparseCheckout=false",
            "-c", "core.fileMode=true",
        )
    try:
        result = subprocess.run(
            [str(git), *options, *args],
            cwd=cwd,
            env=env,
            input=data,
            stdin=subprocess.DEVNULL if data is None else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except OSError as exc:
        raise RuntimeError(f"Could not run Git: {exc}") from exc
    if result.returncode not in allowed_codes:
        error = result.stderr.decode(errors="replace").strip()
        raise RuntimeError(f"git {args[0]}: {error or f'exit {result.returncode}'}")
    return result.stdout


def _reject_filters(git: Path, checkout: Path, *, fixed_worktree: bool = False) -> None:
    # Inspect config before commands that might invoke repository-configured clean filters.
    if _git(
        git, checkout, "config", "--name-only", "--get-regexp", r"^filter\.",
        allowed_codes=(0, 1), fixed_worktree=fixed_worktree,
    ):
        raise RuntimeError("The Git checkout defines filters. Use a clean clone without Git filters.")


class Workspace:
    """Pin the committed state of a clean source without modifying its checkout."""

    def __init__(self, source: Path, *, git: Path | None = None) -> None:
        self.git = resolve_git(str(git) if git is not None else None)
        root = _git(self.git, source, "rev-parse", "--show-toplevel").removesuffix(b"\n")
        self.root = Path(os.fsdecode(root))
        self.revision = _git(self.git, self.root, "rev-parse", "--verify", "HEAD^{commit}").decode().strip()
        self._object_format = _git(self.git, self.root, "rev-parse", "--show-object-format").decode().strip()
        _reject_filters(self.git, self.root)
        if _git(
            self.git, self.root, "status", "--porcelain=v1", "-z", "--untracked-files=all", "--ignore-submodules=dirty",
        ):
            raise RuntimeError("The source Git checkout has local changes. Commit or stash them before running.")
        if _git(self.git, self.root, "rev-parse", "--verify", "HEAD^{commit}").decode().strip() != self.revision:
            raise RuntimeError("HEAD moved while checking the source checkout. Start the run again.")

    def clone(self, destination: Path) -> Path:
        """Make a private checkout of the pinned commit using only the local Git repository."""
        try:
            destination.mkdir()
        except OSError as exc:
            raise RuntimeError(f"Could not create Git checkout at {destination}: {exc}") from exc
        _git(self.git, destination, "init", "--quiet", "--template=", f"--object-format={self._object_format}")
        _git(
            self.git, destination, "fetch", "--quiet", "--depth=1", "--no-tags", "--no-recurse-submodules",
            "--no-write-fetch-head", "--no-auto-maintenance", "--", str(self.root), self.revision,
        )
        # Materialize committed symlinks as text so agents cannot follow them outside this checkout.
        _git(self.git, destination, "config", "--local", "core.symlinks", "false")
        _git(self.git, destination, "checkout", "--quiet", "--detach", self.revision)
        return destination


def normalize_path(path: str) -> str:
    """Require one exact, portable Git path relative to the checkout root."""
    if not isinstance(path, str):
        raise RuntimeError("Repository paths must be strings.")
    candidate = PurePosixPath(path)
    if (
        not candidate.parts
        or candidate.is_absolute()
        or str(candidate) != path
        or any(part.lower() in {"..", ".git"} for part in candidate.parts)
        or any(character in "\\:" or ord(character) < 32 or ord(character) == 127 for character in path)
    ):
        raise RuntimeError(f"Unsafe repository path: {path!r}")
    return path


def _checkout_files(checkout: Path) -> set[str]:
    def raise_walk_error(error: OSError) -> None:
        raise error

    files: set[str] = set()
    metadata = checkout / ".git"
    try:
        directories = (checkout, metadata, metadata / "objects")
        if any(not stat.S_ISDIR(path.lstat().st_mode) for path in directories) or not stat.S_ISREG(
            (metadata / "config").lstat().st_mode,
        ):
            raise RuntimeError("The Developer checkout must retain its original Git directory.")
        for directory, subdirs, names in os.walk(checkout, onerror=raise_walk_error):
            root = Path(directory)
            if root == checkout:
                subdirs[:] = [name for name in subdirs if name != ".git"]
            for name in [*subdirs, *names]:
                path = root / name
                mode = path.lstat().st_mode
                if name.lower() == ".git":
                    raise RuntimeError(f"Nested Git metadata is unsupported: {path.relative_to(checkout)}")
                if stat.S_ISREG(mode):
                    files.add(path.relative_to(checkout).as_posix())
                elif not stat.S_ISDIR(mode):
                    raise RuntimeError(f"Developer created a non-regular file: {path.relative_to(checkout)}")
    except OSError as exc:
        raise RuntimeError(f"Could not read Developer checkout: {exc}") from exc
    return files


def _verify_delta(
    git: Path, checkout: Path, base: str, index: Path, allowed_paths: Collection[str], *, fixed_worktree: bool = False,
) -> bool:
    owned = {normalize_path(path) for path in allowed_paths}
    delta = _git(
        git, checkout, "diff", "--cached", "--raw", "--no-renames", "--no-ext-diff", "--ignore-submodules=none", "-z",
        base, "--", index=index, fixed_worktree=fixed_worktree,
    )
    if not delta:
        return False
    entries = iter(delta[:-1].split(b"\0"))
    ordinary_modes = {b"000000", b"100644", b"100755"}
    for metadata, name in zip(entries, entries, strict=True):
        path = normalize_path(os.fsdecode(name))
        if path not in owned:
            raise RuntimeError(f"Change is outside the Developer's assignment: {path!r}")
        old_mode, new_mode = metadata[1:].split()[:2]
        if old_mode not in ordinary_modes or new_mode not in ordinary_modes:
            raise RuntimeError(f"Change affects a non-regular file: {path!r}")
    return True


def collect_patch(
    checkout: Path, allowed_paths: Collection[str], *, base: str = "HEAD", git: Path | None = None,
) -> bytes:
    """Capture a Developer's final working files against the pinned base in a private index."""
    git = resolve_git(str(git) if git is not None else None)
    checkout = checkout.absolute()
    files = _checkout_files(checkout)
    _reject_filters(git, checkout, fixed_worktree=True)
    with tempfile.TemporaryDirectory(prefix=".vb-index-", dir=checkout.parent) as directory:
        index = Path(directory) / "index"
        _git(git, checkout, "read-tree", base, index=index, fixed_worktree=True)
        _git(git, checkout, "add", "--all", "--force", "--", ".", index=index, fixed_worktree=True)
        tracked = _git(git, checkout, "ls-files", "--cached", "-z", index=index, fixed_worktree=True)
        uncaptured = files - {os.fsdecode(name) for name in tracked.split(b"\0") if name}
        if uncaptured:
            raise RuntimeError(f"Could not capture Developer file: {min(uncaptured)!r}")
        if not _verify_delta(git, checkout, base, index, allowed_paths, fixed_worktree=True):
            return b""
        return _index_diff(git, checkout, base, index=index, fixed_worktree=True)


def apply_patch(
    checkout: Path, patch: bytes | str, allowed_paths: Collection[str], *, git: Path | None = None,
) -> None:
    """Apply a patch after checking the actual index delta against the task's paths."""
    git = resolve_git(str(git) if git is not None else None)
    try:
        data = patch.encode() if isinstance(patch, str) else patch
    except UnicodeEncodeError as exc:
        raise RuntimeError("The patch must be UTF-8 text.") from exc
    previous_tree = _git(git, checkout, "write-tree").decode().strip()
    with tempfile.TemporaryDirectory(prefix=".vb-index-", dir=checkout.parent) as directory:
        index = Path(directory) / "index"
        _git(git, checkout, "read-tree", previous_tree, index=index)
        _git(git, checkout, "apply", "--cached", "--check", "-", data=data, index=index)
        _git(git, checkout, "apply", "--cached", "-", data=data, index=index)
        if not _verify_delta(git, checkout, previous_tree, index, allowed_paths):
            raise RuntimeError("The patch has no file changes.")
        verified_patch = _index_diff(git, checkout, previous_tree, index=index)
    _git(git, checkout, "apply", "--index", "--check", "-", data=verified_patch)
    _git(git, checkout, "apply", "--index", "-", data=verified_patch)


def integrated_diff(checkout: Path, *, git: Path | None = None) -> bytes:
    """Return the combined patch against the pinned commit."""
    git = resolve_git(str(git) if git is not None else None)
    return _index_diff(git, checkout, "HEAD")


def _index_diff(
    git: Path, checkout: Path, base: str, *, index: Path | None = None, fixed_worktree: bool = False,
) -> bytes:
    return _git(
        git, checkout, "diff", "--cached", "--binary", "--full-index", "--no-renames",
        "--no-ext-diff", "--no-textconv", "--no-color", "--ignore-submodules=none",
        "--src-prefix=a/", "--dst-prefix=b/", base, "--",
        index=index, fixed_worktree=fixed_worktree,
    )
