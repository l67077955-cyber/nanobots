"""File system tools: read, write, edit, list."""

import base64
import difflib
import hashlib
import mimetypes
import os
import re
from pathlib import Path
from typing import Any

from nanobot.tools.base import Tool
from nanobot.utils.helpers import detect_image_mime


# ---------------------------------------------------------------------------
# Strictly only the core "read unchanged since last read + force + write invalidation" feature.
# No PDF, no office docs, no device blacklist, no limit/MAX changes, no extra params like pages.
# All in this file only. Local code wins on any conflict.
# ---------------------------------------------------------------------------


_file_read_states: dict[str, dict] = {}  # minimal dict-based, no dataclass


def _hash_file(p: str | Path) -> str | None:
    try:
        return hashlib.sha256(Path(p).read_bytes()).hexdigest()
    except OSError:
        return None


def _record_read(path: str | Path, offset: int = 1, limit: int | None = None) -> None:
    p = str(Path(path).resolve())
    try:
        mtime = os.path.getmtime(p)
    except OSError:
        return
    _file_read_states[p] = {
        "mtime": mtime,
        "offset": offset,
        "limit": limit,
        "hash": _hash_file(p),
    }


def _invalidate_read_cache(path: str | Path) -> None:
    """On write: drop cache so the next read_file returns actual (new) content."""
    p = str(Path(path).resolve())
    _file_read_states.pop(p, None)


def _is_unchanged_since_last_read(path: str | Path, offset: int = 1, limit: int | None = None) -> bool:
    p = str(Path(path).resolve())
    entry = _file_read_states.get(p)
    if entry is None:
        return False
    if entry["offset"] != offset or entry.get("limit") != limit:
        return False
    try:
        cur_mtime = os.path.getmtime(p)
    except OSError:
        return False
    if cur_mtime != entry["mtime"]:
        cur_h = _hash_file(p)
        if cur_h and cur_h != entry.get("hash"):
            return False
        entry["mtime"] = cur_mtime
        return True
    return True


def _resolve_builtin_skill_path(path: str) -> Path | None:
    """Map ``skills/<name>/...`` to bundled skill files when absent from workspace."""
    norm = path.replace("\\", "/").lstrip("./")
    if not norm.startswith("skills/"):
        return None
    from nanobot.skills.loader import BUILTIN_SKILLS_DIR

    candidate = (BUILTIN_SKILLS_DIR / norm.removeprefix("skills/")).resolve()
    if candidate.exists():
        return candidate
    return None


def _resolve_path(
    path: str,
    workspace: Path | None = None,
    allowed_dir: Path | None = None,
    extra_allowed_dirs: list[Path] | None = None,
) -> Path:
    """Resolve path against workspace (if relative) and enforce directory restriction."""
    p = Path(path).expanduser()
    if not p.is_absolute() and workspace:
        p = workspace / p
    resolved = p.resolve()
    if not resolved.exists():
        builtin = _resolve_builtin_skill_path(path)
        if builtin is not None:
            resolved = builtin
    if allowed_dir:
        all_dirs = [allowed_dir] + (extra_allowed_dirs or [])
        if not any(_is_under(resolved, d) for d in all_dirs):
            raise PermissionError(f"Path {path} is outside allowed directory {allowed_dir}")
    return resolved


def _is_under(path: Path, directory: Path) -> bool:
    try:
        path.relative_to(directory.resolve())
        return True
    except ValueError:
        return False


class _FsTool(Tool):
    """Shared base for filesystem tools — common init and path resolution."""

    def __init__(
        self,
        workspace: Path | None = None,
        allowed_dir: Path | None = None,
        extra_allowed_dirs: list[Path] | None = None,
    ):
        self._workspace = workspace
        self._allowed_dir = allowed_dir
        self._extra_allowed_dirs = extra_allowed_dirs

    def _resolve(self, path: str) -> Path:
        return _resolve_path(path, self._workspace, self._allowed_dir, self._extra_allowed_dirs)


# ---------------------------------------------------------------------------
# read_file
# ---------------------------------------------------------------------------

class ReadFileTool(_FsTool):
    """Read file contents with optional line-based pagination."""

    _MAX_CHARS = 64_000
    _DEFAULT_LIMIT = 300

    @property
    def name(self) -> str:
        return "read_file"

    @property
    def description(self) -> str:
        return (
            "Read the contents of a file. Returns numbered lines (format: 'N| actual content'). "
            "The 'N| ' prefixes are for reference only. "
            "Use offset and limit to paginate through large files. "
            "When editing later with edit_file, copy ONLY the raw content after the 'N| ' prefix — do not include line numbers or the pipe in old_text."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "The file path to read"},
                "offset": {
                    "type": "integer",
                    "description": "Line number to start reading from (1-indexed, default 1)",
                    "minimum": 1,
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of lines to read (default 300)",
                    "minimum": 1,
                },
                "force": {
                    "type": "boolean",
                    "description": "Bypass unchanged-since-last-read dedup and force a fresh read.",
                    "default": False,
                },
            },
            "required": ["path"],
        }

    async def execute(self, path: str, offset: int = 1, limit: int | None = None, force: bool = False, **kwargs: Any) -> Any:
        try:
            fp = self._resolve(path)
            if not fp.exists():
                return f"Error: File not found: {path}"
            if not fp.is_file():
                return f"Error: Not a file: {path}"

            # Core feature only: dedup check (local descriptions / logic win on conflicts)
            if not force:
                if _is_unchanged_since_last_read(fp, offset=offset, limit=limit):
                    return f"[File unchanged since last read: {path}]"

            raw = fp.read_bytes()
            if not raw:
                _record_read(fp, offset=offset, limit=limit)
                return f"(Empty file: {path})"

            mime = detect_image_mime(raw) or mimetypes.guess_type(path)[0]
            if mime and mime.startswith("image/"):
                b64 = base64.b64encode(raw).decode()
                _record_read(fp, offset=offset, limit=limit)
                return [
                    {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}, "_meta": {"path": str(fp)}},
                    {"type": "text", "text": f"(Image file: {path})"}
                ]

            try:
                text_content = raw.decode("utf-8")
            except UnicodeDecodeError:
                return f"Error: Cannot read binary file {path} (MIME: {mime or 'unknown'}). Only UTF-8 text and images are supported."

            all_lines = text_content.splitlines()
            total = len(all_lines)

            if offset < 1:
                offset = 1
            if offset > total:
                return f"Error: offset {offset} is beyond end of file ({total} lines)"

            start = offset - 1
            end = min(start + (limit or self._DEFAULT_LIMIT), total)
            numbered = [f"{start + i + 1}| {line}" for i, line in enumerate(all_lines[start:end])]
            result = "\n".join(numbered)

            if len(result) > self._MAX_CHARS:
                trimmed, chars = [], 0
                for line in numbered:
                    chars += len(line) + 1
                    if chars > self._MAX_CHARS:
                        break
                    trimmed.append(line)
                end = start + len(trimmed)
                result = "\n".join(trimmed)

            if end < total:
                result += f"\n\n(Showing lines {offset}-{end} of {total}. Use offset={end + 1} to continue.)"
            else:
                result += f"\n\n(End of file — {total} lines total)"

            _record_read(fp, offset=offset, limit=limit)

            # Keep the existing local reminder exactly as-is (priority to local)
            if offset == 1:
                result += "\n[Reminder: line prefixes 'N| ' are reference only. Omit them when using edit_file old_text.]"
            return result
        except PermissionError as e:
            return f"Error: {e}"
        except Exception as e:
            return f"Error reading file: {e}"


# ---------------------------------------------------------------------------
# write_file
# ---------------------------------------------------------------------------

class WriteFileTool(_FsTool):
    """Write content to a file."""

    @property
    def name(self) -> str:
        return "write_file"

    @property
    def description(self) -> str:
        return (
            "Write (overwrite) content to a file at the given path. Creates parent directories if needed. "
            "For precise edits on existing files, prefer edit_file (or read_file then targeted edit_file) over write_file — write_file replaces the ENTIRE file content. "
            "Always provide both 'path' and 'content'."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "The file path to write to"},
                "content": {"type": "string", "description": "The content to write"},
            },
            "required": ["path", "content"],
            "additionalProperties": False,
        }

    async def execute(self, path: str, content: str, **kwargs: Any) -> str:
        try:
            fp = self._resolve(path)
            fp.parent.mkdir(parents=True, exist_ok=True)
            fp.write_text(content, encoding="utf-8")
            _invalidate_read_cache(fp)  # only for the dedup feature: next read will see fresh content
            return f"Successfully wrote {len(content)} bytes to {fp}"
        except PermissionError as e:
            return f"Error: {e}"
        except Exception as e:
            return f"Error writing file: {e}"


# ---------------------------------------------------------------------------
# edit_file
# ---------------------------------------------------------------------------

def _find_match(content: str, old_text: str) -> tuple[str | None, int]:
    """Locate old_text in content: exact first, then line-trimmed sliding window.

    Both inputs should use LF line endings (caller normalises CRLF).
    Returns (matched_fragment, count) or (None, 0).
    """
    if old_text in content:
        return old_text, content.count(old_text)

    old_lines = old_text.splitlines()
    if not old_lines:
        return None, 0
    stripped_old = [l.strip() for l in old_lines]
    content_lines = content.splitlines()

    candidates = []
    for i in range(len(content_lines) - len(stripped_old) + 1):
        window = content_lines[i : i + len(stripped_old)]
        if [l.strip() for l in window] == stripped_old:
            candidates.append("\n".join(window))

    if candidates:
        return candidates[0], len(candidates)
    return None, 0


def _strip_read_file_prefixes(text: str) -> str:
    """Remove common 'N| ' or 'N: ' line-number prefixes produced by ReadFileTool.

    This allows models to copy-paste directly from read_file output into edit_file
    old_text without manual cleanup, enabling precise positioning.
    ReadFileTool always emits exactly 'N| ' (number, pipe, single space) + original_line.
    We remove only the added annotation, preserving the original line's leading whitespace.
    """
    if not text:
        return text
    # Per-line: remove a leading optional-ws + digits + optional-ws + [|:]+ optional-ws
    # Use a regex that targets the *annotation* added by the reader.
    # This is safe because real source rarely starts a line with "<number>| " or "<number>: " at column 0 after indent.
    pattern = re.compile(r'^(\s*)(\d+)(\s*[\|:]+\s?)(.*)$', re.DOTALL)
    lines = text.splitlines(keepends=True)
    cleaned = []
    for line in lines:
        if "|" in line or ":" in line[:12]:
            m = pattern.match(line)
            if m:
                _lead, _num, _sep, rest = m.groups()
                # rest already contains the original line's exact content (including its indent ws)
                cleaned.append(rest if rest is not None else line)
                continue
        cleaned.append(line)
    return "".join(cleaned)


class EditFileTool(_FsTool):
    """Edit a file by replacing text with fallback matching."""

    @property
    def name(self) -> str:
        return "edit_file"

    @property
    def description(self) -> str:
        return (
            "Edit a file by replacing old_text with new_text. "
            "Supports minor whitespace/line-ending differences via automatic matching. "
            "old_text can be copied directly from a recent read_file result (the 'N| ' line prefixes are automatically stripped for convenience, enabling precise positioning at the exact location you just read). "
            "Provide enough unique surrounding context in old_text if the snippet appears multiple times. "
            "Set replace_all=true to replace every occurrence."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "The file path to edit"},
                "old_text": {"type": "string", "description": "The text to find and replace"},
                "new_text": {"type": "string", "description": "The text to replace with"},
                "replace_all": {
                    "type": "boolean",
                    "description": "Replace all occurrences (default false)",
                },
            },
            "required": ["path", "old_text", "new_text"],
        }

    async def execute(
        self, path: str, old_text: str, new_text: str,
        replace_all: bool = False, **kwargs: Any,
    ) -> str:
        try:
            fp = self._resolve(path)
            if not fp.exists():
                return f"Error: File not found: {path}"

            raw = fp.read_bytes()
            uses_crlf = b"\r\n" in raw
            content = raw.decode("utf-8").replace("\r\n", "\n")
            # Auto-strip read_file "N| " / "N: " prefixes so copy-paste from read_file works directly.
            cleaned_old = _strip_read_file_prefixes(old_text.replace("\r\n", "\n"))
            if cleaned_old == "":
                return f"Error: old_text is empty after stripping (line numbers etc.). Cannot use empty old_text for edit; provide context text or use write_file for full overwrite / new files."

            match, count = _find_match(content, cleaned_old)

            if match is None:
                return self._not_found_msg(cleaned_old, content, path)
            if count > 1 and not replace_all:
                return (
                    f"Warning: old_text appears {count} times. "
                    "Provide more context to make it unique, or set replace_all=true."
                )

            norm_new = _strip_read_file_prefixes(new_text.replace("\r\n", "\n"))
            new_content = content.replace(match, norm_new) if replace_all else content.replace(match, norm_new, 1)
            if uses_crlf:
                new_content = new_content.replace("\n", "\r\n")

            fp.write_bytes(new_content.encode("utf-8"))
            _invalidate_read_cache(fp)  # keep dedup cache correct: edits are modifications, so next read should be fresh
            return f"Successfully edited {fp}"
        except PermissionError as e:
            return f"Error: {e}"
        except Exception as e:
            return f"Error editing file: {e}"

    @staticmethod
    def _not_found_msg(old_text: str, content: str, path: str) -> str:
        lines = content.splitlines(keepends=True)
        old_lines = old_text.splitlines(keepends=True)
        window = len(old_lines)

        best_ratio, best_start = 0.0, 0
        for i in range(max(1, len(lines) - window + 1)):
            ratio = difflib.SequenceMatcher(None, old_lines, lines[i : i + window]).ratio()
            if ratio > best_ratio:
                best_ratio, best_start = ratio, i

        if best_ratio > 0.5:
            diff = "\n".join(difflib.unified_diff(
                old_lines, lines[best_start : best_start + window],
                fromfile="old_text (provided)",
                tofile=f"{path} (actual, line {best_start + 1})",
                lineterm="",
            ))
            return f"Error: old_text not found in {path}.\nBest match ({best_ratio:.0%} similar) at line {best_start + 1}:\n{diff}"
        return f"Error: old_text not found in {path}. No similar text found. Verify the file content."


# ---------------------------------------------------------------------------
# list_dir
# ---------------------------------------------------------------------------

class ListDirTool(_FsTool):
    """List directory contents with optional recursion."""

    _DEFAULT_MAX = 200
    _IGNORE_DIRS = {
        ".git", "node_modules", "__pycache__", ".venv", "venv",
        "dist", "build", ".tox", ".mypy_cache", ".pytest_cache",
        ".ruff_cache", ".coverage", "htmlcov",
    }

    @property
    def name(self) -> str:
        return "list_dir"

    @property
    def description(self) -> str:
        return (
            "List the contents of a directory. "
            "Set recursive=true to explore nested structure. "
            "Common noise directories (.git, node_modules, __pycache__, etc.) are auto-ignored."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "The directory path to list"},
                "recursive": {
                    "type": "boolean",
                    "description": "Recursively list all files (default false)",
                },
                "max_entries": {
                    "type": "integer",
                    "description": "Maximum entries to return (default 200)",
                    "minimum": 1,
                },
            },
            "required": ["path"],
        }

    async def execute(
        self, path: str, recursive: bool = False,
        max_entries: int | None = None, **kwargs: Any,
    ) -> str:
        try:
            dp = self._resolve(path)
            if not dp.exists():
                return f"Error: Directory not found: {path}"
            if not dp.is_dir():
                return f"Error: Not a directory: {path}"

            cap = max_entries or self._DEFAULT_MAX
            items: list[str] = []
            total = 0

            if recursive:
                for item in sorted(dp.rglob("*")):
                    if any(p in self._IGNORE_DIRS for p in item.parts):
                        continue
                    total += 1
                    if len(items) < cap:
                        rel = item.relative_to(dp)
                        items.append(f"{rel}/" if item.is_dir() else str(rel))
            else:
                for item in sorted(dp.iterdir()):
                    if item.name in self._IGNORE_DIRS:
                        continue
                    total += 1
                    if len(items) < cap:
                        pfx = "📁 " if item.is_dir() else "📄 "
                        items.append(f"{pfx}{item.name}")

            if not items and total == 0:
                return f"Directory {path} is empty"

            result = "\n".join(items)
            if total > cap:
                result += f"\n\n(truncated, showing first {cap} of {total} entries)"
            return result
        except PermissionError as e:
            return f"Error: {e}"
        except Exception as e:
            return f"Error listing directory: {e}"
