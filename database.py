"""
GrievAI — Database Helper Module
Supports both PostgreSQL (Railway) and SQLite (local)
"""
import os
import sqlite3
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.environ.get('DATABASE_URL', '')
USE_POSTGRES = bool(DATABASE_URL)

if USE_POSTGRES:
    import psycopg2
    if DATABASE_URL.startswith('postgres://'):
        DATABASE_URL = DATABASE_URL.replace('postgres://', 'postgresql://', 1)

BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
SQLITE_PATH = os.path.join(BASE_DIR, 'data', 'grievai.db')

if not USE_POSTGRES:
    os.makedirs(os.path.join(BASE_DIR, 'data'), exist_ok=True)

DEPARTMENTS = [
    ("Water Supply",    "water",       "Er. Suresh Patel",      "+91-731-2700100", 0),
    ("Roads & PWD",     "roads",       "EE Rakesh Dubey",       "+91-731-2700200", 0),
    ("Electricity",     "electricity", "Er. Anil Sharma",       "+91-731-2700300", 0),
    ("Sanitation",      "sanitation",  "Sanitation Inspector",  "+91-731-2700400", 0),
    ("Public Services", "services",    "Ward Officer",          "+91-731-2700500", 0),
    ("Healthcare",      "healthcare",  "CMO Dr. Priya Sharma",  "+91-731-2700600", 0),
]

SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS otp_verifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mobile TEXT NOT NULL,
    otp TEXT NOT NULL,
    verified INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now')),
    expires_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS citizens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT UNIQUE NOT NULL,
    mobile TEXT,
    name TEXT,
    password_hash TEXT,
    verified INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now')),
    last_login TEXT
);
CREATE TABLE IF NOT EXISTS complaints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    complaint_id TEXT UNIQUE NOT NULL,
    citizen_name TEXT NOT NULL,
    mobile TEXT NOT NULL,
    mobile_verified INTEGER DEFAULT 0,
    district TEXT,
    area TEXT,
    language TEXT DEFAULT 'en',
    raw_text TEXT NOT NULL,
    department TEXT,
    category TEXT,
    priority TEXT DEFAULT 'medium',
    status TEXT DEFAULT 'open',
    ai_confidence REAL DEFAULT 0.0,
    ai_summary TEXT,
    eta_days TEXT,
    officer_name TEXT,
    dept_full TEXT,
    latitude REAL,
    longitude REAL,
    location_accuracy REAL,
    input_mode TEXT DEFAULT 'text',
    photo_count INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS timeline_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    complaint_id TEXT NOT NULL,
    event_title TEXT NOT NULL,
    event_desc TEXT,
    event_time TEXT DEFAULT (datetime('now')),
    status TEXT DEFAULT 'done'
);
CREATE TABLE IF NOT EXISTS departments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    short_name TEXT NOT NULL,
    officer_name TEXT,
    contact TEXT,
    complaint_count INTEGER DEFAULT 0
);
"""

def get_conn():
    """Get DB connection — PostgreSQL or SQLite"""
    if USE_POSTGRES:
        return psycopg2.connect(DATABASE_URL)
    conn = sqlite3.connect(SQLITE_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn

def _sql(sql):
    """Convert %s placeholders for SQLite"""
    return sql if USE_POSTGRES else sql.replace('%s', '?')

def qexec(conn, sql, params=()):
    cur = conn.cursor()
    cur.execute(_sql(sql), params)
    return cur

def qmany(conn, sql, rows):
    cur = conn.cursor()
    cur.executemany(_sql(sql), rows)
    return cur

def to_dict(cur, row):
    if row is None: return None
    if USE_POSTGRES:
        cols = [d[0] for d in cur.description]
        return {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in zip(cols, row)}
    return dict(row)

def all_dicts(cur):
    rows = cur.fetchall()
    if USE_POSTGRES:
        cols = [d[0] for d in cur.description]
        return [{k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in zip(cols, r)} for r in rows]
    return [dict(r) for r in rows]

def init_db():
    """Initialize database tables and seed departments"""
    conn = get_conn()
    db_type = "PostgreSQL" if USE_POSTGRES else "SQLite"
    print(f"[DB] Connecting to {db_type}...")

    if USE_POSTGRES:
        cur = conn.cursor()
        postgres_tables = [
            """CREATE TABLE IF NOT EXISTS otp_verifications (
                id SERIAL PRIMARY KEY, mobile TEXT NOT NULL, otp TEXT NOT NULL,
                verified BOOLEAN DEFAULT FALSE, created_at TIMESTAMP DEFAULT NOW(), expires_at TIMESTAMP NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS citizens (
                id SERIAL PRIMARY KEY, email TEXT UNIQUE NOT NULL,
                mobile TEXT, name TEXT, password_hash TEXT,
                verified BOOLEAN DEFAULT FALSE, created_at TIMESTAMP DEFAULT NOW(),
                last_login TIMESTAMP)""",
            """CREATE TABLE IF NOT EXISTS complaints (
                id SERIAL PRIMARY KEY, complaint_id TEXT UNIQUE NOT NULL,
                citizen_name TEXT NOT NULL, mobile TEXT NOT NULL, mobile_verified BOOLEAN DEFAULT FALSE,
                district TEXT, area TEXT, language TEXT DEFAULT 'en', raw_text TEXT NOT NULL,
                department TEXT, category TEXT, priority TEXT DEFAULT 'medium', status TEXT DEFAULT 'open',
                ai_confidence REAL DEFAULT 0.0, ai_summary TEXT, eta_days TEXT, officer_name TEXT, dept_full TEXT,
                latitude REAL, longitude REAL, location_accuracy REAL, input_mode TEXT DEFAULT 'text',
                photo_count INTEGER DEFAULT 0, created_at TIMESTAMP DEFAULT NOW(), updated_at TIMESTAMP DEFAULT NOW())""",
            """CREATE TABLE IF NOT EXISTS timeline_events (
                id SERIAL PRIMARY KEY, complaint_id TEXT NOT NULL, event_title TEXT NOT NULL,
                event_desc TEXT, event_time TIMESTAMP DEFAULT NOW(), status TEXT DEFAULT 'done')""",
            """CREATE TABLE IF NOT EXISTS departments (
                id SERIAL PRIMARY KEY, name TEXT NOT NULL, short_name TEXT NOT NULL,
                officer_name TEXT, contact TEXT, complaint_count INTEGER DEFAULT 0)""",
        ]
        for stmt in postgres_tables:
            cur.execute(stmt)

        # Add missing columns if upgrading
        for col_sql in [
            "ALTER TABLE citizens ADD COLUMN IF NOT EXISTS name TEXT",
            "ALTER TABLE citizens ADD COLUMN IF NOT EXISTS email TEXT",
            "ALTER TABLE citizens ADD COLUMN IF NOT EXISTS password_hash TEXT",
            "ALTER TABLE citizens ADD COLUMN IF NOT EXISTS last_login TIMESTAMP",
            "ALTER TABLE complaints ADD COLUMN IF NOT EXISTS latitude REAL",
            "ALTER TABLE complaints ADD COLUMN IF NOT EXISTS longitude REAL",
            "ALTER TABLE complaints ADD COLUMN IF NOT EXISTS location_accuracy REAL",
            "ALTER TABLE complaints ADD COLUMN IF NOT EXISTS input_mode TEXT DEFAULT 'text'",
            "ALTER TABLE complaints ADD COLUMN IF NOT EXISTS photo_count INTEGER DEFAULT 0",
        ]:
            try: cur.execute(col_sql)
            except: pass

        cur.execute("SELECT COUNT(*) FROM departments")
        if cur.fetchone()[0] == 0:
            cur.executemany(
                "INSERT INTO departments(name,short_name,officer_name,contact,complaint_count) VALUES(%s,%s,%s,%s,%s)",
                DEPARTMENTS
            )
        conn.commit()
        cur.close()
    else:
        conn.executescript(SQLITE_SCHEMA)
        for col, ct in [
            ('latitude', 'REAL'), ('longitude', 'REAL'), ('location_accuracy', 'REAL'),
            ('input_mode', "TEXT DEFAULT 'text'"), ('photo_count', 'INTEGER DEFAULT 0')
        ]:
            try: conn.execute(f'ALTER TABLE complaints ADD COLUMN {col} {ct}'); conn.commit()
            except: pass
        cur = conn.execute("SELECT COUNT(*) FROM departments")
        if cur.fetchone()[0] == 0:
            conn.executemany(
                "INSERT INTO departments(name,short_name,officer_name,contact,complaint_count) VALUES(?,?,?,?,?)",
                DEPARTMENTS
            )
        conn.commit()

    conn.close()
    print(f"[DB] ✅ Ready — {db_type}")
    if not USE_POSTGRES:
        print(f"[DB] 📁 SQLite file: {SQLITE_PATH}")
