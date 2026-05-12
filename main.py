"""
YT Channel Monitor — RSS Edition
- Không scrape HTML, không cần API key
- Dùng YouTube RSS feed chính thức (cập nhật ~1-2 phút sau khi upload)
- Đa luồng, mỗi kênh 1 worker thread
- Filter video cũ bằng published timestamp chính xác
- Gửi Telegram khi có video mới
- uvicorn main:app --host 0.0.0.0 --port $PORT
"""

import os, json, re, time, threading, logging, gzip, xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from contextlib import asynccontextmanager
import urllib.request, urllib.error

from fastapi import FastAPI

# ===================== CONFIG =====================
TELEGRAM_TOKEN      = os.environ.get("TELEGRAM_TOKEN",      "")
TELEGRAM_CHAT_ID    = os.environ.get("TELEGRAM_CHAT_ID",    "")
INTERVAL            = int(os.environ.get("INTERVAL",        "60"))   # giây
MAX_VIDEO_AGE_HOURS = float(os.environ.get("MAX_VIDEO_AGE_HOURS", "6"))

# URL app trên Render để tự ping, tránh sleep (free tier)
RENDER_URL = os.environ.get("RENDER_URL", "")

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "yt_state.json")

# Chỉ cần channel_id — RSS tự động build từ đây
CHANNELS = [
    {"id": "UCx7mKeu3e3F5XoD65_69XLQ", "name": "Kênh 1"},
    {"id": "UCDNg-F0nDTWwrCFZxRWVShQ", "name": "Kênh 2"},
    {"id": "UCLolgwyJNsUCnEvr41-DuHQ", "name": "Kênh 3"},
    {"id": "UCp6WQCReo512WxExm7u6Vyg", "name": "Kênh 4"},
    {"id": "UCsz3EZKmnnlBHkZsNHtOquw", "name": "Kênh 5"},
    {"id": "UCgHG6kRpULWaJ1AGUr2pOXQ", "name": "Kênh 6"},
    {"id": "UCNzWZmsJ2QmBss30LeZZTdg", "name": "Kênh 7"},
    {"id": "UCLXQlPDLHcTOYWVtj5N6fOg", "name": "Kênh 8"},
]

def rss_url(channel_id: str) -> str:
    return f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"

# ===================== LOGGING =====================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("yt_monitor")

# ===================== STATE =====================
_state_lock = threading.Lock()

def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def save_state(state: dict):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

_state: dict = load_state()

# ===================== RSS FETCHER =====================
HEADERS = {
    "User-Agent":      "Mozilla/5.0 (compatible; YTMonitor/2.0)",
    "Accept":          "application/atom+xml,application/xml,text/xml,*/*",
    "Accept-Encoding": "gzip, deflate",
}

def fetch_rss(channel_id: str) -> list[dict]:
    """
    Fetch RSS feed và trả về list video mới nhất.
    Mỗi item: {id, title, url, published, published_dt, channel_name, channel_url}
    """
    url = rss_url(channel_id)
    req = urllib.request.Request(url, headers=HEADERS)

    with urllib.request.urlopen(req, timeout=15) as res:
        raw = res.read()
        if res.info().get("Content-Encoding") == "gzip":
            raw = gzip.decompress(raw)

    # YouTube RSS dùng Atom format
    NS = {
        "atom":  "http://www.w3.org/2005/Atom",
        "yt":    "http://www.youtube.com/xml/schemas/2015",
        "media": "http://search.yahoo.com/mrss/",
    }

    root = ET.fromstring(raw)

    channel_title_el = root.find("atom:title", NS)
    channel_name = channel_title_el.text if channel_title_el is not None else channel_id

    channel_link_el = root.find("atom:link[@rel='alternate']", NS)
    channel_url = channel_link_el.get("href", "") if channel_link_el is not None else ""

    videos = []
    for entry in root.findall("atom:entry", NS):
        vid_id_el = entry.find("yt:videoId", NS)
        if vid_id_el is None:
            continue
        vid_id = vid_id_el.text.strip()

        title_el = entry.find("atom:title", NS)
        title = title_el.text.strip() if title_el is not None else vid_id

        published_el = entry.find("atom:published", NS)
        published_str = published_el.text.strip() if published_el is not None else ""

        published_dt = None
        if published_str:
            try:
                published_dt = datetime.fromisoformat(published_str.replace("Z", "+00:00"))
            except ValueError:
                pass

        videos.append({
            "id":           vid_id,
            "title":        title,
            "url":          f"https://www.youtube.com/watch?v={vid_id}",
            "published":    published_str,
            "published_dt": published_dt,
            "channel_name": channel_name,
            "channel_url":  channel_url,
        })

    return videos

# ===================== FILTER =====================
def is_new_video(v: dict) -> tuple[bool, str]:
    if v["published_dt"] is None:
        return True, ""  # không có timestamp → cho qua

    now = datetime.now(timezone.utc)
    age_hours = (now - v["published_dt"]).total_seconds() / 3600

    if age_hours > MAX_VIDEO_AGE_HOURS:
        age_str = v["published_dt"].strftime("%d/%m %H:%M")
        return False, f"quá cũ ({age_hours:.1f}h > {MAX_VIDEO_AGE_HOURS}h) [{age_str} UTC]"

    return True, ""

# ===================== TELEGRAM =====================
def _tg_post(endpoint: str, payload: dict):
    url  = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/{endpoint}"
    data = json.dumps(payload).encode("utf-8")
    req  = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=10) as res:
        return json.loads(res.read())

def notify(video: dict):
    vid_id = video["id"]
    thumb  = f"https://i.ytimg.com/vi/{vid_id}/maxresdefault.jpg"
    now_vn = datetime.now(timezone.utc) + timedelta(hours=7)

    if video["published_dt"]:
        pub_vn   = video["published_dt"] + timedelta(hours=7)
        pub_line = f"\n🕓 <b>Đăng lúc:</b> {pub_vn.strftime('%d/%m/%Y %H:%M')} (VN)"
    else:
        pub_line = ""

    caption = (
        f"🔔 <b>Video mới!</b>\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"📺 <b>Kênh:</b> <a href='{video['channel_url']}'>{video['channel_name']}</a>\n"
        f"🎬 <b>Video:</b> <a href='{video['url']}'>{video['title']}</a>"
        f"{pub_line}\n"
        f"🕐 <b>Phát hiện:</b> {now_vn.strftime('%d/%m/%Y %H:%M')} (VN)"
    )

    try:
        _tg_post("sendPhoto", {
            "chat_id":    TELEGRAM_CHAT_ID,
            "photo":      thumb,
            "caption":    caption,
            "parse_mode": "HTML",
        })
        log.info(f"[TG ✅] {video['channel_name']} — {video['title'][:60]}")
    except Exception as e:
        log.warning(f"[TG] sendPhoto lỗi ({e}), fallback sendMessage...")
        try:
            _tg_post("sendMessage", {
                "chat_id":                  TELEGRAM_CHAT_ID,
                "text":                     caption,
                "parse_mode":               "HTML",
                "disable_web_page_preview": False,
            })
        except Exception as e2:
            log.error(f"[TG ❌] Thất bại hoàn toàn: {e2}")

# ===================== KEEP ALIVE =====================
def keep_alive(stop_event: threading.Event):
    if not RENDER_URL:
        log.info("[PING] RENDER_URL chưa set, bỏ qua keep-alive")
        return
    log.info(f"[PING] Keep-alive bật → {RENDER_URL}")
    stop_event.wait(timeout=60)
    while not stop_event.is_set():
        try:
            urllib.request.urlopen(RENDER_URL, timeout=10)
            log.info("[PING] ✅ OK")
        except Exception as e:
            log.warning(f"[PING] ⚠️ {e}")
        stop_event.wait(timeout=300)  # ping mỗi 5 phút

# ===================== WORKER =====================
def channel_worker(channel: dict, stop_event: threading.Event):
    ch_id = channel["id"]
    name  = channel.get("name", ch_id)
    key   = ch_id  # dùng channel_id làm key trong state

    # ── Lần đầu: seed state, không gửi TG ─────────────────────────────────
    with _state_lock:
        first_run = key not in _state

    if first_run:
        log.info(f"[{name}] Lần đầu — seed state, không gửi TG...")
        try:
            videos = fetch_rss(ch_id)
            with _state_lock:
                _state[key] = [v["id"] for v in videos]
                save_state(_state)
            log.info(f"[{name}] ✅ Seed {len(videos)} video ID")
        except Exception as e:
            log.error(f"[{name}] ❌ Lỗi seed: {e}")
            with _state_lock:
                _state[key] = []
                save_state(_state)

    # ── Loop chính ─────────────────────────────────────────────────────────
    while not stop_event.is_set():
        stop_event.wait(timeout=INTERVAL)
        if stop_event.is_set():
            break

        try:
            videos = fetch_rss(ch_id)

            with _state_lock:
                seen_ids   = set(_state.get(key, []))
                candidates = [v for v in videos if v["id"] not in seen_ids]

            new_videos = []
            for v in candidates:
                ok, reason = is_new_video(v)
                if ok:
                    new_videos.append(v)
                else:
                    log.info(f"[{name}] ⏭ '{v['title'][:40]}' — {reason}")
                seen_ids.add(v["id"])

            if new_videos:
                log.info(f"[{name}] 🎉 {len(new_videos)} video mới!")
                for v in reversed(new_videos):  # gửi theo thứ tự cũ → mới
                    notify(v)

            with _state_lock:
                _state[key] = list(seen_ids)
                save_state(_state)

        except Exception as e:
            log.error(f"[{name}] ❌ {e}")

    log.info(f"[{name}] Dừng")

# ===================== FASTAPI APP =====================
_stop_event = threading.Event()

@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("=" * 50)
    log.info(f"YT Monitor (RSS) | {len(CHANNELS)} kênh | interval={INTERVAL}s")
    log.info(f"Max age={MAX_VIDEO_AGE_HOURS}h")
    log.info("=" * 50)

    threads = []

    t_ping = threading.Thread(
        target=keep_alive, args=(_stop_event,),
        name="keep-alive", daemon=True
    )
    t_ping.start()
    threads.append(t_ping)

    for ch in CHANNELS:
        t = threading.Thread(
            target=channel_worker,
            args=(ch, _stop_event),
            name=f"worker-{ch['id']}",
            daemon=True,
        )
        t.start()
        threads.append(t)
        log.info(f"▶ {ch.get('name')} ({ch['id']})")
        time.sleep(0.2)

    yield  # app đang chạy

    log.info("Shutdown — dừng workers...")
    _stop_event.set()
    for t in threads:
        t.join(timeout=5)
    log.info("Đã dừng.")

app = FastAPI(lifespan=lifespan)

@app.get("/")
async def root():
    with _state_lock:
        seen_counts = {
            ch["name"]: len(_state.get(ch["id"], []))
            for ch in CHANNELS
        }
    return {
        "status":   "running",
        "channels": len(CHANNELS),
        "interval": f"{INTERVAL}s",
        "max_age":  f"{MAX_VIDEO_AGE_HOURS}h",
        "seen":     seen_counts,
    }

@app.head("/")
async def root_head():
    return {}

@app.get("/debug/{channel_index}")
async def debug_channel(channel_index: int = 0):
    """Xem RSS parse result của kênh. /debug/0, /debug/1, ..."""
    ch = CHANNELS[channel_index % len(CHANNELS)]
    try:
        videos = fetch_rss(ch["id"])
    except Exception as e:
        return {"error": str(e)}

    return {
        "channel_id":   ch["id"],
        "channel_name": ch.get("name"),
        "rss_url":      rss_url(ch["id"]),
        "video_count":  len(videos),
        "videos": [
            {
                "id":        v["id"],
                "title":     v["title"],
                "url":       v["url"],
                "published": v["published"],
                "channel":   v["channel_name"],
            }
            for v in videos
        ],
    }
