#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
«Оглавление телеграм-канала» · новостной конвейер v2 (ИИ-карточки)
==================================================================
GitHub Actions запускает скрипт каждые 2 часа:

    RSS-источники (мировые ленты + российские агентства) → фото из статьи
    → ИИ-выжимка (OpenRouter / Gemini / Groq)
    → красивая карточка в Telegram-канал (фото + подпись или текст)
    → запись в docs/posts.json → коммит → Pages обновляет мини-апп.

Отбор: источники обходятся по кругу (сдвиг зависит от времени суток),
    иначе круглосуточные российские агентства (публикуют в разы чаще
    мировых лент) вытеснили бы BBC/Al Jazeera/Guardian из ленты.
    Мировые ленты приходят на английском — ИИ переводит; российские
    публикуют по-русски — ИИ просто сжимает суть.

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
    GROQ_API_KEY     ключ ИИ (имя историческое): OpenRouter (openrouter.ai,
                     "sk-or-…", есть бесплатные модели) / Google Gemini
                     ("AIza…") / Groq ("gsk_…"). БЕЗ НЕГО ПУБЛИКАЦИЯ СТОИТ:
                     конвейер не постит сырые анонсы без ИИ-выжимки.
                     Провайдер распознаётся по префиксу ключа.

Лимиты: --max новостей за запуск, --daily-cap постов в сутки (по posts.json).
Фото: из RSS (media:content/enclosure/thumbnail) или og:image статьи;
    нет фото → постим текстом; Telegram не принял фото → тоже текстом.
Видео: если в записи RSS есть видео-вложение (media/enclosure) или на странице
    статьи og:video с прямым mp4 — публикуем ВИДЕОПОСТ (kind=video):
    Telegram берёт файл сам по URL (≤20 МБ) или качаем и льём файлом (≤45 МБ);
    не вышло → фото, затем текст. Агентства дают короткие нарезки 1–2 мин
    в невысоком разрешении (~4–8 МБ) — для канала достаточно.
Дедуп: seen.json (hash) + ссылки в posts.json + нормализованные заголовки.

Сверка оглавления (publish-запуски): последние ~120 постов опрашиваются в
    канале editMessageText/Caption ТОМ ЖЕ текстом («message is not modified»
    = жив, и на экране ничего не меняется; старые посты без сохранённого
    текста — t.me-эмбедом). Пост, удалённый в Telegram, вычищается из
    posts.json — оглавление больше не ссылается на «Пост не найден».
    Ручная сверка без публикаций: Run workflow → sync_only=true.
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

# Groq блокирует дата-центровые IP, поэтому основные провайдеры —
# OpenRouter (агрегатор, облачные IP не блокирует) и Google Gemini.
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
GEMINI_MODEL_DEFAULT = "gemini-2.5-flash"

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
# Бесплатные модели (суффикс :free), пробуются по очереди, пока одна не ответит:
# 1) GLM — лучший русский среди бесплатных + structured_outputs (надёжный JSON);
# 2) MiniMax M3 — 1M контекста, response_format;
# 3) Nemotron Super — компактная, structured_outputs;
# 4) Nemotron Ultra — самый крупный резерв.
# Переопределить можно секретом/переменной AI_MODEL (можно списком через запятую).
OPENROUTER_MODELS = [
    "z-ai/glm-5.2:free",
    "minimax/minimax-m3:free",
    "nvidia/nemotron-3-super-120b-a12b:free",
    "nvidia/nemotron-3-ultra-550b-a55b:free",
]

# Эмодзи, которые ИИ может поставить карточке (вне списка — эмодзи источника)
EMOJI_WHITELIST = [
    "🌍", "🔥", "⚡", "💰", "🏛️", "⚖️", "🚀", "🔬", "💊", "🎓", "⚠️", "🌐",
    "📱", "🛰️", "🏭", "🎭", "🏆", "🌊", "✈️", "🚗", "📊", "🤝", "🗳️", "🕊️",
    "⛽", "📈", "📉", "🧑‍⚖️", "🏗️", "🛡️",
]

# Слова, в которые «встроены» ключевые корни ниже — вырезаются перед
# сопоставлением: «газета» не должна быть «газом», «невролог» — евро,
# «Европа» — валютой евро, «чипсы» — чипом, «судьба» — судом, «рейсинг» — рейсом.
FALSE_STEMS = ["европ", "газет", "неврол", "чипс", "судьб", "рейсинг"]

# Тема по ключевым словам (для второй метки в оглавлении).
# Латинские ключи сопоставляются ЦЕЛИКОМ по границе слова (иначе «ai»
# ловит «airline»), кириллические — по корню («нефт» ловит «нефти/нефть»).
KEYWORD_TAGS = [
    ("конфликт",  ["войн", "удар", "обстрел", "наступлен", "боев", "перемир", "ракет", "дрон", "атак"]),
    ("политика",  ["выбор", "президен", "парламент", "министр", "выборы", "саммит", "переговор", "выставил", "депутат"]),
    ("экономика", ["инфляц", "ставк", "банк", "рынк", "доллар", "евро", "нефт", "газ", "санкц", "бюджет", "тариф", "экспорт", "импорт", "дивиденд"]),
    ("наука",     ["учен", "наук", "исследован", "открыт", "космос", "nasa", "ракет-носител", "климат"]),
    ("технологии",["ai", "искусственн", "технолог", "приложен", "чек", "cyber", "хакер", "чип", "apple", "google", "tesla"]),
    ("здоровье",  ["медиц", "врач", "болезн", "вирус", "вакцин", "пациент", "здоровь", "эпидеми"]),
    ("происшествия", ["землетрясен", "наводнен", "пожар", "авиакатастроф", "крушен", "вспышк", "авар"]),
    ("культура",  ["фильм", "преми", "фестивал", "альбом", "сериал", "книг", "выставк", "оскар"]),
    ("спорт",     ["чемпион", "матч", "кубок", "олимпиад", "турнир", "футбол", "хоккей"]),
    ("общество",  ["забастовк", "протест", "мигрант", "суд", "приговор", "закон", "школ", "больниц"]),
    ("энергетика",["энергет", "электроэнерг", "аэс", "атэс", "гэс", "тэс", "нефтепровод", "газопровод", "энергоблок"]),
    ("транспорт", ["аэропорт", "метро", "железнодорож", "ж/д", "рейс", "паром", "трамвай", "трасс", "пробк", "перелет", "перелёт"]),
]

# Управляемый словарь тем: из него ИИ выбирает метки (tags), из него же
# работает keyword-фолбэк — чипсы в оглавлении никогда не разъезжаются.
TAG_WHITELIST = [t for t, _ in KEYWORD_TAGS]

AI_SYSTEM = (
    "Ты — новостной редактор русскоязычного Telegram-канала новостей "
    "(мировые и российские события).\n"
    "На вход приходит заголовок и описание новости из RSS: мировые ленты — "
    "на английском, российские агентства — на русском.\n"
    "Верни СТРОГО один JSON-объект без markdown-обёрток:\n"
    '{"emoji": "…", "headline": "…", "lede": "…", "bullets": ["…", "…"], "tags": ["…", "…"]}\n'
    "Правила (строго):\n"
    "— Всё по-русски. Имена собственные — в устоявшейся русской передаче; организации — как принято.\n"
    "— Если вход уже на русском — не переводи и не пересказывай дословно: "
    "сожми суть своими словами, сохранив все цифры, имена и факты.\n"
    "— tags: 1–3 метки СТРОГО из списка, первая — главная тема:\n"
    "  " + ", ".join(TAG_WHITELIST) + "\n"
    "  Опиши фактическую тему новости (например, нефть/курсы → экономика, выборы/саммиты → политика, "
    "болезни/медицина → здоровье, ЧП/катастрофы → происшествия). Нет уверенной темы — []. "
    "Другие метки (в т.ч. «россия», «мир») запрещены — страна/регион и так видны по источнику.\n"
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

def keyword_tags(text, default="#мировыеновости"):
    low = " " + (text or "").lower() + " "
    for stem in FALSE_STEMS:
        low = low.replace(stem, "§")
    for tag, keys in KEYWORD_TAGS:
        for k in keys:
            pat = (r"\b" + re.escape(k) + r"\b") if k.isascii() else (r"\b" + re.escape(k))
            if re.search(pat, low):
                return "#" + tag
    return default

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


def _video_url(el):
    """URL видео из media:*/enclosure-элемента (тип video/* или .mp4/.m4v/.mov
    в адресе) или ''. Редиректы (например file.aspx у РИА) разрешаются позже,
    перед отправкой (resolve_video)."""
    if el is None:
        return ""
    url = (el.get("url") or el.get("href") or "").strip()
    if not re.match(r"^https?://", url):
        return ""
    mime = (el.get("type") or el.get("medium") or "").lower()
    if not (mime.startswith("video") or mime == "movie"
            or re.search(r"\.(mp4|m4v|mov)([?#]|$)", url, re.I)):
        return ""
    return url


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

    # видео: первое попавшееся video-вложение (enclosure/media:content)
    video = ""
    for e in all_els("enclosure") + all_els("media:content"):
        video = _video_url(e)
        if video:
            break

    when = _when(first("pubDate", "published", "updated", "date"))
    guid_el = first("guid", "id")
    return {
        "title": _txt(title_el),
        "link": link,
        "summary": summary,
        "image": img,
        "video": video,
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


OG_VID_RE = re.compile(
    r'<meta[^>]+(?:property=["\']og:video(?::secure_url|:url)?["\]'
    r'|name=["\']twitter:player:stream["\'])[^>]+content=["\']([^"\']+)["\']',
    re.IGNORECASE)
OG_VID_RE2 = re.compile(
    r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property=["\']og:video(?::secure_url|:url)?["\]'
    r'|name=["\']twitter:player:stream["\'])',
    re.IGNORECASE)


def og_media(article_url):
    """Резервные медиа со страницы статьи: (og:image, og:video). Best-effort.
    Видео берём только прямые файлы (.mp4/.m4v/.mov) — og:video часто
    указывает на iframe-плеер, который Telegram съесть не может."""
    if not article_url:
        return "", ""
    try:
        raw = http_get_bytes(article_url, timeout=10, max_len=300_000).decode("utf-8", "ignore")
    except Exception:
        return "", ""
    m = OG_RE.search(raw) or OG_RE2.search(raw)
    img = html_mod.unescape(m.group(1)).strip() if m else ""
    if img and not re.match(r"^https?://", img):
        img = ""
    vid = ""
    mv = OG_VID_RE.search(raw) or OG_VID_RE2.search(raw)
    if mv:
        vid = html_mod.unescape(mv.group(1)).strip()
        if not re.search(r"\.(mp4|m4v|mov)([?#]|$)", vid, re.I):
            vid = ""
    return img, vid


# ───────────────────────── ИИ-выжимка (OpenRouter/Gemini/Groq) ─────────────────────────

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
    tags_raw = d.get("tags")
    ai_tags = []
    if isinstance(tags_raw, list):
        for t_ in tags_raw:
            if isinstance(t_, str):
                t_ = clean_html(t_).strip().lower().lstrip("#")
                if t_ in TAG_WHITELIST and ("#" + t_) not in ai_tags:
                    ai_tags.append("#" + t_)
            if len(ai_tags) >= 3:
                break
    return {
        "headline": trim(headline, 110),
        "lede": trim(lede, 340),
        "bullets": bullets,
        "emoji": emoji if emoji in EMOJI_WHITELIST else "",
        "tags": ai_tags,
    }


def ai_card(title, summary, source_name):
    """ИИ-выжимка одной новости. None — ИИ недоступен/ответ некорректен.
    Провайдер — по префиксу ключа: "sk-or-…" → OpenRouter, "AIza…" → Gemini,
    "gsk_…" → Groq. Переменные AI_URL / AI_MODEL переопределяют вручную
    (в AI_MODEL можно перечислить несколько моделей через запятую — будут
    пробоваться по очереди). У OpenRouter бесплатных моделей лимит частоты,
    поэтому список моделей — цепочка запасных: 429/сбой → следующая модель."""
    key = (os.environ.get("GEMINI_API_KEY") or os.environ.get("GROQ_API_KEY") or "").strip()
    if not key:
        return None
    url = (os.environ.get("AI_URL") or "").strip()
    model_env = (os.environ.get("AI_MODEL") or os.environ.get("GROQ_MODEL") or "").strip().strip("\"'")
    if not url:
        if key.startswith("AIza"):
            url = GEMINI_URL
        elif key.startswith("sk-or-"):
            url = OPENROUTER_URL
        else:
            url = GROQ_URL
    if model_env:
        models = [m.strip() for m in model_env.split(",") if m.strip()]
    elif "openrouter" in url:
        models = OPENROUTER_MODELS
    elif "generativelanguage" in url:
        models = [GEMINI_MODEL_DEFAULT]
    else:
        models = [GROQ_MODEL_DEFAULT]
    headers = {"Authorization": f"Bearer {key}"}
    if "openrouter" in url:
        headers["HTTP-Referer"] = "https://eremeevmaks41-sys.github.io/soderzhanie-demo/"
        headers["X-Title"] = "soderzhanie-demo"
    user_msg = (f"Источник: {source_name}\n"
                f"Заголовок: {title}\n"
                f"Описание: {summary or '(пусто)'}")
    for i, model in enumerate(models):
        payload = {
            "model": model,
            "temperature": 0.2,
            "max_tokens": 2500,
            "messages": [
                {"role": "system", "content": AI_SYSTEM},
                {"role": "user", "content": user_msg},
            ],
        }
        if "openrouter" in url:
            # «мыслящие» модели не должны тратить лимит токенов на reasoning
            payload["reasoning"] = {"enabled": False}
        if i:
            log(f"    · пробую модель {model}…")
        try:
            resp = http_json(url, payload, headers=headers, timeout=60)
            card = _parse_ai_json((resp["choices"][0]["message"].get("content") or ""))
            if card:
                return card
            log(f"    · ИИ {model}: в ответе нет корректного JSON")
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read(300).decode("utf-8", "ignore")
            except Exception:
                pass
            log(f"    · ИИ {model}: HTTP {e.code} {e.reason} :: {detail or '(тело ответа пустое)'}")
            if e.code in (401, 402, 403):
                return None          # ключ/доступ — смена модели не поможет
        except Exception as e:
            log(f"    · ИИ {model}: {e}")
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
    if item.get("image") or item.get("video"):
        kind_media = "video" if item.get("video") else "photo"
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
            return text, kind_media
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


def tg_send_video(token, chat, video_url, caption):
    api = f"https://api.telegram.org/bot{token}/sendVideo"
    return http_json(api, {
        "chat_id": chat, "video": video_url, "caption": caption,
        "parse_mode": "HTML", "supports_streaming": True,
    })


def tg_send_video_upload(token, chat, video_url, caption, timeout=240):
    """Фолбэк: Telegram не смог забрать видео по URL — качаем сами и льём
    файлом (multipart; лимит бота 50 МБ, забираем не больше 45 МБ)."""
    import uuid
    raw = http_get_bytes(video_url, timeout=180, max_len=48_000_000)
    bnd = "----Soderzhanie" + uuid.uuid4().hex
    parts = []
    for name, val in (("chat_id", chat), ("caption", caption),
                      ("parse_mode", "HTML"), ("supports_streaming", "true")):
        parts.append((f"--{bnd}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{val}\r\n").encode("utf-8"))
    parts.append((f"--{bnd}\r\nContent-Disposition: form-data; name=\"video\"; filename=\"news.mp4\"\r\n"
                  f"Content-Type: video/mp4\r\n\r\n").encode("utf-8"))
    parts.append(raw)
    parts.append(f"\r\n--{bnd}--\r\n".encode("utf-8"))
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendVideo", data=b"".join(parts),
        method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={bnd}"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def resolve_video(url):
    """HEAD-проверка видео: (итоговый URL, content-type, content-length).
    Многие ленты дают редиректы (РИА: file.aspx → *.mp4) — идём за ними."""
    try:
        req = urllib.request.Request(url, method="HEAD", headers={
            "User-Agent": "Mozilla/5.0 (compatible; Soderzhanie/2.0)"})
        with urllib.request.urlopen(req, timeout=15) as r:
            ctype = (r.headers.get("Content-Type") or "").lower()
            try:
                clen = int(r.headers.get("Content-Length") or 0)
            except ValueError:
                clen = 0
            return r.geturl(), ctype, clen
    except Exception:
        return url, "", 0


def publish_item(token, chat, item, text, kind):
    """Видео → sendVideo (по URL или файлом); фото → sendPhoto;
    не получилось — фолбэк ниже (видео → фото → текст).
    Возвращает (ok, actual_kind, message_id)."""
    if kind == "video" and item.get("video"):
        url, ctype, clen = resolve_video(item["video"])
        ok_video = ctype.startswith("video") or not ctype   # HEAD мог не пройти — пробуем как есть
        if ok_video and clen > 45_000_000:
            log("    · видео больше 45 МБ — шлю как фото/текст")
            ok_video = False
        if ok_video:
            # по URL Telegram сам забирает файлы ≤20 МБ; больше — только файлом
            if clen == 0 or clen <= 20_000_000:
                try:
                    resp = tg_send_video(token, chat, url, text)
                    if resp.get("ok"):
                        return True, "video", resp["result"]["message_id"]
                    log(f"    · видео по URL отклонено ({resp.get('description')}) — пробую файлом")
                except Exception as e:
                    log(f"    · видео по URL не отправилось ({e}) — пробую файлом")
            else:
                log(f"    · видео ~{clen // 1_000_000} МБ — загружаю файлом")
            try:
                resp = tg_send_video_upload(token, chat, url, text)
                if resp.get("ok"):
                    return True, "video", resp["result"]["message_id"]
                log(f"    · видео файлом отклонено ({resp.get('description')}) — шлю как фото/текст")
            except Exception as e:
                log(f"    · видео файлом не отправилось ({e}) — шлю как фото/текст")
    if kind in ("video", "photo") and item.get("image"):
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


# ───────────────────── сверка оглавления с каналом ─────────────────────

SYNC_PROBE_LIMIT = 120          # сколько свежих постов проверяем за запуск


def classify_probe_error(desc):
    """Ответ Telegram на edit-пробу тем же текстом → состояние поста:
    alive (пост жив, на экране ничего не изменилось), dead (удалён),
    chat (канал недоступен — сверку надо прервать), unknown (не ясен)."""
    low = (desc or "").lower()
    if "chat not found" in low or "chat_id is invalid" in low:
        return "chat"
    if "message is not modified" in low:
        return "alive"
    if "message to edit not found" in low or ("not found" in low and "message" in low):
        return "dead"
    if "no text in the message" in low or "no caption" in low:
        return "alive"            # есть, но другой тип — переспросим эмбедом
    return "unknown"


def probe_alive_api(token, chat, msg_id, meta):
    """editMessageText/editMessageCaption ТОМ ЖЕ тексту: пост жив →
    Telegram отвечает «message is not modified» (контент не меняется);
    удалён → «message to edit not found»."""
    kind = meta.get("kind") or "text"
    if kind == "text":
        api, field = "editMessageText", "text"
    else:
        api, field = "editMessageCaption", "caption"
    payload = {"chat_id": "@" + chat, "message_id": msg_id, field: meta["text"],
               "parse_mode": "HTML"}
    if kind == "text":
        payload["disable_web_page_preview"] = True
    try:
        resp = http_json(f"https://api.telegram.org/bot{token}/{api}", payload, timeout=20)
        return "alive" if resp.get("ok") else "unknown"
    except urllib.error.HTTPError as e:
        desc = ""
        try:
            desc = e.read(300).decode("utf-8", "ignore")
        except Exception:
            pass
        return classify_probe_error(desc)
    except Exception:
        return "unknown"


def probe_alive_embed(chat, msg_id):
    """Фолбэк для постов без сохранённого текста: t.me-эмбед удалённого поста
    содержит tgme_widget_message_error («Post not found»), живого — нет."""
    try:
        raw = http_get_bytes(f"https://t.me/{chat}/{msg_id}?embed=1&mode=tme",
                             timeout=10, max_len=150_000).decode("utf-8", "ignore")
    except Exception:
        return "unknown"
    low = raw.lower()
    if "tgme_widget_message_error" in low:
        return "dead"
    if "tgme_widget_message_date" in low or "tgme_widget_message_text" in low:
        return "alive"
    return "unknown"


def sync_deleted(token, chat, posts, texts):
    """Сверяет оглавление с каналом: последние SYNC_PROBE_LIMIT постов
    опрашиваются edit-методом (есть сохранённый текст) или t.me-эмбедом.
    Удалённые в Telegram вычищаются из posts.json (и из texts).
    Возвращает число вычищенных (0 — ничего, -1 — канал недоступен)."""
    with_id = [p for p in posts if p.get("id")]
    dead = []
    for p in with_id[-SYNC_PROBE_LIMIT:]:
        meta = texts.get(str(p["id"]))
        if meta and meta.get("text"):
            state = probe_alive_api(token, chat, p["id"], meta)
        else:
            state = probe_alive_embed(chat, p["id"])
        if state == "chat":
            log("!! канал недоступен для бота — сверка прервана, ничего не удаляю")
            return -1
        if state == "dead":
            dead.append(p["id"])
            log(f"  × пост удалён в канале → вычищаю из оглавления: id={p['id']} «{trim(p.get('title',''), 50)}»")
        elif state == "unknown":
            log(f"  · сверка: состояние id={p['id']} не выяснено — не трогаю")
    if dead:
        dead_set = set(dead)
        posts[:] = [p for p in posts if p.get("id") not in dead_set]
        for mid in dead:
            texts.pop(str(mid), None)
    return len(dead)


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


def merge_tags(base, card, text):
    """Итоговые метки поста: метка источника (первая) + темы от ИИ (из
    TAG_WHITELIST), всего ≤3. ИИ не дал тем → keyword-фолбэк; фолбэк
    не дублирует метку источника."""
    base = list(base or ["#мировыеновости"])
    picked = [t for t in (card.get("tags") or []) if t not in base]
    tags = (base + picked)[:3]
    if len(tags) == len(base):
        extra = keyword_tags(text, default=base[0])
        if extra not in tags:
            tags.append(extra)
    return tags


def interleave_by_source(candidates, offset=0):
    """Честная ротация источников: внутри источника — по свежести,
    между источниками — round-robin со сдвигом offset (сдвиг меняется
    от запуска к запуску, т.к. считается от часа суток). Без ротации
    источник с самым частым потоком занял бы весь дневной лимит."""
    groups = {}
    for c in candidates:
        groups.setdefault(c["source"].get("name", "?"), []).append(c)
    for g in groups.values():
        g.sort(key=lambda x: x["dt"], reverse=True)
    names = sorted(groups)
    if not names:
        return []
    offset %= len(names)
    names = names[offset:] + names[:offset]
    out = []
    for idx in range(max(len(g) for g in groups.values())):
        for n in names:
            g = groups[n]
            if idx < len(g):
                out.append(g[idx])
    return out


def today_count(posts):
    """Сколько постов опубликовано сегодня (MSK) — для дневного лимита."""
    today = datetime.now(MSK).strftime("%Y-%m-%d")
    return sum(1 for p in posts if p.get("date") == today)


def save_state(posts_doc, seen_doc, posts, chat, args):
    """Единая запись результатов запуска: оглавление + seen.json
    (в нём же хранятся тексты постов для будущих edit-проб сверки)."""
    posts.sort(key=lambda p: (p.get("date", ""), p.get("time", "")), reverse=True)
    posts_doc["posts"] = posts[:1500]
    posts_doc["updated_at"] = datetime.now(MSK).isoformat(timespec="seconds")
    if isinstance(posts_doc.get("channel"), dict) and chat:
        posts_doc["channel"]["url"] = f"https://t.me/{chat}"
    save_json(args.posts, posts_doc)
    seen_doc["seen"] = dict(sorted((seen_doc.get("seen") or {}).items(),
                                   key=lambda kv: kv[1], reverse=True)[:5000])
    texts = seen_doc.get("texts") or {}
    if len(texts) > 400:
        seen_doc["texts"] = dict(list(texts.items())[-300:])
    save_json(args.seen, seen_doc)


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
    texts = seen_doc.setdefault("texts", {})   # тексты постов для edit-пробы сверки

    token = os.environ.get("BOT_TOKEN", "")
    chat = (os.environ.get("CHANNEL_USERNAME", "") or "").strip()
    if args.mode == "publish" and (not token or not chat):
        log("!! publish требует BOT_TOKEN и CHANNEL_USERNAME (GitHub Secrets)")
        return 1
    chat = chat.lstrip("@").replace("https://t.me/", "")

    # Сверка оглавления с каналом: посты, удалённые в Telegram, вычищаются
    # из posts.json (работает и при --max 0 — «только сверка»).
    removed = 0
    if args.mode == "publish":
        removed = max(0, sync_deleted(token, chat, posts, texts))

    published_today = today_count(posts)
    if published_today >= args.daily_cap:
        log(f"Дневной лимит исчерпан ({published_today}/{args.daily_cap}) — "
            "новые посты не публикую"
            + (f"; вычищено удалённых: {removed}" if removed else ""))

    log(f"Источников: {len(sources)}; в оглавлении {len(posts)} постов; "
        f"сегодня уже {published_today}/{args.daily_cap}")
    candidates = []
    for src in sources:
        candidates += pick(fetch_feed(src), src, seen, existing_srcs,
                           existing_titles, args.max_per_source)
    # Ротация источников: сдвиг = номер 2-часового слота суток, поэтому
    # каждый запуск первым опрашивает другой источник (см. interleave_by_source).
    rotation = (datetime.now(MSK).hour // 2) % max(1, len(sources))
    candidates = interleave_by_source(candidates, rotation)
    log(f"Ротация источников: первым в очереди №{rotation + 1} из {len(sources)}")
    room = args.daily_cap - published_today
    candidates = candidates[: max(0, min(args.max, room))]
    log(f"К публикации отобрано: {len(candidates)}")
    if not candidates:
        log("Новых новостей нет"
            + (f"; вычищено удалённых постов: {removed}" if removed else ""))
        if args.mode == "publish" and removed:
            save_state(posts_doc, seen_doc, posts, chat, args)
            log(f"Оглавление обновлено: {len(posts_doc['posts'])} постов")
        return 0

    ai_ready = bool((os.environ.get("GEMINI_API_KEY") or os.environ.get("GROQ_API_KEY") or "").strip())
    if args.mode == "publish" and not ai_ready:
        log("!! ключ ИИ не задан — публиковать сырые анонсы без выжимки "
            "не буду. Добавьте ключ OpenRouter (openrouter.ai/keys, "
            "бесплатно) в секрет GROQ_API_KEY.")

    added = 0
    for item in candidates:
        date_s, time_s = item["dt"].strftime("%Y-%m-%d"), item["dt"].strftime("%H:%M")

        # медиа: RSS-вложения → og:image/og:video статьи (best-effort)
        if not item.get("image") and not item.get("video") and not args.no_og_image:
            item["image"], item["video"] = og_media(item["src"])

        card = ai_card(item["title"], item["summary"], item["source"].get("name", ""))
        if card is None:
            if args.mode == "publish":
                log(f"  × без ИИ-выжимки не публикую: {trim(item['title'], 60)}")
                continue                      # hash НЕ записан → ретрай на следующем запуске
            card = fallback_card(item)

        tags = merge_tags(item["source"].get("tags"), card,
                          item["title"] + " " + item["summary"] + " " + card.get("lede", ""))

        text, kind = compose(item, card)

        if args.mode == "dry":
            media = " (с фото)" if kind == "photo" else (" (видео)" if kind == "video" else "")
            log(f"\n--- DRY ({date_s} {time_s}){media} ---\n{text}\n")
            log(f"    метки: {' '.join(tags)}")
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
        texts[str(msg_id)] = {"kind": kind, "text": text}   # для edit-пробы будущих сверок
        added += 1
        log(f"  ✓ {kind}: {trim(card['headline'], 60)} → t.me/{chat}/{msg_id}")

    if args.mode == "publish" and (added or removed):
        save_state(posts_doc, seen_doc, posts, chat, args)
        log(f"\nГотово: {added} новых, {removed} вычищено; в оглавлении {len(posts_doc['posts'])}")
    elif args.mode == "publish":
        log("\nОглавление без изменений")
    elif args.mode == "dry":
        log(f"\nDRY-режим: было бы опубликовано {added}; файлы не менялись")
    return 0

if __name__ == "__main__":
    sys.exit(main())
