import json
import os
import urllib.request

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")


def send_message(chat_id: str, text: str, parse_mode: str = "Markdown") -> dict:
    """Отправляет сообщение в Telegram через Bot API."""
    if not BOT_TOKEN:
        return {"ok": False, "error": "TELEGRAM_BOT_TOKEN not set"}

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    body = json.dumps({
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode,
    }).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
    )

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except urllib.request.URLError as e:
        return {"ok": False, "error": str(e)}
