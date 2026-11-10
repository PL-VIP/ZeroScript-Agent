"""Workspace tools exposed by the local ZeroScript Agent bridge."""

from __future__ import annotations

import difflib
import fnmatch
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Iterable

from .patching import PatchError, apply_patches, parse_unified_diff
from .security import (
    DEFAULT_EXCLUDES,
    DEFAULT_SECRET_PATTERNS,
    PathGuard,
    WorkspaceSecurityError,
    looks_binary,
    revision,
)


class ToolError(RuntimeError):
    pass


def _schema(properties: dict[str, Any], required: Iterable[str] = ()) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": list(required), "additionalProperties": False}


TOOLS = [
    {
        "name": "workspace_info",
        "description": "Show the selected workspace, safety limits, runner mode, and Git availability.",
        "inputSchema": _schema({}),
    },
    {
        "name": "list_files",
        "description": "List files and directories below a workspace-relative path.",
        "inputSchema": _schema({
            "path": {"type": "string", "description": "Workspace-relative directory; defaults to ."},
            "depth": {"type": "integer", "minimum": 0, "maximum": 10},
            "max_results": {"type": "integer", "minimum": 1, "maximum": 1000},
        }),
    },
    {
        "name": "read_file",
        "description": "Read a UTF-8 text file or a 1-based inclusive line range. Returns a revision for safe edits.",
        "inputSchema": _schema({
            "path": {"type": "string"},
            "start_line": {"type": "integer", "minimum": 1},
            "end_line": {"type": "integer", "minimum": 1},
            "max_bytes": {"type": "integer", "minimum": 1, "maximum": 1000000},
        }, ["path"]),
    },
    {
        "name": "batch_read",
        "description": "Run up to 10 read-only workspace calls and return clearly separated results. Used for multi-call DSML replies.",
        "inputSchema": _schema({
            "calls": {
                "type": "array",
                "minItems": 1,
                "maxItems": 10,
                "items": {
                    "type": "object",
                    "properties": {
                        "tool": {"type": "string"},
                        "arguments": {"type": "object"},
                    },
                    "required": ["tool"],
                },
            },
        }, ["calls"]),
    },
    {
        "name": "search_files",
        "description": "Find workspace files by glob pattern.",
        "inputSchema": _schema({
            "pattern": {"type": "string", "description": "Glob such as **/*.py or *config*"},
            "path": {"type": "string"},
            "max_results": {"type": "integer", "minimum": 1, "maximum": 1000},
        }, ["pattern"]),
    },
    {
        "name": "search_text",
        "description": "Search UTF-8 files for literal text and return file, line, and a short excerpt.",
        "inputSchema": _schema({
            "query": {"type": "string"},
            "path": {"type": "string"},
            "include": {"type": "array", "items": {"type": "string"}},
            "exclude": {"type": "array", "items": {"type": "string"}},
            "case_sensitive": {"type": "boolean"},
            "max_results": {"type": "integer", "minimum": 1, "maximum": 500},
        }, ["query"]),
    },
    {
        "name": "stat_path",
        "description": "Show safe metadata for a workspace-relative file or directory.",
        "inputSchema": _schema({"path": {"type": "string"}}, ["path"]),
    },
    {
        "name": "git_status",
        "description": "Return the repository branch and porcelain status without modifying Git.",
        "inputSchema": _schema({}),
    },
    {
        "name": "git_diff",
        "description": "Return an unstaged or staged Git diff, optionally scoped to one path.",
        "inputSchema": _schema({
            "path": {"type": "string"},
            "staged": {"type": "boolean"},
            "max_bytes": {"type": "integer", "minimum": 1, "maximum": 1000000},
        }),
    },
    {
        "name": "apply_patch",
        "description": "Atomically apply a standard unified diff inside the workspace. Returns the resulting diff and revisions.",
        "inputSchema": _schema({
            "patch": {"type": "string", "description": "Standard unified diff with ---/+++ and @@ headers"},
            "expected_revisions": {"type": "object", "additionalProperties": {"type": "string"}},
        }, ["patch"]),
    },
    {
        "name": "replace_lines",
        "description": "Replace a 1-based inclusive line range after verifying the exact file revision.",
        "inputSchema": _schema({
            "path": {"type": "string"},
            "start_line": {"type": "integer", "minimum": 1},
            "end_line": {"type": "integer", "minimum": 1},
            "content": {"type": "string"},
            "expected_revision": {"type": "string"},
        }, ["path", "start_line", "end_line", "content", "expected_revision"]),
    },
    {
        "name": "move_path",
        "description": "Move one file or directory within the workspace. Requires local user approval.",
        "inputSchema": _schema({
            "source": {"type": "string"},
            "destination": {"type": "string"},
            "expected_revision": {"type": "string"},
        }, ["source", "destination"]),
    },
    {
        "name": "create_directory",
        "description": "Create a workspace directory, optionally including missing parents.",
        "inputSchema": _schema({
            "path": {"type": "string"},
            "parents": {"type": "boolean"},
        }, ["path"]),
    },
    {
        "name": "delete_file",
        "description": "Permanently delete one exact file after revision verification and local user approval.",
        "inputSchema": _schema({
            "path": {"type": "string"},
            "expected_revision": {"type": "string"},
        }, ["path", "expected_revision"]),
    },
    {
        "name": "trash_path",
        "description": "Move a file or directory to the recoverable ZeroScript trash. Requires local user approval.",
        "inputSchema": _schema({
            "path": {"type": "string"},
            "expected_revision": {"type": "string"},
        }, ["path"]),
    },
    {
        "name": "run_command",
        "description": "Propose a command in the workspace. Every invocation requires fresh local user approval before execution.",
        "inputSchema": _schema({
            "argv": {"type": "array", "items": {"type": "string"}, "minItems": 1},
            "cwd": {"type": "string", "description": "Workspace-relative directory; defaults to ."},
            "timeout_ms": {"type": "integer", "minimum": 100, "maximum": 600000},
            "network": {"type": "boolean", "description": "Request network access; always shown in the approval dialog"},
        }, ["argv"]),
    },
]


class WorkspaceRuntime:
    def __init__(self, root: Path, config: dict[str, Any], data_dir: Path) -> None:
        ws = config.get("workspace", {})
        self.guard = PathGuard(
            root,
            excludes=ws.get("exclude", DEFAULT_EXCLUDES),
            secret_patterns=ws.get("secret_patterns", DEFAULT_SECRET_PATTERNS),
        )
        self.root = self.guard.root
        self.max_file_bytes = int(ws.get("max_file_bytes", 250_000))
        self.max_output_bytes = int(config.get("limits", {}).get("max_output_bytes", 200_000))
        self.runner = config.get("runner", {})
        self.data_dir = data_dir
        self.trash_dir = data_dir / "trash"
        self.audit_path = data_dir / "audit.jsonl"
        self._mutation_lock = threading.Lock()
        data_dir.mkdir(parents=True, exist_ok=True)

    @property
    def tools(self) -> list[dict[str, Any]]:
        return [{**tool, "server": "workspace"} for tool in TOOLS]

    def call(self, name: str, args: dict[str, Any]) -> str:
        method = getattr(self, f"tool_{name}", None)
        if not method:
            raise ToolError(f"unknown tool: {name}")
        started = time.time()
        try:
            result = method(**args)
            self._audit(name, args, True, time.time() - started)
            text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, indent=2)
            encoded = text.encode("utf-8")
            if len(encoded) > self.max_output_bytes:
                text = encoded[:self.max_output_bytes].decode("utf-8", errors="ignore")
                text += "\n... tool result truncated by workspace output limit ..."
            return text
        except (TypeError, ValueError, OSError, PatchError, WorkspaceSecurityError, subprocess.SubprocessError) as exc:
            self._audit(name, args, False, time.time() - started, str(exc))
            raise ToolError(str(exc)) from exc

    def approval_details(self, name: str, args: dict[str, Any]) -> dict[str, Any] | None:
        if name == "run_command":
            argv = args.get("argv") or []
            cwd = args.get("cwd", ".")
            mode = str(self.runner.get("mode", "host"))
            return {
                "title": "Uruchomić komendę?",
                "risk": "command",
                "command": self._display_command(argv),
                "argv": argv,
                "cwd": cwd,
                "runner": mode,
                "network": bool(args.get("network", False)),
                "timeout_ms": int(args.get("timeout_ms", self.runner.get("timeout_ms", 120000))),
                "warning": (
                    "Tryb host może uzyskać dostęp poza workspace i nie wymusza blokady sieci. Zatwierdź tylko zaufane polecenie."
                    if mode == "host" else
                    "Polecenie zostanie uruchomione w skonfigurowanym kontenerze."
                ),
            }
        if name in {"trash_path", "move_path", "delete_file"}:
            return {
                "title": "Zatwierdzić operację na plikach?",
                "risk": "destructive" if name in {"trash_path", "delete_file"} else "move",
                "operation": name,
                "arguments": args,
                "warning": (
                    "Ta operacja trwale usuwa plik. Sprawdź ścieżkę i revision przed zatwierdzeniem."
                    if name == "delete_file" else
                    "Sprawdź dokładne ścieżki przed zatwierdzeniem."
                ),
            }
        return None

    def _audit(self, name: str, args: dict[str, Any], ok: bool, elapsed: float, error: str = "") -> None:
        safe_args = {k: ("<redacted>" if "content" in k or k == "patch" else v) for k, v in args.items()}
        record = {
            "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "tool": name,
            "arguments": safe_args,
            "ok": ok,
            "elapsed_ms": round(elapsed * 1000),
        }
        if error:
            record["error"] = error[:500]
        with self.audit_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    def audit_approval(self, name: str, args: dict[str, Any], approved: bool) -> None:
        self._audit(
            f"approval:{name}",
            args,
            approved,
            0.0,
            "" if approved else "operation denied by the user",
        )

    def _read_bytes(self, path: Path, *, max_bytes: int | None = None) -> bytes:
        self.guard.assert_readable(path)
        if not path.is_file():
            raise ToolError(f"not a file: {self.guard.relative(path)}")
        limit = min(max_bytes or self.max_file_bytes, 1_000_000)
        size = path.stat().st_size
        if size > limit:
            raise ToolError(f"file is {size} bytes; limit is {limit}. Read a smaller file or raise max_bytes explicitly.")
        data = path.read_bytes()
        if looks_binary(data):
            raise ToolError("binary files cannot be read or edited with text tools")
        return data

    @staticmethod
    def _decode(data: bytes) -> tuple[str, bool]:
        bom = data.startswith(b"\xef\xbb\xbf")
        try:
            return data.decode("utf-8-sig" if bom else "utf-8"), bom
        except UnicodeDecodeError as exc:
            raise ToolError("only UTF-8 text files are supported") from exc

    @staticmethod
    def _encode(text: str, *, bom: bool, newline: str) -> bytes:
        if newline != "\n":
            text = text.replace("\n", newline)
        data = text.encode("utf-8")
        return (b"\xef\xbb\xbf" + data) if bom else data

    @staticmethod
    def _newline(data: bytes) -> str:
        return "\r\n" if b"\r\n" in data else "\n"

    def _iter_files(self, start: Path) -> Iterable[Path]:
        if start.is_file():
            self.guard.assert_readable(start)
            yield start
            return
        for base, dirs, files in os.walk(start, followlinks=False):
            base_path = Path(base)
            kept = []
            for dirname in dirs:
                candidate = base_path / dirname
                rel = self.guard.relative(candidate)
                if self.guard.is_excluded(rel) or candidate.is_symlink():
                    continue
                kept.append(dirname)
            dirs[:] = kept
            for filename in files:
                candidate = base_path / filename
                rel = self.guard.relative(candidate)
                if self.guard.is_excluded(rel) or candidate.is_symlink() or self.guard.is_secret(rel):
                    continue
                yield candidate

    def _atomic_write(self, path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        previous_mode = path.stat().st_mode if path.exists() else None
        fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".zs-tmp", dir=str(path.parent))
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            if previous_mode is not None:
                os.chmod(tmp_name, previous_mode)
            os.replace(tmp_name, path)
        finally:
            try:
                os.unlink(tmp_name)
            except FileNotFoundError:
                pass

    @staticmethod
    def _display_command(argv: list[Any]) -> str:
        def quote(value: Any) -> str:
            text = str(value)
            return json.dumps(text, ensure_ascii=False) if any(c.isspace() or c in '"&|;<>$`' for c in text) else text
        return " ".join(quote(x) for x in argv)

    def tool_workspace_info(self) -> dict[str, Any]:
        git = shutil.which("git") is not None and (self.root / ".git").exists()
        return {
            "workspace": self.root.name,
            "root_exposed_to_agent": ".",
            "platform": os.name,
            "git": git,
            "runner": self.runner.get("mode", "host"),
            "command_approval": "required for every invocation",
            "max_file_bytes": self.max_file_bytes,
            "max_output_bytes": self.max_output_bytes,
        }

    def tool_list_files(self, path: str = ".", depth: int = 2, max_results: int = 300) -> dict[str, Any]:
        start = self.guard.resolve(path)
        self.guard.assert_readable(start)
        if not start.is_dir():
            raise ToolError("list_files path must be a directory")
        depth = max(0, min(int(depth), 10))
        limit = max(1, min(int(max_results), 1000))
        base_depth = len(start.parts)
        entries: list[dict[str, Any]] = []
        for base, dirs, files in os.walk(start, followlinks=False):
            current = Path(base)
            level = len(current.parts) - base_depth
            dirs[:] = sorted(
                d for d in dirs
                if level < depth
                and not (current / d).is_symlink()
                and not self.guard.is_excluded(self.guard.relative(current / d))
            )
            for dirname in dirs:
                entries.append({"path": self.guard.relative(current / dirname), "type": "directory"})
                if len(entries) >= limit:
                    return {"entries": entries, "truncated": True}
            for filename in sorted(files):
                candidate = current / filename
                rel = self.guard.relative(candidate)
                if candidate.is_symlink() or self.guard.is_excluded(rel) or self.guard.is_secret(rel):
                    continue
                entries.append({"path": rel, "type": "file", "size": candidate.stat().st_size})
                if len(entries) >= limit:
                    return {"entries": entries, "truncated": True}
        return {"entries": entries, "truncated": False}

    def tool_read_file(
        self,
        path: str,
        start_line: int | None = None,
        end_line: int | None = None,
        max_bytes: int | None = None,
    ) -> str:
        target = self.guard.resolve(path)
        data = self._read_bytes(target, max_bytes=max_bytes)
        text, bom = self._decode(data)
        lines = text.splitlines()
        total = len(lines)
        start = int(start_line or 1)
        end = int(end_line or total or 1)
        if start < 1 or end < start:
            raise ToolError("line range must be 1-based with end_line >= start_line")
        if total and start > total:
            raise ToolError(f"start_line {start} is outside the file, which has {total} lines")
        selected = lines[start - 1:end]
        actual_end = min(end, total)
        encoding = "utf-8-bom" if bom else "utf-8"
        newline = "crlf" if self._newline(data) == "\r\n" else "lf"
        numbered = "\n".join(f"{start + i:>6} | {line}" for i, line in enumerate(selected))
        line_label = f"{start}-{actual_end} of {total}" if total else "empty file (0 lines)"
        return (
            f"FILE: {self.guard.relative(target)}\n"
            f"REVISION: {revision(data)}\n"
            f"ENCODING: {encoding}; NEWLINE: {newline}\n"
            f"LINES: {line_label}\n"
            f"---\n{numbered}"
        ).rstrip()

    def tool_batch_read(self, calls: list[dict[str, Any]]) -> str:
        if not isinstance(calls, list) or not 1 <= len(calls) <= 10:
            raise ToolError("batch_read requires between 1 and 10 calls")
        allowed = {
            "workspace_info", "list_files", "read_file", "search_files",
            "search_text", "stat_path", "git_status", "git_diff",
        }
        sections = []
        for index, call in enumerate(calls, 1):
            if not isinstance(call, dict):
                raise ToolError(f"batch_read item {index} must be an object")
            name = str(call.get("tool") or call.get("command") or "")
            arguments = call.get("arguments")
            if arguments is None:
                arguments = call.get("params") or {}
            if name not in allowed:
                raise ToolError(f"batch_read only accepts read-only tools; item {index} requested {name!r}")
            if not isinstance(arguments, dict):
                raise ToolError(f"batch_read arguments for item {index} must be an object")
            method = getattr(self, f"tool_{name}")
            result = method(**arguments)
            text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, indent=2)
            label = arguments.get("path") or arguments.get("query") or ""
            sections.append(f"=== {index}/{len(calls)} {name}{f' [{label}]' if label else ''} ===\n{text}")
        return "\n\n".join(sections)

    def tool_search_files(self, pattern: str, path: str = ".", max_results: int = 200) -> dict[str, Any]:
        start = self.guard.resolve(path)
        self.guard.assert_readable(start)
        limit = max(1, min(int(max_results), 1000))
        matches = []
        for candidate in self._iter_files(start):
            rel = self.guard.relative(candidate)
            if fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(candidate.name, pattern):
                matches.append(rel)
                if len(matches) >= limit:
                    return {"matches": matches, "truncated": True}
        return {"matches": matches, "truncated": False}

    def tool_search_text(
        self,
        query: str,
        path: str = ".",
        include: list[str] | None = None,
        exclude: list[str] | None = None,
        case_sensitive: bool = False,
        max_results: int = 100,
    ) -> dict[str, Any]:
        if not query:
            raise ToolError("query must not be empty")
        start = self.guard.resolve(path)
        self.guard.assert_readable(start)
        include = include or []
        exclude = exclude or []
        limit = max(1, min(int(max_results), 500))
        needle = query if case_sensitive else query.casefold()
        matches = []
        skipped = 0
        for candidate in self._iter_files(start):
            rel = self.guard.relative(candidate)
            if include and not any(fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(candidate.name, pat) for pat in include):
                continue
            if any(fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(candidate.name, pat) for pat in exclude):
                continue
            try:
                if candidate.stat().st_size > self.max_file_bytes:
                    skipped += 1
                    continue
                data = candidate.read_bytes()
                if looks_binary(data):
                    skipped += 1
                    continue
                text, _ = self._decode(data)
            except (OSError, ToolError):
                skipped += 1
                continue
            for line_no, line in enumerate(text.splitlines(), 1):
                haystack = line if case_sensitive else line.casefold()
                if needle in haystack:
                    matches.append({"path": rel, "line": line_no, "text": line.strip()[:300]})
                    if len(matches) >= limit:
                        return {"matches": matches, "truncated": True, "skipped_files": skipped}
        return {"matches": matches, "truncated": False, "skipped_files": skipped}

    def tool_stat_path(self, path: str) -> dict[str, Any]:
        target = self.guard.resolve(path)
        self.guard.assert_readable(target)
        stat = target.stat()
        result = {
            "path": self.guard.relative(target),
            "type": "directory" if target.is_dir() else "file",
            "size": stat.st_size,
            "modified": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(stat.st_mtime)),
            "symlink": target.is_symlink(),
        }
        if target.is_file() and stat.st_size <= self.max_file_bytes:
            result["revision"] = revision(target.read_bytes())
        return result

    def _git(self, argv: list[str], max_bytes: int | None = None) -> str:
        if not shutil.which("git"):
            raise ToolError("git is not installed")
        proc = subprocess.run(
            ["git", *argv], cwd=self.root, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=30, shell=False,
        )
        # Preserve leading spaces: `git status --short` uses the first two
        # columns as meaningful index/worktree state (for example " M file").
        output = (proc.stdout + proc.stderr).rstrip()
        if proc.returncode != 0:
            raise ToolError(output or f"git exited with code {proc.returncode}")
        limit = min(max_bytes or self.max_output_bytes, 1_000_000)
        encoded = output.encode("utf-8")
        if len(encoded) > limit:
            output = encoded[:limit].decode("utf-8", errors="ignore") + "\n... output truncated ..."
        return output

    def tool_git_status(self) -> str:
        branch = self._git(["branch", "--show-current"])
        status = self._git(["status", "--short"])
        visible = []
        for line in status.splitlines():
            raw_path = line[3:].strip() if len(line) > 3 else ""
            if " -> " in raw_path:
                raw_path = raw_path.split(" -> ", 1)[1]
            raw_path = raw_path.strip('"').replace("\\", "/")
            if raw_path and self.guard.is_secret(raw_path):
                continue
            visible.append(line)
        return f"branch: {branch or '(detached)'}\n{chr(10).join(visible) or 'working tree clean'}"

    def tool_git_diff(self, path: str | None = None, staged: bool = False, max_bytes: int | None = None) -> str:
        argv = ["diff"]
        if staged:
            argv.append("--cached")
        if path:
            target = self.guard.resolve(path)
            self.guard.assert_readable(target)
            safe = self.guard.relative(target)
            argv.extend(["--", safe])
        else:
            argv.extend(["--", "."])
            for pattern in self.guard.secret_patterns:
                argv.append(f":(exclude,glob){pattern}")
                argv.append(f":(exclude,glob)**/{pattern}")
        return self._git(argv, max_bytes=max_bytes) or "no diff"

    def tool_replace_lines(
        self,
        path: str,
        start_line: int,
        end_line: int,
        content: str,
        expected_revision: str,
    ) -> dict[str, Any]:
        target = self.guard.resolve(path, allow_root=False)
        with self._mutation_lock:
            original = self._read_bytes(target)
            if revision(original) != expected_revision:
                raise ToolError("revision conflict: the file changed after it was read; read it again before editing")
            text, bom = self._decode(original)
            newline = self._newline(original)
            lines = text.splitlines()
            start, end = int(start_line), int(end_line)
            if start < 1 or end < start or end > len(lines):
                raise ToolError(f"invalid line range {start}-{end}; file has {len(lines)} lines")
            replacement = content.replace("\r\n", "\n").splitlines()
            updated_lines = lines[:start - 1] + replacement + lines[end:]
            updated = "\n".join(updated_lines)
            if text.endswith(("\n", "\r")):
                updated += "\n"
            data = self._encode(updated, bom=bom, newline=newline)
            self._atomic_write(target, data)
            diff = "\n".join(difflib.unified_diff(
                text.splitlines(), updated.splitlines(),
                fromfile=f"a/{self.guard.relative(target)}", tofile=f"b/{self.guard.relative(target)}", lineterm="",
            ))
            return {"path": self.guard.relative(target), "revision": revision(data), "diff": diff}

    def tool_apply_patch(self, patch: str, expected_revisions: dict[str, str] | None = None) -> dict[str, Any]:
        parsed = parse_unified_diff(patch)
        expected_revisions = expected_revisions or {}
        originals: dict[str, bytes | None] = {}
        texts: dict[str, str] = {}
        encodings: dict[str, tuple[bool, str]] = {}
        paths: dict[str, Path] = {}
        with self._mutation_lock:
            for item in parsed:
                if item.old_path and item.new_path and item.old_path != item.new_path:
                    raise ToolError("apply_patch does not rename files; use move_path with local approval")
                if item.old_path:
                    old_target = self.guard.resolve(item.old_path, allow_root=False)
                    old_data = self._read_bytes(old_target)
                    old_text, bom = self._decode(old_data)
                    originals[item.old_path] = old_data
                    texts[item.old_path] = old_text
                    encodings[item.old_path] = (bom, self._newline(old_data))
                    wanted = expected_revisions.get(item.old_path)
                    if wanted and wanted != revision(old_data):
                        raise ToolError(f"revision conflict for {item.old_path}; read it again before applying the patch")
                if item.new_path:
                    target = self.guard.resolve(item.new_path, must_exist=False, allow_root=False)
                    rel = self.guard.relative(target)
                    if self.guard.is_excluded(rel) or self.guard.is_secret(rel):
                        raise ToolError(f"patch target is blocked by workspace policy: {rel}")
                    if item.old_path is None and target.exists():
                        raise ToolError(f"new file already exists: {item.new_path}")
                    paths[item.new_path] = target
            updated = apply_patches(texts, parsed)
            encoded: dict[str, bytes] = {}
            for item in parsed:
                assert item.new_path is not None
                source_key = item.old_path
                bom, newline = encodings.get(source_key or "", (False, "\n"))
                encoded[item.new_path] = self._encode(updated[item.new_path], bom=bom, newline=newline)
            backups = {name: (path.read_bytes() if path.exists() else None) for name, path in paths.items()}
            written: list[str] = []
            try:
                for name, data in encoded.items():
                    self._atomic_write(paths[name], data)
                    written.append(name)
            except Exception:
                for name in written:
                    previous = backups[name]
                    if previous is None:
                        paths[name].unlink(missing_ok=True)
                    else:
                        self._atomic_write(paths[name], previous)
                raise
            diffs = []
            revisions = {}
            for item in parsed:
                assert item.new_path is not None
                before = texts.get(item.old_path or "", "")
                after = updated[item.new_path]
                diffs.extend(difflib.unified_diff(
                    before.splitlines(), after.splitlines(),
                    fromfile=f"a/{item.old_path or item.new_path}", tofile=f"b/{item.new_path}", lineterm="",
                ))
                revisions[item.new_path] = revision(encoded[item.new_path])
            return {"changed_files": list(encoded), "revisions": revisions, "diff": "\n".join(diffs)}

    def _verify_optional_revision(self, target: Path, expected_revision: str | None) -> None:
        if expected_revision:
            if not target.is_file():
                raise ToolError("expected_revision can only be used with a file")
            current = revision(self._read_bytes(target))
            if current != expected_revision:
                raise ToolError("revision conflict: the file changed after it was read")

    def tool_move_path(self, source: str, destination: str, expected_revision: str | None = None) -> dict[str, Any]:
        src = self.guard.resolve(source, allow_root=False)
        dst = self.guard.resolve(destination, must_exist=False, allow_root=False)
        if dst.exists():
            raise ToolError(f"destination already exists: {destination}")
        self._verify_optional_revision(src, expected_revision)
        with self._mutation_lock:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))
        return {"moved": self.guard.relative(src), "destination": self.guard.relative(dst)}

    def tool_create_directory(self, path: str, parents: bool = True) -> dict[str, Any]:
        target = self.guard.resolve(path, must_exist=False, allow_root=False)
        rel = self.guard.relative(target)
        if self.guard.is_excluded(rel) or self.guard.is_secret(rel):
            raise ToolError(f"directory is blocked by workspace policy: {rel}")
        if target.exists():
            if not target.is_dir():
                raise ToolError(f"a non-directory already exists at: {rel}")
            return {"path": rel, "created": False, "message": "directory already exists"}
        with self._mutation_lock:
            target.mkdir(parents=bool(parents), exist_ok=False)
        return {"path": rel, "created": True}

    def tool_delete_file(self, path: str, expected_revision: str) -> dict[str, Any]:
        self.guard.assert_no_link_components(path)
        target = self.guard.resolve(path, allow_root=False)
        self.guard.assert_readable(target)
        if not target.is_file():
            raise ToolError("delete_file only deletes one file; directories are not allowed")
        with self._mutation_lock:
            current = target.read_bytes()
            if revision(current) != expected_revision:
                raise ToolError("revision conflict: the file changed after it was inspected")
            rel = self.guard.relative(target)
            target.unlink()
        return {"deleted": rel, "permanent": True}

    def tool_trash_path(self, path: str, expected_revision: str | None = None) -> dict[str, Any]:
        target = self.guard.resolve(path, allow_root=False)
        self._verify_optional_revision(target, expected_revision)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        destination = self.trash_dir / f"{stamp}-{target.name}"
        destination.parent.mkdir(parents=True, exist_ok=True)
        with self._mutation_lock:
            shutil.move(str(target), str(destination))
        return {
            "trashed": self.guard.relative(target),
            "trash_id": destination.name,
            "recoverable": True,
            "note": "The item is stored in the local ZeroScript Agent data directory, outside the workspace.",
        }

    def tool_run_command(
        self,
        argv: list[str],
        cwd: str = ".",
        timeout_ms: int | None = None,
        network: bool = False,
    ) -> dict[str, Any]:
        if not isinstance(argv, list) or not argv or not all(isinstance(x, str) and x for x in argv):
            raise ToolError("argv must be a non-empty array of non-empty strings")
        workdir = self.guard.resolve(cwd)
        if not workdir.is_dir():
            raise ToolError("cwd must be a directory")
        timeout = min(int(timeout_ms or self.runner.get("timeout_ms", 120000)), 600000) / 1000
        mode = str(self.runner.get("mode", "host"))
        command = list(argv)
        run_cwd = workdir
        if mode == "disabled":
            raise ToolError("command runner is disabled in config.json")
        if mode == "docker":
            docker = shutil.which(str(self.runner.get("executable", "docker")))
            if not docker:
                raise ToolError("Docker runner is configured but Docker was not found")
            image = str(self.runner.get("image", "python:3.12-slim"))
            rel_cwd = workdir.relative_to(self.root).as_posix()
            command = [
                docker, "run", "--rm", "--init",
                "--network", "bridge" if network else "none",
                "--memory", str(self.runner.get("memory", "1g")),
                "--pids-limit", str(self.runner.get("pids_limit", 256)),
                "-v", f"{self.root}:/workspace",
                "-w", f"/workspace/{rel_cwd}" if rel_cwd != "." else "/workspace",
                image, *argv,
            ]
            run_cwd = self.root
        elif mode != "host":
            raise ToolError(f"unsupported runner mode: {mode}")
        if mode == "host":
            default_env = [
                "PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC",
                "TEMP", "TMP", "HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA",
                "LANG", "LC_ALL", "TERM",
            ]
            inherited = self.runner.get("inherit_env", default_env)
            command_env = {key: os.environ[key] for key in inherited if key in os.environ}
            command_env["NO_COLOR"] = "1"
        else:
            # These variables configure the Docker client only; Docker doesn't
            # copy them into the container unless explicit -e flags are added.
            command_env = os.environ.copy()
        started = time.monotonic()
        try:
            proc = subprocess.run(
                command,
                cwd=run_cwd,
                capture_output=True,
                timeout=timeout,
                shell=False,
                env=command_env,
            )
            raw = proc.stdout + (b"\n" if proc.stdout and proc.stderr else b"") + proc.stderr
            truncated = len(raw) > self.max_output_bytes
            raw = raw[:self.max_output_bytes]
            output = raw.decode("utf-8", errors="replace")
            if truncated:
                output += "\n... output truncated ..."
            return {
                "argv": argv,
                "cwd": self.guard.relative(workdir),
                "runner": mode,
                "network_requested": bool(network),
                "exit_code": proc.returncode,
                "elapsed_ms": round((time.monotonic() - started) * 1000),
                "output": output,
            }
        except subprocess.TimeoutExpired as exc:
            raise ToolError(f"command timed out after {timeout:g}s") from exc

