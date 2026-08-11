# Ping River Image Center v7

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
