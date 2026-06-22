#!/usr/bin/env python3
"""Legacy standalone entry — dashboard is now served by `nanobot gateway`."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Git change dashboard (legacy — prefer `nanobot gateway --foreground`)",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--repo", type=Path, default=None)
    parser.add_argument("--refresh", type=int, default=5)
    parser.add_argument("--password", default=None)
    parser.add_argument("--token", default=None)
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parents[2]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    from nanobot.runtime.dashboard import DashboardServer, resolve_repo

    print("note: chat requires `nanobot gateway` — this mode is git/architecture only")
    repo = resolve_repo(args.repo)
    server = DashboardServer(
        host=args.host,
        port=args.port,
        repo=repo,
        chat_hub=None,
        password=args.password,
        token=args.token,
        refresh_s=args.refresh,
    )
    url = server.start()
    print(f"code-watch (standalone): {url}")
    print(f"repo: {repo}")
    try:
        import time

        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        print("\ncode-watch: stopped")
        server.stop()


if __name__ == "__main__":
    main()