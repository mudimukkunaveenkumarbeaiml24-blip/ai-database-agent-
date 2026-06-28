# -*- coding: utf-8 -*-
# Windows console output encoding fix
import sys
if sys.stdout.encoding and sys.stdout.encoding.lower() in ('cp1252', 'ascii'):
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')
"""
AI Database Agent — FastAPI Backend  (v3.2 — Export / Voice / History)
...
Run: uvicorn app:app --reload
"""

import os, re, json, time, random, sqlite3, logging, traceback, csv, io as _io
from datetime import datetime, timedelta
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s: %(message)s",
)

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict
from openai import OpenAI
from dotenv import load_dotenv

# ── env ────────────────────────────────────────────────────────────────────────
load_dotenv(dotenv_path=str(Path(__file__).resolve().parent / ".env"))

# ── app ────────────────────────────────────────────────────────────────────────
app = FastAPI(title="AI Database Agent", version="3.2.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

_BASE_DIR = Path(__file__).resolve().parent

_FRONTEND_DIR = _BASE_DIR.parent / "frontend"
if _FRONTEND_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(_FRONTEND_DIR), html=False), name="static")

@app.get("/", response_class=FileResponse)
def serve_frontend():
    path = _FRONTEND_DIR / "index.html"
    if not path.exists():
        path = _BASE_DIR / "index.html"
    if not path.exists():
        raise HTTPException(status_code=404, detail="index.html not found")
    return FileResponse(path)


# ── config ─────────────────────────────────────────────────────────────────────
class Settings(BaseSettings):
    OPENAI_API_KEY: str = ""
    OPENAI_API_BASE: str = "https://openrouter.ai/api/v1"
    OPENAI_MODEL: str = "google/gemma-4-26b-a4b-it:free"
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

settings = Settings()
OPENAI_API_KEY  = settings.OPENAI_API_KEY
OPENAI_API_BASE = settings.OPENAI_API_BASE
OPENAI_MODEL    = settings.OPENAI_MODEL

# ── FORCE IN‑MEMORY DATABASE – GUARANTEED WRITABLE ──────────────────────────
DB_PATH = ":memory:"
logging.info("🔒 Using in‑memory database (data resets on each start).")

if not OPENAI_API_KEY:
    logging.warning("OPENAI_API_KEY not set — LLM features will use smart fallback.")
else:
    logging.info(f"[OK] API key loaded ({OPENAI_API_KEY[:12]}...)")
    logging.info(f"[OK] Model: {OPENAI_MODEL}")
    logging.info(f"[OK] Base URL: {OPENAI_API_BASE}")

_llm = OpenAI(api_key=OPENAI_API_KEY, base_url=OPENAI_API_BASE, timeout=30.0)


# ══════════════════════════════════════════════════════════════════════════════
# STEP 2 — SCHEMA FETCH
# ══════════════════════════════════════════════════════════════════════════════

def get_schema() -> dict:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    tables = c.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
    schema = {}
    for (t,) in tables:
        cols = c.execute(f"PRAGMA table_info('{t}')").fetchall()
        fks  = c.execute(f"PRAGMA foreign_key_list('{t}')").fetchall()
        schema[t] = {
            "columns": [
                {"name": col[1], "type": col[2], "pk": bool(col[5])}
                for col in cols
            ],
            "foreign_keys": [
                {"column": fk[3], "table": fk[2], "ref_col": fk[4]}
                for fk in fks
            ],
        }
    conn.close()
    return schema


def schema_summary_text(schema: dict) -> str:
    lines = []
    for tbl, meta in schema.items():
        col_str = ", ".join(
            f"{c['name']} ({c['type']}{'  PK' if c['pk'] else ''})"
            for c in meta["columns"]
        )
        lines.append(f"  TABLE {tbl}: {col_str}")
        for fk in meta.get("foreign_keys", []):
            lines.append(f"    FK: {tbl}.{fk['column']} → {fk['table']}.{fk['ref_col']}")
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════════
# STEP 4 — SQL VALIDATION
# ══════════════════════════════════════════════════════════════════════════════

def validate_sql(sql: str, schema: dict) -> tuple:
    errors = []
    forbidden = [r"\bDROP\b", r"\bDELETE\b", r"\bUPDATE\b",
                 r"\bINSERT\b", r"\bALTER\b", r"\bTRUNCATE\b"]
    if any(re.search(p, sql, re.IGNORECASE) for p in forbidden):
        errors.append("Only SELECT queries are allowed.")
        return False, errors
    if not re.match(r"^\s*SELECT\b", sql, re.IGNORECASE):
        errors.append("Query must start with SELECT.")
        return False, errors
    schema_tables_lower = {t.lower() for t in schema}
    from_tables = re.findall(
        r"\bFROM\s+([a-zA-Z_][a-zA-Z0-9_]*)", sql, re.IGNORECASE
    ) + re.findall(
        r"\bJOIN\s+([a-zA-Z_][a-zA-Z0-9_]*)", sql, re.IGNORECASE
    )
    for t in from_tables:
        if t.lower() not in schema_tables_lower:
            errors.append(f"Table '{t}' does not exist in this database.")
    return len(errors) == 0, errors


# ══════════════════════════════════════════════════════════════════════════════
# STEP 5 — SQL EXECUTION
# ══════════════════════════════════════════════════════════════════════════════

def run_sql(sql: str) -> dict:
    sql = sql.strip().rstrip(";")
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    t0 = time.time()
    c.execute(sql)
    rows = c.fetchall()
    elapsed = round((time.time() - t0) * 1000, 1)
    cols = [d[0] for d in c.description] if c.description else []
    conn.close()
    return {
        "columns": cols,
        "rows": [list(r) for r in rows],
        "row_count": len(rows),
        "exec_time_ms": elapsed,
    }


# ══════════════════════════════════════════════════════════════════════════════
# LLM CALL
# ══════════════════════════════════════════════════════════════════════════════

def call_llm(messages: list, temperature=0.3, max_tokens=1500) -> str:
    try:
        resp = _llm.chat.completions.create(
            model=OPENAI_MODEL,
            temperature=temperature,
            max_tokens=max_tokens,
            messages=messages,
        )
        choices = getattr(resp, "choices", None)
        if not choices:
            raise Exception("LLM returned no choices (empty response body)")
        content = getattr(choices[0].message, "content", None)
        if not content or not str(content).strip():
            raise Exception("LLM returned empty content string")
        return str(content).strip()
    except Exception as exc:
        raise Exception(f"LLM call failed: {repr(exc)}")


def extract_json(text: str) -> dict:
    if not text:
        return {}
    text = re.sub(r"<thought>.*?</thought>", "", text, flags=re.DOTALL)
    text = re.sub(r"```(?:json)?", "", text).strip("`").strip()
    if text.startswith("{"):
        try:
            return json.loads(text)
        except Exception:
            pass
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            pass
    m2 = re.search(r'"sql_query"\s*:\s*"(.*?)"(?:\s*[,}])', text, re.DOTALL)
    if m2:
        return {"sql_query": m2.group(1).replace("\\n", "\n")}
    return {}


def call_llm_to_fix_sql(query: str, failed_sql: str, error_message: str, schema_txt: str) -> str:
    prompt = f"""You are an AI Data Analyst Agent. A generated SQL query failed to execute.
Your job is to fix the SQL query based on the database schema and the error message.

DATABASE SCHEMA:
{schema_txt}

USER QUERY: {query}
FAILED SQL: {failed_sql}
ERROR MESSAGE: {error_message}

RULES:
- Use ONLY tables and columns from the VERIFIED SCHEMA. Never hallucinate.
- Generate a correct, optimized, and valid SQLite SELECT query.
- When generating SQL, return only the SQL query inside the "sql_query" key of the JSON response, without explanations or inline comments.
- Return ONLY a JSON object with the key "sql_query":
{{
  "sql_query": "<corrected SQLite SELECT query>"
}}
"""
    try:
        raw = call_llm([{"role": "user", "content": prompt}], temperature=0.1, max_tokens=600)
        parsed = extract_json(raw)
        return parsed.get("sql_query", "").strip()
    except Exception as e:
        logging.error(f"Error calling LLM to fix SQL: {e}")
        return ""


# ══════════════════════════════════════════════════════════════════════════════
# STEP 3 — SMART NLP FALLBACK SQL GENERATOR
# ══════════════════════════════════════════════════════════════════════════════

# ... (all the smart_sql_fallback function – you already have it, so I won't paste it again for brevity)
# I'm assuming you have the full function in your current file. If not, keep the existing one.

# ══════════════════════════════════════════════════════════════════════════════
# ER DIAGRAM BUILDER, CHART SELECTOR, INSIGHT GENERATOR, PIPELINE, etc.
# These are unchanged – copy them from your existing app.py.
# I'll skip to the DATABASE INIT and ROUTES for brevity, but you need to include everything.

# ══════════════════════════════════════════════════════════════════════════════
# DB HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def get_db_stats() -> dict:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    tables = c.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()
    stats = {t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for (t,) in tables}
    conn.close()
    return stats


# ══════════════════════════════════════════════════════════════════════════════
# DATABASE INIT
# ══════════════════════════════════════════════════════════════════════════════

def init_db():
    # For :memory: we don't need to check file existence – just connect and create schema.
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.executescript("""
        CREATE TABLE IF NOT EXISTS regions (
            id INTEGER PRIMARY KEY,
            region_name TEXT NOT NULL,
            country_code TEXT
        );
        CREATE TABLE IF NOT EXISTS customers (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            email TEXT UNIQUE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            category TEXT,
            price REAL NOT NULL,
            stock INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY,
            customer_id INTEGER REFERENCES customers(id),
            region_id INTEGER REFERENCES regions(id),
            total_amount REAL NOT NULL,
            status TEXT DEFAULT 'completed',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS order_items (
            id INTEGER PRIMARY KEY,
            order_id INTEGER REFERENCES orders(id),
            product_id INTEGER REFERENCES products(id),
            quantity INTEGER NOT NULL,
            unit_price REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY,
            order_id INTEGER REFERENCES orders(id),
            amount REAL NOT NULL,
            method TEXT,
            status TEXT DEFAULT 'paid'
        );
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS query_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            query TEXT NOT NULL,
            sql_generated TEXT,
            chart_type TEXT,
            chart_title TEXT,
            row_count INTEGER DEFAULT 0,
            is_favorite INTEGER DEFAULT 0,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
    """)

    # Seed data only if tables are empty (works for :memory: as well)
    if c.execute("SELECT COUNT(*) FROM regions").fetchone()[0] == 0:
        c.executemany("INSERT INTO regions VALUES (?,?,?)", [
            (1,"North America","US"),(2,"Europe","EU"),(3,"Asia Pacific","AP"),
            (4,"Latin America","LA"),(5,"Middle East","ME"),(6,"Africa","AF"),
        ])
        customers = [
            ("Acme Corp","billing@acme.com"),
            ("TechSolutions Ltd","accounts@techsol.io"),
            ("GlobalTrade Inc","finance@globaltrade.net"),
            ("Nexus Partners","ap@nexuspartners.co"),
            ("Vertex Systems","purchasing@vertex.com"),
            ("Apex Retail Group","orders@apexretail.com"),
            ("Orion Dynamics","billing@oriondyn.io"),
            ("CoreLogic Co","finance@corelogic.io"),
            ("Summit Enterprises","ap@summit-e.com"),
            ("Pinnacle Group","accounting@pinnacle.net"),
        ] + [(f"Customer {i}", f"c{i}@example.com") for i in range(11, 101)]
        c.executemany("INSERT INTO customers (name,email) VALUES (?,?)", customers)
        c.executemany("INSERT INTO products (name,category,price,stock) VALUES (?,?,?,?)", [
            ("Enterprise Suite","Software",2999.99,500),
            ("Pro License","Software",499.99,2000),
            ("Hardware Kit","Hardware",1299.99,150),
            ("Support Plan","Services",799.99,999),
            ("Analytics Add-on","Software",299.99,800),
        ])

        monthly_targets = [1200000,1400000,1100000,1600000,1800000,2100000,
                           1900000,2300000,2000000,2847320,3100000,2850000]
        region_weights  = [0.38,0.24,0.19,0.11,0.05,0.03]
        base = datetime(2024, 1, 1)
        oid = pid = payid = 1

        for mi, target in enumerate(monthly_targets):
            mo = base + timedelta(days=30*mi)
            num = int(target / 307)
            region_pool = []
            for ri, w in enumerate(region_weights):
                region_pool.extend([ri+1]*int(num*w))
            for rid in region_pool:
                cid  = random.randint(1, 100)
                amt  = round(random.uniform(100, 2000), 2)
                dt   = (mo + timedelta(days=random.randint(0, 27))).strftime("%Y-%m-%d %H:%M:%S")
                stat = "completed" if random.random() > 0.05 else "refunded"
                c.execute("INSERT INTO orders VALUES (?,?,?,?,?,?)", (oid,cid,rid,amt,stat,dt))
                c.execute("INSERT INTO order_items VALUES (?,?,?,?,?)",
                          (pid, oid, random.randint(1,5), random.randint(1,5), round(amt/2,2)))
                c.execute("INSERT INTO payments VALUES (?,?,?,?,?)",
                          (payid, oid, amt,
                           random.choice(["credit_card","bank_transfer","paypal"]),
                           "paid" if stat=="completed" else "refunded"))
                oid+=1; pid+=1; payid+=1

    conn.commit()
    conn.close()
    print(f"[DB] Ready → {DB_PATH}")


# ══════════════════════════════════════════════════════════════════════════════
# PYDANTIC MODELS
# ══════════════════════════════════════════════════════════════════════════════

class ChatRequest(BaseModel):
    session_id: str
    query: str
    history: list = []

# ... (other models – you already have them)

# ══════════════════════════════════════════════════════════════════════════════
# ROUTES – only showing the essential ones; you already have them.
# ══════════════════════════════════════════════════════════════════════════════

# I'll include the diagnostic endpoint again to confirm.

@app.get("/api/dbstatus")
def db_status():
    writable = False
    error = None
    try:
        test_conn = sqlite3.connect(DB_PATH)
        test_conn.execute("CREATE TABLE IF NOT EXISTS _test (x int)")
        test_conn.execute("INSERT INTO _test VALUES (1)")
        test_conn.commit()
        test_conn.close()
        writable = True
    except Exception as e:
        error = str(e)
    return {
        "db_path": DB_PATH,
        "is_writable": writable,
        "error": error,
    }


@app.post("/api/chat")
async def chat(body: ChatRequest):
    # ... (your existing chat endpoint – unchanged)
    pass


# ── Other endpoints (history, export, etc.) remain exactly as before ──

# ══════════════════════════════════════════════════════════════════════════════
# STARTUP
# ══════════════════════════════════════════════════════════════════════════════

@app.on_event("startup")
def startup():
    import platform, pydantic, fastapi as _fa
    logging.info(f"Python version: {platform.python_version()}")
    logging.info(f"Pydantic version: {pydantic.__version__}")
    logging.info(f"FastAPI version: {_fa.__version__}")
    logging.info(f"Loaded model: {OPENAI_MODEL}")
    logging.info(f"API base URL: {OPENAI_API_BASE}")
    base_url_lower = OPENAI_API_BASE.lower()
    if "openrouter.ai" in base_url_lower:
        provider = "OpenRouter"
    elif "api.openai.com" in base_url_lower:
        provider = "OpenAI"
    elif "nvidia" in base_url_lower:
        provider = "NVIDIA"
    else:
        provider = "OpenAI-Compatible"
    logging.info(f"[Server] Active Provider: {provider}")
    init_db()
    logging.info("[Server] Docs → http://localhost:8000/docs")
