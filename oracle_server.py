"""
Sychos Hub — Oracle Server v3.0
Zentraler Server: Login & Register (50 Start-Credits), Credit-System (2-4 dynamisch),
KI-Chat, Chat-Verwaltung (inkl. Umbenennen), Admin-API (Online-User, Credits vergeben).
Serviert zudem das Frontend:  /        -> Sychos Hub   /admin/  -> Admin-Panel
Nur Python-Stdlib. Start:  python oracle_server.py
"""
import os, sys, json, time, uuid, threading, mimetypes, zlib, base64
import sqlite3, urllib.request, urllib.parse, hashlib, hmac, re, math
from http.server import HTTPServer, BaseHTTPRequestHandler
try:
    from http.server import ThreadingHTTPServer as HTTPServerCls
except ImportError:
    HTTPServerCls = HTTPServer
from urllib.parse import urlparse, unquote

# pythonw (versteckter Always-On-Modus) hat kein stdout/stderr -> auf devnull
if getattr(sys, "stdout", None) is None:
    sys.stdout = open(os.devnull, "w")
if getattr(sys, "stderr", None) is None:
    sys.stderr = open(os.devnull, "w")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOST = os.environ.get("ORACLE_HOST", "0.0.0.0")
PORT = int(os.environ.get("ORACLE_PORT", "7777"))
DB_PATH = os.environ.get("ORACLE_DB", os.path.join(os.path.dirname(os.path.abspath(__file__)), "sychos.db"))
# Provider-Keys: fest im Server hinterlegt (kein manuelles Eintragen noetig).
# Env GEMINI_API_KEY / GROQ_API_KEY / CLINE_API_KEY koennen sie bei Bedarf ueberschreiben.
# NIEMALS im Frontend, in Logs oder Fehlermeldungen ausgeben.
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "AQ.Ab8RN6JC_fpFIoudZSPJyi0oLHbEpFZXt-PdJPBDobUt76TtOQ")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "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")
CLINE_API_KEY = os.environ.get("CLINE_API_KEY", "sk_44b7a08f5bbad92391cf22535ff37373db50457d90979e1df3e5d992fb835381")

START_CREDITS = 50.0
START_TOKENS = 25000.0   # Start-Bonus-Tokens fuer neue Accounts
# Passive Gutschrift: alle 7 Stunden bekommen alle User +25 Credits
CREDIT_REGEN_SECONDS = 7 * 3600
CREDIT_REGEN_AMOUNT = 25.0
ONLINE_WINDOW = 300
MAX_MSG_LEN = 8000
MAX_TITLE_LEN = 80
SEND_RATE_LIMIT = 20
LOGIN_RATE_LIMIT = 10
MAX_ACCOUNTS_PER_IP = int(os.environ.get("MAX_ACCOUNTS_PER_IP", "3"))

# Zentrale Credit-Preise je KI-Stufe (serverseitig verbindlich)
STRENGTHS = {
    "low":    {"name": "Low",    "max_tokens": 512,  "temp": 0.3, "cost": 1},
    "medium": {"name": "Medium", "max_tokens": 2048, "temp": 0.7, "cost": 2},
    "high":   {"name": "High",   "max_tokens": 4096, "temp": 1.0, "cost": 4},
    "extra":  {"name": "Extra",  "max_tokens": 8192, "temp": 1.3, "cost": 6},
}

# KI-Modelle: Anbieter + Kostenfaktor (Preis = Stufen-Cost x Faktor)
MODELS = {
    "gemini-3.6-flash": {"name": "Gemini 3.6 Flash", "provider": "gemini", "factor": 1.0},
    "gemini-2.5-flash": {"name": "Gemini 2.5 Flash", "provider": "gemini", "factor": 1.0},
    "gemini-2.5-lite":  {"name": "Gemini 2.5 Lite (schnell)", "provider": "gemini", "factor": 0.6},
    "gemini-3.6-pro":   {"name": "Gemini 3.6 Pro", "provider": "gemini", "factor": 1.5, "paid": True},
    "gpt-oss-120b":     {"name": "GPT-OSS 120B", "provider": "groq", "factor": 1.0,
                         "api_model": "openai/gpt-oss-120b"},
    "gpt-oss-20b":      {"name": "GPT-OSS 20B", "provider": "groq", "factor": 0.7,
                         "api_model": "openai/gpt-oss-20b"},
    "qwen3-27b":        {"name": "Qwen3 27B", "provider": "groq", "factor": 0.9,
                         "api_model": "qwen/qwen3.8-27b"},
    "llama-3.3-70b":    {"name": "Llama 3.3 70B", "provider": "groq", "factor": 0.8,
                         "api_model": "llama-3.3-70b-versatile"},
    "llama-3.1-8b":     {"name": "Llama 3.1 8B (schnell)", "provider": "groq", "factor": 0.4,
                         "api_model": "llama-3.1-8b-instant"},
    "qwen3-32b":        {"name": "Qwen3 32B", "provider": "groq", "factor": 0.8,
                         "api_model": "qwen/qwen3-32b"},
    "deepseek-r1-70b":  {"name": "DeepSeek R1 70B", "provider": "groq", "factor": 0.9,
                         "api_model": "deepseek-r1-distill-llama-70b"},
    "kimi-k2":          {"name": "Kimi K2", "provider": "groq", "factor": 1.0, "paid": True,
                         "api_model": "moonshotai/kimi-k2-instruct"},
    "nemotron-ultra":   {"name": "Nemotron Ultra 550B", "provider": "cline", "factor": 0.6,
                         "api_model": "nvidia/nemotron-3-ultra-550b-a55b:free"},
    "nemotron-super":   {"name": "Nemotron Super 120B", "provider": "cline", "factor": 0.5,
                         "api_model": "nvidia/nemotron-3-super-120b-a12b:free"},
    "qwen3-27b-free":   {"name": "Qwen3.8 27B", "provider": "cline", "factor": 0.4,
                         "api_model": "qwen/qwen3.8-27b:free"},
    "gemma-4-31b":      {"name": "Gemma 4 31B", "provider": "cline", "factor": 0.3,
                         "api_model": "google/gemma-4-31b-it:free"},
    "north-mini-code":  {"name": "North Mini Code", "provider": "cline", "factor": 0.3,
                         "api_model": "cohere/north-mini-code:free"},
    "llama-3.3-70b-free": {"name": "Llama 3.3 70B (free)", "provider": "cline", "factor": 0.5,
                         "api_model": "meta-llama/llama-3.3-70b-instruct:free"},
    "deepseek-v3-free": {"name": "DeepSeek V3 (free)", "provider": "cline", "factor": 0.5,
                         "api_model": "deepseek/deepseek-chat-v3-0324:free"},
    "deepseek-r1-free": {"name": "DeepSeek R1 (free)", "provider": "cline", "factor": 0.6,
                         "api_model": "deepseek/deepseek-r1-0528:free"},
    "qwen3-235b-free":  {"name": "Qwen3 235B (free)", "provider": "cline", "factor": 0.7, "paid": True,
                         "api_model": "qwen/qwen3-235b-a22b:free"},
    "mistral-small-free": {"name": "Mistral Small 3.1 (free)", "provider": "cline", "factor": 0.4,
                         "api_model": "mistralai/mistral-small-3.1-24b-instruct:free"},
    "phi-4-free":       {"name": "Phi-4 (free)", "provider": "cline", "factor": 0.3,
                         "api_model": "microsoft/phi-4:free"},
    "gemma-3-27b-free": {"name": "Gemma 3 27B (free)", "provider": "cline", "factor": 0.3,
                         "api_model": "google/gemma-3-27b-it:free"},
    "mimo-v26-pro":     {"name": "MiMo v2.6 Pro", "provider": "cline", "factor": 0.7, "paid": True,
                         "api_model": "cline-pass/mimo-v2.6-pro"},
    "mimo-v26-flash":   {"name": "MiMo v2.6 Flash", "provider": "cline", "factor": 0.5, "paid": True,
                         "api_model": "cline-pass/mimo-v2.6-flash"},
    "kimi-k3":          {"name": "Kimi K3", "provider": "cline", "factor": 1.0, "paid": True,
                         "api_model": "cline-pass/kimi-k3"},
    "deepseek-v4-pro":  {"name": "DeepSeek V4 Pro", "provider": "cline", "factor": 0.8, "paid": True,
                         "api_model": "cline-pass/deepseek-v4-pro"},
    "glm-53":           {"name": "GLM 5.3", "provider": "cline", "factor": 0.8, "paid": True,
                         "api_model": "cline-pass/glm-5.3"},
}

# ── Token- & Plan-System (wie Cline): Nutzung in Tokens, Limits in 3 Fenstern ──
TOKEN_WINDOWS = {
    "5h":    {"seconds": 5 * 3600,       "label": "5 Stunden"},
    "week":  {"seconds": 7 * 24 * 3600,  "label": "Woche"},
    "month": {"seconds": 30 * 24 * 3600, "label": "Monat"},
}
PLANS = {
    "free":     {"name": "Free",     "price": 0.0,    "desc": "Zum Ausprobieren",
                 "t5": 40000,    "tweek": 250000,    "tmonth": 750000},
    "basic":    {"name": "Basic",    "price": 6.99,   "desc": "Fuer den Einstieg",
                 "t5": 150000,   "tweek": 1200000,   "tmonth": 4000000},
    "pro":      {"name": "Pro",      "price": 14.99,  "desc": "Fuer jeden Tag",
                 "t5": 400000,   "tweek": 3500000,   "tmonth": 12000000},
    "max":      {"name": "Max",      "price": 39.99,  "desc": "Viel los",
                 "t5": 1200000,  "tweek": 10000000,  "tmonth": 40000000},
    "ultra":    {"name": "Ultra",    "price": 99.99,  "desc": "Fast ohne Grenzen",
                 "t5": 3000000,  "tweek": 25000000,  "tmonth": 100000000},
    "business": {"name": "Business", "price": 199.99, "desc": "Fuer Teams & Firmen",
                 "t5": 8000000,  "tweek": 60000000,  "tmonth": 250000000},
}
PLAN_ORDER = ["free", "basic", "pro", "max", "ultra", "business"]
PLAN_DAYS = 30   # ein gekaufter Plan gilt 30 Tage (monatlich kuendbar)

def effective_plan(user):
    """Aktiver Plan: gekaufter Plan (bis plan_until) > Admin-Paid (= Pro) > free."""
    if not user:
        return "free"
    p = user.get("plan") or "free"
    if p != "free":
        until = user.get("plan_until", 0) or 0
        if until and time.time() > until:
            p = "free"
        else:
            return p
    paid_until = user.get("paid_until", 0) or 0
    if user.get("is_paid") and (not paid_until or time.time() <= paid_until):
        return "pro"
    return "free"

def plan_limits(plan):
    p = PLANS.get(plan, PLANS["free"])
    return {"5h": p["t5"], "week": p["tweek"], "month": p["tmonth"]}

def usage_state(uid, plan):
    """Drei Fenster (5h / Woche / Monat): Verbrauch, Limit und Reset-Zeit."""
    now = time.time()
    lim = plan_limits(plan)
    out = {}
    db = get_db()
    brow = db.execute("SELECT bonus_tokens FROM users WHERE uid=?", (uid,)).fetchone()
    bonus = (brow["bonus_tokens"] if brow else 0) or 0
    for win, info in TOKEN_WINDOWS.items():
        row = db.execute("SELECT start, tokens FROM usage WHERE uid=? AND win=?", (uid, win)).fetchone()
        start = (row["start"] if row else 0) or 0
        used = (row["tokens"] if row else 0) or 0
        if not start or now - start >= info["seconds"]:
            start, used = now, 0.0
            db.execute("INSERT OR REPLACE INTO usage (uid,win,start,tokens) VALUES (?,?,?,?)",
                       (uid, win, start, used))
        out[win] = {"used": used, "limit": lim[win] + bonus,
                    "remaining": max(0.0, lim[win] + bonus - used),
                    "resets_at": start + info["seconds"]}
    db.commit(); db.close()
    return out

def add_usage(uid, tokens):
    """Verbrauchte Tokens in alle drei Fenster eintragen (Fenster laufen automatisch ab)."""
    now = time.time()
    db = get_db()
    for win, info in TOKEN_WINDOWS.items():
        row = db.execute("SELECT start, tokens FROM usage WHERE uid=? AND win=?", (uid, win)).fetchone()
        start = (row["start"] if row else 0) or 0
        used = (row["tokens"] if row else 0) or 0
        if not start or now - start >= info["seconds"]:
            start, used = now, 0.0
        db.execute("INSERT OR REPLACE INTO usage (uid,win,start,tokens) VALUES (?,?,?,?)",
                   (uid, win, start, used + max(0.0, float(tokens))))
    db.commit(); db.close()

def est_tokens(history):
    """Grobe Vorab-Schaetzung (Input + Reserve fuer die Antwort)."""
    chars = sum(len(m.get("content") or "") for m in history)
    return int(chars / 4) + 900

def limit_block(uid, plan, est):
    """None = ok, sonst (win, state) des ersten ueberzogenen Fensters."""
    st = usage_state(uid, plan)
    for win in ("5h", "week", "month"):
        if st[win]["used"] + est > st[win]["limit"]:
            return win, st[win], st
    return None

def fmt_wait(seconds):
    seconds = max(0, int(seconds))
    h, m = seconds // 3600, (seconds % 3600) // 60
    if h:
        return "%d Std %d Min" % (h, m)
    return "%d Min" % max(1, m)

def strength_cost(strength, model_id):
    s = STRENGTHS.get(strength, STRENGTHS["medium"])
    f = MODELS.get(model_id, {}).get("factor", 1.0)
    return max(1, int(round(s["cost"] * f)))

request_times = []
LOAD_LOCK = threading.Lock()

def note_request():
    with LOAD_LOCK:
        now = time.time()
        recent = [t for t in request_times if now - t < 60]
        recent.append(now)
        request_times[:] = recent

def load_factor():
    """Auslastungs-Aufschlag: viele Anfragen pro Minute -> hoehere Kosten."""
    with LOAD_LOCK:
        n = len(request_times)
    if n >= 240:
        return 2.5
    if n >= 120:
        return 2.0
    if n >= 60:
        return 1.5
    if n >= 30:
        return 1.25
    return 1.0

def message_cost(strength, model_id):
    """Endgueltiger Credit-Preis inkl. Auslastungs-Aufschlag."""
    return max(1, int(round(strength_cost(strength, model_id) * load_factor())))

RATE = {}
RATE_LOCK = threading.Lock()

def rate_ok(key, limit, window=60):
    now = time.time()
    with RATE_LOCK:
        lst = [t for t in RATE.get(key, []) if now - t < window]
        if len(lst) >= limit:
            RATE[key] = lst
            return False
        lst.append(now)
        RATE[key] = lst
        return True

START_TIME = time.time()
# ═══════════════════════════════════════════════════════════
#  DATABASE
# ═══════════════════════════════════════════════════════════
def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn

def hash_pw(pw):
    salt = os.urandom(16).hex()
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 100000)
    return "pbkdf2$%s$%s" % (salt, dk.hex())

def verify_pw(pw, stored):
    if stored.startswith("pbkdf2$"):
        _, salt, dk = stored.split("$", 2)
        calc = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 100000).hex()
        return hmac.compare_digest(calc, dk)
    return hmac.compare_digest(hashlib.sha256(pw.encode()).hexdigest(), stored)

def new_token():
    return uuid.uuid4().hex + os.urandom(16).hex()

def init_db():
    db = get_db()
    db.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            uid TEXT PRIMARY KEY,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            display_name TEXT DEFAULT '',
            credits REAL DEFAULT 50.0,
            is_banned INTEGER DEFAULT 0,
            ban_reason TEXT DEFAULT '',
            ban_until REAL DEFAULT 0,
            is_admin INTEGER DEFAULT 0,
            is_paid INTEGER DEFAULT 0,
            reg_ip TEXT DEFAULT '',
            created_at REAL DEFAULT 0,
            last_login REAL DEFAULT 0,
            last_seen REAL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS chats (
            id TEXT PRIMARY KEY,
            uid TEXT NOT NULL,
            title TEXT DEFAULT 'Neuer Chat',
            model TEXT DEFAULT 'gemini-3.6-flash',
            created_at REAL DEFAULT 0,
            FOREIGN KEY (uid) REFERENCES users(uid)
        );
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            model TEXT DEFAULT '',
            tokens INTEGER DEFAULT 0,
            cost REAL DEFAULT 0,
            created_at REAL DEFAULT 0,
            FOREIGN KEY (chat_id) REFERENCES chats(id)
        );
        CREATE TABLE IF NOT EXISTS sessions (
            token TEXT PRIMARY KEY,
            uid TEXT NOT NULL,
            created_at REAL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS user_keys (
            uid TEXT NOT NULL,
            provider TEXT NOT NULL,
            api_key TEXT NOT NULL,
            created_at REAL DEFAULT 0,
            PRIMARY KEY (uid, provider)
        );
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        );
    """)
    row = db.execute("SELECT uid FROM users WHERE is_admin=1").fetchone()
    if not row:
        uid = str(uuid.uuid4())
        # Admin-Passwort: Env ORACLE_ADMIN_PASSWORD, sonst fest "57455745"
        pw = os.environ.get("ORACLE_ADMIN_PASSWORD", "57455745")
        db.execute("INSERT INTO users (uid,email,password_hash,display_name,credits,is_admin,created_at) VALUES (?,?,?,?,?,?,?)",
                   (uid, "admin@sychos.net", hash_pw(pw), "Sychos", 99999, 1, time.time()))
        db.commit()
    db.execute("""
        CREATE TABLE IF NOT EXISTS usage (
            uid TEXT NOT NULL,
            win TEXT NOT NULL,
            start REAL DEFAULT 0,
            tokens REAL DEFAULT 0,
            PRIMARY KEY (uid, win)
        );""")
    for col in ("last_login REAL DEFAULT 0", "last_seen REAL DEFAULT 0",
                "ban_reason TEXT DEFAULT ''", "ban_until REAL DEFAULT 0",
                "is_paid INTEGER DEFAULT 0", "reg_ip TEXT DEFAULT ''",
                "paid_until REAL DEFAULT 0", "last_credit REAL DEFAULT 0",
                "plan TEXT DEFAULT 'free'", "plan_until REAL DEFAULT 0",
                "bonus_tokens REAL DEFAULT 0"):
        try:
            db.execute("ALTER TABLE users ADD COLUMN " + col)
            db.commit()
        except Exception:
            pass
    db.close()

def touch_user(uid):
    """Markiert einen User als aktiv (online)."""
    db = get_db()
    db.execute("UPDATE users SET last_seen=? WHERE uid=?", (time.time(), uid))
    db.commit(); db.close()

# ═══════════════════════════════════════════════════════════
#  KI-PROVIDER
# ═══════════════════════════════════════════════════════════
def mask_key(k):
    """Key unsichtbar machen (fuer Statusanzeigen)."""
    if not k:
        return ""
    return "*" * 8 + k[-4:]

def get_setting(db, key):
    row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else ""

def resolve_key(db, uid, provider):
    """Key-Auflösung: Admin-Settings (DB) > Env > fest im Code hinterlegter Default."""
    db_key = get_setting(db, provider + "_api_key")
    if db_key:
        return db_key
    return {"gemini": GEMINI_API_KEY, "groq": GROQ_API_KEY, "cline": CLINE_API_KEY}.get(provider, "")

def web_search(query):
    # Kostenlose Websuche (DuckDuckGo + Wikipedia) fuer KI-Kontext. Kein Key noetig.
    out = []
    try:
        url = "https://api.duckduckgo.com/?q=" + urllib.parse.quote(query) + "&format=json&no_html=1&skip_disambig=1"
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=10) as resp:
            d = json.loads(resp.read())
        if d.get("AbstractText"):
            out.append("- " + d["AbstractText"][:400] + " (Quelle: " + d.get("AbstractURL", "") + ")")
        for t in (d.get("RelatedTopics") or [])[:5]:
            if isinstance(t, dict) and t.get("Text"):
                out.append("- " + t["Text"][:300])
    except Exception:
        pass
    if len(out) < 2:
        try:
            url = ("https://de.wikipedia.org/w/api.php?action=query&list=search&srsearch="
                   + urllib.parse.quote(query) + "&format=json&utf8=1&srlimit=5")
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=10) as resp:
                d = json.loads(resp.read())
            for r in (d.get("query", {}).get("search") or []):
                snip = re.sub("<[^>]+>", "", r.get("snippet", ""))
                out.append("- " + r.get("title", "") + ": " + snip[:300])
        except Exception:
            pass
    return out

def call_gemini(messages, api_key, strength="medium", model="gemini-3.6-flash"):
    """Echter Gemini-Aufruf. Kein Demo-/Mock-Fallback."""
    if not api_key:
        return {"ok": False, "error_type": "missing_key",
                "error": "Kein Gemini-API-Key hinterlegt. Bitte in den Einstellungen einen Key hinterlegen."}
    s = STRENGTHS.get(strength, STRENGTHS["medium"])
    contents = []
    for m in messages:
        role = "user" if m["role"] == "user" else "model"
        contents.append({"role": role, "parts": [{"text": m["content"]}]})
    payload = json.dumps({
        "contents": contents,
        "generationConfig": {"maxOutputTokens": s["max_tokens"], "temperature": s["temp"]},
    }).encode()
    url = ("https://generativelanguage.googleapis.com/v1beta/models/"
           + model + ":generateContent?key=" + api_key)
    req = urllib.request.Request(url, data=payload,
        headers={"Content-Type": "application/json",
                 "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read())
            cand = data.get("candidates") or [{}]
            parts = (cand[0].get("content") or {}).get("parts") or []
            text = "".join(p.get("text", "") for p in parts)
            if not text.strip():
                return {"ok": False, "error_type": "api",
                        "error": "Gemini hat eine leere Antwort geliefert."}
            tokens = data.get("usageMetadata", {}).get("totalTokenCount", 0)
            return {"ok": True, "text": text, "tokens": tokens}
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return {"ok": False, "error_type": "invalid_key",
                    "error": "Gemini-API-Key ist ungueltig oder gesperrt. Bitte Key pruefen."}
        if e.code == 429:
            return {"ok": False, "error_type": "rate_limit",
                    "error": "Gemini-Rate-Limit erreicht. Bitte kurz warten und erneut versuchen."}
        return {"ok": False, "error_type": "api",
                "error": "Gemini-Fehler (HTTP %d). Bitte spaeter erneut versuchen." % e.code}
    except Exception:
        return {"ok": False, "error_type": "api",
                "error": "Gemini nicht erreichbar. Bitte spaeter erneut versuchen."}

def call_groq(messages, api_key, strength="medium", model="llama-3.3-70b-versatile"):
    """Echter Groq-Aufruf (OpenAI-kompatibel). Kein Demo-/Mock-Fallback."""
    if not api_key:
        return {"ok": False, "error_type": "missing_key",
                "error": "Kein Groq-API-Key hinterlegt. Bitte in den Einstellungen einen Key hinterlegen."}
    s = STRENGTHS.get(strength, STRENGTHS["medium"])
    msgs = [{"role": ("user" if m["role"] == "user" else "assistant"), "content": m["content"]}
            for m in messages]
    effort = {"low": "low", "medium": "medium", "high": "high", "extra": "high"}.get(strength, "medium")
    payload = json.dumps({
        "model": model, "messages": msgs,
        "max_tokens": s["max_tokens"] + 512, "temperature": s["temp"],
        "reasoning_effort": effort,
    }).encode()
    req = urllib.request.Request("https://api.groq.com/openai/v1/chat/completions",
        data=payload, method="POST", headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + api_key,
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
            "Accept": "application/json",
        })
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read())
            msg = (data["choices"][0].get("message") or {})
            text = msg.get("content") or ""
            if not text.strip():
                return {"ok": False, "error_type": "api",
                        "error": "Groq hat eine leere Antwort geliefert. Bitte erneut versuchen."}
            tokens = data.get("usage", {}).get("total_tokens", 0)
            return {"ok": True, "text": text, "tokens": tokens}
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return {"ok": False, "error_type": "invalid_key",
                    "error": "Groq-API-Key ist ungueltig oder gesperrt. Bitte Key pruefen."}
        if e.code == 429:
            return {"ok": False, "error_type": "rate_limit",
                    "error": "Groq-Rate-Limit erreicht. Bitte kurz warten und erneut versuchen."}
        return {"ok": False, "error_type": "api",
                "error": "Groq-Fehler (HTTP %d). Bitte spaeter erneut versuchen." % e.code}
    except Exception:
        return {"ok": False, "error_type": "api",
                "error": "Groq nicht erreichbar. Bitte spaeter erneut versuchen."}

def call_cline(messages, api_key, strength="medium", model="anthropic/claude-sonnet-4.6"):
    """Echter Cline-Aufruf (OpenAI-kompatibel, api.cline.bot)."""
    if not api_key:
        return {"ok": False, "error_type": "missing_key",
                "error": "Kein Cline-API-Key hinterlegt (Admin-Panel -> API-Keys)."}
    s = STRENGTHS.get(strength, STRENGTHS["medium"])
    msgs = [{"role": ("user" if m["role"] == "user" else "assistant"), "content": m["content"]}
            for m in messages]
    # Free-Modelle lehnen "temperature" ab (HTTP 500) – nur Basis-Parameter senden
    payload = json.dumps({
        "model": model, "messages": msgs,
        "max_tokens": s["max_tokens"] + 512,
    }).encode()
    def _do_call():
        req2 = urllib.request.Request("https://api.cline.bot/api/v1/chat/completions",
            data=payload, method="POST", headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + api_key,
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
                "HTTP-Referer": "https://sychos.hub",
                "X-Title": "Sychos",
            })
        with urllib.request.urlopen(req2, timeout=90) as resp:
            data = json.loads(resp.read())
        ch = data.get("choices") or (data.get("data") or {}).get("choices") or []
        msg = (ch[0].get("message") or {}) if ch else {}
        text = msg.get("content") or ""
        return text, data.get("usage", {}).get("total_tokens", 0)

    try:
        text, tokens = _do_call()
        if not text.strip():
            return {"ok": False, "error_type": "api",
                    "error": "Cline hat eine leere Antwort geliefert."}
        return {"ok": True, "text": text, "tokens": tokens}
    except urllib.error.HTTPError as e:
        if e.code == 500:
            # Free-Tier-Flakiness: ein automatischer Wiederholungsversuch
            try:
                time.sleep(1.5)
                text, tokens = _do_call()
                if text.strip():
                    return {"ok": True, "text": text, "tokens": tokens}
            except Exception:
                pass
            return {"ok": False, "error_type": "api",
                    "error": "Free-Modell voruebergehend ueberlastet. Bitte erneut versuchen."}
        if e.code == 402:
            return {"ok": False, "error_type": "insufficient_credits",
                    "error": "Cline-Guthaben aufgebraucht (Cline Credits). Bitte unter app.cline.bot aufladen."}
        if e.code in (401, 403):
            return {"ok": False, "error_type": "invalid_key",
                    "error": "Cline-API-Key ist ungueltig oder gesperrt."}
        if e.code == 429:
            return {"ok": False, "error_type": "rate_limit",
                    "error": "Cline-Rate-Limit erreicht. Bitte kurz warten."}
        return {"ok": False, "error_type": "api",
                "error": "Cline-Fehler (HTTP %d). Bitte spaeter erneut versuchen." % e.code}
    except Exception:
        return {"ok": False, "error_type": "api", "error": "Cline nicht erreichbar."}


# ═══════════════════════════════════════════════════════════
#  STREAMING — Token-fuer-Token (SSE) fuer echtes Live-Tippen
# ═══════════════════════════════════════════════════════════
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"

def _http_err(provider, e):
    if provider == "Cline" and e.code == 402:
        return {"error_type": "insufficient_credits",
                "error": "Cline-Guthaben aufgebraucht (Cline Credits). Bitte unter app.cline.bot aufladen."}
    if e.code in (401, 403):
        return {"error_type": "invalid_key",
                "error": provider + "-API-Key ist ungueltig oder gesperrt."}
    if e.code == 429:
        return {"error_type": "rate_limit",
                "error": provider + "-Rate-Limit erreicht. Bitte kurz warten."}
    return {"error_type": "api",
            "error": provider + "-Fehler (HTTP %d). Bitte spaeter erneut versuchen." % e.code}

def stream_gemini(messages, api_key, strength, model):
    if not api_key:
        yield {"type": "error", "error_type": "missing_key",
               "error": "Kein Gemini-API-Key hinterlegt (Admin-Panel -> API-Keys)."}
        return
    s = STRENGTHS.get(strength, STRENGTHS["medium"])
    contents = [{"role": ("user" if m["role"] == "user" else "model"), "parts": [{"text": m["content"]}]}
                for m in messages]
    payload = json.dumps({"contents": contents,
        "generationConfig": {"maxOutputTokens": s["max_tokens"], "temperature": s["temp"]}}).encode()
    url = ("https://generativelanguage.googleapis.com/v1beta/models/"
           + model + ":streamGenerateContent?alt=sse&key=" + api_key)
    req = urllib.request.Request(url, data=payload,
        headers={"Content-Type": "application/json", "User-Agent": UA}, method="POST")
    got = False
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            for raw in resp:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                blob = line[5:].strip()
                if not blob or blob == "[DONE]":
                    continue
                try:
                    d = json.loads(blob)
                except Exception:
                    continue
                cand = (d.get("candidates") or [{}])[0]
                parts = (cand.get("content") or {}).get("parts") or []
                txt = "".join(p.get("text", "") for p in parts)
                if txt:
                    got = True
                    yield {"type": "delta", "text": txt}
        if got:
            yield {"type": "end"}
            return
    except urllib.error.HTTPError as e:
        yield {"type": "error", **_http_err("Gemini", e)}
        return
    except Exception:
        pass
    r = call_gemini(messages, api_key, strength, model)   # Fallback ohne Stream
    if r["ok"]:
        yield {"type": "delta", "text": r["text"]}
        yield {"type": "end", "tokens": r.get("tokens", 0)}
    else:
        yield {"type": "error", "error_type": r.get("error_type", "api"), "error": r["error"]}

def stream_openai_style(messages, api_key, strength, model, provider, base_url):
    name = "Cline" if provider == "cline" else "Groq"
    if not api_key:
        yield {"type": "error", "error_type": "missing_key",
               "error": "Kein %s-API-Key hinterlegt (Admin-Panel -> API-Keys)." % name}
        return
    s = STRENGTHS.get(strength, STRENGTHS["medium"])
    msgs = [{"role": ("user" if m["role"] == "user" else "assistant"), "content": m["content"]}
            for m in messages]
    body = {"model": model, "messages": msgs, "max_tokens": s["max_tokens"] + 512, "stream": True}
    if provider != "cline":
        body["temperature"] = s["temp"]
        body["reasoning_effort"] = {"low": "low", "medium": "medium", "high": "high", "extra": "high"}.get(strength, "medium")
    payload = json.dumps(body).encode()
    headers = {"Content-Type": "application/json", "Authorization": "Bearer " + api_key,
               "User-Agent": UA, "Accept": "text/event-stream"}
    if provider == "cline":
        headers["HTTP-Referer"] = "https://sychos.hub"
        headers["X-Title"] = "Sychos"

    def once():
        req = urllib.request.Request(base_url, data=payload, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=120) as resp:
            for raw in resp:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                blob = line[5:].strip()
                if blob == "[DONE]":
                    break
                try:
                    d = json.loads(blob)
                except Exception:
                    continue
                ch = d.get("choices") or []
                delta = ((ch[0].get("delta") or {}) if ch else {}).get("content") or ""
                if delta:
                    yield delta

    got = False
    try:
        for delta in once():
            got = True
            yield {"type": "delta", "text": delta}
        if got:
            yield {"type": "end"}
            return
    except urllib.error.HTTPError as e:
        yield {"type": "error", **_http_err(name, e)}
        return
    except Exception:
        pass
    r = call_cline(messages, api_key, strength, model) if provider == "cline" \
        else call_groq(messages, api_key, strength, model)
    if r["ok"]:
        yield {"type": "delta", "text": r["text"]}
        yield {"type": "end", "tokens": r.get("tokens", 0)}
    else:
        yield {"type": "error", "error_type": r.get("error_type", "api"), "error": r["error"]}

def stream_provider(messages, api_key, strength, model):
    info = MODELS.get(model, MODELS["gemini-3.6-flash"])
    prov = info["provider"]
    api_model = info.get("api_model", model)
    if prov == "groq":
        gen = stream_openai_style(messages, api_key, strength, api_model, "groq",
                                  "https://api.groq.com/openai/v1/chat/completions")
    elif prov == "cline":
        gen = stream_openai_style(messages, api_key, strength, api_model, "cline",
                                  "https://api.cline.bot/api/v1/chat/completions")
    else:
        gen = stream_gemini(messages, api_key, strength, model)
    for ev in gen:
        yield ev


def call_provider(messages, api_key, strength, model):
    info = MODELS.get(model, MODELS["gemini-3.6-flash"])
    prov = info["provider"]
    api_model = info.get("api_model", model)
    if prov == "groq":
        return call_groq(messages, api_key, strength, api_model)
    if prov == "cline":
        return call_cline(messages, api_key, strength, api_model)
    return call_gemini(messages, api_key, strength, model)

# ═══════════════════════════════════════════════════════════
#  EINGEBETTETES FRONTEND (Single-File-Modus)
# ═══════════════════════════════════════════════════════════
WEB_ASSETS = {
    "index.html": ("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" "osYbPW4GnP8D6F9vpw==", "text/html; charset=utf-8"),
    "css/style.css": ("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", "text/css; charset=utf-8"),
    "js/app.js": ("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" "pXlTkzGGOa7NO/D7fwHVv/dx", "application/javascript; charset=utf-8"),
    "admin/index.html": ("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" "GGLfG5QvUe0NzBvaewP6l73/C7pPVb4=", "text/html; charset=utf-8"),
    "404.html": ("eNqVVtuO2zYQfS/Qf5iqSLDBWrLstTde35qgaVH0oVl00wLpGy2NJNYUKZCULxsE6Ef0c/rWP+mXdEhJvm2KoLuQRHLODOdyOPT8qzdvv333/v47KGwpll9+" "MXdfEEzmiyDFwK8gS923RMsgKZg2aBfBL+++DyfBYV2yEhfBhuO2UtoGkChpURJuy1NbLFLc8ARDP+kBl9xyJkKTMIGLQRRf2kmUUJrEBZZ4Yitlen0JtQ4T" "eoUT5NfxJL6LUw+23ApcjuIR/PPHn/CwTwpl5v1mlcSCyzVoFIuAk3oAhcZsEWRs46aR2eQB2H1FO/GS5dinhetdKYJz1UojoSUmtjNQWFuZab+fkUsmypXK" "BbKKmyhR5f9VNpZZnnhNSLQyRmmec3mw8vkd+4kxw28yVnKxX7ytbcbtdJsX9tU4jme39LykZxLHz1vIvajN9Y9szbRl1w9MmgY9ItSJxvOUm0qw/cJsWRU0" "wRi7F2gKRHsR5Ymgddg51ffLEY083M9oABDJLNxqVsEHNwOolCHSKDl11igdG5zBY8hlirspDGZQICcPaRjHm2LW6LTuTSETuJsBEzyXIbdYmikkxBPUM/i9" "NpZn+7ClzlFQsTTlMp/CcFTtvMGPnV8J0yl8AIs7G3qjR62S7RqWT2F8G1duV0nEaT3nBiG6NZDUK56EK3zkqK+iYS+a9KKb3uDF7GQPlWIXu6to2FRmChum" "r8LQL6Wk4AfbNniq4OxExfBHJNcEK6urwZC86cFwuNm6N01ImaqDYZe56K7VFWgpltBULPEJCKN4hOUM/CGbgtXEh4ppirhVoP1Xa25DnxBjtVrTtsNq1/pK" "ZsjTBtpACpaq7RRi+ndJOsGFuVDbDnySukzwZI0aRoa6R+YaCB5r8mqN+0xTOzAHXJu5+FkP7m7c65ZexI1nVDflArN7T5uPDe5u5DDjM2k0HrfyQ1EsyzvL" "B25x6bO4EipZu/prOplkekJhUXSOAQcm0QwGHZsAVkqnSAkd0LJRgqdP89BiQs1SXhNrb+KD9mdJ0VR/MHAunLHk5YEll5WOblydfY18lTOlyynUVYU6YQYP" "FLis6/nJKAaUxicEHA4d/0aOfjcN+5pchStlrSrdyXWenpui43++Z1lbvIxwFI2d5oW54eTMHEsckQzZu+gKOavIxtCB/7MXXLLMH2SKUavylDHxDE7S5ofU" "qvC9j90dbrDqgn8neKkkOgxtNu93jXDe767flUr37pvyjcuoMYugbZGBb5gXApe+RvBUpNzNThfivE+CT2OI6cHygVobE7BBTRVAeQYvBssHpFMIkieFhRyz" "mnoxYUjQQqrlG46UqJ8VFQ1wxym7qG2rcVVioV8AL9srOfwJ7WM0X+lWnf5+5SgEevS2pnMAhiPU5Qolk9L665xC0bBlmowiFGQ+mverT4fUMiA42J+zTrqy" "EugJK00NR+8PF1Sw/K3Wf/+VrOGxLuGHejXvs0+rH1RYWnJJiq/dN7xnEsVR6ZjBbnT8dvXt+19i/wLI3AeU", "text/html; charset=utf-8"),
    "favicon.svg": ("eNp1Uk1vgzAMvU/af7DSy3ZICCEEOsEOu+y0H8FC+NAYQSEt9N/PoaXSJk0iefbzs7GtFPO5hfV7GOeSdN5PL1G0LAtbEmZdGwnOeYQKAufeLG92LQkHDkri" "R14fHwCK2jTzZqE99KOp3Lur6t6MHvq6JJi6xphF4HKFVZQkRm+DWyKmzt5OYJtmNn6TBZ9qO1hXkkMe57rJSfSPPP4j5+pT1fIuL6LffV37ju6NF85oD0tf" "+64kOBd0pm87f7UdjhwrAk0/DKH0J6/j6la6mCrfAQ75ISUIrmnCUjyC5kxSyXIaJ+gqqiDG+8gkZBjIGPqCHZFMQELKBCgM5YjhiBBmKUYEy/CWGOPIZBSL" "IHekcrsFy4HTFNmEKozif8m+Hri1O9rRhN04+2VKcnLD06F93gl6mzi9E2FNuppK4uxprPchde/0YEDjIiRq9QUxLAZhX8teOWQU4b0g/gAsK5qT", "image/svg+xml"),
}

def get_embedded(rel):
    """Eingebettete Frontend-Datei (gzip+base64), falls keine auf Disk liegt."""
    if not WEB_ASSETS:
        return None
    key = rel.replace("\\", "/").lstrip("/")
    if key in ("", "index.html"):
        key = "index.html"
    item = WEB_ASSETS.get(key)
    if item is None:
        return None
    return zlib.decompress(base64.b64decode(item[0])), item[1]


# ═══════════════════════════════════════════════════════════
#  HTTP HANDLER
# ═══════════════════════════════════════════════════════════
class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        try:
            ts = time.strftime("%H:%M:%S")
            sys.stderr.write(f"  [{ts}] {fmt % args}\n")
        except Exception:
            pass

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Requested-With, Accept")

    def _json(self, code, data):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self):
        ln = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(ln)) if ln else {}

    def _get_user(self):
        """Auth per Session-Token (Bearer). Gibt User-Dict oder None zurueck."""
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return None
        token = auth[7:].strip()
        db = get_db()
        row = db.execute("SELECT u.* FROM sessions s JOIN users u ON u.uid=s.uid WHERE s.token=?",
                         (token,)).fetchone()
        user = dict(row) if row else None
        if user:
            # Zeitlich begrenzte Sperre automatisch aufheben
            if user["is_banned"] and user.get("ban_until", 0) and time.time() > user["ban_until"]:
                db.execute("UPDATE users SET is_banned=0, ban_reason='', ban_until=0 WHERE uid=?",
                           (user["uid"],))
                db.commit()
                user["is_banned"] = 0
                user["ban_reason"] = ""
                user["ban_until"] = 0
            # Zeitlich begrenztes Paid laeuft automatisch ab
            if user.get("is_paid") and user.get("paid_until", 0) and time.time() > user["paid_until"]:
                db.execute("UPDATE users SET is_paid=0, paid_until=0 WHERE uid=?", (user["uid"],))
                db.commit()
                user["is_paid"] = 0
                user["paid_until"] = 0
            # Abgelaufene Plaene laufen automatisch aus (zurueck auf Free)
            if user.get("plan", "free") != "free" and user.get("plan_until", 0) \
                    and time.time() > user["plan_until"]:
                db.execute("UPDATE users SET plan='free', plan_until=0 WHERE uid=?", (user["uid"],))
                db.commit()
                user["plan"] = "free"
                user["plan_until"] = 0
            db.execute("UPDATE users SET last_seen=? WHERE uid=?", (time.time(), user["uid"]))
            db.commit()
        db.close()
        return user

    def _ban_info(self, user):
        """Sperr-Details fuer das Frontend-Modal."""
        until = user.get("ban_until", 0) or 0
        return {"banned": True,
                "ban_reason": user.get("ban_reason", ""),
                "ban_until": until,
                "ban_permanent": not bool(until)}

    def _require_user(self, allow_banned=False):
        """Auth + serverseitige Ban-Pruefung. Gibt (user, error) zurueck."""
        user = self._get_user()
        if not user:
            return None, (401, {"ok": False, "error": "Nicht angemeldet"})
        if user["is_banned"] and not allow_banned:
            d = {"ok": False, "error": "Account gesperrt"}
            d.update(self._ban_info(user))
            return None, (403, d)
        return user, None

    def _check_admin(self):
        """Nur angemeldete Admin-User (Session-Token). Kein statischer Admin-Key mehr."""
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return False
        tok = auth[7:].strip()
        if not tok:
            return False
        db = get_db()
        row = db.execute("SELECT u.is_admin FROM sessions s JOIN users u ON u.uid=s.uid WHERE s.token=?",
                         (tok,)).fetchone()
        db.close()
        return bool(row and row["is_admin"])

    def _serve_static(self, path):
        """Liefert Frontend-Dateien aus dem Projekt-Root aus."""
        if path in ("/", "/index.html"):
            path = "/index.html"
        elif path in ("/admin", "/admin/"):
            path = "/admin/index.html"
        rel = unquote(path).lstrip("/")
        full = os.path.normpath(os.path.join(ROOT, rel))
        if not full.startswith(os.path.normpath(ROOT)):
            self._json(403, {"ok": False, "error": "Zugriff verweigert"}); return
        if os.path.isfile(full):
            ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
            with open(full, "rb") as f:
                data = f.read()
        else:
            emb = get_embedded(rel)
            if emb is None:
                # 404-Seite: echter 404-Status mit moderner "Page Not Found"
                nf = get_embedded("404.html")
                if nf and "." not in os.path.basename(rel):
                    data, ctype = nf
                    self.send_response(404)
                    self.send_header("Content-Type", ctype)
                    self._cors()
                    self.end_headers()
                    self.wfile.write(data)
                    return
                self._json(404, {"ok": False, "error": "Nicht gefunden"}); return
            data, ctype = emb
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self._cors()
        self.end_headers()
        self.wfile.write(data)

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    # ── GET ───────────────────────────────────────────────
    def do_GET(self):
        note_request()
        path = urlparse(self.path).path

        if path == "/status":
            self._json(200, {"ok": True, "server": "Sychos Oracle", "version": "4.0.0",
                "uptime": int(time.time() - START_TIME), "load": len(request_times)}); return

        if path == "/models":
            u = self._get_user()
            plan = effective_plan(u)
            paid_ok = bool(u and (u.get("is_admin") or plan != "free"))
            self._json(200, {"ok": True, "is_paid": paid_ok, "plan": plan, "load_factor": load_factor(),
                "models": [{"id": k, "name": v["name"], "provider": v["provider"],
                            "factor": v["factor"], "paid": bool(v.get("paid")),
                            "locked": bool(v.get("paid")) and not paid_ok}
                           for k, v in MODELS.items()],
                "strengths": [{"id": k, "name": v["name"], "cost": v["cost"],
                               "max_tokens": v["max_tokens"]}
                              for k, v in STRENGTHS.items()]}); return

        # Plaene & Token-Limits (public – wird im Settings-Planpicker angezeigt)
        if path == "/plans":
            self._json(200, {"ok": True, "days": PLAN_DAYS,
                "plans": [{"id": pid, "name": PLANS[pid]["name"], "price": PLANS[pid]["price"],
                           "desc": PLANS[pid]["desc"],
                           "limits": {"5h": PLANS[pid]["t5"], "week": PLANS[pid]["tweek"],
                                      "month": PLANS[pid]["tmonth"]}}
                          for pid in PLAN_ORDER]}); return

        if path == "/me":
            user = self._get_user()
            if not user:
                self._json(401, {"ok": False, "error": "Nicht angemeldet"}); return
            plan = effective_plan(user)
            d = {"ok": True, "uid": user["uid"], "email": user["email"],
                 "display_name": user["display_name"], "credits": user["credits"],
                 "is_admin": user["is_admin"], "is_paid": bool(user["is_paid"]),
                 "created_at": user.get("created_at", 0),
                 "paid_until": user.get("paid_until", 0) or 0,
                 "plan": plan, "plan_name": PLANS[plan]["name"],
                 "plan_until": user.get("plan_until", 0) or 0,
                 "usage": usage_state(user["uid"], plan)}
            if user["is_banned"]:
                d.update(self._ban_info(user))
            self._json(200, d); return

        if path == "/chats":
            user = self._get_user()
            if not user:
                self._json(401, {"ok": False, "error": "Nicht angemeldet"}); return
            db = get_db()
            rows = db.execute("SELECT id,title,model,created_at FROM chats WHERE uid=? ORDER BY created_at DESC",
                              (user["uid"],)).fetchall()
            db.close()
            self._json(200, {"ok": True, "chats": [dict(r) for r in rows]}); return

        if path.startswith("/messages/"):
            user = self._get_user()
            if not user:
                self._json(401, {"ok": False, "error": "Nicht angemeldet"}); return
            cid = path.split("/")[2]
            db = get_db()
            own = db.execute("SELECT id FROM chats WHERE id=? AND uid=?", (cid, user["uid"])).fetchone()
            rows = []
            if own:
                rows = db.execute("SELECT role,content,model,cost,created_at FROM messages WHERE chat_id=? ORDER BY id",
                                  (cid,)).fetchall()
            db.close()
            if not own:
                self._json(404, {"ok": False, "error": "Chat nicht gefunden"}); return
            self._json(200, {"ok": True, "messages": [dict(r) for r in rows]}); return

        # User-eigene Keys: Feature entfernt
        if path == "/settings/keys":
            self._json(410, {"ok": False, "error": "Eigene API-Keys sind entfernt."}); return

        if path == "/admin/users":
            if not self._check_admin():
                self._json(403, {"ok": False, "error": "Kein Admin"}); return
            db = get_db()
            db.execute("UPDATE users SET is_paid=0, paid_until=0 WHERE is_paid=1 AND paid_until>0 AND paid_until<?",
                       (time.time(),))
            db.commit()
            rows = db.execute("SELECT uid,email,display_name,credits,bonus_tokens,is_banned,ban_reason,ban_until,is_admin,is_paid,paid_until,created_at,last_seen FROM users").fetchall()
            db.close()
            now = time.time()
            users = []
            for r in rows:
                u = dict(r)
                u["online"] = (now - u.get("last_seen", 0)) < ONLINE_WINDOW
                u["ban_permanent"] = bool(u.get("is_banned")) and not bool(u.get("ban_until", 0))
                users.append(u)
            self._json(200, {"ok": True, "users": users}); return

        if path == "/admin/settings":
            if not self._check_admin():
                self._json(403, {"ok": False, "error": "Kein Admin"}); return
            db = get_db()
            res = {"ok": True}
            for prov in ("gemini", "groq", "cline"):
                res[prov + "_key_set"] = bool(resolve_key(db, None, prov))
            db.close()
            self._json(200, res); return

        self._serve_static(path)

    def do_POST(self):
        note_request()
        path = urlparse(self.path).path
        body = self._read_body()
        ip = self.client_address[0] if self.client_address else "?"

        # Register (Start-Tokens) -> Session-Token
        if path == "/register":
            if not rate_ok("login:" + ip, LOGIN_RATE_LIMIT):
                self._json(429, {"ok": False, "error": "Zu viele Versuche. Bitte kurz warten."}); return
            email = body.get("email", "").strip().lower()
            pw = body.get("password", "")
            name = (body.get("display_name", "").strip() or email.split("@")[0])[:40]
            if not email or len(pw) < 3 or "@" not in email:
                self._json(400, {"ok": False, "error": "Gueltige Email und Passwort (min. 3 Zeichen) benoetigt"}); return
            db = get_db()
            n_ip = db.execute("SELECT COUNT(*) AS n FROM users WHERE reg_ip=?", (ip,)).fetchone()["n"]
            if n_ip >= MAX_ACCOUNTS_PER_IP:
                db.close()
                self._json(429, {"ok": False,
                    "error": "Maximale Anzahl von Accounts (" + str(MAX_ACCOUNTS_PER_IP)
                             + ") pro IP erreicht."}); return
            if db.execute("SELECT uid FROM users WHERE email=?", (email,)).fetchone():
                db.close()
                self._json(409, {"ok": False, "error": "Email bereits registriert"}); return
            uid = str(uuid.uuid4())
            token = new_token()
            db.execute("INSERT INTO users (uid,email,password_hash,display_name,credits,bonus_tokens,created_at,reg_ip) VALUES (?,?,?,?,?,?,?,?)",
                       (uid, email, hash_pw(pw), name, START_CREDITS, START_TOKENS, time.time(), ip))
            db.execute("INSERT INTO sessions (token,uid,created_at) VALUES (?,?,?)",
                       (token, uid, time.time()))
            db.commit(); db.close()
            self._json(200, {"ok": True, "token": token, "uid": uid, "display_name": name,
                             "credits": START_CREDITS, "is_admin": 0}); return

        # Login -> Session-Token (auch bei Sperre: Sperr-Modal erscheint im App-Inneren)
        if path == "/login":
            if not rate_ok("login:" + ip, LOGIN_RATE_LIMIT):
                self._json(429, {"ok": False, "error": "Zu viele Versuche. Bitte kurz warten."}); return
            email = body.get("email", "").strip().lower()
            pw = body.get("password", "")
            db = get_db()
            row = db.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
            if not row or not verify_pw(pw, row["password_hash"]):
                db.close()
                self._json(401, {"ok": False, "error": "Falsche Anmeldedaten"}); return
            user = dict(row)
            if not user["password_hash"].startswith("pbkdf2$"):
                db.execute("UPDATE users SET password_hash=? WHERE uid=?", (hash_pw(pw), user["uid"]))
            if user["is_banned"] and user.get("ban_until", 0) and time.time() > user["ban_until"]:
                db.execute("UPDATE users SET is_banned=0, ban_reason='', ban_until=0 WHERE uid=?",
                           (user["uid"],))
                user["is_banned"] = 0
                user["ban_reason"] = ""
                user["ban_until"] = 0
            token = new_token()
            now = time.time()
            db.execute("INSERT INTO sessions (token,uid,created_at) VALUES (?,?,?)",
                       (token, user["uid"], now))
            db.execute("UPDATE users SET last_login=?, last_seen=? WHERE uid=?", (now, now, user["uid"]))
            db.commit(); db.close()
            d = {"ok": True, "token": token, "uid": user["uid"], "email": user["email"],
                 "display_name": user["display_name"], "credits": user["credits"],
                 "is_admin": user["is_admin"]}
            if user["is_banned"]:
                d.update(self._ban_info(user))
            self._json(200, d); return

        # Logout: Session invalidieren
        if path == "/logout":
            auth = self.headers.get("Authorization", "")
            if auth.startswith("Bearer "):
                db = get_db()
                db.execute("DELETE FROM sessions WHERE token=?", (auth[7:].strip(),))
                db.commit(); db.close()
            self._json(200, {"ok": True}); return

        # Eigener API-Key: Feature entfernt (Keys verwaltet ausschliesslich der Admin)
        if path == "/settings/keys":
            self._json(410, {"ok": False, "error": "Eigene API-Keys sind entfernt. Keys verwaltet der Admin."}); return

        # ── Profil & Einstellungen ─────────────────────────
        # Anzeigename aendern
        if path == "/settings/profile":
            user, err = self._require_user(allow_banned=True)
            if err:
                self._json(err[0], err[1]); return
            name = (body.get("display_name", "") or "").strip()[:40]
            if not name:
                self._json(400, {"ok": False, "error": "Name darf nicht leer sein."}); return
            db = get_db()
            db.execute("UPDATE users SET display_name=? WHERE uid=?", (name, user["uid"]))
            db.commit(); db.close()
            self._json(200, {"ok": True, "display_name": name}); return

        # E-Mail aendern (Passwort bestaetigen)
        if path == "/settings/email":
            user, err = self._require_user(allow_banned=True)
            if err:
                self._json(err[0], err[1]); return
            email = (body.get("email", "") or "").strip().lower()
            pw = body.get("password", "")
            if not email or "@" not in email:
                self._json(400, {"ok": False, "error": "Ungueltige E-Mail."}); return
            db = get_db()
            row = db.execute("SELECT password_hash FROM users WHERE uid=?", (user["uid"],)).fetchone()
            if not row or not verify_pw(pw, row["password_hash"]):
                db.close()
                self._json(401, {"ok": False, "error": "Passwort falsch."}); return
            if db.execute("SELECT 1 FROM users WHERE email=? AND uid<>?", (email, user["uid"])).fetchone():
                db.close()
                self._json(409, {"ok": False, "error": "E-Mail bereits vergeben."}); return
            db.execute("UPDATE users SET email=? WHERE uid=?", (email, user["uid"]))
            db.commit(); db.close()
            self._json(200, {"ok": True, "email": email}); return

        # Passwort aendern (aktuelles Passwort noetig)
        if path == "/settings/password":
            user, err = self._require_user(allow_banned=True)
            if err:
                self._json(err[0], err[1]); return
            cur_pw = body.get("current_password", "")
            new_pw = body.get("new_password", "")
            if len(new_pw) < 4:
                self._json(400, {"ok": False, "error": "Neues Passwort: mind. 4 Zeichen."}); return
            db = get_db()
            row = db.execute("SELECT password_hash FROM users WHERE uid=?", (user["uid"],)).fetchone()
            if not row or not verify_pw(cur_pw, row["password_hash"]):
                db.close()
                self._json(401, {"ok": False, "error": "Aktuelles Passwort falsch."}); return
            db.execute("UPDATE users SET password_hash=? WHERE uid=?", (hash_pw(new_pw), user["uid"]))
            db.commit(); db.close()
            self._json(200, {"ok": True}); return

        # Account endgueltig loeschen (Passwort noetig; Admin-Account ist geschuetzt)
        if path == "/settings/delete":
            user, err = self._require_user(allow_banned=True)
            if err:
                self._json(err[0], err[1]); return
            pw = body.get("password", "")
            db = get_db()
            row = db.execute("SELECT password_hash, is_admin FROM users WHERE uid=?", (user["uid"],)).fetchone()
            if not row or not verify_pw(pw, row["password_hash"]):
                db.close()
                self._json(401, {"ok": False, "error": "Passwort falsch."}); return
            if row["is_admin"]:
                db.close()
                self._json(403, {"ok": False, "error": "Der Admin-Account kann nicht geloescht werden."}); return
            for r in db.execute("SELECT id FROM chats WHERE uid=?", (user["uid"],)).fetchall():
                db.execute("DELETE FROM messages WHERE chat_id=?", (r["id"],))
            db.execute("DELETE FROM chats WHERE uid=?", (user["uid"],))
            db.execute("DELETE FROM sessions WHERE uid=?", (user["uid"],))
            db.execute("DELETE FROM user_keys WHERE uid=?", (user["uid"],))
            db.execute("DELETE FROM users WHERE uid=?", (user["uid"],))
            db.commit(); db.close()
            self._json(200, {"ok": True}); return

        # Plan kaufen: derzeit DEAKTIVIERT – das Plan-/Paid-Menue bleibt sichtbar,
        # aber es kann (noch) kein Plan gekauft werden. Zum spaeteren Aktivieren nur
        # diesen Return entfernen und den Kauf-Code wieder einsetzen.
        if path == "/plan/buy":
            self._json(503, {"ok": False, "error": "Plan-Kauf ist derzeit deaktiviert."}); return

        # Neuer Chat
        if path == "/chat/new":
            user, err = self._require_user()
            if err:
                self._json(err[0], err[1]); return
            cid = str(uuid.uuid4())
            title = (body.get("title", "Neuer Chat") or "Neuer Chat")[:MAX_TITLE_LEN]
            model = body.get("model", "")
            if model not in MODELS:
                model = "gemini-3.6-flash"
            db = get_db()
            db.execute("INSERT INTO chats (id,uid,title,model,created_at) VALUES (?,?,?,?,?)",
                       (cid, user["uid"], title, model, time.time()))
            db.commit(); db.close()
            self._json(200, {"ok": True, "chat_id": cid, "title": title, "model": model}); return

        # Chat umbenennen
        if path == "/chat/rename":
            user, err = self._require_user()
            if err:
                self._json(err[0], err[1]); return
            cid = body.get("chat_id", "")
            title = (body.get("title", "") or "").strip()[:MAX_TITLE_LEN] or "Neuer Chat"
            db = get_db()
            cur = db.execute("UPDATE chats SET title=? WHERE id=? AND uid=?",
                             (title, cid, user["uid"]))
            db.commit(); db.close()
            if cur.rowcount == 0:
                self._json(404, {"ok": False, "error": "Chat nicht gefunden"}); return
            self._json(200, {"ok": True, "chat_id": cid, "title": title}); return

        # Chat loeschen
        if path == "/chat/delete":
            user, err = self._require_user()
            if err:
                self._json(err[0], err[1]); return
            cid = body.get("chat_id", "")
            db = get_db()
            db.execute("DELETE FROM messages WHERE chat_id=? AND chat_id IN (SELECT id FROM chats WHERE uid=?)",
                       (cid, user["uid"]))
            db.execute("DELETE FROM chats WHERE id=? AND uid=?", (cid, user["uid"]))
            db.commit(); db.close()
            self._json(200, {"ok": True}); return

        # AI-Chat senden: Credits atomar reservieren (kein Doppelabzug bei Parallel-Requests)
        if path == "/chat/send":
            user, err = self._require_user()
            if err:
                self._json(err[0], err[1]); return
            if not rate_ok("send:" + user["uid"], SEND_RATE_LIMIT):
                self._json(429, {"ok": False, "error": "Zu viele Nachrichten. Bitte kurz warten."}); return
            cid = body.get("chat_id", "")
            msg = (body.get("message", "") or "").strip()
            model = body.get("model", "")
            strength = body.get("strength", "medium")
            if model not in MODELS:
                self._json(400, {"ok": False, "error": "Unbekanntes Modell"}); return
            plan = effective_plan(user)
            if MODELS[model].get("paid") and plan == "free" and not user.get("is_admin"):
                self._json(402, {"ok": False, "error_type": "premium_locked",
                    "error": "Premium-Modell – ab dem Basic-Plan verfuegbar. Bitte unter Einstellungen -> Plan freischalten."}); return
            if strength not in STRENGTHS:
                strength = "medium"
            if not msg:
                self._json(400, {"ok": False, "error": "Leere Nachricht"}); return
            if len(msg) > MAX_MSG_LEN:
                self._json(400, {"ok": False, "error": "Nachricht zu lang (max. %d Zeichen)" % MAX_MSG_LEN}); return

            db = get_db()
            own = db.execute("SELECT id FROM chats WHERE id=? AND uid=?", (cid, user["uid"])).fetchone()
            if not own:
                db.close()
                self._json(404, {"ok": False, "error": "Chat nicht gefunden"}); return
            rows = db.execute("SELECT role,content FROM messages WHERE chat_id=? ORDER BY id",
                              (cid,)).fetchall()
            history = [{"role": r["role"], "content": r["content"]} for r in rows]
            history.append({"role": "user", "content": msg})

            # Token-Limits (5h / Woche / Monat) wie Cline: bei Erreichen bis zum Reset warten
            est = est_tokens(history)
            lim = limit_block(user["uid"], plan, est)
            if lim:
                win, wstate, _ = lim
                db.close()
                self._json(429, {"ok": False, "error_type": "limit_reached",
                    "error": "%s-Limit erreicht (Plan %s). Weiter in %s – oder Plan upgraden."
                             % (TOKEN_WINDOWS[win]["label"], PLANS[plan]["name"],
                                fmt_wait(wstate["resets_at"] - time.time())),
                    "limit_win": win, "resets_at": wstate["resets_at"]}); return

            db.execute("INSERT INTO messages (chat_id,role,content,model,created_at) VALUES (?,?,?,?,?)",
                       (cid, "user", msg, model, time.time()))
            web_used = False
            if body.get("web"):
                hits = web_search(msg)
                if hits:
                    history[-1]["content"] += "\n\n[Aktuelle Web-Recherche als Kontext]\n" + "\n".join(hits)
                web_used = True
            api_key = resolve_key(db, user["uid"], MODELS[model]["provider"])
            db.commit(); db.close()

            # SSE-Stream: Tokens live an das Frontend senden
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Accel-Buffering", "no")
            self._cors()
            self.end_headers()

            def push(evt, data):
                self.wfile.write(("event: %s\ndata: %s\n\n" % (evt, json.dumps(data, ensure_ascii=False))).encode("utf-8"))
                self.wfile.flush()

            full, tokens, err = [], 0, None
            for ev in stream_provider(history, api_key, strength, model):
                if ev["type"] == "delta":
                    full.append(ev["text"])
                    push("delta", {"t": ev["text"]})
                elif ev["type"] == "end":
                    tokens = ev.get("tokens", 0)
                elif ev["type"] == "error":
                    err = ev
            text = "".join(full)
            if err or not text.strip():
                e2 = err or {"error": "Leere Antwort – bitte erneut versuchen.", "error_type": "api"}
                push("error", {"error": e2["error"], "error_type": e2.get("error_type", "api")})
                return
            # Token-Abrechnung (Input + Output) auf alle drei Limit-Fenster
            in_tok = int(sum(len(m.get("content") or "") for m in history) / 4)
            out_tok = tokens if tokens else max(1, len(text) // 4)
            used = max(1, in_tok + out_tok)
            add_usage(user["uid"], used)
            db = get_db()
            db.execute("INSERT INTO messages (chat_id,role,content,model,tokens,cost,created_at) VALUES (?,?,?,?,?,?,?)",
                       (cid, "assistant", text, model, used, 0, time.time()))
            db.commit(); db.close()
            push("done", {"text": text, "web": web_used, "tokens_used": used,
                          "usage": usage_state(user["uid"], plan)})
            return

        # ── Admin ─────────────────────────────────────────
        if path == "/admin/tokens":
            if not self._check_admin():
                self._json(403, {"ok": False, "error": "Kein Admin"}); return
            uid = body.get("uid", "")
            try:
                amount = float(body.get("tokens", 0))
            except (TypeError, ValueError):
                self._json(400, {"ok": False, "error": "Ungueltige Menge"}); return
            if amount == 0 or abs(amount) > 10 ** 9:
                self._json(400, {"ok": False, "error": "Menge ungueltig"}); return
            db = get_db()
            cur = db.execute("UPDATE users SET bonus_tokens = MAX(0, COALESCE(bonus_tokens,0) + ?) WHERE uid=?",
                             (amount, uid))
            row = db.execute("SELECT bonus_tokens FROM users WHERE uid=?", (uid,)).fetchone()
            db.commit(); db.close()
            if cur.rowcount == 0:
                self._json(404, {"ok": False, "error": "User nicht gefunden"}); return
            self._json(200, {"ok": True, "uid": uid, "bonus_tokens": (row["bonus_tokens"] if row else 0)}); return

        # User sperren/entsperren: Grund + Dauer (Minuten, 0 = dauerhaft)
        if path == "/admin/ban":
            if not self._check_admin():
                self._json(403, {"ok": False, "error": "Kein Admin"}); return
            uid = body.get("uid", "")
            ban = 1 if body.get("ban", True) else 0
            reason = (body.get("reason", "") or "").strip()[:200]
            try:
                duration = max(0, int(body.get("duration_minutes", 0) or 0))
            except (TypeError, ValueError):
                duration = 0
            until = (time.time() + duration * 60) if duration else 0
            db = get_db()
            cur = db.execute(
                "UPDATE users SET is_banned=?, ban_reason=?, ban_until=? WHERE uid=?",
                (ban, reason if ban else "", until if ban else 0, uid))
            db.commit(); db.close()
            if cur.rowcount == 0:
                self._json(404, {"ok": False, "error": "User nicht gefunden"}); return
            self._json(200, {"ok": True, "banned": bool(ban), "ban_until": until if ban else 0}); return

        # Admin vergibt "Sychos Paid" -> entsperrt Premium-Modelle
        if path == "/admin/paid":
            if not self._check_admin():
                self._json(403, {"ok": False, "error": "Kein Admin"}); return
            uid = body.get("uid", "")
            # paid: true/1 -> geben, false/0/"nein" -> WEGNEHMEN
            raw = body.get("paid", True)
            paid = 0 if (raw is False or raw == 0 or str(raw).strip().lower() in ("0", "false", "nein", "weg", "off")) else 1
            # Dauer in Minuten: 0 = dauerhaft, sonst zeitlich begrenzt (laeuft automatisch ab)
            try:
                dur = max(0, int(body.get("duration_minutes", 0) or 0))
            except (TypeError, ValueError):
                dur = 0
            until = (time.time() + dur * 60) if (paid and dur) else 0
            db = get_db()
            cur = db.execute("UPDATE users SET is_paid=?, paid_until=? WHERE uid=?",
                             (paid, until if paid else 0, uid))
            db.commit(); db.close()
            if cur.rowcount == 0:
                self._json(404, {"ok": False, "error": "User nicht gefunden"}); return
            self._json(200, {"ok": True, "paid": bool(paid), "paid_until": until}); return

        # Admin: globale Provider-Keys setzen (leerer Key = entfernen)
        if path == "/admin/settings":
            if not self._check_admin():
                self._json(403, {"ok": False, "error": "Kein Admin"}); return
            db = get_db()
            for prov in ("gemini", "groq", "cline"):
                if prov + "_api_key" in body:
                    val = (body[prov + "_api_key"] or "").strip()[:200]
                    db.execute("INSERT OR REPLACE INTO settings (key,value) VALUES (?,?)",
                               (prov + "_api_key", val))
            db.commit(); db.close()
            self._json(200, {"ok": True}); return

def write_api_keys_to_db():
    """Traegt die Provider-API-Keys DIREKT in die Datenbank (settings) ein.

    Nutzung:
      python oracle_server.py --set-keys
          -> eingebaute bzw. per Env uebergebene Keys in die DB schreiben
      python oracle_server.py --set-keys GEMINI=xxx GROQ=yyy CLINE=zzz
          -> eigene Keys eintragen (leerer Wert = entfernen)
    Danach wird der Server automatisch gestartet (--no-start = nur in die DB schreiben).
    """
    custom = {}
    for a in sys.argv[1:]:
        if "=" in a and not a.startswith("-"):
            k, v = a.split("=", 1)
            custom[k.strip().upper()] = v
    keys = {
        "gemini": custom.get("GEMINI", GEMINI_API_KEY),
        "groq": custom.get("GROQ", GROQ_API_KEY),
        "cline": custom.get("CLINE", CLINE_API_KEY),
    }
    init_db()
    db = get_db()
    print("\n  API-Keys direkt in die DB schreiben (" + DB_PATH + "):")
    for prov, val in keys.items():
        val = (val or "").strip()
        db.execute("INSERT OR REPLACE INTO settings (key,value) VALUES (?,?)",
                   (prov + "_api_key", val))
        print("    [OK] %-6s -> %s" % (prov.capitalize(), mask_key(val) if val else "(leer = entfernt)"))
    db.commit(); db.close()
    print("  Fertig. Keys sind sofort aktiv (Admin-Panel zeigt 'hinterlegt').")


# ═══════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════
def main():
    init_db()
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    print("\n  ========================================")
    print("     S Y C H O S   O R A C L E  v3.0     ")
    print("  ========================================")
    print(f"  Hub   : http://localhost:{PORT}")
    print(f"  Admin : http://localhost:{PORT}/admin/")
    print(f"  DB    : {DB_PATH}")
    print("  ----------------------------------------")
    print("  ========================================\n")
    print("  -> Ctrl+C zum Beenden\n")
    # Always-On: selbstheilend + Port-Konflikte loesen (alter Sychos-Server)
    attempts = 0
    while True:
        try:
            server = HTTPServerCls((HOST, PORT), Handler)
        except OSError as e:
            err = getattr(e, "errno", 0)
            if err in (98, 48, 10048) and attempts < 2:
                attempts += 1
                # 1) zuerst alten Sychos-Server raeumen (wichtigster Schritt!)
                if free_port():
                    print("  Alter Sychos-Server beendet -> Port %d ist wieder frei..." % PORT)
                    continue
                # 2) erst danach pruefen, ob schon eine laufende Instanz existiert
                if _service_active():
                    print("\n  Laeuft bereits als Always-On Service (sychos-hub).")
                    print("  -> http://localhost:%d  (systemctl status sychos-hub)" % PORT)
                    return
                print("\n  Port %d ist belegt! Anderen Port waehlen:" % PORT)
                print("    Windows: set ORACLE_PORT=7778 && python oracle_server.py")
                print("    Linux:   ORACLE_PORT=7778 python3 oracle_server.py")
                sys.exit(1)
            raise
        attempts = 0
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\n  Server gestoppt.")
            server.server_close()
            break
        except Exception as e:
            print("  Fehler -> Neustart in 5s:", e)
            time.sleep(5)

def install_autostart():
    """Always-On: Windows = Autostart-Link, Linux = systemd (mit sudo)."""
    script = os.path.abspath(__file__)
    if os.name == "nt":
        import subprocess
        py = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
        if not os.path.isfile(py):
            py = sys.executable
        startup = os.path.join(os.environ.get("APPDATA", ""),
            "Microsoft", "Windows", "Start Menu", "Programs", "Startup")
        lnk = os.path.join(startup, "SychosHub.lnk")
        ps = ("$s=(New-Object -ComObject WScript.Shell).CreateShortcut('%s');"
              "$s.TargetPath='%s';$s.Arguments='\"%s\"';$s.WindowStyle=7;$s.Save()"
              % (lnk.replace("'", "''"), py, script))
        subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=True)
        print("  [OK] Autostart installiert (startet bei jedem Windows-Login):")
        print("       " + lnk)
    else:
        if os.geteuid() != 0:
            print("  Linux: Bitte mit sudo starten fuer systemd-Autostart:")
            print("         sudo python3 oracle_server.py --install")
            return
        unit = ("[Unit]\nDescription=Sychos Hub (Always-On)\nAfter=network.target\n\n"
                "[Service]\nType=simple\nExecStart=%s %s\nRestart=always\nRestartSec=5\n"
                "Environment=ORACLE_HOST=0.0.0.0\nEnvironment=ORACLE_PORT=%d\n\n"
                "[Install]\nWantedBy=multi-user.target\n"
                % (sys.executable, script, PORT))
        with open("/etc/systemd/system/sychos-hub.service", "w") as f:
            f.write(unit)
        os.system("systemctl daemon-reload")
        os.system("systemctl enable sychos-hub >/dev/null 2>&1")
        os.system("systemctl restart sychos-hub")
        print("  [OK] systemd-Service installiert (Always-On): sychos-hub")
        print("       -> laeuft bereits im Hintergrund, kein extra Start noetig")
        return True
    return False

def _service_active():
    """True, wenn der eigene systemd-Service sychos-hub bereits laeuft."""
    if os.name == "nt":
        return False
    return os.system("systemctl is-active --quiet sychos-hub >/dev/null 2>&1") == 0

def free_port():
    """Beendet alte Sychos-Prozesse (v2 / Sychos Net) auf dem Port — nie sich selbst."""
    me = os.getpid()
    me_path = os.path.abspath(__file__)
    pat = ("sychos-oracle", "SychosOracle")
    killed = []
    try:
        if os.name == "nt":
            import subprocess
            res = subprocess.run(["powershell", "-NoProfile", "-Command",
                "(Get-NetTCPConnection -LocalPort %d -State Listen).OwningProcess" % PORT],
                capture_output=True, text=True).stdout
            pids = set(x.strip() for x in res.splitlines() if x.strip())
            for pid in pids:
                if pid == str(me):
                    continue
                info = subprocess.run(["powershell", "-NoProfile", "-Command",
                    "(Get-CimInstance Win32_Process -Filter 'ProcessId=%s').CommandLine" % pid],
                    capture_output=True, text=True).stdout
                if any(p in info for p in pat) or (
                        "oracle_server" in info and me_path not in info and "sychos-hub" not in info):
                    subprocess.run(["taskkill", "/F", "/PID", pid], capture_output=True)
                    killed.append(pid)
        else:
            # WICHTIG: alten systemd-Service (v2) stoppen, sonst startet er durch
            # Restart=always sofort wieder und beide streiten um den Port
            if hasattr(os, "geteuid") and os.geteuid() == 0:
                os.system("systemctl stop sychos-oracle >/dev/null 2>&1")
                os.system("systemctl disable sychos-oracle >/dev/null 2>&1")
            for pid in os.listdir("/proc"):
                if not pid.isdigit() or int(pid) == me:
                    continue
                try:
                    with open("/proc/%s/cmdline" % pid, "rb") as f:
                        cmd = f.read().replace(b"\0", b" ").decode("utf-8", "replace")
                except Exception:
                    continue
                if any(p in cmd for p in pat) or (
                        "oracle_server" in cmd and me_path not in cmd and "sychos-hub" not in cmd):
                    try:
                        os.kill(int(pid), 15)
                        killed.append(pid)
                    except Exception:
                        pass
    except Exception:
        pass
    if killed:
        time.sleep(1.5)
    # Erfolg = auf dem Port lauscht nichts mehr
    import socket as _sock
    try:
        c = _sock.socket(_sock.AF_INET, _sock.SOCK_STREAM)
        c.settimeout(1)
        c.connect(("127.0.0.1", PORT))
        c.close()
        return False
    except Exception:
        return True

def uninstall_autostart():
    if os.name == "nt":
        startup = os.path.join(os.environ.get("APPDATA", ""),
            "Microsoft", "Windows", "Start Menu", "Programs", "Startup")
        lnk = os.path.join(startup, "SychosHub.lnk")
        if os.path.isfile(lnk):
            os.remove(lnk)
        print("  [OK] Autostart entfernt.")
    else:
        os.system("systemctl disable --now sychos-hub >/dev/null 2>&1")
        if os.path.isfile("/etc/systemd/system/sychos-hub.service"):
            os.remove("/etc/systemd/system/sychos-hub.service")
        print("  [OK] systemd-Service entfernt.")

if __name__ == "__main__":
    if "--set-keys" in sys.argv:
        write_api_keys_to_db()
        if "--no-start" in sys.argv:
            sys.exit(0)
        print("  -> Server wird gestartet...\n")
    if "--install" in sys.argv:
        if install_autostart():
            sys.exit(0)      # Linux: Service laeuft bereits -> nicht doppelt starten
    elif "--uninstall" in sys.argv:
        uninstall_autostart()
        sys.exit(0)
    main()
