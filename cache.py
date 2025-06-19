import sqlite3, time

DB = sqlite3.connect("cache.db", check_same_thread=False)
CUR = DB.cursor()
CUR.execute("""
CREATE TABLE IF NOT EXISTS image_pool(
    tag TEXT,
    url TEXT,
    api TEXT,
    md5 TEXT,
    used INT DEFAULT 0,
    fetched INT,
    PRIMARY KEY(tag, url)
)
""")
CUR.execute("""
CREATE TABLE IF NOT EXISTS seen(
    chat_id TEXT,
    url TEXT,
    PRIMARY KEY(chat_id, url)
)
""")
DB.commit()

async def fetch_image(tag):
    return None, None

import requests, hashlib, time

async def prefetch(tag, n=100):
    # Danbooru
    url = f"https://danbooru.donmai.us/posts.json?tags={tag}&limit={n}"
    resp = requests.get(url)
    for post in resp.json():
        img_url = post.get("file_url")
        md5 = post.get("md5")
        if not img_url or not md5:
            continue
        # Перевірка на дублі
        CUR.execute("SELECT 1 FROM image_pool WHERE md5=?", (md5,))
        if CUR.fetchone():
            continue
        CUR.execute(
            "INSERT OR IGNORE INTO image_pool(tag, url, api, md5, used, fetched) VALUES (?, ?, ?, ?, 0, ?)",
            (tag, img_url, "danbooru", md5, int(time.time()))
        )
    DB.commit()
    # Аналогічно для Wallhaven (цикл по сторінках, зберігати md5 як hash від url)
