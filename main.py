import io
import os
import re
import json
import math
import tempfile
import subprocess
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from bs4 import BeautifulSoup
from PIL import Image, ImageDraw, ImageFont, ImageOps
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, StreamingResponse, FileResponse
from starlette.background import BackgroundTask

APP_TITLE = "CCTV Ping River"
APP_BASE = "https://appserv.net/pingriver.php"
APP_ORIGIN = "https://appserv.net"
PLAYBACK_HOST = "https://ns38.appservhosting.com/pingriver/cctv-playback.php"

STATIONS = {
    "P.1": "สะพานนวรัฐ",
    "P.67": "บ้านแม่แต",
}

BKK = timezone(timedelta(hours=7))
HTTP_TIMEOUT = 30
MAX_WORKERS = 6
TMP_DIR = Path(tempfile.gettempdir()) / "pingriver_image_center_v2"
TMP_DIR.mkdir(parents=True, exist_ok=True)

PNG_SIZE = 1080
GIF_SIZE = 640
GIF_DURATION_MS = 250

app = FastAPI(title=APP_TITLE)

JOBS = {}
JOBS_LOCK = threading.Lock()
JOB_TTL_SECONDS = 3600


def now_bkk():
    return datetime.now(BKK)


def make_session():
    s = requests.Session()
    retry = Retry(
        total=3,
        connect=3,
        read=3,
        backoff_factor=0.7,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "HEAD"],
    )
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (CCTVPingRiver/31.0)",
        "Accept": "*/*",
        "Referer": "https://appserv.net/pingriver.php",
    })
    return s


HTTP = make_session()



def purge_old_jobs():
    cutoff = now_bkk() - timedelta(seconds=JOB_TTL_SECONDS)
    remove_ids = []
    with JOBS_LOCK:
        for job_id, job in JOBS.items():
            updated = job.get("updated_at") or job.get("created_at") or now_bkk()
            if updated < cutoff:
                remove_ids.append(job_id)
        for job_id in remove_ids:
            path = JOBS[job_id].get("path")
            if path:
                remove_file(path)
            JOBS.pop(job_id, None)


def create_job(job_type: str, payload: dict):
    purge_old_jobs()
    job_id = uuid.uuid4().hex
    now = now_bkk()
    with JOBS_LOCK:
        JOBS[job_id] = {
            "job_id": job_id,
            "job_type": job_type,
            "payload": payload,
            "status": "queued",
            "progress": 0,
            "message": "รอเริ่มงาน...",
            "created_at": now,
            "updated_at": now,
            "path": None,
            "filename": None,
            "error": None,
        }
    return job_id


def update_job(job_id: str, **kwargs):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return
        job.update(kwargs)
        job["updated_at"] = now_bkk()


def get_job(job_id: str):
    purge_old_jobs()
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return None
        out = dict(job)
        for k in ("created_at", "updated_at"):
            if isinstance(out.get(k), datetime):
                out[k] = out[k].isoformat()
        return out


def job_progress_callback(job_id: str, start_pct=0, end_pct=100):
    def cb(progress_value, message):
        try:
            progress_value = max(0.0, min(100.0, float(progress_value)))
        except Exception:
            progress_value = 0.0
        mapped = start_pct + ((end_pct - start_pct) * (progress_value / 100.0))
        update_job(
            job_id,
            status="running",
            progress=int(mapped),
            message=message,
        )
    return cb


def validate_station(station: str) -> str:
    station = station.strip().upper()
    if station not in STATIONS:
        raise HTTPException(400, f"station ต้องเป็น {', '.join(STATIONS.keys())}")
    return station


def fetch_text(url: str) -> str:
    r = HTTP.get(url, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    if not r.encoding or r.encoding.lower() == "iso-8859-1":
        r.encoding = r.apparent_encoding or "utf-8"
    return r.text


def fetch_bytes(url: str) -> bytes:
    r = HTTP.get(url, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    return r.content


def get_font(size, bold=False):
    """
    ใช้ฟอนต์ Prompt เป็นหลัก เพื่อให้ข้อความไทยใน PNG/GIF แสดงถูกต้อง
    """
    candidates = [
        "/usr/share/fonts/truetype/prompt/Prompt-Bold.ttf" if bold else "/usr/share/fonts/truetype/prompt/Prompt-Regular.ttf",
        "/usr/local/share/fonts/Prompt-Bold.ttf" if bold else "/usr/local/share/fonts/Prompt-Regular.ttf",
        "/usr/share/fonts/truetype/noto/NotoSansThai-Bold.ttf" if bold else "/usr/share/fonts/truetype/noto/NotoSansThai-Regular.ttf",
        "/usr/share/fonts/opentype/noto/NotoSansThai-Bold.ttf" if bold else "/usr/share/fonts/opentype/noto/NotoSansThai-Regular.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for p in candidates:
        if os.path.exists(p):
            return ImageFont.truetype(p, size=size)
    return ImageFont.load_default()



def text_size(draw: ImageDraw.ImageDraw, text: str, font):
    """คืนค่าความกว้าง/สูงของข้อความด้วยฟอนต์ที่กำหนด"""
    box = draw.textbbox((0, 0), str(text), font=font)
    return box[2] - box[0], box[3] - box[1]


def fit_font_to_width(
    draw: ImageDraw.ImageDraw,
    text: str,
    max_width: int,
    start_size: int,
    bold: bool = False,
    min_size: int = 12,
):
    """ลดขนาดฟอนต์อัตโนมัติจนข้อความพอดีกับความกว้าง"""
    text = str(text)
    max_width = max(1, int(max_width))
    size = max(int(start_size), int(min_size))

    while size >= min_size:
        font = get_font(size, bold)
        width, _ = text_size(draw, text, font)
        if width <= max_width:
            return font
        size -= 1

    return get_font(min_size, bold)


def wrap_text_to_width(
    draw: ImageDraw.ImageDraw,
    text: str,
    font,
    max_width: int,
    max_lines: int = 2,
):
    """ตัดบรรทัดข้อความให้พอดีกับกรอบ โดยไม่เกิน max_lines"""
    text = str(text).strip()
    if not text:
        return [""]

    max_width = max(1, int(max_width))
    max_lines = max(1, int(max_lines))
    words = text.split()

    # ภาษาไทยบางข้อความไม่มี space มากพอ: fallback ตัดทีละตัวอักษร
    if len(words) <= 1:
        lines = []
        current = ""
        for ch in text:
            trial = current + ch
            width, _ = text_size(draw, trial, font)
            if width <= max_width or not current:
                current = trial
            else:
                lines.append(current)
                current = ch
                if len(lines) >= max_lines - 1:
                    break
        if current and len(lines) < max_lines:
            lines.append(current)

        consumed = "".join(lines)
        if len(consumed) < len(text) and lines:
            remaining = text[len(consumed):]
            lines[-1] += remaining

        # trim last line with ellipsis
        if lines:
            last = lines[-1]
            while text_size(draw, last, font)[0] > max_width and len(last) > 1:
                last = last[:-2].rstrip() + "…"
            lines[-1] = last
        return lines[:max_lines]

    lines = []
    current = ""

    for word in words:
        trial = word if not current else current + " " + word
        width, _ = text_size(draw, trial, font)

        if width <= max_width or not current:
            current = trial
        else:
            lines.append(current)
            current = word
            if len(lines) >= max_lines - 1:
                break

    if current and len(lines) < max_lines:
        lines.append(current)

    # รวมคำที่เหลือลงบรรทัดสุดท้าย แล้ว trim
    used_words = sum(len(line.split()) for line in lines)
    if used_words < len(words) and lines:
        remaining = " ".join(words[used_words:])
        lines[-1] = (lines[-1] + " " + remaining).strip()

    if lines:
        last = lines[-1]
        while text_size(draw, last, font)[0] > max_width and len(last) > 1:
            last = last[:-2].rstrip() + "…"
        lines[-1] = last

    return lines[:max_lines]


def temp_path(suffix: str):
    f = tempfile.NamedTemporaryFile(delete=False, suffix=suffix, dir=TMP_DIR)
    name = f.name
    f.close()
    return name


def remove_file(path: str):
    try:
        os.unlink(path)
    except OSError:
        pass



def extract_exp_sig_from_url(url: str):
    m_exp = re.search(r"[?&]exp=(\d+)", url)
    m_sig = re.search(r"[?&]sig=([a-fA-F0-9]+)", url)
    exp = m_exp.group(1) if m_exp else None
    sig = m_sig.group(1) if m_sig else None
    return exp, sig


def playback_auth_from_env(station: str):
    """
    รองรับ 3 รูปแบบ:
    1) PLAYBACK_<station>_URL_SAMPLE = full signed playback url
       เช่น PLAYBACK_P67_URL_SAMPLE
    2) PLAYBACK_<station>_EXP / PLAYBACK_<station>_SIG
    3) PLAYBACK_EXP / PLAYBACK_SIG (ใช้ร่วมทุก station)
    หมายเหตุ station P.67 -> key ใช้ P67, P.1 -> P1
    """
    key = station.replace(".", "").upper()

    sample = os.getenv(f"PLAYBACK_{key}_URL_SAMPLE", "").strip()
    if sample:
        exp, sig = extract_exp_sig_from_url(sample)
        if exp and sig:
            return exp, sig, f"env:PLAYBACK_{key}_URL_SAMPLE"

    exp = os.getenv(f"PLAYBACK_{key}_EXP", "").strip()
    sig = os.getenv(f"PLAYBACK_{key}_SIG", "").strip()
    if exp and sig:
        return exp, sig, f"env:PLAYBACK_{key}_EXP/SIG"

    exp = os.getenv("PLAYBACK_EXP", "").strip()
    sig = os.getenv("PLAYBACK_SIG", "").strip()
    if exp and sig:
        return exp, sig, "env:PLAYBACK_EXP/SIG"

    return None, None, None


def camera_timestamp_from_cache_url(value: str):
    m = re.search(r"cctv_(\d{14})_", value, re.I)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y%m%d%H%M%S").replace(tzinfo=BKK)
    except ValueError:
        return None


def parse_hourly_filename(name: str):
    m = re.search(r"hourly/(\d{4}-\d{2}-\d{2})_(\d{2})\.mp4$", name)
    if not m:
        return None
    try:
        d = datetime.strptime(m.group(1), "%Y-%m-%d").replace(tzinfo=BKK)
        return d.replace(hour=int(m.group(2)), minute=0, second=0, microsecond=0)
    except ValueError:
        return None


def _collect_strings(obj):
    out = []
    if isinstance(obj, str):
        out.append(obj)
    elif isinstance(obj, dict):
        for k, v in obj.items():
            out.extend(_collect_strings(k))
            out.extend(_collect_strings(v))
    elif isinstance(obj, (list, tuple)):
        for x in obj:
            out.extend(_collect_strings(x))
    return out


def _extract_cache_jpgs(raw: str, station: str):
    """
    v4: คืน logic แบบ v1 ที่เคยจับ P.67 ได้
    รองรับ:
      1) URL เต็ม https://appserv.net/cache/P.67/...jpg
      2) relative /cache/P.67/...jpg
      3) JSON/HTML ที่มีเพียงชื่อไฟล์ cctv_YYYYMMDDHHMMSS_hash.jpg
         แล้วประกอบ path จาก timestamp ในชื่อไฟล์
    """
    strings = [raw]

    # JSON response อาจซ่อนชื่อไฟล์อยู่ใน key/value
    try:
        strings.extend(_collect_strings(json.loads(raw)))
    except Exception:
        pass

    # HTML attributes
    try:
        soup = BeautifulSoup(raw, "html.parser")
        for tag in soup.find_all(["img", "a", "source"]):
            for attr in ("src", "href"):
                value = tag.get(attr)
                if value:
                    strings.append(value)
    except Exception:
        pass

    blob = "\n".join(strings)
    found = []

    # full / relative cache path
    pattern = rf"((?:https?://appserv\.net)?/cache/{re.escape(station)}/[^\s\"'<>\\]+?\.jpe?g(?:\?[^\s\"'<>\\]*)?)"
    found.extend(re.findall(pattern, blob, re.I))

    # สำคัญ: บาง response/page มีแค่ชื่อไฟล์ ไม่มี /cache/P.x/
    filenames = re.findall(
        r"(cctv_(\d{14})_[A-Za-z0-9]+\.jpe?g)",
        blob,
        re.I,
    )
    for filename, stamp in filenames:
        try:
            dt = datetime.strptime(stamp, "%Y%m%d%H%M%S").replace(tzinfo=BKK)
            found.append(
                f"{APP_ORIGIN}/cache/{station}/{dt:%Y}/{dt:%m}/{filename}"
            )
        except ValueError:
            pass

    clean = []
    seen = set()
    for u in found:
        u = u.replace("\\/", "/").replace("&amp;", "&").strip()
        if u.startswith("/"):
            u = APP_ORIGIN + u
        base = u.split("?")[0]
        if base not in seen:
            seen.add(base)
            clean.append(base)

    clean.sort(
        key=lambda x: camera_timestamp_from_cache_url(x)
        or datetime(2000, 1, 1, tzinfo=BKK)
    )
    return clean


def latest_cache_jpg(station: str):
    """
    ลอง 3 source ตาม request ที่ browser ของ AppServ ใช้จริง:
      camlist -> page -> ajax_data_only
    """
    nonce = int(now_bkk().timestamp() * 1000)
    sources = [
        ("camlist", f"{APP_BASE}?op=camlist&station={station}&_={nonce}"),
        ("page", f"{APP_BASE}?station={station}&_={nonce}"),
        ("ajax", f"{APP_BASE}?ajax_data_only=true&station={station}&_={nonce}"),
    ]

    all_found = []
    errors = {}
    for name, url in sources:
        try:
            raw = fetch_text(url)
            found = _extract_cache_jpgs(raw, station)
            all_found.extend(found)
        except Exception as e:
            errors[name] = str(e)

    all_found = list(dict.fromkeys(all_found))
    all_found.sort(
        key=lambda x: camera_timestamp_from_cache_url(x)
        or datetime(2000, 1, 1, tzinfo=BKK)
    )

    if not all_found:
        detail = "; ".join(f"{k}:{v}" for k, v in errors.items())
        raise RuntimeError(
            f"ไม่พบภาพล่าสุดของ {station}"
            + (f" ({detail})" if detail else "")
        )
    return all_found[-1], all_found


def debug_latest_sources(station: str):
    nonce = int(now_bkk().timestamp() * 1000)
    sources = [
        ("camlist", f"{APP_BASE}?op=camlist&station={station}&_={nonce}"),
        ("page", f"{APP_BASE}?station={station}&_={nonce}"),
        ("ajax", f"{APP_BASE}?ajax_data_only=true&station={station}&_={nonce}"),
    ]
    result = {"station": station, "sources": {}}
    for name, url in sources:
        try:
            raw = fetch_text(url)
            found = _extract_cache_jpgs(raw, station)
            names = re.findall(r"cctv_\d{14}_[A-Za-z0-9]+\.jpe?g", raw, re.I)
            result["sources"][name] = {
                "ok": True,
                "found_count": len(found),
                "found": found[-5:],
                "filename_only_count": len(set(names)),
                "filename_only": list(dict.fromkeys(names))[-5:],
                "preview": raw[:700],
            }
        except Exception as e:
            result["sources"][name] = {
                "ok": False,
                "error": str(e),
            }
    return result

def _recursive_find_auth(obj):
    """
    หา exp/sig ไม่ว่าจะอยู่ top-level, auth, data.auth หรือ nested object อื่น
    """
    if isinstance(obj, dict):
        exp = obj.get("exp")
        sig = obj.get("sig")
        if exp is not None and sig:
            return str(exp), str(sig), "nested"

        auth = obj.get("auth")
        if isinstance(auth, dict):
            exp = auth.get("exp")
            sig = auth.get("sig")
            if exp is not None and sig:
                return str(exp), str(sig), "auth"

        for value in obj.values():
            found = _recursive_find_auth(value)
            if found:
                return found

    elif isinstance(obj, list):
        for value in obj:
            found = _recursive_find_auth(value)
            if found:
                return found

    return None


def parse_camlist_response(raw: str):
    obj = None
    try:
        obj = json.loads(raw)
    except Exception:
        pass

    files = []
    exp = None
    sig = None
    auth_shape = None

    if isinstance(obj, dict):
        if isinstance(obj.get("files"), list):
            files = [str(x) for x in obj.get("files", [])]

        found = _recursive_find_auth(obj)
        if found:
            exp, sig, auth_shape = found

    if not files:
        files = re.findall(r'hourly/\d{4}-\d{2}-\d{2}_\d{2}\.mp4', raw)

    # regex fallback เผื่อ response ไม่ใช่ JSON สมบูรณ์
    if exp is None:
        m = re.search(r'"exp"\s*:\s*"?(?P<v>\d+)"?', raw)
        if m:
            exp = m.group("v")
            auth_shape = auth_shape or "regex"
    if sig is None:
        m = re.search(r'"sig"\s*:\s*"(?P<v>[a-fA-F0-9]+)"', raw)
        if m:
            sig = m.group("v")
            auth_shape = auth_shape or "regex"

    files = list(dict.fromkeys(files))
    return {
        "files": files,
        "exp": exp,
        "sig": sig,
        "auth_shape": auth_shape,
        "raw_preview": raw[:2000],
        "raw_tail": raw[-1500:],
    }


def _prime_appserv_session(station: str):
    """
    Browser เปิดหน้า pingriver.php ก่อน แล้ว JS จึง fetch camlist ใน same-origin
    ทำแบบเดียวกันเพื่อรับ Set-Cookie/session ก่อน
    """
    nonce = int(now_bkk().timestamp() * 1000)
    url = f"{APP_BASE}?station={station}&_={nonce}"
    r = HTTP.get(url, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    return {
        "status": r.status_code,
        "cookie_names": [c.name for c in HTTP.cookies],
    }


def fetch_camlist(station: str):
    nonce = int(now_bkk().timestamp() * 1000)

    # Prime session เหมือน browser
    prime_info = None
    try:
        prime_info = _prime_appserv_session(station)
    except Exception as e:
        prime_info = {"error": str(e), "cookie_names": [c.name for c in HTTP.cookies]}

    url = f"{APP_BASE}?op=camlist&station={station}&_={nonce}"
    r = HTTP.get(
        url,
        timeout=HTTP_TIMEOUT,
        headers={
            "Referer": f"{APP_BASE}?station={station}",
            "Accept": "application/json, text/plain, */*",
        },
    )
    r.raise_for_status()
    if not r.encoding or r.encoding.lower() == "iso-8859-1":
        r.encoding = r.apparent_encoding or "utf-8"

    parsed = parse_camlist_response(r.text)
    parsed["prime_info"] = prime_info
    parsed["cookie_names"] = [c.name for c in HTTP.cookies]

    # ถ้า auth ยังไม่มา ลอง prime + camlist ใหม่อีกครั้ง
    if parsed["files"] and not (parsed.get("exp") and parsed.get("sig")):
        try:
            _prime_appserv_session(station)
            r2 = HTTP.get(
                url + "&retry=1",
                timeout=HTTP_TIMEOUT,
                headers={
                    "Referer": f"{APP_BASE}?station={station}",
                    "Accept": "application/json, text/plain, */*",
                    "Cache-Control": "no-cache",
                },
            )
            r2.raise_for_status()
            parsed2 = parse_camlist_response(r2.text)
            parsed2["prime_info"] = prime_info
            parsed2["cookie_names"] = [c.name for c in HTTP.cookies]
            if parsed2.get("exp") and parsed2.get("sig"):
                parsed = parsed2
        except Exception:
            pass

    if not parsed["files"]:
        raise RuntimeError(f"ไม่พบไฟล์ playback ของ {station}")
    return parsed

def try_discover_sig_from_page(station: str):
    html = fetch_text(f"{APP_BASE}?station={station}&_={int(now_bkk().timestamp()*1000)}")
    exp = None
    sig = None
    m = re.search(r'cctv-playback\.php\?op=vid[^"\']*?[?&]exp=(\d+)[^"\']*?[?&]sig=([a-fA-F0-9]+)', html)
    if m:
        exp = m.group(1)
        sig = m.group(2)
    return exp, sig


def playback_url_candidates(station: str, rel_file: str, exp=None, sig=None):
    rel_enc = quote(rel_file, safe="")
    urls = []
    if exp and sig:
        urls.append(f"{PLAYBACK_HOST}?op=vid&station={station}&f={rel_enc}&exp={exp}&sig={sig}")
    urls.append(f"{PLAYBACK_HOST}?op=vid&station={station}&f={rel_enc}")
    return urls


def _playback_request_headers(station: str):
    headers = {
        "Referer": f"{APP_BASE}?station={station}",
        "Origin": "https://appserv.net",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/151 Safari/537.36",
        "Accept": "*/*",
    }
    return headers


def download_playback_video(urls, station: str):
    """
    v7: ไม่ให้ ffmpeg ยิง URL ตรง ๆ แล้ว
    เพราะ ffmpeg client มักโดน 403 แม้ signed URL จะถูกต้อง
    ใช้ requests.Session เดิมดาวน์โหลด mp4 ก่อน แล้วค่อยให้ ffmpeg อ่านไฟล์ local
    """
    last_err = None
    tried = []
    headers = _playback_request_headers(station)

    for url in urls:
        try:
            resp = HTTP.get(url, headers=headers, stream=True, timeout=HTTP_TIMEOUT, allow_redirects=True)
            ct = resp.headers.get("content-type", "")
            if resp.status_code != 200:
                last_err = f"HTTP {resp.status_code} {url}"
                tried.append(last_err)
                continue
            if "video" not in ct and "octet-stream" not in ct and "mp4" not in ct:
                prefix = resp.raw.read(120)
                last_err = f"unexpected content-type={ct!r} url={url} prefix={prefix!r}"
                tried.append(last_err)
                continue

            path = temp_path(".mp4")
            with open(path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=1024 * 256):
                    if chunk:
                        f.write(chunk)
            return path

        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
            tried.append(last_err)

    raise RuntimeError(" | ".join(tried[-4:]) if tried else (last_err or "download video failed"))


def probe_video_duration(video_path: str):
    """
    อ่าน duration จริงของ MP4 ด้วย ffprobe
    hourly/*.mp4 อาจไม่ได้ยาว 3600 วินาทีจริง
    """
    cmd = [
        "ffprobe",
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        video_path,
    ]
    res = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=60,
    )
    if res.returncode != 0:
        err = res.stderr.decode("utf-8", "ignore")[:800]
        raise RuntimeError(f"ffprobe failed: {err}")
    try:
        duration = float(res.stdout.decode("utf-8", "ignore").strip())
    except Exception:
        duration = 0.0
    if duration <= 0:
        raise RuntimeError("ffprobe duration <= 0")
    return duration


def extract_frame_from_local_video(video_path: str, target_seconds: float):
    """
    ลอง seek 2 รูปแบบ:
    1) accurate seek: -i ก่อน -ss
    2) fast seek: -ss ก่อน -i
    """
    target_seconds = max(0.0, float(target_seconds))
    attempts = [
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-i", video_path,
            "-ss", f"{target_seconds:.3f}",
            "-frames:v", "1",
            "-f", "image2pipe",
            "-vcodec", "mjpeg",
            "pipe:1",
        ],
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-ss", f"{target_seconds:.3f}",
            "-i", video_path,
            "-frames:v", "1",
            "-f", "image2pipe",
            "-vcodec", "mjpeg",
            "pipe:1",
        ],
    ]

    last = None
    for cmd in attempts:
        res = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=120,
        )
        if res.returncode == 0 and res.stdout:
            return res.stdout
        last = {
            "returncode": res.returncode,
            "stderr": res.stderr.decode("utf-8", "ignore")[:800],
            "stdout_len": len(res.stdout),
            "target_seconds": target_seconds,
        }

    raise RuntimeError(
        "ffmpeg extract local frame failed: "
        + json.dumps(last or {}, ensure_ascii=False)
    )


def wallclock_offset_to_video_seconds(offset_seconds: int, duration: float):
    """
    map นาทีภายในชั่วโมง -> ตำแหน่งจริงใน MP4

    ตัวอย่าง:
      นาที 30 = 1800/3600 = 50% ของไฟล์
    ไม่สมมติว่า MP4 ยาว 3600 วินาที
    """
    fraction = max(0.0, min(0.999, float(offset_seconds) / 3600.0))

    # หลีกเลี่ยงเฟรมแรกที่อาจยังดำ และท้ายไฟล์ที่ชน EOF
    if fraction <= 0:
        return min(max(0.15, duration * 0.002), max(0.0, duration - 0.15))

    target = duration * fraction
    if duration > 0.5:
        target = min(target, duration - 0.20)
    return max(0.0, target)


def extract_frames_for_tasks(tasks, progress_cb=None, progress_start=0, progress_end=100, progress_label='กำลังดึงเฟรม'):
    """
    รองรับทั้ง MP4 playback และภาพ CCTV ล่าสุด (snapshot)
    """
    if not tasks:
        return [], []

    results = []
    diagnostics = []

    snapshot_tasks = [t for t in tasks if t.get("source_type") == "snapshot"]
    video_tasks = [t for t in tasks if t.get("source_type") != "snapshot"]

    # snapshot ล่าสุด
    for task in snapshot_tasks:
        try:
            b = fetch_bytes(task["snapshot_url"] + f"?t={int(now_bkk().timestamp())}")
            results.append((task["slot"], b))
            diagnostics.append({
                "station": task["station"],
                "file": "LATEST_JPG",
                "slot": task["slot"].isoformat(),
                "ok_frames": 1,
            })
        except Exception as e:
            diagnostics.append({
                "station": task["station"],
                "file": "LATEST_JPG",
                "slot": task["slot"].isoformat(),
                "error": str(e),
            })

    groups = {}
    for task in video_tasks:
        groups.setdefault(
            (task["station"], task["rel_file"]),
            {
                "station": task["station"],
                "rel_file": task["rel_file"],
                "urls": task["urls"],
                "tasks": [],
            },
        )["tasks"].append(task)

    ordered_groups = sorted(
        groups.items(),
        key=lambda kv: min(t["slot"] for t in kv[1]["tasks"])
    )
    total_groups = max(1, len(ordered_groups))

    for idx, ((_, _), group) in enumerate(ordered_groups, start=1):
        if progress_cb:
            group_pct = ((idx - 1) / total_groups) * 100.0
            progress_cb(group_pct, f"กำลังประมวลผล {idx}/{total_groups}")
        video_path = None
        try:
            video_path = download_playback_video(group["urls"], group["station"])
            size_bytes = os.path.getsize(video_path)
            duration = probe_video_duration(video_path)

            ok_count = 0
            for task in sorted(group["tasks"], key=lambda x: x["slot"]):
                target = wallclock_offset_to_video_seconds(task["offset"], duration)
                try:
                    frame = extract_frame_from_local_video(video_path, target)
                    results.append((task["slot"], frame))
                    ok_count += 1
                except Exception as e:
                    diagnostics.append({
                        "station": group["station"],
                        "file": group["rel_file"],
                        "slot": task["slot"].isoformat(),
                        "wall_offset": task["offset"],
                        "duration": duration,
                        "target": target,
                        "error": str(e),
                    })

            diagnostics.append({
                "station": group["station"],
                "file": group["rel_file"],
                "size_bytes": size_bytes,
                "duration": duration,
                "requested_frames": len(group["tasks"]),
                "ok_frames": ok_count,
            })
            if progress_cb:
                group_pct = (idx / total_groups) * 100.0
                progress_cb(group_pct, f"ประมวลผลแล้ว {idx}/{total_groups}")

        except Exception as e:
            diagnostics.append({
                "station": group["station"],
                "file": group["rel_file"],
                "error": str(e),
            })
        finally:
            if video_path:
                remove_file(video_path)

    results.sort(key=lambda x: x[0])
    return results, diagnostics

def ffmpeg_extract_frame(urls, offset_seconds: int, station: str):
    """
    compatibility helper — ใช้เฉพาะ debug/legacy
    """
    video_path = None
    try:
        video_path = download_playback_video(urls, station)
        duration = probe_video_duration(video_path)
        target = wallclock_offset_to_video_seconds(offset_seconds, duration)
        return extract_frame_from_local_video(video_path, target)
    finally:
        if video_path:
            remove_file(video_path)

def _parse_meter(cell: str):
    m = re.search(r'([+-]?\d+(?:\.\d+)?)\s*(?:m\b|เมตร)', cell, re.I)
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


def _time_to_datetime(hhmm: str):
    h, m = map(int, hhmm.split(":"))
    n = now_bkk()
    candidate = n.replace(hour=h % 24, minute=m, second=0, microsecond=0)
    if candidate > n + timedelta(minutes=5):
        candidate -= timedelta(days=1)
    return candidate


def parse_water_history(html: str, station: str):
    soup = BeautifulSoup(html, "html.parser")
    rows = []
    station_col = None

    for tr in soup.find_all("tr"):
        cells = [c.get_text(" ", strip=True) for c in tr.find_all(["th", "td"])]
        if not cells:
            continue

        if any(station in c for c in cells):
            for i, c in enumerate(cells):
                if station in c:
                    station_col = i
                    break
            continue

        tm = None
        for c in cells:
            mt = re.fullmatch(r'\s*(\d{1,2}:\d{2})\s*', c)
            if mt:
                tm = mt.group(1)
                break
        if not tm:
            continue

        value = None
        if station_col is not None and station_col < len(cells):
            value = _parse_meter(cells[station_col])

        if value is None:
            meter_values = []
            for c in cells:
                v = _parse_meter(c)
                if v is not None:
                    meter_values.append(v)
            if len(meter_values) >= 2:
                value = meter_values[0] if station == "P.1" else meter_values[1]
            elif len(meter_values) == 1:
                value = meter_values[0]

        if value is not None:
            rows.append((_time_to_datetime(tm), value))

    if not rows:
        text = soup.get_text("\n", strip=True)
        for line in text.splitlines():
            m = re.search(
                r'(\d{1,2}:\d{2}).*?([+-]?\d+(?:\.\d+)?)\s*m.*?([+-]?\d+(?:\.\d+)?)\s*m',
                line, re.I
            )
            if m:
                dt = _time_to_datetime(m.group(1))
                val = float(m.group(2) if station == "P.1" else m.group(3))
                rows.append((dt, val))

    dedup = {}
    for dt, val in rows:
        dedup[dt.replace(second=0, microsecond=0)] = val
    return sorted(dedup.items(), key=lambda x: x[0])


def fetch_water_history(station: str):
    html = fetch_text(f"{APP_BASE}?station={station}&_={int(now_bkk().timestamp()*1000)}")
    return parse_water_history(html, station), html


def nearest_water_level(history, dt):
    if not history:
        return None
    near = min(history, key=lambda x: abs((x[0] - dt).total_seconds()))
    if abs((near[0] - dt).total_seconds()) > 2 * 3600:
        return None
    return near[1]


def hourly_water_level(history, dt):
    """
    ใช้ค่าระดับน้ำตามชั่วโมงเต็มสำหรับภาพล่าสุด/PNG
    """
    hour_dt = dt.astimezone(BKK).replace(minute=0, second=0, microsecond=0)
    return nearest_water_level(history, hour_dt), hour_dt


def playback_hour_water_level(history, slot_dt):
    """
    สำหรับ GIF playback ให้แสดงค่าระดับน้ำของ "ชั่วโมงปัจจุบัน" ของคลิป
    เช่นคลิปช่วง 11:00-12:00 ให้โชว์ค่าชั่วโมง 12:00
    เพื่อไม่ให้ข้อมูลช้ากว่าช่วงเวลาในคลิป 1 ชั่วโมง
    """
    start_hour = slot_dt.astimezone(BKK).replace(minute=0, second=0, microsecond=0)
    display_dt = start_hour + timedelta(hours=1)
    return nearest_water_level(history, display_dt), display_dt


def build_overall_period_text(slots):
    if not slots:
        return "-"
    first_slot = min(slots).astimezone(BKK)
    last_slot = max(slots).astimezone(BKK)
    return f"{first_slot.strftime('%d/%m/%Y %H:%M')} - {last_slot.strftime('%d/%m/%Y %H:%M')} น."


def build_period_text_from_cam(cam, slots=None):
    try:
        start_dt = datetime.fromisoformat(cam.get("requested_start_boundary"))
        end_dt = datetime.fromisoformat(cam.get("requested_end_boundary"))
        return f"{start_dt.astimezone(BKK).strftime('%d/%m/%Y %H:%M')} - {end_dt.astimezone(BKK).strftime('%d/%m/%Y %H:%M')} น."
    except Exception:
        return build_overall_period_text(slots or [])


def latest_water_level(history):
    if not history:
        return None, None
    latest_dt, latest_level = max(history, key=lambda x: x[0])
    return latest_level, latest_dt.astimezone(BKK)


def render_report_frame(cctv_bytes: bytes, station: str, captured_at: datetime, water_level, size: int, water_level_dt=None, fixed_period_text=None, zoom_timestamp=False):
    # ปรับโทนโดยรวมให้เป็นสีฟ้ามากขึ้น
    canvas = Image.new("RGB", (size, size), (10, 32, 60))
    draw = ImageDraw.Draw(canvas)

    margin = int(size * 0.032)
    header_h = int(size * 0.175)
    footer_h = int(size * 0.24)

    title_text = f"{station}  {STATIONS[station]}"
    title_font = fit_font_to_width(
        draw,
        title_text,
        max_width=size - (margin * 4),
        start_size=max(44, int(size * 0.078)),
        bold=True,
        min_size=24,
    )
    label_font = get_font(max(21, int(size * 0.032)), True)
    value_font = get_font(max(31, int(size * 0.054)), True)
    small_font = get_font(max(16, int(size * 0.024)), False)

    # Header
    draw.rounded_rectangle(
        (margin, margin, size - margin, margin + header_h),
        radius=int(size * 0.022),
        fill=(29, 79, 135),
    )
    title_w, title_h = text_size(draw, title_text, title_font)
    title_x = (size - title_w) // 2
    title_y = margin + ((header_h - title_h) // 2) - 6
    draw.text((title_x, title_y), title_text, font=title_font, fill="white")

    # Image area
    img_top = margin + header_h + int(size * 0.02)
    img_bottom = size - margin - footer_h - int(size * 0.02)
    img_left = margin
    img_right = size - margin
    image_box_w = img_right - img_left
    image_box_h = img_bottom - img_top

    try:
        orig = Image.open(io.BytesIO(cctv_bytes)).convert("RGB")
        # ใช้ contain แทน fit เพื่อไม่ให้ด้านบนโดนครอปจนเวลา CCTV มุมซ้ายบนหาย
        fitted = ImageOps.contain(
            orig,
            (image_box_w, image_box_h),
            method=Image.Resampling.LANCZOS,
        )
        image_panel = Image.new("RGB", (image_box_w, image_box_h), (16, 28, 44))
        paste_x = (image_box_w - fitted.width) // 2
        paste_y = (image_box_h - fitted.height) // 2
        image_panel.paste(fitted, (paste_x, paste_y))
        canvas.paste(image_panel, (img_left, img_top))

        if zoom_timestamp:
            # ขยายบริเวณวันที่เวลา มุมซ้ายบน โดยทำกรอบให้สั้นลงและเนื้อหาเต็มกรอบมากขึ้น
            crop_w = max(128, int(orig.width * 0.205))
            crop_h = max(24, int(orig.height * 0.055))
            crop_w = min(crop_w, orig.width)
            crop_h = min(crop_h, orig.height)
            ts_crop = orig.crop((0, 0, crop_w, crop_h))

            crop_ratio = crop_w / max(1, crop_h)
            inset_h = int(image_box_h * 0.105)
            inset_w = int(inset_h * crop_ratio)
            inset_w = min(inset_w, int(image_box_w * 0.30))
            inset_h = int(inset_w / crop_ratio)

            inset_x = img_left + int(size * 0.026)
            inset_y = img_top + int(size * 0.050)

            draw.rounded_rectangle(
                (inset_x - 4, inset_y - 4, inset_x + inset_w + 4, inset_y + inset_h + 4),
                radius=10,
                fill=(8, 18, 32),
                outline=(120, 180, 235),
                width=2,
            )

            # ใช้ fit เต็มกรอบ และดันภาพขึ้นด้านบนเล็กน้อยให้ตัวเลขอยู่ในจุดเด่น
            target_w = max(1, inset_w - 8)
            target_h = max(1, inset_h - 8)
            inset_img = ImageOps.fit(
                ts_crop,
                (target_w, target_h),
                method=Image.Resampling.NEAREST,
                centering=(0.0, 0.0),
            )
            inset_panel = Image.new("RGB", (inset_w, inset_h), (5, 10, 18))
            inset_panel.paste(inset_img, (4, 1))
            canvas.paste(inset_panel, (inset_x, inset_y))

            # เส้นชี้สั้น ๆ จาก timestamp เดิมลงมาหากรอบซูม
            callout_start = (img_left + int(size * 0.078), img_top + int(size * 0.030))
            callout_mid = (img_left + int(size * 0.083), img_top + int(size * 0.040))
            callout_end = (inset_x + int(inset_w * 0.22), inset_y)
            draw.line([callout_start, callout_mid, callout_end], fill=(120, 180, 235), width=2)
    except Exception:
        draw.rectangle((img_left, img_top, img_right, img_bottom), fill=(40, 56, 74))
        draw.text((img_left + 20, img_top + 20), "โหลดภาพ CCTV ไม่สำเร็จ", font=label_font, fill="white")

    # Footer panel
    y1 = size - margin - footer_h
    y2 = size - margin
    draw.rounded_rectangle(
        (margin, y1, size - margin, y2),
        radius=int(size * 0.022),
        fill=(21, 54, 95),
    )

    inner_pad_x = int(size * 0.020)
    inner_pad_y = int(size * 0.022)
    left_x = margin + inner_pad_x
    right_x = int(size * 0.54)
    top_y = y1 + inner_pad_y
    content_h = footer_h - (inner_pad_y * 2)

    # Separator
    sep_x = int(size * 0.505)
    draw.line((sep_x, y1 + inner_pad_y, sep_x, y2 - inner_pad_y), fill=(52, 94, 144), width=2)

    level_text = "-" if water_level is None else f"{water_level:.2f} เมตร"
    level_time = (water_level_dt or captured_at.astimezone(BKK).replace(minute=0, second=0, microsecond=0)).astimezone(BKK)
    current_dt_text = level_time.strftime("%d/%m/%Y %H:%M")

    period_start = captured_at.astimezone(BKK).replace(minute=0, second=0, microsecond=0)
    period_end = period_start + timedelta(hours=1)
    period_text = fixed_period_text or f"{period_start.strftime('%d/%m/%Y %H:%M')} - {period_end.strftime('%H:%M')} น."

    # LEFT BLOCK: current water level + latest fixed hour
    left_w = sep_x - left_x - inner_pad_x
    draw.text((left_x, top_y + int(content_h * 0.02)), "ระดับน้ำปัจจุบัน", font=label_font, fill=(190, 220, 252))
    draw.text((left_x, top_y + int(content_h * 0.30)), level_text, font=value_font, fill="white")

    left_time_font = fit_font_to_width(draw, current_dt_text, max_width=left_w, start_size=max(17, int(size * 0.028)), bold=False, min_size=13)
    draw.text((left_x, top_y + int(content_h * 0.70)), current_dt_text, font=left_time_font, fill=(216, 228, 240))

    # RIGHT BLOCK: overall CCTV range (fixed for the whole GIF when provided)
    right_w = size - margin - right_x - inner_pad_x
    draw.text((right_x, top_y + int(content_h * 0.02)), "CCTV", font=label_font, fill=(190, 220, 252))

    period_font = fit_font_to_width(draw, period_text, max_width=right_w, start_size=max(17, int(size * 0.027)), bold=False, min_size=12)
    period_lines = wrap_text_to_width(draw, period_text, period_font, max_width=right_w, max_lines=2)
    line_h = text_size(draw, "Ag", period_font)[1] + 5
    start_line_y = top_y + int(content_h * 0.35)
    for i, line in enumerate(period_lines):
        draw.text((right_x, start_line_y + (i * line_h)), line, font=period_font, fill="white" if i == 0 else (216, 228, 240))

    source_text = "ระบบโทรมาตร กรมชลประทาน"
    source_font = fit_font_to_width(draw, source_text, max_width=right_w, start_size=max(14, int(size * 0.022)), bold=False, min_size=11)
    source_y = y2 - inner_pad_y - text_size(draw, "Ag", source_font)[1]
    draw.text((right_x, source_y), source_text, font=source_font, fill=(193, 210, 229))

    return canvas

def latest_png(station: str):
    """
    คงชื่อฟังก์ชันเดิมไว้ แต่ผลลัพธ์เป็น JPG ตามการใช้งานล่าสุด
    """
    url, _ = latest_cache_jpg(station)
    dt = camera_timestamp_from_cache_url(url) or now_bkk()
    history, _ = fetch_water_history(station)
    level, level_dt = hourly_water_level(history, dt)
    b = fetch_bytes(url + f"?t={int(now_bkk().timestamp())}")
    img = render_report_frame(b, station, dt, level, PNG_SIZE, water_level_dt=level_dt).convert("RGB")
    path = temp_path(".jpg")
    img.save(path, "JPEG", quality=92, optimize=True)
    return path


def build_inclusive_hour_range_tasks(station: str, start_dt: datetime, end_hour_dt: datetime, step: int):
    """
    ดึง CCTV แบบ inclusive ที่ชั่วโมงปลายทาง

    ตัวอย่าง:
      start_dt = 12:00
      end_hour_dt = 13:00
    จะดึงช่วงจริง 12:00 ถึงก่อน 14:00
    หรือประมาณ 12:00-13:59

    ใช้ทั้ง hourly/.._12.mp4 และ hourly/.._13.mp4
    """
    cam = fetch_camlist(station)
    exp = cam.get("exp")
    sig = cam.get("sig")
    auth_source = "camlist" if (exp and sig) else None

    if not (exp and sig):
        discovered_exp, discovered_sig = try_discover_sig_from_page(station)
        if discovered_exp and discovered_sig:
            exp = discovered_exp
            sig = discovered_sig
            auth_source = "page"

    if not (exp and sig):
        env_exp, env_sig, source = playback_auth_from_env(station)
        if env_exp and env_sig:
            exp = env_exp
            sig = env_sig
            auth_source = source

    cam["auth_source"] = auth_source
    cam["exp"] = exp
    cam["sig"] = sig

    file_map = {}
    for rel in cam.get("files", []):
        dt = parse_hourly_filename(rel)
        if dt:
            file_map[dt] = rel

    start_dt = start_dt.astimezone(BKK).replace(second=0, microsecond=0)
    end_hour_dt = end_hour_dt.astimezone(BKK).replace(minute=0, second=0, microsecond=0)

    # สำคัญ: end hour เป็น inclusive จึงขยายไปอีก 1 ชั่วโมง
    exclusive_end = end_hour_dt + timedelta(hours=1)

    tasks = []
    cursor = start_dt
    while cursor < exclusive_end:
        hour_dt = cursor.replace(minute=0, second=0, microsecond=0)
        rel = file_map.get(hour_dt)
        if rel:
            offset = int((cursor - hour_dt).total_seconds())
            tasks.append({
                "station": station,
                "slot": cursor,
                "rel_file": rel,
                "offset": offset,
                "urls": playback_url_candidates(station, rel, exp=exp, sig=sig),
            })
        cursor += timedelta(minutes=step)

    cam["range_start"] = start_dt.isoformat()
    cam["range_end_inclusive_hour"] = end_hour_dt.isoformat()
    cam["range_exclusive_end"] = exclusive_end.isoformat()
    cam["task_count"] = len(tasks)
    return tasks, cam


def build_slot_tasks(station: str, hours: int, step: int):
    """
    v29: Rolling Window ตามเวลาปัจจุบันจริง

    ตัวอย่าง:
      ตอนนี้ 14:00, hours=1 -> 13:00 ถึง 14:00
      ตอนนี้ 14:30, hours=1 -> 13:30 ถึง 14:30

    ถ้า MP4 ของชั่วโมงปัจจุบันยังไม่ถูกสร้าง จะใช้ภาพ CCTV ล่าสุด
    เป็นเฟรมปลายทางแทน เพื่อไม่ให้ช่วงเวลาถอยไปตามไฟล์ playback ที่เสร็จแล้ว
    """
    cam = fetch_camlist(station)
    exp = cam.get("exp")
    sig = cam.get("sig")
    auth_source = "camlist" if (exp and sig) else None

    if not (exp and sig):
        discovered_exp, discovered_sig = try_discover_sig_from_page(station)
        if discovered_exp and discovered_sig:
            exp = exp or discovered_exp
            sig = sig or discovered_sig
            auth_source = "page"

    if not (exp and sig):
        env_exp, env_sig, source = playback_auth_from_env(station)
        if env_exp and env_sig:
            exp = env_exp
            sig = env_sig
            auth_source = source

    cam["auth_source"] = auth_source
    cam["exp"] = exp
    cam["sig"] = sig

    file_map = {}
    available_hours = []
    for rel in cam["files"]:
        dt = parse_hourly_filename(rel)
        if dt:
            file_map[dt] = rel
            available_hours.append(dt)

    available_hours.sort()

    # จบช่วงที่เวลาปัจจุบันจริง (ปัดวินาทีทิ้ง เพื่อให้ข้อความอ่านง่าย)
    end_boundary = now_bkk().replace(second=0, microsecond=0)
    start_boundary = end_boundary - timedelta(hours=hours)

    tasks = []
    cursor = start_boundary

    # รวมปลายช่วงด้วย เช่น 13:30, 14:30 เมื่อ step=60
    while cursor <= end_boundary:
        hour_dt = cursor.replace(minute=0, second=0, microsecond=0)
        rel = file_map.get(hour_dt)

        if rel:
            offset = int((cursor - hour_dt).total_seconds())
            tasks.append({
                "source_type": "video",
                "station": station,
                "slot": cursor,
                "rel_file": rel,
                "offset": offset,
                "urls": playback_url_candidates(
                    station,
                    rel,
                    exp=exp,
                    sig=sig,
                ),
            })
        elif cursor == end_boundary:
            # ชั่วโมงปัจจุบันยังเป็น file:null ได้ จึงใช้ CCTV ล่าสุดเป็นเฟรมปลายทาง
            try:
                latest_url, _ = latest_cache_jpg(station)
                tasks.append({
                    "source_type": "snapshot",
                    "station": station,
                    "slot": cursor,
                    "snapshot_url": latest_url,
                    "rel_file": "LATEST_JPG",
                    "offset": 0,
                    "urls": [],
                })
            except Exception:
                pass

        cursor += timedelta(minutes=step)

    # ถ้า step หารช่วงไม่ลงตัว ให้แน่ใจว่ามีเฟรมปลายทาง "ตอนนี้"
    if tasks and tasks[-1]["slot"] < end_boundary:
        hour_dt = end_boundary.replace(minute=0, second=0, microsecond=0)
        rel = file_map.get(hour_dt)
        if rel:
            tasks.append({
                "source_type": "video",
                "station": station,
                "slot": end_boundary,
                "rel_file": rel,
                "offset": int((end_boundary - hour_dt).total_seconds()),
                "urls": playback_url_candidates(station, rel, exp=exp, sig=sig),
            })
        else:
            try:
                latest_url, _ = latest_cache_jpg(station)
                tasks.append({
                    "source_type": "snapshot",
                    "station": station,
                    "slot": end_boundary,
                    "snapshot_url": latest_url,
                    "rel_file": "LATEST_JPG",
                    "offset": 0,
                    "urls": [],
                })
            except Exception:
                pass

    uniq = []
    seen = set()
    for t in tasks:
        key = (t["slot"], t.get("rel_file"), t.get("offset"), t.get("source_type"))
        if key not in seen:
            seen.add(key)
            uniq.append(t)

    cam["latest_available_hour"] = available_hours[-1].isoformat() if available_hours else None
    cam["requested_start_boundary"] = start_boundary.isoformat()
    cam["requested_end_boundary"] = end_boundary.isoformat()
    cam["playback_end_boundary"] = end_boundary.isoformat()
    cam["task_count"] = len(uniq)

    return uniq, cam

def extract_task_frame(task):
    b = ffmpeg_extract_frame(task["urls"], task["offset"], task["station"])
    return task["slot"], b


def build_gif(station: str, hours: int, step: int, progress_cb=None):
    if progress_cb:
        progress_cb(3, f"กำลังเตรียมข้อมูล {station}...")
    tasks, cam = build_slot_tasks(station, hours, step)
    if len(tasks) < 2:
        raise RuntimeError("จำนวนช่วงเวลาที่สร้างได้ไม่พอสำหรับทำ GIF")

    if progress_cb:
        progress_cb(10, "กำลังอ่านระดับน้ำ...")
    history, _ = fetch_water_history(station)

    if progress_cb:
        progress_cb(15, "กำลังดึงภาพ CCTV...")
    results, diagnostics = extract_frames_for_tasks(
        tasks,
        progress_cb=progress_cb,
        progress_start=15,
        progress_end=75,
        progress_label=f"{station} ดาวน์โหลด/สกัดเฟรม",
    )
    if len(results) < 2:
        raise RuntimeError(
            "ดึงเฟรมจากวิดีโอได้ไม่พอสำหรับทำ GIF | "
            + json.dumps(diagnostics[-8:], ensure_ascii=False, default=str)
        )

    if progress_cb:
        progress_cb(80, "กำลังจัดทำภาพ...")
    frames = []
    total_results = max(1, len(results))
    slots_only = [slot for slot, _ in results]
    fixed_period_text = build_period_text_from_cam(cam, slots_only)
    fixed_level, fixed_level_dt = latest_water_level(history)
    if fixed_level_dt is None:
        latest_slot_hour = max(slots_only).astimezone(BKK).replace(minute=0, second=0, microsecond=0)
        fixed_level = nearest_water_level(history, latest_slot_hour)
        fixed_level_dt = latest_slot_hour
    for idx, (slot, b) in enumerate(results, start=1):
        fr = render_report_frame(
            b,
            station,
            slot,
            fixed_level,
            GIF_SIZE,
            water_level_dt=fixed_level_dt,
            fixed_period_text=fixed_period_text,
        )
        fr = fr.quantize(colors=128, method=Image.Quantize.MEDIANCUT)
        frames.append(fr)
        if progress_cb and idx % max(1, total_results // 5) == 0:
            progress_cb(80 + (idx / total_results) * 10, f"กำลังจัดวางภาพ {idx}/{total_results}")

    if progress_cb:
        progress_cb(92, "กำลังสร้าง GIF...")
    path = temp_path(".gif")
    frames[0].save(
        path,
        save_all=True,
        append_images=frames[1:],
        duration=GIF_DURATION_MS,
        loop=0,
        optimize=False,
        disposal=2,
    )
    if progress_cb:
        progress_cb(100, "เสร็จแล้ว")
    return path, len(frames), len(tasks), {
        **cam,
        "frame_diagnostics": diagnostics[-20:],
    }

def build_combined_gif(hours: int, step: int, progress_cb=None):
    if progress_cb:
        progress_cb(3, "กำลังเตรียมข้อมูลสำหรับ P.1 และ P.67...")
    p1_tasks, p1_cam = build_slot_tasks("P.1", hours, step)
    p67_tasks, p67_cam = build_slot_tasks("P.67", hours, step)

    map1 = {t["slot"]: t for t in p1_tasks}
    map67 = {t["slot"]: t for t in p67_tasks}
    common_slots = sorted(set(map1.keys()) & set(map67.keys()))

    if len(common_slots) < 2:
        raise RuntimeError(
            "ช่วงเวลาที่ P.1 และ P.67 ซ้อนกันมีไม่พอสำหรับ GIF เปรียบเทียบ"
        )

    p1_common = [map1[s] for s in common_slots]
    p67_common = [map67[s] for s in common_slots]

    if progress_cb:
        progress_cb(10, "กำลังดึงภาพ P.1...")
    p1_results, p1_diag = extract_frames_for_tasks(
        p1_common,
        progress_cb=job_progress_callback("__tmp__", 0, 0) if False else (lambda p,m: progress_cb(10 + p * 0.3, m) if progress_cb else None),
        progress_label="P.1 ดาวน์โหลด/สกัดเฟรม",
    )
    if progress_cb:
        progress_cb(40, "กำลังดึงภาพ P.67...")
    p67_results, p67_diag = extract_frames_for_tasks(
        p67_common,
        progress_cb=(lambda p,m: progress_cb(40 + p * 0.3, m) if progress_cb else None),
        progress_label="P.67 ดาวน์โหลด/สกัดเฟรม",
    )

    bytes1 = {slot: b for slot, b in p1_results}
    bytes67 = {slot: b for slot, b in p67_results}

    if progress_cb:
        progress_cb(72, "กำลังอ่านระดับน้ำ...")
    h1, _ = fetch_water_history("P.1")
    h67, _ = fetch_water_history("P.67")

    frames = []
    total_slots = max(1, len(common_slots))
    try:
        p1_start = datetime.fromisoformat(p1_cam.get("requested_start_boundary"))
        p67_start = datetime.fromisoformat(p67_cam.get("requested_start_boundary"))
        p1_end = datetime.fromisoformat(p1_cam.get("requested_end_boundary"))
        p67_end = datetime.fromisoformat(p67_cam.get("requested_end_boundary"))
        period_start = max(p1_start, p67_start)
        period_end = min(p1_end, p67_end)
        fixed_period_text = f"{period_start.astimezone(BKK).strftime('%d/%m/%Y %H:%M')} - {period_end.astimezone(BKK).strftime('%d/%m/%Y %H:%M')} น."
    except Exception:
        fixed_period_text = build_overall_period_text(common_slots)
    fixed_left_level, fixed_left_dt = latest_water_level(h1)
    fixed_right_level, fixed_right_dt = latest_water_level(h67)
    if fixed_left_dt is None:
        fixed_left_dt = max(common_slots).astimezone(BKK).replace(minute=0, second=0, microsecond=0)
        fixed_left_level = nearest_water_level(h1, fixed_left_dt)
    if fixed_right_dt is None:
        fixed_right_dt = max(common_slots).astimezone(BKK).replace(minute=0, second=0, microsecond=0)
        fixed_right_level = nearest_water_level(h67, fixed_right_dt)
    for idx, slot in enumerate(common_slots, start=1):
        if slot not in bytes1 or slot not in bytes67:
            continue

        left = render_report_frame(
            bytes1[slot],
            "P.1",
            slot,
            fixed_left_level,
            GIF_SIZE,
            water_level_dt=fixed_left_dt,
            fixed_period_text=fixed_period_text,
        )
        right = render_report_frame(
            bytes67[slot],
            "P.67",
            slot,
            fixed_right_level,
            GIF_SIZE,
            water_level_dt=fixed_right_dt,
            fixed_period_text=fixed_period_text,
        )

        canvas = Image.new(
            "RGB",
            (GIF_SIZE * 2, GIF_SIZE),
            (10, 20, 34),
        )
        canvas.paste(left, (0, 0))
        canvas.paste(right, (GIF_SIZE, 0))
        frames.append(
            canvas.quantize(
                colors=128,
                method=Image.Quantize.MEDIANCUT,
            )
        )
        if progress_cb and idx % max(1, total_slots // 5) == 0:
            progress_cb(75 + (idx / total_slots) * 15, f"กำลังประกอบภาพ {idx}/{total_slots}")

    if len(frames) < 2:
        raise RuntimeError(
            "สร้าง GIF เปรียบเทียบไม่สำเร็จ | "
            + json.dumps({
                "p1": p1_diag[-6:],
                "p67": p67_diag[-6:],
            }, ensure_ascii=False, default=str)
        )

    if progress_cb:
        progress_cb(94, "กำลังสร้าง GIF...")
    path = temp_path(".gif")
    frames[0].save(
        path,
        save_all=True,
        append_images=frames[1:],
        duration=GIF_DURATION_MS,
        loop=0,
        optimize=False,
        disposal=2,
    )

    if progress_cb:
        progress_cb(100, "เสร็จแล้ว")
    return path, len(frames), len(common_slots), {
        "P.1": p1_cam,
        "P.67": p67_cam,
        "diagnostics": {
            "P.1": p1_diag[-20:],
            "P.67": p67_diag[-20:],
        },
    }


HTML = """
<!doctype html>
<html lang=\"th\">
<head>
<meta charset=\"utf-8\">
<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">
<title>CCTV Ping River v31</title>
<style>
:root{color-scheme:dark}
*{box-sizing:border-box}
body{margin:0;font-family:system-ui,-apple-system,\"Noto Sans Thai\",sans-serif;background:#07111f;color:#eef5ff}
.wrap{max-width:1120px;margin:auto;padding:24px}
h1{font-size:clamp(26px,4vw,44px);margin:0 0 6px}
.sub{color:#99acc4;margin-bottom:24px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:18px}
.card{background:#0e2038;border:1px solid #203a5c;border-radius:18px;padding:18px;box-shadow:0 16px 40px #0005}
.preview{aspect-ratio:16/9;background:#06101d;border-radius:14px;overflow:hidden;display:flex;align-items:center;justify-content:center}
.preview img{width:100%;height:100%;object-fit:cover}
.row{display:flex;gap:8px;flex-wrap:wrap;margin-top:12px}
button,a.btn{border:0;border-radius:10px;padding:10px 14px;background:#2373d8;color:white;text-decoration:none;cursor:pointer;font-weight:700}
button.alt,a.alt{background:#244260}
select{background:#0a1728;color:white;border:1px solid #36506c;padding:9px;border-radius:9px}
.status{font-size:14px;color:#a9bad0;min-height:22px;margin-top:8px}
.big{font-size:28px;font-weight:800;margin:9px 0}
.tools{margin-top:18px}
.note{margin-top:20px;padding:14px;border-radius:12px;background:#102a44;color:#bcd0e5}
.progress-wrap{margin-top:10px;background:#0a1728;border:1px solid #284664;border-radius:999px;overflow:hidden;height:14px}
.progress-bar{height:100%;width:0%;background:#2f86ff;transition:width .25s ease}
.muted{color:#8ea6c5;font-size:13px;margin-top:8px}
</style>
</head>
<body>
<div class=\"wrap\">
  <h1>🌊 CCTV Ping River v31</h1>
  
  <div class=\"grid\">
    <section class=\"card\" data-station=\"P.67\">
      <h2>P.67 บ้านแม่แต</h2>
      <div class=\"preview\"><img src=\"/camera/latest?station=P.67&t=1\" alt=\"P.67 CCTV\"></div>
      <div class=\"big\" id=\"level-P67\">กำลังอ่านระดับน้ำ…</div>
      <div class=\"status\" id=\"status-P67\"></div>
      <div class=\"row\">
        <button class=\"btn\" onclick=\"saveLatest('P.67')\">รูป CCTV ล่าสุด</button>
        <button class=\"alt\" onclick=\"refreshCamera('P.67')\">รีเฟรช CCTV</button>
      </div>
    </section>

    <section class=\"card\" data-station=\"P.1\">
      <h2>P.1 สะพานนวรัฐ</h2>
      <div class=\"preview\"><img src=\"/camera/latest?station=P.1&t=1\" alt=\"P.1 CCTV\"></div>
      <div class=\"big\" id=\"level-P1\">กำลังอ่านระดับน้ำ…</div>
      <div class=\"status\" id=\"status-P1\"></div>
      <div class=\"row\">
        <button class=\"btn\" onclick=\"saveLatest('P.1')\">รูป CCTV ล่าสุด</button>
        <button class=\"alt\" onclick=\"refreshCamera('P.1')\">รีเฟรช CCTV</button>
      </div>
    </section>
  </div>

  <section class=\"card tools\">
    <h2>สร้างไฟล์ภาพ .GIF การเปลี่ยนแปลงระดับน้ำ</h2>
    <div class=\"row\">
      <label>ย้อนหลัง
        <select id=\"hours\">
          <option value=\"1\">1 ชั่วโมง</option>
          <option value=\"3\">3 ชั่วโมง</option>
          <option value=\"6\">6 ชั่วโมง</option>
          <option value=\"12\">12 ชั่วโมง</option>
          <option value=\"24\" selected>24 ชั่วโมง</option>
          <option value=\"48\">48 ชั่วโมง</option>
          <option value=\"72\">72 ชั่วโมง</option>
        </select>
      </label>
      <label>ดึงภาพทุก
        <select id=\"step\">
          <option value=\"5\">5 นาที</option>
          <option value=\"10\">10 นาที</option>
          <option value=\"15\">15 นาที</option>
          <option value=\"30\">30 นาที</option>
          <option value=\"60\" selected>1 ชั่วโมง</option>
        </select>
      </label>
    </div>
    <div class=\"row\">
      <button onclick=\"makeGif('P.1')\">P.1</button>
      <button onclick=\"makeGif('P.67')\">P.67</button>
      <button class=\"alt\" onclick=\"makeCombined()\">P.1 + P.67</button>
      <button class=\"alt\" onclick=\"checkHistory()\">ตรวจไฟล์ Playback</button>
    </div>
    <div class=\"progress-wrap\"><div class=\"progress-bar\" id=\"workProgressBar\"></div></div>
    <div class=\"muted\" id=\"workProgressText\">พร้อมสร้าง GIF</div>
  </section>
</div>
<script>
const id = s => s.replace('.','');
let currentJobPoll = null;

function setWorkProgress(percent, message){
  const bar=document.getElementById('workProgressBar');
  const txt=document.getElementById('workProgressText');
  bar.style.width=`${Math.max(0, Math.min(100, percent||0))}%`;
  txt.textContent=message || '';
}

async function loadStatus(station){
  const el=document.getElementById('status-'+id(station));
  const lv=document.getElementById('level-'+id(station));
  try{
    const r=await fetch('/api/status?station='+encodeURIComponent(station));
    const j=await r.json();
    if(!r.ok) throw new Error(j.detail||'error');
    lv.textContent = j.water_level==null ? 'ระดับน้ำ: -' : `ระดับน้ำ ${j.water_level.toFixed(2)} เมตร`;
    el.textContent=`CCTV ล่าสุด ${j.camera_time||'-'} | Playback files ${j.playback_count}`;
  }catch(e){
    lv.textContent='อ่านข้อมูลไม่ได้';
    el.textContent=e.message;
  }
}
function refreshCamera(station){
  const card=document.querySelector(`[data-station="${station}"]`);
  const img=card.querySelector('img');
  const status=document.getElementById('status-'+id(station));
  status.textContent='กำลังรีเฟรชภาพ CCTV...';
  img.onload = () => {
    status.textContent='รีเฟรชภาพ CCTV สำเร็จ';
    loadStatus(station);
  };
  img.onerror = () => {
    status.textContent='รีเฟรชภาพ CCTV ไม่สำเร็จ';
  };
  img.src='/camera/latest?station='+encodeURIComponent(station)+'&t='+Date.now();
}
async function saveLatest(station){
  const status=document.getElementById('status-'+id(station));
  status.textContent='กำลังสร้างและบันทึกภาพล่าสุด...';
  try{
    const r = await fetch('/download/jpg?station='+encodeURIComponent(station));
    if(!r.ok){
      const txt = await r.text();
      throw new Error(txt || 'download failed');
    }
    const blob = await r.blob();
    const cd = r.headers.get('Content-Disposition') || '';
    let filename = `${station.replace('.', '')}_latest.jpg`;
    const m = cd.match(/filename="?([^";]+)"?/i);
    if(m && m[1]) filename = m[1];
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
    status.textContent='บันทึกภาพล่าสุดสำเร็จ';
  }catch(e){
    status.textContent='บันทึกภาพล่าสุดไม่สำเร็จ: ' + e.message;
  }
}
async function startJob(url){
  if(currentJobPoll){
    clearInterval(currentJobPoll);
    currentJobPoll=null;
  }
  setWorkProgress(2, 'กำลังเริ่มงาน...');
  try{
    const r = await fetch(url);
    const j = await r.json();
    if(!r.ok) throw new Error(j.detail || 'start job failed');
    pollJob(j.job_id);
  }catch(e){
    setWorkProgress(0, 'เริ่มงานไม่สำเร็จ: ' + e.message);
  }
}
function makeGif(station){
  const h=document.getElementById('hours').value;
  const s=document.getElementById('step').value;
  startJob(`/api/job/start-gif?station=${encodeURIComponent(station)}&hours=${h}&step=${s}`);
}
function makeCombined(){
  const h=document.getElementById('hours').value;
  const s=document.getElementById('step').value;
  startJob(`/api/job/start-gif-combined?hours=${h}&step=${s}`);
}
async function pollJob(jobId){
  setWorkProgress(3, 'กำลังประมวลผล...');
  const poll = async () => {
    try{
      const r = await fetch(`/api/job-status?job_id=${encodeURIComponent(jobId)}`);
      const j = await r.json();
      if(!r.ok) throw new Error(j.detail || 'status failed');

      setWorkProgress(j.progress || 0, j.message || 'กำลังทำงาน...');

      if(j.status === 'done'){
        clearInterval(currentJobPoll);
        currentJobPoll = null;
        setWorkProgress(100, 'เสร็จแล้ว กำลังดาวน์โหลดไฟล์...');
        window.location.href = `/api/job-download?job_id=${encodeURIComponent(jobId)}`;
      }else if(j.status === 'error'){
        clearInterval(currentJobPoll);
        currentJobPoll = null;
        setWorkProgress(j.progress || 0, 'เกิดข้อผิดพลาด: ' + (j.error || 'ไม่ทราบสาเหตุ'));
      }
    }catch(e){
      setWorkProgress(0, 'ตรวจสถานะไม่สำเร็จ: ' + e.message);
    }
  };
  await poll();
  currentJobPoll = setInterval(poll, 1500);
}
async function checkHistory(){
  const h=document.getElementById('hours').value;
  const el=document.getElementById('workProgressText');
  el.textContent='กำลังตรวจ...';
  try{
    const [a,b]=await Promise.all([
      fetch(`/api/history-check?station=P.1&hours=${h}`).then(r=>r.json()),
      fetch(`/api/history-check?station=P.67&hours=${h}`).then(r=>r.json())
    ]);
    el.textContent=`P.1: ${a.in_period}/${a.total_files} ไฟล์ชั่วโมง | P.67: ${b.in_period}/${b.total_files} ไฟล์ชั่วโมง`;
  }catch(e){el.textContent='ตรวจไม่สำเร็จ: '+e.message}
}
loadStatus('P.1'); loadStatus('P.67');
</script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
def home():
    return HTMLResponse(HTML)


@app.get("/health")
def health():
    return {"ok": True, "time": now_bkk().isoformat()}


@app.get("/api/status")
def api_status(station: str = Query(...)):
    station = validate_station(station)

    latest_url = None
    latest_dt = None
    level = None
    playback_count = 0
    exp = None
    sig_present = False
    errors = {}

    try:
        latest_url, _ = latest_cache_jpg(station)
        latest_dt = camera_timestamp_from_cache_url(latest_url)
    except Exception as e:
        errors["latest_camera"] = str(e)

    try:
        history, _ = fetch_water_history(station)
        level = nearest_water_level(history, latest_dt or now_bkk())
    except Exception as e:
        errors["water_level"] = str(e)

    try:
        cam = fetch_camlist(station)
        playback_count = len(cam["files"])
        exp = cam.get("exp")
        sig_present = bool(cam.get("sig"))
    except Exception as e:
        errors["playback"] = str(e)

    return {
        "station": station,
        "name": STATIONS[station],
        "water_level": level,
        "camera_time": latest_dt.strftime("%d/%m/%Y %H:%M:%S") if latest_dt else None,
        "latest_camera_ok": bool(latest_url),
        "playback_count": playback_count,
        "exp": exp,
        "sig_present": sig_present,
        "errors": errors,
    }


@app.get("/api/history-check")
def history_check(station: str = Query(...), hours: int = Query(24, ge=1, le=72)):
    station = validate_station(station)
    try:
        cam = fetch_camlist(station)
        dts = [parse_hourly_filename(x) for x in cam["files"]]
        dts = sorted([x for x in dts if x])

        end_boundary = now_bkk().replace(second=0, microsecond=0)
        cutoff = end_boundary - timedelta(hours=hours)

        # นับไฟล์รายชั่วโมงที่ทับซ้อนกับ rolling window
        in_period = [
            x for x in dts
            if (x + timedelta(hours=1)) > cutoff and x <= end_boundary
        ]

        return {
            "station": station,
            "hours": hours,
            "total_files": len(dts),
            "in_period": len(in_period),
            "first": dts[0].isoformat() if dts else None,
            "last": dts[-1].isoformat() if dts else None,
            "latest_available_hour": dts[-1].isoformat() if dts else None,
            "requested_start_boundary": cutoff.isoformat(),
            "requested_end_boundary": end_boundary.isoformat(),
            "sig_present": bool(cam.get("sig")),
            "exp": cam.get("exp"),
        }
    except Exception as e:
        raise HTTPException(502, f"ตรวจ playback ไม่สำเร็จ: {e}")


@app.get("/api/debug/video-probe")
def api_debug_video_probe(station: str = Query(...)):
    station = validate_station(station)
    tasks, cam = build_slot_tasks(station, hours=1, step=30)

    if not tasks:
        raise HTTPException(
            502,
            {
                "message": "ไม่มี task สำหรับทดสอบ",
                "latest_available_hour": cam.get("latest_available_hour"),
                "playback_end_boundary": cam.get("playback_end_boundary"),
                "requested_start_boundary": cam.get("requested_start_boundary"),
                "files": len(cam.get("files", [])),
            },
        )

    task = tasks[0]
    video_path = None
    try:
        video_path = download_playback_video(task["urls"], station)
        size_bytes = os.path.getsize(video_path)
        duration = probe_video_duration(video_path)

        tests = []
        for fraction in (0.0, 0.25, 0.5, 0.75):
            target = wallclock_offset_to_video_seconds(
                int(fraction * 3600),
                duration,
            )
            try:
                b = extract_frame_from_local_video(video_path, target)
                tests.append({
                    "fraction": fraction,
                    "target_seconds": target,
                    "jpeg_bytes": len(b),
                    "ok": True,
                })
            except Exception as e:
                tests.append({
                    "fraction": fraction,
                    "target_seconds": target,
                    "ok": False,
                    "error": str(e),
                })

        return {
            "station": station,
            "latest_available_hour": cam.get("latest_available_hour"),
            "playback_end_boundary": cam.get("playback_end_boundary"),
            "file": task["rel_file"],
            "size_bytes": size_bytes,
            "duration_seconds": duration,
            "tests": tests,
        }
    finally:
        if video_path:
            remove_file(video_path)



@app.get("/api/debug/playback-url-sample")
def api_debug_playback_url_sample(station: str = Query(...)):
    station = validate_station(station)
    tasks, cam = build_slot_tasks(station, hours=1, step=30)
    sample_task = tasks[0] if tasks else None
    return {
        "station": station,
        "auth_source": cam.get("auth_source"),
        "has_exp": bool(cam.get("exp")),
        "has_sig": bool(cam.get("sig")),
        "sample_urls": (sample_task or {}).get("urls", []),
    }


@app.get("/api/debug/session-camlist")
def api_debug_session_camlist(station: str = Query(...)):
    station = validate_station(station)
    try:
        cam = fetch_camlist(station)
        return {
            "station": station,
            "files": len(cam.get("files", [])),
            "has_exp": bool(cam.get("exp")),
            "has_sig": bool(cam.get("sig")),
            "exp": cam.get("exp"),
            "auth_shape": cam.get("auth_shape"),
            "cookie_names": cam.get("cookie_names", []),
            "prime_info": cam.get("prime_info"),
            "raw_tail": cam.get("raw_tail", "")[-900:],
        }
    except Exception as e:
        raise HTTPException(502, str(e))


@app.get("/api/debug/playback-auth")
def api_debug_playback_auth(station: str = Query(...)):
    station = validate_station(station)
    cam_exp = cam_sig = None
    page_exp = page_sig = None
    try:
        cam = fetch_camlist(station)
        cam_exp = cam.get("exp")
        cam_sig = cam.get("sig")
    except Exception:
        pass
    try:
        page_exp, page_sig = try_discover_sig_from_page(station)
    except Exception:
        pass

    env_exp, env_sig, env_source = playback_auth_from_env(station)
    return {
        "station": station,
        "camlist_has_exp": bool(cam_exp),
        "camlist_has_sig": bool(cam_sig),
        "page_has_exp": bool(page_exp),
        "page_has_sig": bool(page_sig),
        "env_has_exp": bool(env_exp),
        "env_has_sig": bool(env_sig),
        "env_source": env_source,
        "effective_source_if_needed": env_source if (env_exp and env_sig) else None,
    }


@app.get("/api/debug/camlist")
def debug_camlist(station: str = Query(...)):
    station = validate_station(station)
    try:
        cam = fetch_camlist(station)
        exp2, sig2 = try_discover_sig_from_page(station)
        env_exp, env_sig, env_source = playback_auth_from_env(station)
        return {
            "station": station,
            "count": len(cam["files"]),
            "sample_files": cam["files"][:10],
            "exp_from_camlist": cam.get("exp"),
            "sig_from_camlist": bool(cam.get("sig")),
            "auth_shape": cam.get("auth_shape"),
            "cookie_names": cam.get("cookie_names", []),
            "exp_from_page": exp2,
            "sig_from_page": bool(sig2),
            "exp_from_env": env_exp,
            "sig_from_env": bool(env_sig),
            "env_source": env_source,
            "raw_preview": cam["raw_preview"],
        }
    except Exception as e:
        raise HTTPException(502, str(e))


@app.get("/api/debug/latest-sources")
def api_debug_latest_sources(station: str = Query(...)):
    station = validate_station(station)
    return debug_latest_sources(station)


@app.get("/api/debug/latest")
def debug_latest(station: str = Query(...)):
    station = validate_station(station)
    out = {"station": station}
    try:
        url, urls = latest_cache_jpg(station)
        dt = camera_timestamp_from_cache_url(url)
        out.update({
            "ok": True,
            "latest_url": url,
            "count": len(urls),
            "camera_time": dt.isoformat() if dt else None,
        })
    except Exception as e:
        out.update({"ok": False, "error": str(e)})
    return out


@app.get("/camera/latest")
def camera_latest(station: str = Query(...)):
    station = validate_station(station)
    try:
        url, _ = latest_cache_jpg(station)
        b = fetch_bytes(url + f"?t={int(now_bkk().timestamp())}")
        return StreamingResponse(io.BytesIO(b), media_type="image/jpeg",
                                 headers={"Cache-Control": "no-store"})
    except Exception as e:
        raise HTTPException(502, f"โหลด CCTV ไม่สำเร็จ: {e}")


@app.get("/download/jpg")
def download_jpg(station: str = Query(...)):
    station = validate_station(station)
    try:
        path = latest_png(station)
        filename = f"{station.replace('.','')}_latest_{now_bkk():%Y%m%d_%H%M}.jpg"
        return FileResponse(path, media_type="image/jpeg", filename=filename,
                            background=BackgroundTask(remove_file, path))
    except Exception as e:
        raise HTTPException(502, f"บันทึกภาพ JPG ไม่สำเร็จ: {e}")


@app.get("/download/png")
def download_png(station: str = Query(...)):
    # alias เดิมเพื่อ backward compatibility
    return download_jpg(station)



def _run_gif_job(job_id: str, station: str, hours: int, step: int):
    try:
        cb = job_progress_callback(job_id)
        update_job(job_id, status="running", progress=1, message="เริ่มงานสร้าง GIF...")
        path, frames, tasks, cam = build_gif(station, hours, step, progress_cb=cb)
        filename = f"{station.replace('.','')}_{hours}h_step{step}m_{frames}frames.gif"
        update_job(
            job_id,
            status="done",
            progress=100,
            message=f"สร้างเสร็จแล้ว {frames} เฟรม พร้อมดาวน์โหลด",
            path=path,
            filename=filename,
            meta={"frames": frames, "tasks": tasks, "cam": cam},
        )
    except Exception as e:
        update_job(
            job_id,
            status="error",
            message="สร้าง GIF ไม่สำเร็จ",
            error=str(e),
        )


def _run_combined_gif_job(job_id: str, hours: int, step: int):
    try:
        cb = job_progress_callback(job_id)
        update_job(job_id, status="running", progress=1, message="เริ่มงานสร้าง GIF เปรียบเทียบ...")
        path, frames, slots, meta = build_combined_gif(hours, step, progress_cb=cb)
        filename = f"P1_P67_compare_{hours}h_step{step}m_{frames}frames.gif"
        update_job(
            job_id,
            status="done",
            progress=100,
            message=f"สร้างเสร็จแล้ว {frames} เฟรม พร้อมดาวน์โหลด",
            path=path,
            filename=filename,
            meta={"frames": frames, "slots": slots, "meta": meta},
        )
    except Exception as e:
        update_job(
            job_id,
            status="error",
            message="สร้าง GIF เปรียบเทียบไม่สำเร็จ",
            error=str(e),
        )


def parse_local_bkk_datetime(value: str):
    try:
        dt = datetime.fromisoformat(value)
    except Exception:
        raise HTTPException(400, "รูปแบบเวลาต้องเป็น YYYY-MM-DDTHH:MM")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=BKK)
    else:
        dt = dt.astimezone(BKK)
    return dt


def build_gif_range(station: str, start_dt: datetime, end_hour_dt: datetime, step: int, progress_cb=None):
    if progress_cb:
        progress_cb(3, f"กำลังเตรียม CCTV {station} ตามช่วงเวลาที่เลือก...")
    tasks, cam = build_inclusive_hour_range_tasks(station, start_dt, end_hour_dt, step)
    if len(tasks) < 2:
        raise RuntimeError("ช่วงเวลาที่เลือกมีภาพ CCTV ไม่เพียงพอ")

    if progress_cb:
        progress_cb(10, "กำลังอ่านข้อมูลระดับน้ำ...")
    history, _ = fetch_water_history(station)

    results, diagnostics = extract_frames_for_tasks(
        tasks,
        progress_cb=progress_cb,
        progress_start=15,
        progress_end=75,
        progress_label=f"{station} ดาวน์โหลด/สกัดเฟรม",
    )
    if len(results) < 2:
        raise RuntimeError(
            "ดึงเฟรมจากวิดีโอได้ไม่พอ | "
            + json.dumps(diagnostics[-8:], ensure_ascii=False, default=str)
        )

    frames = []
    for slot, b in results:
        level, level_dt = playback_hour_water_level(history, slot)
        fr = render_report_frame(b, station, slot, level, GIF_SIZE, water_level_dt=level_dt)
        frames.append(fr.quantize(colors=128, method=Image.Quantize.MEDIANCUT))

    if progress_cb:
        progress_cb(94, "กำลังเข้ารหัส GIF...")
    path = temp_path(".gif")
    frames[0].save(
        path,
        save_all=True,
        append_images=frames[1:],
        duration=GIF_DURATION_MS,
        loop=0,
        optimize=False,
        disposal=2,
    )
    if progress_cb:
        progress_cb(100, "เสร็จแล้ว")
    return path, len(frames), len(tasks), cam


def _run_gif_range_job(job_id: str, station: str, start_dt: datetime, end_hour_dt: datetime, step: int):
    try:
        cb = job_progress_callback(job_id)
        update_job(job_id, status="running", progress=1, message="เริ่มงานสร้าง GIF ตามช่วงเวลา...")
        path, frames, tasks, cam = build_gif_range(
            station,
            start_dt,
            end_hour_dt,
            step,
            progress_cb=cb,
        )
        filename = (
            f"{station.replace('.','')}_"
            f"{start_dt:%Y%m%d_%H%M}-"
            f"{end_hour_dt:%Y%m%d_%H}59_"
            f"{frames}frames.gif"
        )
        update_job(
            job_id,
            status="done",
            progress=100,
            message=f"สร้างเสร็จแล้ว {frames} เฟรม พร้อมดาวน์โหลด",
            path=path,
            filename=filename,
            meta={"frames": frames, "tasks": tasks, "cam": cam},
        )
    except Exception as e:
        update_job(job_id, status="error", message="สร้าง GIF ไม่สำเร็จ", error=str(e))


@app.get("/api/job/start-gif-range")
def api_job_start_gif_range(
    station: str = Query(...),
    start: str = Query(..., description="YYYY-MM-DDTHH:MM เวลาไทย"),
    end: str = Query(..., description="YYYY-MM-DDTHH:MM เวลาไทย; ชั่วโมงปลายทาง inclusive"),
    step: int = Query(60),
):
    station = validate_station(station)
    if step not in (5, 10, 15, 30, 60):
        raise HTTPException(400, "step ต้องเป็น 5, 10, 15, 30 หรือ 60 นาที")
    start_dt = parse_local_bkk_datetime(start)
    end_dt = parse_local_bkk_datetime(end)
    if end_dt < start_dt:
        raise HTTPException(400, "end ต้องไม่น้อยกว่า start")
    # guard: inclusive ending hour
    total_minutes = ((end_dt.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)) - start_dt).total_seconds() / 60
    estimated = math.ceil(total_minutes / step)
    if estimated > 240:
        raise HTTPException(400, "จำนวนเฟรมมากเกินไปสำหรับ Render Free")

    job_id = create_job("gif-range", {
        "station": station,
        "start": start_dt.isoformat(),
        "end_inclusive_hour": end_dt.isoformat(),
        "step": step,
    })
    t = threading.Thread(
        target=_run_gif_range_job,
        args=(job_id, station, start_dt, end_dt, step),
        daemon=True,
    )
    t.start()
    return {
        "ok": True,
        "job_id": job_id,
        "effective_range": {
            "start": start_dt.isoformat(),
            "end_approximately": (end_dt.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1, seconds=-1)).isoformat(),
        },
    }


@app.get("/api/job/start-gif")
def api_job_start_gif(
    station: str = Query(...),
    hours: int = Query(24, ge=1, le=72),
    step: int = Query(60),
):
    station = validate_station(station)
    if step not in (5, 10, 15, 30, 60):
        raise HTTPException(400, "step ต้องเป็น 5, 10, 15, 30 หรือ 60 นาที")
    estimated = math.ceil((hours + 1) * 60 / step)
    if estimated > 240:
        raise HTTPException(400, "จำนวนเฟรมมากเกินไปสำหรับ Render Free กรุณาเพิ่ม step หรือลดชั่วโมง")
    job_id = create_job("gif", {"station": station, "hours": hours, "step": step})
    t = threading.Thread(target=_run_gif_job, args=(job_id, station, hours, step), daemon=True)
    t.start()
    return {"ok": True, "job_id": job_id}


@app.get("/api/job/start-gif-combined")
def api_job_start_gif_combined(
    hours: int = Query(24, ge=1, le=72),
    step: int = Query(60),
):
    if step not in (5, 10, 15, 30, 60):
        raise HTTPException(400, "step ต้องเป็น 5, 10, 15, 30 หรือ 60 นาที")
    estimated = math.ceil(hours * 60 / step)
    if estimated > 120:
        raise HTTPException(400, "GIF เปรียบเทียบใช้ทรัพยากรมาก กรุณาเลือก step มากขึ้นหรือลดชั่วโมง")
    job_id = create_job("gif-combined", {"hours": hours, "step": step})
    t = threading.Thread(target=_run_combined_gif_job, args=(job_id, hours, step), daemon=True)
    t.start()
    return {"ok": True, "job_id": job_id}


@app.get("/api/job-status")
def api_job_status(job_id: str = Query(...)):
    job = get_job(job_id)
    if not job:
        raise HTTPException(404, "ไม่พบ job นี้")
    return job


@app.get("/api/job-download")
def api_job_download(job_id: str = Query(...)):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            raise HTTPException(404, "ไม่พบ job นี้")
        if job.get("status") != "done" or not job.get("path"):
            raise HTTPException(400, "งานยังไม่เสร็จหรือไม่มีไฟล์ผลลัพธ์")
        path = job["path"]
        filename = job.get("filename") or "result.gif"

    def cleanup():
        remove_file(path)
        with JOBS_LOCK:
            if job_id in JOBS:
                JOBS[job_id]["path"] = None
                JOBS[job_id]["status"] = "downloaded"
                JOBS[job_id]["updated_at"] = now_bkk()

    return FileResponse(
        path,
        media_type="image/gif",
        filename=filename,
        background=BackgroundTask(cleanup),
    )


@app.get("/download/gif")
def download_gif(
    station: str = Query(...),
    hours: int = Query(24, ge=1, le=72),
    step: int = Query(60),
):
    station = validate_station(station)
    if step not in (5, 10, 15, 30, 60):
        raise HTTPException(400, "step ต้องเป็น 5, 10, 15, 30 หรือ 60 นาที")
    estimated = math.ceil((hours + 1) * 60 / step)
    if estimated > 240:
        raise HTTPException(400, "จำนวนเฟรมมากเกินไปสำหรับ Render Free กรุณาเพิ่ม step หรือลดชั่วโมง")
    try:
        path, frames, tasks, cam = build_gif(station, hours, step)
        filename = f"{station.replace('.','')}_{hours}h_step{step}m_{frames}frames.gif"
        return FileResponse(path, media_type="image/gif", filename=filename,
                            background=BackgroundTask(remove_file, path))
    except Exception as e:
        raise HTTPException(502, f"สร้าง GIF ไม่สำเร็จ: {e}")


@app.get("/download/gif-combined")
def download_gif_combined(hours: int = Query(24, ge=1, le=72), step: int = Query(15)):
    if step not in (5, 10, 15, 30, 60):
        raise HTTPException(400, "step ต้องเป็น 5, 10, 15, 30 หรือ 60 นาที")
    estimated = math.ceil(hours * 60 / step)
    if estimated > 120:
        raise HTTPException(400, "GIF เปรียบเทียบใช้ทรัพยากรมาก กรุณาเลือก step มากขึ้นหรือลดชั่วโมง")
    try:
        path, frames, slots, meta = build_combined_gif(hours, step)
        filename = f"P1_P67_compare_{hours}h_step{step}m_{frames}frames.gif"
        return FileResponse(path, media_type="image/gif", filename=filename,
                            background=BackgroundTask(remove_file, path))
    except Exception as e:
        raise HTTPException(502, f"สร้าง GIF เปรียบเทียบไม่สำเร็จ: {e}")
