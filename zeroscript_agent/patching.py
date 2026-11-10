"""Small transactional unified-diff engine used by apply_patch."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, List, Optional


class PatchError(ValueError):
    pass


@dataclass
class FilePatch:
    old_path: Optional[str]
    new_path: Optional[str]
    hunks: List[tuple[int, int, int, int, list[str]]]


_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def _header_path(line: str) -> Optional[str]:
    value = line[4:].split("\t", 1)[0].strip()
    if value == "/dev/null":
        return None
    if value.startswith("a/") or value.startswith("b/"):
        value = value[2:]
    if not value:
        raise PatchError("empty path in patch header")
    return value


def parse_unified_diff(text: str) -> list[FilePatch]:
    lines = text.replace("\r\n", "\n").splitlines()
    out: list[FilePatch] = []
    i = 0
    while i < len(lines):
        if lines[i].startswith("diff --git "):
            i += 1
            continue
        if not lines[i].startswith("--- "):
            if lines[i].strip():
                raise PatchError(f"expected '---' file header, got: {lines[i][:80]}")
            i += 1
            continue
        old_path = _header_path(lines[i])
        i += 1
        if i >= len(lines) or not lines[i].startswith("+++ "):
            raise PatchError("missing '+++' file header")
        new_path = _header_path(lines[i])
        i += 1
        if old_path is None and new_path is None:
            raise PatchError("both patch paths cannot be /dev/null")
        hunks = []
        while i < len(lines) and not lines[i].startswith("--- ") and not lines[i].startswith("diff --git "):
            if not lines[i].strip():
                i += 1
                continue
            match = _HUNK.match(lines[i])
            if not match:
                raise PatchError(f"invalid hunk header: {lines[i][:80]}")
            old_start = int(match.group(1))
            old_count = int(match.group(2) or "1")
            new_start = int(match.group(3))
            new_count = int(match.group(4) or "1")
            i += 1
            body: list[str] = []
            while i < len(lines):
                line = lines[i]
                if line.startswith("@@ ") or line.startswith("--- ") or line.startswith("diff --git "):
                    break
                if line == "\\ No newline at end of file":
                    i += 1
                    continue
                if not line or line[0] not in " +-":
                    raise PatchError(f"invalid patch line: {line[:80]}")
                body.append(line)
                i += 1
            seen_old = sum(1 for line in body if line[0] in " -")
            seen_new = sum(1 for line in body if line[0] in " +")
            if seen_old != old_count or seen_new != new_count:
                raise PatchError(
                    f"hunk count mismatch: header -{old_count}/+{new_count}, body -{seen_old}/+{seen_new}"
                )
            hunks.append((old_start, old_count, new_start, new_count, body))
        if not hunks:
            raise PatchError("file patch contains no hunks")
        out.append(FilePatch(old_path, new_path, hunks))
    if not out:
        raise PatchError("patch contains no file changes")
    return out


def apply_file_patch(original: str, patch: FilePatch) -> str:
    source = original.splitlines()
    trailing_newline = original.endswith(("\n", "\r"))
    result: list[str] = []
    cursor = 0
    for old_start, _old_count, _new_start, _new_count, body in patch.hunks:
        target = max(0, old_start - 1)
        if target < cursor or target > len(source):
            raise PatchError("overlapping or out-of-range hunk")
        result.extend(source[cursor:target])
        cursor = target
        for line in body:
            marker, value = line[0], line[1:]
            if marker in " -":
                if cursor >= len(source) or source[cursor] != value:
                    actual = source[cursor] if cursor < len(source) else "<end of file>"
                    raise PatchError(f"patch context mismatch: expected {value!r}, got {actual!r}")
                if marker == " ":
                    result.append(value)
                cursor += 1
            elif marker == "+":
                result.append(value)
    result.extend(source[cursor:])
    joined = "\n".join(result)
    if trailing_newline or patch.old_path is None:
        joined += "\n"
    return joined


def apply_patches(contents: Dict[str, str], patches: list[FilePatch]) -> Dict[str, str]:
    output: Dict[str, str] = {}
    for patch in patches:
        if patch.new_path is None:
            raise PatchError("file deletion is not supported by apply_patch; use trash_path")
        original = "" if patch.old_path is None else contents.get(patch.old_path)
        if original is None:
            raise PatchError(f"source file does not exist: {patch.old_path}")
        output[patch.new_path] = apply_file_patch(original, patch)
    return output

