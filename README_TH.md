# Ping River Image Center

ระบบออนไลน์สำหรับสร้างภาพจริงจาก CCTV + ระดับน้ำแม่น้ำปิง

สถานี:
- P.1 สะพานนวรัฐ
- P.67 บ้านแม่แต

ความสามารถ:
- ดู CCTV ล่าสุดผ่าน backend
- อ่านระดับน้ำย้อนหลังจากหน้า AppServ
- สร้าง PNG ล่าสุด 1080x1080
- สร้าง GIF ย้อนหลัง 1/3/6/12/24/48/72 ชั่วโมง
- เลือกเฟรมทุก 5/10/15/30 นาที
- GIF เปรียบเทียบ P.1 + P.67
- ไม่ต้องเก็บภาพถาวรบน Render

## Deploy บน Render แบบฟรี

1. สร้าง GitHub repository ใหม่ เช่น `pingriver-image-center`
2. อัปโหลดไฟล์ทั้งหมดในโฟลเดอร์นี้เข้า repository
3. เข้า Render Dashboard
4. New > Web Service
5. เชื่อม GitHub และเลือก repository
6. Language เลือก `Docker`
7. Instance Type เลือก `Free`
8. Health Check Path ใส่ `/health` ถ้ามีช่อง
9. กด Create Web Service / Deploy
10. รอ build เสร็จ แล้วเปิด URL `https://ชื่อระบบ.onrender.com`

Dockerfile จะติดตั้ง Noto Sans Thai ให้เอง จึงไม่ต้องแนบไฟล์ฟอนต์

## ทดสอบหลัง Deploy

เปิด:
- `/health`
- `/api/status?station=P.1`
- `/api/status?station=P.67`
- `/api/history-check?station=P.67&hours=24`
- `/api/debug/camlist?station=P.67`

ถ้า `/api/history-check` พบภาพย้อนหลังจำนวนมาก แปลว่าพร้อมสร้าง GIF

## หมายเหตุ Render Free

ไฟล์ที่สร้างเป็นไฟล์ชั่วคราวและส่งให้ดาวน์โหลดทันที
จึงไม่พึ่ง Persistent Disk และไม่ต้องเก็บภาพทุก 5 นาที
