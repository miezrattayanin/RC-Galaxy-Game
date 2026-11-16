# RC BEYOND TOGETHER: GALACTIC FLEET

เว็บเกมออนไลน์แบบ Multiplayer แบบเรียลไทม์สำหรับกิจกรรมองค์กร (5 ทีม) พร้อมหน้าจอ Host ขึ้นจอใหญ่และ QR Code

## การติดตั้งและเปิดใช้งาน (สำหรับเล่นจริงข้ามอุปกรณ์)

1. ติดตั้ง Python 3.10+ และแพ็กเกจที่จำเป็น:
   ```bash
   pip install -r requirements.txt
   ```
2. รันเซิร์ฟเวอร์:
   ```bash
   uvicorn server:app --host 0.0.0.0 --port 8000
   ```
   หรือรัน `./start.sh`
3. การใช้งานในงานกิจกรรม:
   - **หน้าจอ Host (ขึ้นจอใหญ่/โปรเจกเตอร์):** เปิด `http://<IP-ของเครื่อง>:8000/?host=1&room=RC2026` หรือกดปุ่ม **เปิดหน้าจอ Host (ขึ้นจอใหญ่)** ในหน้าแรก
   - **ผู้เล่นทั้ง 5 ทีม (มือถือ/แท็บเล็ต/คอมพิวเตอร์):** สแกน **QR Code** จากหน้าจอ Host หรือเปิดลิงก์เดียวกันเพื่อกรอกรหัสห้อง (เช่น `RC2026`) และเลือกทีม

## การ Deploy ขึ้น Cloud (เช่น Render / Railway / Fly.io)
- Start Command: `uvicorn server:app --host 0.0.0.0 --port $PORT`
- ฐานข้อมูล SQLite (`galactic_fleet.db`) จะถูกสร้างอัตโนมัติในโฟลเดอร์โปรเจกต์ (สามารถกำหนด `DB_PATH` เพื่อชี้ไปยัง Persistent Disk ได้)
