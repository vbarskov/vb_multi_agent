"""Local Git snapshots and ownership checks for agent patches."""

from __future__ import annotations

import os
import stat
import subprocess
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


def _git(
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
            ["git", *options, *args],
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


def _reject_filters(checkout: Path, *, fixed_worktree: bool = False) -> None:
    # Inspect config before commands that might invoke repository-configured clean filters.
    if _git(
        checkout, "config", "--name-only", "--get-regexp", r"^filter\.",
        allowed_codes=(0, 1), fixed_worktree=fixed_worktree,
    ):
        raise RuntimeError("The Git checkout defines filters. Use a clean clone without Git filters.")


class Workspace:
    """Pin the committed state of a clean source without modifying its checkout."""

    def __init__(self, source: Path) -> None:
        self.root = Path(os.fsdecode(_git(source, "rev-parse", "--show-toplevel").removesuffix(b"\n")))
        self.revision = _git(self.root, "rev-parse", "--verify", "HEAD^{commit}").decode().strip()
        self._object_format = _git(self.root, "rev-parse", "--show-object-format").decode().strip()
        _reject_filters(self.root)
        if _git(
            self.root, "status", "--porcelain=v1", "-z", "--untracked-files=all", "--ignore-submodules=dirty",
        ):
            raise RuntimeError("The source Git checkout has local changes. Commit or stash them before running.")
        if _git(self.root, "rev-parse", "--verify", "HEAD^{commit}").decode().strip() != self.revision:
            raise RuntimeError("HEAD moved while checking the source checkout. Start the run again.")

    def clone(self, destination: Path) -> Path:
        """Make a private checkout of the pinned commit using only the local Git repository."""
        try:
            destination.mkdir()
        except OSError as exc:
            raise RuntimeError(f"Could not create Git checkout at {destination}: {exc}") from exc
        _git(destination, "init", "--quiet", "--template=", f"--object-format={self._object_format}")
        _git(
            destination, "fetch", "--quiet", "--depth=1", "--no-tags", "--no-recurse-submodules",
            "--no-write-fetch-head", "--no-auto-maintenance", "--", str(self.root), self.revision,
        )
        # Materialize committed symlinks as text so agents cannot follow them outside this checkout.
        _git(destination, "config", "--local", "core.symlinks", "false")
        _git(destination, "checkout", "--quiet", "--detach", self.revision)
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
    checkout: Path, base: str, index: Path, allowed_paths: Collection[str], *, fixed_worktree: bool = False,
) -> bool:
    owned = {normalize_path(path) for path in allowed_paths}
    delta = _git(
        checkout, "diff", "--cached", "--raw", "--no-renames", "--no-ext-diff", "--ignore-submodules=none", "-z",
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


def collect_patch(checkout: Path, allowed_paths: Collection[str], *, base: str = "HEAD") -> bytes:
    """Capture a Developer's final working files against the pinned base in a private index."""
    checkout = checkout.absolute()
    files = _checkout_files(checkout)
    _reject_filters(checkout, fixed_worktree=True)
    with tempfile.TemporaryDirectory(prefix=".vb-index-", dir=checkout.parent) as directory:
        index = Path(directory) / "index"
        _git(checkout, "read-tree", base, index=index, fixed_worktree=True)
        _git(checkout, "add", "--all", "--force", "--", ".", index=index, fixed_worktree=True)
        tracked = _git(checkout, "ls-files", "--cached", "-z", index=index, fixed_worktree=True)
        uncaptured = files - {os.fsdecode(name) for name in tracked.split(b"\0") if name}
        if uncaptured:
            raise RuntimeError(f"Could not capture Developer file: {min(uncaptured)!r}")
        if not _verify_delta(checkout, base, index, allowed_paths, fixed_worktree=True):
            return b""
        return _index_diff(checkout, base, index=index, fixed_worktree=True)


def apply_patch(checkout: Path, patch: bytes | str, allowed_paths: Collection[str]) -> None:
    """Apply a patch after checking the actual index delta against the task's paths."""
    try:
        data = patch.encode() if isinstance(patch, str) else patch
    except UnicodeEncodeError as exc:
        raise RuntimeError("The patch must be UTF-8 text.") from exc
    previous_tree = _git(checkout, "write-tree").decode().strip()
    with tempfile.TemporaryDirectory(prefix=".vb-index-", dir=checkout.parent) as directory:
        index = Path(directory) / "index"
        _git(checkout, "read-tree", previous_tree, index=index)
        _git(checkout, "apply", "--cached", "--check", "-", data=data, index=index)
        _git(checkout, "apply", "--cached", "-", data=data, index=index)
        if not _verify_delta(checkout, previous_tree, index, allowed_paths):
            raise RuntimeError("The patch has no file changes.")
        verified_patch = _index_diff(checkout, previous_tree, index=index)
    _git(checkout, "apply", "--index", "--check", "-", data=verified_patch)
    _git(checkout, "apply", "--index", "-", data=verified_patch)


def integrated_diff(checkout: Path) -> bytes:
    """Return the combined patch against the pinned commit."""
    return _index_diff(checkout, "HEAD")


def _index_diff(
    checkout: Path, base: str, *, index: Path | None = None, fixed_worktree: bool = False,
) -> bytes:
    return _git(
        checkout, "diff", "--cached", "--binary", "--full-index", "--no-renames",
        "--no-ext-diff", "--no-textconv", "--no-color", "--ignore-submodules=none",
        "--src-prefix=a/", "--dst-prefix=b/", base, "--",
        index=index, fixed_worktree=fixed_worktree,
    )
