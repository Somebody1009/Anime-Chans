import time, random, requests, hashlib, xml.etree.ElementTree as ET
from cache import CUR, DB
from decouple import config

WALLHAVEN_API_KEY = config("WALLHAVEN_API_KEY")

HEADERS = {"User-Agent": "AniBotPrefetch/1.0"}

def _insert(tag, url, api, md5=None):
    if not url or not url.lower().split('?')[0].endswith(('.jpg','.jpeg','.png','.gif','.webp')):
        return
    if not md5:
        md5 = hashlib.md5(url.encode()).hexdigest()
    CUR.execute("SELECT 1 FROM image_pool WHERE md5=?", (md5,))
    if CUR.fetchone():            # уже є така картинка
        return
    CUR.execute("""INSERT OR IGNORE INTO image_pool
        (tag,url,api,md5,used,fetched) VALUES (?,?,?,?,0,?)""",
        (tag, url, api, md5, int(time.time())))
    DB.commit()

def prefetch_danbooru(tag, n):
    try:
        j = requests.get(
            f"https://danbooru.donmai.us/posts.json?tags={tag}+rating:safe&limit={n}",
            headers=HEADERS, timeout=10).json()
        for p in j:
            _insert(tag, p.get("file_url"), "danbooru", p.get("md5"))
    except: pass

def prefetch_safebooru(tag, n):
    try:
        xml = requests.get(
            f"https://safebooru.org/index.php?page=dapi&s=post&q=index&limit={n}&tags={tag}",
            headers=HEADERS, timeout=10).text
        for post in ET.fromstring(xml).findall("post"):
            url = "https:" + post.attrib.get("file_url", "")
            _insert(tag, url, "safebooru", post.attrib.get("md5"))
    except: pass

def prefetch_konachan(tag, n):
    try:
        j = requests.get(
            f"https://konachan.net/post.json?limit={n}&tags={tag}+rating:safe",
            headers=HEADERS, timeout=10).json()
        for p in j:
            _insert(tag, p.get("file_url"), "konachan", p.get("md5"))
    except: pass

def prefetch_wallhaven(tag, pages=3):
    try:
        for page in range(1, pages+1):
            j = requests.get(
                "https://wallhaven.cc/api/v1/search",
                params=dict(q=tag, categories=1, purity=1, sorting="random",
                             page=page, atleast="1920x1080",
                             apikey=WALLHAVEN_API_KEY),
                headers=HEADERS, timeout=10).json()
            for p in j.get("data", []):
                _insert(tag, "https:" + p["path"], "wallhaven")
    except: pass

def prefetch_waifu_pics(tag, n=50):
    if tag not in ("waifu","neko","hug","smile","kiss","pat","wink","cuddle"):
        return
    for _ in range(n):
        try:
            url = requests.get(f"https://api.waifu.pics/sfw/{tag}", timeout=5).json()["url"]
            _insert(tag, url, "waifu.pics")
        except: pass

def prefetch(tag, total=300):
    prefetch_danbooru(tag, total//3)
    prefetch_safebooru(tag, total//3)
    prefetch_konachan(tag, total//3)
    prefetch_wallhaven(tag, pages=2)
    prefetch_waifu_pics(tag, 30)
