import calendar
import html
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

import feedparser
import requests

ROOT = Path(__file__).parent
CONFIG_PATH = ROOT / "config.json"
STATE_PATH = ROOT / "state.json"


def load_json(path, fallback):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return fallback


def clean_text(value):
    value = re.sub(r"<[^>]+>", " ", value or "")
    value = html.unescape(value)
    return re.sub(r"\s+", " ", value).strip()


def entry_timestamp(entry):
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        parsed = entry.get(key)
        if parsed:
            try:
                return datetime.fromtimestamp(calendar.timegm(parsed), tz=timezone.utc)
            except Exception:
                pass
    return None


def canonical_url(url):
    # Remove common tracking parameters while keeping the article path.
    from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
    parts = urlsplit(url or "")
    kept = [(k, v) for k, v in parse_qsl(parts.query) if not k.lower().startswith("utm_")
            and k.lower() not in {"fbclid", "gclid", "mc_cid", "mc_eid"}]
    return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), urlencode(kept), ""))


def is_relevant(title, summary, config):
    text = (title + " " + summary).lower()
    if any(word.lower() in text for word in config.get("exclude_keywords", [])):
        return False
    keywords = config.get("relevance_keywords", [])
    return not keywords or any(word.lower() in text for word in keywords)


def fetch_items(config, state):
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=int(config.get("lookback_hours", 36)))
    seen = set(state.get("seen", []))
    collected = []

    for source in config.get("sources", []):
        if not source.get("enabled", True):
            continue
        name, url = source.get("name", "Источник"), source.get("url", "")
        try:
            parsed = feedparser.parse(
                url,
                request_headers={"User-Agent": "SignalPluginsDigest/1.0 (+RSS reader)"}
            )
            if parsed.bozo and not parsed.entries:
                print(f"[WARN] Не удалось прочитать {name}: {parsed.bozo_exception}")
                continue
            for entry in parsed.entries:
                title = clean_text(entry.get("title", ""))
                link = canonical_url(entry.get("link", ""))
                summary = clean_text(entry.get("summary", "") or entry.get("description", ""))
                if not title or not link or len(title) < int(config.get("min_title_length", 8)):
                    continue
                published = entry_timestamp(entry)
                # If feed omits a date, keep it only if unseen; do not invent a date.
                if published and published < cutoff:
                    continue
                if not is_relevant(title, summary, config):
                    continue
                key = link.lower()
                if key in seen:
                    continue
                collected.append({
                    "source": name,
                    "title": title,
                    "link": link,
                    "summary": summary[:500],
                    "published": published,
                    "key": key
                })
        except Exception as exc:
            print(f"[WARN] Ошибка источника {name}: {exc}")

    collected.sort(key=lambda x: x["published"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return collected[:int(config.get("max_items", 8))]


def format_post(items, config):
    date_label = datetime.now().strftime("%d.%m.%Y")
    lines = [f"🎛 <b>{html.escape(config.get('channel_title', 'SIGNAL / PLUGINS'))}</b>",
             f"<i>Дайджест · {date_label}</i>", ""]
    for i, item in enumerate(items, 1):
        title = html.escape(item["title"])
        source = html.escape(item["source"])
        link = html.escape(item["link"], quote=True)
        lines.append(f"<b>{i}. <a href=\"{link}\">{title}</a></b>")
        lines.append(f"<i>{source}</i>")
        if item["summary"]:
            summary = item["summary"]
            # Keep Telegram post readable; no invented translation or claims.
            if len(summary) > 260:
                summary = summary[:257].rsplit(" ", 1)[0] + "…"
            lines.append(html.escape(summary))
        lines.append("")
    lines.append("🔗 Ссылки ведут на первоисточники.")
    return "\n".join(lines).strip()


def send_telegram(text):
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        raise RuntimeError("Не заданы TELEGRAM_BOT_TOKEN и/или TELEGRAM_CHAT_ID")
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    response = requests.post(url, json={
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }, timeout=30)
    response.raise_for_status()
    payload = response.json()
    if not payload.get("ok"):
        raise RuntimeError(f"Telegram API вернул ошибку: {payload}")


def main():
    config = load_json(CONFIG_PATH, {})
    state = load_json(STATE_PATH, {"seen": []})
    items = fetch_items(config, state)

    if not items:
        print("Новых подходящих публикаций нет.")
        if config.get("post_when_empty", False):
            send_telegram("🎛 <b>SIGNAL / PLUGINS</b>\nСегодня новых подходящих публикаций не найдено.")
        return

    post = format_post(items, config)
    print(post)
    send_telegram(post)

    seen = set(state.get("seen", []))
    seen.update(item["key"] for item in items)
    # Keep the state file compact.
    state["seen"] = sorted(seen)[-3000:]
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Опубликовано новостей: {len(items)}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        raise
