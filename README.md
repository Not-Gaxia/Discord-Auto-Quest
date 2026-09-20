# Discord Quest Auto-Farmer (multi-user · Railway-hostable)

แผง embed ปักในห้อง → เพื่อนๆ กดปุ่มลงทะเบียน **token ของตัวเอง** → บอทเก็บลง
**Postgres (เข้ารหัส)** แล้ว farm **เควสวิดีโอ** ให้อัตโนมัติ 24/7 + จัดอันดับว่าใครทำเยอะสุด

## ปุ่มในแผง

| ปุ่ม | ทำอะไร |
|---|---|
| ➕ **เพิ่ม Token** | เปิด modal วาง token → ตรวจสอบ → เก็บแบบเข้ารหัส แล้วเริ่ม farm ทันที |
| 📊 **สถานะของฉัน** | ดูบัญชีของตัวเอง + จำนวนเควสที่ทำ + สถานะ token |
| 🗑️ **ลบ Token** | เลือกบัญชีของตัวเองออกจากระบบ |
| 🏆 **อันดับ** | top 10 ว่าใครทำเควสเยอะสุด (นับรวมทุก token ของคนนั้น) |

> ทุกคนจัดการได้เฉพาะ token **ของตัวเอง** (ผูกกับ Discord id คนกด) · `/setup_panel` ปักแผงได้เฉพาะ `OWNER_ID`

## บอททำอะไรได้ / ไม่ได้

| Quest type | บน cloud |
|---|---|
| 📺 `WATCH_VIDEO` | ✅ **farm จบในตัวอัตโนมัติ** |
| 🖥️ `PLAY_ON_DESKTOP` | ❌ heartbeat ต้องมาจาก Discord desktop native client — REST ปลอมไม่ได้ |

---

## Setup

### 1. สร้าง Bot
1. https://discord.com/developers/applications → **New Application**
2. แท็บ **Bot** → **Reset Token** → copy = `DISCORD_BOT_TOKEN`
3. **OAuth2 → URL Generator**: scopes `bot` + `applications.commands`,
   permissions `Send Messages` + `Embed Links` → เปิด URL → invite เข้า server

### 2. id ที่ต้องใช้
- `OWNER_ID` = user id ของคุณ (Developer Mode → คลิกขวาตัวเอง → Copy User ID)
- `GUILD_ID` = คลิกขวา server → Copy Server ID (ใส่แล้ว slash โผล่ทันที)

### 3. สร้าง ENCRYPTION_KEY (ครั้งเดียว ห้ามเปลี่ยน)
```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```
> เปลี่ยน key เมื่อไหร่ = decrypt token เก่าทั้งหมดไม่ได้ ทุกคนต้องลงทะเบียนใหม่

### 4. รัน local (เลือก 1 วิธี)

**วิธี A — เบาสุด ไม่ต้องมี Postgres (SQLite):**
```bash
pip install -r requirements.txt
copy .env.example .env          # แก้ DISCORD_BOT_TOKEN / OWNER_ID / ENCRYPTION_KEY
python bot.py                   # ไม่ตั้ง DATABASE_URL = ใช้ไฟล์ questbot.db อัตโนมัติ
```
ใน Discord: พิมพ์ `/setup_panel` ในห้องที่อยาก → ปักหมุดแผง → กดปุ่มทดสอบ

**วิธี B — Docker (บอท + Postgres):**
```bash
copy .env.example .env          # แก้ค่า + ตั้ง POSTGRES_PASSWORD
docker compose up -d --build
docker compose logs -f bot
```

### 5. Deploy โฮสต์อื่น (ที่ไม่ใช่ Railway)

บอทเป็น **worker** ไม่ต้อง expose port — โฮสต์ไหนรัน Docker ได้ก็รันได้:

| โฮสต์ | วิธี |
|---|---|
| **Render (ฟรี ไม่ต้องมีบัตร)** | ดูขั้นตอน Render ด้านล่าง — Blueprint `render.yaml` เตรียมไว้แล้ว + Postgres ฟรีข้างนอก (Supabase/Neon) |
| Koyeb (พักให้บริการชั่วคราว) | เดิมใช้ได้ ตอนนี้ปิดอยู่ — กลับมาเมื่อไหร่ดูข้อ 5.2 |
| Fly.io | New service → **Deploy from GitHub** → เลือก repo นี้ (มันเจอ `Dockerfile` เอง) → ใส่ env `DISCORD_BOT_TOKEN`, `OWNER_ID`, `ENCRYPTION_KEY` + `DATABASE_URL` (Postgres ฟรีข้างนอก) |
| VPS (Ubuntu/Debian) | `git clone` → `cp .env.example .env` (แก้ค่า) → `docker compose up -d --build` (ได้ Postgres ด้วย) |
| อยากได้ Postgres บน Render/Fly | add managed Postgres ของโฮสต์นั้น → ก๊อป connection string มาใส่ `DATABASE_URL` → บอทสลับไปใช้ Postgres เอง |

> ย้ายเครื่อง/ย้าย DB ทีหลังได้ — แต่ `ENCRYPTION_KEY` ต้องเป็นค่าเดิมเสมอ ไม่งั้นถอด token เก่าไม่ได้

### 5.1 Deploy Render ฟรี (ไม่ต้องมีบัตร) — ทางหลักตอนนี้

เงื่อนไขฟรีที่ต้องรู้: **Web service sleep ถ้าเงียบ 15 นาที** (Worker ฟรีไม่มี) +
**Postgres ของ Render หมดอายุหลัง 90 วัน** — เลยใช้ DB ข้างนอกแทน:

1. สมัคร https://supabase.com (หรือ neon.tech) → สร้าง Postgres ฟรี → ก๊อป connection string
   (รูปแบบ `postgresql://...` — บอทสลับไปใช้เอง)
2. push repo นี้ขึ้น GitHub
3. สมัคร https://render.com (ด้วย GitHub — ไม่ต้องมีบัตร) →
   **New → Blueprint** → เลือก repo → Apply (อ่าน `render.yaml` เอง: Web + Docker + `/healthz`)
4. ใส่ env ใน dashboard: `DISCORD_BOT_TOKEN` · `OWNER_ID` · `ENCRYPTION_KEY` ·
   `DATABASE_URL` (จากข้อ 1) — (`PORT` Render ใส่ให้เอง)
5. Deploy → เปิด `https://<ชื่อ>.onrender.com/healthz` เห็น `ok` = รอด → ไป `/setup_panel`
6. **กัน sleep (บังคับ):** สมัคร https://cron-job.org (ฟรี) → job ping
   `https://<ชื่อ>.onrender.com/healthz` ทุก **10 นาที** (ต้องถี่กว่า 15 นาที)

### 5.2 Koyeb (พักให้บริการชั่วคราว — 2026-09-20)

เงื่อนไขฟรีที่ต้องรู้: ใช้ได้แค่ **Web service** (Worker ไม่ได้) + **sleep ถ้าไม่มี traffic 1 ชม.**
บอทนี้เตรียมไว้แล้ว (`/healthz` + ใช้ Postgres) — ทำตามนี้:

1. push repo นี้ขึ้น GitHub
2. สมัคร https://koyeb.com (ด้วย GitHub — ไม่ต้องมีบัตร) → **Create Service → GitHub** → เลือก repo
   - Builder: **Dockerfile** · Instance: **Free** · Region: Frankfurt หรือ Washington
   - Service type: **Web** · Port: `8000` · Health check path: `/healthz`
3. แท็บ **Database → Create Postgres (Free)** → ก๊อป connection string
4. แท็บ **Environment** ของ service ใส่:
   `DISCORD_BOT_TOKEN` · `OWNER_ID` · `ENCRYPTION_KEY` · `DATABASE_URL` (จากข้อ 3)
   (`PORT` Koyeb ใส่ให้เอง — ไม่ต้องตั้ง)
5. Deploy → เปิด `https://<ชื่อ>.koyeb.app/healthz` เห็น `ok` = รอด → ไป `/setup_panel`
6. **กัน sleep:** สมัคร https://cron-job.org (ฟรี) → สร้าง job ping
   `https://<ชื่อ>.koyeb.app/healthz` ทุก **30 นาที** → service จะไม่ scale-to-zero

> Koyeb Postgres ฟรีเองก็ sleep ตอนเงียบ 5 นาที — ไม่เป็นไร มันตื่นเองเมื่อบอทต่อเข้าไป

### 6. Deploy Railway (เหมือนเดิม)
1. push โฟลเดอร์นี้ขึ้น GitHub
2. https://railway.app → **New Project → Deploy from GitHub repo**
3. ในโปรเจกต์เดียวกัน กด **+ New → Database → Add PostgreSQL**
   (Railway จะ inject `DATABASE_URL` ให้บอทอัตโนมัติ)
4. แท็บ **Variables** ของ service บอท → ใส่ `DISCORD_BOT_TOKEN`, `OWNER_ID`,
   `ENCRYPTION_KEY`, (`GUILD_ID`)
5. Deploy → ไปที่ server → `/setup_panel`

---

## โครงสร้าง
```
bot.py         แผงปุ่ม + modal + auto-farm loop + leaderboard + /setup_panel
quest_api.py   REST layer ของ Discord Quest (login / list / enroll / video-progress)
db.py          Postgres pool + schema + เข้ารหัส token (Fernet) + CRUD + leaderboard
```

## ตาราง DB
- `accounts` — 1 แถว = 1 token (เก็บ `token_enc` เข้ารหัส, ผูก `discord_user_id` คนลงทะเบียน)
- `completions` — log เควสที่ทำเสร็จ (กันนับซ้ำด้วย `UNIQUE(account_id, quest_id)`)

## ความเสี่ยง / ความปลอดภัย
- การ automate บัญชี Discord ผิด **ToS** — บัญชีโดนแบนถาวรได้ (ผู้ลงทะเบียนยอมรับเอง)
- คุณเป็น **ผู้ดูแล token ของเพื่อน** — token เก็บแบบเข้ารหัส แต่ถ้า `ENCRYPTION_KEY` + DB หลุดพร้อมกัน = ถอดได้ ดูแล key ให้ดี
- อย่า commit `.env` ขึ้น repo (มี `.gitignore` กันแล้ว) · ใส่ secret ใน Railway Variables เท่านั้น
