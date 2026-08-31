#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
«Оглавление телеграм-канала» · импорт истории
====================================
Превращает экспорт Telegram Desktop (result.json) в docs/posts.json.

Как покупатель получает result.json:
    Telegram Desktop → канал → ⋮ (три точки) → «Экспорт истории чата»
    → формат «Машинно-читаемый JSON» → скачать.

Использование:
    python pipeline/export_to_posts.py \
        --input import/result.json \
        --posts docs/posts.json \
        --channel-username @my_channel

Флаги:
    --merge     дополнить существующее оглавление (экспорт имеет приоритет)
                [включён по умолчанию]
    --replace   полностью заменить оглавление экспортом

Посты без текста (только фото/видео/аудио) получают заголовок «📷 Фотопост» и т.п.
Опросы подписываются вопросом: «📊 <вопрос>».
Записи-сервисы (участник присоединился и т.п.) пропускаются.
"""
import argparse
import html as html_mod
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone

MSK = timezone(timedelta(hours=3))
PHOTO_TITLE = "📷 Фотопост"
VIDEO_TITLE = "🎬 Видеопост"
AUDIO_TITLE = "🎧 Аудио"
FILE_TITLE = "📎 Файл"
POLL_TITLE = "📊 Опрос"

def log(m): print(m, flush=True)

def clean(s):
    s = re.sub(r"\s+", " ", s or "").strip()
    return s

def strip_hashtags(s):
    return clean(re.sub(r"#[\wа-яё]+", "", s or "", flags=re.I))

def extract_text(raw):
    """text в result.json бывает строкой или списком строк и объектов."""
    if isinstance(raw, str):
        return raw
    parts = []
    if isinstance(raw, list):
        for p in raw:
            if isinstance(p, str):
                parts.append(p)
            elif isinstance(p, dict):
                parts.append(str(p.get("text") or ""))
    return "\n".join(x for x in parts if x)

def split_title(line, limit=100):
    """(заголовок, остаток строки): резка ТОЛЬКО по границе слова,
    чтобы preview не начинался с обрывка слова."""
    if len(line) <= limit:
        return line, ""
    cut = line[:limit]
    sp = cut.rfind(" ")
    if sp > 40:
        return cut[:sp].rstrip(" ,;:-") + "…", line[sp:].strip()
    return cut.rstrip(" ,;:-") + "…", ""

def clean_line(l):
    """Чистит ОДНУ строку: сжимает пробелы, но сохраняет переводы строк."""
    return re.sub(r"[ \t\u00a0]+", " ", l or "").strip()

def first_meaningful_line(body):
    """Индекс первой содержательной строки (не пустой, не из одних хэштегов
    и не только из эмодзи/символов — заголовку нужна буква или цифра)."""
    lines = [clean_line(l) for l in (body or "").splitlines()]
    for i, line in enumerate(lines):
        if not line:
            continue
        if re.fullmatch(r"#[\wа-яё]+(\s+#[\wа-яё]+)*", line, flags=re.I):
            continue  # строка из одних хэштегов — берём следующую
        if not re.search(r"[0-9a-zа-яё]", line, flags=re.I):
            continue  # строка только из эмодзи («🌿») — заголовком быть не может
        return i, lines
    return -1, lines

def detect_kind(msg):
    """Тип контента для универсальной группировки оглавления."""
    if (msg.get("poll") or {}).get("question"):
        return "poll"
    if msg.get("photo"):
        return "photo"
    if msg.get("media_type") in ("video_file", "video_message"):
        return "video"
    if msg.get("media_type") == "audio_file":
        return "audio"
    if msg.get("sticker_emoji"):
        return "sticker"
    if msg.get("file"):
        return "file"
    return "text"

def build_entry(msg, username):
    raw_text = extract_text(msg.get("text"))
    hashtags = re.findall(r"#([\wа-яё]+)", raw_text, flags=re.I)
    i0, lines = first_meaningful_line(raw_text)
    has_media = any(msg.get(k) for k in ("photo", "video_file", "media_type", "file"))
    if i0 < 0:
        # текстового заголовка нет — подписываем медиа/опрос
        poll_q = clean((msg.get("poll") or {}).get("question") or "")
        if poll_q:
            title, _ = split_title(POLL_TITLE + " «" + poll_q + "»", 110)
            rest_line = ""
        elif msg.get("photo"):
            title, rest_line = PHOTO_TITLE, ""
        elif msg.get("media_type") in ("video_file", "video_message"):
            title, rest_line = VIDEO_TITLE, ""
        elif msg.get("media_type") == "audio_file":
            title, rest_line = AUDIO_TITLE, ""
        elif msg.get("file"):
            title, rest_line = FILE_TITLE, ""
        elif hashtags:
            title, rest_line = "#" + hashtags[0], ""
        else:
            return None  # нечего показывать
    else:
        title, rest_line = split_title(lines[i0])
    # preview: остаток строки заголовка + последующие строки, без хэштегов
    preview = strip_hashtags(" ".join(x for x in [rest_line] + lines[i0 + 1:] if x)) if i0 >= 0 else ""
    dt = msg.get("date") or ""
    try:
        d = datetime.fromisoformat(dt)
        date_s, time_s = d.strftime("%Y-%m-%d"), d.strftime("%H:%M")
    except ValueError:
        date_s, time_s = dt[:10], dt[11:16] if len(dt) > 15 else ""
    tags = ["#" + t for t in hashtags[:4]]
    return {
        "id": msg.get("id"),
        "date": date_s, "time": time_s,
        "title": title,
        "preview": preview[:180],
        "tags": tags,
        "kind": detect_kind(msg),
        "url": f"https://t.me/{username}/{msg.get('id')}",
        "src": "",  # у постов канала нет внешнего источника
    }

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True, help="путь к result.json (экспорт Telegram Desktop)")
    ap.add_argument("--posts", default="docs/posts.json")
    ap.add_argument("--channel-username", required=True, help="@юзернейм канала")
    ap.add_argument("--replace", action="store_true", help="заменить оглавление экспортом целиком")
    args = ap.parse_args()

    username = args.channel_username.strip().lstrip("@").replace("https://t.me/", "")
    try:
        with open(args.input, "r", encoding="utf-8") as f:
            export = json.load(f)
    except FileNotFoundError:
        log(f"!! файл не найден: {args.input}")
        return 1
    except json.JSONDecodeError as e:
        log(f"!! это не JSON: {e}")
        return 1

    messages = export.get("messages") or []
    log(f"Экспорт: «{export.get('name','?')}», записей: {len(messages)}")

    fresh, skipped = [], 0
    for m in messages:
        if m.get("type") != "message":
            skipped += 1
            continue
        e = build_entry(m, username)
        if e:
            fresh.append(e)
        else:
            skipped += 1

    if args.replace:
        merged = fresh
    else:
        doc = None
        if os.path.exists(args.posts):
            with open(args.posts, "r", encoding="utf-8") as f:
                doc = json.load(f)
        old = (doc or {}).get("posts") or []
        by_url = {p.get("url"): p for p in old}
        for p in fresh:
            by_url[p["url"]] = p   # экспорт приоритетнее
        merged = list(by_url.values())

    merged.sort(key=lambda p: (p.get("date", ""), p.get("time", ""), str(p.get("id", ""))), reverse=True)
    doc = {
        "version": 1,
        "updated_at": datetime.now(MSK).isoformat(timespec="seconds"),
        "channel": {
            "name": export.get("name") or username,
            "url": f"https://t.me/{username}",
        },
        "posts": merged,
    }
    os.makedirs(os.path.dirname(args.posts) or ".", exist_ok=True)
    with open(args.posts, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)
    log(f"Готово: постов в оглавлении {len(merged)} (новых из экспорта: {len(fresh)}, пропущено: {skipped})")
    return 0

if __name__ == "__main__":
    sys.exit(main())
