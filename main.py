"""
YT Channel Monitor
- uvicorn main:app --host 0.0.0.0 --port $PORT
- Scrape HTML trang /videos, không cần webhook/ngrok
- Đa luồng, mỗi kênh 1 worker thread
- Filter video cũ bằng publishedTimeText + viewCount
- Gửi Telegram khi có video mới
"""

import os, json, re, time, threading, logging
from datetime import datetime, timedelta, timezone
from contextlib import asynccontextmanager
import urllib.request, urllib.error

from fastapi import FastAPI

# ===================== CONFIG =====================
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN",   "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
INTERVAL         = int(os.environ.get("INTERVAL", 60))
MAX_VIDEO_AGE_HOURS = float(os.environ.get("MAX_VIDEO_AGE_HOURS", "6"))
MAX_VIEW_COUNT      = int(os.environ.get("MAX_VIEW_COUNT", "50000"))  # 0 = tắt

# URL của app trên Render để tự ping, tránh bị sleep (free tier)
# Ví dụ: https://yt-monitor.onrender.com
RENDER_URL = os.environ.get("RENDER_URL", "")

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "yt_state.json")

CHANNELS = [
    {"url": "https://www.youtube.com/channel/UCx7mKeu3e3F5XoD65_69XLQ/videos", "name": "UCx7mKeu3e3F5XoD65_69XLQ"},
    {"url": "https://www.youtube.com/channel/UCDNg-F0nDTWwrCFZxRWVShQ/videos", "name": "UCDNg-F0nDTWwrCFZxRWVShQ"},
    {"url": "https://www.youtube.com/channel/UCLolgwyJNsUCnEvr41-DuHQ/videos", "name": "UCLolgwyJNsUCnEvr41-DuHQ"},
    {"url": "https://www.youtube.com/channel/UCp6WQCReo512WxExm7u6Vyg/videos", "name": "UCp6WQCReo512WxExm7u6Vyg"},
    {"url": "https://www.youtube.com/channel/UCsz3EZKmnnlBHkZsNHtOquw/videos", "name": "UCsz3EZKmnnlBHkZsNHtOquw"},
    {"url": "https://www.youtube.com/channel/UCgHG6kRpULWaJ1AGUr2pOXQ/videos", "name": "UCgHG6kRpULWaJ1AGUr2pOXQ"},
    {"url": "https://www.youtube.com/channel/UCNzWZmsJ2QmBss30LeZZTdg/videos", "name": "UCNzWZmsJ2QmBss30LeZZTdg"},
    {"url": "https://www.youtube.com/channel/UCLXQlPDLHcTOYWVtj5N6fOg/videos", "name": "UCLXQlPDLHcTOYWVtj5N6fOg"},
]

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

# ===================== TIME / VIEW PARSER =====================
def parse_relative_time(text: str) -> datetime | None:
    if not text:
        return None
    t = text.lower().strip()
    now = datetime.now(timezone.utc)

    if any(x in t for x in ["just now", "방금", "gerade", "à l'instant", "adesso"]):
        return now

    patterns = [
        (r"(\d+)\s*(?:second|sec|초|секунд|sekund)",    "seconds"),
        (r"(\d+)\s*(?:minute|min|분|минут|minut)",       "minutes"),
        (r"(\d+)\s*(?:hour|hr|시간|час|ore|stund)",      "hours"),
        (r"(\d+)\s*(?:day|일|день|дн|gün|dag)",          "days"),
        (r"(\d+)\s*(?:week|주|недел|semain|semana)",      "weeks"),
        (r"(\d+)\s*(?:month|개월|месяц|mois|mes)",       "months"),
        (r"(\d+)\s*(?:year|년|год|лет|an\b|año)",        "years"),
    ]
    for pattern, unit in patterns:
        m = re.search(pattern, t)
        if m:
            n = int(m.group(1))
            delta = {
                "seconds": timedelta(seconds=n),
                "minutes": timedelta(minutes=n),
                "hours":   timedelta(hours=n),
                "days":    timedelta(days=n),
                "weeks":   timedelta(weeks=n),
                "months":  timedelta(days=n * 30),
                "years":   timedelta(days=n * 365),
            }[unit]
            return now - delta
    return None

def parse_view_count(text: str) -> int | None:
    if not text:
        return None
    t = text.lower().replace(",", "").replace(" ", "")
    m = re.search(r"([\d.]+)\s*([kmb만억]?)\s*(?:view|조회|просмотр)?", t)
    if not m:
        return None
    num = float(m.group(1))
    mult = {"k": 1_000, "m": 1_000_000, "b": 1_000_000_000,
            "만": 10_000, "억": 100_000_000}.get(m.group(2), 1)
    return int(num * mult)

# ===================== SCRAPER =====================
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
}

def fetch_html(url: str) -> str:
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=15) as res:
        return res.read().decode("utf-8", errors="replace")

def parse_videos(html: str, max_count: int = 10) -> list:
    m = re.search(
        r"(?:var\s+)?ytInitialData\s*=\s*(\{.+?\});\s*(?:</script>|var\s+)",
        html, re.DOTALL
    )
    if not m:
        raise RuntimeError("Không tìm thấy ytInitialData")

    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError as e:
        raise RuntimeError(f"JSON parse thất bại: {e}")

    videos = []
    seen   = set()

    def extract_video(vi: dict) -> dict | None:
        vid_id = vi.get("videoId")
        if not vid_id or vid_id in seen:
            return None
        seen.add(vid_id)
        title = "".join(r.get("text", "") for r in vi.get("title", {}).get("runs", []))
        age_text = (
            vi.get("publishedTimeText", {}).get("simpleText")
            or vi.get("publishedTimeText", {}).get("runs", [{}])[0].get("text")
        )
        vct = vi.get("viewCountText", {})
        views_text = (
            vct.get("simpleText")
            or "".join(r.get("text", "") for r in vct.get("runs", []))
        )
        return {
            "id":         vid_id,
            "title":      title,
            "url":        f"https://www.youtube.com/watch?v={vid_id}",
            "age_text":   age_text,
            "age_dt":     parse_relative_time(age_text),
            "views":      parse_view_count(views_text),
            "views_text": views_text,
        }

    def try_extract(items: list) -> bool:
        for item in items:
            vi = (
                item.get("richItemRenderer", {}).get("content", {}).get("videoRenderer")
                or item.get("gridVideoRenderer")
                or item.get("videoRenderer")
            )
            if vi:
                v = extract_video(vi)
                if v:
                    videos.append(v)
            if len(videos) >= max_count:
                return True
        return False

    try:
        tabs = data["contents"]["twoColumnBrowseResultsRenderer"]["tabs"]
        for tab in tabs:
            tr = tab.get("tabRenderer", {})
            if not tr.get("selected"):
                continue
            content = tr.get("content", {})
            items = content.get("richGridRenderer", {}).get("contents", [])
            if items:
                try_extract(items)
            if videos:
                break
            for section in content.get("sectionListRenderer", {}).get("contents", []):
                for inner in section.get("itemSectionRenderer", {}).get("contents", []):
                    if try_extract(inner.get("gridRenderer", {}).get("items", [])):
                        break
                if videos:
                    break
            if videos:
                break
    except (KeyError, TypeError):
        pass

    # Fallback regex
    if not videos:
        log.warning("ytInitialData parse thất bại, dùng fallback regex (không có age/view)")
        for vid_id, title in re.findall(
            r'"videoId":"([A-Za-z0-9_-]{11})"[^}]*?"title":\{"runs":\[\{"text":"([^"]+)"',
            html,
        ):
            if vid_id not in seen:
                seen.add(vid_id)
                videos.append({
                    "id": vid_id, "title": title,
                    "url": f"https://www.youtube.com/watch?v={vid_id}",
                    "age_text": None, "age_dt": None,
                    "views": None,    "views_text": None,
                })
            if len(videos) >= max_count:
                break

    return videos

# ===================== FILTER =====================
def is_new_video(v: dict) -> tuple[bool, str]:
    """
    (True, "")        → gửi
    (False, lý do)    → bỏ qua
    """
    now = datetime.now(timezone.utc)

    if v["age_dt"] is not None:
        age_hours = (now - v["age_dt"]).total_seconds() / 3600
        if age_hours > MAX_VIDEO_AGE_HOURS:
            return False, f"quá cũ ({age_hours:.1f}h > {MAX_VIDEO_AGE_HOURS}h) [{v['age_text']}]"

    if MAX_VIEW_COUNT > 0 and v["views"] is not None:
        if v["views"] > MAX_VIEW_COUNT:
            return False, f"view quá cao ({v['views']:,} > {MAX_VIEW_COUNT:,})"

    return True, ""

# ===================== TELEGRAM =====================
def _tg_post(endpoint: str, payload: dict):
    url  = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/{endpoint}"
    data = json.dumps(payload).encode("utf-8")
    req  = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as res:
        return json.loads(res.read())

def notify(video: dict, channel_name: str):
    vid_id  = video["id"]
    thumb   = f"https://i.ytimg.com/vi/{vid_id}/maxresdefault.jpg"
    now_vn  = datetime.now(timezone.utc) + timedelta(hours=7)
    age_line   = f"\n🕓 <b>Đăng:</b> {video['age_text']}" if video["age_text"] else ""
    views_line = f"\n👁 <b>Views:</b> {video['views_text']}" if video["views_text"] else ""
    caption = (
        f"🔔 <b>Video mới!</b>\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"📺 <b>Kênh:</b> {channel_name}\n"
        f"🎬 <b>Video:</b> <a href='{video['url']}'>{video['title']}</a>"
        f"{age_line}{views_line}\n"
        f"🕐 <b>Phát hiện:</b> {now_vn.strftime('%d/%m/%Y %H:%M')} (VN)"
    )
    try:
        _tg_post("sendPhoto", {
            "chat_id": TELEGRAM_CHAT_ID, "photo": thumb,
            "caption": caption, "parse_mode": "HTML",
        })
        log.info(f"[TG ✅] {channel_name} — {video['title'][:60]}")
    except Exception as e:
        log.warning(f"[TG] sendPhoto lỗi ({e}), fallback sendMessage...")
        try:
            _tg_post("sendMessage", {
                "chat_id": TELEGRAM_CHAT_ID, "text": caption,
                "parse_mode": "HTML", "disable_web_page_preview": False,
            })
        except Exception as e2:
            log.error(f"[TG ❌] Thất bại hoàn toàn: {e2}")

# ===================== KEEP ALIVE =====================
def keep_alive(stop_event: threading.Event):
    """Tự ping chính nó mỗi 10 phút để Render free tier không sleep."""
    if not RENDER_URL:
        log.info("[PING] RENDER_URL chưa set, bỏ qua keep-alive")
        return
    log.info(f"[PING] Keep-alive bật → {RENDER_URL}")
    stop_event.wait(timeout=60)   # chờ app khởi động xong rồi mới ping
    while not stop_event.is_set():
        try:
            urllib.request.urlopen(RENDER_URL, timeout=10)
            log.info("[PING] ✅ OK")
        except Exception as e:
            log.warning(f"[PING] ⚠️ {e}")
        stop_event.wait(timeout=300)   # 10 phút

# ===================== WORKER =====================
def channel_worker(channel: dict, stop_event: threading.Event):
    url  = channel["url"]
    name = channel.get("name") or url

    with _state_lock:
        first_run = url not in _state

    if first_run:
        log.info(f"[{name}] Lần đầu — lưu state hiện tại, không gửi TG...")
        try:
            videos = parse_videos(fetch_html(url))
            with _state_lock:
                _state[url] = [v["id"] for v in videos]
                save_state(_state)
            log.info(f"[{name}] ✅ Lưu {len(videos)} ID")
        except Exception as e:
            log.error(f"[{name}] ❌ Lỗi khởi tạo: {e}")
            with _state_lock:
                _state[url] = []
                save_state(_state)

    while not stop_event.is_set():
        stop_event.wait(timeout=INTERVAL)
        if stop_event.is_set():
            break

        try:
            videos     = parse_videos(fetch_html(url))
            with _state_lock:
                seen_ids   = set(_state.get(url, []))
                candidates = [v for v in videos if v["id"] not in seen_ids]

            new_videos = []
            for v in candidates:
                ok, reason = is_new_video(v)
                if ok:
                    new_videos.append(v)
                else:
                    log.info(f"[{name}] ⏭ '{v['title'][:40]}' — {reason}")
                seen_ids.add(v["id"])   # thêm vào seen dù bỏ qua, tránh check lại

            if new_videos:
                log.info(f"[{name}] 🎉 {len(new_videos)} video mới!")
                for v in reversed(new_videos):
                    notify(v, name)

            with _state_lock:
                _state[url] = list(seen_ids)
                save_state(_state)

        except Exception as e:
            log.error(f"[{name}] ❌ {e}")

    log.info(f"[{name}] Dừng")

# ===================== FASTAPI APP =====================
_stop_event = threading.Event()

@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("=" * 50)
    log.info(f"YT Monitor | {len(CHANNELS)} kênh | interval={INTERVAL}s")
    log.info(f"Max age={MAX_VIDEO_AGE_HOURS}h | Max views={MAX_VIEW_COUNT:,}")
    log.info("=" * 50)

    threads = []
    t_ping = threading.Thread(target=keep_alive, args=(_stop_event,), name="keep-alive", daemon=True)
    t_ping.start()
    threads.append(t_ping)

    for ch in CHANNELS:
        t = threading.Thread(
            target=channel_worker,
            args=(ch, _stop_event),
            name=f"worker-{ch.get('name', '?')}",
            daemon=True,
        )
        t.start()
        threads.append(t)
        log.info(f"▶ {ch.get('name')} ({ch['url']})")
        time.sleep(0.3)

    yield   # app đang chạy

    log.info("Shutdown — dừng workers...")
    _stop_event.set()
    for t in threads:
        t.join(timeout=5)
    log.info("Đã dừng.")

app = FastAPI(lifespan=lifespan)

@app.get("/")
async def root():
    with _state_lock:
        seen_counts = {ch["name"]: len(_state.get(ch["url"], [])) for ch in CHANNELS}
    return {
        "status":   "running",
        "channels": len(CHANNELS),
        "interval": INTERVAL,
        "seen":     seen_counts,
    }
