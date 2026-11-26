import os
import re
import json
import html
import hashlib
import logging
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlparse

import feedparser
import requests

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

FEEDS = [
    ("KVR Audio", "https://www.kvraudio.com/news-feed.php"),
    ("Bedroom Producers Blog", "https://bedroomproducersblog.com/feed/"),
    ("rekkerd.org", "https://rekkerd.org/feed/"),
]

STATE_FILE = Path("state.json")
MAX_ITEMS = int(os.getenv("MAX_ITEMS", "8"))
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

# Keywords are deliberately broad for the first version; edit these to tune the feed.
KEYWORDS = (
    "plugin", "plug-in", "vst", "vst3", "audio", "synth", "synthesizer",
    "effect", "effects", "sound design", "sampler", " Kontakt", "ableton",
    "reaper", "free", " freeware", "update", "release", "daw", "midi",
    "granular", "modular", "fx", "instrument", "compressor", "reverb",
    "delay", "distortion", "filter", "sequencer"
)

def load_state():
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return set(data.get("seen", []))
    except (OSError, json.JSONDecodeError):
        return set()

def save_state(seen):
    # Keep a bounded history so the state file does not grow forever.
    STATE_FILE.write_text(
        json.dumps({"seen": list(seen)[-3000:]}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8"
    )

def clean_text(value):
    value = re.sub(r"<[^>]+>", " ", value or "")
    value = html.unescape(value)
    return re.sub(r"\s+", " ", value).strip()

def entry_date(entry):
    for key in ("published", "updated", "created"):
        raw = entry.get(key)
        if raw:
            try:
                dt = parsedate_to_datetime(raw)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt.astimezone(timezone.utc)
            except Exception:
                pass
    return datetime.now(timezone.utc)

def stable_id(url, title):
    raw = (url or title).strip().lower()
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()

def is_relevant(title, summary):
    text = f"{title} {summary}".lower()
    return any(word.strip().lower() in text for word in KEYWORDS)

def fetch_items(seen):
    found = []
    headers = {"User-Agent": "SignalPluginsDigest/1.0 (+RSS reader)"}
    for source, feed_url in FEEDS:
        try:
            response = requests.get(feed_url, headers=headers, timeout=25)
            response.raise_for_status()
            feed = feedparser.parse(response.content)
            if feed.bozo and not feed.entries:
                logging.warning("Не удалось разобрать RSS: %s (%s)", source, feed_url)
                continue
            logging.info("%s: получено записей: %d", source, len(feed.entries))
            for entry in feed.entries:
                title = clean_text(entry.get("title", "Без названия"))
                url = entry.get("link", "").strip()
                summary = clean_text(entry.get("summary", entry.get("description", "")))
                if not url or not is_relevant(title, summary):
                    continue
                item_id = stable_id(url, title)
                if item_id in seen:
                    continue
                found.append({
                    "id": item_id,
                    "source": source,
                    "title": title,
                    "url": url,
                    "summary": summary[:450],
                    "date": entry_date(entry),
                })
        except Exception as exc:
            logging.warning("Ошибка RSS %s: %s", source, exc)

    # Newest first, then de-duplicate by URL-derived ID.
    unique = {item["id"]: item for item in found}
    return sorted(unique.values(), key=lambda x: x["date"], reverse=True)

def telegram_message(items):
    today = datetime.now().strftime("%d.%m.%Y")
    lines = [f"<b>SIGNAL / PLUGINS</b> · {today}", ""]
    for i, item in enumerate(items, 1):
        title = html.escape(item["title"])
        source = html.escape(item["source"])
        url = html.escape(item["url"], quote=True)
        summary = html.escape(item["summary"])
        lines.append(f"<b>{i}. <a href=\"{url}\">{title}</a></b>")
        lines.append(f"<i>{source}</i>")
        if summary:
            lines.append(summary[:350])
        lines.append("")
    lines.append("<i>Автоматический RSS-дайджест. Проверяйте детали на сайте источника.</i>")
    return "\n".join(lines)

def send_telegram(text):
    if not BOT_TOKEN or not CHAT_ID:
        raise RuntimeError("Не заданы TELEGRAM_BOT_TOKEN и/или TELEGRAM_CHAT_ID в GitHub Secrets")
    api = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    # Telegram message limit is 4096 characters; split conservatively if necessary.
    chunks = []
    while len(text) > 3900:
        cut = text.rfind("\n\n", 0, 3900)
        if cut < 1000:
            cut = 3900
        chunks.append(text[:cut])
        text = text[cut:].lstrip()
    if text:
        chunks.append(text)
    for chunk in chunks:
        response = requests.post(api, json={
            "chat_id": CHAT_ID,
            "text": chunk,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }, timeout=25)
        response.raise_for_status()
        payload = response.json()
        if not payload.get("ok"):
            raise RuntimeError(f"Telegram API error: {payload}")

def main():
    seen = load_state()
    items = fetch_items(seen)
    if not items:
        logging.info("Новых подходящих новостей нет. Публикация пропущена.")
        return

    selected = items[:MAX_ITEMS]
    message = telegram_message(selected)
    send_telegram(message)

    for item in selected:
        seen.add(item["id"])
    save_state(seen)
    logging.info("Опубликовано новостей: %d", len(selected))

if __name__ == "__main__":
    main()
