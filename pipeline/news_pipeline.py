#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
«Оглавление телеграм-канала» · новостной конвейер
=================================
GitHub Actions запускает скрипт по расписанию (4 раза в сутки):

    RSS-источники → отбор новых → анонс в Telegram-канал (Bot API)
    → запись в docs/posts.json → коммит → Pages обновляет мини-апп.

Режимы:
    --mode dry      показать, что БЫЛО бы опубликовано (без отправки и записи)
    --mode publish  полный цикл: публикация в канал + обновление posts.json

Секреты (только для publish, задаются в GitHub → Settings → Secrets):
    BOT_TOKEN        токен бота от @BotFather
    CHANNEL_USERNAME юзернейм канала вида @my_channel (канал публичный!)

Копирайт: в канал уходит КОРОТКИЙ анонс (1–2 предложения из RSS) со
 ссылкой на источник — стандартная практика новостных дайджестов.
"""
import argparse
import hashlib
import html as html_mod
import json
import os
import re
import sys
import urllib.request
import urllib.parse
from datetime import datetime, timedelta, timezone

MSK = timezone(timedelta(hours=3))

# Тема по ключевым словам (для второй метки в оглавлении)
KEYWORD_TAGS = [
    ("семья",   ["семья", "семь", "дет", "мама", "папа", "родител", "школ"]),
    ("медицина",["врач", "лечен", "операц", "болниц", " терап", "реабилит", "диагноз"]),
    ("наука",   ["учен", "наук", "исследован", "открыт", "изучен", "космос"]),
    ("помощь",  ["помог", "поддерж", "собрал", "донор", "волонтер", "благотвор", "спас"]),
    ("общество",["общест", "город", "жител", "акци", "проект", "инициатив"]),
]

def log(msg):
    print(msg, flush=True)

def http_json(url, payload=None, headers=None, timeout=30):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET",
                                 headers=headers or {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))

def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default

def save_json(path, obj):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)

def clean_html(s):
    s = re.sub(r"<[^>]+>", " ", s or "")
    s = html_mod.unescape(s)
    return re.sub(r"\s+", " ", s).strip()

def trim(s, limit, dots="…"):
    s = (s or "").strip()
    if len(s) <= limit:
        return s
    cut = s[:limit - 1]
    cut = cut[:cut.rfind(" ")] if " " in cut[-15:] else cut
    return cut.rstrip(",;:- ") + dots

def keyword_tags(text):
    low = " " + (text or "").lower() + " "
    for tag, keys in KEYWORD_TAGS:
        if any(k in low for k in keys):
            return "#" + tag
    return "#общество"

def fetch_feed(source):
    """Загружает и разбирает один RSS/Atom-поток штатными средствами Python
    (без внешних зависимостей — в Actions не нужен pip install).
    Ошибка одного источника не роняет запуск."""
    import xml.etree.ElementTree as ET
    try:
        req = urllib.request.Request(source["url"], headers={
            "User-Agent": "Mozilla/5.0 (compatible; Soderzhanie/1.0)",
            "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
        })
        with urllib.request.urlopen(req, timeout=25) as r:
            raw = r.read()
        root = ET.fromstring(raw)
        # RSS 2.0: <rss><channel><item>… | Atom: <feed><entry>…
        items = root.findall(".//item") or root.findall(".//{*}entry")
        entries = [normalize_entry(it) for it in items]
        entries = [e for e in entries if e]
        log(f"  · {source.get('name','?')}: {len(entries)} записей")
        return entries
    except Exception as e:
        log(f"  · {source.get('name','?')}: ОШИБКА ({e}) — пропускаю")
        return []


def _txt(el):
    if el is None:
        return ""
    return clean_html("".join(el.itertext()))


def _when(el):
    """pubDate/updated → struct_time; поддержка RSS (RFC822) и Atom (ISO)."""
    from email.utils import parsedate_to_datetime
    if el is None or not (el.text or "").strip():
        return None
    s = el.text.strip()
    try:
        return parsedate_to_datetime(s).utctimetuple()          # RFC822 (RSS)
    except Exception:
        pass
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).utctimetuple()  # Atom/ISO
    except Exception:
        return None


def normalize_entry(item):
    """RSS <item> или Atom <entry> → единый словарь записи."""
    def first(*paths):
        for p in paths:
            found = item.find(p)
            if found is None:
                found = item.find("{*}" + p.split("}")[-1])   # namespace-агностично
            if found is not None:
                return found
        return None

    title_el = first("title")
    if title_el is None or _txt(title_el) == "":
        return None

    # ссылка: RSS <link>текст</link> | Atom <link href="…" />
    link = ""
    link_el = first("link")
    if link_el is not None:
        link = (link_el.text or "").strip() or (link_el.get("href") or "").strip()
    if not link:
        return None

    # описание: первый НЕПУСТОЙ из возможных тегов (у многих лент пустой description,
    # а настоящий текст лежит в content:encoded / summary)
    summary = ""
    for tag_path in ("description", "summary", "content", "encoded"):
        el = first(tag_path)
        if el is not None:
            cand = _txt(el)
            if cand:
                summary = cand
                break
    when = _when(first("pubDate", "published", "updated", "date"))
    guid_el = first("guid", "id")
    return {
        "title": _txt(title_el),
        "link": link,
        "summary": summary,
        "id": ((guid_el.text if guid_el is not None else "") or link),
        "published_parsed": when,
    }

def pick(entries, source, seen, existing_srcs, max_items):
    """Отбирает самые свежие ещё не публиковавшиеся записи одного источника."""
    out = []
    for e in entries:
        link = e.get("link") or ""
        guid = e.get("id") or link
        if not link:
            continue
        h = hashlib.sha1(guid.encode("utf-8")).hexdigest()
        if h in seen or link in existing_srcs:
            continue
        title = clean_html(e.get("title") or "")
        summary = clean_html(e.get("summary", ""))
        if not title:
            continue
        when = e.get("published_parsed") or e.get("updated_parsed")
        dt = (datetime(*when[:6], tzinfo=timezone.utc).astimezone(MSK)
              if when else datetime.now(MSK))
        out.append({"src": link, "hash": h, "title": title, "summary": summary,
                    "dt": dt, "source": source})
        if len(out) >= max_items:
            break
    return out

def compose(item, add_tags=True):
    s = item["source"]
    emoji = s.get("emoji", "📰")
    tags = list(s.get("tags", ["#добрыеновости"]))
    extra = keyword_tags(item["title"] + " " + item["summary"])
    if extra not in tags:
        tags.append(extra)
    title = trim(item["title"], 110)
    summary = trim(item["summary"], 280)
    lines = [f"{emoji} <b>{html_mod.escape(title)}</b>"]
    if summary and summary.lower() != title.lower():
        lines += ["", html_mod.escape(summary)]
    lines += ["", f"🔗 <a href=\"{html_mod.escape(item['src'])}\">{html_mod.escape(s.get('name','источник'))}</a>"]
    if add_tags:
        lines.append(" ".join(tags))
    text = "\n".join(lines)
    if len(text) > 4096:
        text = trim(text, 4090, "…")
    return text, tags

def tg_send(token, chat, text, link_preview=False):
    api = f"https://api.telegram.org/bot{token}/sendMessage"
    return http_json(api, {
        "chat_id": chat, "text": text, "parse_mode": "HTML",
        "disable_web_page_preview": not link_preview,
    })

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["dry", "publish"], default="dry")
    ap.add_argument("--max", type=int, default=3, help="максимум новостей за запуск (на ВСЕ источники)")
    ap.add_argument("--max-per-source", type=int, default=2)
    ap.add_argument("--sources", default="pipeline/sources.json")
    ap.add_argument("--posts", default="docs/posts.json")
    ap.add_argument("--seen", default="pipeline/seen.json")
    ap.add_argument("--link-preview", action="store_true", help="показывать превью ссылки в посте")
    args = ap.parse_args()

    cfg = load_json(args.sources, {"sources": []})
    sources = cfg.get("sources") or []
    if not sources:
        log("!! sources.json не содержит источников — делать нечего")
        return 1

    posts_doc = load_json(args.posts, {"version": 1, "updated_at": "", "channel": {}, "posts": []})
    posts = posts_doc.get("posts") or []
    existing_srcs = {p.get("src") for p in posts if p.get("src")}

    seen_doc = load_json(args.seen, {"seen": {}})
    seen = seen_doc.get("seen") or {}

    log(f"Источников: {len(sources)}; в оглавлении уже {len(posts)} постов")
    candidates = []
    for src in sources:
        candidates += pick(fetch_feed(src), src, seen, existing_srcs, args.max_per_source)
    candidates.sort(key=lambda x: x["dt"], reverse=True)
    candidates = candidates[: args.max]
    log(f"К публикации отобрано: {len(candidates)}")
    if not candidates:
        log("Новых новостей нет — posts.json не меняется")
        return 0

    token = os.environ.get("BOT_TOKEN", "")
    chat = (os.environ.get("CHANNEL_USERNAME", "") or "").strip()
    if args.mode == "publish" and (not token or not chat):
        log("!! publish требует BOT_TOKEN и CHANNEL_USERNAME (GitHub Secrets)")
        return 1
    chat = chat.lstrip("@").replace("https://t.me/", "")

    added = 0
    for item in candidates:
        text, tags = compose(item, add_tags=True)
        date_s, time_s = item["dt"].strftime("%Y-%m-%d"), item["dt"].strftime("%H:%M")
        if args.mode == "dry":
            log(f"\n--- DRY ({date_s} {time_s}) ---\n{text}\n")
            added += 1
            continue
        try:
            resp = tg_send(token, "@" + chat, text, link_preview=args.link_preview)
        except Exception as e:
            log(f"  × Telegram отклонил («{e}») — пропускаю запись")
            continue
        if not resp.get("ok"):
            log(f"  × Telegram вернул ошибку: {resp.get('description')} — пропускаю")
            continue
        msg_id = resp["result"]["message_id"]
        posts.append({
            "id": msg_id, "date": date_s, "time": time_s,
            "title": trim(item["title"], 110), "preview": trim(item["summary"], 180),
            "tags": tags, "kind": "text",
            "url": f"https://t.me/{chat}/{msg_id}", "src": item["src"],
        })
        seen[item["hash"]] = date_s
        added += 1
        log(f"  ✓ опубликовано: {trim(item['title'], 60)} → t.me/{chat}/{msg_id}")

    if args.mode == "publish" and added:
        posts.sort(key=lambda p: (p.get("date", ""), p.get("time", "")), reverse=True)
        posts_doc["posts"] = posts[:1500]
        posts_doc["updated_at"] = datetime.now(MSK).isoformat(timespec="seconds")
        if isinstance(posts_doc.get("channel"), dict) and chat:
            posts_doc["channel"]["url"] = f"https://t.me/{chat}"
        save_json(args.posts, posts_doc)
        seen_doc["seen"] = dict(sorted(seen.items(), key=lambda kv: kv[1], reverse=True)[:5000])
        save_json(args.seen, seen_doc)
        log(f"\nГотово: {added} новых постов; в оглавлении {len(posts_doc['posts'])}")
    elif args.mode == "dry":
        log(f"\nDRY-режим: было бы опубликовано {added}; файлы не менялись")
    return 0

if __name__ == "__main__":
    sys.exit(main())
