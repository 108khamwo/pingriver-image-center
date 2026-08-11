import io
import os
import re
import json
import math
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin
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
BASE_URL = "https://appserv.net/pingriver.php"
APP_ORIGIN = "https://appserv.net"
STATIONS = {
    "P.1": "สะพานนวรัฐ",
    "P.67": "บ้านแม่แต"
}
BKK = timezone(timedelta(hours=7))
HTTP_TIMEOUT = 25
MAX_WORKERS = 8
TMP_DIR = Path(tempfile.gettempdir()) / "pingriver_image_center"
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
        allowed_methods=["GET"],
    )
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (PingRiverImageCenter/1.0)",
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


def camera_timestamp(value: str):
    m = re.search(r"cctv_(\d{14})_", value, re.I)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y%m%d%H%M%S").replace(tzinfo=BKK)
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


def extract_camera_urls(raw: str, station: str):
    strings = [raw]
    try:
        strings += _collect_strings(json.loads(raw))
    except Exception:
        pass

    soup = BeautifulSoup(raw, "html.parser")
    for tag in soup.find_all(["img", "a", "source"]):
        for attr in ("src", "href"):
            v = tag.get(attr)
            if v:
                strings.append(v)

    blob = "\n".join(strings)
    found = []

    pat = re.compile(
        rf'((?:https?://appserv\.net)?/cache/{re.escape(station)}/'
        rf'[^"\'<>\s\\]+?\.jpe?g(?:\?[^"\'<>\s\\]*)?)',
        re.I
    )
    found.extend(pat.findall(blob))

    filenames = re.findall(r'(cctv_\d{14}_[A-Za-z0-9]+\.jpe?g)', blob, re.I)
    for fn in filenames:
        dt = camera_timestamp(fn)
        if dt:
            found.append(f"{APP_ORIGIN}/cache/{station}/{dt:%Y}/{dt:%m}/{fn}")

    clean = []
    seen = set()
    for u in found:
        u = u.replace("\\/", "/").replace("&amp;", "&").strip()
        if u.startswith("/"):
            u = APP_ORIGIN + u
        elif not u.startswith("http"):
            u = urljoin(APP_ORIGIN + "/", u)
        base = u.split("?")[0]
        if base not in seen:
            seen.add(base)
            clean.append(base)

    clean.sort(key=lambda x: camera_timestamp(x) or datetime(2000, 1, 1, tzinfo=BKK))
    return clean


def fetch_camlist(station: str):
    url = f"{BASE_URL}?op=camlist&station={station}&_={int(now_bkk().timestamp()*1000)}"
    raw = fetch_text(url)
    urls = extract_camera_urls(raw, station)

    if not urls:
        main_raw = fetch_text(f"{BASE_URL}?station={station}&_={int(now_bkk().timestamp()*1000)}")
        urls = extract_camera_urls(main_raw, station)

    return urls, raw


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
                r'(\d{1,2}:\d{2}).*?([+-]?\d+(?:\.\d+)?)\s*m.*?'
                r'([+-]?\d+(?:\.\d+)?)\s*m',
                line, re.I
            )
            if m:
                dt = _time_to_datetime(m.group(1))
                val = float(m.group(2) if station == "P.1" else m.group(3))
                rows.append((dt, val))

    d = {}
    for dt, val in rows:
        d[dt.replace(second=0, microsecond=0)] = val
    return sorted(d.items(), key=lambda x: x[0])


def fetch_water_history(station: str):
    url = f"{BASE_URL}?station={station}&_={int(now_bkk().timestamp()*1000)}"
    html = fetch_text(url)
    return parse_water_history(html, station), html


def nearest_water_level(history, dt):
    if not history:
        return None
    nearest = min(history, key=lambda x: abs((x[0] - dt).total_seconds()))
    if abs((nearest[0] - dt).total_seconds()) > 2 * 3600:
        return None
    return nearest[1]


def latest_camera(station: str):
    urls, raw = fetch_camlist(station)
    if not urls:
        raise RuntimeError(f"ไม่พบภาพ CCTV ของ {station} จาก camlist")
    return urls[-1], urls


def choose_cameras_for_period(urls, hours: int, step: int):
    n = now_bkk()
    cutoff = n - timedelta(hours=hours)
    with_dt = [(camera_timestamp(u), u) for u in urls]
    with_dt = [(dt, u) for dt, u in with_dt if dt and cutoff <= dt <= n + timedelta(minutes=10)]
    if not with_dt:
        return []

    with_dt.sort()
    chosen = []
    cursor = cutoff
    tolerance = timedelta(minutes=max(step, 7))
    while cursor <= n:
        dt, u = min(with_dt, key=lambda x: abs(x[0] - cursor))
        if abs(dt - cursor) <= tolerance:
            if not chosen or chosen[-1][1] != u:
                chosen.append((dt, u))
        cursor += timedelta(minutes=step)

    if not chosen:
        chosen = with_dt
    return chosen


def font_path(bold=False):
    candidates = [
        "/usr/share/fonts/truetype/noto/NotoSansThai-Bold.ttf" if bold else "/usr/share/fonts/truetype/noto/NotoSansThai-Regular.ttf",
        "/usr/share/fonts/opentype/noto/NotoSansThai-Bold.ttf" if bold else "/usr/share/fonts/opentype/noto/NotoSansThai-Regular.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for p in candidates:
        if os.path.exists(p):
            return p
    return None


def get_font(size, bold=False):
    p = font_path(bold)
    if p:
        return ImageFont.truetype(p, size=size)
    return ImageFont.load_default()


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

    draw.rounded_rectangle(
        (margin, margin, size - margin, margin + header_h),
        radius=int(size * 0.02), fill=(25, 59, 101)
    )
    draw.text(
        (int(margin * 1.5), margin + int(header_h * 0.17)),
        f"{station}  {STATIONS[station]}",
        font=title_font, fill="white"
    )

    img_top = margin + header_h + int(size * 0.02)
    img_bottom = size - margin - footer_h - int(size * 0.02)
    img_left = margin
    img_right = size - margin

    try:
        im = Image.open(io.BytesIO(cctv_bytes)).convert("RGB")
        im = ImageOps.fit(
            im,
            (img_right - img_left, img_bottom - img_top),
            method=Image.Resampling.LANCZOS,
            centering=(0.5, 0.5),
        )
        canvas.paste(im, (img_left, img_top))
    except Exception:
        draw.rectangle((img_left, img_top, img_right, img_bottom), fill=(30, 38, 49))
        draw.text((img_left + 20, img_top + 20), "โหลดภาพ CCTV ไม่สำเร็จ", font=label_font, fill="white")

    y1 = size - margin - footer_h
    y2 = size - margin
    draw.rounded_rectangle(
        (margin, y1, size - margin, y2),
        radius=int(size * 0.02), fill=(20, 43, 72)
    )

    level_text = "-" if water_level is None else f"{water_level:.2f} เมตร"
    draw.text((int(margin * 1.5), y1 + int(footer_h * 0.14)), "ระดับน้ำ", font=label_font, fill=(184, 211, 245))
    draw.text((int(margin * 1.5), y1 + int(footer_h * 0.39)), level_text, font=value_font, fill="white")

    time_text = captured_at.astimezone(BKK).strftime("%d/%m/%Y  %H:%M")
    x_time = int(size * 0.53)
    draw.text((x_time, y1 + int(footer_h * 0.14)), "เวลา CCTV", font=label_font, fill=(184, 211, 245))
    draw.text((x_time, y1 + int(footer_h * 0.43)), time_text, font=small_font, fill="white")
    draw.text(
        (x_time, y1 + int(footer_h * 0.68)),
        "ข้อมูล: AppServ / ระบบโทรมาตร กรมชลประทาน",
        font=small_font, fill=(210, 220, 230)
    )
    return canvas


def download_many(items):
    result = [None] * len(items)
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = {
            ex.submit(fetch_bytes, u + ("&" if "?" in u else "?") + f"t={int(now_bkk().timestamp())}"): i
            for i, (_, u) in enumerate(items)
        }
        for f in as_completed(futures):
            i = futures[f]
            try:
                b = f.result()
                dt, u = items[i]
                result[i] = (dt, u, b)
            except Exception:
                result[i] = None
    return [x for x in result if x is not None]


def temp_path(suffix: str):
    f = tempfile.NamedTemporaryFile(delete=False, suffix=suffix, dir=TMP_DIR)
    p = f.name
    f.close()
    return p


def remove_file(path: str):
    try:
        os.unlink(path)
    except OSError:
        pass


def build_png(station: str):
    url, _ = latest_camera(station)
    dt = camera_timestamp(url) or now_bkk()
    history, _ = fetch_water_history(station)
    level = nearest_water_level(history, dt)
    cctv = fetch_bytes(url + f"?t={int(now_bkk().timestamp())}")
    im = render_report_frame(cctv, station, dt, level, PNG_SIZE)
    path = temp_path(".png")
    im.save(path, "PNG", optimize=True)
    return path


def build_gif(station: str, hours: int, step: int):
    urls, _ = fetch_camlist(station)
    items = choose_cameras_for_period(urls, hours, step)
    if len(items) < 2:
        raise RuntimeError(
            f"พบภาพย้อนหลังของ {station} เพียง {len(items)} ภาพ "
            "camlist อาจส่งรายการไม่ครบหรือรูปแบบ endpoint มีการเปลี่ยนแปลง"
        )

    history, _ = fetch_water_history(station)
    downloaded = download_many(items)
    if len(downloaded) < 2:
        raise RuntimeError("ดาวน์โหลดภาพ CCTV ได้ไม่เพียงพอสำหรับสร้าง GIF")

    frames = []
    for dt, url, b in downloaded:
        level = nearest_water_level(history, dt)
        fr = render_report_frame(b, station, dt, level, GIF_SIZE)
        fr = fr.quantize(colors=128, method=Image.Quantize.MEDIANCUT)
        frames.append(fr)

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
    return path, len(frames)


def nearest_camera_to_slot(items, slot, tolerance_minutes):
    if not items:
        return None
    best = min(items, key=lambda x: abs(x[0] - slot))
    if abs(best[0] - slot) > timedelta(minutes=tolerance_minutes):
        return None
    return best


def render_combined_frame(left_b, right_b, dt, p1_level, p67_level, size=640):
    left = render_report_frame(left_b, "P.1", dt, p1_level, size)
    right = render_report_frame(right_b, "P.67", dt, p67_level, size)
    canvas = Image.new("RGB", (size * 2, size), (10, 20, 34))
    canvas.paste(left, (0, 0))
    canvas.paste(right, (size, 0))
    return canvas.quantize(colors=128, method=Image.Quantize.MEDIANCUT)


def build_combined_gif(hours: int, step: int):
    p1_urls, _ = fetch_camlist("P.1")
    p67_urls, _ = fetch_camlist("P.67")
    p1_items = choose_cameras_for_period(p1_urls, hours, step)
    p67_items = choose_cameras_for_period(p67_urls, hours, step)
    if len(p1_items) < 2 or len(p67_items) < 2:
        raise RuntimeError("ภาพย้อนหลังของ P.1 หรือ P.67 ไม่เพียงพอสำหรับ GIF เปรียบเทียบ")

    h1, _ = fetch_water_history("P.1")
    h67, _ = fetch_water_history("P.67")
    n = now_bkk()
    start = n - timedelta(hours=hours)
    slots = []
    cursor = start
    while cursor <= n:
        slots.append(cursor)
        cursor += timedelta(minutes=step)

    pairs = []
    for slot in slots:
        a = nearest_camera_to_slot(p1_items, slot, max(step, 10))
        b = nearest_camera_to_slot(p67_items, slot, max(step, 10))
        if a and b:
            pairs.append((slot, a, b))

    if len(pairs) < 2:
        raise RuntimeError("ไม่สามารถจับคู่เวลา P.1 กับ P.67 ได้เพียงพอ")

    unique_items = []
    seen = set()
    for _, a, b in pairs:
        for item in (a, b):
            if item[1] not in seen:
                seen.add(item[1])
                unique_items.append(item)

    downloaded = download_many(unique_items)
    bytes_map = {u: bb for dt, u, bb in downloaded}

    frames = []
    for slot, a, b in pairs:
        if a[1] not in bytes_map or b[1] not in bytes_map:
            continue
        level1 = nearest_water_level(h1, slot)
        level67 = nearest_water_level(h67, slot)
        frames.append(render_combined_frame(bytes_map[a[1]], bytes_map[b[1]], slot, level1, level67))

    if len(frames) < 2:
        raise RuntimeError("ดาวน์โหลดภาพสำหรับ GIF เปรียบเทียบไม่เพียงพอ")

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
    return path, len(frames)


HTML = r'''
<!doctype html>
<html lang="th">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Ping River Image Center</title>
<style>
:root{color-scheme:dark}
*{box-sizing:border-box}
body{margin:0;font-family:system-ui,-apple-system,"Noto Sans Thai",sans-serif;background:#07111f;color:#eef5ff}
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
<div class="wrap">
  <h1>🌊 Ping River Image Center</h1>
  <div class="sub">สร้างภาพ PNG และ GIF CCTV + ระดับน้ำ P.1 / P.67 แบบดึงข้อมูลเมื่อสั่งสร้าง</div>

  <div class="grid">
    <section class="card" data-station="P.1">
      <h2>P.1 สะพานนวรัฐ</h2>
      <div class="preview"><img src="/camera/latest?station=P.1&t=1" alt="P.1 CCTV"></div>
      <div class="big" id="level-P1">กำลังอ่านระดับน้ำ…</div>
      <div class="status" id="status-P1"></div>
      <div class="row">
        <a class="btn" href="/download/png?station=P.1">สร้าง PNG ล่าสุด</a>
        <button class="alt" onclick="refreshCamera('P.1')">รีเฟรช CCTV</button>
      </div>
    </section>

    <section class="card" data-station="P.67">
      <h2>P.67 บ้านแม่แต</h2>
      <div class="preview"><img src="/camera/latest?station=P.67&t=1" alt="P.67 CCTV"></div>
      <div class="big" id="level-P67">กำลังอ่านระดับน้ำ…</div>
      <div class="status" id="status-P67"></div>
      <div class="row">
        <a class="btn" href="/download/png?station=P.67">สร้าง PNG ล่าสุด</a>
        <button class="alt" onclick="refreshCamera('P.67')">รีเฟรช CCTV</button>
      </div>
    </section>
  </div>

  <section class="card tools">
    <h2>สร้าง GIF ย้อนหลัง</h2>
    <div class="row">
      <label>ย้อนหลัง
        <select id="hours">
          <option value="1">1 ชั่วโมง</option>
          <option value="3">3 ชั่วโมง</option>
          <option value="6">6 ชั่วโมง</option>
          <option value="12">12 ชั่วโมง</option>
          <option value="24" selected>24 ชั่วโมง</option>
          <option value="48">48 ชั่วโมง</option>
          <option value="72">72 ชั่วโมง</option>
        </select>
      </label>
      <label>เลือกภาพทุก
        <select id="step">
          <option value="5">5 นาที</option>
          <option value="10">10 นาที</option>
          <option value="15" selected>15 นาที</option>
          <option value="30">30 นาที</option>
        </select>
      </label>
    </div>
    <div class="row">
      <button onclick="makeGif('P.1')">GIF P.1</button>
      <button onclick="makeGif('P.67')">GIF P.67</button>
      <button class="alt" onclick="makeCombined()">GIF เปรียบเทียบ P.1 + P.67</button>
      <button class="alt" onclick="checkHistory()">ตรวจจำนวนภาพย้อนหลัง</button>
    </div>
    <div class="status" id="workStatus"></div>
  </section>

  <div class="note">
    ระบบนี้ไม่เก็บ CCTV ทุก 5 นาทีบน Render — ตอนกดสร้าง ระบบจะขอรายการภาพย้อนหลังจาก AppServ แล้วสร้างไฟล์ชั่วคราวให้ดาวน์โหลดทันที
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
    el.textContent=`CCTV ล่าสุด ${j.camera_time||'-'} | พบภาพใน camlist ${j.camera_count} ภาพ`;
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
  document.getElementById('workStatus').textContent='กำลังสร้าง GIF... อาจใช้เวลาสักครู่';
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
    el.textContent=`P.1: ${a.in_period}/${a.total_camlist} ภาพ | P.67: ${b.in_period}/${b.total_camlist} ภาพ`;
  }catch(e){el.textContent='ตรวจไม่สำเร็จ: '+e.message}
}
loadStatus('P.1'); loadStatus('P.67');
</script>
</body>
</html>
'''


@app.get("/", response_class=HTMLResponse)
def home():
    return HTMLResponse(HTML)


@app.get("/health")
def health():
    return {"ok": True, "time": now_bkk().isoformat()}


@app.get("/api/status")
def api_status(station: str = Query(...)):
    station = validate_station(station)
    try:
        cam_url, urls = latest_camera(station)
        cam_dt = camera_timestamp(cam_url)
        history, _ = fetch_water_history(station)
        level = nearest_water_level(history, cam_dt or now_bkk())
        return {
            "station": station,
            "name": STATIONS[station],
            "water_level": level,
            "camera_time": cam_dt.strftime("%d/%m/%Y %H:%M:%S") if cam_dt else None,
            "camera_count": len(urls),
            "latest_camera_url": cam_url,
            "water_points": len(history),
        }
    except Exception as e:
        raise HTTPException(502, f"ดึงข้อมูลต้นทางไม่สำเร็จ: {e}")


@app.get("/api/history-check")
def history_check(station: str = Query(...), hours: int = Query(24, ge=1, le=72)):
    station = validate_station(station)
    try:
        urls, _ = fetch_camlist(station)
        cutoff = now_bkk() - timedelta(hours=hours)
        in_period = [u for u in urls if camera_timestamp(u) and camera_timestamp(u) >= cutoff]
        return {
            "station": station,
            "hours": hours,
            "total_camlist": len(urls),
            "in_period": len(in_period),
            "first": camera_timestamp(urls[0]).isoformat() if urls and camera_timestamp(urls[0]) else None,
            "last": camera_timestamp(urls[-1]).isoformat() if urls and camera_timestamp(urls[-1]) else None,
        }
    except Exception as e:
        raise HTTPException(502, f"ตรวจ camlist ไม่สำเร็จ: {e}")


@app.get("/api/debug/camlist")
def debug_camlist(station: str = Query(...)):
    station = validate_station(station)
    try:
        urls, raw = fetch_camlist(station)
        return {
            "station": station,
            "count": len(urls),
            "urls": urls[-20:],
            "raw_preview": raw[:1500],
        }
    except Exception as e:
        raise HTTPException(502, str(e))


@app.get("/camera/latest")
def camera_latest(station: str = Query(...)):
    station = validate_station(station)
    try:
        url, _ = latest_camera(station)
        b = fetch_bytes(url + f"?t={int(now_bkk().timestamp())}")
        return StreamingResponse(io.BytesIO(b), media_type="image/jpeg",
                                 headers={"Cache-Control": "no-store"})
    except Exception as e:
        raise HTTPException(502, f"โหลด CCTV ไม่สำเร็จ: {e}")


@app.get("/download/png")
def download_png(station: str = Query(...)):
    station = validate_station(station)
    try:
        path = build_png(station)
        filename = f"{station.replace('.','')}_latest_{now_bkk():%Y%m%d_%H%M}.png"
        return FileResponse(
            path, media_type="image/png", filename=filename,
            background=BackgroundTask(remove_file, path)
        )
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
    if estimated > 320:
        raise HTTPException(400, "จำนวนเฟรมมากเกินไป กรุณาเพิ่ม step หรือลดจำนวนชั่วโมง")
    try:
        path, frames = build_gif(station, hours, step)
        filename = f"{station.replace('.','')}_{hours}h_step{step}m_{frames}frames.gif"
        return FileResponse(
            path, media_type="image/gif", filename=filename,
            background=BackgroundTask(remove_file, path)
        )
    except Exception as e:
        raise HTTPException(502, f"สร้าง GIF ไม่สำเร็จ: {e}")


@app.get("/download/gif-combined")
def download_gif_combined(
    hours: int = Query(24, ge=1, le=72),
    step: int = Query(15),
):
    if step not in (5, 10, 15, 30):
        raise HTTPException(400, "step ต้องเป็น 5, 10, 15 หรือ 30 นาที")
    estimated = math.ceil(hours * 60 / step)
    if estimated > 180:
        raise HTTPException(
            400,
            "GIF เปรียบเทียบมี 2 ภาพต่อเฟรม กรุณาใช้ไม่เกินประมาณ 180 เฟรม "
            "(เช่น 24 ชม. เลือกทุก 10/15/30 นาที)"
        )
    try:
        path, frames = build_combined_gif(hours, step)
        filename = f"P1_P67_compare_{hours}h_step{step}m_{frames}frames.gif"
        return FileResponse(
            path, media_type="image/gif", filename=filename,
            background=BackgroundTask(remove_file, path)
        )
    except Exception as e:
        raise HTTPException(502, f"สร้าง GIF เปรียบเทียบไม่สำเร็จ: {e}")
