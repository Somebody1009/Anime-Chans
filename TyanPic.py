import os
import json
import random
import logging
import asyncio
from datetime import date, datetime
from uuid import uuid4
import difflib
import requests
import xml.etree.ElementTree as ET
import re
from urllib.parse import urlparse

import aiohttp
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    InlineQueryResultPhoto,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    InlineQueryHandler,
    ContextTypes,
    filters,
)
from telegram.error import BadRequest
from cache import CUR, DB
from decouple import config
from stats import incr, save, load
from prefetch import prefetch, _insert, prefetch_wallhaven
from aiohttp import ClientTimeout
from telegram.constants import ChatAction

# ——— Configuration & Logging ———
logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN    = config("TELEGRAM_TOKEN")
WALLHAVEN_API_KEY = config("WALLHAVEN_API_KEY")

# ——— Data files ———
DATA_DIR    = os.path.join(os.path.dirname(__file__), "data")
os.makedirs(DATA_DIR, exist_ok=True)
FAVS_FILE   = os.path.join(os.path.dirname(__file__), "favorites.json")
SUBS_FILE   = os.path.join(DATA_DIR, "subscribers.json")
STATS_FILE  = os.path.join(DATA_DIR, "stats.json")
VIEWED_FILE = os.path.join(DATA_DIR, "viewed.json")
REPORTS_FILE = os.path.join(os.path.dirname(__file__), "reports.json")
PENDING_ARTS_FILE = os.path.join(DATA_DIR, "pending_arts.json")
USER_ARTS_FILE = os.path.join(DATA_DIR, "user_arts.json")
ACTIVE_USERS_FILE = os.path.join(DATA_DIR, "active_users.json")

# ——— Admins ———
ADMIN_IDS = {810423029}  # ваші Telegram ID для broadcast

# ——— Persistence helpers ———
def load_json(path, default):
    if os.path.exists(path):
        return json.load(open(path, encoding="utf-8"))
    return default

def save_json(path, data):
    json.dump(data, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

favorites   = load_json(FAVS_FILE, {})
subscribers = load_json(SUBS_FILE, {})
stats       = load_json(STATS_FILE, {
    "images_sent": 0,
    "favorites_added": 0,
    "favorites_by_tag": {},
    "favorites_by_tag_date": {}
})
viewed      = load_json(VIEWED_FILE, {})
pending_arts = load_json(PENDING_ARTS_FILE, [])
user_arts = load_json(USER_ARTS_FILE, [])

# ——— In-memory state ———
last_image = {}  # chat_id → URL
last_tag   = {}  # chat_id → tag
user_lang  = {}  # chat_id → 'ua' or 'en'

CATEGORIES = ["waifu","neko","hug","smile","kiss","pat","wink","cuddle"]
APIS       = ["waifu.pics", "danbooru", "wallhaven", "safebooru", "konachan"]

pending_reports = set()  # chat_id тих, хто зараз пише репорт
chat_ended = set()  # chat_id, де чат завершено

# ——— Localization ———
LOCALES = {
    "ua": {
        "welcome": "Ласкаво просимо! Оберіть дію або /help для інструкцій:",
        "search_prompt": "Введіть тег англійською (/waifu, /neko, ... /cuddle) або оберіть категорію:",
        "no_results": "За тегом «{tag}» нічого не знайдено.",
        "followup": "Наступну — /next\nТой самий тег — /same\nВ улюблені — /like\nДо інструкцій — /help",
        "ended": "Чат завершено. Щоб знову — /start",
        "no_prev_tag": "Немає попереднього тегу. Почніть /start",
        "help": (
            """
/start — меню
/next — нова картинка за останнім тегом
/same — ще одна картинка з тим самим тегом
/like — додати в улюблені
/favorites — ваші лайки
/random_fav — випадковий улюблений
/trending — топ-5 тегів
/similar <тег> — схожі теги
/stats — ваша статистика
/top_today — топ за сьогодні
/subscribe — підписка на розсилку
/unsubscribe — відписка від розсилки
/clearstats — очистити статистику
/clearlike — очистити лайки
/clearfavorites — очистити улюблені
/cleartrendings — очистити тренди
/cleartop_today — очистити топ за сьогодні
/langua — українська мова
/langen — англійська мова
/report — поскаржитись на картинку
/badges — ваші досягнення
/swap_status — статус обміну
/sendart — надіслати свою картинку на модерацію
/arts — переглянути роботи користувачів
"""
        ),
        "lang_set": "Мову змінено на українську",
        "lang_usage": "Використовуйте /langen для використання бота англійською мовою або /langua для використання бота українською мовою",
        "menu": (
            "🔍 — Пошук/категорії\n"
            "❤️ — Ваші лайки\n"
            "🎲 — Випадковий улюблений\n"
            "📈 — Трендові теги\n"
            "📊 — Статистика\n"
            "🌐 — Змінити мову\n"
            "❌ — Завершити чат\n"
            "🔔 — Підписка на розсилку"
        ),
        "random_fav_caption": "Випадковий улюблений ❤",
        "like_added": "Додано до улюблених ❤",
        "already_liked": "Вже в улюблених ❤",
        "trending_title": "🔥 Трендові теги:",
        "stats_title": "📊 Ваша статистика:",
        "stats_sent": "• Відправлено картинок: {sent}",
        "stats_fav": "• Улюблених картинок: {fav}",
        "stats_top_tag": "• Найпопулярніший тег: {tag}",
        "subscribe_prompt": (
            "Вкажіть інтервал (хвилини) і кількість, напр: /subscribe 30 2\n"
            "Або: /subscribe daily 9 3 (щодня о 9:00 по 3 картинки)\n"
            "Приклад: /subscribe 60 1"
        ),
        "subscribe_confirm": "Ви підписані на розсилку кожні {interval} хв по {count} картинок(ки).",
        "daily_subscribe_confirm": "Ви підписані на щоденну розсилку о {hour}:00 по {count} картинок(ки).",
        "scheduled_caption": "Планова розсилка ({api})",
        "daily_caption": "Щоденна розсилка ({api})",
        "no_likes": "У вас ще немає лайків ❤",
        "unknown_tag": "Невідомий тег.",

        "all_viewed": "Всі картинки за тегом «{tag}» вже переглянуті! Спробуйте інший тег.",
        "choose_language": "Виберіть мову: /langen або /langua",
        "unsubscribed": "Ви відписалися від розсилки.",
        "not_subscribed": "Ви не підписані.",
        "already_subscribed": "Ви вже підписані на розсилку. Щоб змінити налаштування, вкажіть інтервал і кількість, напр: /subscribe 30 2\nАбо: /subscribe daily 9 3 (щодня о 9:00 по 3 картинки)\nПриклад: /subscribe 60 1",
        "unsubscribed_hint": "Ви відписалися від розсилки. Щоб підписатися знову, використайте /subscribe",
        "no_similar_tags": "Схожих тегів не знайдено.",
        "loading": "Завантаження зображення…",
        "img_not_found": "Не вдалося знайти зображення.",
        "sendart_prompt": "Надішліть фото, яке хочете додати у спільний альбом (можна з підписом)",
        "art_sent": "Вашу картинку надіслано на модерацію!",
        "art_approved": "Ваша картинка схвалено модератором!",
        "art_rejected": "Вашу картинку відхилено модератором.",
        "no_user_arts": "Немає схвалених картинок від інших користувачів.",
        "from_user": "Від користувача {user_id}\n{caption}",
        "swap_send": "Надішліть зображення для обміну.",
        "swap_wait": "Чекаємо іншого учасника…",
        "swap_done": "🎁 Ось ваш обмін!",
        "badges_none": "У вас поки немає бейджів 😅",
        "badges_list": "Ваші бейджі:\n{badges}",
        "report_prompt": "Опишіть проблему одним повідомленням. Ваш текст буде надіслано адміну.",
        "report_sent": "Дякую! Ваше повідомлення надіслано адміну.",
        "photo_received": "Фото отримано, але воно не підпадає під жодну дію.",
        "clearstats_done": "Статистика очищена.",
        "clearlike_done": "Ваші лайки очищено.",
        "clearfavorites_done": "Ваші улюблені очищено.",
        "cleartrendings_done": "Трендові теги очищено.",
        "cleartop_today_done": "Топ за сьогодні очищено.",
        "top_today_caption": "Тег: {tag} ({count} лайків)",
        "top_today_choose": "Обери дію:",
        "top_today_yesterday": "Вчора",
        "top_today_week": "Останні 7 днів",
        "similar_prompt": "Вкажіть тег: /similar <tag>",
        "similar_found": "Схожі теги: {tags}",
        "similar_none": "Схожих тегів не знайдено.",
        "maybe_you_meant": "Можливо ви мали на увазі: {tag}",
        "swap_status": "Зараз у черзі: {count} людей.",
        "no_image_to_like": "Немає картинки для додавання в улюbлені.",
        "no_trending_tags": "Ще немає трендових тегів.",
    },
    "en": {
        "welcome": "Welcome! Choose an action or /help for instructions:",
        "search_prompt": "Enter a tag in English (/waifu, /neko, ... /cuddle) or choose a category:",
        "no_results": "No results for '{tag}'.",
        "followup": "Next — /next\nSame tag — /same\nTo favorites — /like\nFor instructions — /help",
        "ended": "Chat ended. To start again — /start",
        "no_prev_tag": "No previous tag. Start with /start",
        "help": (
            """
/start — menu
/next — new art with the last tag
/same — another art with the same tag
/like — add to favorites
/favorites — your likes
/random_fav — random favorite
/trending — top-5 tags
/similar <tag> — similar tags
/stats — your stats
/top_today — today's top
/subscribe — subscribe to delivery
/unsubscribe — unsubscribe from delivery
/clearstats — clear stats
/clearlike — clear likes
/clearfavorites — clear favorites
/cleartrendings — clear trendings
/cleartop_today — clear top today
/langua — Ukrainian language
/langen — English language
/report — report an art
/badges — your achievements
/swap_status — swap status
/sendart — submit your art for moderation
/arts — view user-submitted arts
"""
        ),
        "lang_set": "Language set to English",
        "lang_usage": "Use /langen to use the bot in English or /langua to use the bot in Ukrainian",
        "menu": (
            "🔍 — Search/Categories\n"
            "❤️ — Your likes\n"
            "🎲 — Random favorite\n"
            "📈 — Trending tags\n"
            "📊 — Stats\n"
            "🌐 — Change language\n"
            "❌ — End chat\n"
            "🔔 — Subscribe"
        ),
        "random_fav_caption": "Random favorite ❤",
        "like_added": "Added to favorites ❤",
        "already_liked": "Already in favorites ❤",
        "trending_title": "🔥 Trending tags:",
        "stats_title": "📊 Your stats:",
        "stats_sent": "• Images sent: {sent}",
        "stats_fav": "• Favorites: {fav}",
        "stats_top_tag": "• Top tag: {tag}",
        "subscribe_prompt": (
            "Specify interval (minutes) and count, e.g.: /subscribe 30 2\n"
            "Or: /subscribe daily 9 3 (daily at 9:00, 3 images)\n"
            "Example: /subscribe 60 1"
        ),
        "subscribe_confirm": "You are subscribed to receive {count} image(s) every {interval} min.",
        "daily_subscribe_confirm": "You are subscribed to daily delivery at {hour}:00, {count} image(s).",
        "scheduled_caption": "Scheduled delivery ({api})",
        "daily_caption": "Daily delivery ({api})",
        "no_likes": "You have no likes yet ❤",
        "unknown_tag": "Unknown tag.",
        "all_viewed": "All images for tag '{tag}' have already been viewed! Try another tag.",
        "choose_language": "Choose language: /langen or /langua",
        "unsubscribed": "You have unsubscribed from scheduled delivery.",
        "not_subscribed": "You are not subscribed.",
        "already_subscribed": "You are already subscribed. To change settings, specify interval (minutes) and count, e.g.: /subscribe 30 2\nOr: /subscribe daily 9 3 (daily at 9:00, 3 images)\nExample: /subscribe 60 1",
        "unsubscribed_hint": "You have unsubscribed. To subscribe again, use /subscribe",
        "no_similar_tags": "No similar tags found.",
        "loading": "Loading image…",
        "img_not_found": "Could not find an image.",
        "sendart_prompt": "Send a photo you want to add to the shared album (you can add a caption)",
        "art_sent": "Your art has been sent for moderation!",
        "art_approved": "Your art has been approved by the moderator!",
        "art_rejected": "Your art has been rejected by the moderator.",
        "no_user_arts": "No approved arts from other users.",
        "from_user": "From user {user_id}\n{caption}",
        "swap_send": "Send an image for swap.",
        "swap_wait": "Waiting for another participant…",
        "swap_done": "🎁 Here is your swap!",
        "badges_none": "You have no badges yet 😅",
        "badges_list": "Your badges:\n{badges}",
        "report_prompt": "Describe the problem in one message. Your text will be sent to the admin.",
        "report_sent": "Thank you! Your message has been sent to the admin.",
        "photo_received": "Photo received, but it does not match any action.",
        "clearstats_done": "Stats cleared.",
        "clearlike_done": "Your likes have been cleared.",
        "clearfavorites_done": "Your favorites have been cleared.",
        "cleartrendings_done": "Trending tags cleared.",
        "cleartop_today_done": "Today's top cleared.",
        "top_today_caption": "Tag: {tag} ({count} likes)",
        "top_today_choose": "Choose an action:",
        "top_today_yesterday": "Yesterday",
        "top_today_week": "Last 7 days",
        "similar_prompt": "Specify a tag: /similar <tag>",
        "similar_found": "Similar tags: {tags}",
        "similar_none": "No similar tags found.",
        "maybe_you_meant": "Maybe you meant: {tag}",
        "swap_status": "Currently in queue: {count} people.",
        "no_image_to_like": "No image to add to favorites.",
        "no_trending_tags": "No trending tags yet.",
    }
}

def t(chat_id, key, **kw):
    lang = user_lang.get(str(chat_id), "en")  # Тепер англійська за замовчуванням
    return LOCALES[lang][key].format(**kw)

# ——— aiohttp session ———
_session = None
async def ensure_session():
    global _session
    if _session is None:
        _session = aiohttp.ClientSession()

# ——— Image fetchers ———
async def get_waifu_pics(tag):
    await ensure_session()
    try:
        r = await _session.get(f"https://api.waifu.pics/sfw/{tag}", timeout=10)
        r.raise_for_status()
        return (await r.json())["url"]
    except:
        return None

async def get_danbooru(tag):
    await ensure_session()
    url = f"https://danbooru.donmai.us/posts.json?tags={tag}+rating:safe+order:random&limit=1"
    try:
        r = await _session.get(url, timeout=10); r.raise_for_status()
        posts = await r.json()
        return posts[0]["file_url"] if posts else None
    except:
        return None

async def get_wallhaven(tag):
    await ensure_session()
    url = (
        f"https://wallhaven.cc/api/v1/search?q={tag}"
        f"&categories=1&purity=1&sorting=random&atleast=1920x1080"
        f"&apikey={WALLHAVEN_API_KEY}"
    )
    try:
        r = await _session.get(url, timeout=10); r.raise_for_status()
        data = await r.json(); hits = data.get("data",[])
        return f"https://wallhaven.cc{hits[0]['path']}" if hits else None
    except:
        return None

async def get_safebooru(tag):
    url = (
        "https://safebooru.org/index.php"
        f"?page=dapi&s=post&q=index&limit=100&tags={tag}"
    )
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=10) as resp:
                if resp.status != 200:
                    return None, None
                xml = await resp.text()
                root = ET.fromstring(xml)
                posts = root.findall("post")
                if not posts:
                    return None, None
                import random
                post = random.choice(posts)
                url = f"https://safebooru.org{post.attrib.get('file_url', '')}"
                if not url:
                    return None, None
                return url, "safebooru"
    except Exception as e:
        print(f"Safebooru error: {e}")
        return None, None

async def get_konachan(tag):
    url = (
        "https://konachan.net/post.json"
        f"?limit=100&tags={tag}+rating:safe"
    )
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=10) as resp:
                if resp.status != 200:
                    return None, None
                posts = await resp.json()
                if not posts:
                    return None, None
                import random
                post = random.choice(posts)
                file_url = post.get("file_url")
                if not file_url:
                    return None, None
                return file_url, "konachan"
    except Exception as e:
        print(f"Konachan error: {e}")
        return None, None

async def fetch_image(tag: str):
    apis = [
        ("waifu.pics",    get_waifu_pics),
        ("safebooru",     get_safebooru),
        ("danbooru",      get_danbooru),
        ("wallhaven",     get_wallhaven),
    ]
    for name, fn in apis:
        try:
            url = await asyncio.wait_for(fn(tag), timeout=3.0)
            if url and await validate_url(url):
                return url, name
        except asyncio.TimeoutError:
            continue
        except Exception:
            continue
    return None, None

# ——— Keyboards ———
def kb_main(chat_id):
    lang = user_lang.get(str(chat_id), "en")
    if lang == "ua":
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("🔍 Пошук", callback_data="START"),
             InlineKeyboardButton("❤️ Лайки", callback_data="SHOW_FAVS"),
             InlineKeyboardButton("🎲 Випадковий", callback_data="RANDOM_FAV")],
            [InlineKeyboardButton("🖼 Галерея", callback_data="SHOW_USER_ARTS"),
             InlineKeyboardButton("➕ Додати арт", callback_data="SEND_USER_ART")],
            [InlineKeyboardButton("📈 Тренди", callback_data="TRENDING"),
             InlineKeyboardButton("📊 Статистика", callback_data="STATS")],
            [InlineKeyboardButton("🌐 Мова", callback_data="LANG")],
            [InlineKeyboardButton("🔔 Підписка", callback_data="SUBSCRIBE"),
             InlineKeyboardButton("⚠️ Поскаржитись", callback_data="REPORT")],
        ])
    else:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("🔍 Search", callback_data="START"),
             InlineKeyboardButton("❤️ Likes", callback_data="SHOW_FAVS"),
             InlineKeyboardButton("🎲 Random", callback_data="RANDOM_FAV")],
            [InlineKeyboardButton("🖼 Gallery", callback_data="SHOW_USER_ARTS"),
             InlineKeyboardButton("➕ Add Art", callback_data="SEND_USER_ART")],
            [InlineKeyboardButton("📈 Trending", callback_data="TRENDING"),
             InlineKeyboardButton("📊 Stats", callback_data="STATS")],
            [InlineKeyboardButton("🌐 Language", callback_data="LANG")],
            [InlineKeyboardButton("🔔 Subscribe", callback_data="SUBSCRIBE"),
             InlineKeyboardButton("⚠️ Report", callback_data="REPORT")],
        ])

def kb_cats():
    kb, row = [], []
    for i, cat in enumerate(CATEGORIES, 1):
        row.append(InlineKeyboardButton(cat, callback_data=f"TAG|{cat}"))
        if i % 4 == 0:
            kb.append(row); row = []
    if row:
        kb.append(row)
    return InlineKeyboardMarkup(kb)

def kb_lang():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Українська 🇺🇦", callback_data="SET_LANG_UA"),
         InlineKeyboardButton("English 🇬🇧", callback_data="SET_LANG_EN")]
    ])

# ——— Art Swap & Achievements ———
SWAP_POOL_FILE = os.path.join(DATA_DIR, "data_swap_pool.json")
ACHIEVEMENTS_FILE = os.path.join(DATA_DIR, "data_achievements.json")

swap_pool = load_json(SWAP_POOL_FILE, {})
achievements = load_json(ACHIEVEMENTS_FILE, {})

BADGES = {
    "first_art": ("🎨 Перша картинка", "send_art"),
    "first_like": ("👍 Перший лайк", "like"),
    "arts_viewed": [
        (10, "👀 10 картинок"), (50, "👁 50 картинок"), (100, "🧿 100 картинок")
    ],
    "collector": ("🏆 Колекціонер", "all_tag"),
    "first_swap": ("🔄 Перший обмін", "swap"),
    "moderator": ("🛡️ Модератор", "moderate"),
    "gallery": (5, "🖼 Галерист"),
    "trendsetter": (10, "🔥 Творець тренду"),
    "favorite": (50, "🌟 Улюбленець"),
    "multigenre": (5, "🎭 Мультижанр"),
    "tagmaster": (3, "🏷️ Тег-майстер"),
    "first_caption": ("✍️ Перший коментар", "caption"),
    "earlybird": ("🌅 Ранній птах", "early"),
    "nightowl": ("🌙 Нічна сова", "night"),
    "dailyfan": (7, "📅 Щоденний фан"),
    "secret_tag": ("🕵️‍♂️ Секретний тег", "secret"),
    "memmaster": ("😂 Мем-майстер", "meme"),
}

def update_achievements(cid, event=None, extra=None):
    global achievements
    cid = str(cid)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if cid not in achievements:
        achievements[cid] = []
    user_ach = achievements[cid]

    def add_badge(name):
        if not any(a["achievement"] == name for a in user_ach):
            user_ach.append({"achievement": name, "date": now})

    # 1. Перший арт
    if event == "send_art":
        add_badge(BADGES["first_art"][0])

    # 2. Перший лайк
    if event == "like" and extra == 1:
        add_badge(BADGES["first_like"][0])

    # 3. Переглянуто N арту
    if event == "view_art":
        for n, name in BADGES["arts_viewed"]:
            if extra == n:
                add_badge(name)

    # 4. Колекціонер (всі арти з тегу)
    if event == "all_tag":
        add_badge(BADGES["collector"][0])

    # 5. Перший обмін
    if event == "swap":
        add_badge(BADGES["first_swap"][0])

    # 6. Модератор
    if event == "moderate":
        add_badge(BADGES["moderator"][0])

    # 7. Галерист (5 схвалених арту)
    if event == "gallery" and extra == 5:
        add_badge(BADGES["gallery"][1])

    # 8. Творець тренду (арт отримав 10+ лайків)
    if event == "trend" and extra >= BADGES["trendsetter"][0]:
        add_badge(BADGES["trendsetter"][1])

    # 9. Улюбленець (арт переглянули 50+ разів)
    if event == "favorite" and extra >= BADGES["favorite"][0]:
        add_badge(BADGES["favorite"][1])

    # 10. Мультижанр (5 різних тегів)
    if event == "multigenre" and extra == BADGES["multigenre"][0]:
        add_badge(BADGES["multigenre"][1])

    # 11. Тег-майстер (3 різних тегів)
    if event == "tagmaster" and extra == BADGES["tagmaster"][0]:
        add_badge(BADGES["tagmaster"][1])

    # 12. Перший коментар
    if event == "caption":
        add_badge(BADGES["first_caption"][0])

    # 13. Ранній птах
    if event == "early":
        add_badge(BADGES["earlybird"][0])

    # 14. Нічна сова
    if event == "night":
        add_badge(BADGES["nightowl"][0])

    # 15. Щоденний фан (7 днів підряд)
    if event == "dailyfan" and extra == BADGES["dailyfan"][0]:
        add_badge(BADGES["dailyfan"][1])

    # 16. Секретний тег
    if event == "secret":
        add_badge(BADGES["secret_tag"][0])

    # 17. Мем-майстер
    if event == "meme":
        add_badge(BADGES["memmaster"][0])

    save_json(ACHIEVEMENTS_FILE, achievements)

# ——— Handlers ———
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    if cid in chat_ended:
        chat_ended.remove(cid)
    username = update.effective_user.username
    add_active_user(cid, username, active_users)
    if cid not in user_lang:
        user_lang[cid] = "en"
    if cid not in active_users:
        active_users.append({"id": cid, "username": username or ""})
        save_active_users(active_users)
    welcome_text = f"{t(cid, 'welcome')}\n\n{t(cid, 'menu')}"
    await update.message.reply_text(welcome_text, reply_markup=kb_main(cid))

async def help_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    await update.message.reply_text(t(cid, "help"))

async def lang_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    await update.message.reply_text(t(cid, "lang_usage"))

async def inline_q(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.inline_query.query.strip()
    if not query:
        return
    url, api = await fetch_image(query.replace(" ", "_").lower())
    if url:
        result = InlineQueryResultPhoto(
            id=str(uuid4()),
            photo_url=url,
            thumbnail_url=url,
            caption=f"{query} ({api})"
        )
        await ctx.bot.answer_inline_query(update.inline_query.id, [result], cache_time=0)

async def cb_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data
    cid = q.message.chat.id

    if cid in chat_ended:
        await ctx.bot.send_message(cid, t(cid, "ended"))
        return

    if data == "START":
        await ctx.bot.send_message(cid, t(cid, "search_prompt"), reply_markup=kb_cats())
    elif data == "SUBSCRIBE":
        if str(cid) in subscribers:
            await ctx.bot.send_message(cid, t(cid, "already_subscribed"))
        else:
            await ctx.bot.send_message(cid, t(cid, "subscribe_prompt"))
    elif data == "TAG|":
        pass  # (not used)
    elif data.startswith("TAG|"):
        tag = data.split("|", 1)[1]
        await on_tag(update, ctx, tag)
    elif data == "SHOW_FAVS":
        favs = favorites.get(str(cid), [])
        if not favs:
            await ctx.bot.send_message(cid, t(cid, "no_likes"))
        else:
            media = [InputMediaPhoto(u) for u in favs[:10]]
            await ctx.bot.send_media_group(cid, media)
    elif data == "RANDOM_FAV":
        favs = favorites.get(str(cid), [])
        if not favs:
            await ctx.bot.send_message(cid, t(cid, "no_likes"))
        else:
            url = random.choice(favs)
            await ctx.bot.send_photo(cid, photo=url, caption=t(cid, "random_fav_caption"))
    elif data == "TRENDING":
        tag_stats = stats.get("favorites_by_tag", {})
        if not tag_stats:
            await ctx.bot.send_message(cid, t(cid, "no_trending_tags"))
            return
        # Сортуємо за кількістю лайків
        top = sorted(tag_stats.items(), key=lambda x: x[1], reverse=True)[:5]
        text = t(cid, "trending_title") + "\n"
        for i, (tag, count) in enumerate(top, 1):
            text += f"{i}. {tag} ({count})\n"
        await ctx.bot.send_message(cid, text)
    elif data == "STATS":
        await stats_cmd(update, ctx)
    elif data == "LANG":
        await ctx.bot.send_message(cid, t(cid, "choose_language"), reply_markup=kb_lang())
    elif data == "SET_LANG_UA":
        user_lang[str(cid)] = "ua"
        await ctx.bot.send_message(cid, LOCALES["ua"]["lang_set"])
    elif data == "SET_LANG_EN":
        user_lang[str(cid)] = "en"
        await ctx.bot.send_message(cid, LOCALES["en"]["lang_set"])
    elif data.startswith("SHOW_TAG|"):
        tag = data.split("|", 1)[1]
        await on_tag(update, ctx, tag)
    elif data == "REPORT":
        await report_cmd(update, ctx)
    elif data == "SHOW_USER_ARTS":
        await arts_cmd(update, ctx)
    elif data == "SEND_USER_ART":
        await sendart_cmd(update, ctx)

async def msg_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    if cid in chat_ended:
        chat_ended.remove(cid)
    if cid in pending_reports:
        # Надіслати адміну
        admin_id = list(ADMIN_IDS)[0]  # твій Telegram ID
        text = f"⚠️ Report from @{update.effective_user.username or cid} ({cid}):\n{update.message.text}" if user_lang.get(str(cid), "en") == "en" else f"⚠️ Репорт від @{update.effective_user.username or cid} ({cid}):\n{update.message.text}"
        try:
            await ctx.bot.send_message(admin_id, text)
        except Exception as e:
            print(f"Не вдалося надіслати адміну: {e}")
        await update.message.reply_text(t(cid, "report_sent"))
        # Зберегти у reports.json
        report_entry = {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "username": f"@{update.effective_user.username}" if update.effective_user.username else str(cid),
            "user_id": cid,
            "text": update.message.text
        }
        try:
            with open(REPORTS_FILE, "r", encoding="utf-8") as f:
                reports = json.load(f)
        except Exception:
            reports = []
        reports.append(report_entry)
        with open(REPORTS_FILE, "w", encoding="utf-8") as f:
            json.dump(reports, f, ensure_ascii=False, indent=2)
        pending_reports.remove(cid)
        return
    tag = update.message.text.strip().replace(" ", "_" ).lower()
    if not is_pool_ready(tag):
        asyncio.create_task(prefetch(tag, 200))
    await on_tag(update, ctx, tag)

async def on_tag(update, ctx, tag):
    cid = update.effective_chat.id
    await ctx.bot.send_chat_action(cid, ChatAction.UPLOAD_PHOTO)
    loading = await ctx.bot.send_message(cid, t(cid, "loading"))

    url, api = await fetch_image(tag)

    await ctx.bot.delete_message(cid, loading.message_id)

    if not url:
        await ctx.bot.send_message(cid, t(cid, "img_not_found"))
        return
    await ctx.bot.send_photo(cid, photo=url, caption=f"{tag} ({api})")
    await send_after_photo_menu(cid, ctx)

async def send_after_photo_menu(cid, ctx):
    await ctx.bot.send_message(cid, t(cid, "followup"))

async def next_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    # Просто показуємо меню з кнопками категорій
    await ctx.bot.send_message(cid, t(cid, "search_prompt"), reply_markup=kb_cats())

async def same_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    tag = last_tag.get(cid)
    if not tag:
        await ctx.bot.send_message(cid, t(cid, "no_prev_tag"))
    else:
        await on_tag(update, ctx, tag)

async def subscribe_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cid = str(update.effective_chat.id)
    args = ctx.args
    if not args:
        await update.message.reply_text(
            t(cid, "subscribe_prompt")
        )
        return

    if args[0] == "daily":
        hour = int(args[1]) if len(args) > 1 else 9
        count = int(args[2]) if len(args) > 2 else 1
        subscribers[cid] = {"interval": None, "count": count, "hour": hour}
        save_json(SUBS_FILE, subscribers)
        await update.message.reply_text(t(cid, "daily_subscribe_confirm", hour=hour, count=count))
    else:
        interval = int(args[0])
        count = int(args[1]) if len(args) > 1 else 1
        subscribers[cid] = {"interval": interval, "count": count, "hour": None}
        save_json(SUBS_FILE, subscribers)
        await update.message.reply_text(t(cid, "subscribe_confirm", interval=interval, count=count))

async def send_scheduled():
    now = datetime.now()
    for cid, sub in subscribers.items():
        if sub.get("interval"):
            last = sub.get("last_sent", 0)
            if isinstance(last, str):
                last_time = sub.get("last_time", 0)
            else:
                last_time = last
            if now.timestamp() - last_time >= sub["interval"] * 60:
                for _ in range(sub["count"]):
                    url, api = await fetch_image(random.choice(CATEGORIES))
                    if url:
                        await app.bot.send_photo(chat_id=int(cid), photo=url, caption=t(cid, "scheduled_caption", api=api))
                        sub["last_sent"] = url
                        sub["last_time"] = now.timestamp()
                        if "all_sent" not in sub:
                            sub["all_sent"] = []
                        sub["all_sent"].append(url)
        elif sub.get("hour") is not None:
            last_day = sub.get("last_day", None)
            if now.hour == sub["hour"] and (last_day != now.date().isoformat()):
                for _ in range(sub["count"]):
                    url, api = await fetch_image(random.choice(CATEGORIES))
                    if url:
                        await app.bot.send_photo(chat_id=int(cid), photo=url, caption=t(cid, "daily_caption", api=api))
                        sub["last_sent"] = url
                        if "all_sent" not in sub:
                            sub["all_sent"] = []
                        sub["all_sent"].append(url)
                sub["last_day"] = now.date().isoformat()
    save_json(SUBS_FILE, subscribers)

async def favorites_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cid = str(update.effective_chat.id)
    favs = favorites.get(cid, [])
    if not favs:
        await update.message.reply_text("У вас ще немає лайків ❤")
    else:
        media = [InputMediaPhoto(u) for u in favs[:10]]
        await ctx.bot.send_media_group(update.effective_chat.id, media)

async def random_fav_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cid = str(update.effective_chat.id)
    favs = favorites.get(cid, [])
    if not favs:
        await update.message.reply_text("У вас ще немає лайків ❤")
    else:
        url = random.choice(favs)
        await ctx.bot.send_photo(update.effective_chat.id, photo=url, caption=t(cid, "random_fav_caption"))

async def trending_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    tag_stats = stats.get("favorites_by_tag", {})
    if not tag_stats:
        await ctx.bot.send_message(cid, t(cid, "no_trending_tags"))
        return
    # Сортуємо за кількістю лайків
    top = sorted(tag_stats.items(), key=lambda x: x[1], reverse=True)[:5]
    text = t(cid, "trending_title") + "\n"
    for i, (tag, count) in enumerate(top, 1):
        text += f"{i}. {tag} ({count})\n"
    await ctx.bot.send_message(cid, text)

async def similar_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    args = ctx.args
    if not args:
        await update.message.reply_text("Вкажіть тег: /similar <tag>")
        return
    tag = args[0].lower()
    # Знаходимо схожі теги зі списку CATEGORIES
    similar = [cat for cat in CATEGORIES if cat != tag and (tag in cat or cat in tag)]
    # Якщо мало результатів — додаємо ще за схожістю (Levenshtein/difflib)
    if len(similar) < 3:
        matches = difflib.get_close_matches(tag, CATEGORIES, n=5, cutoff=0.4)
        similar = list(set(similar + [m for m in matches if m != tag]))
    if similar:
        await update.message.reply_text("Схожі теги: " + ", ".join(similar))
    else:
        # Якщо нічого не знайдено, спробувати запропонувати найближчий тег
        closest = difflib.get_close_matches(tag, CATEGORIES, n=1, cutoff=0.0)
        if closest:
            await update.message.reply_text(t(cid, "no_similar_tags") + f" Можливо ви мали на увазі: {closest[0]}")
        else:
            await update.message.reply_text(t(cid, "no_similar_tags"))

async def stats_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cid = str(update.effective_chat.id)
    total_sent = stats.get("images_sent", 0)
    total_likes = stats.get("favorites_added", 0)
    favs = favorites.get(cid, [])
    fav_count = len(favs)
    tag_stats = stats.get("favorites_by_tag", {})
    if tag_stats:
        top_tag = max(tag_stats.items(), key=lambda x: x[1])
        top_tag_str = f'{top_tag[0]} ({top_tag[1]} разів)'
    else:
        top_tag_str = "—"
    text = (
        f"{t(cid, 'stats_title')}\n"
        f"{t(cid, 'stats_sent', sent=total_sent)}\n"
        f"{t(cid, 'stats_fav', fav=fav_count)}\n"
        f"{t(cid, 'stats_top_tag', tag=top_tag_str)}"
    )
    await ctx.bot.send_message(cid, text)

async def top_today_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    top_tags = get_top_tags_by_date(date.today(), limit=3)
    media = []
    keyboard = []
    for tag, count in top_tags:
        url, _ = await fetch_image(tag)
        media.append(InputMediaPhoto(url, caption=f"Тег: {tag} ({count} лайків)"))
        keyboard.append([InlineKeyboardButton(f"🔍 ще {tag}", callback_data=f"SHOW_TAG|{tag}")])
    keyboard.append([
        InlineKeyboardButton("Вчора", callback_data="TOP_TODAY|yesterday"),
        InlineKeyboardButton("Останні 7 днів", callback_data="TOP_TODAY|week")
    ])
    await update.message.reply_media_group(media)
    await update.message.reply_text("Обери дію:", reply_markup=InlineKeyboardMarkup(keyboard))

async def like_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cid = str(update.effective_chat.id)
    url = last_image.get(update.effective_chat.id)
    if not url:
        await update.message.reply_text(t(cid, "no_image_to_like"))
        return
    favs = favorites.setdefault(cid, [])
    if url not in favs:
        favs.append(url)
        save_json(FAVS_FILE, favorites)
        incr("favorites_added")
        update_achievements(cid, event="like", extra=1)
        await update.message.reply_text(t(cid, "like_added"))
    else:
        await update.message.reply_text(t(cid, "already_liked"))

async def category_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    tag = update.message.text[1:].split()[0].lower()  # отримуємо тег з команди
    if tag in CATEGORIES:
        await on_tag(update, ctx, tag)
    else:
        await update.message.reply_text(t(update.effective_chat.id, "unknown_tag"))

async def clearstats_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    stats["images_sent"] = 0
    stats["favorites_added"] = 0
    save_json(STATS_FILE, stats)
    await update.message.reply_text("Статистика очищена.")

async def clearlike_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cid = str(update.effective_chat.id)
    favorites[cid] = []
    save_json(FAVS_FILE, favorites)
    await update.message.reply_text("Ваші лайки очищено.")

async def clearfavorites_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cid = str(update.effective_chat.id)
    favorites[cid] = []
    save_json(FAVS_FILE, favorites)
    await update.message.reply_text("Ваші улюbлені очищено.")

async def cleartrendings_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    stats["favorites_by_tag"] = {}
    save_json(STATS_FILE, stats)
    await update.message.reply_text("Трендові теги очищено.")

async def cleartop_today_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    stats["favorites_by_tag_date"] = {}
    save_json(STATS_FILE, stats)
    await update.message.reply_text("Топ за сьогодні очищено.")

def get_top_tags_by_date(day, limit=3):
    tag_stats = stats.get("favorites_by_tag", {})
    return sorted(tag_stats.items(), key=lambda x: x[1], reverse=True)[:limit]

async def unsubscribe_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cid = str(update.effective_chat.id)
    if cid in subscribers:
        del subscribers[cid]
        save_json(SUBS_FILE, subscribers)
        await update.message.reply_text(t(cid, "unsubscribed_hint"))
    else:
        await update.message.reply_text(t(cid, "not_subscribed"))

async def report_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    pending_reports.add(cid)
    if update.message is not None:
        await update.message.reply_text(t(cid, "report_prompt"))
    else:
        await ctx.bot.send_message(cid, t(cid, "report_prompt"))

async def badges_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cid = str(update.effective_chat.id)
    user_ach = achievements.get(cid, {})
    result = []
    for k, badge_list in BADGES.items():
        if k == "tags":
            for tag, name in badge_list:
                if tag in user_ach.get("tags_used", []):
                    result.append(name)
        else:
            for value, name in badge_list:
                if user_ach.get(k, 0) >= value:
                    result.append(name)
    if result:
        await update.message.reply_text("Ваші бейджі:\n" + "\n".join(result))
    else:
        await update.message.reply_text("У вас поки немає бейджів 😅")

async def swap_cmd(update, ctx):
    cid = update.effective_chat.id
    swap_waiting.add(cid)
    await ctx.bot.send_message(cid, "Надішліть зображення для обміну.")

# --- /sendart ---
sendart_waiting = set()

async def sendart_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    sendart_waiting.add(cid)
    text = t(cid, "sendart_prompt")
    if update.message is not None:
        await update.message.reply_text(text)
    else:
        await ctx.bot.send_message(cid, text)

# --- Модерація ---
async def art_moderation_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    data = q.data
    if data.startswith("ARTAPPROVE") or data.startswith("ARTREJECT"):
        action, entry_id = data.split("|", 1)
        # Знайти у pending_arts.json
        try:
            with open(PENDING_ARTS_FILE, "r", encoding="utf-8") as f:
                pending = json.load(f)
        except Exception:
            pending = []
        idx = next((i for i, e in enumerate(pending) if e.get("entry_id") == entry_id), None)
        if idx is not None:
            entry = pending.pop(idx)
            if action == "ARTAPPROVE":
                entry["status"] = "approved"
                # Додаємо у user_arts.json
                try:
                    with open(USER_ARTS_FILE, "r", encoding="utf-8") as f:
                        user_arts = json.load(f)
                except Exception:
                    user_arts = []
                user_arts.append(entry)
                with open(USER_ARTS_FILE, "w", encoding="utf-8") as f:
                    json.dump(user_arts, f, ensure_ascii=False, indent=2)
                await ctx.bot.send_message(entry["user_id"], "Ваша картинка схвалена модератором!")
                await q.edit_message_caption(caption="✅ Картинка схвалена")
    else:
        entry["status"] = "rejected"
        await ctx.bot.send_message(entry["user_id"], "Вашу картинку  відхилено модератором.")
        await q.edit_message_caption(caption="❌ Картинку відхилено")
    # Оновити pending_arts.json
    with open(PENDING_ARTS_FILE, "w", encoding="utf-8") as f:
        json.dump(pending, f, ensure_ascii=False, indent=2)

# --- /arts ---
async def arts_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    try:
        with open(USER_ARTS_FILE, "r", encoding="utf-8") as f:
            arts = json.load(f)
    except Exception:
        arts = []
    arts = [a for a in arts if a["status"] == "approved" and a["user_id"] != cid]
    if not arts:
        await update.message.reply_text(t(cid, "no_user_arts"))
        return
    import random
    art = random.choice(arts)
    await ctx.bot.send_photo(cid, photo=art["photo_id"], caption=t(cid, "from_user", user_id=art["user_id"], caption=art["caption"]))

# --- photo_handler ---
swap_waiting = set()

async def photo_handler(update, ctx):
    cid = update.effective_chat.id

    # --- Art Swap ---
    if cid in swap_waiting:
        photo = update.message.photo[-1].file_id
        swap_pool[cid] = photo
        swap_waiting.remove(cid)

        # якщо хтось інший також в пулі
        for other, img in swap_pool.items():
            if other != cid:
                # надсилання один одному
                await ctx.bot.send_photo(cid, photo=img, caption="🎁 Ось ваш обмін!")
                await ctx.bot.send_photo(other, photo=photo, caption="🎁 Ось ваш обмін!")
                del swap_pool[cid], swap_pool[other]
                return

        await ctx.bot.send_message(cid, "Чекаємо іншого учасника…")
        return

    # --- Надсилання свого арту (модерація) ---
    if cid in sendart_waiting:
        sendart_waiting.remove(cid)
        photo = update.message.photo[-1].file_id
        caption = update.message.caption or ""
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        entry_id = str(uuid4())
        entry = {
            "entry_id": entry_id,
            "user_id": cid,
            "photo_id": photo,
            "date": now,
            "caption": caption,
            "status": "pending"
        }
        # Додаємо у pending_arts.json
        try:
            with open(PENDING_ARTS_FILE, "r", encoding="utf-8") as f:
                pending = json.load(f)
        except Exception:
            pending = []
        pending.append(entry)
        with open(PENDING_ARTS_FILE, "w", encoding="utf-8") as f:
            json.dump(pending, f, ensure_ascii=False, indent=2)
        await update.message.reply_text("Вашу картинку надіслано на модерацію!")
        # Надіслати адміну на модерацію
        for admin_id in ADMIN_IDS:
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Прийняти", callback_data=f"ARTAPPROVE|{entry_id}"),
                 InlineKeyboardButton("❌ Відхилити", callback_data=f"ARTREJECT|{entry_id}")]
            ])
            try:
                await ctx.bot.send_photo(admin_id, photo=photo, caption=f"Картинка від {cid}\n{caption}", reply_markup=kb)
            except Exception as e:
                print(f"Не вдалося надіслати адміну: {e}")
        return

    # --- Якщо це просто фото ---
    await update.message.reply_text("Фото отримано, але воно не підпадає під жодну дію.")

# ——— Реєстрація та запуск ———
scheduler = AsyncIOScheduler()

def main():
    global app
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).post_init(on_startup).build()

    # CommandHandlers
    app.add_handler(CommandHandler("start",      start))
    app.add_handler(CommandHandler("help",       help_cmd))
    app.add_handler(CommandHandler("lang",       lang_cmd))
    app.add_handler(CommandHandler("next",       next_cmd))
    app.add_handler(CommandHandler("same",       same_cmd))
    app.add_handler(CommandHandler("subscribe", subscribe_cmd))
    app.add_handler(CommandHandler("favorites", favorites_cmd))
    app.add_handler(CommandHandler("random_fav", random_fav_cmd))
    app.add_handler(CommandHandler("trending", trending_cmd))
    app.add_handler(CommandHandler("similar", similar_cmd))
    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_handler(CommandHandler("top_today", top_today_cmd))
    app.add_handler(CommandHandler("like", like_cmd))
    app.add_handler(CommandHandler("clearstats", clearstats_cmd))
    app.add_handler(CommandHandler("clearlike", clearlike_cmd))
    app.add_handler(CommandHandler("clearfavorites", clearfavorites_cmd))
    app.add_handler(CommandHandler("cleartrendings", cleartrendings_cmd))
    app.add_handler(CommandHandler("cleartop_today", cleartop_today_cmd))
    app.add_handler(CommandHandler("langua", langua_cmd))
    app.add_handler(CommandHandler("langen", langen_cmd))
    app.add_handler(CommandHandler("unsubscribe", unsubscribe_cmd))
    app.add_handler(CommandHandler("report", report_cmd))
    app.add_handler(CommandHandler("badges", badges_cmd))
    app.add_handler(CommandHandler("swap_status", swap_status_cmd))
    app.add_handler(CommandHandler("sendart", sendart_cmd))
    app.add_handler(CommandHandler("arts", arts_cmd))
    app.add_handler(MessageHandler(filters.PHOTO & ~filters.COMMAND, photo_handler))
    app.add_handler(CallbackQueryHandler(art_moderation_cb, pattern=r"^ART(APPROVE|REJECT)"))

    # Inline mode
    app.add_handler(InlineQueryHandler(inline_q))

    # CallbackQuery
    app.add_handler(CallbackQueryHandler(cb_handler))

    # Text messages
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, msg_handler))

    # Додаємо хендлери для кожного тегу
    for cat in CATEGORIES:
        app.add_handler(CommandHandler(cat, category_cmd))

    app.add_handler(CommandHandler("active", active_cmd))

    scheduler.add_job(send_scheduled, 'interval', minutes=1)

    logger.info("Bot is running.")
    app.run_polling()

async def on_startup(app):
    scheduler.start()

async def langua_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    user_lang[chat_id] = "ua"
    await update.message.reply_text(LOCALES["ua"]["lang_set"])

async def langen_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    user_lang[chat_id] = "en"
    await update.message.reply_text(LOCALES["en"]["lang_set"])

def is_valid_image_url(url):
    try:
        p = urlparse(url)
        if p.scheme not in ("http", "https"):
            return False
        if not p.netloc:
            return False
        if not re.search(r"\.(jpg|jpeg|png|gif|webp)$", p.path, re.IGNORECASE):
            return False
        return True
    except Exception:
        return False

async def swap_status_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Зараз у черзі: {len(swap_pool)} людей.")

async def validate_url(url: str) -> bool:
    try:
        timeout = ClientTimeout(total=2)
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            r = await sess.head(url, allow_redirects=True)
            ct = r.headers.get("Content-Type","")
            return r.status == 200 and ct.startswith("image/")
    except Exception:
        return False

def require_active_chat(func):
    async def wrapper(update, ctx, *args, **kwargs):
        cid = update.effective_chat.id
        if cid in chat_ended and update.message.text != "/start":
            await ctx.bot.send_message(cid, t(cid, "ended"))
            return
        return await func(update, ctx, *args, **kwargs)
    return wrapper

def load_active_users():
    if os.path.exists(ACTIVE_USERS_FILE):
        users = json.load(open(ACTIVE_USERS_FILE, encoding="utf-8"))
        # Міграція: якщо є int, перетворити на dict
        users = [
            {"id": u, "username": ""} if isinstance(u, int) else u
            for u in users
        ]
        # Перезаписати файл у новому форматі
        json.dump(users, open(ACTIVE_USERS_FILE, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        return users
    return []

def save_active_users(users):
    json.dump(users, open(ACTIVE_USERS_FILE, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

active_users = load_active_users()

async def active_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    if cid not in ADMIN_IDS:
        return
    if os.path.exists(ACTIVE_USERS_FILE):
        users = json.load(open(ACTIVE_USERS_FILE, encoding="utf-8"))
    else:
        users = []
    # Створюємо словник, щоб залишити лише унікальні id
    unique = {}
    for u in users:
        if not isinstance(u, dict) or "id" not in u or "first" not in u or "last" not in u:
            continue
        unique[u["id"]] = u  # останній запис з цим id перезапише попередній

    lines = [f"Active users: {len(unique)}"]
    for u in unique.values():
        uname = f"@{u.get('username','')}" if u.get('username') else ""
        first = u.get("first", "")
        last = u.get("last", "")
        lines.append(f"{uname} {u['id']}  first: {first}  last: {last}")
    await update.message.reply_text('\n'.join(lines))

def add_active_user(cid, username, users):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    found = False
    for u in users:
        if u.get("id") == cid:
            u["last"] = now
            u["username"] = username or u.get("username", "")
            found = True
            break
    if not found:
        users.append({
            "id": cid,
            "username": username or "",
            "first": now,
            "last": now
        })
    json.dump(users, open(ACTIVE_USERS_FILE, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

def is_user_active(cid, users):
    return any(u["id"] == cid for u in users)

if __name__ == "__main__":
    main()