#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
«Оглавление телеграм-канала» · ассистент тем оглавления
=======================================
Читает docs/posts.json и предлагает черновик docs/topics.json:
превращает самые частые хэштеги постов в темы.

Бренд-теги отсекаются автоматически: если тег стоит у более чем 40% постов,
он бесполезен как тема (это имя канала) и в черновик не попадает.

Это АССИСТЕНТ, а не магия: черновик нужно просмотреть — поправить названия
(«books» → «Книги и чтение») и подобрать иконки. Правьте получивший файл
напрямую, он перезаписывается только при повторном запуске с флагом --force.

Использование:
    python3 pipeline/suggest_topics.py                       # показать черновик в консоли
    python3 pipeline/suggest_topics.py --write               # записать docs/topics.json
    python3 pipeline/suggest_topics.py --write --force       # перезаписать существующий
    python3 pipeline/suggest_topics.py --limit 6 --min 3     # меньше тем, выше порог

Флаги:
    --limit N   максимум тем в черновике (по умолчанию 8)
    --min N     минимум постов с тегом, чтобы тег стал темой (по умолчанию 2)
"""
import argparse
import json
import os
import sys
from collections import Counter

# Подбор иконки по смыслу тега (первые буквы слова); иначе 🏷
ICONS = {
    "книг": "📚", "читен": "📚", "чтени": "📚",
    "библи": "📖", "писан": "📖", "псалом": "📖", "евангел": "📖",
    "размышл": "🧠", "дневник": "🧠", "психолог": "🧠",
    "цитат": "💬", "афоризм": "💬",
    "истори": "🏛", "церков": "⛪", "вера": "⛪", "вероучен": "⛪", "богослов": "⛪",
    "анонс": "📣", "новост": "📣", "событи": "📣",
    "юмор": "😄", "шутк": "😄", "анекдот": "😄",
    "видео": "🎬", "подкаст": "🎙", "аудио": "🎧", "музык": "🎵",
    "опрос": "📊", "итог": "📈", "статистик": "📈",
    "семь": "👨‍👩‍👧", "дет": "👨‍👩‍👧", "воспитан": "👨‍👩‍👧",
    "обществ": "🏙", "город": "🏙", "люд": "🏙",
    "жизн": "🌿", "здоров": "🌿", "спорт": "⚽",
    "техник": "💻", "наук": "🔬", "финанс": "💼", "деньг": "💼",
    "едa": "🍳", "еда": "🍳", "рецепт": "🍳", "путешеств": "✈️",
    "культур": "🎭", "кино": "🍿", "искусств": "🎭",
}


def log(m): print(m, flush=True)

# Служебные теги-маркеры серий: темами быть не должны
STOP_TAGS = {"продолжение", "окончание", "завершение", "продолжение_следует"}


def norm(t):
    return str(t or "").lstrip("#").lower()


def icon_for(tag):
    for k, v in ICONS.items():
        if k in tag:
            return v
    return "🏷"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--posts", default="docs/posts.json")
    ap.add_argument("--write", action="store_true", help="записать черновик в topics.json")
    ap.add_argument("--force", action="store_true", help="перезаписать существующий файл")
    ap.add_argument("--limit", type=int, default=8)
    ap.add_argument("--min", type=int, default=2)
    args = ap.parse_args()

    if not os.path.exists(args.posts):
        log(f"!! нет файла {args.posts}")
        return 1
    with open(args.posts, "r", encoding="utf-8") as f:
        doc = json.load(f)
    posts = doc.get("posts") or []
    if not posts:
        log("В posts.json пока нет постов. Сначала прогоните конвейер или импорт.")
        return 1
    total = len(posts)

    counts = Counter()
    for p in posts:
        for t in p.get("tags") or []:
            k = norm(t)
            if k:
                counts[k] += 1

    brand = {t for t, n in counts.items() if n > total * 0.4}
    if brand:
        log("Похоже на бренд-теги (стоит у большинства постов, темами быть не должны): "
            + ", ".join("#" + t for t in sorted(brand)))

    candidates = [(t, n) for t, n in counts.most_common()
                  if n >= args.min and t not in brand and t not in STOP_TAGS]
    if not candidates:
        log("Подходящих тегов не нашлось. Если в канале нет хэштегов — проставьте темы вручную "
            "в docs/topics.json (по ключевым словам) или попросите продавца настроить рубрикатор.")
        return 1

    topics = []
    for t, n in candidates[: args.limit]:
        topics.append({
            "id": t,
            "title": t[:1].upper() + t[1:],
            "icon": icon_for(t),
            "tags": [t],
        })

    draft = {
        "version": 1,
        "_note": "Черновик от suggest_topics.py: поправьте названия и иконки, ненужное удалите.",
        "topics": topics,
    }

    log(f"\nЧерновик тем ({len(topics)} шт., постов всего {total}):")
    for t in topics:
        n = counts[t["id"]]
        log(f"  {t['icon']} {t['title']} — {n} {n % 10 == 1 and n % 100 != 11 and 'пост' or (n % 10 in (2,3,4) and n % 100 not in (12,13,14) and 'поста' or 'постов')}")
    misc = sum(1 for p in posts if not any(norm(x) in {tt["id"] for tt in topics} for x in (p.get("tags") or [])))
    log(f"  🗂 Разное (не попадёт ни в одну тему): {misc} постов")
    log("\nДальше: просмотрите черновик — названия тем («books» → «Книги и чтение»), иконки, состав тегов темы (в tags можно вписать несколько синонимов).")

    if args.write:
        out = "docs/topics.json"
        if os.path.exists(out) and not args.force:
            log(f"\n!! {out} уже существует — добавьте --force, чтобы перезаписать.")
            return 1
        os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
        with open(out, "w", encoding="utf-8") as f:
            json.dump(draft, f, ensure_ascii=False, indent=2)
            f.write("\n")
        log(f"Черновик записан: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
