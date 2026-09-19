# รันได้ทุกโฮสต์ที่มี Docker (Railway / Render / Fly.io / Koyeb / VPS)
# default ใช้ SQLite ใน /data (ไม่ต้องมี Postgres) —
# ถ้าตั้ง DATABASE_URL เป็น Postgres จะใช้ Postgres แทนอัตโนมัติ
FROM python:3.13-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py quest_api.py db.py presence.py ./

ENV SQLITE_PATH=/data/questbot.db

VOLUME ["/data"]

CMD ["python", "bot.py"]
