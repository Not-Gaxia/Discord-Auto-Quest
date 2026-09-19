"""
Discord Quest Auto-Farmer — multi-user (deploy บน Railway ได้)

ผู้ใช้ลงทะเบียน token เองผ่านปุ่มในแผง embed → บอทเก็บลง Postgres (เข้ารหัส)
แล้ว farm เควสวิดีโอให้อัตโนมัติ 24/7 + จัดอันดับว่าใครทำเควสเยอะสุด

ENV (Railway → Variables):
  DISCORD_BOT_TOKEN   token ของบอท (Developer Portal)
  DATABASE_URL        connection string ของ Postgres (Railway ใส่ให้อัตโนมัติ)
  ENCRYPTION_KEY      Fernet key สำหรับเข้ารหัส token  (สร้างครั้งเดียว ห้ามเปลี่ยน)
  OWNER_ID            Discord id ของคุณ (ใช้สิทธิ์ /setup_panel)
  GUILD_ID            (optional) server id — ใส่แล้ว slash sync ทันที
"""
from __future__ import annotations

import os
import random
import time
import asyncio
import logging
from datetime import datetime, timezone

# โหลด .env ก่อน import อื่นที่อ่าน env (db.py อ่าน DATABASE_URL/ENCRYPTION_KEY)
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

import aiohttp
from aiohttp import web
import discord
from discord import app_commands
from discord.ext import commands, tasks

from quest_api import QuestAccount, Quest
from db import DB, decrypt
from presence import PresenceManager

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("questbot")

BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "")
OWNER_ID = int(os.getenv("OWNER_ID", "0") or 0)
GUILD_ID = int(os.getenv("GUILD_ID", "0") or 0)
FARM_INTERVAL = int(os.getenv("FARM_INTERVAL_MIN", "10") or 10)
ENABLE_PRESENCE = os.getenv("ENABLE_PRESENCE", "1").strip().lower() not in ("0", "false", "no", "off")

db = DB()
PRESENCE: PresenceManager | None = None   # ตั้งใน setup_hook ถ้า ENABLE_PRESENCE


# ════════════════════════════════════════════════════════════════
#  Bot
# ════════════════════════════════════════════════════════════════
class QuestBot(commands.Bot):
    def __init__(self) -> None:
        super().__init__(command_prefix="!", intents=discord.Intents.default())
        self.session: aiohttp.ClientSession | None = None
        self.farm_tasks: dict[tuple[int, str], asyncio.Task] = {}   # วิดีโอ (ต่อ quest)
        self.game_workers: dict[int, asyncio.Task] = {}             # เกม (ต่อ account)
        self.farm_state: dict[int, dict] = {}                       # สถานะสด: {current, queue}
        self._pres_i = 0                                            # index สลับสถานะ
        self._health_runner: web.AppRunner | None = None    # /healthz ให้โฮสต์เช็ค + กัน sleep

    async def setup_hook(self) -> None:
        self.session = aiohttp.ClientSession()
        await db.connect()
        log.info(f"✓ database connected ({db.mode})")
        await start_health_server(self)   # bind $PORT ก่อน sync command (โฮสต์อย่าง Koyeb เช็คตรงนี้)
        global PRESENCE
        if ENABLE_PRESENCE:
            PRESENCE = PresenceManager(self.session)
            log.info("✓ presence เปิด (โชว์เควสบนโปรไฟล์ user ระหว่าง farm)")
        self.add_view(PanelView())            # ปุ่มทำงานต่อได้แม้บอท restart
        if GUILD_ID:
            g = discord.Object(id=GUILD_ID)
            self.tree.copy_global_to(guild=g)
            await self.tree.sync(guild=g)
        else:
            await self.tree.sync()
        self.autofarm.start()
        self.refresh_panels.start()
        self.rotate_presence.start()

    async def close(self) -> None:
        for t in (*self.farm_tasks.values(), *self.game_workers.values()):
            t.cancel()
        if self._health_runner is not None:
            await self._health_runner.cleanup()
            self._health_runner = None
        if PRESENCE:
            await PRESENCE.close()
        if self.session:
            await self.session.close()
        await db.close()
        await super().close()

    # ── background auto-farm ─────────────────────────────────────
    @tasks.loop(minutes=FARM_INTERVAL)
    async def autofarm(self) -> None:
        rows = await db.all_active_accounts()
        log.info(f"autofarm: scanning {len(rows)} account(s)")
        # สแกนทุกบัญชีพร้อมกัน + แยกอิสระ (ตัวนึงค้าง/พังไม่ลามตัวอื่น)
        await asyncio.gather(*(_safe_scan(r) for r in rows))

    @autofarm.before_loop
    async def _before_autofarm(self) -> None:
        await self.wait_until_ready()

    # ── อัปเดตสถิติในแผงทุกนาที ───────────────────────────────
    @tasks.loop(seconds=60)
    async def refresh_panels(self) -> None:
        panels = await db.all_panels()
        if not panels:
            return
        embed = panel_embed(await db.global_stats(), farming_count())
        for p in panels:
            try:
                ch = self.get_channel(p["channel_id"]) or await self.fetch_channel(p["channel_id"])
                msg = await ch.fetch_message(p["message_id"])
                await msg.edit(embed=embed)
            except discord.NotFound:
                await db.remove_panel(p["message_id"])   # แผงถูกลบ → เอาออกจาก DB
            except Exception as e:
                log.warning(f"refresh panel: {e}")

    @refresh_panels.before_loop
    async def _before_refresh(self) -> None:
        await self.wait_until_ready()

    # ── สลับสถานะบอทให้ดูมีชีวิต (ดึงสถิติสด) ─────────────────
    @tasks.loop(seconds=30)
    async def rotate_presence(self) -> None:
        try:
            stats = await db.global_stats()
        except Exception:
            stats = None
        statuses = build_statuses(stats, farming_count())
        act, st = statuses[self._pres_i % len(statuses)]
        self._pres_i += 1
        try:
            await self.change_presence(activity=act, status=st)
        except Exception as e:
            log.warning(f"presence: {e}")

    @rotate_presence.before_loop
    async def _before_presence(self) -> None:
        await self.wait_until_ready()


bot = QuestBot()


# ── helpers ─────────────────────────────────────────────────────
async def dm(discord_user_id: int, text: str) -> None:
    try:
        u = bot.get_user(discord_user_id) or await bot.fetch_user(discord_user_id)
        await u.send(text)
    except Exception as e:
        log.warning(f"dm {discord_user_id}: {e}")


async def scan_account(row) -> None:
    """login + ดูเควส → farm วิดีโอ (parallel) + เกม (worker เดียวต่อ account ไล่ทีละอัน)"""
    acc = QuestAccount(decrypt(row["token_enc"]), bot.session)
    if not await acc.login():
        await db.mark_error(row["id"], "login ไม่ผ่าน (token หมดอายุ?)")
        await dm(row["discord_user_id"],
                 f"⚠️ token ของบัญชี **{row['username']}** ใช้ไม่ได้แล้ว — ถูกปิดใช้งาน "
                 f"กด 🗑️ ลบแล้ว ➕ เพิ่มใหม่นะ")
        return
    try:
        quests = await acc.list_quests()
    except Exception as e:
        log.warning(f"list {acc.username}: {e}")
        return

    for q in quests:
        if q.completed:
            await db.record_completion(row["id"], q.id, q.name)   # เก็บที่เคยทำไว้ด้วย

    # วิดีโอ — ทำพร้อมกันได้
    for q in quests:
        if q.completed or not q.headless_farmable:
            continue
        key = (row["id"], q.id)
        if key not in bot.farm_tasks:
            bot.farm_tasks[key] = asyncio.create_task(run_video(acc, q, row))

    # เกม — worker เดียวต่อ account ไล่ทำทีละอัน
    if any(q.game_farmable and not q.completed for q in quests):
        if row["id"] not in bot.game_workers:
            bot.game_workers[row["id"]] = asyncio.create_task(game_worker(acc, row))


async def _safe_scan(row) -> None:
    try:
        await scan_account(row)
    except Exception as e:
        log.error(f"scan {row['username']}: {e}")


async def _announce(acc: QuestAccount, q: Quest, row) -> None:
    await db.record_completion(row["id"], q.id, q.name)
    log.info(f"✅ {acc.username}: {q.name}")
    await update_completion_embed(row["discord_user_id"])


async def build_completion_embed(uid: int) -> discord.Embed:
    rows = await db.recent_completions(uid, 12)
    total = await db.user_completion_total(uid)
    e = discord.Embed(title="🎉 เควสที่ทำเสร็จแล้ว", color=0x57F287,
                      timestamp=discord.utils.utcnow())
    if not rows:
        e.description = "ยังไม่มีเควสเสร็จ"
        return e
    e.description = "\n".join(
        f"✅ **{r['quest_name']}** · `{r['username']}` · <t:{_ts(r['completed_at'])}:R>"
        for r in rows)
    e.set_footer(text=f"รวมทั้งหมด {total} เควส • อัปเดตล่าสุด")
    return e


async def update_completion_embed(uid: int) -> None:
    """แทนการ DM ทุกเควส → embed เดียวที่สะสมรายการ + bump DM ขึ้นบนแล้วลบให้คลีน"""
    try:
        user = bot.get_user(uid) or await bot.fetch_user(uid)
        dm_ch = user.dm_channel or await user.create_dm()
        embed = await build_completion_embed(uid)
        ref = await db.get_notify_msg(uid)
        msg = None
        if ref:
            try:
                msg = await dm_ch.fetch_message(ref["message_id"])
            except (discord.NotFound, discord.Forbidden):
                msg = None
        if msg:
            await msg.edit(embed=embed)
            try:   # bump: เด้ง DM ขึ้นบน + แจ้งเตือน แล้วลบให้เหลือแค่ embed
                bump = await dm_ch.send("🎉 เควสใหม่เสร็จแล้ว!")
                await bump.delete()
            except Exception:
                pass
        else:
            msg = await dm_ch.send(embed=embed)
            await db.set_notify_msg(uid, dm_ch.id, msg.id)
    except Exception as e:
        log.warning(f"completion embed {uid}: {e}")


async def run_video(acc: QuestAccount, q: Quest, row) -> None:
    try:
        if not q.enrolled:
            await acc.enroll(q.id)
        if await acc.farm_video(q):
            await _announce(acc, q, row)
    except Exception as e:
        log.error(f"video {acc.username}/{q.name}: {e}")
    finally:
        bot.farm_tasks.pop((row["id"], q.id), None)


async def game_worker(acc: QuestAccount, row) -> None:
    """ไล่ทำเควสเกมทีละอันจนหมด — อัปเดต farm_state (current+queue) + presence ระหว่างทำ"""
    aid = row["id"]
    try:
        while True:
            quests = await acc.list_quests()
            pending = [q for q in quests if q.game_farmable and not q.completed]
            if not pending:
                break
            q = pending[0]
            bot.farm_state[aid] = {
                "current": {"name": q.name, "done": q.progress, "target": q.target},
                "queue": [p.name for p in pending[1:]],
            }
            start_ms = int(time.time() * 1000)
            log.info(f"🎮 {acc.username}: เริ่มเควสเกม {q.name} ({q.progress}/{q.target}s)")
            pres_on = bool(PRESENCE) and await db.get_presence_pref(row["discord_user_id"])
            if pres_on:
                try:
                    await PRESENCE.set_quest(acc.token, q.name, q.app_id, q.progress, q.target, start_ms)
                except Exception as e:
                    log.debug(f"presence start: {e}")

            async def prog(done, target, _aid=aid, _name=q.name, _tok=acc.token,
                           _app=q.app_id, _s=start_ms, _on=pres_on):
                stt = bot.farm_state.get(_aid)
                if stt and stt.get("current"):
                    stt["current"]["done"] = done
                if done % 60 < 3:
                    log.info(f"🎮 {acc.username}: {_name} {done}/{target}s")
                if _on and PRESENCE:
                    try:
                        await PRESENCE.set_quest(_tok, _name, _app, done, target, _s)
                    except Exception:
                        pass

            try:
                if await acc.farm_game(q, on_progress=prog):
                    await _announce(acc, q, row)
                else:
                    log.warning(f"🎮 {acc.username}: {q.name} ไม่ครบ (หมดอายุ/stall) — หยุด")
                    break
            except Exception as e:
                log.error(f"game {acc.username}/{q.name}: {e}")
                break
    finally:
        bot.farm_state.pop(aid, None)
        if PRESENCE:
            try:
                await PRESENCE.clear(acc.token)
            except Exception:
                pass
        bot.game_workers.pop(aid, None)


def farming_count() -> int:
    """จำนวนเควสที่กำลัง farm อยู่ตอนนี้ (เกม + วิดีโอ)"""
    videos = len([t for t in bot.farm_tasks.values() if not t.done()])
    return len(bot.game_workers) + videos


def _progbar(pct: int, width: int = 10) -> str:
    filled = max(0, min(width, round(width * pct / 100)))
    return "█" * filled + "░" * (width - filled)


def create_health_app() -> web.Application:
    """แอปเล็กๆ ให้โฮสต์ (Koyeb/Render/Fly) health-check + ให้ตัว ping เลี้ยงกัน sleep"""
    async def healthz(_req: web.Request) -> web.Response:
        return web.Response(text="ok")
    app = web.Application()
    app.router.add_get("/healthz", healthz)
    return app


async def start_health_server(bot: QuestBot) -> int:
    """bind 0.0.0.0:$PORT (Koyeb ใส่ PORT ให้เอง, default 8000) → คืน port ที่ bind ได้"""
    port = int(os.getenv("PORT", "8000") or 8000)
    bot._health_runner = web.AppRunner(create_health_app())
    await bot._health_runner.setup()
    await web.TCPSite(bot._health_runner, "0.0.0.0", port).start()
    log.info(f"✓ health server :{port}/healthz")
    return port


def _ts(v) -> int:
    """completed_at เป็น datetime (Postgres) หรือ str (SQLite) → epoch วินาที"""
    if v is None:
        return 0
    if isinstance(v, (int, float)):
        return int(v)
    if hasattr(v, "timestamp"):
        try:
            return int(v.timestamp())
        except Exception:
            return 0
    s = str(v).strip()
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except Exception:
        return 0


def _vids_running(account_id: int) -> int:
    return sum(1 for (a, _), t in bot.farm_tasks.items() if a == account_id and not t.done())


# สถานะหมุนของบอท (BOT โชว์ได้แค่ type+name +url สำหรับ streaming — ไม่มี GIF/details/state)
_TWITCH = "https://www.twitch.tv/gaxiapx"


def build_statuses(stats, farming: int):
    q = stats["quests"] if stats else 0
    a = stats["accounts"] if stats else 0
    W, L, C = (discord.ActivityType.watching, discord.ActivityType.listening,
               discord.ActivityType.competing)
    online = discord.Status.online
    return [
        (discord.Streaming(name="🎮 farming quests 24/7", url=_TWITCH), online),
        (discord.Activity(type=W, name=f"{farming} เควสกำลัง farm 🔥"), online),
        (discord.Game(name="Discord Quests 🎯"), online),
        (discord.Activity(type=W, name=f"{a} บัญชีในระบบ 👥"), online),
        (discord.Activity(type=C, name="🏆 quest leaderboard"), online),
        (discord.Activity(type=L, name="heartbeat 💓 ทุก 90 วิ"), online),
        (discord.Game(name=f"farm ไปแล้ว {q} เควส 🚜"), online),
        (discord.Streaming(name="กด /setup_panel เริ่มเลย", url=_TWITCH), online),
    ]


def panel_embed(stats=None, farming: int = 0) -> discord.Embed:
    e = discord.Embed(
        title="🎯 Discord Quest Auto-Farmer",
        description="ลงทะเบียน token บัญชีคุณ แล้วบอท farm **เควสวิดีโอ + เกม** ให้อัตโนมัติ 24/7 🚀",
        color=0x5865F2,
        timestamp=discord.utils.utcnow())
    if stats:
        e.add_field(
            name="📊 สถิติรวม",
            value=(f"👥 ผู้ใช้ **{stats['users']}** คน · บัญชี **{stats['accounts']}**\n"
                   f"✅ ทำเควสสะสม **{stats['quests']}** ครั้ง\n"
                   f"🔄 กำลัง farm ตอนนี้ **{farming}** เควส"),
            inline=False)
    e.add_field(
        name="🕹️ ใช้ยังไง (ง่ายมาก)",
        value=("**1.** กด ➕ **เพิ่ม Token** → วาง token บัญชีคุณ\n"
               "**2.** จบ! บอท farm ให้เอง แล้ว DM ตอนเควสเสร็จ\n"
               "**3.** ดูความคืบหน้าที่ 📊 · แข่งใครเยอะสุดที่ 🏆\n"
               "_ไม่รู้จัก token? กดปุ่ม_ ❓ _วิธีใช้_"),
        inline=False)
    e.set_footer(text="🔐 token เก็บแบบเข้ารหัส · ⚠️ automate ผิด ToS เสี่ยงแบน · อัปเดตสด")
    return e


# ════════════════════════════════════════════════════════════════
#  UI — แผงปุ่ม (persistent) + modal + select ลบ
# ════════════════════════════════════════════════════════════════
class AddTokenModal(discord.ui.Modal, title="ลงทะเบียน Discord Token"):
    token = discord.ui.TextInput(
        label="Discord user token",
        placeholder="วาง token ของบัญชีที่จะให้ทำเควส",
        style=discord.TextStyle.short, required=True, max_length=120)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        tok = self.token.value.strip().strip('"').strip()
        acc = QuestAccount(tok, bot.session)
        if not await acc.login():
            return await interaction.followup.send(
                "❌ token ใช้ไม่ได้ — เช็คว่าก๊อปมาครบทั้งเส้น (ดูวิธีในแผง)", ephemeral=True)
        action = await db.add_account(
            discord_user_id=interaction.user.id, discord_name=str(interaction.user),
            token=tok, account_user_id=acc.user_id, username=acc.username)
        verb = "อัปเดต" if action == "updated" else "ลงทะเบียน"
        await interaction.followup.send(
            f"✅ {verb}บัญชี **{acc.username}** เรียบร้อย!\n"
            f"บอทจะ farm เควสวิดีโอให้อัตโนมัติ แล้ว DM มาบอกตอนเสร็จ 🎯", ephemeral=True)
        # เตะให้เริ่ม farm ทันที ไม่ต้องรอรอบถัดไป
        rows = await db.list_user_accounts(interaction.user.id)
        for r in rows:
            if r["account_user_id"] == acc.user_id:
                full = await db.all_active_accounts()
                match = next((x for x in full if x["id"] == r["id"]), None)
                if match:
                    asyncio.create_task(scan_account(match))
                break


class RemoveSelect(discord.ui.Select):
    def __init__(self, rows) -> None:
        opts = [discord.SelectOption(label=r["username"], value=str(r["id"]),
                                     description=f"id {r['account_user_id']}") for r in rows[:25]]
        super().__init__(placeholder="เลือกบัญชีที่จะลบ...", options=opts,
                         min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction) -> None:
        account_id = int(self.values[0])
        ok = await db.remove_account(account_id, interaction.user.id)
        for k in [k for k in bot.farm_tasks if k[0] == account_id]:
            bot.farm_tasks[k].cancel()
            bot.farm_tasks.pop(k, None)
        await interaction.response.edit_message(
            content="🗑️ ลบบัญชีออกแล้ว" if ok else "ลบไม่สำเร็จ (ไม่ใช่ token ของคุณ?)", view=None)


class RemoveView(discord.ui.View):
    def __init__(self, rows) -> None:
        super().__init__(timeout=120)
        self.add_item(RemoveSelect(rows))


class PanelView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)   # persistent

    @discord.ui.button(label="เพิ่ม Token", emoji="➕",
                       style=discord.ButtonStyle.success, custom_id="panel:add", row=0)
    async def add(self, interaction: discord.Interaction, _btn: discord.ui.Button):
        await interaction.response.send_modal(AddTokenModal())

    @discord.ui.button(label="สถานะของฉัน", emoji="📊",
                       style=discord.ButtonStyle.primary, custom_id="panel:status", row=0)
    async def status(self, interaction: discord.Interaction, _btn: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        rows = await db.list_user_accounts(interaction.user.id)
        if not rows:
            return await interaction.followup.send(
                "คุณยังไม่ได้ลงทะเบียน token เลย — กด ➕ **เพิ่ม Token** ก่อนนะ", ephemeral=True)
        e = discord.Embed(title="📊 สถานะบัญชีของคุณ", color=0x5865F2)
        for r in rows:
            cnt = await db.count_for_account(r["id"])
            state = "🟢" if r["active"] else f"🔴 {r['last_error'] or 'ปิดใช้'}"
            parts = [f"✅ ทำไปแล้ว **{cnt}** เควส"]
            st = bot.farm_state.get(r["id"])
            if st and st.get("current"):
                c = st["current"]
                pct = int(c["done"] * 100 / c["target"]) if c["target"] else 0
                parts.append(f"🚜 **กำลังทำ:** {c['name']}\n"
                             f"`{_progbar(pct)}` {c['done']}/{c['target']}s ({pct}%)")
                queue = st.get("queue") or []
                if queue:
                    shown = ", ".join(queue[:6])
                    extra = f" +{len(queue) - 6}" if len(queue) > 6 else ""
                    parts.append(f"⏳ **คิว ({len(queue)}):** {shown}{extra}")
            vids = _vids_running(r["id"])
            if vids:
                parts.append(f"📺 วิดีโอกำลังทำ: **{vids}**")
            if not (st and st.get("current")) and not vids:
                parts.append("💤 ไม่มีเควสค้าง")
            e.add_field(name=f"{state} {r['username']}", value="\n".join(parts), inline=False)
        pref = await db.get_presence_pref(interaction.user.id)
        e.set_footer(text=f"👁️ Presence โปรไฟล์: {'เปิด ✅' if pref else 'ปิด 🚫'} · กดปุ่ม 👁️ เพื่อสลับ")
        await interaction.followup.send(embed=e, ephemeral=True)

    @discord.ui.button(label="ลบ Token", emoji="🗑️",
                       style=discord.ButtonStyle.danger, custom_id="panel:remove", row=0)
    async def remove(self, interaction: discord.Interaction, _btn: discord.ui.Button):
        rows = await db.list_user_accounts(interaction.user.id)
        if not rows:
            return await interaction.response.send_message("ไม่มี token ให้ลบ", ephemeral=True)
        await interaction.response.send_message(
            "เลือก token ที่จะลบ:", view=RemoveView(rows), ephemeral=True)

    @discord.ui.button(label="อันดับ", emoji="🏆",
                       style=discord.ButtonStyle.secondary, custom_id="panel:lb", row=1)
    async def leaderboard(self, interaction: discord.Interaction, _btn: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        rows = await db.leaderboard(10)
        if not rows or all(r["total"] == 0 for r in rows):
            return await interaction.followup.send(
                "ยังไม่มีใครทำเควสเลย — ลงทะเบียนแล้วรอบอท farm นะ 😎", ephemeral=True)
        medals = ["🥇", "🥈", "🥉"] + [f"`#{i}`" for i in range(4, 11)]
        top = max(1, rows[0]["total"])
        lines = []
        for i, r in enumerate(rows):
            filled = round(10 * r["total"] / top)
            bar = "█" * filled + "░" * (10 - filled)
            lines.append(f"{medals[i]} **{r['name'] or r['discord_user_id']}** · "
                         f"{r['total']} เควส ({r['accounts']} บัญชี)\n`{bar}`")
        e = discord.Embed(title="🏆 อันดับนักฟาร์มเควส",
                          description="\n".join(lines), color=0xFEE75C)
        rank, total, people = await db.user_rank(interaction.user.id)
        stats = await db.global_stats()
        foot = f"🌍 รวม {stats['quests']} เควส · {people} คน"
        if rank:
            foot = f"อันดับคุณ #{rank} ({total} เควส)  ·  " + foot
        e.set_footer(text=foot)
        await interaction.followup.send(embed=e, ephemeral=True)

    @discord.ui.button(label="Farm ทันที", emoji="🚜",
                       style=discord.ButtonStyle.success, custom_id="panel:farmnow", row=1)
    async def farmnow(self, interaction: discord.Interaction, _btn: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        mine = [r for r in await db.all_active_accounts()
                if r["discord_user_id"] == interaction.user.id]
        if not mine:
            return await interaction.followup.send(
                "คุณยังไม่มี token ที่ active — กด ➕ ก่อนนะ", ephemeral=True)
        for r in mine:
            asyncio.create_task(_safe_scan(r))
        await interaction.followup.send(
            f"🚜 เริ่มสแกน + farm **{len(mine)}** บัญชีของคุณแล้ว! เควสไหนเสร็จ DM ไปบอกเลย",
            ephemeral=True)

    @discord.ui.button(label="วิธีใช้", emoji="❓",
                       style=discord.ButtonStyle.secondary, custom_id="panel:help", row=1)
    async def help_btn(self, interaction: discord.Interaction, _btn: discord.ui.Button):
        e = discord.Embed(
            title="❓ วิธีเอา Discord Token (ทำบนคอม)",
            color=0x5865F2,
            description=(
                "1️⃣ เปิด **discord.com/app** ในเบราว์เซอร์ → login บัญชีที่จะ farm\n"
                "2️⃣ กด `F12` → แท็บ **Network**\n"
                "3️⃣ ช่องกรองพิมพ์ `/api/v` → คลิกอะไรใน Discord สักที\n"
                "4️⃣ คลิก request ที่ขึ้นมา → เลื่อนหา **Request Headers**\n"
                "5️⃣ หาบรรทัด `authorization:` → ก๊อปค่ายาวๆ ข้างหลัง\n"
                "6️⃣ กลับมากด ➕ **เพิ่ม Token** → วาง → เสร็จ!\n\n"
                "🔐 token เก็บแบบเข้ารหัส · ⚠️ อย่าบอก token ใคร = เข้าบัญชีคุณได้เลย"))
        await interaction.response.send_message(embed=e, ephemeral=True)

    @discord.ui.button(label="Presence โปรไฟล์", emoji="👁️",
                       style=discord.ButtonStyle.secondary, custom_id="panel:presence", row=2)
    async def presence_toggle(self, interaction: discord.Interaction, _btn: discord.ui.Button):
        """เปิด/ปิด แสดงเควสบนโปรไฟล์ตัวเอง (รายคน)"""
        await interaction.response.defer(ephemeral=True, thinking=True)
        uid = interaction.user.id
        new = not await db.get_presence_pref(uid)
        await db.set_presence_pref(uid, new)
        if not new and PRESENCE:   # ปิด → เคลียร์ presence บัญชีของคนนี้ทันที
            for r in await db.all_active_accounts():
                if r["discord_user_id"] == uid:
                    try:
                        await PRESENCE.clear(decrypt(r["token_enc"]))
                    except Exception:
                        pass
        note = "" if PRESENCE else "\n_(ระบบ presence ปิดทั้งระบบอยู่ — `ENABLE_PRESENCE=0`)_"
        if new:
            await interaction.followup.send(
                "👁️ **เปิด** presence แล้ว — โปรไฟล์บัญชีคุณจะโชว์เควสที่กำลังทำ + progress "
                "(เริ่มในรอบ farm ถัดไป)\n⚠️ บัญชีจะออนไลน์ broadcast = เสี่ยงแบนขึ้น" + note,
                ephemeral=True)
        else:
            await interaction.followup.send(
                "🚫 **ปิด** presence แล้ว — โปรไฟล์จะไม่โชว์เควส (farm เงียบๆ เหมือนเดิม)" + note,
                ephemeral=True)


# ════════════════════════════════════════════════════════════════
#  Slash command (เจ้าของบอท)
# ════════════════════════════════════════════════════════════════
@bot.tree.command(description="ปักแผงควบคุมในห้องนี้ (เจ้าของบอทเท่านั้น)")
async def setup_panel(interaction: discord.Interaction):
    if OWNER_ID and interaction.user.id != OWNER_ID:
        return await interaction.response.send_message("เฉพาะเจ้าของบอทเท่านั้น", ephemeral=True)
    msg = await interaction.channel.send(
        embed=panel_embed(await db.global_stats(), farming_count()), view=PanelView())
    await db.add_panel(interaction.channel.id, msg.id)
    await interaction.response.send_message(
        "✅ ปักแผงแล้ว — ปักหมุด (pin) ไว้ได้เลย แผงจะอัปเดตสถิติเองทุกนาที 🔄", ephemeral=True)


@bot.event
async def on_ready():
    log.info(f"🟢 online: {bot.user} | farm ทุก {FARM_INTERVAL} นาที")


if __name__ == "__main__":
    # DATABASE_URL ไม่บังคับ — ไม่ตั้ง = SQLite ไฟล์ questbot.db (local/โฮสต์เล็ก)
    missing = [k for k in ("DISCORD_BOT_TOKEN", "ENCRYPTION_KEY")
               if not os.getenv(k)]
    if missing:
        raise SystemExit(f"ต้องตั้ง ENV ก่อน: {', '.join(missing)}")
    bot.run(BOT_TOKEN)
