@echo off
title GrievAI Portal
color 0A
echo.
echo  ╔══════════════════════════════════════════════╗
echo  ║   GrievAI Portal — MP Government             ║
echo  ║   Civic Complaint Management System          ║
echo  ╚══════════════════════════════════════════════╝
echo.

cd /d "%~dp0"

python --version >nul 2>&1
if errorlevel 1 (
    color 0C
    echo  ERROR: Python nahi mila! Python install karein.
    pause & exit
)

echo  [1/3] Dependencies install ho rahi hain...
pip install flask flask-cors python-dotenv requests twilio psycopg2-binary gunicorn -q
echo.
echo  [2/3] Database setup ho raha hai...
python init_db.py
echo.
echo  [3/3] Server start ho raha hai...
echo.
echo  ╔══════════════════════════════════════════════╗
echo  ║  Browser mein kholein:                       ║
echo  ║  http://localhost:8000                       ║
echo  ║                                              ║
echo  ║  ⚠  OTP TEST MODE:                          ║
echo  ║  OTP is window mein print hoga!             ║
echo  ╚══════════════════════════════════════════════╝
echo.

python app.py

echo.
pause
