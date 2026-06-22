"""Read-only git snapshots for the code-watch dashboard."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path


def _run(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(err or f"git {' '.join(args)} failed")
    return proc.stdout


def repo_root(start: Path | None = None) -> Path:
    root = start or Path.cwd()
    if root.is_file():
        root = root.parent
    out = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=False,
    )
    if out.returncode != 0:
        raise RuntimeError("Not a git repository")
    return Path(out.stdout.strip())


def summary(repo: Path) -> dict:
    branch = _run(repo, "branch", "--show-current").strip() or "(detached)"
    head = _run(repo, "rev-parse", "--short", "HEAD").strip()
    subject = _run(repo, "log", "-1", "--pretty=%s").strip()
    author = _run(repo, "log", "-1", "--pretty=%an").strip()
    date = _run(repo, "log", "-1", "--pretty=%ci").strip()
    porcelain = _run(repo, "status", "--porcelain")
    files = [_parse_status_line(line) for line in porcelain.splitlines() if line.strip()]
    stat = _run(repo, "diff", "--stat").strip()
    shortstat = _run(repo, "diff", "--shortstat").strip()
    untracked = sum(1 for f in files if f["status"] == "untracked")
    return {
        "branch": branch,
        "head": head,
        "last_commit": {"subject": subject, "author": author, "date": date},
        "dirty_count": len(files),
        "untracked_count": untracked,
        "diff_stat": stat,
        "diff_shortstat": shortstat,
    }


def changed_files(repo: Path) -> list[dict]:
    porcelain = _run(repo, "status", "--porcelain")
    return [_parse_status_line(line) for line in porcelain.splitlines() if line.strip()]


def diff(repo: Path, path: str | None = None, staged: bool = False) -> str:
    args = ["diff"]
    if staged:
        args.append("--cached")
    if path:
        args.extend(["--", path])
    return _run(repo, *args)


def log(repo: Path, limit: int = 30) -> list[dict]:
    raw = _run(
        repo,
        "log",
        f"-{limit}",
        "--pretty=format:%H|%h|%an|%ci|%s",
    )
    entries = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        full, short, author, date, subject = line.split("|", 4)
        entries.append({
            "hash": full,
            "short": short,
            "author": author,
            "date": date,
            "subject": subject,
        })
    return entries


def show_commit(repo: Path, ref: str) -> dict:
    meta = _run(repo, "show", "--no-patch", "--pretty=format:%H|%h|%an|%ci|%s", ref)
    full, short, author, date, subject = meta.strip().split("|", 4)
    patch = _run(repo, "show", ref, "--pretty=format:", "--stat")
    return {
        "hash": full,
        "short": short,
        "author": author,
        "date": date,
        "subject": subject,
        "patch": patch,
    }


def snapshot(repo: Path) -> dict:
    return {
        "repo": str(repo),
        "summary": summary(repo),
        "files": changed_files(repo),
        "log": log(repo),
    }


def _parse_status_line(line: str) -> dict:
    xy = line[:2]
    path = line[3:]
    if " -> " in path:
        path = path.split(" -> ", 1)[1]
    status = _status_label(xy)
    return {"xy": xy, "status": status, "path": path}


def _status_label(xy: str) -> str:
    x, y = (xy + "  ")[:2]
    if x == "?" and y == "?":
        return "untracked"
    if x == "A":
        return "staged-new"
    if x == "M":
        return "staged-modified"
    if x == "D":
        return "staged-deleted"
    if x == "R":
        return "staged-renamed"
    if y == "M":
        return "modified"
    if y == "D":
        return "deleted"
    if y == "?":
        return "untracked"
    if y == "A":
        return "added"
    return "changed"