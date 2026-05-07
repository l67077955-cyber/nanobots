#!/usr/bin/env python3
"""Send a photo to Telegram from scripts."""
import argparse, json, sys, urllib.request, urllib.error, mimetypes, uuid
from pathlib import Path

def _get_token():
    cfg = json.loads(Path.home().joinpath(".nanobot", "config.json").read_text())
    token = cfg.get("channels", {}).get("telegram", {}).get("token", "")
    if not token:
        print("Error: no telegram token", file=sys.stderr); sys.exit(1)
    return token

def send_photo(token, chat_id, image_path, caption=""):
    url = f"https://api.telegram.org/bot{token}/sendPhoto"
    img = Path(image_path)
    if not img.exists():
        print(f"Error: {image_path} not found", file=sys.stderr); return False
    boundary = uuid.uuid4().hex
    mime = mimetypes.guess_type(str(img))[0] or "image/png"
    data = img.read_bytes()
    parts = []
    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="chat_id"\r\n\r\n{chat_id}'.encode())
    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="photo"; filename="{img.name}"\r\nContent-Type: {mime}\r\n\r\n'.encode() + data)
    if caption:
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="caption"\r\n\r\n{caption}'.encode())
    parts.append(f'--{boundary}--\r\n'.encode())
    body = b"\r\n".join(parts)
    req = urllib.request.Request(url, data=body, headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            r = json.loads(resp.read().decode())
            if r.get("ok"):
                print(f"OK: sent {len(data)} bytes to {chat_id}"); return True
            print(f"API error: {r}", file=sys.stderr); return False
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr); return False

if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--chat-id", required=True)
    p.add_argument("--image-path", required=True)
    p.add_argument("--caption", default="")
    a = p.parse_args()
    sys.exit(0 if send_photo(_get_token(), a.chat_id, a.image_path, a.caption) else 1)
