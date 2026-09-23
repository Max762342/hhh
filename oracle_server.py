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
    "gpt-oss-120b":     {"name": "GPT-OSS 120B", "provider": "groq", "factor": 1.0,
                         "api_model": "openai/gpt-oss-120b"},
    "gpt-oss-20b":      {"name": "GPT-OSS 20B", "provider": "groq", "factor": 0.7,
                         "api_model": "openai/gpt-oss-20b"},
    "qwen3-27b":        {"name": "Qwen3 27B", "provider": "groq", "factor": 0.9,
                         "api_model": "qwen/qwen3.8-27b"},
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
    for col in ("last_login REAL DEFAULT 0", "last_seen REAL DEFAULT 0",
                "ban_reason TEXT DEFAULT ''", "ban_until REAL DEFAULT 0",
                "is_paid INTEGER DEFAULT 0", "reg_ip TEXT DEFAULT ''"):
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
    "index.html": ("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" "JM80UTIYOu9Vh2bZ1ns7Xgx4/wXefqAb", "text/html; charset=utf-8"),
    "css/style.css": ("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", "text/css; charset=utf-8"),
    "js/app.js": ("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" "rQ==", "application/javascript; charset=utf-8"),
    "admin/index.html": ("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", "text/html; charset=utf-8"),
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
            paid_ok = bool(u and (u.get("is_paid") or u.get("is_admin")))
            self._json(200, {"ok": True, "is_paid": paid_ok,
                "models": [{"id": k, "name": v["name"], "provider": v["provider"],
                            "factor": v["factor"], "paid": bool(v.get("paid")),
                            "locked": bool(v.get("paid")) and not paid_ok}
                           for k, v in MODELS.items()],
                "strengths": [{"id": k, "name": v["name"], "cost": v["cost"]}
                              for k, v in STRENGTHS.items()]}); return

        if path == "/me":
            user = self._get_user()
            if not user:
                self._json(401, {"ok": False, "error": "Nicht angemeldet"}); return
            d = {"ok": True, "uid": user["uid"], "email": user["email"],
                 "display_name": user["display_name"], "credits": user["credits"],
                 "is_admin": user["is_admin"], "is_paid": bool(user["is_paid"])}
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
            rows = db.execute("SELECT uid,email,display_name,credits,is_banned,ban_reason,ban_until,is_admin,is_paid,created_at,last_seen FROM users").fetchall()
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

        # Register (50 Start-Credits) -> Session-Token
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
            db.execute("INSERT INTO users (uid,email,password_hash,display_name,credits,created_at,reg_ip) VALUES (?,?,?,?,?,?,?)",
                       (uid, email, hash_pw(pw), name, START_CREDITS, time.time(), ip))
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
            if MODELS[model].get("paid") and not (user.get("is_paid") or user.get("is_admin")):
                self._json(402, {"ok": False, "error_type": "premium_locked",
                    "error": "Premium-Modell \u2013 Sychos Paid (9,99/Monat) benoetigt. Bitte ueber das Schloss abschliessen."}); return
            if strength not in STRENGTHS:
                strength = "medium"
            if not msg:
                self._json(400, {"ok": False, "error": "Leere Nachricht"}); return
            if len(msg) > MAX_MSG_LEN:
                self._json(400, {"ok": False, "error": "Nachricht zu lang (max. %d Zeichen)" % MAX_MSG_LEN}); return
            cost = strength_cost(strength, model)

            db = get_db()
            own = db.execute("SELECT id FROM chats WHERE id=? AND uid=?", (cid, user["uid"])).fetchone()
            if not own:
                db.close()
                self._json(404, {"ok": False, "error": "Chat nicht gefunden"}); return
            # Atomare Reservierung: zieht NUR ab, wenn genug Guthaben da ist
            cur = db.execute("UPDATE users SET credits = credits - ? WHERE uid=? AND credits >= ?",
                             (cost, user["uid"], cost))
            db.commit()
            if cur.rowcount == 0:
                db.close()
                self._json(402, {"ok": False, "error": "Nicht genug Credits (noetig: %d)" % cost,
                                 "needed": cost}); return
            db.execute("INSERT INTO messages (chat_id,role,content,model,created_at) VALUES (?,?,?,?,?)",
                       (cid, "user", msg, model, time.time()))
            rows = db.execute("SELECT role,content FROM messages WHERE chat_id=? ORDER BY id",
                              (cid,)).fetchall()
            history = [{"role": r["role"], "content": r["content"]} for r in rows]
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

            def refund():
                db2 = get_db()
                db2.execute("UPDATE users SET credits = credits + ? WHERE uid=?", (cost, user["uid"]))
                db2.commit(); db2.close()

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
                refund()
                e2 = err or {"error": "Leere Antwort – bitte erneut versuchen.", "error_type": "api"}
                push("error", {"error": e2["error"], "error_type": e2.get("error_type", "api")})
                return
            db = get_db()
            db.execute("INSERT INTO messages (chat_id,role,content,model,tokens,cost,created_at) VALUES (?,?,?,?,?,?,?)",
                       (cid, "assistant", text, model, tokens, cost, time.time()))
            bal = db.execute("SELECT credits FROM users WHERE uid=?", (user["uid"],)).fetchone()
            db.commit(); db.close()
            push("done", {"cost": cost, "text": text, "web": web_used,
                          "remaining_credits": bal["credits"] if bal else 0})
            return

        # ── Admin ─────────────────────────────────────────
        if path == "/admin/credits":
            if not self._check_admin():
                self._json(403, {"ok": False, "error": "Kein Admin"}); return
            uid = body.get("uid", "")
            try:
                amount = float(body.get("amount", 0))
            except (TypeError, ValueError):
                self._json(400, {"ok": False, "error": "Ungueltige Menge"}); return
            if abs(amount) > 100000:
                self._json(400, {"ok": False, "error": "Menge zu gross"}); return
            db = get_db()
            cur = db.execute("UPDATE users SET credits = MAX(0, credits + ?) WHERE uid=?", (amount, uid))
            db.commit(); db.close()
            if cur.rowcount == 0:
                self._json(404, {"ok": False, "error": "User nicht gefunden"}); return
            self._json(200, {"ok": True, "uid": uid, "amount": amount}); return

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
            paid = 1 if body.get("paid", True) else 0
            db = get_db()
            cur = db.execute("UPDATE users SET is_paid=? WHERE uid=?", (paid, uid))
            db.commit(); db.close()
            if cur.rowcount == 0:
                self._json(404, {"ok": False, "error": "User nicht gefunden"}); return
            self._json(200, {"ok": True, "paid": bool(paid)}); return

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
    if "--install" in sys.argv:
        if install_autostart():
            sys.exit(0)      # Linux: Service laeuft bereits -> nicht doppelt starten
    elif "--uninstall" in sys.argv:
        uninstall_autostart()
        sys.exit(0)
    main()
