# Ping River Image Center v14

เวอร์ชันนี้อัปเดตให้ใช้ CCTV Playback แบบวิดีโอรายชั่วโมง (`hourly/*.mp4`) เพื่อสร้าง GIF จากภาพจริงย้อนหลัง

## ใหม่ในเวอร์ชันนี้
- ใช้ `op=camlist` เพื่ออ่านรายการไฟล์วิดีโอ
- ใช้ `ffmpeg` เพื่อสกัดภาพจริงจาก MP4
- รองรับ GIF ย้อนหลังจาก Playback ของ P.1 และ P.67
- รองรับ GIF เปรียบเทียบ 2 จุด
- Dockerfile ติดตั้ง `ffmpeg` และฟอนต์ไทยให้แล้ว

## Deploy
อัปโหลดไฟล์ทั้งหมดขึ้น GitHub แล้วเชื่อมกับ Render เป็น Web Service แบบ Docker
- Language: Docker
- Instance Type: Free
- Health Check Path: `/health`

## หลัง Deploy
ลองเปิด:
- `/health`
- `/api/history-check?station=P.67&hours=24`
- `/api/debug/camlist?station=P.67`

จากนั้นเข้าหน้าหลัก `/` แล้วลองสร้าง GIF 24 ชั่วโมง ทุก 15 นาที

## หมายเหตุ
ถ้า playback ของ AppServ ต้องใช้ signed URL (`exp` / `sig`) แต่ backend หาไม่เจอ GIF อาจยังสร้างไม่ได้
ในกรณีนั้นให้เปิด `/api/debug/camlist?station=P.67` แล้วส่งผลกลับมาเพื่อปรับ parser ต่อ


## แก้ไข v3
- แก้ 502 ที่ `/api/status` และ `/camera/latest`
- ถ้า camlist ไม่มี JPG ล่าสุด จะ fallback ไปอ่านหน้า `pingriver.php?station=P.x`
- Playback error จะไม่ทำให้ status ทั้งหมดล้ม
- เพิ่ม `/api/debug/latest?station=P.67`


## แก้ไข v4
- คืน extractor แบบ v1 ที่เคยจับ CCTV P.67 ได้
- รองรับกรณี AppServ ส่งมาแค่ชื่อไฟล์ `cctv_YYYYMMDDHHMMSS_hash.jpg`
- ประกอบ `/cache/P.x/YYYY/MM/<filename>` อัตโนมัติ
- ตรวจ 3 source: `op=camlist`, หน้า station และ `ajax_data_only`
- เพิ่ม `/api/debug/latest-sources?station=P.67`


## แก้ไข v5
- เพิ่มรองรับ signed Playback URL ผ่าน Render Environment Variables
- ใช้ได้ทั้งแบบ:
  - `PLAYBACK_P1_URL_SAMPLE`
  - `PLAYBACK_P67_URL_SAMPLE`
  - หรือ `PLAYBACK_P1_EXP` / `PLAYBACK_P1_SIG`, `PLAYBACK_P67_EXP` / `PLAYBACK_P67_SIG`
- เพิ่ม `/api/debug/playback-auth?station=P.67`
- ถ้า AppServ ไม่ส่ง `exp/sig` มาเอง ระบบจะ fallback ไปใช้ค่าจาก env


## แก้ไข v6 — automatic AppServ session
จาก JavaScript ต้นทาง `DvdPlayer.auth` มาจาก camlist
v6 จึง:
1. เปิดหน้า station ก่อนเพื่อรับ session/cookie
2. เรียก camlist ด้วย requests.Session เดิม
3. หา exp/sig ทั้ง top-level และ nested `auth`
4. ส่ง Referer, Origin, User-Agent และ Cookie เดิมให้ ffmpeg
5. Environment Variables ของ v5 ยังใช้เป็น fallback ได้

Debug:
- `/api/debug/session-camlist?station=P.1`
- `/api/debug/session-camlist?station=P.67`


## แก้ไข v7 — ดาวน์โหลด MP4 ก่อนค่อยสกัดเฟรม
จาก debug ของ v6 พบว่า camlist มี `exp/sig` แล้ว แต่ ffmpeg ยิง URL ตรง ๆ ยัง 403
v7 จึงเปลี่ยนวิธี:
1. ใช้ `requests.Session` เดิมดาวน์โหลด signed MP4 จาก AppServ/Playback
2. บันทึกเป็นไฟล์ชั่วคราว local
3. ใช้ ffmpeg อ่านไฟล์ local เพื่อสกัดเฟรม

เพิ่ม debug:
- `/api/debug/playback-url-sample?station=P.1`
- `/api/debug/playback-url-sample?station=P.67`


## แก้ไข v8 — seek ตาม duration จริงของ MP4
v7 ดาวน์โหลด MP4 ได้แล้ว แต่ใช้ offset 0/900/1800/2700 วินาทีตรง ๆ
ซึ่งอาจเกิน duration จริงของวิดีโอ playback

v8:
- ใช้ ffprobe อ่าน duration จริง
- map นาที 0/15/30/45 เป็น 0/25/50/75% ของไฟล์
- ลอง accurate seek และ fast seek
- ดาวน์โหลด MP4 แต่ละชั่วโมงเพียงครั้งเดียว
- ข้ามเฟรมเสียบางเฟรมได้ ไม่ทำให้ทั้งงานล้มทันที
- เพิ่ม `/api/debug/video-probe?station=P.1`


## แก้ไข v9 — anchor ตาม Playback ล่าสุด
ปัญหา v8: ถ้าปัจจุบัน 13:xx แต่ AppServ มีไฟล์ล่าสุดถึง 11.mp4
ระบบ hours=1 จะมองหา 12.mp4 และได้ 0 task

v9:
- ใช้ไฟล์ hourly ล่าสุดที่มีจริงเป็นปลายช่วง
- 11.mp4 = ช่วง 11:00-12:00
- hours=1 จะเลือกเฟรมจาก 11:00-12:00
- hours=24 จะย้อนหลัง 24 ชั่วโมงจาก playback ล่าสุด
- ไม่ขึ้น `ไม่มี task` เพียงเพราะไฟล์ชั่วโมงปัจจุบันยังสร้างไม่เสร็จ


## แก้ไข v10 — ใช้ฟอนต์ Prompt
จากภาพที่สร้างได้แล้ว พบว่าข้อความไทยใน PNG/GIF เพี้ยนหรือขึ้นเป็นสี่เหลี่ยม
v10 จึง:
- ติดตั้งฟอนต์ `Prompt` ใน Dockerfile
- ใช้ `Prompt-Regular.ttf` และ `Prompt-Bold.ttf` เป็นฟอนต์หลัก
- ถ้าโหลด Prompt ไม่ได้ จะ fallback ไป `Noto Sans Thai`

หลัง Deploy ใหม่ ข้อความไทยใน PNG/GIF ควรอ่านได้ปกติ


## แก้ไข v11 — แสดงสถานะตอนสร้าง GIF
เพิ่มระบบ job + progress สำหรับการสร้าง GIF:
- กดปุ่มแล้วไม่เด้งไปค้างที่หน้าดาวน์โหลดทันที
- หน้าเว็บจะแสดงสถานะและเปอร์เซ็นต์ความคืบหน้า
- เมื่อเสร็จ ระบบจะดาวน์โหลดไฟล์ให้อัตโนมัติ

API ใหม่:
- `/api/job/start-gif`
- `/api/job/start-gif-combined`
- `/api/job-status?job_id=...`
- `/api/job-download?job_id=...`


## แก้ไข v12 — จัดข้อความส่วนล่างของภาพใหม่
ปรับเลย์เอาต์ข้อมูลด้านล่างให้พอดีกับพื้นที่มากขึ้น:
- ใต้ตัวเลขระดับน้ำ เพิ่มบรรทัดเวลา
- `เวลา CCTV` และค่าวันเวลาอยู่บรรทัดเดียวกัน
- ข้อความ `ข้อมูล AppServ / ระบบโทรมาตร กรมชลประทาน` ปรับขนาดและตัดบรรทัดให้อยู่ในกรอบ
- เพิ่มตัวปรับขนาดฟอนต์อัตโนมัติตามความกว้างพื้นที่


## แก้ไข v13 — ปรับข้อความและโทนภาพตามโจทย์ล่าสุด
- เปลี่ยนส่วนล่างให้แสดงเพียง:
  - ระดับน้ำปัจจุบัน
  - วันที่เวลา
  - ช่วงเวลา
- ตัดหัวข้อ `เวลา CCTV` ออก เพราะมีเวลาอยู่บนภาพแล้ว
- เพิ่มหัวข้อ `ช่วงเวลา` เช่น `ช่วง 11/08/2026 11:00 - 12:00 น.`
- ขยายส่วนหัวให้ใหญ่ขึ้น
- ปรับโทนภาพรวมให้เป็นสีฟ้ามากขึ้น


## แก้ไข v14 — Hotfix fit_font_to_width
v13 มีบั๊กจากการรวมไฟล์ ทำให้ helper สำหรับจัดขนาดข้อความหลุดออกไป และเกิด:
`name 'fit_font_to_width' is not defined`

v14 คืนฟังก์ชัน:
- `text_size`
- `fit_font_to_width`
- `wrap_text_to_width`

คงความสามารถเดิมทั้งหมด:
- Prompt font
- Layout/โทนฟ้า v13
- GIF Playback
- Progress bar / สถานะการสร้าง GIF
