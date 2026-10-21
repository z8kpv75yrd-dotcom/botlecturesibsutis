import asyncio, csv, hashlib, hmac, io, json, logging, os, sqlite3, tempfile, time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qsl, quote
from zoneinfo import ZoneInfo

import aiohttp
import icalendar
import recurring_ical_events
from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (BotCommand, BufferedInputFile, FSInputFile, InlineKeyboardButton,
                           InlineKeyboardMarkup, MenuButtonWebApp, WebAppInfo)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("att")

TOKEN = os.environ["BOT_TOKEN"]
BASE_URL = os.environ["BASE_URL"].rstrip("/")
OWNERS = {int(x) for x in os.getenv("OWNER_IDS", "").replace(" ", "").split(",") if x}
TZ = ZoneInfo(os.getenv("TZ_NAME", "Asia/Novosibirsk"))
TPL = os.getenv("SCHEDULE_URL_TEMPLATE", "")
BEFORE = int(os.getenv("MARK_BEFORE_MIN", "15"))
AFTER = int(os.getenv("MARK_AFTER_MIN", "30"))
DATA = Path(os.getenv("DATA_DIR", "data"))
DATA.mkdir(parents=True, exist_ok=True)

# ---------------- БАЗА ----------------
db = sqlite3.connect(DATA / "attendance.db", check_same_thread=False)
db.row_factory = sqlite3.Row
db.executescript("""
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS grps(id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL, url TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS students(id INTEGER PRIMARY KEY,
  group_id INTEGER NOT NULL REFERENCES grps(id) ON DELETE CASCADE,
  name TEXT NOT NULL, tg_id INTEGER, UNIQUE(group_id, name));
CREATE TABLE IF NOT EXISTS lessons(id INTEGER PRIMARY KEY,
  group_id INTEGER NOT NULL REFERENCES grps(id) ON DELETE CASCADE,
  title TEXT NOT NULL, teacher TEXT DEFAULT '', room TEXT DEFAULT '',
  ts_start INTEGER NOT NULL, ts_end INTEGER NOT NULL, uid TEXT, UNIQUE(group_id, uid));
CREATE TABLE IF NOT EXISTS marks(
  lesson_id INTEGER REFERENCES lessons(id) ON DELETE CASCADE,
  student_id INTEGER REFERENCES students(id) ON DELETE CASCADE,
  status TEXT NOT NULL, ts INTEGER NOT NULL, PRIMARY KEY(lesson_id, student_id));
""")

def q(sql, a=()): return db.execute(sql, a).fetchall()
def q1(sql, a=()): return db.execute(sql, a).fetchone()
def ex(sql, a=()):
    c = db.execute(sql, a); db.commit(); return c

class Err(Exception): pass

# ---------------- АВТОРИЗАЦИЯ MINI APP ----------------
def verify(init: str):
    d = dict(parse_qsl(init, keep_blank_values=True))
    h = d.pop("hash", None)
    if not h or "user" not in d: return None
    s = "\n".join(f"{k}={v}" for k, v in sorted(d.items()))
    key = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    if not hmac.compare_digest(hmac.new(key, s.encode(), hashlib.sha256).hexdigest(), h): return None
    if time.time() - int(d.get("auth_date", 0)) > 86400: return None
    return json.loads(d["user"])

def is_owner(u): return u["id"] in OWNERS
def student(u): return q1("SELECT * FROM students WHERE tg_id=?", (u["id"],))
def fmt(ts): return datetime.fromtimestamp(ts, TZ).strftime("%H:%M")
def today(): return datetime.now(TZ).strftime("%Y-%m-%d")
def clean(s): return " ".join(str(s or "").split())

def day_bounds(ds):
    d = datetime.strptime(ds, "%Y-%m-%d").replace(tzinfo=TZ)
    return int(d.timestamp()), int((d + timedelta(days=1)).timestamp())

def to_ts(date, hm):
    return int(datetime.strptime(f"{date} {hm}", "%Y-%m-%d %H:%M").replace(tzinfo=TZ).timestamp())

# ---------------- API ----------------
def a_me(u, b):
    s = student(u)
    r = {"owner": is_owner(u), "today": today(), "tpl": bool(TPL), "before": BEFORE, "after": AFTER,
         "student": None, "groups": [dict(x) for x in q("SELECT id,name FROM grps ORDER BY name")]}
    if s:
        g = q1("SELECT name FROM grps WHERE id=?", (s["group_id"],))
        r["student"] = {"id": s["id"], "name": s["name"], "group": g["name"] if g else ""}
    return r

def a_free(u, b):
    return {"students": [dict(x) for x in q(
        "SELECT id,name FROM students WHERE group_id=? AND tg_id IS NULL ORDER BY name", (int(b["group_id"]),))]}

def a_register(u, b):
    if student(u): raise Err("Вы уже зарегистрированы")
    gid = int(b["group_id"])
    if b.get("student_id"):
        s = q1("SELECT * FROM students WHERE id=? AND group_id=?", (int(b["student_id"]), gid))
        if not s or s["tg_id"]: raise Err("Это имя уже занято")
        ex("UPDATE students SET tg_id=? WHERE id=?", (u["id"], s["id"]))
    else:
        name = clean(b.get("name"))
        if len(name) < 3: raise Err("Введите фамилию и имя")
        try: ex("INSERT INTO students(group_id,name,tg_id) VALUES(?,?,?)", (gid, name, u["id"]))
        except sqlite3.IntegrityError: raise Err("Такое имя уже есть в списке — выберите его из списка")
    return {}

def a_lessons(u, b):
    s = student(u)
    if is_owner(u) and b.get("group_id"): gid = int(b["group_id"])
    elif s: gid = s["group_id"]
    else: raise Err("Сначала зарегистрируйтесь")
    a, e = day_bounds(b["date"]); now = time.time(); out = []
    total = q1("SELECT COUNT(*) c FROM students WHERE group_id=?", (gid,))["c"]
    for l in q("SELECT * FROM lessons WHERE group_id=? AND ts_start>=? AND ts_start<? ORDER BY ts_start", (gid, a, e)):
        d = {"id": l["id"], "title": l["title"], "teacher": l["teacher"], "room": l["room"],
             "start": fmt(l["ts_start"]), "end": fmt(l["ts_end"])}
        if is_owner(u) and b.get("group_id"):
            c = {r["status"]: r["c"] for r in q("SELECT status,COUNT(*) c FROM marks WHERE lesson_id=? GROUP BY status", (l["id"],))}
            d.update(present=c.get("present", 0), absent=c.get("absent", 0), total=total)
        else:
            m = q1("SELECT status FROM marks WHERE lesson_id=? AND student_id=?", (l["id"], s["id"]))
            d["mark"] = m["status"] if m else None
            d["can_mark"] = l["ts_start"] - BEFORE * 60 <= now <= l["ts_end"] + AFTER * 60
        out.append(d)
    return {"lessons": out}

def a_mark(u, b):
    s = student(u)
    if not s: raise Err("Сначала зарегистрируйтесь")
    l = q1("SELECT * FROM lessons WHERE id=? AND group_id=?", (int(b["lesson_id"]), s["group_id"]))
    if not l: raise Err("Пара не найдена")
    if b["status"] not in ("present", "absent"): raise Err("Неверный статус")
    now = time.time()
    if not (l["ts_start"] - BEFORE * 60 <= now <= l["ts_end"] + AFTER * 60):
        raise Err("Сейчас отметка недоступна")
    ex("INSERT OR REPLACE INTO marks VALUES(?,?,?,?)", (l["id"], s["id"], b["status"], int(now)))
    return {}

# ---- владелец ----
def a_groups(u, b):
    out = []
    for g in q("SELECT * FROM grps ORDER BY name"):
        st = q("SELECT id,name,tg_id IS NOT NULL AS linked FROM students WHERE group_id=? ORDER BY name", (g["id"],))
        out.append({"id": g["id"], "name": g["name"], "url": g["url"], "students": [dict(x) for x in st]})
    return {"groups": out}

def a_group_add(u, b):
    name = clean(b.get("name"))
    if not name: raise Err("Введите название группы")
    try: ex("INSERT INTO grps(name,url) VALUES(?,?)", (name, clean(b.get("url"))))
    except sqlite3.IntegrityError: raise Err("Такая группа уже есть")
    return {}

def a_group_del(u, b):
    ex("DELETE FROM grps WHERE id=?", (int(b["group_id"]),)); return {}

def a_students_add(u, b):
    gid = int(b["group_id"]); n = 0
    for line in str(b.get("names", "")).splitlines():
        name = clean(line.lstrip("0123456789.)- \t"))
        if len(name) > 2 and ex("INSERT OR IGNORE INTO students(group_id,name) VALUES(?,?)", (gid, name)).rowcount: n += 1
    return {"added": n}

def a_student_del(u, b):
    ex("DELETE FROM students WHERE id=?", (int(b["student_id"]),)); return {}

def a_student_unlink(u, b):
    ex("UPDATE students SET tg_id=NULL WHERE id=?", (int(b["student_id"]),)); return {}

def a_lesson_add(u, b):
    title = clean(b.get("title"))
    if not title: raise Err("Введите название пары")
    ts, te = to_ts(b["date"], b["start"]), to_ts(b["date"], b["end"])
    if te <= ts: raise Err("Конец пары раньше начала")
    ex("INSERT INTO lessons(group_id,title,teacher,room,ts_start,ts_end) VALUES(?,?,?,?,?,?)",
       (int(b["group_id"]), title, clean(b.get("teacher")), clean(b.get("room")), ts, te))
    return {}

def a_lesson_del(u, b):
    ex("DELETE FROM lessons WHERE id=?", (int(b["lesson_id"]),)); return {}

def a_attendance(u, b):
    l = q1("SELECT * FROM lessons WHERE id=?", (int(b["lesson_id"]),))
    if not l: raise Err("Пара не найдена")
    rows = q("""SELECT s.id,s.name,s.tg_id IS NOT NULL AS linked,m.status,m.ts FROM students s
                LEFT JOIN marks m ON m.student_id=s.id AND m.lesson_id=? WHERE s.group_id=? ORDER BY s.name""",
             (l["id"], l["group_id"]))
    return {"lesson": {"id": l["id"], "title": l["title"], "start": fmt(l["ts_start"]), "end": fmt(l["ts_end"])},
            "students": [{"id": r["id"], "name": r["name"], "linked": bool(r["linked"]),
                          "status": r["status"], "time": fmt(r["ts"]) if r["ts"] else ""} for r in rows]}

def a_override(u, b):
    lid, sid, st = int(b["lesson_id"]), int(b["student_id"]), b["status"]
    if st == "none": ex("DELETE FROM marks WHERE lesson_id=? AND student_id=?", (lid, sid))
    elif st in ("present", "absent"): ex("INSERT OR REPLACE INTO marks VALUES(?,?,?,?)", (lid, sid, st, int(time.time())))
    else: raise Err("Неверный статус")
    return {}

async def a_import(u, b):
    g = q1("SELECT * FROM grps WHERE id=?", (int(b["group_id"]),))
    if not g: raise Err("Группа не найдена")
    url = clean(b.get("url")) or g["url"] or (TPL.format(group=quote(g["name"])) if TPL else "")
    if not url: raise Err("Укажите ссылку на расписание (ICS)")
    if clean(b.get("url")): ex("UPDATE grps SET url=? WHERE id=?", (url, g["id"]))
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as s:
            async with s.get(url, headers={"User-Agent": "Mozilla/5.0"}) as r:
                if r.status != 200: raise Err(f"Сайт ответил кодом {r.status}")
                raw = await r.read()
    except aiohttp.ClientError as e:
        raise Err(f"Не удалось открыть ссылку: {e}")
    try: cal = icalendar.Calendar.from_ical(raw)
    except Exception: raise Err("Ссылка не ведёт на календарь в формате ICS")
    t0 = datetime.now(TZ) - timedelta(days=7)
    n = 0
    for ev in recurring_ical_events.of(cal).between(t0, t0 + timedelta(days=150)):
        ds = ev.get("DTSTART").dt
        de = (ev.get("DTEND") or ev.get("DTSTART")).dt
        if not isinstance(ds, datetime): continue
        if ds.tzinfo is None: ds = ds.replace(tzinfo=TZ)
        if not isinstance(de, datetime): de = ds + timedelta(minutes=90)
        if de.tzinfo is None: de = de.replace(tzinfo=TZ)
        ts, te = int(ds.timestamp()), int(de.timestamp())
        desc = clean(str(ev.get("DESCRIPTION", "")))[:120]
        db.execute("""INSERT INTO lessons(group_id,title,teacher,room,ts_start,ts_end,uid) VALUES(?,?,?,?,?,?,?)
            ON CONFLICT(group_id,uid) DO UPDATE SET title=excluded.title,teacher=excluded.teacher,
            room=excluded.room,ts_start=excluded.ts_start,ts_end=excluded.ts_end""",
            (g["id"], clean(str(ev.get("SUMMARY", "Пара"))), desc, clean(str(ev.get("LOCATION", ""))),
             ts, te, f"{ev.get('UID')}|{ts}"))
        n += 1
    db.commit()
    return {"imported": n}

ROUTES = {  # имя: (функция, только_владелец)
    "me": (a_me, False), "free": (a_free, False), "register": (a_register, False),
    "lessons": (a_lessons, False), "mark": (a_mark, False),
    "groups": (a_groups, True), "group_add": (a_group_add, True), "group_del": (a_group_del, True),
    "students_add": (a_students_add, True), "student_del": (a_student_del, True),
    "student_unlink": (a_student_unlink, True), "lesson_add": (a_lesson_add, True),
    "lesson_del": (a_lesson_del, True), "attendance": (a_attendance, True),
    "override": (a_override, True), "import": (a_import, True),
}

async def api(request):
    u = verify(request.headers.get("X-Init-Data", ""))
    if not u: return web.json_response({"error": "Откройте приложение через Telegram"}, status=401)
    route = ROUTES.get(request.match_info["name"])
    if not route: return web.json_response({"error": "Не найдено"}, status=404)
    fn, owner_only = route
    if owner_only and not is_owner(u): return web.json_response({"error": "Только для преподавателя"}, status=403)
    try:
        b = await request.json() if request.can_read_body else {}
        r = fn(u, b)
        if asyncio.iscoroutine(r): r = await r
        return web.json_response(r)
    except Err as e:
        return web.json_response({"error": str(e)}, status=400)
    except (KeyError, ValueError, TypeError):
        return web.json_response({"error": "Неверные данные"}, status=400)
    except Exception:
        log.exception("api error"); return web.json_response({"error": "Ошибка сервера"}, status=500)

async def index(request):
    return web.FileResponse(Path(__file__).parent / "webapp" / "index.html", headers={"Cache-Control": "no-cache"})

# ---------------- БОТ ----------------
dp = Dispatcher()

@dp.message(CommandStart())
async def start(m):
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📋 Открыть журнал", web_app=WebAppInfo(url=BASE_URL))]])
    await m.answer("Привет! Здесь отмечается посещаемость пар. Нажмите кнопку ниже.", reply_markup=kb)

@dp.message(Command("id"))
async def my_id(m): await m.answer(f"Ваш Telegram ID: {m.from_user.id}")

@dp.message(Command("export"))
async def export(m, command: CommandObject):
    if m.from_user.id not in OWNERS: return
    g = q1("SELECT * FROM grps WHERE name=?", (clean(command.args),))
    if not g:
        names = ", ".join(x["name"] for x in q("SELECT name FROM grps")) or "групп пока нет"
        return await m.answer(f"Укажите группу: /export НАЗВАНИЕ\nГруппы: {names}")
    lessons = q("SELECT * FROM lessons WHERE group_id=? ORDER BY ts_start", (g["id"],))
    studs = q("SELECT * FROM students WHERE group_id=? ORDER BY name", (g["id"],))
    marks = {(r["lesson_id"], r["student_id"]): r["status"] for r in q(
        "SELECT mk.* FROM marks mk JOIN lessons l ON l.id=mk.lesson_id WHERE l.group_id=?", (g["id"],))}
    sym = {"present": "+", "absent": "н", None: ""}
    buf = io.StringIO(); w = csv.writer(buf, delimiter=";")
    w.writerow(["Студент"] + [f"{datetime.fromtimestamp(l['ts_start'], TZ):%d.%m %H:%M} {l['title']}" for l in lessons])
    for s in studs: w.writerow([s["name"]] + [sym[marks.get((l["id"], s["id"]))] for l in lessons])
    await m.answer_document(BufferedInputFile(buf.getvalue().encode("utf-8-sig"), filename=f"{g['name']}.csv"),
                            caption="+ был, н — не был, пусто — не отметился")

@dp.message(Command("backup"))
async def backup(m):
    if m.from_user.id not in OWNERS: return
    tmp = Path(tempfile.gettempdir()) / "backup.db"
    dst = sqlite3.connect(tmp); db.backup(dst); dst.close()
    await m.answer_document(FSInputFile(tmp, filename="attendance.db"))

async def main():
    if not OWNERS: log.warning("OWNER_IDS пуст — владельца нет!")
    bot = Bot(TOKEN)
    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_get("/healthz", lambda r: web.Response(text="ok"))
    app.router.add_post("/api/{name}", api)
    runner = web.AppRunner(app); await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", int(os.getenv("PORT", "8080"))).start()
    await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text="Журнал", web_app=WebAppInfo(url=BASE_URL)))
    await bot.set_my_commands([BotCommand(command="start", description="Открыть журнал"),
                               BotCommand(command="id", description="Мой Telegram ID")])
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
