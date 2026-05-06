"""
GrievAI Production Server v3.2.2
- FIXED: Email verification enforce kiya — bina verify ke login nahi hoga
- FIXED: Galat email pe registration rok — already verified check
- FIXED: Unverified user login karne pe dobara verification email bheja jayega
- FIXED: %s vs ? placeholder for SQLite/PostgreSQL
- FIXED: to_dict None check
- FIXED: otp LIKE query
- FIXED: Reset URL ab reset-password.html pe jaayega (login.html nahi)
"""
import os, random, string, threading, requests, traceback, hashlib, secrets
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__, static_folder='static', static_url_path='')
CORS(app)

from database import init_db as _init_db
_init_db()

DATABASE_URL   = os.environ.get('DATABASE_URL', '')
TWILIO_SID     = os.environ.get('TWILIO_ACCOUNT_SID', '')
TWILIO_TOKEN   = os.environ.get('TWILIO_AUTH_TOKEN', '')
TWILIO_FROM    = os.environ.get('TWILIO_PHONE_NUMBER', '')
RESEND_API_KEY = os.environ.get('RESEND_API_KEY', '')
ALERT_EMAILS   = os.environ.get('ALERT_EMAILS', '')
ANTHROPIC_KEY  = os.environ.get('ANTHROPIC_API_KEY', '')

USE_POSTGRES = bool(DATABASE_URL)
USE_TWILIO   = bool(TWILIO_SID and TWILIO_TOKEN and TWILIO_FROM)
USE_EMAIL    = bool(RESEND_API_KEY and ALERT_EMAILS)
USE_CLAUDE   = bool(ANTHROPIC_KEY)

from database import get_conn, qexec, qmany, to_dict, all_dicts, init_db
from ai_engine import classify_complaint, calculate_stats

PRIORITY_EMOJI = {'critical': '🚨 CRITICAL', 'high': '🔴 HIGH', 'medium': '🟡 MEDIUM', 'low': '🟢 LOW'}

def err(msg, code=400):
    return jsonify({"error": msg}), code

def gen_id():
    return f"GRV-{datetime.now().strftime('%y%m%d')}-{''.join(random.choices(string.digits, k=4))}"

def fmt_mobile(m):
    m = m.strip().replace(' ', '').replace('-', '')
    if m.startswith('0'): m = m[1:]
    if not m.startswith('+'): m = '+91' + m
    return m

def fix_sql(sql):
    if not USE_POSTGRES:
        sql = sql.replace('%s', '?')
    return sql

# ── OTP SERVICE ───────────────────────────────────────────────────────────────
def send_otp_svc(mobile):
    mobile = fmt_mobile(mobile)
    otp    = ''.join(random.choices(string.digits, k=6))
    exp    = (datetime.now() + timedelta(minutes=10)).isoformat()
    try:
        conn = get_conn()
        qexec(conn, fix_sql("UPDATE otp_verifications SET verified=1 WHERE mobile=%s AND verified=0"), (mobile,))
        qexec(conn, fix_sql("INSERT INTO otp_verifications(mobile,otp,verified,expires_at) VALUES(%s,%s,0,%s)"), (mobile, otp, exp))
        conn.commit()
        conn.close()
    except Exception as e:
        return {"success": False, "error": f"DB error: {e}"}

    if USE_TWILIO:
        try:
            from twilio.rest import Client
            Client(TWILIO_SID, TWILIO_TOKEN).messages.create(
                body=f"GrievAI Portal - MP Govt\nOTP: {otp}\nValid 10 min. Do not share.",
                from_=TWILIO_FROM, to=mobile)
            print(f"[OTP] SMS sent to {mobile[:4]}****{mobile[-3:]}")
        except Exception as e:
            return {"success": False, "error": f"SMS failed: {e}"}
    else:
        border = "═" * 52
        print(f"\n  ╔{border}╗")
        print(f"  ║{'':^52}║")
        print(f"  ║{'🔐  GrievAI OTP — TEST MODE':^52}║")
        print(f"  ║{'':^52}║")
        print(f"  ║  📱 Mobile : {mobile:<38}║")
        print(f"  ║  🔢 OTP    : {otp:<38}║")
        print(f"  ║  ⏱  Valid  : 10 minutes{'':<27}║")
        print(f"  ║{'':^52}║")
        print(f"  ╚{border}╝\n")

    return {"success": True, "message": f"OTP sent to {mobile[:3]}****{mobile[-3:]}", "test_mode": not USE_TWILIO, "expires_in": 600}

def verify_otp_svc(mobile, otp):
    mobile = fmt_mobile(mobile)
    try:
        conn = get_conn()
        cur  = qexec(conn, fix_sql("SELECT id,otp,expires_at FROM otp_verifications WHERE mobile=%s AND verified=0 ORDER BY id DESC LIMIT 1"), (mobile,))
        row  = cur.fetchone()
        if not row:
            conn.close()
            return {"success": False, "error": "OTP nahi mila ya expire ho gaya।"}
        if USE_POSTGRES:
            rid, stored, exp = row
            expired = datetime.now() > exp
        else:
            row = dict(row)
            rid, stored, exp = row['id'], row['otp'], row['expires_at']
            expired = datetime.now().isoformat() > exp
        if expired:
            conn.close()
            return {"success": False, "error": "OTP expire ho gaya। Naya OTP mangvaein।"}
        if str(otp).strip() != str(stored).strip():
            conn.close()
            return {"success": False, "error": "Galat OTP। Sahi number enter karein।"}
        qexec(conn, fix_sql("UPDATE otp_verifications SET verified=1 WHERE id=%s"), (rid,))
        if USE_POSTGRES:
            qexec(conn, fix_sql("INSERT INTO citizens(mobile,verified) VALUES(%s,TRUE) ON CONFLICT(mobile) DO UPDATE SET verified=TRUE"), (mobile,))
        else:
            qexec(conn, fix_sql("INSERT OR REPLACE INTO citizens(mobile,verified) VALUES(%s,1)"), (mobile,))
        conn.commit()
        conn.close()
        print(f"[OTP] ✅ Verified: {mobile[:4]}****{mobile[-3:]}")
        return {"success": True, "verified": True, "message": "Mobile verified! ✅"}
    except Exception as e:
        return {"success": False, "error": str(e)}

# ── EMAIL ─────────────────────────────────────────────────────────────────────
def _user_email_html(complaint, ai):
    return f"<h3>Complaint {complaint['complaint_id']} submitted</h3><p>{complaint['raw_text']}</p>"

def _govt_email_html(complaint, ai):
    return f"<h3>New Complaint: {complaint['complaint_id']}</h3><p>Department: {ai['department']}<br>Priority: {ai['priority']}</p><p>{complaint['raw_text']}</p>"

def send_emails(complaint, ai):
    cid = complaint['complaint_id']
    user_email = complaint.get('email', '').strip()
    has_user_email = bool(user_email and '@' in user_email)
    if not USE_EMAIL:
        border = "─" * 52
        print(f"\n  ┌{border}┐")
        print(f"  │{'  📧  EMAIL TEST MODE':^52}│")
        print(f"  ├{border}┤")
        print(f"  │  TO (User) : {user_email if has_user_email else 'NOT PROVIDED':<37}│")
        print(f"  ├{border}┤")
        print(f"  │  TO (Govt) : {ALERT_EMAILS[:37] if ALERT_EMAILS else 'NOT SET':<37}│")
        print(f"  └{border}┘\n")
        return
    def _send():
        headers = {"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"}
        if has_user_email:
            try:
                requests.post("https://api.resend.com/emails", headers=headers, json={
                    "from": "GrievAI Portal <noreply@resend.dev>", "to": [user_email],
                    "subject": f"✅ Complaint {cid} confirmed", "html": _user_email_html(complaint, ai)
                }, timeout=15)
            except Exception as e:
                print(f"[EMAIL] ❌ User email error: {e}")
        govt_to = [e.strip() for e in ALERT_EMAILS.split(',') if e.strip()]
        if govt_to:
            try:
                requests.post("https://api.resend.com/emails", headers=headers, json={
                    "from": "GrievAI Alerts <noreply@resend.dev>", "to": govt_to,
                    "subject": f"[GrievAI] {ai['priority'].upper()} | {cid}", "html": _govt_email_html(complaint, ai)
                }, timeout=15)
            except Exception as e:
                print(f"[EMAIL] ❌ Govt email error: {e}")
    threading.Thread(target=_send, daemon=True).start()

def send_email_alert(complaint, ai):
    send_emails(complaint, ai)

def analyze_photo_with_claude(image_b64, media_type='image/jpeg'):
    if not USE_CLAUDE: return None
    try:
        r = requests.post("https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": "claude-3-sonnet-20240229", "max_tokens": 400, "messages": [{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": image_b64}},
                {"type": "text", "text": "Analyze this photo and write a civic complaint in Hindi (2-3 sentences)."}
            ]}]}, timeout=20)
        if r.status_code == 200:
            data = r.json()
            return data['content'][0]['text'] if data.get('content') else None
    except Exception as e:
        print(f"[CLAUDE] Error: {e}")
    return None

# ── ROUTES ───────────────────────────────────────────────────────────────────
@app.route('/')
def index():
    return send_from_directory('static', 'index.html')

@app.route('/<path:p>')
def static_files(p):
    try: return send_from_directory('static', p)
    except: return send_from_directory('static', 'index.html')

@app.route('/api/health')
def health():
    db_ok = False
    try:
        conn = get_conn(); conn.close(); db_ok = True
    except: pass
    return jsonify({"status": "ok", "version": "3.2.2", "db": "PostgreSQL ✅" if USE_POSTGRES else "SQLite ✅",
        "db_connected": db_ok, "otp_mode": "Twilio SMS" if USE_TWILIO else "Terminal (Test Mode)",
        "email": "Resend Active" if USE_EMAIL else "Test Mode", "claude": "Active" if USE_CLAUDE else "Disabled",
        "timestamp": datetime.now().isoformat()})

@app.route('/api/otp/send', methods=['POST'])
def otp_send():
    d = request.get_json() or {}
    m = d.get('mobile', '').strip()
    if not m: return err("mobile required")
    return jsonify(send_otp_svc(m))

@app.route('/api/otp/verify', methods=['POST'])
def otp_verify():
    d = request.get_json() or {}
    return jsonify(verify_otp_svc(d.get('mobile', ''), d.get('otp', '')))

@app.route('/api/analyze-photo', methods=['POST'])
def analyze_photo():
    d = request.get_json() or {}
    desc = analyze_photo_with_claude(d.get('image', ''), d.get('media_type', 'image/jpeg'))
    if desc: return jsonify({"success": True, "description": desc})
    return jsonify({"success": False, "description": "फोटो प्राप्त हुई। कृपया विवरण लिखें।"})

@app.route('/api/complaints', methods=['GET', 'POST'])
def complaints():
    if request.method == 'POST':
        try:
            d = request.get_json() or {}
            for f in ['citizen_name', 'mobile', 'raw_text']:
                if not d.get(f, '').strip(): return err(f"'{f}' is required")
            mobile = d['mobile'].strip(); email = d.get('email', '').strip()
            text = d['raw_text'].strip(); ai = classify_complaint(text)
            cid = gen_id(); now = datetime.now().isoformat()
            lat = d.get('latitude'); lng = d.get('longitude')
            acc = d.get('location_accuracy'); mode = d.get('input_mode', 'text')
            photos = int(d.get('photo_count', 0))
            conn = get_conn()
            qexec(conn, fix_sql("""INSERT INTO complaints(
                complaint_id,citizen_name,mobile,mobile_verified,district,area,language,
                raw_text,department,category,priority,status,ai_confidence,ai_summary,
                eta_days,officer_name,dept_full,latitude,longitude,location_accuracy,
                input_mode,photo_count,created_at,updated_at
            ) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"""),
                (cid, d.get('citizen_name','').strip(), mobile, 1,
                 d.get('district',''), d.get('area',''), d.get('language', ai['language']),
                 text, ai['department'], ai['category'], ai['priority'], 'open',
                 ai['confidence'], ai['summary'], ai['eta'], ai['officer'], ai['dept_full'],
                 lat, lng, acc, mode, photos, now, now))
            loc_note = f" | Location: {lat:.5f},{lng:.5f}" if lat and lng else ""
            qmany(conn, fix_sql("INSERT INTO timeline_events(complaint_id,event_title,event_desc,event_time,status) VALUES(%s,%s,%s,%s,%s)"), [
                (cid, "Complaint received", f"Submitted by {d.get('citizen_name','')} from {d.get('area','')}, {d.get('district','')}.{loc_note}", now, "done"),
                (cid, "AI classification complete", f"Classified as {ai['department']} with {ai['confidence']}% confidence.", now, "done"),
            ])
            qexec(conn, fix_sql("UPDATE departments SET complaint_count=complaint_count+1 WHERE name=%s"), (ai['department'],))
            conn.commit()
            cur = qexec(conn, fix_sql("SELECT * FROM complaints WHERE complaint_id=%s"), (cid,))
            row = to_dict(cur, cur.fetchone()); conn.close()
            print(f"\n[COMPLAINT] ✅ {cid} → {ai['department']} | {ai['priority'].upper()}\n")
            send_emails({'complaint_id': cid, 'citizen_name': d.get('citizen_name',''), 'mobile': mobile,
                'email': email, 'raw_text': text, 'district': d.get('district',''),
                'area': d.get('area',''), 'latitude': lat, 'longitude': lng, 'input_mode': mode, 'photo_count': photos}, ai)
            return jsonify({"success": True, "complaint_id": cid, "complaint": row, "ai_result": ai}), 201
        except Exception as e:
            traceback.print_exc(); return jsonify({"error": str(e)}), 500
    else:
        try:
            dept = request.args.get('department'); status = request.args.get('status')
            priority = request.args.get('priority')
            limit = int(request.args.get('limit', 50)); offset = int(request.args.get('offset', 0))
            where, params = [], []
            if dept:     where.append(fix_sql("department=%s")); params.append(dept)
            if status:   where.append(fix_sql("status=%s"));     params.append(status)
            if priority: where.append(fix_sql("priority=%s"));   params.append(priority)
            wsql = ("WHERE " + " AND ".join(where)) if where else ""
            conn = get_conn()
            cur  = qexec(conn, fix_sql(f"SELECT * FROM complaints {wsql} ORDER BY created_at DESC LIMIT %s OFFSET %s"), params + [limit, offset])
            comps = all_dicts(cur)
            cur2  = qexec(conn, fix_sql(f"SELECT COUNT(*) FROM complaints {wsql}"), params)
            total = cur2.fetchone()[0]; conn.close()
            return jsonify({"complaints": comps, "total": total, "stats": calculate_stats(comps)})
        except Exception as e:
            traceback.print_exc(); return jsonify({"error": str(e)}), 500

@app.route('/api/complaints/<cid>', methods=['GET', 'PATCH'])
def complaint_detail(cid):
    try:
        conn = get_conn()
        cur  = qexec(conn, fix_sql("SELECT * FROM complaints WHERE complaint_id=%s"), (cid,))
        fetched = cur.fetchone()
        if fetched is None: conn.close(); return err(f"Complaint {cid} not found", 404)
        row = to_dict(cur, fetched)
        if request.method == 'GET':
            cur2 = qexec(conn, fix_sql("SELECT * FROM timeline_events WHERE complaint_id=%s ORDER BY event_time ASC"), (cid,))
            tl = all_dicts(cur2); conn.close()
            return jsonify({"complaint": row, "timeline": tl})
        d = request.get_json() or {}
        allowed = ['status', 'priority', 'officer_name', 'eta_days']
        updates = {f: d[f] for f in allowed if f in d}
        if not updates: conn.close(); return err("No valid fields")
        updates['updated_at'] = datetime.now().isoformat()
        sc = ", ".join(fix_sql(f"{k}=%s") for k in updates)
        qexec(conn, fix_sql(f"UPDATE complaints SET {sc} WHERE complaint_id=%s"), list(updates.values()) + [cid])
        if 'status' in updates:
            lbs = {'in_progress': ("Work In Progress", "Department ne kaam shuru kar diya।"),
                   'resolved': ("Issue Resolved", "Complaint resolve kar di gayi।"),
                   'closed': ("Complaint Closed", "Case closed।")}
            lb, dc = lbs.get(updates['status'], ("Status Updated", "Status badla।"))
            qexec(conn, fix_sql("INSERT INTO timeline_events(complaint_id,event_title,event_desc,event_time,status) VALUES(%s,%s,%s,%s,%s)"),
                  (cid, lb, dc, datetime.now().isoformat(), 'done'))
        conn.commit()
        cur3 = qexec(conn, fix_sql("SELECT * FROM complaints WHERE complaint_id=%s"), (cid,))
        updated = to_dict(cur3, cur3.fetchone()); conn.close()
        return jsonify({"success": True, "complaint": updated})
    except Exception as e:
        traceback.print_exc(); return jsonify({"error": str(e)}), 500

@app.route('/api/analytics')
def analytics():
    try:
        import random as rnd
        conn = get_conn()
        cur  = qexec(conn, "SELECT * FROM complaints"); comps = all_dicts(cur)
        cur2 = qexec(conn, "SELECT * FROM departments ORDER BY complaint_count DESC"); depts = all_dicts(cur2)
        conn.close(); stats = calculate_stats(comps)
        return jsonify({
            "summary": {"total_complaints": len(comps), "resolved_today": sum(1 for c in comps if c.get('status') == 'resolved'),
                        "avg_resolution_days": 2.4, "ai_accuracy": 94.2},
            "dept_stats": stats.get("dept_counts", {}), "priority_stats": stats.get("priority_counts", {}),
            "status_stats": stats.get("status_counts", {}), "language_stats": stats.get("lang_counts", {}),
            "departments": depts,
            "weekly_trend": [{"week": f"W{8-i}", "submitted": (s := rnd.randint(180, 350)), "resolved": int(s * rnd.uniform(0.6, 0.9))} for i in range(7, -1, -1)],
            "resolution_times": {"Water Supply": 3.2, "Roads & PWD": 5.1, "Electricity": 1.8, "Sanitation": 2.4, "Public Services": 4.0, "Healthcare": 2.1}
        })
    except Exception as e:
        traceback.print_exc(); return jsonify({"error": str(e)}), 500

@app.route('/api/departments')
def departments():
    try:
        conn = get_conn()
        cur  = qexec(conn, "SELECT * FROM departments ORDER BY complaint_count DESC")
        rows = all_dicts(cur); conn.close()
        return jsonify({"departments": rows})
    except Exception as e:
        traceback.print_exc(); return jsonify({"error": str(e)}), 500

@app.route('/api/classify', methods=['POST'])
def classify_only():
    d = request.get_json() or {}
    t = d.get('text', '').strip()
    if not t: return err("text required")
    return jsonify({"success": True, "classification": classify_complaint(t)})


# ── AUTH HELPERS ─────────────────────────────────────────────────────────────
def hash_pw(pw):
    return hashlib.sha256(pw.strip().encode()).hexdigest()

def make_token(key):
    return hashlib.sha256(f"{key}-{secrets.token_hex(16)}".encode()).hexdigest()[:32]

SESSIONS = {}

def get_session(token):
    return SESSIONS.get(token)

def create_session(email, name):
    token = make_token(email)
    SESSIONS[token] = {"email": email, "name": name, "at": datetime.now().isoformat()}
    return token

def save_reset_token(email, token, token_type):
    exp = (datetime.now() + timedelta(hours=1)).isoformat()
    try:
        conn = get_conn()
        qexec(conn, fix_sql("DELETE FROM otp_verifications WHERE mobile=%s AND verified=0"), (f"__reset__{email}",))
        qexec(conn, fix_sql("INSERT INTO otp_verifications(mobile,otp,verified,expires_at) VALUES(%s,%s,0,%s)"),
              (f"__reset__{email}", f"{token_type}:{token}", exp))
        conn.commit(); conn.close()
    except Exception as e:
        print(f"[TOKEN] Save error: {e}")

def get_reset_token(token):
    try:
        conn = get_conn()
        if USE_POSTGRES:
            cur = qexec(conn, "SELECT mobile, otp, expires_at FROM otp_verifications WHERE otp LIKE %s AND verified=0", (f"%:{token}",))
        else:
            cur = qexec(conn, "SELECT mobile, otp, expires_at FROM otp_verifications WHERE otp LIKE ? AND verified=0", (f"%:{token}",))
        fetched = cur.fetchone(); conn.close()
        if fetched is None: return None
        row = dict(fetched) if not isinstance(fetched, dict) else fetched
        try:
            exp_val = row.get("expires_at")
            if exp_val:
                exp = datetime.fromisoformat(str(exp_val))
                if datetime.now() > exp: return None
        except: pass
        otp_val = row.get("otp", "")
        if ":" not in otp_val: return None
        token_type, _ = otp_val.split(":", 1)
        email = row.get("mobile", "").replace("__reset__", "")
        return {"email": email, "type": token_type}
    except Exception as e:
        print(f"[TOKEN] Get error: {e}"); return None

def consume_reset_token(token):
    try:
        conn = get_conn()
        if USE_POSTGRES:
            qexec(conn, "UPDATE otp_verifications SET verified=1 WHERE otp LIKE %s AND verified=0", (f"%:{token}",))
        else:
            qexec(conn, "UPDATE otp_verifications SET verified=1 WHERE otp LIKE ? AND verified=0", (f"%:{token}",))
        conn.commit(); conn.close()
    except Exception as e:
        print(f"[TOKEN] Consume error: {e}")

def send_verification_email(email, name, token):
    verify_url = f"{os.environ.get('APP_URL','http://localhost:8000')}/api/auth/verify-email?token={token}"
    if not USE_EMAIL:
        print(f"\n  [EMAIL TEST] Verification link for {email}:\n  {verify_url}\n")
        return
    try:
        headers = {"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"}
        requests.post("https://api.resend.com/emails", headers=headers, json={
            "from": "GrievAI Portal <noreply@resend.dev>", "to": [email],
            "subject": "✅ GrievAI — Email Verify Karein",
            "html": f"""<div style="font-family:sans-serif;max-width:480px;margin:auto;padding:30px;border-radius:16px;background:#f9f9f9;">
                <h2 style="color:#FF6200;">🏛️ GrievAI Portal</h2>
                <p>Namaste <b>{name}</b>!</p>
                <p>Aapka account verify karne ke liye neeche click karein:</p>
                <a href="{verify_url}" style="display:inline-block;margin:20px 0;padding:14px 28px;background:#FF6200;color:white;border-radius:30px;text-decoration:none;font-weight:bold;">✅ Email Verify Karein</a>
                <p style="color:#999;font-size:12px;">Yeh link 1 ghante valid hai.</p>
            </div>"""
        }, timeout=15)
        print(f"[EMAIL] ✅ Verification email sent → {email}")
    except Exception as e:
        print(f"[EMAIL] ❌ Verification email error: {e}")

def send_reset_email(email, name, token):
    # FIXED: reset URL ab reset-password.html pe jaayega
    reset_url = f"{os.environ.get('APP_URL','http://localhost:8000')}/reset-password.html?token={token}&email={email}"
    if not USE_EMAIL:
        print(f"\n  [EMAIL TEST] Password reset link for {email}:\n  {reset_url}\n")
        return True
    try:
        headers = {"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"}
        requests.post("https://api.resend.com/emails", headers=headers, json={
            "from": "GrievAI Portal <noreply@resend.dev>", "to": [email],
            "subject": "🔑 GrievAI — Password Reset Link",
            "html": f"""<div style="font-family:sans-serif;max-width:480px;margin:auto;padding:30px;border-radius:16px;background:#f9f9f9;">
                <h2 style="color:#FF6200;">🏛️ GrievAI Portal</h2>
                <p>Namaste <b>{name}</b>!</p>
                <p>Password reset karne ke liye neeche click karein:</p>
                <a href="{reset_url}" style="display:inline-block;margin:20px 0;padding:14px 28px;background:#FF6200;color:white;border-radius:30px;text-decoration:none;font-weight:bold;">🔑 Password Reset Karein</a>
                <p style="color:#999;font-size:12px;">Yeh link 1 ghante valid hai.</p>
            </div>"""
        }, timeout=15)
        print(f"[EMAIL] ✅ Reset email sent → {email}")
        return True
    except Exception as e:
        print(f"[EMAIL] ❌ Reset email error: {e}"); return False


# ── AUTH ROUTES ───────────────────────────────────────────────────────────────

@app.route("/api/auth/register", methods=["POST"])
def auth_register():
    d     = request.get_json() or {}
    name  = d.get("name", "").strip()
    email = d.get("email", "").strip().lower()
    pw    = d.get("password", "").strip()

    if not name or not email or not pw:
        return err("Naam, email aur password required hain।")
    if "@" not in email or "." not in email.split("@")[-1] or len(email.split("@")[0]) < 1:
        return err("Sahi email address dalein। (example: name@gmail.com)")
    if len(pw) < 6:
        return err("Password kam se kam 6 characters ka hona chahiye।")

    pw_hash = hash_pw(pw)
    try:
        conn = get_conn()
        cur  = qexec(conn, fix_sql("SELECT id, verified FROM citizens WHERE email=%s"), (email,))
        fetched = cur.fetchone()
        row = dict(fetched) if fetched else None

        if row and row.get("verified") and str(row.get("verified")) not in ("0", "False", "false"):
            conn.close()
            return err("Yeh email already registered aur verified hai। Login karein।")

        if row and (not row.get("verified") or str(row.get("verified")) in ("0", "False", "false")):
            if USE_POSTGRES:
                qexec(conn, "UPDATE citizens SET name=%s, password_hash=%s WHERE email=%s", (name, pw_hash, email))
            else:
                qexec(conn, "UPDATE citizens SET name=?, password_hash=? WHERE email=?", (name, pw_hash, email))
            conn.commit(); conn.close()
            vtok = make_token(email)
            save_reset_token(email, vtok, "verify")
            threading.Thread(target=send_verification_email, args=(email, name, vtok), daemon=True).start()
            return jsonify({
                "success": True,
                "message": "Aapki email pehle registered hai lekin verify nahi hui। Dobara verification link bheja gaya — inbox check karein।",
                "needs_verification": True
            })

        if USE_POSTGRES:
            qexec(conn, "INSERT INTO citizens(email,name,password_hash,verified) VALUES(%s,%s,%s,FALSE)", (email, name, pw_hash))
        else:
            qexec(conn, "INSERT INTO citizens(email,name,password_hash,verified) VALUES(?,?,?,0)", (email, name, pw_hash))
        conn.commit(); conn.close()

        vtok = make_token(email)
        save_reset_token(email, vtok, "verify")
        threading.Thread(target=send_verification_email, args=(email, name, vtok), daemon=True).start()

        print(f"[AUTH] ✅ Registered (pending verify): {email} ({name})")
        return jsonify({
            "success": True,
            "message": "Registration successful! Aapke email pe verification link bheja gaya। Pehle email verify karein, phir login karein।",
            "needs_verification": True
        })
    except Exception as e:
        traceback.print_exc(); return jsonify({"error": str(e)}), 500


@app.route("/api/auth/verify-email", methods=["GET"])
def verify_email():
    token = request.args.get("token", "")
    rec   = get_reset_token(token)
    if not rec or rec.get("type") != "verify":
        return "<h3>❌ Invalid or expired verification link.</h3><p>Dobara register karein ya support se contact karein.</p>", 400
    consume_reset_token(token)
    try:
        conn = get_conn()
        qexec(conn, fix_sql("UPDATE citizens SET verified=1 WHERE email=%s"), (rec["email"],))
        conn.commit(); conn.close()
        print(f"[AUTH] ✅ Email verified: {rec['email']}")
        return """<html><body style='font-family:sans-serif;text-align:center;padding:60px 20px;background:#f5f5f5;'>
            <div style='max-width:400px;margin:auto;background:white;padding:40px;border-radius:16px;box-shadow:0 4px 20px rgba(0,0,0,0.1);'>
            <h2 style='color:#2e7d32;'>✅ Email Verified!</h2>
            <p style='color:#555;'>Aapka account successfully activate ho gaya।</p>
            <a href='/login.html' style='display:inline-block;margin-top:20px;padding:12px 30px;background:#FF6200;color:white;border-radius:8px;text-decoration:none;font-weight:bold;'>Login Karein →</a>
            </div></body></html>"""
    except Exception as e:
        return f"<h3>Error: {e}</h3>", 500


@app.route("/api/auth/login", methods=["POST"])
def auth_login():
    d     = request.get_json() or {}
    email = d.get("email", "").strip().lower()
    pw    = d.get("password", "").strip()

    if not email or not pw:
        return err("Email aur password required hain।")

    try:
        conn = get_conn()
        cur  = qexec(conn, fix_sql("SELECT * FROM citizens WHERE email=%s"), (email,))
        fetched = cur.fetchone(); conn.close()

        if fetched is None:
            return err("Yeh email registered nahi hai। Pehle register karein।")
        row = dict(fetched) if not isinstance(fetched, dict) else fetched

        if not row.get("password_hash"):
            return err("Is account ka password set nahi hai।")
        if row["password_hash"] != hash_pw(pw):
            return err("Password galat hai। Dobara try karein।")

        verified = row.get("verified")
        if not verified or str(verified) in ("0", "False", "false"):
            name = row.get("name", "")
            vtok = make_token(email)
            save_reset_token(email, vtok, "verify")
            threading.Thread(target=send_verification_email, args=(email, name, vtok), daemon=True).start()
            return jsonify({
                "error": "Aapki email abhi verify nahi hui। Inbox mein verification link bheja gaya hai — pehle verify karein phir login karein।",
                "needs_verification": True
            }), 403

        token = create_session(email, row.get("name", ""))
        print(f"[AUTH] ✅ Login: {email}")
        return jsonify({"success": True, "token": token, "user": {"email": email, "name": row.get("name", "")}})
    except Exception as e:
        traceback.print_exc(); return jsonify({"error": str(e)}), 500


@app.route("/api/auth/forgot-password", methods=["POST"])
def auth_forgot():
    d     = request.get_json() or {}
    email = d.get("email", "").strip().lower()
    if not email: return err("Email required hai।")
    try:
        conn = get_conn()
        cur  = qexec(conn, fix_sql("SELECT * FROM citizens WHERE email=%s"), (email,))
        fetched = cur.fetchone(); conn.close()
        if fetched is None: return err("Yeh email registered nahi hai। Pehle register karein।")
        row = dict(fetched) if not isinstance(fetched, dict) else fetched
    except Exception:
        return err("Database error", 500)

    rtok = make_token(email)
    save_reset_token(email, rtok, "reset")
    threading.Thread(target=send_reset_email, args=(email, row.get("name",""), rtok), daemon=True).start()
    # FIXED: Test mode ke liye bhi reset URL sahi hai
    if not USE_EMAIL:
        reset_url = f"{os.environ.get('APP_URL','http://localhost:8000')}/reset-password.html?token={rtok}&email={email}"
        print(f"\n  [TEST] Password reset link:\n  {reset_url}\n")
    return jsonify({"success": True, "message": "Password reset link aapke email pe bheja gaya! Inbox check karein।"})


@app.route("/api/auth/reset-password", methods=["POST"])
def auth_reset():
    d      = request.get_json() or {}
    token  = d.get("token", "").strip()
    new_pw = d.get("new_password", "").strip()
    if not token or not new_pw: return err("Token aur new_password required hain।")
    if len(new_pw) < 6: return err("Password kam se kam 6 characters ka hona chahiye।")
    rec = get_reset_token(token)
    if not rec or rec.get("type") != "reset":
        return err("Reset link invalid ya expire ho gaya। Dobara 'Forgot Password' try karein।")
    consume_reset_token(token)
    pw_hash = hash_pw(new_pw); email = rec["email"]
    try:
        conn = get_conn()
        qexec(conn, fix_sql("UPDATE citizens SET password_hash=%s, verified=1 WHERE email=%s"), (pw_hash, email))
        conn.commit(); conn.close()
        print(f"[AUTH] ✅ Password reset: {email}")
        return jsonify({"success": True, "message": "Password reset ho gaya! Ab login karein।"})
    except Exception as e:
        traceback.print_exc(); return jsonify({"error": str(e)}), 500


@app.route("/api/auth/me", methods=["GET"])
def auth_me():
    auth_header = request.headers.get("Authorization", "")
    token = auth_header[7:] if auth_header.startswith("Bearer ") else request.headers.get("X-Auth-Token", "")
    sess = get_session(token)
    if not sess: return jsonify({"authenticated": False}), 401
    return jsonify({"authenticated": True, "user": sess})


@app.route("/api/auth/logout", methods=["POST"])
def auth_logout():
    auth_header = request.headers.get("Authorization", "")
    token = auth_header[7:] if auth_header.startswith("Bearer ") else request.headers.get("X-Auth-Token", "")
    SESSIONS.pop(token, None)
    return jsonify({"success": True})


# ── STARTUP ───────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    border = "═" * 53
    print(f"\n  ╔{border}╗")
    print(f"  ║{'':^53}║")
    print(f"  ║{'  🏛️  GrievAI Portal — MP Government':^53}║")
    print(f"  ║{'  Civic Complaint Management System v3.2.2':^53}║")
    print(f"  ║{'':^53}║")
    print(f"  ╠{border}╣")
    init_db()
    port = int(os.environ.get('PORT', 8000))
    print(f"  ║{'':^53}║")
    print(f"  ║  🌐 URL      : http://localhost:{port:<20}║")
    print(f"  ║  🗄️  Database : {'PostgreSQL' if USE_POSTGRES else 'SQLite (local)':<24}║")
    print(f"  ║  📱 OTP Mode : {'Twilio SMS' if USE_TWILIO else 'Terminal Print (TEST)':<24}║")
    print(f"  ║  📧 Email    : {'Resend Active' if USE_EMAIL else 'Test Mode':<24}║")
    print(f"  ║  🤖 Claude   : {'Photo AI Active' if USE_CLAUDE else 'Disabled':<24}║")
    print(f"  ║{'':^53}║")
    if not USE_TWILIO:
        print(f"  ║  ⚠️  OTP TEST MODE: Codes print here!{'':15}║")
        print(f"  ║     Terminal khula rakho OTP dekhne ke liye{'':8}║")
        print(f"  ║{'':^53}║")
    print(f"  ╠{border}╣")
    print(f"  ║  Press Ctrl+C to stop{'':31}║")
    print(f"  ╚{border}╝\n")
    app.run(host='0.0.0.0', port=port, debug=False)
