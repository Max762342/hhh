"""
Sychos Hub — Oracle Server v5.0
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
    "index.html": ("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", "text/html; charset=utf-8"),
    "css/style.css": ("eNrdPVmS20aW/zpFjhRqFW2CAsC1SuGJluStp+WWwyVHj+cPJJIkukCAA4BVKjkU0UeYj56zzP8cpU8y7+WGzEQCBKtKdmgitJAgkMvLt294/gX553//1+f1" "5xEh5PKX19+/vST//Ps/yOO3yzKJkygjL1dJ/Jh8Tctkk5HL27KiO7z366Tcp9HtBVzKKPnf/yGv8hi+/RBlRb5nF15efaBZdcEGIG+SHSXRYU3kuDjGZwck" "8sXzRxdFnlfkV1i/5y03F+SJv/DP/fgFu1AeinW0oniVBn4w41dpSq+jisZwOZgE82DNL2/za1rgtSig4YJfW+ZFzC6GfjgJ1/pFr6yKPMMZw3gMP/LfKvoe" "YPxkHa7HdF1f8kK4uDxfxiuxht2BL2AxX8TnK35tHSV4QE+ms+lqtuTXIjgtuBQv1qvpqr7kxckOR5zF55NIu1zmaxih2CyjszCYDUk4DYfkHP6OgoF22ybN" "b9y3hVNxXxxlG7bz9Xq6msz1i8Ys0yl/ch5ok5SH1YqWJTw9iWK68PnVm6jIEgYxOJRVMOVXiyhODqUHNwf+/r1+Da5MzCteekFCdVe5jWLch0+C2f49mSzg" "H7Yof0j4n9FUbmedZ5UHkHz89lCtk+rxkDy+pJuckp//BJ/LKIMV0CJZa3cv4e4f00NJ/i26iooqIpdwl/0gI0DvkJhjfHz0BfmVLPP3Xpl8YFsWOAOXXpBd" "VGySDNb9guyjOGa/w+ePj7bVLh3CrfEtPL2lyWZbIVT8p/gjvwzLW0arq02RHzLYznVUnCHes12u8jQv5DXEOnaV7WUd7ZL0Vv7Gt1f/CoukNbDTJKOemh1A" "yGByQ5dXScUfLXdAc1u27iirkihNopIykkMSWjPk2iZxTDMEBS784mJJ13lB2QZWMAZjRY8fAwTyMqmSHMCxTt7DICTJSloxgHzwkiym7zmgcqANACC9hicB" "M7I8oy9Ivgd0rmBfYpUaaJChEcSaKPU2+D88d3buA/KQKfs3qshi+pR4gf906CQFfzoYkqqAY91HBTxNZv7TwdA57pyNOAnFuDgmCeqBz8+HcIwwZjhBKvHn" "joFrSEVr2OgnA5Q/tiDlJbtoA8d/KNKzx3FURRfswvPyevPle8DHp+PX8JHAx6z86tm2qvYXz5/f3NyMbsajvNg8D33fx5ufkZskrrZfPQtC/5nAXv7l6fgb" "GGSdpLivJP7qWSYv0XeHYnlIabaisKKSflvQ/zzAt9uvnvmj82ckO+zerqromsLM4bPn/KnnfCT+paCrqnViwu/86hnu7Wk4zgZqDFgwfHrMwX6oqjwDkjNI" "Jcm2QMzVC7I6FCXSlYAs0mKS7Q8VnCHQGJxgBMQPMgUW0jpEkzJhlBGnEXgqltIbT4v8S7Lb58BwsgrvuriQtFeuijxNlxEgB9/yBQGW90JxCvbF+YBXbQ+7" "JbKkJu/QhRksS3yXHPjYmBdMajpHZuKMbXQLWL8F9N+ObQhp/CiGW1NaIe6WiK3IXbyRH9AdDvFotKwyRhQKVknGONU6pbBE4EGbzEuAGcOiV5Qf1N8OZZWs" "bz1FR/KHTbS/IHPO7hQLRvFDArxqA4GvUgoqxjelYhDAM2WegkrlBKeTXUv9Y+BADC9ssOUxrohduBEHDeymCSsOKniWcRbBKurJQThPy6HcGZtXXNI/s2eB" "Ue/YdyQOhHvHIbNfBm4MV4Nd8I8pbPqXMw9AxtEfR4bTjJYpjWHwmkdNaqLL8sqLUpAoyPG0ATlb44N4+wJYVtEmHVHjqRHbWKj4SVx7Aipi6Ef8eN9r6oUP" "jBvOuX6GKVADCR45PwfTBSz5TO1rYEHtCZ2s1/PIXo66bC1F7pArXkyn0B9zqGDjqbah9XqxnNujdJympt+1QIzfYUwRjc9naor1IU1r9iR1F/ZTuYMfFLXN" "kdhChdoC10PObh6N1glNY3agxlA1sYb4/JSTsFsjOoVGu+m9oSWR/FAh8xF4aBNdk8ZqfKoJi23x4gKY2Ypu8zRmZ2IAu+ag4uZ1vgJ99NcuXLYx1ydjE3XF" "4SKQo/1eFz6bIgEiw3+Bgnd7JFec4rDLACbhHHXsYF280DXT662umBQUnkmuqaaRBGz1aQ4KL4oOysTd8Seeg8n+j7/DH3KZxBRFnvgKFt+oFJcMacDFAP4L" "thFqBWx8vnwbR/DoQIAp9S1Y+DHdDMFKXPlxMCaouD3xI38Z+AzvDBwRe3fiFGcJ5dKr8r2O7CHDVgbACcfw0RIQJtah3yHHmLgKgr2yHWA6UFmADQZMPtdW" "BDGG9+DuK52IxhNdX+DfLNw/30swltsiya5QqWzC+SRx28WVm6y3Sz8wpOACpaBOmeeCG9i822/l3QxIWbTjdsnRmcU884ZAxqWg5WSL5GCG2guT65rsOuz3" "tFiBumsvgu6kflRWtynjLsUuSi3pKiAnuGpGb46yyUmTTWrmh8ko46jc0rtzygdVaMKmQgO6AIB1oVQT3H8t0I6KeJNVOpFSZ5CrbVR5aVIyzT4V3EnauB5g" "SXSocg3cCyXU+vElTtljQbNsNqQpF2drZwwzpx4b9tBjG6aNy4mAB2ge30iK3YZ81bGqSxMNNbUzbGqlIT9eBY876p/6EKNohXLGOYamj7dhkGAaR6cho1Xi" "VUmVUh1jbrbwG0NsRtM3RbR/0XSVcD5RX6ZpmuzLpOQzJB5AFZiEx2xPzq/k+DuQCIL+dZ8S6qyLO2lIpsos0UcYTC4YNMjbUpD4DqQJpzR9vyFqJB8LDUkV" "Cg72ALZfHz7IjYseu5cKWpNDtaCwgkINg0Ceb7ehZdvpbZjIxhnFNO2t5jeNBabCrPOc+aE0xjJRWsxM4y6g67TrQycxQuYAZrsoaJxUpbdP0Jo4hRlKH3K9" "6EBpXT2s+uO8oY850VeRcAtClyLhT44rEgbQ4Jt3HaW2RSEIW18EO0o2sq40McWhyf9c06TRkqZtpouxuxmKeaHBphRjCFx+6svxmXSxJwIL4qaP9TsZuB9t" "gENhu7Fz5VUA/D+UDPtPwzyT+QprdhQBCgl7pb8iPkVF7jdSxC1WoONKh69L16bvo7cz5l4D3dvRKgLIm1JNCTvtRq6599EtH0ACq9VFSWpNyiwzJ/o/zLwR" "Y5ZlExsZ4gkXbW0y/wCTk+fkexqhT0EznHf4w699WbIJf8voR+81H/809jyx2TOjEx3/lWnbZmGbSMyYQOAP4RTg7wx4wFyo9nGR7z0eAQD1Mz0UZzhZ7bXb" "0exg+95f9FUOjNNmwdyBSfwcRHEhtUFdX+srH6Zt8kHq7Yg390Yy/TiYcPf7mA5HlH998532WSP+0CWCDZDWirBh+/4mqu59Dq+hnzuUZdylEGJHQibdzI/x" "WpcG4MSZFrWDMyFJUk2SAYWxZj5/iVYgrlZbEDwG66FlGW1o2dOQDtGS9qVMPe5gvKHpKmfCYBe9l1xrxlIFVLh9fr1l0yA3Y7TA2FTNoDQvGmdIUZbsIjFx" "UlIympVAectk5S3ph4QWZ6NwOFoMR+NhMNBX4aX5JtcRczrTpT3/ppIA+Jq4I8/CQunf/gycb+HM7Xwbz9qcb/LItqEpTVdptNuf4QkMyeT6Zsi0I+f0jmgh" "eoxsH6mQj2rCva2rSvbd8PJbA4WSrZeHDaByQyZ3+s+DdcG956alg0MxVKkl4rRPGHJg4jBq0+6jFilQR+2XQU/nD7MWuoMdRwKKuOWjLrsuh0tLXDEUcUVg" "NuXGpZZwyIf8ZBWXmM+EyeikSJsp4AZg+BHyBlh+g0WMp0d4BK5NGQQSw5gJLa0FHJ4ZH/Vtp8eQe8lauZzlYbkUOkqNhhg2cnIlEw0dOUR2BtGMZZwYEmdf" "UI/LnBsY3FsWNAIDh/3n4RVb+9TOazF9Wp9CCQoMJkoYe7gPEWhODWaiql1rh09G0kLRTm+hZG8PQS2M3B4+8CDsNvybwh1TA/nZVrd7GETnUIbbjJHD1EDy" "mWa0iqdhKVkd0J3rgmzeZrW2Cpw6xD82xOsSlnUFmBKWsMR1kgGm2Gu4yKqtt9omaXwWDnTK82LK9gYPdzwzdj8zYc/88YrergswJkuxkl9ZCHCB/6BSaSQn" "hFODA5WrKKVnowWCnEzMewPHnYwNmHMyzgEysMh3+tN+G6dDlw0bBbhU62xCNTMMw9f5DnQp0yZcyWu/WhEElvIgNaia+jj/NDRuk3H20tfkrEA7eVqe4mZZ" "uKKgvgpaIlO5IJy1oKyh7fj/EGyi1TxSsGQCk61cEhauSaac6Rx3aoVtOvzQrVaqLcMN1aYHYwpG0/4+yekR1tTqFa8hoPSAFnd2feeIZeid7L4Sgygn5Ykc" "2u3tb99yk38zC6rJ6BkNlJW3hSFrSdLmFA0ba5EHa4yz7HL3tmya6Us58ENP5TC66PejvOsOaXiKbnVsZ/k/47u65ftSqu6Vv48xH/anijEGrXVw9dZ1DRiP" "YixreCC5a1tlizbnPp+fe8bwtBQmREsA7qFi7IgzXZBmqzMmHb/E8Vj25trKR54wGGmqXDg1veTztvDkneIu3faSDgSBRezbQDsuXMS+kaaGM09O0u96OmKE" "rnYSQ5kJlwxf8OnpAue2/Nap8lxs94gMOkKjht+0r03psGZ75g9o53e/bIF6DC5t7pI0op/LaJd4yJ07GPx9jAWncGoR0yyoS7PYK/KbdtuY4a2BOkypguce" "jg13oVKqa048clv/I9K7OpwPCzvTciH1Db5xnj8JvAiLV05LowxVcZFdVxVKJUMAVxYFmC7/gvLT1MnpaMihkWV6zNKeTrmyrq7M/IZ95xsgUcvtm4jKMnlp" "ZuTmTnxdPE38k/nH75pwaB914JaOOt6ZXq2htMGMpHkGpbaU8KZZOPJnA1LkFQi9M28W040Jbnei/Hjakimv70k66bUAYR5HYHVpViCuEqBtCf32QiNe0teI" "w02GZAp2MwbhwtYoHLNe73nCWqTAl44Z3FQzHKTZrpO5smZVycwUg5mNQMRvxuw6dBLSdC6GXc5FBQMPA7M9TeoGfJl/DoavbijGDA0wc5eAL6WcnKmu6hGc" "aeHWftz5nfVg7y2R3Tf6aqSa6Cm69eJh2TOH0jyXHlc+/70UBxhAFG7WAmym3CiTU3NCVZ64NvR+JKxFwzJxGKqaccgfX7Ma6Q6Z38ADJfdtD3gdBbmit94y" "zVdX/VMKVN2V+fypxnnQSyNSuuZpvqUmmi6O+DnEXkrg3aw6Q/f9cHXbwrvQ7zImlnd1FvvNVaqEpnqBIPorC8cZ754D8w5DUGmCcCH1GgOeoqh7YI93yFpG" "BGMPhvRx3MA5oigIr0dkymlXkou6SxYItSRVvM4zYMwRiOZdnuWMqb1wkQ0rLIwAG1bCGycjtoZGM3VlRHCzxfJEzj5RwNZVmKXLH1chWNPpoYq39MCtDGwg" "GGJasewmIxCkKq3u5KnpUYnlLhBwVxKYC122eg4fjao8Kln2REOfUfFc5tURBT38i1Jwzs9P49cSP9msLgD2qSvtlxz6YM6SNtXDoXiUhMo0U7bDUX7lTse0" "uMhE07gXMwr6Uw2lES2KvjmdzRzlWpt9g8VlujJrVZtZmWsPqHbWpW2rqIg7dc/xuWmJYVIDGftt7q+TovYPq2Qe0zH5hmX5miuRpi1tQn9yG5h6zNjvqTQG" "Umm0Rju1dkp/vFdGSKixexbxndxNW+idYjMTcQINw5TUswNggQFiRli6GOJqgCsn3jXQw0qgnhVjirSbUvqRvrFRuTWVBKZCcq4SLUtz3ydk5EzuUUrca59m" "cqgNdymFYQ+m9Dj/fQOCJwQ+FsdLFGB3bXG8toQWm2KFCeTiO0etQmE36EQs7SxNnryMMWDxKgIZQJOMapIF7uE/XlIQGATrAtkdKdIG+Tq/OuxgKeJX71/J" "Dz9fXhLeMAMk0VVOs4yyTltncVSS7w9LsqTbiKaV7PljJ+2SjwOcF38dRWxmqzkQ9yTjD5aUa+ZWqivv65Tgj42hxWfhQqhVJ5CHq6tbOF8Emu4JmjpHsUSw" "Y4HmDrBIgj2OkXszizMIzDROv5EryszssGemqL6/u2rqppOkkbwXzn7rdHZtU6qwXOsyhf5esNX6JysWdE+j6gwBDVNWQzxAOJKzYDrFtEzgmoOBlU9ninyV" "6wDT3jsn62hkrwb/wg4l1UbVkaRFV6IiLL538PZIUiIbTJQg9eS9HI36pXM367daQ7xsISenQzxMMsTMnQyhM2RZ/bWPMpp+YtQx401hKyY/UuvBZOUTCvya" "eRv6ebnFpKuNwMKR1uwoMGBtCD4+qiKWCWmVRSj8TaN9SZn5yj4htKst21YjBusqKj9h+3fBGuf+W5Jo+rJY2GBsOGbDTgZdq7L1PyN/7CyDR+DJjjpsEocX" "zBoomHJCpHol2amOq9ZkIFU0cmoiUJPRMF9LvKGtGXRGJYueNtDT09kzbaKJD8cdsmzhXs5We39vpxhtvW4brnHI08GL9hAqG20ZZXcqzhaPM5F/14y4AtTA" "7qLCWVtGJSZs8hZ07dEEl3HTHMypa2kzNF28WiJRMLUzU8fKJQPqTHXbFsLurPwJJ3ZDqrFmHnyOPYS/e/MSbJA3L3/55ifWSPjbIgezJva+Ax5Tej/vsQcS" "JQe6BP4V0x15BQTk8e7C/y9aAm9wn46OTzPe8clNvNif0/1LOBD9ZNm4qgXwScMHraOPzdG7nCzGeMZDYBkI+wB1lwEpo+pQYFYBkMxT49YEzNHigof3UWVk" "JXfulS2YFEUqeAOoksUJdqPjOPQ9ax2zKXhnLWBeZbQjN2CD0AL+km+jYrmmLKlEWLJGL1gte71H/1ZQZVAPmakGriE2cA3bGrie231Ww7YGrgveGLZu4AoD" "B/6ipYErSIu+A8/NjrNwBPhXDAurDRY4asCWO7NHnfLzuknS1FttUS5oqSf4i8jM8BbCkaw5c9kRf10k6woMCe6+hwP38kOlSiyAA8PJZYAbeLhaQYL2LNN2" "eWGCy8IZx7LZMhMsBEsSSPu96OUdEi9k//FHOFb9nIGtXpQ0BcUV0WqdRnS1pVmj31vTVkYZPoa/53W754a5zEVATSADvaVxv7vN9m/GLVrrN60mvtuqn3zq" "hfZVa+cND8Lvt3jD2Q3zsOQF/r/ISoYvdYStqfqw0YyeFc6D+lSAbwmzDI37GdcdKAcD7KjL1O2/o9Nn19NBW2Zu5jIdF0dhy3oaC9C7tKhTXlYZfC5Resr+" "bvCp2cfroaHVWJ0sdOgzy4OBpLsHlHkkqi3okDjbg/UyUk7chFGe2lly+enRp18BKOdeE8650OAbnbRrofmAiOLaziViIvY2WB8ABH+OCvz8JXnFapRKJq70" "LgBDonXiHBK9E1CvFsPtxHvYIIVYJKQzFI1/SkofEjtcfTrXMPJ/G3sIT9mDSjrtSiRFkd7m/7b02xD1W2GsTfwJSXbi0N7k+RU7mmwttq65Lo52O0mdWf+C" "CXQymj5i5sQjuIstSt798uNb8tM33/7pL98wI7CMDmDvUfQxoZZOySX27FhXN0lxdcg25GSLS0SljPR07qdqhgllPUKzKXxnchu2ghdPGn1bW/qgt2TCNdJU" "rVa5R0rljNrNO8xsh/b1ysSW9Njj+2iGaOXwZk3TnScIVEs2rQTQlX3YsQi7HsbxdKPvX/8ER+fErY3q3GO4V2Q24nHf00xkvTNOho5cT7kflhrwAEjXSMNp" "T8y2k2ROn7wrv/VGpVf6Tqeb0WvhdOztkVobtAJbOr4fdsf6tHYo7tNssA2f2uIHWpiy/WUcWjDs0yx7fFqCNw9g/XYQnHdBEHSMKto4lyPlF6ohwBXdMBa0" "9+iPO+A6ETnTmy9gOdeAaV6id397ahN3xxi+k47SHtEYS2/AO58ZCbEzlvVDnJ6dfz/zeJv8tiKp7sIVHLYzR/EjUzXjW2xDlu9B4a635XonSONmd/MykTjW" "p9xp6r9o6qjaW8aE88vZVlDlp+FpGI2jjh5ea28O1YlX3Vi3EnC05vBUZbY0enzxoJF0YxauyHdxfM7Bj5/e/tm7fPfLm2+0CAi+BbEAHMTmxt/TIvdkV5Qh" "WaU0ysgfAH3S+LONfuhYo7e5WqjmyrL3Sm0FPpz1PnHlXIRaF/Lf2etm9bpc1C3S71BXbEDSKi6uoWBktpsRisl04KpAnUzai41brWnzWI0qZEciyIm1x2b9" "slWuMWurRNay/qYLu/QynPiuXrCM98xE0o17T31LldXTznj1kTIwo3hwIv4ubBJiceo+jVz61/mf3gKssaDaR6MqtI1+05PWftOtQ51SyLxQbgrkr+iGeUNp" "8eGAjrrY5MGwPkq+K7h7hkltFrxnpWWUyIbFreUYHx3PaCEYV/sSmcDKT0hoOSKq45/ydgCbO8nerY4V1V2AyXVSJsskZSFPKwvX3LndL9UngZXIe8EHS+vM" "fk9uzmqv2f0W0KZ1YNI3b8jJQ3dTbMg5mYh2Lw3ddezIU1MFZnr/zUYHWfWr1UTVcWdL902tSEvPMul+B+DCbsNpvw3p/NxgBlz7an2phu0U7BG5txtidr40" "73gHIaQ68cqsC3KVAuqSv0TXyYZFhXk0dSlNTq3nZShKpKUwvF/GliuHLzypkaHg4o/0lxyd2oq/mc2oiuBYgRA7mB6mVjuLqTseKTVZdmAvrkCaoy75Ggw9" "71Wa09UVJWev8/2tx4MFA6M9sjuWYbdp5hVNVttOrqW1VEI6O4/pT28nJ6QBT/VEfEYLPpnpzhox6CHVEtB50xHFowTj9R2PpYn22Fj1GhA5h9xktpGzZ8Jk" "r3jY/E4ZxTOTQ8heA653KTDxCvggyuYNEVVXLJgAfmEqSJ/6fYcDs1KEt8Mw132vg2g0h23vO2x3jBXrAAqSsdlWAb/QpLrdeW7alqz6sM3I7lV25Z/Sh/FY" "4ZVp23S+d8usg5AvMRIgdzdrPPJ61SP1EeplsGoS9aKuZpPV8xm7V7Jr8dYy3FJUlBTVUQ09DPNjbBLpon4To1n7dVmBfbFDNvWatdUhZ++S/V6lVdk8W3Gu" "Ee/C0wKbZqfbErg23WODDa3hrYtXa2v7saC75LAjz8lf6dK7PKy2FD6/2+KA+rLqJmZIrVbHoHPhv0XPU7Q5pZjhvN040amrrddKjc9Wfa5hMc3aSPDYW2qY" "BrlUjEHmBBuN98eudWm9Y43+Uz0JvZNbWHZye+J+s1OqKEESe+psk3qMssQYo9zllTjScb1P0ztXC63W1yU7iGZUbXnPZUdD6Ik7c1HvJD0/xhIUXnQZwx/t" "wDp5yfL/hbEGity7HCiOGK42NWtB17QovYLGhxWNvV0uRTp+51rkF0Pyhcx8ZR+jdUWlX15rTI25DOzhkR/sSv1d8NadAAZ+KxzlAQ2boHFzjVZHx+VVqh7W" "o14neP4sIGDehUqu8HIJyxNZi/QThe4Xq4jfsXiCudWFV7n9wIQfXaR/lTtM+YuWKj8MGRtPEBOB+CER1pvB8MWpMud4Yh2/o1ud+TY+UyQAywPOVpIzoICy" "BEzY5YAU64ICOaTJaqsLhSeluPutyG2RbcmM95IoVyzcLRNS7lh43rsycnZyB6rGIk8tGgwfqJynLehXr0yVipjBDx0fZnZlx9TeXv2SchcisfseqIp24jeq" "aH1rFidGfIr+5Ke/rpb3LzBez6AtvG+Na+vb4Ni5PMR70+SCDLR94PbeXPcWs/HKrT4tbcbTga3cpVHmiXTCs2+SDNTDND3AeBk2EsBfDd1zj7cj2t0bGcOx" "7y7p/ihm6YgSPQw+mlEZVrh8OrdayDJduWTRZ+KIsuNMwnRGXni1gjb+lrXUOx7jEWfNn9TfWHh36pCj7YtkRe9fUG4OJ1740WDktcY1a8mZYYPEtFyd1qKQ" "PZYmO5FilZSaiWC0rfFr1whrRKm9c6VjprY2gMd5nEEBI2FUaqXqsnXFZxsf/+Htqz+94SmhvK/JjzxxWPSH/QNrUcISnb+Psvi2/Gxj4g4H7zxUDl44PuC4" "GxATGZzpmocggete0SQjydtL7z/yfEeWNNkR9AbwqiRCZPI9y7gfqujkkKgXKTSCpIArbDoO7T/n+zW+9AjflkcOoNVesoaKH2gCmnURHUDlxOBThey/+jDg" "s3Y3bGmEP1yvpuUN6ZSC3DWimWrr2A8hT3gbyHewfeerAdmGcWeYCPA+JHh7RSjoznkpACnVqm7RFXIp1fUemDojRuhQ5p4Xxs+6piIYpG/e0KE5yI0xiimZ" "eciqlktmwNOSXGPTH/KqYL2ADmjjADaxqsiWcxRVQHY4PFRL0pLuHBKBEK00u423GSGTsipotdqakTBtGK3ZmKIafqqGlm0+Y1ha8gYOqp8xqPwOQYq8/Srf" "7aOrinxJtnmRfIANwWlwKxQHOvtrUrICwF1ON9zKcoJN9rpoiCsXZlfbYfOaUQ2wkN4rtiuzTF4XDNaPtelgZJec2/JJNqBqeCZ59pjIOW0E2CQEBV8+Q7b8" "JWcigwsD04aqEgVv5L/8RJMtFZTmsEblezyub4jH9B+ZzNbS1Dg0XwKv3Sq6DGsNg7X7HL0EzAcF6jiL/cc1YdZtYk9DcuNhVTelLjjwtoH2nAo1zftYAl8v" "b4ABf/lqL8aCaueAZa2pn48wTrPD3UJHJUChCxko9r4F+ANfOvuxyNdJ+vzlcocF22BxaLIpw08lSSN6WFeiYYDg3voLqt0CSOPL6sXq+vlO6vO1SgAcDWj4" "fSoG6xJrhk1NiOt1uiav4EBhnUSFFYZBh+0uSmGOnzBthe9VL5+yO5D5Kl/y/wDrAK0v", "text/css; charset=utf-8"),
    "js/app.js": ("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" "Oa7NO/D7fwGwcfOh", "application/javascript; charset=utf-8"),
    "admin/index.html": ("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" "rxT+H5NB+EU=", "text/html; charset=utf-8"),
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
            self._json(200, {"ok": True, "server": "Sychos Oracle", "version": "5.0.0", "build": "23.09.2026-UI",
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

        # Admin: Account zuruecksetzen -> Free-Plan + Standard-Credits/-Tokens (Usage wird geleert)
        if path == "/admin/reset":
            if not self._check_admin():
                self._json(403, {"ok": False, "error": "Kein Admin"}); return
            uid = body.get("uid", "")
            db = get_db()
            cur = db.execute("UPDATE users SET plan='free', plan_until=0, is_paid=0, paid_until=0, "
                             "credits=?, bonus_tokens=? WHERE uid=?",
                             (START_CREDITS, START_TOKENS, uid))
            db.execute("DELETE FROM usage WHERE uid=?", (uid,))
            db.commit(); db.close()
            if cur.rowcount == 0:
                self._json(404, {"ok": False, "error": "User nicht gefunden"}); return
            self._json(200, {"ok": True, "plan": "free", "credits": START_CREDITS,
                             "bonus_tokens": START_TOKENS}); return

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
            raw = body.get("paid", True)
            # Plan-Auswahl: 'plan' = free/basic/pro/max/ultra/business (ohne 'plan' alt: paid -> pro)
            pid = (body.get("plan") or "").strip().lower()
            if pid not in PLANS:
                pid = "pro" if not (raw is False or raw == 0 or str(raw).strip().lower() in ("0", "false", "nein", "weg", "off")) else "free"
            # Dauer in Minuten: 0 = dauerhaft, sonst zeitlich begrenzt (laeuft automatisch ab)
            try:
                dur = max(0, int(body.get("duration_minutes", 0) or 0))
            except (TypeError, ValueError):
                dur = 0
            until = (time.time() + dur * 60) if (pid != "free" and dur) else 0
            paid = 0 if pid == "free" else 1
            db = get_db()
            cur = db.execute("UPDATE users SET is_paid=?, paid_until=?, plan=?, plan_until=? WHERE uid=?",
                             (paid, until, pid, until, uid))
            db.commit(); db.close()
            if cur.rowcount == 0:
                self._json(404, {"ok": False, "error": "User nicht gefunden"}); return
            self._json(200, {"ok": True, "paid": bool(paid), "paid_until": until, "plan": pid,
                             "plan_name": PLANS[pid]["name"], "plan_until": until}); return

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
    print("     S Y C H O S   O R A C L E  v5.0     ")
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
