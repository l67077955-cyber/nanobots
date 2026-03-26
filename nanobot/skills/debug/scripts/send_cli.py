#!/usr/bin/env python3
"""Send a message to Telegram from scripts.

Usage:
  python3 send_cli.py --chat-id 123456 --text "Hello!"
  python3 send_cli.py --chat-id 123456 --text "$(date)" 

Reads bot token from ~/.nanobot/config.json automatically.
"""

import argparse
import json
import sys
import urllib.request
import urllib.error
from pathlib import Path


def _get_token() -> str:
    cfg_path = Path.home() / ".nanobot" / "config.json"
    if not cfg_path.exists():
        print("Error: ~/.nanobot/config.json not found", file=sys.stderr)
        sys.exit(1)
    cfg = json.loads(cfg_path.read_text())
    token = cfg.get("channels", {}).get("telegram", {}).get("token", "")
    if not token:
        print("Error: no telegram token in config", file=sys.stderr)
        sys.exit(1)
    return token


def send(token: str, chat_id: str, text: str) -> bool:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = json.dumps({"chat_id": chat_id, "text": text}).encode()
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status == 200
    except urllib.error.HTTPError as e:
        print(f"HTTP {e.code}: {e.read().decode()[:100]}", file=sys.stderr)
        return False
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return False


def main():
    parser = argparse.ArgumentParser(description="Send Telegram message")
    parser.add_argument("--chat-id", required=True, help="Telegram chat ID")
    parser.add_argument("--text", required=True, help="Message text")
    args = parser.parse_args()

    token = _get_token()
    ok = send(token, args.chat_id, args.text)
    if ok:
        print(f"Sent to {args.chat_id}")
    else:
        sys.exit(1)


if __name__ == "__main__":
    main()
