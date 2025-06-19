import json
import os
from datetime import date

FILE = os.path.join(os.path.dirname(__file__), "data", "stats.json")

def load():
    if not os.path.exists(FILE):
        return {}
    with open(FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save(data):
    with open(FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def incr(key, by=1):
    data = load()
    data[key] = data.get(key, 0) + by
    save(data)

def get_today():
    data = load()
    today = date.today().isoformat()
    return data.get("by_date", {}).get(today, {})

def unique_arts():
    viewed_file = os.path.join(os.path.dirname(__file__), "data", "viewed.json")
    if not os.path.exists(viewed_file):
        return 0
    with open(viewed_file, "r", encoding="utf-8") as f:
        viewed = json.load(f)
    all_urls = set()
    for user in viewed.values():
        for tag_urls in user.values():
            all_urls.update(tag_urls)
    return len(all_urls)
