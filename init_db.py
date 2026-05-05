"""
GrievAI — Database Initialization
Run this ONCE after deploying to Railway.
Command: python init_db.py
"""
from dotenv import load_dotenv
load_dotenv()

from database import init_db, USE_POSTGRES

print("\n" + "="*50)
print("  GrievAI — Database Setup")
print("="*50)
print(f"  Mode: {'PostgreSQL' if USE_POSTGRES else 'SQLite (local)'}")
print("="*50 + "\n")

init_db()

print("\n✅ Database ready! Ab server start karein:")
print("   python app.py\n")
