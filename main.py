import os, threading, time, xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from fastapi import FastAPI, Request
from contextlib import asynccontextmanager
import urllib.request, urllib.parse

# ===================== CONFIG =====================
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "8629444233:AAHuDd3Z7OMmW3O2NpZNR09_IgIpDWlPkfA")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "8058190656")
CALLBACK_URL     = os.environ.get("CALLBACK_URL", "")
HUB_URL          = "https://pubsubhubbub.appspot.com/subscribe"

CHANNEL_IDS = [
    "UCx7mKeu3e3F5XoD65_69XLQ",
    "UCDNg-F0nDTWwrCFZxRWVShQ",
    "UCLolgwyJNsUCnEvr41-DuHQ",
    "UCp6WQCReo512WxExm7u6Vyg",
    "UCsz3EZKmnnlBHkZsNHtOquw",
    "UCgHG6kRpULWaJ1AGUr2pOXQ",
    "UCNzWZmsJ2QmBss30LeZZTdg",
    "UCLXQlPDLHcTOYWVtj5N6fOg",
]

# ===================== TELEGRAM =====================
def send_telegram(msg: str):
    try:
        data = urllib.parse.urlencode({
            "chat_id": TELEGRAM_CHAT_ID,
            "text": msg,
            "parse_mode": "HTML",
            "disable_web_page_preview": "true"
        }).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data=data, method="POST"
        )
        urllib.request.urlopen(req, timeout=10)
        print(f"[TG] Đã gửi: {msg[:60]}")
    except Exception as e:
        print(f"[TG ERROR] {e}")

# ===================== RSS POLLING =====================
seen_video_ids = set()

def poll_rss():
    global seen_video_ids
    print("[RSS] Bắt đầu polling...")
    for cid in CHANNEL_IDS:
        try:
            url = f"https://www.youtube.com/feeds/videos.xml?channel_id={cid}"
            res = urllib.request.urlopen(url, timeout=10)
            root = ET.fromstring(res.read())
            ns = "{http://www.w3.org/2005/Atom}"
            for entry in root.findall(f"{ns}entry"):
                vid_el = entry.find("{http://www.youtube.com/xml/schemas/2015}videoId")
                if vid_el is not None:
                    seen_video_ids.add(vid_el.text)
        except Exception as e:
            print(f"[RSS INIT] {cid}: {e}")
    print(f"[RSS] Đã load {len(seen_video_ids)} video IDs cũ")

    while True:
        time.sleep(60)
        for cid in CHANNEL_IDS:
            try:
                url = f"https://www.youtube.com/feeds/videos.xml?channel_id={cid}"
                res = urllib.request.urlopen(url, timeout=10)
                root = ET.fromstring(res.read())
                ns   = "{http://www.w3.org/2005/Atom}"
                nsvt = "{http://www.youtube.com/xml/schemas/2015}"
                for entry in root.findall(f"{ns}entry"):
                    vid_el   = entry.find(f"{nsvt}videoId")
                    title_el = entry.find(f"{ns}title")
                    pub_el   = entry.find(f"{ns}published")
                    if vid_el is None: continue
                    vid = vid_el.text
                    if vid in seen_video_ids: continue
                    seen_video_ids.add(vid)
                    title = title_el.text if title_el is not None else "?"
                    pub   = pub_el.text   if pub_el   is not None else ""
                    try:
                        pub_utc = datetime.fromisoformat(pub.replace("Z", "+00:00"))
                        pub_vn  = pub_utc + timedelta(hours=7)
                        pub_str = pub_vn.strftime("%d/%m/%Y %H:%M")
                    except:
                        pub_str = pub
                    msg = (
                        f"📺 <b>Video mới!</b> [RSS]\n"
                        f"🎬 <a href='https://youtube.com/watch?v={vid}'>{title}</a>\n"
                        f"📡 Channel: <code>{cid}</code>\n"
                        f"🗓 Đăng: {pub_str} (VN)"
                    )
                    print(f"[RSS] Video mới: {vid} — {title}")
                    send_telegram(msg)
            except Exception as e:
                print(f"[RSS] {cid}: {e}")

# ===================== SUBSCRIBE =====================
def subscribe_all():
    if not CALLBACK_URL:
        print("[SUB] Chưa set CALLBACK_URL, bỏ qua subscribe")
        return
    time.sleep(3)
    for cid in CHANNEL_IDS:
        try:
            data = urllib.parse.urlencode({
                "hub.mode": "subscribe",
                "hub.topic": f"https://www.youtube.com/feeds/videos.xml?channel_id={cid}",
                "hub.callback": CALLBACK_URL,
                "hub.verify": "async",
            }).encode()
            req = urllib.request.Request(HUB_URL, data=data, method="POST")
            res = urllib.request.urlopen(req, timeout=10)
            print(f"[SUB] {cid} → HTTP {res.status}")
        except Exception as e:
            print(f"[SUB ERROR] {cid}: {e}")

# ===================== KEEP ALIVE =====================
def keep_alive():
    time.sleep(60)
    while True:
        try:
            url = os.environ.get("CALLBACK_URL", "").replace("/youtube/callback", "")
            if url:
                urllib.request.urlopen(url, timeout=10)
                print("[PING] Keep alive OK")
        except Exception as e:
            print(f"[PING] {e}")
        time.sleep(600)

# ===================== FASTAPI =====================
@asynccontextmanager
async def lifespan(app):
    threading.Thread(target=subscribe_all, daemon=True).start()
    threading.Thread(target=poll_rss, daemon=True).start()
    threading.Thread(target=keep_alive, daemon=True).start()
    yield

app = FastAPI(lifespan=lifespan)

@app.get("/")
async def root():
    return {"status": "YT Monitor running", "channels": len(CHANNEL_IDS)}

@app.get("/youtube/callback")
async def verify(request: Request):
    ch = request.query_params.get("hub.challenge")
    if ch:
        print(f"[VERIFY] Challenge OK: {ch[:20]}")
        return ch
    return "ok"

@app.post("/youtube/callback")
async def callback(request: Request):
    body = await request.body()
    try:
        root = ET.fromstring(body.decode("utf-8"))
        ns   = "{http://www.w3.org/2005/Atom}"
        nsvt = "{http://www.youtube.com/xml/schemas/2015}"
        entry = root.find(f"{ns}entry")
        if entry is not None:
            vid   = entry.find(f"{nsvt}videoId").text
            title = entry.find(f"{ns}title").text
            pub   = entry.find(f"{ns}published").text
            ch_id = entry.find(f"{nsvt}channelId").text

            pub_utc = datetime.fromisoformat(pub.replace("Z", "+00:00"))
            pub_vn  = pub_utc + timedelta(hours=7)
            delay   = (datetime.now(timezone.utc) - pub_utc).total_seconds()

            print(f"[PUSH] {ch_id} | {vid} | delay={delay:.0f}s")

            if delay > 180:
                print(f"[PUSH] SKIP — video cũ ({delay:.0f}s)")
                return "OK"

            if vid not in seen_video_ids:
                seen_video_ids.add(vid)
                msg = (
                    f"📺 <b>Video mới!</b> [PUSH]\n"
                    f"🎬 <a href='https://youtube.com/watch?v={vid}'>{title}</a>\n"
                    f"📡 Channel: <code>{ch_id}</code>\n"
                    f"🗓 Đăng: {pub_vn.strftime('%d/%m/%Y %H:%M')} (VN)\n"
                    f"⏱ Delay: {delay:.0f}s"
                )
                send_telegram(msg)
    except Exception as e:
        print(f"[PUSH ERROR] {e}")
    return "OK"
