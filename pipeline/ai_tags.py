#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
«Оглавление телеграм-канала» · разметка тегами через любой ИИ
=============================================================
Продукт сознательно не содержит ИИ внутри (ноль расходов на серверы).
Вместо этого скрипт делает два простых действия:

  1) --prepare  готовит из result.json компактный файл с постами и ГОТОВЫМ
     промптом: скопируйте его содержимое в любой чат-бот (ChatGPT, Claude,
     GigaChat, Алиса...) и сохраните ответ в файл ai_tags_answer.json.

  2) --apply    собирает ответ ИИ обратно: строит docs/posts.json, где каждому
     посту присвоены теги от ИИ (посты без текста — медиа/опросы — получают
     типовые подписи как при обычном импорте).

Дальше запустите suggest_topics.py — он превратит теги в темы оглавления.

Использование:
    python3 pipeline/ai_tags.py --prepare --input import/result.json
    #  → появится ai_tags_prompt.txt: скопируйте его боту, ответ сохраните
    #    как ai_tags_answer.json рядом с result.json

    python3 pipeline/ai_tags.py --apply --input import/result.json \
        --tags ai_tags_answer.json --posts docs/posts.json \
        --channel-username @my_channel

Пусть ИИ ошибётся в паре постов — не страшно: правьте теги прямо в
docs/posts.json, формат записи очевиден.
"""
import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from export_to_posts import build_entry, detect_kind, log  # noqa: E402

PROMPT_RULES = """ЗАДАЧА: разложи посты Telegram-канала по темам.

ПРАВИЛА:
1. Придумай от 4 до 8 коротких рубрик (тегов) на русском языке, строчными
   буквами, без решётки. Например: книги, библия, размышления, цитаты, юмор.
2. Каждому посту присвой 1–2 рубрики ИЗ ТВОЕГО СПИСКА (одинаковый набор для
   всех постов, не придумывай новые рубрики на каждый пост).
3. Служебные/мелкие посты можно пометить рубрикой "разное".
4. Отвечай СТРОГО валидным JSON-массивом без пояснений и без markdown:
[{"id": 42, "tags": ["книги"]}, {"id": 43, "tags": ["библия", "цитата"]}]

ПОСТЫ (формат: id | дата | начало текста):
"""


def clean_one_line(s, limit=110):
    s = re.sub(r"\s+", " ", s or "").strip()
    return s[:limit]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--prepare", action="store_true", help="собрать промпт для ИИ")
    ap.add_argument("--apply", action="store_true", help="собрать ответ ИИ в posts.json")
    ap.add_argument("--input", default="import/result.json", help="result.json экспорта")
    ap.add_argument("--tags", default="ai_tags_answer.json", help="ответ ИИ (JSON-массив)")
    ap.add_argument("--out", default="ai_tags_prompt.txt", help="куда записать промпт")
    ap.add_argument("--posts", default="docs/posts.json")
    ap.add_argument("--channel-username", default="", help="@юзернейм канала (для --apply)")
    ap.add_argument("--limit", type=int, default=120, help="символов текста поста в промпте")
    args = ap.parse_args()

    if not args.prepare and not args.apply:
        log("Укажите действие: --prepare (собрать промпт) или --apply (собрать ответ)")
        return 1

    try:
        with open(args.input, "r", encoding="utf-8") as f:
            export = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        log(f"!! не читаю {args.input}: {e}")
        return 1

    messages = [m for m in (export.get("messages") or []) if m.get("type") == "message"]
    username = (args.channel_username or export.get("name") or "channel").strip()

    # ── режим 1: промпт ──
    if args.prepare:
        rows = []
        for m in messages:
            raw = m.get("text")
            if isinstance(raw, list):
                raw = "".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in raw)
            body = clean_one_line(raw, args.limit)
            if not body:
                continue  # медиа/опросы без текста: подписи типовые, ИИ не нужен
            rows.append(f"{m.get('id')} | {m.get('date','')[:10]} | {body}")
        if not rows:
            log("В экспорте нет текстовых постов — ИИ-разметка не нужна.")
            return 1
        if len(rows) > 600:
            log(f"!! постов {len(rows)} — файл промпта получится большой. "
                f"Если бот не примет целиком, разделите: сначала первую половину строк, потом вторую.")
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(PROMPT_RULES)
            f.write("\n".join(rows))
            f.write("\n")
        log(f"Готово: {args.out} ({len(rows)} текстовых постов).")
        log("Скопируйте содержимое файла в любой чат-бот, ответ сохраните как "
            f"{args.tags} и запустите: python3 pipeline/ai_tags.py --apply "
            f"--input {args.input} --tags {args.tags} --channel-username @мой_канал")
        return 0

    # ── режим 2: применить ответ ИИ ──
    if not args.channel_username:
        log("!! для --apply нужен --channel-username @мой_канал")
        return 1
    try:
        with open(args.tags, "r", encoding="utf-8") as f:
            raw_answer = f.read()
    except FileNotFoundError:
        log(f"!! нет файла ответа: {args.tags}")
        return 1
    raw_answer = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw_answer.strip(),
                        flags=re.M | re.S)  # бот мог обернуть в markdown-блок
    try:
        start, end = raw_answer.find("["), raw_answer.rfind("]")
        answer = json.loads(raw_answer[start:end + 1])
    except (ValueError, json.JSONDecodeError) as e:
        log(f"!! ответ ИИ не разобрать: {e}. Попросите бота «верни только JSON-массив» и повторите.")
        return 1

    ai_tags = {}
    for item in answer if isinstance(answer, list) else []:
        if not isinstance(item, dict):
            continue
        try:
            pid = int(item.get("id"))
        except (TypeError, ValueError):
            continue
        tags = []
        for t in (item.get("tags") or [])[:3]:
            t = str(t).strip().lstrip("#").lower()[:30]
            if t and t not in tags:
                tags.append("#" + t)
        if tags:
            ai_tags[pid] = tags
    log(f"Ответ ИИ: теги для {len(ai_tags)} постов.")

    fresh, with_ai, skipped = [], 0, 0
    for m in messages:
        if m.get("type") != "message":
            skipped += 1
            continue
        e = build_entry(m, username.lstrip("@").replace("https://t.me/", ""))
        if not e:
            skipped += 1
            continue
        e["kind"] = detect_kind(m)
        if e["id"] in ai_tags:
            e["tags"] = ai_tags[e["id"]]
            with_ai += 1
        fresh.append(e)

    username_clean = username.lstrip("@").replace("https://t.me/", "")
    old = []
    if os.path.exists(args.posts):
        try:
            with open(args.posts, "r", encoding="utf-8") as f:
                old = (json.load(f).get("posts")) or []
        except (json.JSONDecodeError, OSError):
            old = []
    by_url = {p.get("url"): p for p in old}
    for p in fresh:
        by_url[p["url"]] = p
    merged = list(by_url.values())
    merged.sort(key=lambda p: (p.get("date", ""), p.get("time", ""), str(p.get("id", ""))), reverse=True)

    doc = {
        "version": 1,
        "updated_at": __import__("datetime").datetime.now(
            __import__("datetime").timezone(__import__("datetime").timedelta(hours=3))
        ).isoformat(timespec="seconds"),
        "channel": {
            "name": export.get("name") or username_clean,
            "url": f"https://t.me/{username_clean}",
        },
        "posts": merged,
    }
    os.makedirs(os.path.dirname(args.posts) or ".", exist_ok=True)
    with open(args.posts, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)
    log(f"Готово: в оглавлении {len(merged)} постов; с тегами ИИ: {with_ai}; "
        f"медиа/опросы с типовыми подписями: {len(fresh) - with_ai}; пропущено: {skipped}")
    log("Дальше: python3 pipeline/suggest_topics.py --write — превратит теги в темы оглавления.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
