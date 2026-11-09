#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
EDU BOT — o'quvchi, talaba va o'qituvchilar uchun Telegram bot (bitta fayl)

Ishga tushirish (Termux):
    export BOT_TOKEN="..."      # yoki .env fayl:  BOT_TOKEN=...
    export GEMINI_KEY="..."
    export ADMIN_ID="8824016205"
    python edu_bot.py

Faqat standart kutubxona kerak. Ixtiyoriy: pip install python-pptx  (.pptx slayd uchun)
"""
import os, sys, json, time, re, sqlite3, threading, random, html, io, zipfile, uuid
import base64, traceback, urllib.request, urllib.error
from concurrent.futures import ThreadPoolExecutor
from datetime import date

# ───────────────────────── SOZLAMALAR ─────────────────────────
HERE = os.path.dirname(os.path.abspath(__file__))
def _load_env():
    p = os.path.join(HERE, ".env")
    if os.path.exists(p):
        for ln in open(p, encoding="utf-8"):
            ln = ln.strip()
            if ln and not ln.startswith("#") and "=" in ln:
                k, v = ln.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
_load_env()
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8657849879:AAEqTyxQBQPnI4uulIGuyvSP_44r11lMyzw")
GEMINI_KEY = os.environ.get("GEMINI_KEY", "AQ.Ab8RN6JcsmzqO6GA0LwSqNVnb8apbm6gvNzWXWSrNkf6Hhn6zw")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "8824016205"))
DB_PATH = os.environ.get("DB_PATH", os.path.join(HERE, "edu_bot.db"))
DEFAULT_MODEL = "gemini-3.8-flash"
DEFAULT_PRICE = 3000
FREE_USES = 3

# ───────────────────────── BAZA ─────────────────────────
_lock = threading.RLock()
_conn = sqlite3.connect(DB_PATH, check_same_thread=False)
_conn.row_factory = sqlite3.Row

def q(sql, args=(), one=False):
    with _lock:
        cur = _conn.execute(sql, args)
        _conn.commit()
        rows = cur.fetchall() if cur.description else []
        return (rows[0] if rows else None) if one else rows

def init_db():
    q("""CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, name TEXT, username TEXT,
         free_left INTEGER, balance INTEGER DEFAULT 0, banned INTEGER DEFAULT 0, created INTEGER)""")
    q("CREATE TABLE IF NOT EXISTS settings(k TEXT PRIMARY KEY, v TEXT)")
    q("CREATE TABLE IF NOT EXISTS services(key TEXT PRIMARY KEY, price INTEGER, enabled INTEGER DEFAULT 1)")
    q("CREATE TABLE IF NOT EXISTS usage(id INTEGER PRIMARY KEY AUTOINCREMENT, uid INTEGER, key TEXT, ts INTEGER, paid INTEGER)")
    q("""CREATE TABLE IF NOT EXISTS payments(id INTEGER PRIMARY KEY AUTOINCREMENT, uid INTEGER,
         amount INTEGER, status TEXT, ts INTEGER)""")
    dp = int(get_set("default_price", DEFAULT_PRICE))
    for s in SERVICES:
        price = 0 if s[4] == "mathblitz" else dp
        q("INSERT OR IGNORE INTO services(key,price,enabled) VALUES(?,?,1)", (s[0], price))

def get_set(k, default=""):
    r = q("SELECT v FROM settings WHERE k=?", (k,), one=True)
    return r["v"] if r else default

def set_set(k, v):
    q("INSERT INTO settings(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, str(v)))

def ensure_user(uid, name, username):
    r = q("SELECT * FROM users WHERE id=?", (uid,), one=True)
    if not r:
        q("INSERT INTO users(id,name,username,free_left,balance,banned,created) VALUES(?,?,?,?,0,0,?)",
          (uid, name, username or "", int(get_set("free_uses", FREE_USES)), int(time.time())))
        r = q("SELECT * FROM users WHERE id=?", (uid,), one=True)
    return r

def get_user(uid):
    return q("SELECT * FROM users WHERE id=?", (uid,), one=True)

# ───────────────────────── TELEGRAM API ─────────────────────────
def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)

def tg(method, **p):
    req = urllib.request.Request(f"https://api.telegram.org/bot{BOT_TOKEN}/{method}",
                                 json.dumps(p).encode(), {"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=65) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        try:
            return json.load(e)
        except Exception:
            return {"ok": False}
    except Exception as e:
        log("tg error", method, e)
        return {"ok": False}

def esc(t):
    return html.escape(str(t), quote=False)

def chunks(t, n=3900):
    t = t or ""
    out = []
    while len(t) > n:
        cut = t.rfind("\n", 0, n)
        if cut < n // 2:
            cut = n
        out.append(t[:cut])
        t = t[cut:].lstrip("\n")
    out.append(t)
    return [c for c in out if c.strip()] or ["…"]

def send(chat, text, kb=None, plain=False):
    mid = None
    parts = chunks(text)
    for i, c in enumerate(parts):
        p = {"chat_id": chat, "text": c, "disable_web_page_preview": True}
        if not plain:
            p["parse_mode"] = "HTML"
        if kb and i == len(parts) - 1:
            p["reply_markup"] = kb
        r = tg("sendMessage", **p)
        if r.get("ok"):
            mid = r["result"]["message_id"]
    return mid

def edit(chat, mid, text, kb=None):
    p = {"chat_id": chat, "message_id": mid, "text": text[:4000], "parse_mode": "HTML",
         "disable_web_page_preview": True}
    if kb:
        p["reply_markup"] = kb
    return tg("editMessageText", **p)

def ik(rows):
    return {"inline_keyboard": rows}

def btn(text, data):
    return {"text": text, "callback_data": data}

def send_doc(chat, filename, data, caption=""):
    b = uuid.uuid4().hex
    body = []
    def field(n, v):
        body.append(f'--{b}\r\nContent-Disposition: form-data; name="{n}"\r\n\r\n{v}\r\n'.encode())
    field("chat_id", chat)
    if caption:
        field("caption", caption[:1000])
    body.append((f'--{b}\r\nContent-Disposition: form-data; name="document"; filename="{filename}"\r\n'
                 f'Content-Type: application/octet-stream\r\n\r\n').encode() + data + b"\r\n")
    body.append(f"--{b}--\r\n".encode())
    req = urllib.request.Request(f"https://api.telegram.org/bot{BOT_TOKEN}/sendDocument", b"".join(body),
                                 {"Content-Type": f"multipart/form-data; boundary={b}"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.load(r)
    except Exception as e:
        log("send_doc error", e)
        return {"ok": False}

def tg_file(file_id):
    r = tg("getFile", file_id=file_id)
    path = r["result"]["file_path"]
    with urllib.request.urlopen(f"https://api.telegram.org/file/bot{BOT_TOKEN}/{path}", timeout=90) as f:
        return f.read(), path

def slug(t):
    t = re.sub(r"[^a-zA-Z0-9]+", "_", t).strip("_")
    return (t or "file")[:40]

# ───────────────────────── GEMINI ─────────────────────────
SYS = ("Sen O'zbekistondagi o'quvchilar, talabalar va o'qituvchilar uchun mo'ljallangan professional "
       "ta'lim yordamchisisan. Aniq, xatosiz, tuzilgan va foydali javob ber. Foydalanuvchi boshqa til "
       "so'ramasa, o'zbek tilida (lotin yozuvi) yoz. Faktlarni to'qima; ishonching komil bo'lmasa, buni ayt.")

class AIError(Exception):
    pass

def gemini(prompt, json_mode=False, extra_parts=None):
    key = get_set("gemini_key") or GEMINI_KEY
    if not key:
        raise AIError("Gemini kaliti kiritilmagan")
    model = get_set("model", DEFAULT_MODEL)
    full_text = str(SYS) + chr(10) + chr(10) + str(prompt)
    parts = [{"text": full_text}] + (extra_parts or [])
    payload = {
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {"temperature": 0.7, "maxOutputTokens": 4096}
    }
    if json_mode:
        payload["generationConfig"]["responseMimeType"] = "application/json"
    body = json.dumps(payload).encode("utf-8")
    clean_model = str(model).replace("models/", "").strip()
    if clean_model in ["gemini-3.8-flash", "gemini-flash"]:
        clean_model = "gemini-3.8-flash"
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{clean_model}:generateContent?key={key}"
    last = ""
    for attempt in range(5):
        req = urllib.request.Request(url, body, {"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                d = json.load(r)
            cand = (d.get("candidates") or [{}])[0]
            text = "".join(p.get("text", "") for p in cand.get("content", {}).get("parts", []))
            if text.strip():
                return text
            last = "bo'sh javob"
        except urllib.error.HTTPError as e:
            err_msg = e.read().decode("utf-8", errors="ignore")
            print("API_XATOLIK:", err_msg)
            last = f"HTTP {e.code}"
            if e.code not in (429, 500, 503):
                raise AIError(last)
        except Exception as e:
            last = str(e)
        time.sleep(3 * (attempt + 1))
    raise AIError(last)
def parse_json(t):
    t = re.sub(r"^```(?:json)?|```$", "", t.strip(), flags=re.M).strip()
    try:
        return json.loads(t)
    except Exception:
        m = re.search(r"[\[{].*[\]}]", t, re.S)
        if m:
            return json.loads(m.group(0))
        raise AIError("JSON o'qib bo'lmadi")

def clean(t):
    t = re.sub(r"^#{1,6}\s*", "📌 ", t, flags=re.M)
    t = t.replace("**", "").replace("__", "")
    t = re.sub(r"^\s*[\*\-]\s+", "• ", t, flags=re.M)
    return t.strip()

# ───────────────────────── XIZMATLAR (60+) ─────────────────────────
# (kalit, kategoriya, nom, so'rov matni, tur, prompt)   {inp} — foydalanuvchi kiritgan matn
SERVICES = [
 # ✍️ Yozma ishlar
 ("referat", "w", "📄 Referat", "Referat mavzusini yozing:", "docx",
  "'{inp}' mavzusida to'liq referat yoz: sarlavha, reja, kirish, 4-5 ta asosiy bo'lim, xulosa, foydalanilgan adabiyotlar. Kamida 1500 so'z."),
 ("maqola", "w", "📰 Maqola", "Maqola mavzusini yozing:", "docx",
  "'{inp}' mavzusida jozibali maqola yoz: sarlavha, lid-abzas, bo'limlar, xulosa. Kamida 800 so'z."),
 ("esse", "w", "✒️ Esse", "Esse mavzusini yozing:", "docx",
  "'{inp}' mavzusida esse yoz: tezis, argumentlar, misollar, xulosa. 500-700 so'z."),
 ("mustaqil", "w", "📘 Mustaqil ish", "Mustaqil ish mavzusi:", "docx",
  "'{inp}' mavzusida mustaqil ish yoz: kirish, nazariy qism, amaliy misollar, xulosa, adabiyotlar. Kamida 1200 so'z."),
 ("tezis", "w", "🔬 Ilmiy tezis", "Tezis mavzusi:", "docx",
  "'{inp}' mavzusida ilmiy tezis yoz: annotatsiya, kalit so'zlar, dolzarblik, maqsad, usullar, natijalar, xulosa."),
 ("taqriz", "w", "🧾 Taqriz (retsenziya)", "Taqriz yoziladigan asar/ish nomi yoki matni:", "docx",
  "Quyidagi ish uchun professional taqriz yoz (dolzarblik, ijobiy tomonlar, kamchiliklar, tavsiyalar, baho): {inp}"),
 ("hisobot", "w", "🧪 Amaliy/laboratoriya hisoboti", "Mashg'ulot mavzusi:", "docx",
  "'{inp}' bo'yicha amaliy/laboratoriya ishi hisobotini yoz: maqsad, jihozlar, nazariya, bajarish tartibi, natijalar jadvali, xulosa."),
 ("kurs_reja", "w", "🗂 Kurs ishi rejasi", "Kurs ishi mavzusi:", "text",
  "'{inp}' mavzusida kurs ishining batafsil rejasini tuz: kirish, boblar, paragraflar, xulosa, har bir bo'limda nima yozilishi."),
 ("annotatsiya", "w", "📝 Annotatsiya", "Annotatsiya yoziladigan ish haqida ma'lumot yoki matn:", "text",
  "Quyidagi ish uchun o'zbekcha, ruscha va inglizcha annotatsiya yoz (har biri 4-5 gap): {inp}"),
 ("mavzu_goya", "w", "💡 Ilmiy mavzu g'oyalari", "Soha/fan nomi:", "text",
  "'{inp}' sohasida dolzarb 15 ta kurs ishi/diplom/dissertatsiya mavzusini taklif qil, har biriga 1 gaplik izoh ber."),
 ("adabiyot", "w", "📚 Adabiyotlar ro'yxati", "Mavzu:", "text",
  "'{inp}' mavzusi uchun GOST/APA uslubida adabiyotlar ro'yxati namunasini tuz. Aniq mavjudligiga ishonching komil manbalarnigina yoz, qolganini 'namuna' deb belgila."),
 ("kirish_xulosa", "w", "🧩 Kirish va xulosa", "Ish mavzusi:", "text",
  "'{inp}' mavzusidagi ish uchun professional 'Kirish' (dolzarblik, maqsad, vazifalar, ob'ekt, predmet) va 'Xulosa' qismlarini yoz."),
 # 📊 Slaydlar
 ("slayd", "s", "🖼 Taqdimot (slayd) yaratish", "Mavzu va slaydlar sonini yozing (masalan: Quyosh tizimi, 10):", "slides", ""),
 ("slayd_reja", "s", "🧭 Slayd rejasi", "Taqdimot mavzusi:", "text",
  "'{inp}' mavzusidagi taqdimot uchun slaydma-slayd reja tuz: har slayd sarlavhasi va qisqa mazmuni."),
 ("slayd_nutq", "s", "🎤 Nutq matni", "Taqdimot mavzusi (yoki slayd matni):", "text",
  "Quyidagi taqdimot uchun 5 daqiqalik og'zaki nutq matnini yoz (tabiiy, ishonchli ohangda): {inp}"),
 ("infografika", "s", "📈 Infografika matni", "Mavzu:", "text",
  "'{inp}' mavzusi uchun infografika kontentini tayyorla: sarlavha, 6-8 ta qisqa blok, raqam/faktlar, ikonka tavsiyalari."),
 ("slayd_savol", "s", "❓ Taqdimotdan keyingi savollar", "Mavzu:", "text",
  "'{inp}' taqdimotidan keyin tinglovchilar berishi mumkin bo'lgan 10 ta savol va ularga qisqa javoblar yoz."),
 # 📚 Matn bilan ishlash
 ("grammatika", "t", "✅ Grammatika tuzatish", "Matnni yuboring:", "text",
  "Quyidagi matndagi imlo, grammatika va punktuatsiya xatolarini tuzat. Avval tuzatilgan matnni, keyin qilingan o'zgarishlar ro'yxatini yoz:\n{inp}"),
 ("parafraz", "t", "🔄 Parafraz (qayta yozish)", "Matnni yuboring:", "text",
  "Quyidagi matnni ma'nosini saqlagan holda butunlay o'z so'zlaring bilan qayta yoz (parafraz), 2 ta variant ber:\n{inp}"),
 ("qisqartir", "t", "✂️ Matnni qisqartirish", "Matnni yuboring:", "text",
  "Quyidagi matnni asosiy fikrlarni yo'qotmay 3 barobar qisqartir:\n{inp}"),
 ("kengaytir", "t", "➕ Matnni kengaytirish", "Matnni yuboring:", "text",
  "Quyidagi matnni misollar va tushuntirishlar bilan 2-3 barobar kengaytir:\n{inp}"),
 ("tarjima", "t", "🌐 Tarjima", "Tarjima qilinadigan matn:", "text",
  "Quyidagi matnni aniq va tabiiy tarjima qil:\n{inp}"),
 ("ariza", "t", "📮 Rasmiy xat / ariza", "Kimga, nima haqida, qanday vaziyat (qisqacha):", "text",
  "Quyidagi ma'lumot asosida rasmiy xat/ariza matnini O'zbekiston ish yuritish qoidalariga mos yoz: {inp}"),
 ("rezyume", "t", "👔 Rezyume (CV)", "Ma'lumotlaringiz (ism, ta'lim, tajriba, ko'nikmalar):", "docx",
  "Quyidagi ma'lumotlardan professional rezyume (CV) tuz, ish beruvchiga yoqadigan tarzda: {inp}"),
 ("motivatsion", "t", "💌 Motivatsion xat", "Qaysi o'qish/ish uchun va o'zingiz haqingizda:", "docx",
  "Quyidagi ma'lumotlar asosida ishonarli motivatsion xat yoz (400 so'zgacha): {inp}"),
 ("sinonim", "t", "🔤 Sinonim va boy so'zlar", "So'z yoki jumla:", "text",
  "'{inp}' uchun sinonimlar, antonimlar, shu so'zli 5 ta namunaviy gap va so'z kelib chiqishi haqida qisqa ma'lumot ber."),
 ("matn_tahlil", "t", "🔍 Matn tahlili", "Tahlil qilinadigan matn:", "text",
  "Quyidagi matnni tahlil qil: asosiy g'oya, struktura, uslub, kuchli va zaif tomonlari, yaxshilash bo'yicha 5 ta tavsiya:\n{inp}"),
 # 👨‍🏫 O'qituvchi uchun
 ("dars_ishlanma", "o", "📖 Dars ishlanmasi", "Fan, sinf va dars mavzusi:", "docx",
  "Quyidagi dars uchun batafsil dars ishlanma tuz: maqsadlar (ta'limiy, tarbiyaviy, rivojlantiruvchi), jihozlar, dars bosqichlari (vaqt bilan), interfaol usullar, mustahkamlash, uy vazifasi, baholash: {inp}"),
 ("test", "o", "📋 Test tuzish (javoblari bilan)", "Mavzu va savollar soni (masalan: Fotosintez, 15):", "docx",
  "'{inp}' bo'yicha test tuz: har savolda 4 ta variant (A-D), oxirida to'g'ri javoblar kaliti. Savol soni so'ralmagan bo'lsa 15 ta."),
 ("savol", "o", "🙋 Savollar tuzish", "Mavzu:", "text",
  "'{inp}' mavzusi bo'yicha Blum taksonomiyasi darajalariga mos 15 ta ochiq savol tuz (eslash, tushunish, qo'llash, tahlil, baholash, yaratish)."),
 ("krossvord", "o", "🔠 Krossvord (so'z va ta'riflar)", "Mavzu:", "text",
  "'{inp}' mavzusi uchun krossvord tuz: 12 ta so'z (harf soni bilan), gorizontal/vertikal ta'riflar va javoblar jadvali."),
 ("rubrika", "o", "🎯 Baholash mezoni (rubrika)", "Topshiriq turi va fan:", "text",
  "'{inp}' uchun jadval ko'rinishida baholash rubrikasi tuz: mezonlar, 4 daraja (5-4-3-2), ballar."),
 ("uy_vazifa", "o", "🏠 Uy vazifasi", "Fan, sinf, mavzu:", "text",
  "'{inp}' bo'yicha 3 darajali (oson, o'rta, murakkab) uy vazifalari to'plamini tuz."),
 ("tematik_reja", "o", "🗓 Kalendar-tematik reja", "Fan, sinf va soatlar soni:", "docx",
  "'{inp}' uchun kalendar-tematik reja jadvalini tuz: dars raqami, mavzu, soat, dars turi, jihozlar, izoh."),
 ("ota_ona", "o", "👪 Ota-onalarga xat", "Vaziyat (yig'ilish, o'zlashtirish, tadbir...):", "text",
  "Ota-onalarga yo'naltirilgan hurmatli va aniq xat matnini yoz. Vaziyat: {inp}"),
 ("tarbiya", "o", "🌟 Tarbiya soati ssenariysi", "Mavzu va sinf:", "docx",
  "'{inp}' bo'yicha qiziqarli tarbiya soati ssenariysini yoz: kirish, faoliyatlar, savol-javob, mini o'yin, xulosa."),
 ("ochiq_dars", "o", "🎬 Ochiq dars ssenariysi", "Fan, sinf, mavzu:", "docx",
  "'{inp}' uchun ochiq dars ssenariysi: innovatsion metodlar (klaster, insert, venn diagrammasi...), o'qituvchi/o'quvchi faoliyati, vaqt taqsimoti."),
 ("portfolio", "o", "🗃 Portfolio / o'z-o'zini tahlil", "Faoliyatingiz haqida qisqacha:", "docx",
  "Quyidagi ma'lumotlar asosida o'qituvchi portfoliosi uchun o'z-o'zini tahlil matnini yoz: {inp}"),
 ("dars_tahlil", "o", "🔎 Dars tahlili", "O'tilgan dars haqida ma'lumot:", "text",
  "Quyidagi dars bo'yicha pedagogik tahlil yoz: kuchli tomonlar, kamchiliklar, tavsiyalar: {inp}"),
 ("mashq", "o", "🧮 Mashq va topshiriqlar to'plami", "Fan, sinf, mavzu:", "text",
  "'{inp}' bo'yicha 20 ta xilma-xil mashq/topshiriq tuz va oxirida javoblarini ber."),
 # 🧑‍🎓 Talaba uchun
 ("tushuntir", "u", "💭 Oddiy tushuntir", "Qaysi mavzuni tushunmadingiz?", "text",
  "'{inp}' mavzusini 10 yoshli bolaga tushuntirgandek oddiy, keyin chuqurroq akademik tarzda tushuntir. 2 ta hayotiy misol ber."),
 ("konspekt", "u", "🗒 Konspekt", "Mavzu yoki matn:", "text",
  "Quyidagi mavzu/matn bo'yicha o'qishga qulay konspekt tuz (asosiy tushunchalar, ta'riflar, xulosa): {inp}"),
 ("imtihon", "u", "🎓 Imtihon savollari + javoblar", "Fan va mavzu:", "text",
  "'{inp}' bo'yicha imtihonda tushishi mumkin bo'lgan 12 ta savol va qisqa, aniq javoblarini yoz."),
 ("masala", "u", "🧠 Masala yechish", "Masalani yozing:", "text",
  "Quyidagi masalani bosqichma-bosqich yech, har qadamni izohla va oxirida javobni tekshir: {inp}"),
 ("formula", "u", "📐 Formulalar shpargalkasi", "Fan/mavzu:", "text",
  "'{inp}' bo'yicha asosiy formulalar, qoidalar va ularning qisqa izohini jadval ko'rinishida yoz."),
 ("kitob", "u", "📕 Kitob/asar xulosasi", "Kitob nomi va muallifi:", "text",
  "'{inp}' asarining qisqa mazmuni, asosiy g'oya, qahramonlar, mavzu va o'quvchiga saboqlarini yoz. Asarni bilmasang, ochiq ayt."),
 ("oqish_reja", "u", "📅 Tayyorgarlik rejasi", "Imtihon/mavzu va qancha vaqt qolgani:", "text",
  "Quyidagi sharoit uchun kunma-kun o'qish rejasini tuz (takrorlash va dam olish bilan): {inp}"),
 ("mnemonika", "u", "🧷 Yodlash usullari (mnemonika)", "Yodlash kerak bo'lgan ma'lumot:", "text",
  "Quyidagi ma'lumotni oson yodlash uchun mnemonika, assotsiatsiya, qisqartma va hikoya usullarini taklif qil: {inp}"),
 ("debat", "u", "⚔️ Munozara argumentlari", "Munozara mavzusi:", "text",
  "'{inp}' mavzusida 'ha' va 'yo'q' tomonlar uchun 5 tadan kuchli argument va qarshi dalillarga javoblar tayyorla."),
 ("ingliz", "u", "🗣 Ingliz tili mashqi", "Mavzu yoki darajangiz (masalan: A2 — travel):", "text",
  "'{inp}' bo'yicha ingliz tilida mashq: 10 ta yangi so'z (tarjimasi bilan), 5 ta gap, kichik dialog va 5 ta mashq (javoblari oxirida)."),
 ("ielts", "u", "🏅 IELTS Writing tekshirish", "Inshongizni yuboring (savoli bilan):", "text",
  "Quyidagi IELTS Writing ishini 4 mezon (Task Response, Coherence, Lexical Resource, Grammar) bo'yicha baholab, taxminiy band ber va xatolarni tuzatib, yaxshilangan variantni yoz:\n{inp}"),
 ("pomodoro", "u", "⏱ Diqqat va samaradorlik rejasi", "Qanday muammo bor (masalan: tez chalg'iyman):", "text",
  "Quyidagi muammo uchun talabaga amaliy diqqat va vaqt boshqaruvi rejasi tuz (Pomodoro, ustuvorliklar): {inp}"),
 # 🎮 O'yinlar
 ("quiz", "g", "🧩 Viktorina (tugmali)", "Mavzu va savollar soni (masalan: Dunyo poytaxtlari, 10):", "quiz", ""),
 ("tf", "g", "✅❌ To'g'ri/Noto'g'ri", "Mavzu va gaplar soni (masalan: Tarix, 10):", "tf", ""),
 ("cards", "g", "🃏 Flesh-kartalar", "Mavzu va kartalar soni (masalan: Ingliz fe'llari, 10):", "cards", ""),
 ("hangman", "g", "🔤 So'z topish (Hangman)", "Mavzu (masalan: hayvonlar):", "hangman", ""),
 ("mathblitz", "g", "⚡ Matematik blits (bepul)", None, "mathblitz", ""),
 ("topishmoq", "g", "🕵️ Topishmoqlar", "Mavzu:", "text",
  "'{inp}' mavzusida 8 ta qiziqarli topishmoq tuz, javoblarini oxirida alohida ber."),
 ("mantiq", "g", "🧠 Mantiqiy o'yinlar", "Daraja yoki mavzu:", "text",
  "'{inp}' bo'yicha 6 ta mantiqiy topshiriq/jumboq tuz, yechimlarini oxirida izoh bilan ber."),
 ("sinf_oyin", "g", "🎲 Sinf uchun mini o'yin", "Fan, sinf, mavzu:", "text",
  "'{inp}' uchun sinfda 10-15 daqiqada o'tkaziladigan interfaol o'yin ishlab chiq: qoidalar, jihozlar, bosqichlar, g'olibni aniqlash."),
 # 🤖 AI yordamchi
 ("erkin", "a", "💬 Erkin AI savol", "Savolingizni yozing:", "text", "{inp}"),
 ("kod", "a", "💻 Dasturlash yordami", "Kod yoki vazifa:", "text",
  "Dasturlash bo'yicha yordam ber: kodni yoz/tuzat, qisqa izohla, xatolarni ko'rsat:\n{inp}"),
 ("rasm", "a", "🖼 Rasm/PDF tahlili", "Rasm yoki PDF yuboring (izoh sifatida savol yozishingiz mumkin):", "vision",
  "Yuborilgan fayl mazmunini o'qib tahlil qil. Foydalanuvchi izohi: {inp}"),
 ("karyera", "a", "🚀 Karyera maslahati", "Maqsad va hozirgi holatingiz:", "text",
  "Quyidagi holat uchun aniq karyera yo'l xaritasini tuz (ko'nikmalar, kurslar, loyihalar, 3-6-12 oylik qadamlar): {inp}"),
 ("goya", "a", "✨ G'oyalar generatori", "Qaysi mavzuda g'oya kerak?", "text",
  "'{inp}' bo'yicha 12 ta original va amaliy g'oya ber, har biriga 1 gaplik izoh qo'sh."),
 ("xulosa", "a", "📌 Matn xulosasi", "Matnni yuboring:", "text",
  "Quyidagi matnning qisqa xulosasini yoz: 1 gaplik mohiyat, 5 ta asosiy fikr, yakuniy xulosa:\n{inp}"),
]

CATS = [("w", "✍️ Yozma ishlar"), ("s", "📊 Slaydlar"), ("t", "📚 Matn bilan ishlash"),
        ("o", "👨‍🏫 O'qituvchi uchun"), ("u", "🧑‍🎓 Talaba uchun"), ("g", "🎮 O'yinlar"), ("a", "🤖 AI yordamchi")]
CAT_BY_LABEL = {l: c for c, l in CATS}
SV = {s[0]: dict(key=s[0], cat=s[1], title=s[2], ask=s[3], kind=s[4], prompt=s[5]) for s in SERVICES}

# ───────────────────────── DIZAYNLAR ─────────────────────────
STYLES = {
    "🙂 Oddiy": "Oddiy, tushunarli tilda yoz.",
    "🎓 Rasmiy-akademik": "Rasmiy, akademik uslubda, atamalar bilan yoz.",
    "✨ Qiziqarli": "Jonli, qiziqarli, misol va o'xshatishlar bilan yoz; emoji ishlatishing mumkin.",
    "📌 Qisqa punktlar": "Juda qisqa, faqat punktlar va ro'yxatlar ko'rinishida yoz.",
    "📋 Jadval/sxema": "Imkon qadar jadval va sxema ko'rinishida (matn bilan) tuzilma qil.",
}
LEVELS = {
    "🟢 Oson": "oson (5-7-sinf darajasi)", "🟡 O'rta": "o'rta (9-11-sinf darajasi)",
    "🔴 Qiyin": "qiyin (talaba darajasi)", "🏆 Olimpiada": "juda qiyin (olimpiada darajasi)",
}
LANGS = {"🇺🇿 O'zbekcha": "o'zbek (lotin)", "🇷🇺 Ruscha": "rus", "🇬🇧 Inglizcha": "ingliz", "🇹🇷 Turkcha": "turk"}
DOC_THEMES = {
    "📘 Klassik": dict(font="Times New Roman", sz=28, hc="000000", line=360, just=False),
    "💼 Zamonaviy": dict(font="Calibri", sz=24, hc="1F4E79", line=276, just=False),
    "🎓 Akademik": dict(font="Times New Roman", sz=28, hc="000000", line=360, just=True),
    "🌿 Yashil": dict(font="Arial", sz=24, hc="2E7D32", line=300, just=False),
    "🖤 Minimal": dict(font="Arial", sz=22, hc="333333", line=276, just=False),
}
SLIDE_THEMES = {
    "🌌 Tungi": dict(bg="linear-gradient(135deg,#0f172a,#312e81)", fg="#f8fafc", ac="#38bdf8", font="Segoe UI,Arial", lay="center", pbg="1E1B4B", pfg="F8FAFC", pac="38BDF8"),
    "☀️ Yorug' minimal": dict(bg="#ffffff", fg="#1f2937", ac="#2563eb", font="Segoe UI,Arial", lay="left", pbg="FFFFFF", pfg="1F2937", pac="2563EB"),
    "🌿 Tabiat": dict(bg="linear-gradient(135deg,#ecfdf5,#bbf7d0)", fg="#064e3b", ac="#059669", font="Segoe UI,Arial", lay="split", pbg="D1FAE5", pfg="064E3B", pac="059669"),
    "🔥 Quyosh": dict(bg="linear-gradient(135deg,#fff7ed,#fdba74)", fg="#7c2d12", ac="#ea580c", font="Segoe UI,Arial", lay="left", pbg="FFEDD5", pfg="7C2D12", pac="EA580C"),
    "🌊 Okean": dict(bg="linear-gradient(160deg,#083344,#0e7490)", fg="#ecfeff", ac="#67e8f9", font="Segoe UI,Arial", lay="center", pbg="0E4F63", pfg="ECFEFF", pac="67E8F9"),
    "🎨 Pastel": dict(bg="linear-gradient(135deg,#fdf2f8,#e0e7ff)", fg="#3b0764", ac="#c026d3", font="Segoe UI,Arial", lay="split", pbg="F5E8FF", pfg="3B0764", pac="C026D3"),
    "🖤 Qora-oltin": dict(bg="#111111", fg="#f5f5f5", ac="#facc15", font="Georgia,serif", lay="left", pbg="111111", pfg="F5F5F5", pac="FACC15"),
    "📰 Gazeta": dict(bg="#f5f0e6", fg="#1c1917", ac="#b91c1c", font="Georgia,serif", lay="left", pbg="F5F0E6", pfg="1C1917", pac="B91C1C"),
}

def options_for(sv):
    k = sv["kind"]
    if k == "docx":
        return list(DOC_THEMES)
    if k == "slides":
        return list(SLIDE_THEMES)
    if sv["key"] == "tarjima":
        return list(LANGS)
    if k in ("quiz", "tf", "cards", "hangman", "mathblitz"):
        return list(LEVELS)
    return list(STYLES)

def opt_label(sv):
    k = sv["kind"]
    if k == "docx":
        return "🎨 Hujjat dizaynini tanlang:"
    if k == "slides":
        return "🎨 Slayd dizaynini tanlang:"
    if sv["key"] == "tarjima":
        return "🌐 Qaysi tilga tarjima qilamiz?"
    if k in ("quiz", "tf", "cards", "hangman", "mathblitz"):
        return "🎚 Darajani tanlang:"
    return "🎨 Javob uslubini tanlang:"

# ───────────────────────── HUJJAT (.docx) ─────────────────────────
def _x(t):
    return html.escape(t, quote=False)

def make_docx(md, theme_name):
    th = DOC_THEMES.get(theme_name, list(DOC_THEMES.values())[0])
    font, sz = th["font"], th["sz"]
    def run(text, size=sz, bold=False, color=None):
        rp = f'<w:rFonts w:ascii="{font}" w:hAnsi="{font}" w:cs="{font}"/>'
        if bold:
            rp += "<w:b/>"
        if color:
            rp += f'<w:color w:val="{color}"/>'
        rp += f'<w:sz w:val="{size}"/>'
        return f'<w:r><w:rPr>{rp}</w:rPr><w:t xml:space="preserve">{_x(text)}</w:t></w:r>'
    def para(runs, jc=None, before=0, after=120, ind=None):
        pp = f'<w:spacing w:before="{before}" w:after="{after}" w:line="{th["line"]}" w:lineRule="auto"/>'
        if ind:
            pp += ind
        if jc:
            pp += f'<w:jc w:val="{jc}"/>'
        return f"<w:p><w:pPr>{pp}</w:pPr>{runs}</w:p>"
    def inline(text, size=sz, bold=False, color=None):
        out = ""
        for i, seg in enumerate(re.split(r"\*\*(.+?)\*\*", text)):
            if seg:
                out += run(seg, size, bold or i % 2 == 1, color)
        return out
    body = []
    first_title = True
    for ln in md.splitlines():
        s = ln.rstrip()
        if not s.strip():
            continue
        if re.match(r"^\s*\|?[\s:\-|]+\|?\s*$", s) and "-" in s:
            continue
        m = re.match(r"^(#{1,3})\s+(.*)", s)
        if m:
            lvl, tx = len(m.group(1)), m.group(2).strip("* ")
            if lvl == 1:
                body.append(para(run(tx, sz + 10, True, th["hc"]), "center", 200 if first_title else 360, 240))
                first_title = False
            elif lvl == 2:
                body.append(para(run(tx, sz + 4, True, th["hc"]), None, 240, 120))
            else:
                body.append(para(run(tx, sz + 2, True, th["hc"]), None, 160, 80))
            continue
        if s.lstrip().startswith("|"):
            cells = [c.strip().strip("*") for c in s.strip().strip("|").split("|")]
            body.append(para(run("   ".join(cells), sz - 2)))
            continue
        m = re.match(r"^\s*(?:[-*•]|\d+[.)])\s+(.*)", s)
        if m:
            lead = re.match(r"^\s*(\d+[.)])", s)
            mark = (lead.group(1) + " ") if lead else "• "
            body.append(para(run(mark) + inline(m.group(1)), None, 0, 60,
                             '<w:ind w:left="567" w:hanging="283"/>'))
            continue
        ind = '<w:ind w:firstLine="709"/>' if th["just"] else None
        body.append(para(inline(s.strip()), "both" if th["just"] else None, 0, 120, ind))
    sect = ('<w:sectPr><w:pgSz w:w="11906" w:h="16838"/><w:pgMar w:top="1134" w:right="850" '
            'w:bottom="1134" w:left="1701" w:header="708" w:footer="708" w:gutter="0"/></w:sectPr>')
    doc = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
           '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
           f'<w:body>{"".join(body)}{sect}</w:body></w:document>')
    ct = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
          '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
          '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
          '<Default Extension="xml" ContentType="application/xml"/>'
          '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
          '</Types>')
    rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
            '</Relationships>')
    bio = io.BytesIO()
    with zipfile.ZipFile(bio, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", ct)
        z.writestr("_rels/.rels", rels)
        z.writestr("word/document.xml", doc)
    return bio.getvalue()

# ───────────────────────── SLAYD (.html / .pptx) ─────────────────────────
def make_slides_html(data, theme_name):
    th = SLIDE_THEMES.get(theme_name, list(SLIDE_THEMES.values())[0])
    e = lambda t: html.escape(str(t))
    sl = [f'<section class="slide {th["lay"]}"><div class="t"><h1>{e(data.get("title",""))}</h1>'
          f'<p class="sub">{e(data.get("subtitle",""))}</p></div></section>']
    for s in data.get("slides", []):
        lis = "".join(f"<li>{e(b)}</li>" for b in s.get("bullets", []))
        sl.append(f'<section class="slide {th["lay"]}"><div class="t"><h2>{e(s.get("title",""))}</h2></div>'
                  f'<ul>{lis}</ul></section>')
    css = f"""
:root{{--bg:{th['bg']};--fg:{th['fg']};--ac:{th['ac']}}}
*{{box-sizing:border-box}}body{{margin:0;font-family:{th['font']};background:var(--bg);color:var(--fg);min-height:100vh}}
.slide{{display:none;min-height:100vh;padding:7vh 8vw 12vh;flex-direction:column;justify-content:center}}
.slide.on{{display:flex}}.slide.split.on{{display:grid;grid-template-columns:1fr 1.5fr;gap:5vw;align-items:center}}
.slide.center{{text-align:center}}.slide.center ul{{list-style-position:inside;padding:0}}
h1{{font-size:clamp(34px,6vw,72px);margin:0 0 .3em;color:var(--ac)}}h2{{font-size:clamp(26px,4.2vw,52px);margin:0 0 .6em;color:var(--ac);
border-left:8px solid var(--ac);padding-left:.4em}}.slide.center h2{{border:0;padding:0}}
.sub{{font-size:clamp(16px,2.4vw,28px);opacity:.85}}ul{{font-size:clamp(18px,2.6vw,32px);line-height:1.5}}li{{margin:.4em 0}}
li::marker{{color:var(--ac)}}.nav{{position:fixed;bottom:2vh;left:0;right:0;display:flex;justify-content:center;gap:12px;align-items:center}}
.nav button{{background:var(--ac);color:#fff;border:0;border-radius:999px;padding:10px 22px;font-size:16px;cursor:pointer}}
.nav span{{opacity:.8}}@media print{{.slide{{display:flex!important;page-break-after:always}}.nav{{display:none}}}}"""
    js = """var i=0,s=document.querySelectorAll('.slide'),c=document.getElementById('c');
function go(n){i=Math.max(0,Math.min(s.length-1,n));s.forEach(function(x,k){x.classList.toggle('on',k==i)});c.textContent=(i+1)+' / '+s.length}
document.addEventListener('keydown',function(e){if(e.key=='ArrowRight'||e.key==' ')go(i+1);if(e.key=='ArrowLeft')go(i-1);if(e.key=='f')document.documentElement.requestFullscreen()});
document.getElementById('p').onclick=function(){go(i-1)};document.getElementById('n').onclick=function(){go(i+1)};go(0);"""
    return (f'<!DOCTYPE html><html lang="uz"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1"><title>{e(data.get("title",""))}</title>'
            f'<style>{css}</style></head><body>{"".join(sl)}'
            f'<div class="nav"><button id="p">◀</button><span id="c"></span><button id="n">▶</button></div>'
            f'<script>{js}</script></body></html>').encode("utf-8")

def make_pptx(data, theme_name):
    try:
        from pptx import Presentation
        from pptx.util import Inches, Pt
        from pptx.dml.color import RGBColor
    except Exception:
        return None
    th = SLIDE_THEMES.get(theme_name, list(SLIDE_THEMES.values())[0])
    rgb = RGBColor.from_string
    prs = Presentation()
    prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)
    def add(title, lines, big=False, notes=""):
        s = prs.slides.add_slide(prs.slide_layouts[6])
        f = s.background.fill
        f.solid()
        f.fore_color.rgb = rgb(th["pbg"])
        tb = s.shapes.add_textbox(Inches(.8), Inches(2.4 if big else .5), Inches(11.7), Inches(1.4))
        tb.text_frame.word_wrap = True
        p = tb.text_frame.paragraphs[0]
        p.text = title
        p.font.size, p.font.bold = Pt(48 if big else 34), True
        p.font.color.rgb = rgb(th["pac"])
        if lines:
            bb = s.shapes.add_textbox(Inches(.9), Inches(3.9 if big else 2.0), Inches(11.5), Inches(5))
            tf = bb.text_frame
            tf.word_wrap = True
            for k, ln in enumerate(lines):
                p = tf.paragraphs[0] if k == 0 else tf.add_paragraph()
                p.text = ("" if big else "• ") + str(ln)
                p.font.size = Pt(22 if big else 24)
                p.font.color.rgb = rgb(th["pfg"])
                p.space_after = Pt(10)
        if notes:
            s.notes_slide.notes_text_frame.text = notes
    add(data.get("title", ""), [data.get("subtitle", "")], big=True)
    for s in data.get("slides", []):
        add(s.get("title", ""), s.get("bullets", []), notes=s.get("notes", ""))
    bio = io.BytesIO()
    prs.save(bio)
    return bio.getvalue()

# ───────────────────────── HOLAT ─────────────────────────
ST = {}          # uid -> holat
BUSY = set()
MENU_BTNS = ["💰 Balans", "👤 Profil", "ℹ️ Yordam"]

def main_kb():
    rows, row = [], []
    for _, label in CATS:
        row.append({"text": label})
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([{"text": "💰 Balans"}, {"text": "👤 Profil"}])
    rows.append([{"text": "ℹ️ Yordam"}])
    return {"keyboard": rows, "resize_keyboard": True}

def svc_row(key):
    return q("SELECT * FROM services WHERE key=?", (key,), one=True)

def price_of(key):
    r = svc_row(key)
    return r["price"] if r else DEFAULT_PRICE

def can_use(uid, key):
    u, p = get_user(uid), price_of(key)
    return p <= 0 or u["free_left"] > 0 or u["balance"] >= p

def charge(uid, key):
    u, p = get_user(uid), price_of(key)
    note = ""
    paid = 0
    if p > 0:
        if u["free_left"] > 0:
            q("UPDATE users SET free_left=free_left-1 WHERE id=?", (uid,))
            note = f"🎁 Bepul urinishlar qoldi: {u['free_left'] - 1}"
        else:
            q("UPDATE users SET balance=balance-? WHERE id=?", (p, uid))
            paid = p
            note = f"💳 {p:,} so'm yechildi. Balans: {u['balance'] - p:,} so'm".replace(",", " ")
    q("INSERT INTO usage(uid,key,ts,paid) VALUES(?,?,?,?)", (uid, key, int(time.time()), paid))
    return note

def cat_kb(cid):
    rows = []
    for s in SERVICES:
        if s[1] != cid:
            continue
        r = svc_row(s[0])
        if r and r["enabled"]:
            rows.append([btn(s[2] + (" 🆓" if r["price"] <= 0 else ""), "sv:" + s[0])])
    return ik(rows)

def show_cat(chat, cid):
    label = dict(CATS)[cid]
    send(chat, f"<b>{label}</b>\nXizmatni tanlang 👇", cat_kb(cid))

def welcome(chat, uid, name):
    u = get_user(uid)
    send(chat, f"Assalomu alaykum, <b>{esc(name)}</b>! 👋\n\n"
               f"Men o'quvchi, talaba va o'qituvchilar uchun <b>{len(SERVICES)}+ xizmat</b>ni bir joyga jamlagan AI botman: "
               f"referat, maqola, slayd, test, o'yinlar va boshqalar.\n\n"
               f"🎁 Sizga <b>{u['free_left']} ta bepul</b> urinish berildi. Keyin har bir xizmat o'rtacha "
               f"{int(get_set('default_price', DEFAULT_PRICE)):,} so'm.".replace(",", " ") +
         "\n\nPastdagi tugmalardan bo'lim tanlang 👇", main_kb())

def show_balance(chat, uid):
    u = get_user(uid)
    amounts = [5000, 10000, 20000, 50000, 100000]
    rows = [[btn(f"{a:,} so'm".replace(",", " "), f"tp:{a}") for a in amounts[:3]],
            [btn(f"{a:,} so'm".replace(",", " "), f"tp:{a}") for a in amounts[3:]]]
    send(chat, f"💰 <b>Balansingiz:</b> {u['balance']:,} so'm\n🎁 <b>Bepul urinish:</b> {u['free_left']} ta\n\n"
               "Balansni to'ldirish uchun summani tanlang 👇".replace(",", " "), ik(rows))

def show_profile(chat, uid):
    u = get_user(uid)
    n = q("SELECT COUNT(*) c FROM usage WHERE uid=?", (uid,), one=True)["c"]
    send(chat, f"👤 <b>Profil</b>\nID: <code>{uid}</code>\nIsm: {esc(u['name'])}\n"
               f"Foydalanishlar: {n} ta\nBalans: {u['balance']:,} so'm\nBepul qoldi: {u['free_left']} ta".replace(",", " "))

def show_help(chat):
    send(chat, "ℹ️ <b>Qanday ishlaydi?</b>\n\n1️⃣ Pastdagi tugmalardan bo'lim tanlang\n2️⃣ Xizmatni tanlang\n"
               "3️⃣ Mavzu/matnni yozing\n4️⃣ Dizayn yoki darajani tanlang\n5️⃣ Natijani oling ✅\n\n"
               "Dastlabki urinishlar bepul, keyin balansdan yechiladi. Xatolik bo'lsa, pul yechilmaydi.\n"
               "Bekor qilish: /cancel  •  Menyu: /menu")

# ───────────────────────── XIZMAT ISHGA TUSHIRISH ─────────────────────────
def split_count(inp, default, lo, hi):
    m = re.search(r"(\d+)\s*$", inp.strip())
    if m and lo <= int(m.group(1)) <= hi:
        topic = inp[:m.start()].strip(" ,;:-") or inp
        return topic, int(m.group(1))
    return inp, default

def start_service(chat, uid, key):
    sv = SV[key]
    r = svc_row(key)
    if not r or not r["enabled"]:
        send(chat, "Bu xizmat vaqtincha o'chirilgan.")
        return
    ST[uid] = {"mode": "input", "key": key}
    if sv["ask"] is None:
        ask_options(chat, uid)
        return
    send(chat, f"<b>{sv['title']}</b>\n\n{esc(sv['ask'])}", ik([[btn("❌ Bekor qilish", "cancel")]]))

def ask_options(chat, uid):
    st = ST[uid]
    sv = SV[st["key"]]
    st["mode"] = "options"
    opts = options_for(sv)
    rows = [[btn(o, f"ds:{i}")] for i, o in enumerate(opts)]
    rows.append([btn("❌ Bekor qilish", "cancel")])
    send(chat, opt_label(sv), ik(rows))

def need_money(chat, key):
    send(chat, f"⚠️ Bepul urinishlar tugadi va balans yetarli emas.\nBu xizmat narxi: "
               f"<b>{price_of(key):,} so'm</b>.".replace(",", " "),
         ik([[btn("💰 Balansni to'ldirish", "bal")]]))

def run_service(uid, chat, key, inp, opt):
    sv = SV[key]
    kind = sv["kind"]
    if uid in BUSY:
        send(chat, "⏳ Oldingi so'rovingiz hali bajarilmoqda, biroz kuting.")
        return
    if not can_use(uid, key):
        need_money(chat, key)
        return
    BUSY.add(uid)
    wait = send(chat, "⏳ Tayyorlanmoqda… (30-60 soniya olishi mumkin)")
    ok = False
    try:
        opts = options_for(sv)
        name = opts[opt] if 0 <= opt < len(opts) else opts[0]
        if kind == "text":
            ok = do_text(chat, sv, inp, name)
        elif kind == "docx":
            ok = do_docx(chat, sv, inp, name)
        elif kind == "slides":
            ok = do_slides(chat, sv, inp, name)
        elif kind in ("quiz", "tf", "cards"):
            ok = do_interactive(chat, uid, sv, inp, name)
        elif kind == "hangman":
            ok = do_hangman(chat, uid, inp, name)
        elif kind == "mathblitz":
            ok = do_math(chat, uid, name)
        elif kind == "vision":
            ok = do_vision(chat, sv, inp)
    except AIError as e:
        log("AIError", key, e)
        send(chat, "😕 AI hozir band yoki xatolik berdi. Birozdan keyin qayta urinib ko'ring. Pul yechilmadi.")
    except Exception:
        log(traceback.format_exc())
        send(chat, "😕 Kutilmagan xatolik. Qayta urinib ko'ring. Pul yechilmadi.")
    finally:
        BUSY.discard(uid)
    if wait:
        tg("deleteMessage", chat_id=chat, message_id=wait)
    if ok:
        note = charge(uid, key)
        if note:
            send(chat, note)
        if kind in ("text", "docx", "slides", "vision"):
            send(chat, "Yana xizmat kerak bo'lsa, pastdagi tugmalardan foydalaning 👇", None)

def do_text(chat, sv, inp, style_name):
    if sv["key"] == "tarjima":
        instr = f"Tarjima tili: {LANGS[style_name]}. Faqat tarjimani yoz."
    else:
        instr = "Uslub: " + STYLES[style_name]
    out = gemini(sv["prompt"].replace("{inp}", inp) + "\n\n" + instr)
    send(chat, clean(out), plain=True)
    return True

def do_docx(chat, sv, inp, theme):
    prompt = (sv["prompt"].replace("{inp}", inp) +
              "\n\nFormat: Markdown. Birinchi qator '# Sarlavha', bo'limlar '## ...', ro'yxatlar '- ...'. "
              "Hech qanday kirish/izoh so'zlarisiz, faqat hujjat matnini yoz.")
    out = gemini(prompt)
    data = make_docx(out, theme)
    fn = slug(inp) + ".docx"
    r = send_doc(chat, fn, data, f"{sv['title']} • dizayn: {theme}")
    if not r.get("ok"):
        send(chat, clean(out), plain=True)
    else:
        preview = clean(out)[:600]
        send(chat, "👀 Boshlanishi:\n\n" + preview + "…", plain=True)
    return True

def do_slides(chat, sv, inp, theme):
    topic, n = split_count(inp, 10, 4, 25)
    prompt = (f"'{topic}' mavzusida {n} ta kontent slaydidan iborat taqdimot tuz. Faqat JSON qaytar: "
              '{"title":"...","subtitle":"...","slides":[{"title":"...","bullets":["qisqa band","..."],"notes":"nutq uchun 2-3 gap"}]}. '
              "Har slaydda 3-5 ta qisqa band (har biri 12 so'zdan oshmasin). Oxirgi slayd — xulosa.")
    data = parse_json(gemini(prompt, json_mode=True))
    if isinstance(data, list):
        data = {"title": topic, "subtitle": "", "slides": data}
    h = make_slides_html(data, theme)
    send_doc(chat, slug(topic) + "_slayd.html", h,
             f"🖼 {theme} dizayni. Faylni brauzerda oching (◀ ▶ yoki strelka tugmalari, F — to'liq ekran).")
    px = make_pptx(data, theme)
    if px:
        send_doc(chat, slug(topic) + ".pptx", px, "📎 PowerPoint varianti (nutq matni izohlarda).")
    else:
        send(chat, "ℹ️ .pptx kerak bo'lsa, Termuxda <code>pip install python-pptx</code> o'rnating.")
    return True

def do_vision(chat, sv, inp):
    st = ST.get("_file", {})
    return False  # haqiqiy ishlov handle_message ichida (fayl bilan)

def shuffle_q(item):
    opts = list(item["options"])[:4]
    correct = opts[int(item["answer"]) % len(opts)]
    random.shuffle(opts)
    return {"q": item["q"], "options": opts, "answer": opts.index(correct), "explain": item.get("explain", "")}

def do_interactive(chat, uid, sv, inp, level_name):
    topic, n = split_count(inp, 10, 3, 20)
    lvl = LEVELS[level_name]
    if sv["kind"] == "quiz":
        p = (f"Mavzu: {topic}. Daraja: {lvl}. {n} ta test savoli tuz. Faqat JSON massiv qaytar: "
             '[{"q":"savol","options":["variant1","variant2","variant3","variant4"],"answer":0,"explain":"qisqa izoh"}]. '
             "answer — to'g'ri variant indeksi (0-3). Savollar xilma-xil va aniq bo'lsin.")
        qs = [shuffle_q(x) for x in parse_json(gemini(p, json_mode=True))][:n]
        ST[uid] = {"mode": "play", "quiz": {"qs": qs, "i": 0, "score": 0, "answered": False, "mid": None}}
        send_question(chat, uid)
    elif sv["kind"] == "tf":
        p = (f"Mavzu: {topic}. Daraja: {lvl}. {n} ta gap yoz: yarmi to'g'ri, yarmi noto'g'ri bo'lsin (aralash tartibda). Faqat JSON massiv: "
             '[{"q":"gap","answer":true,"explain":"qisqa izoh"}]')
        qs = []
        for x in parse_json(gemini(p, json_mode=True))[:n]:
            qs.append({"q": x["q"], "options": ["✅ To'g'ri", "❌ Noto'g'ri"],
                       "answer": 0 if x["answer"] in (True, "true", "True") else 1, "explain": x.get("explain", "")})
        ST[uid] = {"mode": "play", "quiz": {"qs": qs, "i": 0, "score": 0, "answered": False, "mid": None}}
        send_question(chat, uid)
    else:
        p = (f"Mavzu: {topic}. Daraja: {lvl}. {n} ta flesh-karta tuz. Faqat JSON massiv: "
             '[{"front":"savol/so\'z","back":"javob/izoh"}]')
        cards = parse_json(gemini(p, json_mode=True))[:n]
        ST[uid] = {"mode": "play", "cards": {"cs": cards, "i": 0, "known": 0}}
        send_card(chat, uid)
    return True

# ----- viktorina motori
def q_text(qz):
    i = qz["i"]
    q_ = qz["qs"][i]
    t = f"❓ <b>{i + 1}/{len(qz['qs'])}</b>   ✅ {qz['score']}\n\n{esc(q_['q'])}\n\n"
    if len(q_["options"]) > 2:
        for j, o in enumerate(q_["options"]):
            t += f"<b>{'ABCD'[j]})</b> {esc(o)}\n"
    return t

def q_kb(qz):
    i = qz["i"]
    o = qz["qs"][i]["options"]
    if len(o) == 2:
        return ik([[btn(o[j], f"qa:{i}:{j}") for j in range(2)]])
    return ik([[btn("ABCD"[j], f"qa:{i}:{j}") for j in range(len(o))]])

def send_question(chat, uid):
    qz = ST[uid]["quiz"]
    qz["mid"] = send(chat, q_text(qz), q_kb(qz))

def on_answer(chat, mid, uid, i, j):
    st = ST.get(uid, {})
    qz = st.get("quiz")
    if not qz or qz["i"] != i or qz["answered"]:
        return
    qz["answered"] = True
    item = qz["qs"][i]
    good = j == item["answer"]
    if good:
        qz["score"] += 1
    res = "✅ <b>To'g'ri!</b>" if good else f"❌ <b>Noto'g'ri.</b> To'g'ri javob: <b>{esc(item['options'][item['answer']])}</b>"
    if item.get("explain"):
        res += f"\n💡 {esc(item['explain'])}"
    last = i + 1 >= len(qz["qs"])
    edit(chat, mid, q_text(qz) + "\n" + res, ik([[btn("🏁 Natija" if last else "➡️ Keyingi", "qn")]]))

def on_next(chat, mid, uid):
    qz = ST.get(uid, {}).get("quiz")
    if not qz or not qz["answered"]:
        return
    qz["i"] += 1
    qz["answered"] = False
    if qz["i"] >= len(qz["qs"]):
        n, s = len(qz["qs"]), qz["score"]
        pct = round(100 * s / n)
        medal = "🏆" if pct >= 90 else "🥈" if pct >= 70 else "🥉" if pct >= 50 else "📚"
        edit(chat, mid, f"{medal} <b>Natija: {s}/{n}</b> ({pct}%)\n\n" +
             ("Ajoyib!" if pct >= 70 else "Yana mashq qilsangiz, yaxshilanadi!"),
             ik([[btn("🎮 Yana o'ynash", "cat:g")]]))
        ST.pop(uid, None)
    else:
        edit(chat, mid, q_text(qz), q_kb(qz))

# ----- flesh-kartalar
def send_card(chat, uid, mid=None):
    c = ST[uid]["cards"]
    card = c["cs"][c["i"]]
    t = f"🃏 <b>{c['i'] + 1}/{len(c['cs'])}</b>\n\n{esc(card['front'])}"
    kb = ik([[btn("👁 Javobni ko'rish", f"fc:show:{c['i']}")]])
    if mid:
        edit(chat, mid, t, kb)
    else:
        send(chat, t, kb)

def on_card(chat, mid, uid, action, idx):
    c = ST.get(uid, {}).get("cards")
    if not c or c["i"] != idx:
        return
    card = c["cs"][idx]
    if action == "show":
        edit(chat, mid, f"🃏 <b>{idx + 1}/{len(c['cs'])}</b>\n\n{esc(card['front'])}\n\n➡️ <b>{esc(card['back'])}</b>",
             ik([[btn("✅ Bildim", f"fc:yes:{idx}"), btn("❌ Bilmadim", f"fc:no:{idx}")]]))
    else:
        if action == "yes":
            c["known"] += 1
        c["i"] += 1
        if c["i"] >= len(c["cs"]):
            edit(chat, mid, f"🏁 Tugadi! Bilganlaringiz: <b>{c['known']}/{len(c['cs'])}</b>",
                 ik([[btn("🎮 Yana o'ynash", "cat:g")]]))
            ST.pop(uid, None)
        else:
            send_card(chat, uid, mid)

# ----- hangman
FALLBACK_WORDS = [("olma", "meva"), ("kitob", "o'qish uchun"), ("maktab", "bilim dargohi"), ("quyosh", "osmonda porlaydi"),
                  ("daryo", "oqadi"), ("kompyuter", "elektron qurilma")]

def do_hangman(chat, uid, inp, level_name):
    lvl = LEVELS[level_name]
    try:
        d = parse_json(gemini(f"Mavzu: {inp}. Daraja: {lvl}. Hangman o'yini uchun bitta so'z tanla: faqat a-z lotin harflaridan "
                              "iborat (o', g', sh, ch belgilarsiz, bo'shliqsiz), 5-10 harf. JSON: "
                              '{"word":"...","hint":"qisqa ishora"}', json_mode=True))
        w = re.sub(r"[^a-z]", "", str(d["word"]).lower())
        hint = d.get("hint", "")
        if len(w) < 4:
            raise ValueError
    except Exception:
        w, hint = random.choice(FALLBACK_WORDS)
    ST[uid] = {"mode": "play", "hang": {"w": w, "hint": hint, "g": [], "lives": 6, "mid": None}}
    h = ST[uid]["hang"]
    h["mid"] = send(chat, hang_text(h), hang_kb(h))
    return True

def hang_text(h, end=None):
    shown = " ".join(c if c in h["g"] else "＿" for c in h["w"])
    t = f"🔤 <b>So'z topish</b>\n\n{'❤️' * h['lives']}{'🖤' * (6 - h['lives'])}\n\n<code>{shown}</code>\n\n💡 Ishora: {esc(h['hint'])}"
    if end:
        t += "\n\n" + end
    return t

def hang_kb(h):
    letters = [c for c in "abcdefghijklmnopqrstuvwxyz" if c not in h["g"]]
    rows = [[btn(c.upper(), "hg:" + c) for c in letters[i:i + 7]] for i in range(0, len(letters), 7)]
    return ik(rows)

def on_hang(chat, mid, uid, ch):
    h = ST.get(uid, {}).get("hang")
    if not h or ch in h["g"]:
        return
    h["g"].append(ch)
    if ch not in h["w"]:
        h["lives"] -= 1
    if all(c in h["g"] for c in h["w"]):
        edit(chat, mid, hang_text(h, "🏆 <b>G'alaba!</b> To'g'ri so'z: " + h["w"].upper()), ik([[btn("🎮 Yana o'ynash", "cat:g")]]))
        ST.pop(uid, None)
    elif h["lives"] <= 0:
        edit(chat, mid, hang_text(h, "💀 <b>Yutqazdingiz.</b> So'z: " + h["w"].upper()), ik([[btn("🎮 Yana o'ynash", "cat:g")]]))
        ST.pop(uid, None)
    else:
        edit(chat, mid, hang_text(h), hang_kb(h))

# ----- matematik blits (lokal, AI kerak emas)
def do_math(chat, uid, level_name):
    lvl = list(LEVELS).index(level_name)
    qs = []
    for _ in range(10):
        if lvl == 0:
            a, b = random.randint(2, 30), random.randint(2, 30)
            op = random.choice("+-")
        elif lvl == 1:
            a, b = random.randint(10, 99), random.randint(10, 99)
            op = random.choice("+-×")
            if op == "×":
                a, b = random.randint(3, 12), random.randint(3, 12)
        else:
            a, b = random.randint(11, 99), random.randint(6, 19)
            op = random.choice("×+-")
            if lvl == 3 and random.random() < .4:
                b = random.randint(2, 9)
                a = b * random.randint(11, 60)
                op = "÷"
        if op == "-" and b > a:
            a, b = b, a
        ans = a + b if op == "+" else a - b if op == "-" else a * b if op == "×" else a // b
        opts = {ans}
        while len(opts) < 4:
            d = random.choice([-10, -2, -1, 1, 2, 10, random.randint(3, 9)])
            if ans + d >= 0:
                opts.add(ans + d)
        opts = [str(x) for x in opts]
        random.shuffle(opts)
        qs.append({"q": f"{a} {op} {b} = ?", "options": opts, "answer": opts.index(str(ans)), "explain": ""})
    ST[uid] = {"mode": "play", "quiz": {"qs": qs, "i": 0, "score": 0, "answered": False, "mid": None}}
    send_question(chat, uid)
    return True

# ───────────────────────── TO'LOV ─────────────────────────
def send_requisites(chat, uid, amount):
    card, owner, link = get_set("card"), get_set("card_owner"), get_set("pay_link")
    ST[uid] = {"mode": "receipt", "amount": amount}
    t = (f"💳 <b>To'lov: {amount:,} so'm</b>\n\n".replace(",", " "))
    if card:
        t += f"Karta: <code>{esc(card)}</code>\n"
    if owner:
        t += f"Egasi: {esc(owner)}\n"
    if not card and not link:
        t += "Karta raqami hali kiritilmagan. Admin bilan bog'laning.\n"
    t += "\nTo'lovni amalga oshirib, <b>chek (skrinshot)</b>ni shu yerga rasm qilib yuboring. Admin tasdiqlagach balans to'ldiriladi."
    kb = ik([[{"text": "💳 Payme orqali to'lash", "url": link}]]) if link.startswith("http") else None
    send(chat, t, kb)

def create_payment(chat, uid, m):
    amount = ST[uid]["amount"]
    pid = q("INSERT INTO payments(uid,amount,status,ts) VALUES(?,?,'pending',?)", (uid, amount, int(time.time())))
    pid = q("SELECT MAX(id) m FROM payments", one=True)["m"]
    u = get_user(uid)
    cap = f"🧾 To'lov #{pid}\nFoydalanuvchi: {u['name']} (ID {uid})\nSumma: {amount:,} so'm".replace(",", " ")
    kb = ik([[btn("✅ Tasdiqlash", f"pa:ok:{pid}"), btn("❌ Rad etish", f"pa:no:{pid}")]])
    if m.get("photo"):
        tg("sendPhoto", chat_id=ADMIN_ID, photo=m["photo"][-1]["file_id"], caption=cap, reply_markup=kb)
    else:
        tg("sendDocument", chat_id=ADMIN_ID, document=m["document"]["file_id"], caption=cap, reply_markup=kb)
    ST.pop(uid, None)
    send(chat, "✅ Chek adminga yuborildi. Tasdiqlangach, sizga xabar keladi.")

# ───────────────────────── ADMIN PANEL ─────────────────────────
def is_admin(uid):
    return uid == ADMIN_ID

def admin_kb():
    return ik([[btn("📊 Statistika", "ad:stat"), btn("🛠 Xizmat/narxlar", "ad:svc:0")],
               [btn("🎁 Bepul limit", "ad:free"), btn("💵 Hammasiga narx", "ad:allp")],
               [btn("💳 Rekvizitlar", "ad:card"), btn("🧾 Kutilayotgan to'lovlar", "ad:pay")],
               [btn("👤 Foydalanuvchi", "ad:user"), btn("📢 Xabar yuborish", "ad:bc")],
               [btn("🤖 Gemini model", "ad:model"), btn("🔑 Gemini kalit", "ad:key")]])

def show_admin(chat):
    send(chat, "🛡 <b>Admin panel</b>", admin_kb())

def admin_stats():
    day = int(time.mktime(date.today().timetuple()))
    c = lambda s, a=(): q(s, a, one=True)[0]
    users = c("SELECT COUNT(*) FROM users")
    new = c("SELECT COUNT(*) FROM users WHERE created>=?", (day,))
    active = c("SELECT COUNT(DISTINCT uid) FROM usage WHERE ts>=?", (day,))
    uses = c("SELECT COUNT(*) FROM usage")
    today = c("SELECT COUNT(*) FROM usage WHERE ts>=?", (day,))
    rev = c("SELECT COALESCE(SUM(amount),0) FROM payments WHERE status='ok'")
    spent = c("SELECT COALESCE(SUM(paid),0) FROM usage")
    pend = c("SELECT COUNT(*) FROM payments WHERE status='pending'")
    bal = c("SELECT COALESCE(SUM(balance),0) FROM users")
    top = q("SELECT key, COUNT(*) n FROM usage GROUP BY key ORDER BY n DESC LIMIT 5")
    t = (f"📊 <b>Statistika</b>\n\n👥 Foydalanuvchilar: <b>{users}</b> (bugun +{new})\n🔥 Bugun faol: {active}\n"
         f"⚙️ Foydalanishlar: {uses} (bugun {today})\n💰 Tasdiqlangan to'lovlar: {rev:,} so'm\n"
         f"🧮 Sarflangan: {spent:,} so'm\n👛 Foydalanuvchilar balansi: {bal:,} so'm\n🧾 Kutilayotgan to'lovlar: {pend}\n").replace(",", " ")
    if top:
        t += "\n🏆 <b>Top xizmatlar:</b>\n" + "\n".join(f"{SV[r['key']]['title']} — {r['n']}" for r in top if r["key"] in SV)
    return t

def admin_svc_list(page):
    per = 10
    rows_all = [(s[0], s[2]) for s in SERVICES]
    chunk = rows_all[page * per:(page + 1) * per]
    rows = []
    for k, title in chunk:
        r = svc_row(k)
        rows.append([btn(f"{'✅' if r['enabled'] else '⛔'} {title} — {r['price']}", f"ad:s:{k}")])
    nav = []
    if page > 0:
        nav.append(btn("◀️", f"ad:svc:{page - 1}"))
    nav.append(btn(f"{page + 1}/{(len(rows_all) + per - 1) // per}", "noop"))
    if (page + 1) * per < len(rows_all):
        nav.append(btn("▶️", f"ad:svc:{page + 1}"))
    rows += [nav, [btn("🏠 Admin menyu", "ad:home")]]
    return ik(rows)

def user_card(uid):
    u = get_user(uid)
    if not u:
        return None, None
    n = q("SELECT COUNT(*) c FROM usage WHERE uid=?", (uid,), one=True)["c"]
    t = (f"👤 <b>{esc(u['name'])}</b> @{esc(u['username'])}\nID: <code>{uid}</code>\nBalans: {u['balance']:,} so'm\n"
         f"Bepul: {u['free_left']}\nFoydalanish: {n}\nHolat: {'🚫 bloklangan' if u['banned'] else '✅ faol'}").replace(",", " ")
    kb = ik([[btn("➕ Balans qo'shish/ayirish", f"ad:ub:{uid}"), btn("🎁 +3 bepul", f"ad:uf:{uid}")],
             [btn("🚫 Bloklash/ochish", f"ad:ux:{uid}")], [btn("🏠 Admin menyu", "ad:home")]])
    return t, kb

def do_broadcast(src_chat, src_mid):
    ids = [r["id"] for r in q("SELECT id FROM users WHERE banned=0")]
    ok = 0
    for i in ids:
        r = tg("copyMessage", chat_id=i, from_chat_id=src_chat, message_id=src_mid)
        ok += 1 if r.get("ok") else 0
        time.sleep(0.05)
    send(ADMIN_ID, f"📢 Yuborildi: {ok}/{len(ids)}")

def admin_input(chat, uid, m, text):
    st = ST[uid]
    mode = st["mode"]
    try:
        if mode == "a_price":
            q("UPDATE services SET price=? WHERE key=?", (int(text), st["key"]))
            send(chat, "✅ Narx yangilandi.", admin_kb())
        elif mode == "a_allp":
            v = int(text)
            set_set("default_price", v)
            q("UPDATE services SET price=? WHERE price>0 OR key!='mathblitz'", (v,))
            send(chat, f"✅ Barcha xizmatlar narxi {v} so'm qilindi.", admin_kb())
        elif mode == "a_free":
            set_set("free_uses", int(text))
            send(chat, f"✅ Yangi foydalanuvchilar uchun bepul limit: {int(text)}", admin_kb())
        elif mode == "a_card":
            p = [x.strip() for x in text.split("|")]
            set_set("card", p[0])
            set_set("card_owner", p[1] if len(p) > 1 else "")
            set_set("pay_link", p[2] if len(p) > 2 else "")
            send(chat, "✅ Rekvizitlar saqlandi.", admin_kb())
        elif mode == "a_model":
            set_set("model", text.strip())
            send(chat, f"✅ Model: {esc(text.strip())}", admin_kb())
        elif mode == "a_key":
            set_set("gemini_key", text.strip())
            tg("deleteMessage", chat_id=chat, message_id=m["message_id"])
            send(chat, "✅ Gemini kaliti yangilandi (xabar o'chirildi).", admin_kb())
        elif mode == "a_bc":
            threading.Thread(target=do_broadcast, args=(chat, m["message_id"]), daemon=True).start()
            send(chat, "📢 Yuborish boshlandi…")
        elif mode == "a_user":
            t, kb = user_card(int(text))
            send(chat, t or "Topilmadi.", kb)
        elif mode == "a_bal":
            v = int(text)
            q("UPDATE users SET balance=balance+? WHERE id=?", (v, st["target"]))
            tg("sendMessage", chat_id=st["target"], text=f"💰 Balansingiz {v:+,} so'm o'zgardi.".replace(",", " "))
            send(chat, "✅ Bajarildi.", admin_kb())
    except ValueError:
        send(chat, "⚠️ Noto'g'ri format. Qayta yuboring yoki /cancel.")
        return
    ST.pop(uid, None)

def handle_admin_cb(chat, mid, uid, d):
    p = d.split(":")
    a = p[1]
    if a == "home":
        edit(chat, mid, "🛡 <b>Admin panel</b>", admin_kb())
    elif a == "stat":
        edit(chat, mid, admin_stats(), ik([[btn("🏠 Admin menyu", "ad:home")]]))
    elif a == "svc":
        edit(chat, mid, "🛠 <b>Xizmatlar va narxlar</b>\nTanlang:", admin_svc_list(int(p[2])))
    elif a == "s":
        k = p[2]
        r = svc_row(k)
        edit(chat, mid, f"<b>{SV[k]['title']}</b>\nNarx: {r['price']} so'm\nHolat: {'yoqilgan' if r['enabled'] else 'o`chirilgan'}",
             ik([[btn("💵 Narxni o'zgartirish", f"ad:sp:{k}"), btn("🔁 Yoq/O'chir", f"ad:st:{k}")],
                 [btn("◀️ Ro'yxat", "ad:svc:0")]]))
    elif a == "st":
        q("UPDATE services SET enabled=1-enabled WHERE key=?", (p[2],))
        handle_admin_cb(chat, mid, uid, f"ad:s:{p[2]}")
    elif a == "sp":
        ST[uid] = {"mode": "a_price", "key": p[2]}
        send(chat, "Yangi narxni so'mda yozing (0 — bepul):")
    elif a == "allp":
        ST[uid] = {"mode": "a_allp"}
        send(chat, "Barcha pullik xizmatlar uchun yangi narxni yozing (so'm):")
    elif a == "free":
        ST[uid] = {"mode": "a_free"}
        send(chat, f"Yangi foydalanuvchilar uchun bepul urinishlar soni (hozir {get_set('free_uses', FREE_USES)}):")
    elif a == "card":
        ST[uid] = {"mode": "a_card"}
        send(chat, "Format:\n<code>karta raqami | karta egasi | Payme havola (ixtiyoriy)</code>\nMasalan:\n"
                   "<code>8600 1234 5678 9012 | Behruz Z. | https://payme.uz/...</code>")
    elif a == "pay":
        rows = q("SELECT * FROM payments WHERE status='pending' ORDER BY id DESC LIMIT 10")
        if not rows:
            send(chat, "Kutilayotgan to'lovlar yo'q.")
        for r in rows:
            send(chat, f"🧾 #{r['id']} • ID {r['uid']} • {r['amount']} so'm",
                 ik([[btn("✅ Tasdiqlash", f"pa:ok:{r['id']}"), btn("❌ Rad etish", f"pa:no:{r['id']}")]]))
    elif a == "user":
        ST[uid] = {"mode": "a_user"}
        send(chat, "Foydalanuvchi ID raqamini yozing:")
    elif a == "ub":
        ST[uid] = {"mode": "a_bal", "target": int(p[2])}
        send(chat, "Summani yozing (+ qo'shish, − ayirish uchun minus bilan):")
    elif a == "uf":
        q("UPDATE users SET free_left=free_left+3 WHERE id=?", (int(p[2]),))
        send(chat, "✅ +3 bepul urinish berildi.")
    elif a == "ux":
        q("UPDATE users SET banned=1-banned WHERE id=?", (int(p[2]),))
        t, kb = user_card(int(p[2]))
        send(chat, t, kb)
    elif a == "bc":
        ST[uid] = {"mode": "a_bc"}
        send(chat, "Barcha foydalanuvchilarga yuboriladigan xabarni yuboring (matn, rasm yoki boshqa):")
    elif a == "model":
        ST[uid] = {"mode": "a_model"}
        send(chat, f"Hozirgi model: <code>{esc(get_set('model', DEFAULT_MODEL))}</code>\nYangi model nomini yozing (masalan gemini-3.8-flash):")
    elif a == "key":
        ST[uid] = {"mode": "a_key"}
        send(chat, "Yangi Gemini API kalitini yuboring (xabar darhol o'chiriladi):")

# ───────────────────────── XABAR / CALLBACK ─────────────────────────
def handle_message(m):
    if m["chat"]["type"] != "private":
        return
    chat = m["chat"]["id"]
    frm = m.get("from", {})
    uid = frm.get("id")
    name = frm.get("first_name", "Foydalanuvchi")
    u = ensure_user(uid, name, frm.get("username"))
    if u["banned"] and not is_admin(uid):
        return
    text = (m.get("text") or m.get("caption") or "").strip()
    st = ST.get(uid, {})
    mode = st.get("mode")

    if text.startswith("/"):
        cmd = text.split()[0].split("@")[0].lower()
        ST.pop(uid, None)
        if cmd == "/start":
            welcome(chat, uid, name)
        elif cmd in ("/menu", "/cancel"):
            send(chat, "🏠 Bosh menyu", main_kb())
        elif cmd == "/admin" and is_admin(uid):
            show_admin(chat)
        elif cmd == "/balans":
            show_balance(chat, uid)
        elif cmd == "/help":
            show_help(chat)
        return

    if text in CAT_BY_LABEL:
        ST.pop(uid, None)
        show_cat(chat, CAT_BY_LABEL[text])
        return
    if text == "💰 Balans":
        ST.pop(uid, None)
        show_balance(chat, uid)
        return
    if text == "👤 Profil":
        show_profile(chat, uid)
        return
    if text == "ℹ️ Yordam":
        show_help(chat)
        return

    if mode and mode.startswith("a_") and is_admin(uid):
        admin_input(chat, uid, m, text)
        return
    if mode == "receipt" and (m.get("photo") or m.get("document")):
        create_payment(chat, uid, m)
        return
    if mode == "input":
        sv = SV[st["key"]]
        if sv["kind"] == "vision":
            fid, mime = None, "image/jpeg"
            if m.get("photo"):
                fid = m["photo"][-1]["file_id"]
            elif m.get("document"):
                fid, mime = m["document"]["file_id"], m["document"].get("mime_type", "application/pdf")
            if not fid:
                send(chat, "📎 Iltimos, rasm yoki PDF fayl yuboring.")
                return
            threading.Thread(target=run_vision, args=(uid, chat, fid, mime, text), daemon=True).start()
            return
        if not text:
            send(chat, "✍️ Iltimos, matn yuboring.")
            return
        st["inp"] = text[:6000]
        ask_options(chat, uid)
        return
    send(chat, "Pastdagi tugmalardan bo'lim tanlang 👇", main_kb())

def run_vision(uid, chat, fid, mime, caption):
    sv = SV["rasm"]
    if uid in BUSY:
        send(chat, "⏳ Oldingi so'rov bajarilmoqda.")
        return
    if not can_use(uid, "rasm"):
        need_money(chat, "rasm")
        return
    BUSY.add(uid)
    wait = send(chat, "⏳ Fayl tahlil qilinmoqda…")
    ok = False
    try:
        data, path = tg_file(fid)
        if path.lower().endswith(".pdf"):
            mime = "application/pdf"
        elif path.lower().endswith(".png"):
            mime = "image/png"
        elif path.lower().endswith(".webp"):
            mime = "image/webp"
        out = gemini(sv["prompt"].replace("{inp}", caption or "Mazmunini tushuntir va asosiy fikrlarni ajrat."),
                     extra_parts=[{"inline_data": {"mime_type": mime, "data": base64.b64encode(data).decode()}}])
        send(chat, clean(out), plain=True)
        ok = True
    except Exception as e:
        log("vision", e)
        send(chat, "😕 Faylni tahlil qilib bo'lmadi. Pul yechilmadi.")
    finally:
        BUSY.discard(uid)
        ST.pop(uid, None)
    if wait:
        tg("deleteMessage", chat_id=chat, message_id=wait)
    if ok:
        note = charge(uid, "rasm")
        if note:
            send(chat, note)

def handle_callback(c):
    cid = c["id"]
    uid = c["from"]["id"]
    msg = c.get("message")
    d = c.get("data", "")
    tg("answerCallbackQuery", callback_query_id=cid)
    if not msg:
        return
    chat, mid = msg["chat"]["id"], msg["message_id"]
    u = ensure_user(uid, c["from"].get("first_name", ""), c["from"].get("username"))
    if u["banned"] and not is_admin(uid):
        return
    if d == "noop":
        return
    if d == "cancel":
        ST.pop(uid, None)
        edit(chat, mid, "❌ Bekor qilindi.")
    elif d == "bal":
        show_balance(chat, uid)
    elif d.startswith("cat:"):
        ST.pop(uid, None)
        show_cat(chat, d[4:])
    elif d.startswith("sv:"):
        if d[3:] in SV:
            start_service(chat, uid, d[3:])
    elif d.startswith("ds:"):
        st = ST.get(uid)
        if not st or st.get("mode") != "options":
            return
        key, inp = st["key"], st.get("inp", "")
        ST.pop(uid, None)
        edit(chat, mid, "✅ Tanlandi.")
        threading.Thread(target=run_service, args=(uid, chat, key, inp, int(d[3:])), daemon=True).start()
    elif d.startswith("qa:"):
        _, i, j = d.split(":")
        on_answer(chat, mid, uid, int(i), int(j))
    elif d == "qn":
        on_next(chat, mid, uid)
    elif d.startswith("fc:"):
        _, act, i = d.split(":")
        on_card(chat, mid, uid, act, int(i))
    elif d.startswith("hg:"):
        on_hang(chat, mid, uid, d[3:])
    elif d.startswith("tp:"):
        send_requisites(chat, uid, int(d[3:]))
    elif d.startswith("pa:") and is_admin(uid):
        _, act, pid = d.split(":")
        r = q("SELECT * FROM payments WHERE id=?", (int(pid),), one=True)
        if not r or r["status"] != "pending":
            send(chat, "Bu to'lov allaqachon ko'rib chiqilgan.")
            return
        if act == "ok":
            q("UPDATE payments SET status='ok' WHERE id=?", (r["id"],))
            q("UPDATE users SET balance=balance+? WHERE id=?", (r["amount"], r["uid"]))
            tg("sendMessage", chat_id=r["uid"], text=f"✅ To'lov tasdiqlandi! Balansingizga {r['amount']:,} so'm qo'shildi.".replace(",", " "))
            send(chat, f"✅ #{r['id']} tasdiqlandi.")
        else:
            q("UPDATE payments SET status='no' WHERE id=?", (r["id"],))
            tg("sendMessage", chat_id=r["uid"], text="❌ To'lovingiz tasdiqlanmadi. Muammo bo'lsa, admin bilan bog'laning.")
            send(chat, f"❌ #{r['id']} rad etildi.")
        tg("editMessageReplyMarkup", chat_id=chat, message_id=mid, reply_markup={"inline_keyboard": []})
    elif d.startswith("ad:") and is_admin(uid):
        handle_admin_cb(chat, mid, uid, d)

def safe_handle(u):
    try:
        if "message" in u:
            handle_message(u["message"])
        elif "callback_query" in u:
            handle_callback(u["callback_query"])
    except Exception:
        log(traceback.format_exc())

def main():
    if not BOT_TOKEN:
        print("❌ BOT_TOKEN topilmadi. Masalan:  export BOT_TOKEN='...'  yoki .env fayl yarating.")
        sys.exit(1)
    if not GEMINI_KEY and not get_set("gemini_key"):
        print("⚠️ GEMINI_KEY topilmadi — admin paneldan kiritishingiz mumkin.")
    init_db()
    me = tg("getMe")
    if not me.get("ok"):
        print("❌ Token noto'g'ri yoki internet yo'q:", me)
        sys.exit(1)
    log("Bot ishga tushdi: @" + me["result"]["username"], "| xizmatlar:", len(SERVICES))
    tg("deleteWebhook")
    tg("setMyCommands", commands=[{"command": "start", "description": "Boshlash"},
                                  {"command": "menu", "description": "Bosh menyu"},
                                  {"command": "balans", "description": "Balans"},
                                  {"command": "help", "description": "Yordam"},
                                  {"command": "cancel", "description": "Bekor qilish"}])
    pool = ThreadPoolExecutor(16)
    offset = 0
    while True:
        try:
            r = tg("getUpdates", offset=offset, timeout=30, allowed_updates=["message", "callback_query"])
            if not r.get("ok"):
                time.sleep(3)
                continue
            for u in r["result"]:
                offset = u["update_id"] + 1
                pool.submit(safe_handle, u)
        except KeyboardInterrupt:
            log("To'xtatildi")
            break
        except Exception:
            log(traceback.format_exc())
            time.sleep(3)

if __name__ == "__main__":
    main()
