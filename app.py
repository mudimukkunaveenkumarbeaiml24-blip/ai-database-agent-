# -*- coding: utf-8 -*-
# Windows console output encoding fix
import sys
if sys.stdout.encoding and sys.stdout.encoding.lower() in ('cp1252', 'ascii'):
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')
"""
AI Database Agent — FastAPI Backend  (v3.2 — Export / Voice / History)
... (full docstring) ...
Run: uvicorn app:app --reload
"""

import os, re, json, time, random, sqlite3, logging, traceback, csv, io as _io, tempfile, stat
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
    DATABASE_URL: str = "demo.db"
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

settings = Settings()
OPENAI_API_KEY  = settings.OPENAI_API_KEY
OPENAI_API_BASE = settings.OPENAI_API_BASE
OPENAI_MODEL    = settings.OPENAI_MODEL

# ── Robust database path handling ──────────────────────────────────────────
def get_writable_db_path():
    candidates = [
        "/tmp/demo.db",
        os.path.join(tempfile.gettempdir(), "demo.db"),
    ]
    for path in candidates:
        try:
            dirname = os.path.dirname(path)
            if dirname and not os.path.exists(dirname):
                os.makedirs(dirname, exist_ok=True)
            test_file = path + ".write_test"
            with open(test_file, 'w') as f:
                f.write("test")
            os.remove(test_file)
            return path
        except Exception as e:
            logging.warning(f"Path {path} is not writable: {e}")
            continue
    logging.warning("No writable filesystem found – using in‑memory database (data will not persist).")
    return ":memory:"

DB_PATH = get_writable_db_path()
logging.info(f"Using database at: {DB_PATH}")

# If we are using a file, ensure it's not read‑only by removing it if it exists and is read‑only
if DB_PATH != ":memory:" and os.path.exists(DB_PATH):
    try:
        test_conn = sqlite3.connect(DB_PATH)
        test_conn.execute("SELECT 1")
        test_conn.close()
    except sqlite3.OperationalError as e:
        if "readonly" in str(e).lower():
            logging.warning(f"Existing database is read‑only, removing it.")
            os.remove(DB_PATH)

if not OPENAI_API_KEY:
    logging.warning("OPENAI_API_KEY not set — LLM features will use smart fallback.")
else:
    logging.info(f"[OK] API key loaded ({OPENAI_API_KEY[:12]}...)")
    logging.info(f"[OK] Model: {OPENAI_MODEL}")
    logging.info(f"[OK] Base URL: {OPENAI_API_BASE}")

_llm = OpenAI(api_key=OPENAI_API_KEY, base_url=OPENAI_API_BASE, timeout=30.0)


# ── (The rest of your code – get_schema, validate_sql, run_sql, etc.) ──
# I'll omit the full rest for brevity; you already have it.
# Just make sure all functions use the global DB_PATH.
# ── END OF CONFIG ────────────────────────────────────────────────────────────

# ... (insert all your existing functions and routes from your original code) ...


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
    init_db()  # This uses DB_PATH
    logging.info("[Server] Docs → http://localhost:8000/docs")
