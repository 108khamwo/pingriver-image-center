# Ping River Image Center v4

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
