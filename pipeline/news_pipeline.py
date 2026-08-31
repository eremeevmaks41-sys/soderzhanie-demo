#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
«Оглавление телеграм-канала» · новостной конвейер v2 (ИИ-карточки)
==================================================================
GitHub Actions запускает скрипт каждые 2 часа:

    RSS-источники → фото из статьи → ИИ-выжимка (Gemini/Groq)
    → красивая карточка в Telegram-канал (фото + подпись или текст)
    → запись в docs/posts.json → коммит → Pages обновляет мини-апп.

Формат карточки (ИИ пишет по-русски, источник любой):
    🌍 Заголовок
    Лид: 2–3 предложения сути.
    • факт 1
    • факт 2
    источник: BBC            ← тихая ссылка, без превью

Режимы:
    --mode dry      показать, что БЫЛО бы опубликовано (без отправки и записи)
    --mode publish  полный цикл: публикация в канал + обновление posts.json

Секреты (GitHub → Settings → Secrets and variables → Actions):
    BOT_TOKEN        токен бота от @BotFather
    CHANNEL_USERNAME юзернейм канала вида @my_channel (канал публичный!)
    GROQ_API_KEY     ключ ИИ — Google Gemini (aistudio.google.com, бесплатно,
                     начинается с "AIza") или Groq (groq.com, "gsk_"). БЕЗ НЕГО
                     ПУБЛИКАЦИЯ СТОИТ: конвейер не постит сырые английские анонсы
                     в русский канал. Провайдер распознаётся по префиксу ключа.

Лимиты: --max новостей за запуск, --daily-cap постов в сутки (по posts.json).
Фото: из RSS (media:content/enclosure/thumbnail) или og:image статьи;
    нет фото → постим текстом; Telegram не принял фото → тоже текстом.
Дедуп: seen.json (hash) + ссылки в posts.json + нормализованные заголовки.
"""
import argparse
import hashlib
import html as html_mod
import json
import os
import re
import sys
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime, timedelta, timezone

MSK = timezone(timedelta(hours=3))

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL_DEFAULT = "openai/gpt-oss-120b"

# Gemini доступен из облачных раннеров (Groq блокирует дата-центровые IP),
# поэтому основной провайдер — Google Gemini через OpenAI-совместимый endpoint.
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
GEMINI_MODEL_DEFAULT = "gemini-2.5-flash"

# Эмодзи, которые ИИ может поставить карточке (вне списка — эмодзи источника)
EMOJI_WHITELIST = [
    "🌍", "🔥", "⚡", "💰", "🏛️", "⚖️", "🚀", "🔬", "💊", "🎓", "⚠️", "🌐",
    "📱", "🛰️", "🏭", "🎭", "🏆", "🌊", "✈️", "🚗", "📊", "🤝", "🗳️", "🕊️",
    "⛽", "📈", "📉", "🧑‍⚖️", "🏗️", "🛡️",
]

# Тема по ключевым словам (для второй метки в оглавлении)
KEYWORD_TAGS = [
    ("конфликт",  ["войн", "удар", "обстрел", "наступлен", "боев", "перемир", "ракет", "дрон"]),
    ("политика",  ["выбор", "президен", "парламент", "министр", "выборы", "саммит", "переговор", "выставил", "депутат"]),
    ("экономика", ["инфляц", "ставк", "банк", "рынк", "доллар", "евро", "нефт", "газ", "санкц", "бюджет", "тариф", "экспорт", "импорт"]),
    ("наука",     ["учен", "наук", "исследован", "открыт", "космос", "nasa", "ракет-носител", "климат"]),
    ("технологии",["ai", "искусственн", "технолог", "приложен", "чек", "cyber", "хакер", "чип", "apple", "google", "tesla"]),
    ("происшествия", ["землетрясен", "наводнен", "пожар", "авиакатастроф", "крушен", "эпидем", "вспышк", "авар"]),
    ("культура",  ["фильм", "преми", "фестивал", "альбом", "сериал", "книг", "выставк", "оскар"]),
    ("спорт",     ["чемпион", "матч", "кубок", "олимпиад", "турнир", "футбол", "хоккей"]),
    ("общество",  ["забастовк", "протест", "мигрант", "суд", "приговор", "закон", "школ", "больниц"]),
]

AI_SYSTEM = (
    "Ты — новостной редактор русскоязычного Telegram-канала мировых новостей.\n"
    "На вход приходит заголовок и описание новости из RSS (обычно на английском).\n"
    "Верни СТРОГО один JSON-объект без markdown-обёрток:\n"
    '{"emoji": "…", "headline": "…", "lede": "…", "bullets": ["…", "…"]}\n'
    "Правила (строго):\n"
    "— Всё по-русски. Имена собственные — в устоявшейся русской передаче; организации — как принято.\n"
    "— headline: до 100 символов, ёмкий заголовок с сутью, без точки в конце, без кавычек-ёлочек по краям.\n"
    "— lede: 2–3 предложения (до 340 символов) — суть события: кто, что, где, когда, цифры.\n"
    "— bullets: 0–3 коротких факта (каждый до 100 символов) ТОЛЬКО если они реально есть во входе.\n"
    "  Нет конкретики — пустой список []. Ничего не выдумывай и не добавляй контекст извне.\n"
    "— emoji: ОДИН из списка, лучше всего отражающий тему:\n"
    "  " + " ".join(EMOJI_WHITELIST) + "\n"
    "— Числа, даты, суммы, имена переноси точно как во входе. Не выдумывай причину и последствия.\n"
    "— Если вход — мнение, анонс или слишком мала для новости, всё равно сделай карточку по факту входа.\n"
)


# ───────────────────────────── базовые утилиты ─────────────────────────────

def log(msg):
    print(msg, flush=True)

def http_json(url, payload=None, headers=None, timeout=30):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    base = {"Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (compatible; Soderzhanie/2.0)"}
    if headers:
        base.update(headers)
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET",
                                 headers=base)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))

def http_get_bytes(url, timeout=12, max_len=400_000):
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (compatible; Soderzhanie/2.0)",
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(max_len)

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

def plain_len(html_text):
    """Длина текста поста БЕЗ html-тегов (Telegram считает именно её)."""
    return len(re.sub(r"<[^>]+>", "", html_text))

def esc(s):
    return html_mod.escape(s or "", quote=True)

def keyword_tags(text):
    low = " " + (text or "").lower() + " "
    for tag, keys in KEYWORD_TAGS:
        if any(k in low for k in keys):
            return "#" + tag
    return "#мировыеновости"

def norm_title(t):
    """Нормализация заголовка для кросс-источникового дедупа."""
    t = (t or "").lower()
    t = re.sub(r"[^\wа-яё ]+", " ", t, flags=re.I)
    return re.sub(r"\s+", " ", t).strip()


# ───────────────────────────── RSS: чтение и фото ─────────────────────────────

def fetch_feed(source):
    """Загружает и разбирает один RSS/Atom-поток штатными средствами Python
    (без внешних зависимостей — в Actions не нужен pip install).
    Ошибка одного источника не роняет запуск."""
    import xml.etree.ElementTree as ET
    try:
        req = urllib.request.Request(source["url"], headers={
            "User-Agent": "Mozilla/5.0 (compatible; Soderzhanie/2.0)",
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


def _image_url(el):
    """URL картинки из media:*/enclosure-элемента + подсказка ширины (для выбора лучшего)."""
    if el is None:
        return "", 0
    url = (el.get("url") or el.get("href") or "").strip()
    if not url:
        return "", 0
    mime = (el.get("type") or el.get("medium") or "").lower()
    if mime and not (mime.startswith("image") or mime == "photo"):
        return "", 0
    if not re.match(r"^https?://", url):
        return "", 0
    try:
        w = int(el.get("width") or 0)
    except ValueError:
        w = 0
    return url, w


def _pick_biggest(cands):
    """Из media:content одной записи берём самый крупный вариант:
    сначала атрибут width, при нуле — эвристика по цифрам в URL."""
    best, best_w = "", -1
    for u, w in cands:
        if w == 0:
            m = re.search(r"[/_.-](\d{2,4})[x×][/.-]", u)  # 640x480 в пути
            if m:
                w = int(m.group(1))
        if w > best_w:
            best, best_w = u, w
    return best


def normalize_entry(item):
    """RSS <item> или Atom <entry> → единый словарь записи (+ фото, если есть)."""
    def _local(p):
        return p.split("}")[-1].split(":")[-1]   # "media:content" → "content"

    def first(*paths):
        for p in paths:
            found = item.find(p)
            if found is None:
                found = item.find("{*}" + _local(p))   # namespace-агностично
            if found is not None:
                return found
        return None

    def all_els(*paths):
        out = []
        for p in paths:
            out.extend(item.findall(p))
            out.extend(item.findall("{*}" + _local(p)))
        return out

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

    # фото: media:content (может быть несколько размеров) → enclosure → media:thumbnail
    img = ""
    cands = [_image_url(e) for e in all_els("media:content")]
    cands = [c for c in cands if c[0]]
    if cands:
        img = _pick_biggest(cands)
    if not img:
        for e in all_els("enclosure"):
            u, _w = _image_url(e)
            if u:
                img = u
                break
    if not img:
        for e in all_els("media:thumbnail"):
            u, _w = _image_url(e)
            if u:
                img = u
                break

    when = _when(first("pubDate", "published", "updated", "date"))
    guid_el = first("guid", "id")
    return {
        "title": _txt(title_el),
        "link": link,
        "summary": summary,
        "image": img,
        "id": ((guid_el.text if guid_el is not None else "") or link),
        "published_parsed": when,
    }


OG_RE = re.compile(
    r'<meta[^>]+(?:property=["\']og:image(?::secure_url)?["\']'
    r'|name=["\']twitter:image(?::src)?["\'])[^>]+content=["\']([^"\']+)["\']',
    re.IGNORECASE)
OG_RE2 = re.compile(
    r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property=["\']og:image(?::secure_url)?["\']'
    r'|name=["\']twitter:image(?::src)?["\'])',
    re.IGNORECASE)


def og_image(article_url):
    """Резервное фото: og:image/twitter:image со страницы статьи. Best-effort."""
    if not article_url:
        return ""
    try:
        raw = http_get_bytes(article_url, timeout=10, max_len=300_000).decode("utf-8", "ignore")
    except Exception:
        return ""
    m = OG_RE.search(raw) or OG_RE2.search(raw)
    if not m:
        return ""
    url = html_mod.unescape(m.group(1)).strip()
    return url if re.match(r"^https?://", url) else ""


# ───────────────────────────── ИИ-выжимка (Gemini / Groq) ─────────────────────────────

def _parse_ai_json(raw):
    """Терпимый парсер ответа модели: срезает ```-обёртки и мусор вокруг JSON."""
    if not raw:
        return None
    s = raw.strip()
    s = re.sub(r"^```(?:json)?\s*", "", s)
    s = re.sub(r"\s*```$", "", s)
    a, b = s.find("{"), s.rfind("}")
    if a == -1 or b <= a:
        return None
    try:
        d = json.loads(s[a:b + 1])
    except Exception:
        return None
    if not isinstance(d, dict):
        return None
    headline = clean_html(str(d.get("headline") or ""))
    lede = clean_html(str(d.get("lede") or ""))
    if not headline:
        return None
    bullets_raw = d.get("bullets")
    bullets = []
    if isinstance(bullets_raw, list):
        for b_ in bullets_raw:
            if isinstance(b_, str) and clean_html(b_):
                bullets.append(trim(clean_html(b_), 100))
            if len(bullets) >= 3:
                break
    emoji = str(d.get("emoji") or "").strip()
    return {
        "headline": trim(headline, 110),
        "lede": trim(lede, 340),
        "bullets": bullets,
        "emoji": emoji if emoji in EMOJI_WHITELIST else "",
    }


def ai_card(title, summary, source_name):
    """ИИ-выжимка одной новости. None — ИИ недоступен/ответ некорректен.
    Провайдер выбирается по ключу: "AIza…" → Google Gemini, "gsk_…" → Groq.
    Переменные AI_URL / AI_MODEL переопределяют выбор вручную."""
    key = (os.environ.get("GEMINI_API_KEY") or os.environ.get("GROQ_API_KEY") or "").strip()
    if not key:
        return None
    url = (os.environ.get("AI_URL") or "").strip()
    model = (os.environ.get("AI_MODEL") or os.environ.get("GROQ_MODEL") or "").strip().strip("\"'")
    if not url:
        url = GEMINI_URL if key.startswith("AIza") else GROQ_URL
    if not model:
        model = GEMINI_MODEL_DEFAULT if "generativelanguage" in url else GROQ_MODEL_DEFAULT
    user_msg = (f"Источник: {source_name}\n"
                f"Заголовок: {title}\n"
                f"Описание: {summary or '(пусто)'}")
    try:
        resp = http_json(url, {
            "model": model,
            "temperature": 0.2,
            "max_tokens": 1500,
            "messages": [
                {"role": "system", "content": AI_SYSTEM},
                {"role": "user", "content": user_msg},
            ],
        }, headers={"Authorization": f"Bearer {key}"}, timeout=40)
        return _parse_ai_json((resp["choices"][0]["message"].get("content") or ""))
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read(300).decode("utf-8", "ignore")
        except Exception:
            pass
        log(f"    · ИИ недоступен: HTTP {e.code} {e.reason} :: {detail or '(тело ответа пустое)'}")
        return None
    except Exception as e:
        log(f"    · ИИ недоступен: {e}")
        return None


def fallback_card(item):
    """Карточка без ИИ (для dry-режима без ключа): сырой RSS-текст."""
    return {
        "headline": trim(item["title"], 110),
        "lede": trim(item["summary"], 340),
        "bullets": [],
        "emoji": "",
    }


# ───────────────────────────── карточка поста ─────────────────────────────

def compose(item, card):
    """HTML-карточка поста. Возвращает (html, kind). Теги идут только в posts.json,
    в тексте поста их нет — лента выглядит чисто.

    С фото подпись должна влезать в 1024 символа ПО ТЕКСТУ (без тегов):
    сначала жертвуем буллетами, потом укорачиваем лид.
    Ссылка на источник — одна тихая строка внизу; превью выключено.
    """
    s = item["source"]
    emoji = card.get("emoji") or s.get("emoji", "🌍")
    src_name = s.get("name", "источник")
    source_line = f"источник: <a href=\"{esc(item['src'])}\">{esc(src_name)}</a>"

    headline = esc(card["headline"])
    lede = esc(card.get("lede") or "")
    bullets = [esc(b) for b in card.get("bullets") or []]

    def build(with_bullets=True):
        lines = [f"{emoji} <b>{headline}</b>"]
        if lede:
            lines += ["", lede]
        if with_bullets and bullets:
            lines += ["", "• " + "\n• ".join(bullets)]
        lines += ["", source_line]
        return "\n".join(lines)

    text = build(True)
    if item.get("image"):
        # сжимаем до лимита подписи (1024 по тексту, запас 60 на реалии Telegram)
        while plain_len(text) > 960:
            if bullets:
                bullets = bullets[:-1]
            elif len(lede) > 120:
                lede = esc(trim(html_mod.unescape(lede), int(len(lede) * 0.75)))
            else:
                break
            text = build(True)
        if plain_len(text) <= 960:
            return text, "photo"
    # текстовый пост: лимит 4096 — тут ничего резать почти никогда не нужно
    text = build(True)
    if plain_len(text) > 4090:
        text = build(False)
    return text, "text"


# ───────────────────────────── Telegram ─────────────────────────────

def tg_send(token, chat, text):
    api = f"https://api.telegram.org/bot{token}/sendMessage"
    return http_json(api, {
        "chat_id": chat, "text": text, "parse_mode": "HTML",
        "disable_web_page_preview": True,
    })


def tg_send_photo(token, chat, photo_url, caption):
    api = f"https://api.telegram.org/bot{token}/sendPhoto"
    return http_json(api, {
        "chat_id": chat, "photo": photo_url, "caption": caption,
        "parse_mode": "HTML",
    })


def publish_item(token, chat, item, text, kind):
    """Фото → sendPhoto; не получилось → sendMessage текстом.
    Возвращает (ok, actual_kind, message_id)."""
    if kind == "photo" and item.get("image"):
        try:
            resp = tg_send_photo(token, chat, item["image"], text)
            if resp.get("ok"):
                return True, "photo", resp["result"]["message_id"]
            log(f"    · фото отклонено ({resp.get('description')}) — шлю текстом")
        except Exception as e:
            log(f"    · фото не отправилось ({e}) — шлю текстом")
    try:
        resp = tg_send(token, chat, text)
    except Exception as e:
        log(f"    × Telegram отклонил («{e}»)")
        return False, "text", None
    if not resp.get("ok"):
        log(f"    × Telegram вернул ошибку: {resp.get('description')}")
        return False, "text", None
    return True, "text", resp["result"]["message_id"]


# ───────────────────────────── отбор кандидатов ─────────────────────────────

def pick(entries, source, seen, existing_srcs, existing_titles, max_items):
    """Отбирает самые свежие ещё не публиковавшиеся записи одного источника.
    Дедуп: hash GUID, ссылка в оглавлении, нормализованный заголовок."""
    out = []
    local_titles = set()
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
        nt = norm_title(title)
        if len(nt) < 15:
            continue                                  # служебные/пустые заголовки
        if nt in existing_titles or nt in local_titles:
            continue                                  # ту же новость несёт другой источник
        local_titles.add(nt)
        when = e.get("published_parsed") or e.get("updated_parsed")
        dt = (datetime(*when[:6], tzinfo=timezone.utc).astimezone(MSK)
              if when else datetime.now(MSK))
        out.append({"src": link, "hash": h, "title": title, "summary": summary,
                    "image": e.get("image", ""), "dt": dt, "source": source})
        if len(out) >= max_items:
            break
    return out


def today_count(posts):
    """Сколько постов опубликовано сегодня (MSK) — для дневного лимита."""
    today = datetime.now(MSK).strftime("%Y-%m-%d")
    return sum(1 for p in posts if p.get("date") == today)


# ───────────────────────────── main ─────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["dry", "publish"], default="dry")
    ap.add_argument("--max", type=int, default=1, help="максимум новостей за запуск (на ВСЕ источники)")
    ap.add_argument("--max-per-source", type=int, default=1)
    ap.add_argument("--daily-cap", type=int, default=10, help="максимум постов в сутки (MSK)")
    ap.add_argument("--no-og-image", action="store_true",
                    help="не ходить за og:image, если RSS не дал фото")
    ap.add_argument("--sources", default="pipeline/sources.json")
    ap.add_argument("--posts", default="docs/posts.json")
    ap.add_argument("--seen", default="pipeline/seen.json")
    args = ap.parse_args()

    cfg = load_json(args.sources, {"sources": []})
    sources = cfg.get("sources") or []
    if not sources:
        log("!! sources.json не содержит источников — делать нечего")
        return 1

    posts_doc = load_json(args.posts, {"version": 1, "updated_at": "", "channel": {}, "posts": []})
    posts = posts_doc.get("posts") or []
    existing_srcs = {p.get("src") for p in posts if p.get("src")}
    existing_titles = {norm_title(p.get("title") or "") for p in posts[-300:]}

    seen_doc = load_json(args.seen, {"seen": {}})
    seen = seen_doc.get("seen") or {}

    published_today = today_count(posts)
    if published_today >= args.daily_cap:
        log(f"Дневной лимит исчерпан ({published_today}/{args.daily_cap}) — запуск не нужен")
        return 0

    log(f"Источников: {len(sources)}; в оглавлении {len(posts)} постов; "
        f"сегодня уже {published_today}/{args.daily_cap}")
    candidates = []
    for src in sources:
        candidates += pick(fetch_feed(src), src, seen, existing_srcs,
                           existing_titles, args.max_per_source)
    candidates.sort(key=lambda x: x["dt"], reverse=True)
    room = args.daily_cap - published_today
    candidates = candidates[: max(0, min(args.max, room))]
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

    ai_ready = bool((os.environ.get("GEMINI_API_KEY") or os.environ.get("GROQ_API_KEY") or "").strip())
    if args.mode == "publish" and not ai_ready:
        log("!! ключ ИИ не задан — публиковать сырые английские анонсы "
            "в русский канал не буду. Добавьте ключ Google Gemini (aistudio.google.com, "
            "бесплатно) в секрет GROQ_API_KEY.")

    added = 0
    for item in candidates:
        date_s, time_s = item["dt"].strftime("%Y-%m-%d"), item["dt"].strftime("%H:%M")

        # фото: RSS-медиа → og:image статьи (best-effort)
        if not item.get("image") and not args.no_og_image:
            item["image"] = og_image(item["src"])

        card = ai_card(item["title"], item["summary"], item["source"].get("name", ""))
        if card is None:
            if args.mode == "publish":
                log(f"  × без ИИ-выжимки не публикую: {trim(item['title'], 60)}")
                continue                      # hash НЕ записан → ретрай на следующем запуске
            card = fallback_card(item)

        tags = list(item["source"].get("tags", ["#мировыеновости"]))
        extra = keyword_tags(item["title"] + " " + item["summary"] + " " + card.get("lede", ""))
        if extra not in tags:
            tags.append(extra)

        text, kind = compose(item, card)

        if args.mode == "dry":
            photo = " (с фото)" if kind == "photo" else ""
            log(f"\n--- DRY ({date_s} {time_s}){photo} ---\n{text}\n")
            added += 1
            continue

        ok, kind, msg_id = publish_item(token, "@" + chat, item, text, kind)
        if not ok:
            continue                          # Telegram не принял вовсе — пропускаю

        posts.append({
            "id": msg_id, "date": date_s, "time": time_s,
            "title": trim(card["headline"], 110), "preview": trim(card.get("lede") or item["summary"], 180),
            "tags": tags, "kind": kind,
            "url": f"https://t.me/{chat}/{msg_id}", "src": item["src"],
        })
        seen[item["hash"]] = date_s
        added += 1
        log(f"  ✓ {kind}: {trim(card['headline'], 60)} → t.me/{chat}/{msg_id}")

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
