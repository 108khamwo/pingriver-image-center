import io
import os
import re
import json
import math
import tempfile
import subprocess
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

APP_TITLE = "Ping River Image Center"
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
        "User-Agent": "Mozilla/5.0 (PingRiverImageCenter/2.0)",
        "Accept": "*/*",
        "Referer": "https://appserv.net/pingriver.php",
    })
    return s


HTTP = make_session()


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
    candidates = [
        "/usr/share/fonts/truetype/noto/NotoSansThai-Bold.ttf" if bold else "/usr/share/fonts/truetype/noto/NotoSansThai-Regular.ttf",
        "/usr/share/fonts/opentype/noto/NotoSansThai-Bold.ttf" if bold else "/usr/share/fonts/opentype/noto/NotoSansThai-Regular.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for p in candidates:
        if os.path.exists(p):
            return ImageFont.truetype(p, size=size)
    return ImageFont.load_default()


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


def _extract_cache_jpgs(raw: str, station: str):
    pattern = rf"((?:https?://appserv\.net)?/cache/{re.escape(station)}/[^\s\"'<>]+?\.jpe?g(?:\?[^\s\"'<>]*)?)"
    urls = re.findall(pattern, raw, re.I)

    try:
        soup = BeautifulSoup(raw, "html.parser")
        for tag in soup.find_all(["img", "a", "source"]):
            for attr in ("src", "href"):
                value = tag.get(attr)
                if value and f"/cache/{station}/" in value and re.search(r"\.jpe?g(?:\?|$)", value, re.I):
                    urls.append(value)
    except Exception:
        pass

    clean = []
    for u in urls:
        u = u.replace("\\/", "/").replace("&amp;", "&").strip()
        if u.startswith("/"):
            u = APP_ORIGIN + u
        clean.append(u.split("?")[0])

    clean = list(dict.fromkeys(clean))
    clean.sort(
        key=lambda x: camera_timestamp_from_cache_url(x)
        or datetime(2000, 1, 1, tzinfo=BKK)
    )
    return clean


def latest_cache_jpg(station: str):
    # camlist รุ่นปัจจุบันเน้นคืนรายชื่อ MP4 จึงอาจไม่มี JPG ล่าสุด
    raw = fetch_text(
        f"{APP_BASE}?op=camlist&station={station}&_={int(now_bkk().timestamp()*1000)}"
    )
    clean = _extract_cache_jpgs(raw, station)

    # fallback เหมือน v1: อ่านหน้า station เพื่อหา <img src="/cache/P.x/...jpg">
    if not clean:
        page = fetch_text(
            f"{APP_BASE}?station={station}&_={int(now_bkk().timestamp()*1000)}"
        )
        clean = _extract_cache_jpgs(page, station)

    if not clean:
        raise RuntimeError(f"ไม่พบภาพล่าสุดของ {station}")
    return clean[-1], clean

def parse_camlist_response(raw: str):
    obj = None
    try:
        obj = json.loads(raw)
    except Exception:
        pass

    files = []
    exp = None
    sig = None
    if isinstance(obj, dict):
        if isinstance(obj.get("files"), list):
            files = [str(x) for x in obj.get("files", [])]
        exp = obj.get("exp")
        sig = obj.get("sig")

    if not files:
        files = re.findall(r'hourly/\d{4}-\d{2}-\d{2}_\d{2}\.mp4', raw)

    if exp is None:
        m = re.search(r'"exp"\s*:\s*"?(?P<v>\d+)"?', raw)
        if m:
            exp = m.group("v")
    if sig is None:
        m = re.search(r'"sig"\s*:\s*"(?P<v>[a-fA-F0-9]+)"', raw)
        if m:
            sig = m.group("v")

    files = list(dict.fromkeys(files))
    return {
        "files": files,
        "exp": exp,
        "sig": sig,
        "raw_preview": raw[:2000]
    }


def fetch_camlist(station: str):
    raw = fetch_text(f"{APP_BASE}?op=camlist&station={station}&_={int(now_bkk().timestamp()*1000)}")
    parsed = parse_camlist_response(raw)
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


def ffmpeg_extract_frame(urls, offset_seconds: int):
    last_err = None
    for url in urls:
        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel", "error",
            "-y",
            "-ss", str(int(offset_seconds)),
            "-i", url,
            "-frames:v", "1",
            "-f", "image2pipe",
            "-vcodec", "mjpeg",
            "pipe:1",
        ]
        try:
            res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
            if res.returncode == 0 and res.stdout:
                return res.stdout
            last_err = res.stderr.decode("utf-8", "ignore")[:500]
        except Exception as e:
            last_err = str(e)
    raise RuntimeError(last_err or "ffmpeg ดึงเฟรมไม่สำเร็จ")


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


def render_report_frame(cctv_bytes: bytes, station: str, captured_at: datetime, water_level, size: int):
    canvas = Image.new("RGB", (size, size), (12, 26, 44))
    draw = ImageDraw.Draw(canvas)

    title_font = get_font(max(28, int(size * 0.046)), True)
    label_font = get_font(max(22, int(size * 0.034)), True)
    value_font = get_font(max(28, int(size * 0.050)), True)
    small_font = get_font(max(17, int(size * 0.026)), False)

    margin = int(size * 0.035)
    header_h = int(size * 0.12)
    footer_h = int(size * 0.23)

    draw.rounded_rectangle((margin, margin, size - margin, margin + header_h),
                           radius=int(size * 0.02), fill=(25, 59, 101))
    draw.text((int(margin * 1.5), margin + int(header_h * 0.17)),
              f"{station}  {STATIONS[station]}", font=title_font, fill="white")

    img_top = margin + header_h + int(size * 0.02)
    img_bottom = size - margin - footer_h - int(size * 0.02)
    img_left = margin
    img_right = size - margin

    try:
        im = Image.open(io.BytesIO(cctv_bytes)).convert("RGB")
        im = ImageOps.fit(im, (img_right - img_left, img_bottom - img_top),
                          method=Image.Resampling.LANCZOS, centering=(0.5, 0.5))
        canvas.paste(im, (img_left, img_top))
    except Exception:
        draw.rectangle((img_left, img_top, img_right, img_bottom), fill=(30, 38, 49))
        draw.text((img_left + 20, img_top + 20), "โหลดภาพ CCTV ไม่สำเร็จ", font=label_font, fill="white")

    y1 = size - margin - footer_h
    y2 = size - margin
    draw.rounded_rectangle((margin, y1, size - margin, y2),
                           radius=int(size * 0.02), fill=(20, 43, 72))

    level_text = "-" if water_level is None else f"{water_level:.2f} เมตร"
    draw.text((int(margin * 1.5), y1 + int(footer_h * 0.14)), "ระดับน้ำ", font=label_font, fill=(184, 211, 245))
    draw.text((int(margin * 1.5), y1 + int(footer_h * 0.39)), level_text, font=value_font, fill="white")

    time_text = captured_at.astimezone(BKK).strftime("%d/%m/%Y  %H:%M")
    x_time = int(size * 0.53)
    draw.text((x_time, y1 + int(footer_h * 0.14)), "เวลา CCTV", font=label_font, fill=(184, 211, 245))
    draw.text((x_time, y1 + int(footer_h * 0.43)), time_text, font=small_font, fill="white")
    draw.text((x_time, y1 + int(footer_h * 0.68)),
              "ข้อมูล: AppServ / ระบบโทรมาตร กรมชลประทาน",
              font=small_font, fill=(210, 220, 230))
    return canvas


def latest_png(station: str):
    url, _ = latest_cache_jpg(station)
    dt = camera_timestamp_from_cache_url(url) or now_bkk()
    history, _ = fetch_water_history(station)
    level = nearest_water_level(history, dt)
    b = fetch_bytes(url + f"?t={int(now_bkk().timestamp())}")
    img = render_report_frame(b, station, dt, level, PNG_SIZE)
    path = temp_path(".png")
    img.save(path, "PNG", optimize=True)
    return path


def build_slot_tasks(station: str, hours: int, step: int):
    cam = fetch_camlist(station)
    exp = cam.get("exp")
    sig = cam.get("sig")
    if not (exp and sig):
        discovered_exp, discovered_sig = try_discover_sig_from_page(station)
        exp = exp or discovered_exp
        sig = sig or discovered_sig

    file_map = {}
    for rel in cam["files"]:
        dt = parse_hourly_filename(rel)
        if dt:
            file_map[dt] = rel

    n = now_bkk()
    start = n - timedelta(hours=hours)
    start = start.replace(minute=0, second=0, microsecond=0)
    tasks = []
    cursor = start
    while cursor <= n:
        hour_dt = cursor.replace(minute=0, second=0, microsecond=0)
        rel = file_map.get(hour_dt)
        if rel:
            offset = int((cursor - hour_dt).total_seconds())
            tasks.append({
                "slot": cursor,
                "rel_file": rel,
                "offset": offset,
                "urls": playback_url_candidates(station, rel, exp=exp, sig=sig),
            })
        cursor += timedelta(minutes=step)

    uniq = []
    seen = set()
    for t in tasks:
        key = (t["slot"], t["rel_file"], t["offset"])
        if key not in seen:
            seen.add(key)
            uniq.append(t)
    return uniq, cam


def extract_task_frame(task):
    b = ffmpeg_extract_frame(task["urls"], task["offset"])
    return task["slot"], b


def build_gif(station: str, hours: int, step: int):
    tasks, cam = build_slot_tasks(station, hours, step)
    if len(tasks) < 2:
        raise RuntimeError("จำนวนช่วงเวลาที่สร้างได้ไม่พอสำหรับทำ GIF")

    history, _ = fetch_water_history(station)
    results = []
    with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, max(2, len(tasks)))) as ex:
        futs = [ex.submit(extract_task_frame, t) for t in tasks]
        for f in as_completed(futs):
            results.append(f.result())

    results.sort(key=lambda x: x[0])

    frames = []
    for slot, b in results:
        level = nearest_water_level(history, slot)
        fr = render_report_frame(b, station, slot, level, GIF_SIZE)
        fr = fr.quantize(colors=128, method=Image.Quantize.MEDIANCUT)
        frames.append(fr)

    if len(frames) < 2:
        raise RuntimeError("ดึงเฟรมจากวิดีโอได้ไม่พอสำหรับทำ GIF")

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
    return path, len(frames), len(tasks), cam


def build_combined_gif(hours: int, step: int):
    p1_tasks, p1_cam = build_slot_tasks("P.1", hours, step)
    p67_tasks, p67_cam = build_slot_tasks("P.67", hours, step)
    map1 = {t["slot"]: t for t in p1_tasks}
    map67 = {t["slot"]: t for t in p67_tasks}
    common_slots = sorted(set(map1.keys()) & set(map67.keys()))
    if len(common_slots) < 2:
        raise RuntimeError("ช่วงเวลาที่ P.1 และ P.67 ซ้อนกันมีไม่พอสำหรับ GIF เปรียบเทียบ")

    h1, _ = fetch_water_history("P.1")
    h67, _ = fetch_water_history("P.67")

    tasks = []
    for slot in common_slots:
        tasks.append(("P.1", slot, map1[slot]))
        tasks.append(("P.67", slot, map67[slot]))

    extracted = {}
    with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, max(2, len(tasks)))) as ex:
        fut_map = {ex.submit(extract_task_frame, t): (station, slot) for station, slot, t in tasks}
        for fut in as_completed(fut_map):
            station, slot = fut_map[fut]
            slot2, b = fut.result()
            extracted[(station, slot2)] = b

    frames = []
    for slot in common_slots:
        if ("P.1", slot) not in extracted or ("P.67", slot) not in extracted:
            continue
        left = render_report_frame(extracted[("P.1", slot)], "P.1", slot, nearest_water_level(h1, slot), GIF_SIZE)
        right = render_report_frame(extracted[("P.67", slot)], "P.67", slot, nearest_water_level(h67, slot), GIF_SIZE)
        canvas = Image.new("RGB", (GIF_SIZE * 2, GIF_SIZE), (10, 20, 34))
        canvas.paste(left, (0, 0))
        canvas.paste(right, (GIF_SIZE, 0))
        frames.append(canvas.quantize(colors=128, method=Image.Quantize.MEDIANCUT))

    if len(frames) < 2:
        raise RuntimeError("สร้าง GIF เปรียบเทียบไม่สำเร็จ")

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
    return path, len(frames), len(common_slots), {"P.1": p1_cam, "P.67": p67_cam}


HTML = """
<!doctype html>
<html lang=\"th\">
<head>
<meta charset=\"utf-8\">
<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">
<title>Ping River Image Center v2</title>
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
</style>
</head>
<body>
<div class=\"wrap\">
  <h1>🌊 Ping River Image Center v2</h1>
  <div class=\"sub\">เวอร์ชันนี้ใช้ CCTV Playback แบบวิดีโอรายชั่วโมงเพื่อสร้าง GIF จากภาพจริงย้อนหลัง</div>

  <div class=\"grid\">
    <section class=\"card\" data-station=\"P.1\">
      <h2>P.1 สะพานนวรัฐ</h2>
      <div class=\"preview\"><img src=\"/camera/latest?station=P.1&t=1\" alt=\"P.1 CCTV\"></div>
      <div class=\"big\" id=\"level-P1\">กำลังอ่านระดับน้ำ…</div>
      <div class=\"status\" id=\"status-P1\"></div>
      <div class=\"row\">
        <a class=\"btn\" href=\"/download/png?station=P.1\">สร้าง PNG ล่าสุด</a>
        <button class=\"alt\" onclick=\"refreshCamera('P.1')\">รีเฟรช CCTV</button>
      </div>
    </section>

    <section class=\"card\" data-station=\"P.67\">
      <h2>P.67 บ้านแม่แต</h2>
      <div class=\"preview\"><img src=\"/camera/latest?station=P.67&t=1\" alt=\"P.67 CCTV\"></div>
      <div class=\"big\" id=\"level-P67\">กำลังอ่านระดับน้ำ…</div>
      <div class=\"status\" id=\"status-P67\"></div>
      <div class=\"row\">
        <a class=\"btn\" href=\"/download/png?station=P.67\">สร้าง PNG ล่าสุด</a>
        <button class=\"alt\" onclick=\"refreshCamera('P.67')\">รีเฟรช CCTV</button>
      </div>
    </section>
  </div>

  <section class=\"card tools\">
    <h2>สร้าง GIF ย้อนหลังจาก Playback</h2>
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
          <option value=\"15\" selected>15 นาที</option>
          <option value=\"30\">30 นาที</option>
        </select>
      </label>
    </div>
    <div class=\"row\">
      <button onclick=\"makeGif('P.1')\">GIF P.1</button>
      <button onclick=\"makeGif('P.67')\">GIF P.67</button>
      <button class=\"alt\" onclick=\"makeCombined()\">GIF เปรียบเทียบ P.1 + P.67</button>
      <button class=\"alt\" onclick=\"checkHistory()\">ตรวจไฟล์ Playback</button>
    </div>
    <div class=\"status\" id=\"workStatus\"></div>
  </section>

  <div class=\"note\">
    ถ้า PNG ใช้ได้แต่ GIF มี error ให้เปิด <code>/api/debug/camlist?station=P.67</code> แล้วส่งผลกลับมา
  </div>
</div>
<script>
const id = s => s.replace('.','');
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
  img.src='/camera/latest?station='+encodeURIComponent(station)+'&t='+Date.now();
  loadStatus(station);
}
function makeGif(station){
  const h=document.getElementById('hours').value;
  const s=document.getElementById('step').value;
  document.getElementById('workStatus').textContent='กำลังสร้าง GIF จาก Playback... อาจใช้เวลาสักครู่';
  location.href=`/download/gif?station=${encodeURIComponent(station)}&hours=${h}&step=${s}`;
}
function makeCombined(){
  const h=document.getElementById('hours').value;
  const s=document.getElementById('step').value;
  document.getElementById('workStatus').textContent='กำลังสร้าง GIF เปรียบเทียบ...';
  location.href=`/download/gif-combined?hours=${h}&step=${s}`;
}
async function checkHistory(){
  const h=document.getElementById('hours').value;
  const el=document.getElementById('workStatus');
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
        cutoff = now_bkk() - timedelta(hours=hours)
        dts = [parse_hourly_filename(x) for x in cam["files"]]
        dts = [x for x in dts if x]
        in_period = [x for x in dts if x >= cutoff.replace(minute=0, second=0, microsecond=0)]
        return {
            "station": station,
            "hours": hours,
            "total_files": len(dts),
            "in_period": len(in_period),
            "first": dts[0].isoformat() if dts else None,
            "last": dts[-1].isoformat() if dts else None,
            "sig_present": bool(cam.get("sig")),
            "exp": cam.get("exp"),
        }
    except Exception as e:
        raise HTTPException(502, f"ตรวจ playback ไม่สำเร็จ: {e}")


@app.get("/api/debug/camlist")
def debug_camlist(station: str = Query(...)):
    station = validate_station(station)
    try:
        cam = fetch_camlist(station)
        exp2, sig2 = try_discover_sig_from_page(station)
        return {
            "station": station,
            "count": len(cam["files"]),
            "sample_files": cam["files"][:10],
            "exp_from_camlist": cam.get("exp"),
            "sig_from_camlist": bool(cam.get("sig")),
            "exp_from_page": exp2,
            "sig_from_page": bool(sig2),
            "raw_preview": cam["raw_preview"],
        }
    except Exception as e:
        raise HTTPException(502, str(e))


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


@app.get("/download/png")
def download_png(station: str = Query(...)):
    station = validate_station(station)
    try:
        path = latest_png(station)
        filename = f"{station.replace('.','')}_latest_{now_bkk():%Y%m%d_%H%M}.png"
        return FileResponse(path, media_type="image/png", filename=filename,
                            background=BackgroundTask(remove_file, path))
    except Exception as e:
        raise HTTPException(502, f"สร้าง PNG ไม่สำเร็จ: {e}")


@app.get("/download/gif")
def download_gif(
    station: str = Query(...),
    hours: int = Query(24, ge=1, le=72),
    step: int = Query(15),
):
    station = validate_station(station)
    if step not in (5, 10, 15, 30):
        raise HTTPException(400, "step ต้องเป็น 5, 10, 15 หรือ 30 นาที")
    estimated = math.ceil(hours * 60 / step)
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
    if step not in (5, 10, 15, 30):
        raise HTTPException(400, "step ต้องเป็น 5, 10, 15 หรือ 30 นาที")
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
