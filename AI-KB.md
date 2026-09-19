# AI-KB — Discord Quest Auto-Farmer

> เอกสารความรู้ครบของโปรเจกต์ ไว้ให้ AI หรือคนโหลด context กลับมาทำงานต่อได้ทันที
> อัปเดตล่าสุด: 2026-06-27 · repo: https://github.com/Gaxia-XP/Discord-Auto-Quest

---

## 1. ภาพรวม (TL;DR)

บอท Discord ที่ **farm เควส (Quests) ให้อัตโนมัติแบบ multi-user** ปักแผง (panel) ในห้อง →
ใครก็ลงทะเบียน **user token** ของตัวเองผ่านปุ่ม → บอทเก็บลง **Postgres (เข้ารหัส)** แล้ว
farm **เควสวิดีโอ + เควสเกม** ให้เองตลอด 24/7 + มี leaderboard แข่งกัน

- **รันที่ไหน:** Railway (worker) + Railway Postgres — หรือ local + Docker Postgres
- **ภาษา:** Python 3.13, discord.py 2.7.1 (บอทจริง ไม่ใช่ self-bot lib), asyncpg, cryptography
- **บอท:** `Hermes-Agent#5154` (application/client_id `1506725211691683850`)
- ⚠️ การ automate user token **ผิด Discord ToS** — บัญชีโดนแบนถาวรได้ (ผู้ลงทะเบียนรับความเสี่ยงเอง)

---

## 2. 🔑 เบรกทรูสำคัญที่สุด: farm เควสเกม (PLAY_ON_DESKTOP) แบบ headless ได้

**เดิมเข้าใจผิดว่า** เควสเกม credit เวลาได้เฉพาะจาก Discord desktop client เพราะ heartbeat
มี "attestation" ปลอมไม่ได้ (REST ยิงเองได้ `401/40001` ตลอด) → เลยทำ hybrid (desktop console script)

**ความจริง (พิสูจน์แล้วด้วยการทดสอบสด):** `401/40001` เกิดเพราะ **User-Agent ไม่มี `Electron/<version>`** เฉยๆ
พอใส่ Electron เข้าไป heartbeat ผ่าน `200` และเครดิตเวลาได้ปกติ → **farm เกมบนคลาวด์ได้ ไม่ต้องเปิดเครื่อง/desktop**

```
[UA: ...discord/1.0.9201]                         -> 401 (code 40001)   ❌
[UA: ...discord/1.0.9201 ... Electron/32.2.7 ...] -> 200  progress++    ✅
```

ที่มาของข้อมูล: docs.discord.food/resources/quests —
> "Heartbeated quest tasks may only be completed from desktop clients. If the requesting
> user-agent does not include electron version info (e.g. `Electron/28.2.10`), the request
> will fail with a 401 unauthorized error."

> หมายเหตุ: aamiaa console script (gist) ใช้คนละวิธี (fake `RunningGameStore` แล้วปล่อยให้ client
> ยิง heartbeat เอง) เพราะมันรัน *ใน* client — แต่ **headless REST + Electron UA ก็ได้ผลเหมือนกัน**

---

## 3. สถาปัตยกรรม / ไฟล์

```
quest-bot-hosted/
├─ bot.py          แกนหลัก: panel/ปุ่ม/modal, autofarm, leaderboard, /setup_panel, refresh+presence loop
├─ quest_api.py    REST layer ของ Discord Quest (login/list/enroll/video-progress/heartbeat/farm_*)
├─ db.py           Postgres pool + schema + เข้ารหัส token (Fernet) + CRUD + leaderboard + panels + prefs
├─ presence.py     Rich Presence บน user token (gateway WS) — โชว์เควสบนโปรไฟล์ user ระหว่าง farm
├─ requirements.txt  discord.py>=2.4, aiohttp, python-dotenv, asyncpg, cryptography
├─ railway.json    build config (NIXPACKS, startCommand: python bot.py, restart ON_FAILURE)
├─ Procfile        worker: python bot.py
├─ .env            ค่าจริง (gitignored)
├─ .env.example    เทมเพลต env
├─ AI-KB.md        เอกสารนี้ (gitignored — local only)
└─ .gitignore      .env, __pycache__/, *.pyc, *.log, AI-KB.md
```

**แยกบทบาท token (สำคัญ):**
- **BOT token** (`DISCORD_BOT_TOKEN`) → ตัวบอท `Hermes-Agent` (รับคำสั่ง, ส่ง DM/embed, ปักแผง)
- **USER tokens** (เก็บใน DB) → token บัญชีผู้ใช้แต่ละคน ใช้ยิง REST quest ของบัญชีนั้นๆ

---

## 4. กลไกเควส (Quest REST API)

Base: `https://discord.com/api/v10`

**Headers ทุก request (ใน `quest_api.py::_req`):**
```
Authorization:      <user_token>        # raw token ไม่มี "Bearer"
X-Super-Properties:  <base64 desktop super-properties>
Content-Type:        application/json
User-Agent:          ...discord/1.0.9201 Chrome/128.0.6613.186 Electron/32.2.7 Safari/537.36
                     # ⚠️ ต้องมี Electron/<ver> ไม่งั้น heartbeat = 401/40001
timeout:             30s
```

**Endpoints:**
| Method | Path | Body | ใช้ทำ |
|---|---|---|---|
| GET | `/users/@me` | — | login / เอา username + user_id |
| GET | `/quests/@me` | — | list เควสทั้งหมด |
| POST | `/quests/{id}/enroll` | `{"location": 0}` | สมัครเควส |
| POST | `/quests/{id}/video-progress` | `{"timestamp": n}` | credit **WATCH_VIDEO** |
| POST | `/quests/{id}/heartbeat` | `{"stream_key": "call:<quest_id>:1", "terminal": false}` | credit **PLAY_ON_DESKTOP** / STREAM / ACTIVITY |

**โครงสร้าง quest (จาก `/quests/@me`):**
- `config.task_config_v2.tasks.<TYPE>.target` → วินาทีที่ต้องทำ (เกมปกติ **900** = 15 นาที)
- `config.application.id` / `.name`, `config.expires_at`
- `user_status.enrolled_at` / `.completed_at` / `.progress.<TYPE>.value`

**ประเภทเควส & การ farm:**
| Type | วิธี farm | headless ได้? |
|---|---|---|
| `WATCH_VIDEO` / `_ON_MOBILE` | `video-progress` ส่ง timestamp ทีละ ~7s จนถึง target | ✅ `farm_video()` |
| `PLAY_ON_DESKTOP` | `heartbeat` ทุก ~90s, `stream_key="call:<quest_id>:1"` | ✅ `farm_game()` |
| `STREAM_ON_DESKTOP` | ต้อง stream จริง + มีคนใน vc ≥1 | ❌ ไม่ทำ |
| `PLAY_ACTIVITY` | heartbeat + อยู่ใน voice call (`call:<channel_id>:1`) | ❌ ไม่ทำ |

**กฎการเครดิตเวลา (heartbeat):**
- `progress.value` เพิ่มขึ้น = **วินาทีจริงที่ผ่านไปตั้งแต่ beat ที่แล้ว** (cap 120s/beat)
- ⇒ เควสเกม 900s = ต้องใช้เวลาจริง **~15 นาที** เร่งไม่ได้ (server นับเวลาจริง)
- beat แรกคือ priming (value 0) แล้วเวลาเริ่มสะสมจาก beat นั้น
- ยิงครบ → ส่ง beat สุดท้าย `terminal: true`

**Error codes ที่เจอ:**
- `401 / 40001` "ไม่ได้รับอนุญาต" → **UA ไม่มี Electron** (จุดตายเดิม)
- `404 / 260017` "ภารกิจหมดอายุ" → เควสหมดอายุ/เลย expires_at

---

## 5. Database schema (Postgres)

```sql
accounts (
  id BIGSERIAL PK,
  discord_user_id BIGINT,        -- คนที่ลงทะเบียน (เจ้าของ token นี้)
  discord_name TEXT,
  token_enc TEXT,                -- token เข้ารหัส Fernet
  account_user_id TEXT UNIQUE,   -- id บัญชี Discord ที่ token เป็น (กันลงซ้ำ)
  username TEXT, active BOOLEAN, last_error TEXT, added_at TIMESTAMPTZ
)
completions (
  id BIGSERIAL PK,
  account_id BIGINT FK->accounts ON DELETE CASCADE,
  quest_id TEXT, quest_name TEXT, completed_at TIMESTAMPTZ,
  UNIQUE(account_id, quest_id)   -- กันนับซ้ำ
)
panels (
  message_id BIGINT PK, channel_id BIGINT, created_at TIMESTAMPTZ
)  -- ตำแหน่งแผงที่ปัก ไว้ auto-refresh
user_prefs (
  discord_user_id BIGINT PK, presence_enabled BOOLEAN DEFAULT TRUE
)  -- เปิด/ปิด presence บนโปรไฟล์ รายคน (toggle ผ่านปุ่ม 👁️)
notify_msgs (
  discord_user_id BIGINT PK, channel_id BIGINT, message_id BIGINT
)  -- embed สรุปเควสเสร็จ 1 อันต่อคน (ไว้แก้ซ้ำ ไม่ DM รัวๆ)
```

> **แจ้งเตือนเควสเสร็จ:** ไม่ DM ทุกครั้ง — ใช้ **embed เดียวต่อคน** (`update_completion_embed`)
> แก้ embed สะสมรายการ (recent 12 + total, relative time) แล้วส่งข้อความ bump → ลบ เพื่อเด้ง DM ขึ้นบน + คลีน

- **Leaderboard** = group by `discord_user_id`, นับ completions รวมทุก token ของคนนั้น
- `user_rank()` คืนอันดับของคนๆ นั้น, `global_stats()` คืน (accounts, users, quests)
- `get_presence_pref()/set_presence_pref()` — pref presence รายคน (default เปิด)

---

## 6. การเข้ารหัส token (Fernet)

- ใช้ `cryptography.fernet` คีย์จาก env `ENCRYPTION_KEY`
- `encrypt()/decrypt()` อ่าน key **แบบ lazy** (ตอนใช้จริง) ไม่ใช่ตอน import — กัน import order bug
- ⚠️ **ถ้าเปลี่ยน `ENCRYPTION_KEY` = token เก่าที่เข้ารหัสไว้ decrypt ไม่ได้ทั้งหมด** (ทุกคนต้องลงทะเบียนใหม่)
- สร้างคีย์: `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`

---

## 7. Panel UI (แผงควบคุม)

- `/setup_panel` (เจ้าของบอทเท่านั้น) โพสต์ embed + `PanelView` แล้วเก็บ message_id ลงตาราง `panels`
- **persistent View** (`timeout=None` + ทุกปุ่มมี `custom_id`) + `bot.add_view()` ใน setup_hook → ปุ่มใช้ได้ต่อแม้บอท restart
- **7 ปุ่ม:**
  | ปุ่ม | custom_id | ทำอะไร |
  |---|---|---|
  | ➕ เพิ่ม Token | `panel:add` | เปิด Modal วาง token → validate (login) → เก็บเข้ารหัส → เตะ farm ทันที |
  | 📊 สถานะของฉัน | `panel:status` | ephemeral: บัญชี + **เควสที่ทำอยู่ (progress bar) + คิว + วิดีโอ** + footer pref presence |
  | 🗑️ ลบ Token | `panel:remove` | Select บัญชีของตัวเอง → ลบ (เช็คเจ้าของ) |
  | 🏆 อันดับ | `panel:lb` | top 10 + progress bar `█░` + อันดับตัวเอง + ยอดรวม |
  | 🚜 Farm ทันที | `panel:farmnow` | สแกน+farm **เฉพาะบัญชีของคนกด** ทันที |
  | ❓ วิธีใช้ | `panel:help` | ephemeral: สอนเอา token ทีละสเตป |
  | 👁️ Presence โปรไฟล์ | `panel:presence` | **toggle รายคน** เปิด/ปิดโชว์เควสบนโปรไฟล์ตัวเอง (ปิด→เคลียร์ทันที) |
- **`bot.farm_state[account_id]`** = `{current:{name,done,target}, queue:[...]}` — สถานะสด ป้อนทั้งปุ่ม status + presence
- **auto-refresh:** loop ทุก 60s แก้ embed ของทุก panel ใน DB ให้โชว์สถิติสด (คนละ loop กับ farm)

### 7.1 Bot presence (สถานะของตัวบอทเอง)
- `rotate_presence` loop ทุก 30s สลับ 8 สถานะ (`build_statuses`) — Playing/Watching/Streaming🟣/Listening/Competing + ดึงสถิติสด
- ⚠️ BOT โชว์ได้แค่ **type + name** (+url streaming) — **ไม่มี** GIF/details/state (rich presence เป็นของ user account)

### 7.2 User presence (`presence.py`) — โชว์เควสบนโปรไฟล์ user
- เปิด **gateway WS ต่อ token** (op2 identify + presence, op1 heartbeat, op3 update) — เหมือน self-bot presence
- ระหว่าง farm เกม: โปรไฟล์ user โชว์ `Playing <quest>` + `details: กำลังทำเควส X%` + `state: done/target` + timer
- เชื่อมตอนเริ่ม farm บัญชีนั้น → ตัดตอน farm จบ (`PresenceManager.set_quest/clear`)
- คุมด้วย: env `ENABLE_PRESENCE` (master, default 1) **และ** pref รายคน `user_prefs.presence_enabled`
- ⚠️ self-bot — บัญชีออนไลน์ broadcast = **เสี่ยงแบนสูงขึ้นกว่า farm REST เงียบๆ**

---

## 8. โมเดล concurrency (สำคัญต่อ ban-safety)

- **autofarm loop** (`@tasks.loop(minutes=FARM_INTERVAL_MIN)`, default 10): สแกนทุกบัญชี **พร้อมกัน**
  ผ่าน `asyncio.gather(_safe_scan(r) ...)` (ตัวนึงค้าง/พังไม่ลามตัวอื่น)
- **วิดีโอ:** spawn task แยกต่อ quest → ทำพร้อมกันได้ (`bot.farm_tasks[(acc_id, quest_id)]`)
- **เกม:** **worker เดียวต่อบัญชี** (`bot.game_workers[acc_id]`) ไล่ทำ **ทีละเควส** — เพราะยิง heartbeat
  หลายเกมพร้อมกัน = เหมือนเล่นหลายเกมพร้อมกัน = สัญญาณบอทชัด เสี่ยงแบน
- เพดาน: เกม = 1/บัญชี (N บัญชี = N พร้อมกัน) · วิดีโอ = ไม่จำกัด
- 🚜 Farm ทันที = สแกนเฉพาะบัญชีคนกด (ไม่ทุกคน) · auto loop = ทุกคน

---

## 9. Environment variables

| ตัวแปร | จำเป็น | ความหมาย |
|---|---|---|
| `DISCORD_BOT_TOKEN` | ✅ | token บอท (Developer Portal) |
| `DATABASE_URL` | ✅ | Postgres conn string (Railway ใส่ให้เองเมื่อ add Postgres; ใช้ `${{Postgres.DATABASE_URL}}`) |
| `ENCRYPTION_KEY` | ✅ | Fernet key — **ห้ามเปลี่ยน** |
| `OWNER_ID` | ✅ | Discord id เจ้าของ (ใช้สิทธิ์ `/setup_panel`) |
| `GUILD_ID` | optional | server id — ใส่แล้ว slash sync ทันที |
| `FARM_INTERVAL_MIN` | optional | กี่นาทีต่อรอบสแกน (default 10; โปรเจกต์นี้ตั้ง 120 = ประหยัด API/ปลอดภัย) |
| `ENABLE_PRESENCE` | optional | master switch presence (default 1; `0`=ปิดทั้งระบบ). รายคนคุมอีกชั้นที่ปุ่ม 👁️ |

> `FARM_INTERVAL_MIN` คุมแค่ "ความถี่ค้นหาเควสใหม่" — ไม่กระทบความเร็ว heartbeat (~90s) หรือเวลาเควสเสร็จ (real-time)
> presence เปิดจริงต่อเมื่อ: `ENABLE_PRESENCE≠0` **และ** ผู้ใช้คนนั้นเปิด pref (default เปิด)

---

## 10. Local dev (Docker Postgres)

```bash
# ⚠️ host port = 5544 (ไม่ใช่ 5432) เพราะเครื่องนี้มี Postgres ตัวอื่นจอง 5432 อยู่
docker run -d --name questbot-pg -e POSTGRES_PASSWORD=questbot \
  -e POSTGRES_DB=questbot -p 5544:5432 postgres:16
# DATABASE_URL=postgresql://postgres:questbot@localhost:5544/questbot

pip install -r requirements.txt
PYTHONIOENCODING=utf-8 python bot.py   # ⚠️ ตั้ง UTF-8 ไม่งั้น console cp1252 พังตอน print อิโมจิ/ไทย
```
- container แค่ `docker stop` ข้อมูลไม่หาย (อยู่ใน writable layer) — `docker start questbot-pg` กลับมาได้
- ⚠️ **local bot ดับทุกครั้งที่ session/เครื่อง idle** (Docker Desktop ปิด, process ตาย) → เหตุผลที่ต้องขึ้น Railway

---

## 11. Deploy Railway

1. railway.app → New Project → Deploy from GitHub repo → เลือก `Discord-Auto-Quest`
2. + New → Database → Add PostgreSQL (ได้ `DATABASE_URL` อัตโนมัติ)
3. service บอท → Variables: `DISCORD_BOT_TOKEN`, `OWNER_ID`, `GUILD_ID`, `ENCRYPTION_KEY`,
   `DATABASE_URL=${{Postgres.DATABASE_URL}}`, (`FARM_INTERVAL_MIN=120`)
4. Deploy → ดู log ให้เห็น `✓ database connected` + `🟢 online`
5. ในเซิร์ฟเวอร์ `/setup_panel` → ปักแผง → ทุกคนลงทะเบียน token
- บอทเป็น **worker** ไม่ต้อง expose port
- ⚠️ **อย่ารัน local + Railway พร้อมกันด้วย BOT token เดียวกัน** → 2 instance ชนกัน (farm ซ้ำ/คำสั่งตีกัน) + คนละ DB

---

## 12. ความเสี่ยง / ToS / ความปลอดภัย

- **ToS:** automate user account ผิดกฎ Discord → แบนถาวรได้ (เคยมี token `_xpex` โดน flag มาก่อน)
- **ปลอดภัยขึ้น:** เกมทำทีละอัน/บัญชี, scan ทุก 120 นาที (ไม่ถี่), token เข้ารหัสเก็บ
- **ความรับผิดชอบ custody:** เก็บ token เพื่อน = ถือ credential เข้าบัญชีเขาได้เต็ม — ถ้า DB + `ENCRYPTION_KEY` หลุดพร้อมกัน = ถอดได้ ต้องดูแล key ให้ดี
- `.env` gitignored, secret อยู่ใน Railway Variables เท่านั้น

---

## 13. Gotchas / บทเรียน (bug ที่เจอ + วิธีแก้)

| ปัญหา | สาเหตุ | วิธีแก้ |
|---|---|---|
| heartbeat 401/40001 | UA ไม่มี `Electron/<ver>` | ใส่ Electron ใน `_UA` (`quest_api.py`) |
| asyncpg `password auth failed user "chaho"` | `from db import` รันก่อน `load_dotenv()` → env ว่าง → fallback OS user | ย้าย `load_dotenv()` บนสุด + ให้ db อ่าน env แบบ lazy |
| asyncpg auth failed (รหัสผิด) | Postgres ตัวอื่นจอง 5432 อยู่ | ใช้ host port 5544 |
| `gather(return_exceptions=True)` กลืน error เงียบ | — | wrap ด้วย `_safe_scan()` ที่ log error |
| `UnicodeEncodeError` (cp1252) ตอน print | Windows console ไม่รองรับอิโมจิ/ไทย | `PYTHONIOENCODING=utf-8` |
| stream_key ผิด → ยัง 401/หาเควสไม่เจอ | ใช้ app_id/channel_id | ต้องเป็น `call:<quest_id>:1` |
| `discord.py 2.1` ของเก่า | requirements >=2.4 | upgrade เป็น 2.7.1 |
| ปุ่มหายหลัง restart | View ไม่ persistent | `timeout=None` + `custom_id` + `add_view()` |

---

## 14. อ้างอิง (references)

- Quest API spec: https://docs.discord.food/resources/quests
- aamiaa console script (desktop method): https://gist.github.com/aamiaa/204cd9d42013ded9faf646fae7f89fbb
- repo: https://github.com/Gaxia-XP/Discord-Auto-Quest (branch `main`)
- ของเดิม hybrid (desktop): `../discord-quest-bot/` (`desktop_quest_completer.js`, `HYBRID_SETUP.md`) — **ล้าสมัยแล้ว** เพราะ headless ทำเกมได้ตรงๆ

---

## 15. สถานะปัจจุบัน (2026-06-27)

- โค้ดล่าสุด push แล้ว: commit `0111bb4` บน `main` (ไล่: da10b14 multi-user → 885e560 bot status → 6c9fb94 user presence → 0111bb4 presence toggle)
- บอท: `Hermes-Agent#5154`, OWNER `829635762400002098`, GUILD `1425133452868718744`
- ลงทะเบียนแล้ว: **2 ผู้ใช้ (เจ้าของ + เพื่อน 1) / 4 token accounts**
- พิสูจน์แล้ว: เควสวิดีโอ ✅, เควสเกม headless ✅ (Where Winds Meet, GOALS ฯลฯ จบจริง)
- ฟีเจอร์ครบ: panel 7 ปุ่ม, leaderboard, สถานะสด (current+queue), bot rotating status, user presence + toggle รายคน
- **Deploy Railway แล้ว ✅** (user ยืนยัน) — Railway auto-redeploy ตอน push main
- DB local (dev): Docker `questbot-pg` (postgres:16) port 5544 — แยกจาก Railway Postgres

---

## 17. Multi-host + local (2026-09-18)

- `db.py` เลือก backend จาก `DATABASE_URL` อัตโนมัติ: `postgresql://` → asyncpg /
  ว่าง/`sqlite:` → SQLite (`SQLITE_PATH` หรือ `questbot.db`) — query เขียนแบบ `$n` แล้วแปลเป็น `?`
- upsert ใช้ `excluded.` (ได้ทั้ง 2 backend) — อย่าใช้ `$n` ซ้ำตำแหน่งเดิมใน query เดียว
- `bot.py`: `DATABASE_URL` ไม่บังคับแล้ว + `_ts()` รับ `completed_at` ได้ทั้ง datetime/str
- ไฟล์ใหม่: `Dockerfile` (default SQLite ใน `/data`), `docker-compose.yml` (bot+Postgres),
  `.dockerignore` · `requirements.txt` + `aiosqlite`
- ⚠️ ยังไม่ได้ verify บนเครื่องนี้: `docker build` + ต่อ Postgres จริง (Docker daemon ไม่ได้รัน)

---

## 16. งานต่อไป / ไอเดีย

- [x] ~~Deploy Railway + Postgres~~ — เสร็จแล้ว
- [ ] (option) ปุ่ม "Farm ทุกคนเดี๋ยวนี้" สำหรับ OWNER
- [ ] (option) `GAME_CONCURRENCY` ตั้งค่าได้ (ปัจจุบัน fix 1 เกม/บัญชี)
- [ ] (option) รองรับ PLAY_ACTIVITY / STREAM (ต้องมี voice/stream logic)
- [ ] (option) presence: custom GIF/assets (ต้อง resolve external-assets เป็น mp: เหมือนสคริปต์ presence เดิม)
- [ ] เฝ้าระวัง: ถ้า Discord ปิดช่อง Electron-UA เมื่อไหร่ เควสเกมจะ 401 อีก
```
