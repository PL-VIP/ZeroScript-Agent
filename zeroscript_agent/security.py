"""Workspace path validation and content safety helpers."""

from __future__ import annotations

import fnmatch
import hashlib
import os
from pathlib import Path
from typing import Iterable


class WorkspaceSecurityError(ValueError):
    """Raised when an operation attempts to leave the selected workspace."""


DEFAULT_EXCLUDES = (
    ".git",
    ".hg",
    ".svn",
    "node_modules",
    ".venv",
    "venv",
    "__pycache__",
    "dist",
    "build",
)

DEFAULT_SECRET_PATTERNS = (
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "id_rsa*",
    "id_ed25519*",
    "credentials.json",
    "service-account*.json",
    ".npmrc",
    ".pypirc",
)


def revision(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def looks_binary(data: bytes) -> bool:
    if not data:
        return False
    if b"\x00" in data[:8192]:
        return True
    sample = data[:8192]
    controls = sum(1 for b in sample if b < 9 or 13 < b < 32)
    return controls / len(sample) > 0.08


class PathGuard:
    """Resolve user supplied paths while enforcing a single workspace root."""

    def __init__(
        self,
        root: Path,
        excludes: Iterable[str] = DEFAULT_EXCLUDES,
        secret_patterns: Iterable[str] = DEFAULT_SECRET_PATTERNS,
    ) -> None:
        root = root.expanduser().resolve(strict=True)
        if not root.is_dir():
            raise WorkspaceSecurityError(f"workspace is not a directory: {root}")
        self.root = root
        self.excludes = tuple(str(x) for x in excludes)
        self.secret_patterns = tuple(str(x) for x in secret_patterns)
        self._root_key = os.path.normcase(str(root))

    def _inside(self, path: Path) -> bool:
        candidate = os.path.normcase(str(path))
        try:
            return os.path.commonpath([self._root_key, candidate]) == self._root_key
        except ValueError:
            return False

    @staticmethod
    def _validate_raw(raw: str) -> str:
        if raw is None:
            raise WorkspaceSecurityError("path is required")
        raw = str(raw).strip().replace("\\", "/")
        if not raw:
            raw = "."
        if "\x00" in raw:
            raise WorkspaceSecurityError("path contains a NUL byte")
        if raw.startswith("//") or Path(raw).is_absolute():
            raise WorkspaceSecurityError("absolute and UNC paths are not allowed")
        parts = [p for p in raw.split("/") if p not in ("", ".")]
        if any(p == ".." for p in parts):
            raise WorkspaceSecurityError("parent path traversal is not allowed")
        if os.name == "nt":
            for part in parts:
                if ":" in part:
                    raise WorkspaceSecurityError("NTFS alternate streams and drive paths are not allowed")
                stem = part.split(".", 1)[0].upper()
                if stem in {"CON", "PRN", "AUX", "NUL"} or stem.startswith("COM") or stem.startswith("LPT"):
                    if stem in {"CON", "PRN", "AUX", "NUL"} or stem[3:].isdigit():
                        raise WorkspaceSecurityError(f"reserved Windows path component: {part}")
        return "/".join(parts) or "."

    def resolve(self, raw: str, *, must_exist: bool = True, allow_root: bool = True) -> Path:
        clean = self._validate_raw(raw)
        lexical = self.root if clean == "." else self.root.joinpath(*clean.split("/"))
        if must_exist:
            resolved = lexical.resolve(strict=True)
        else:
            ancestor = lexical.parent
            missing: list[str] = []
            while not ancestor.exists() and ancestor != self.root:
                missing.append(ancestor.name)
                ancestor = ancestor.parent
            resolved_ancestor = ancestor.resolve(strict=True)
            if not self._inside(resolved_ancestor):
                raise WorkspaceSecurityError("path parent resolves outside the workspace")
            resolved = resolved_ancestor.joinpath(*reversed(missing), lexical.name)
        if not self._inside(resolved):
            raise WorkspaceSecurityError("path resolves outside the workspace")
        if not allow_root and resolved == self.root:
            raise WorkspaceSecurityError("the workspace root cannot be modified")
        return resolved

    def relative(self, path: Path) -> str:
        return path.relative_to(self.root).as_posix() or "."

    def is_excluded(self, relative_path: str) -> bool:
        parts = Path(relative_path).parts
        return any(part in self.excludes for part in parts)

    def is_secret(self, relative_path: str) -> bool:
        name = Path(relative_path).name
        return any(fnmatch.fnmatch(name, pat) or fnmatch.fnmatch(relative_path, pat) for pat in self.secret_patterns)

    def assert_readable(self, path: Path, *, allow_secret: bool = False) -> None:
        rel = self.relative(path)
        if self.is_excluded(rel):
            raise WorkspaceSecurityError(f"path is excluded by workspace policy: {rel}")
        if self.is_secret(rel) and not allow_secret:
            raise WorkspaceSecurityError(f"path is protected as a possible secret: {rel}")

