# GrievAI Portal v3.3 — Deploy + Email Setup Guide

---

## ✅ Email System — Kaise Kaam Karta Hai

Jab koi complaint submit karta hai:

**1. User ko milta hai:**
- ✅ Complaint ID confirm
- 📋 Vibhag + Officer + ETA
- 🔍 "Track Complaint" button (direct link)
- 📝 Poora complaint text

**2. Govt Officers ko milta hai:**
- 🚨 Priority-based alert (CRITICAL / HIGH / MEDIUM / LOW)
- 👤 Citizen ka naam, mobile, email, district
- 📍 GPS coordinates
- 🤖 AI classification details + confidence score
- 📝 Poora complaint text

---

## Step 1 — Resend Setup (Free Email Service)

1. **https://resend.com** par jaao
2. **"Sign up"** → GitHub/Google se login karo
3. Left menu → **"API Keys"** → **"Create API Key"**
4. Key copy karo (re_xxxxxxxx...)
5. `.env` mein paste karo:
   ```
   RESEND_API_KEY=re_aabbccdd11223344...
   ```

> ⚠️ Free plan mein sirf `@resend.dev` domain se email jaayegi.
> Production ke liye apna domain verify karo.

---

## Step 2 — Govt Officer Emails Set Karo

`.env` mein:
```
ALERT_EMAILS=collector@indore.gov.in,officer@mpgov.in,admin@gmail.com
```
Ek se zyada emails — comma se alag karo.

---

## Step 3 — APP_URL Set Karo

Railway deploy ke baad jo URL mile (jaise):
```
APP_URL=https://grievai-portal-production.up.railway.app
```
Isse "Track Complaint" button email mein sahi link dikhayega.

---

## Railway Deploy Steps

1. GitHub pe repo banao → files upload karo
2. https://railway.app → New Project → Deploy from GitHub
3. PostgreSQL add karo (+ New → Database → PostgreSQL)
4. Variables tab mein add karo:
   ```
   RESEND_API_KEY     = re_xxxxx
   ALERT_EMAILS       = officer@gov.in
   APP_URL            = https://your-app.up.railway.app
   TWILIO_ACCOUNT_SID = ACxxxxx   (optional)
   TWILIO_AUTH_TOKEN  = xxxxx     (optional)
   TWILIO_PHONE_NUMBER= +1xxxxx   (optional)
   SECRET_KEY         = koi-bhi-random-string
   ```
5. Shell tab mein: `python init_db.py`
6. Done! ✅

---

## Local Testing

```bash
# 1. Dependencies install karo
pip install -r requirements.txt

# 2. DB setup
python init_db.py

# 3. Server chalao
python app.py
# → http://localhost:8000

# OTP + Email TEST MODE mein terminal mein print hoga
```

---

## Files Structure

```
grievai-portal/
├── app.py          ← Main Flask server
├── database.py     ← DB helper (SQLite + PostgreSQL)
├── ai_engine.py    ← AI complaint classifier
├── init_db.py      ← DB setup (ek baar chalao)
├── requirements.txt
├── Procfile        ← Railway start command
├── railway.json    ← Railway config
├── .env            ← Credentials (GitHub pe mat daalo!)
└── static/
    └── index.html  ← Frontend UI
```
