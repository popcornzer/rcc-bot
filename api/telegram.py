"""RCC Telegram bot — พิมพ์ข้อความแล้วบันทึกลง Neon (Vercel serverless webhook)

Environment variables:
  DATABASE_URL, TELEGRAM_BOT_TOKEN, TELEGRAM_WEBHOOK_SECRET, TELEGRAM_ALLOWED_IDS
  (ไม่บังคับ) DEFAULT_PERSON=Pop  SPLIT_PEOPLE=Pop,Tim
"""
import hmac
import json
import os
import re
import urllib.request
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler

import psycopg2
import psycopg2.extras

TZ = timezone(timedelta(hours=7))                      # เวลาไทย (Vercel รันเป็น UTC)
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
TH_M = ["ม.ค.", "ก.พ.", "มี.ค.", "เม.ย.", "พ.ค.", "มิ.ย.", "ก.ค.", "ส.ค.", "ก.ย.", "ต.ค.", "พ.ย.", "ธ.ค."]
NAMES = ["Pop", "Tim", "Mom", "k-bank", "EXAT"]
NUM = re.compile(r"^\d{1,3}(?:,\d{3})+(?:\.\d+)?$|^\d+(?:\.\d+)?$")
MONTH_ALIAS = {m.lower(): i + 1 for i, m in enumerate(MONTHS)}
MONTH_ALIAS.update({"มค": 1, "กพ": 2, "มีค": 3, "เมย": 4, "พค": 5, "มิย": 6,
                    "กค": 7, "สค": 8, "กย": 9, "ตค": 10, "พย": 11, "ธค": 12})

HELP = """🧾 พิมพ์ข้อความเพื่อบันทึกรายจ่าย
รูปแบบ: รายการ จำนวนเงิน บัตร [ผู้จ่าย]

ตัวอย่าง
• tops 450 kfc
• ข้าว 120 uob world pop
• sushiro 880 kfc pop tim  → แบ่งจ่ายเท่ากัน
• grab 150 uob one แบ่ง  → แบ่ง Pop/Tim
• birken 1792 scb tim ผ่อน 1/4
• กาแฟ 54 uob one เมื่อวาน  (หรือ 25/10)
• ค่าห้อง 1500 pa งวด dec

/summary สรุปงวดล่าสุด
/undo ยกเลิกรายการที่เพิ่งบันทึก"""


# ---------------- ตัวช่วยคำนวณ ----------------
def norm(s):
    return re.sub(r"[\s\-_.]+", "", s.lower())


def money(x):
    return f"฿{x:,.2f}"


def plabel(p):
    y, m = p.split("-")
    return f"{MONTHS[int(m) - 1]} {y}"


def period_for(d, bill_day, fallback):
    """หลังวันตัดบัตร -> งวดเดือนถัดไป"""
    if not bill_day:
        return fallback
    y, m = d.year, d.month
    if d.day > bill_day:
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return f"{y}-{m:02d}"


def split_amounts(amount, n):
    base = round(amount / n, 2)
    return [base] * (n - 1) + [round(amount - base * (n - 1), 2)]


def month_of(tok):
    key = norm(tok)
    if key.isdigit() and 1 <= int(key) <= 12:
        return int(key)
    return MONTH_ALIAS.get(key)


def pick_period(month, today):
    """เลือกปีที่ทำให้งวดใกล้วันนี้ที่สุด"""
    now = today.year * 12 + today.month
    y = min((today.year - 1, today.year, today.year + 1), key=lambda yy: abs(yy * 12 + month - now))
    return f"{y}-{month:02d}"


# ---------------- แปลงข้อความ -> รายการ ----------------
def parse_message(text, cards, bill_days, today, default_person="Pop", split_people=("Pop", "Tim")):
    """คืน (dict, None) หรือ (None, ข้อความ error)"""
    toks = text.replace("฿", " ").replace("บาท", " ").split()
    used = [False] * len(toks)
    card_map = {norm(c): c for c in cards}
    person_map = {norm(p): p for p in NAMES}
    d, period_month, inst_no, inst_total, split = today, None, None, None, False
    card, persons = "", []

    for i, t in enumerate(toks):                       # ผ่อน n/m  (หรือ ผ่อน3/10)
        if used[i]:
            continue
        m = re.fullmatch(r"ผ่อน(\d+)(?:/(\d+))?", t)
        if m:
            inst_no, inst_total = int(m[1]), (int(m[2]) if m[2] else None)
            used[i] = True
        elif t == "ผ่อน" and i + 1 < len(toks):
            m = re.fullmatch(r"(\d+)(?:/(\d+))?", toks[i + 1])
            if m:
                inst_no, inst_total = int(m[1]), (int(m[2]) if m[2] else None)
                used[i] = used[i + 1] = True
    if inst_no and inst_total and inst_no > inst_total:
        return None, "งวดผ่อนต้องไม่มากกว่าจำนวนงวดทั้งหมด"

    for i, t in enumerate(toks):                       # งวด <เดือน>
        if not used[i] and t == "งวด" and i + 1 < len(toks) and month_of(toks[i + 1]):
            period_month = month_of(toks[i + 1])
            used[i] = used[i + 1] = True

    for i, t in enumerate(toks):                       # วันที่ / แบ่งจ่าย
        if used[i]:
            continue
        low = t.lower()
        if low in ("เมื่อวาน", "วานนี้", "yesterday"):
            d, used[i] = today - timedelta(days=1), True
        elif low in ("วันนี้", "today"):
            used[i] = True
        elif low in ("แบ่ง", "หาร", "split"):
            split, used[i] = True, True
        else:
            m = re.fullmatch(r"(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?", t)
            if m:
                yy = int(m[3]) if m[3] else today.year
                yy = yy + 2000 if yy < 100 else yy
                if yy > 2400:
                    yy -= 543                              # พ.ศ. -> ค.ศ.
                try:
                    d = date(yy, int(m[2]), int(m[1]))
                except ValueError:
                    return None, f"วันที่ไม่ถูกต้อง: {t}"
                if not m[3] and d > today + timedelta(days=31):
                    d = date(yy - 1, d.month, d.day)
                used[i] = True

    for n in (3, 2, 1):                                # ชื่อบัตร (ยาวสุดก่อน เช่น "uob world")
        if card:
            break
        for i in range(len(toks) - n + 1):
            if any(used[i:i + n]):
                continue
            key = norm("".join(toks[i:i + n]))
            if key in card_map:
                card = card_map[key]
                for j in range(i, i + n):
                    used[j] = True
                break

    for i, t in enumerate(toks):                       # ผู้จ่าย (pop, pop+tim, pop/tim)
        if used[i]:
            continue
        parts = re.split(r"[+/,&]", t)
        if all(p and norm(p) in person_map for p in parts):
            for p in parts:
                name = person_map[norm(p)]
                if name not in persons:
                    persons.append(name)
            used[i] = True

    idx = [i for i, t in enumerate(toks) if not used[i] and NUM.match(t)]
    if not idx:
        return None, "ไม่พบจำนวนเงิน"
    amount = float(toks[idx[-1]].replace(",", ""))     # ตัวเลขท้ายสุดคือยอดเงิน
    used[idx[-1]] = True
    if amount <= 0:
        return None, "จำนวนเงินต้องมากกว่า 0"
    item = " ".join(t for i, t in enumerate(toks) if not used[i]).strip()
    if not item:
        return None, "ไม่พบชื่อรายการ"

    if split and len(persons) < 2:
        persons = list(split_people)
    if not persons:
        persons = [default_person]
    fallback = f"{today.year}-{today.month:02d}"
    period = pick_period(period_month, today) if period_month else period_for(d, bill_days.get(card), fallback)
    return dict(amount=amount, item=item, card=card, persons=persons, date=d, period=period,
                inst_no=inst_no, inst_total=inst_total, period_forced=bool(period_month)), None


# ---------------- ฐานข้อมูล ----------------
_ready = False


def init_db(cur):
    global _ready
    if _ready:
        return
    cur.execute("CREATE TABLE IF NOT EXISTS tg_updates (update_id BIGINT PRIMARY KEY, "
                "created_at TIMESTAMPTZ DEFAULT now())")
    cur.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
    cur.execute("CREATE TABLE IF NOT EXISTS period_salary (period TEXT PRIMARY KEY, salary NUMERIC NOT NULL)")
    cur.execute("ALTER TABLE expense_types ADD COLUMN IF NOT EXISTS bill_day INT")
    _ready = True


def save_expense(cur, chat_id, p):
    amt = p["amount"]

    def row(t, n, a):
        return (p["period"], p["date"], p["item"], t, n, a, "", p["inst_no"], p["inst_total"], False)

    rows = []
    if len(p["persons"]) >= 2:                          # แถวยอดรวมบัตร + แถวแบ่งตามคน (เหมือนในแอป)
        if p["card"]:
            rows.append(row(p["card"], "", amt))
        rows += [row("", n, a) for n, a in zip(p["persons"], split_amounts(amt, len(p["persons"])))]
    else:
        rows.append(row(p["card"], p["persons"][0], amt))
    ids = []
    for r in rows:
        cur.execute("INSERT INTO expenses (period,date,item,type,name,amount,description,inst_no,inst_total,"
                    "recurring) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id", r)
        ids.append(cur.fetchone()["id"])
    cur.execute("INSERT INTO settings (key,value) VALUES (%s,%s) "
                "ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value", (f"tg_last_{chat_id}", json.dumps(ids)))
    return ids


def saved_text(p):
    lines = [f"✅ บันทึกแล้ว → งวด {plabel(p['period'])}", f"{p['item']}  {money(p['amount'])}"]
    meta = [f"บัตร {p['card']}" if p["card"] else "ไม่ระบุบัตร"]
    if len(p["persons"]) >= 2:
        shares = split_amounts(p["amount"], len(p["persons"]))
        meta.append("แบ่ง " + " · ".join(f"{n} {money(a)}" for n, a in zip(p["persons"], shares)))
    else:
        meta.append(p["persons"][0])
    meta.append(f"{p['date'].day} {TH_M[p['date'].month - 1]}")
    if p["inst_no"]:
        meta.append(f"ผ่อน {p['inst_no']}/{p['inst_total']}" if p["inst_total"] else f"ผ่อน #{p['inst_no']}")
    lines.append(" · ".join(meta))
    if not p["card"]:
        lines.append("⚠️ ไม่ได้ระบุบัตร จึงนับงวดตามเดือนปัจจุบัน")
    return "\n".join(lines)


def summary_text(cur, owner):
    cur.execute("SELECT period FROM expenses ORDER BY id DESC LIMIT 1")
    r = cur.fetchone()
    if not r:
        return "ยังไม่มีข้อมูล"
    period = r["period"]
    cur.execute("SELECT COALESCE(SUM(amount) FILTER (WHERE name=%s),0)::float AS pop, "
                "COALESCE(SUM(amount) FILTER (WHERE type NOT IN ('','-')),0)::float AS card, "
                "COALESCE(SUM(amount) FILTER (WHERE name NOT IN ('','-')),0)::float AS person "
                "FROM expenses WHERE period=%s", (owner, period))
    s = cur.fetchone()
    cur.execute("SELECT salary::float AS s FROM period_salary WHERE period<=%s ORDER BY period DESC LIMIT 1", (period,))
    sal = cur.fetchone()
    out = [f"📊 งวด {plabel(period)}"]
    if sal:
        out.append(f"💰 เงินเดือนคงเหลือ {money(sal['s'] - s['pop'])} (ฐาน ฿{sal['s']:,.0f})")
    out += [f"{owner} {money(s['pop'])}", f"รวมทุกบัตร {money(s['card'])} · รวมทุกคน {money(s['person'])}"]
    return "\n".join(out)


def undo_ids(cur, ids):
    cur.execute("DELETE FROM expenses WHERE id = ANY(%s) RETURNING id", (list(ids),))
    return len(cur.fetchall())


# ---------------- ประมวลผล update ----------------
def allowed_ids():
    return {int(x) for x in re.findall(r"\d+", os.environ.get("TELEGRAM_ALLOWED_IDS", ""))}


def handle_update(upd, cur, today=None):
    """คืนรายการคำสั่งที่ต้องส่งไป Telegram หลัง commit: [(method, payload), ...]"""
    cur.execute("INSERT INTO tg_updates (update_id) VALUES (%s) ON CONFLICT DO NOTHING RETURNING update_id",
                (upd.get("update_id"),))
    if not cur.fetchone():
        return []                                       # update ซ้ำ (Telegram ส่งซ้ำ) ข้าม
    cur.execute("DELETE FROM tg_updates WHERE created_at < now() - interval '7 days'")
    allowed = allowed_ids()
    owner = os.environ.get("DEFAULT_PERSON", "Pop")

    cb = upd.get("callback_query")
    if cb:
        if cb["from"]["id"] not in allowed:
            return [("answerCallbackQuery", {"callback_query_id": cb["id"], "text": "ไม่มีสิทธิ์"})]
        if str(cb.get("data", "")).startswith("u:"):
            ids = [int(x) for x in re.findall(r"\d+", cb["data"])]
            n = undo_ids(cur, ids)
            msg = cb.get("message") or {}
            acts = [("answerCallbackQuery", {"callback_query_id": cb["id"], "text": "ยกเลิกแล้ว"})]
            if msg:
                acts.append(("editMessageText", {"chat_id": msg["chat"]["id"], "message_id": msg["message_id"],
                                                 "text": f"↩️ ยกเลิกรายการแล้ว ({n} แถว)"}))
            return acts
        return [("answerCallbackQuery", {"callback_query_id": cb["id"]})]

    msg = upd.get("message")
    if not msg or "text" not in msg:
        return []
    chat_id, uid = msg["chat"]["id"], (msg.get("from") or {}).get("id")

    def say(text, markup=None):
        payload = {"chat_id": chat_id, "text": text}
        if markup:
            payload["reply_markup"] = markup
        return [("sendMessage", payload)]

    if uid not in allowed:
        return say(f"⛔ ไม่มีสิทธิ์ใช้งานบอทนี้\nID ของคุณ: {uid}\n(เจ้าของนำเลขนี้ไปใส่ใน TELEGRAM_ALLOWED_IDS)")

    text = msg["text"].strip()
    if text.startswith("/"):
        cmd = text.split()[0].split("@")[0].lower()
        if cmd in ("/start", "/help"):
            return say(HELP)
        if cmd == "/summary":
            return say(summary_text(cur, owner))
        if cmd == "/undo":
            key = f"tg_last_{chat_id}"
            cur.execute("SELECT value FROM settings WHERE key=%s", (key,))
            r = cur.fetchone()
            if not r:
                return say("ไม่มีรายการให้ยกเลิก")
            n = undo_ids(cur, json.loads(r["value"]))
            cur.execute("DELETE FROM settings WHERE key=%s", (key,))
            return say(f"↩️ ยกเลิกรายการล่าสุดแล้ว ({n} แถว)")
        return say("ไม่รู้จักคำสั่งนี้ ลอง /help")

    cur.execute("SELECT name, bill_day FROM expense_types ORDER BY id")
    ref = cur.fetchall()
    cards = [r["name"] for r in ref if r["name"]]
    bill_days = {r["name"]: r["bill_day"] for r in ref if r["bill_day"]}
    split_people = [x.strip() for x in os.environ.get("SPLIT_PEOPLE", "Pop,Tim").split(",") if x.strip()]
    today = today or datetime.now(TZ).date()
    p, err = parse_message(text, cards, bill_days, today, owner, split_people)
    if err:
        return say(f"❓ {err}\n\nตัวอย่าง: tops 450 kfc pop\nพิมพ์ /help ดูวิธีใช้ทั้งหมด")
    ids = save_expense(cur, chat_id, p)
    return say(saved_text(p), {"inline_keyboard": [[{"text": "↩️ ยกเลิกรายการนี้",
                                                      "callback_data": "u:" + ",".join(map(str, ids))}]]})


# ---------------- Vercel handler ----------------
def tg(method, **payload):
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{os.environ['TELEGRAM_BOT_TOKEN']}/{method}",
        data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=8) as r:
        return json.loads(r.read())


class handler(BaseHTTPRequestHandler):
    def _send(self, code, body=b"ok"):
        self.send_response(code)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._send(200)

    def do_POST(self):
        secret = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "")
        got = self.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if not secret or not hmac.compare_digest(got.encode(), secret.encode()):
            return self._send(401, b"unauthorized")
        try:
            n = int(self.headers.get("Content-Length") or 0)
            upd = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return self._send(400, b"bad request")

        actions = []
        try:
            conn = psycopg2.connect(os.environ["DATABASE_URL"])
            try:
                with conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    init_db(cur)
                    actions = handle_update(upd, cur)
            finally:
                conn.close()
        except Exception as e:                            # ตอบ 200 เสมอ กัน Telegram ส่งซ้ำ
            print("ERROR", repr(e))
            m = upd.get("message") or {}
            if (m.get("from") or {}).get("id") in allowed_ids():
                actions = [("sendMessage", {"chat_id": m["chat"]["id"],
                                            "text": "⚠️ บันทึกไม่สำเร็จ (ระบบขัดข้อง) ลองส่งใหม่อีกครั้ง"})]
        for method, payload in actions:
            try:
                tg(method, **payload)
            except Exception as e:
                print("TG ERROR", repr(e))
        self._send(200)
