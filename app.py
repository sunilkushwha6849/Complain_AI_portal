"""
GrievAI Portal v3.3 — Main Flask Server
Supports SQLite (local) + PostgreSQL (Railway)
"""
import os
import uuid
import random
import string
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from dotenv import load_dotenv

load_dotenv()

from database import get_conn, qexec, qmany, to_dict, all_dicts, init_db, USE_POSTGRES
from ai_engine import classify_complaint, calculate_stats

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, 'static')
app = Flask(__name__, static_folder=STATIC_DIR, static_url_path='')
CORS(app)

SECRET_KEY = os.environ.get('SECRET_KEY', 'grievai-secret-2024')
app.secret_key = SECRET_KEY

# ─── Resend Email ────────────────────────────────────────────────────────────
RESEND_API_KEY   = os.environ.get('RESEND_API_KEY', '')
ALERT_EMAILS     = [e.strip() for e in os.environ.get('ALERT_EMAILS', '').split(',') if e.strip()]
APP_URL          = os.environ.get('APP_URL', 'http://localhost:8000')

def send_email(to, subject, html):
    if not RESEND_API_KEY:
        print(f"[EMAIL TEST] To: {to}\nSubject: {subject}\n{html[:200]}...")
        return True
    try:
        import requests as req
        r = req.post('https://api.resend.com/emails',
            headers={'Authorization': f'Bearer {RESEND_API_KEY}', 'Content-Type': 'application/json'},
            json={'from': 'GrievAI Portal <onboarding@resend.dev>', 'to': [to], 'subject': subject, 'html': html},
            timeout=10)
        return r.status_code == 200
    except Exception as e:
        print(f"[EMAIL ERROR] {e}")
        return False

# ─── Twilio SMS ───────────────────────────────────────────────────────────────
TWILIO_SID   = os.environ.get('TWILIO_ACCOUNT_SID', '')
TWILIO_TOKEN = os.environ.get('TWILIO_AUTH_TOKEN', '')
TWILIO_PHONE = os.environ.get('TWILIO_PHONE_NUMBER', '')

def send_sms(to, body):
    API_KEY = os.environ.get('FAST2SMS_API_KEY', '')

    # 2Factor.in use karo agar key hai
    if API_KEY:
        try:
            import requests as req
            otp_code = ''.join(filter(str.isdigit, body))[:6]
            url = f"https://2factor.in/API/V1/{API_KEY}/SMS/{to}/{otp_code}/OTP1"
            r = req.get(url, timeout=10)
            res = r.json()
            print(f"[2FACTOR] {res}")
            return res.get('Status') == 'Success'
        except Exception as e:
            print(f"[2FACTOR ERROR] {e}")
            return False

    # Test mode
    print(f"[OTP TEST] To: {to} | {body}")
    return True

def gen_otp():
    return ''.join(random.choices(string.digits, k=6))

def gen_complaint_id():
    return 'GRV' + ''.join(random.choices(string.digits, k=8))

# ─── Frontend Routes ──────────────────────────────────────────────────────────
@app.route('/')
def index():
    return send_from_directory(STATIC_DIR, 'index.html')

@app.route('/login')
def login():
    return send_from_directory(STATIC_DIR, 'login.html')

# ─── OTP Routes ───────────────────────────────────────────────────────────────
@app.route("/api/otp/send", methods=["POST"])
@app.route("/api/send-otp", methods=["POST"])
def send_otp():
    try:
        data   = request.get_json() or {}
        mobile = data.get('mobile', '').strip()
        if not mobile or len(mobile) < 10:
            return jsonify({'success': False, 'error': 'Valid mobile number required'}), 400

        otp     = gen_otp()
        expires = (datetime.now() + timedelta(minutes=10)).strftime('%Y-%m-%d %H:%M:%S')
        conn    = get_conn()

        qexec(conn, "DELETE FROM otp_verifications WHERE mobile = %s", (mobile,))
        qexec(conn, "INSERT INTO otp_verifications (mobile, otp, expires_at) VALUES (%s, %s, %s)",
              (mobile, otp, expires))
        conn.commit()
        conn.close()

        send_sms(f'+91{mobile}', f'GrievAI OTP: {otp}. Valid 10 min. Do not share.')
        print(f"[OTP] {mobile} → {otp}")
        return jsonify({'success': True, 'message': 'OTP sent'})
    except Exception as e:
        print(f"[OTP ERROR] {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route("/api/otp/verify", methods=["POST"])
@app.route("/api/verify-otp", methods=["POST"])
def verify_otp():
    try:
        data   = request.get_json() or {}
        mobile = data.get('mobile', '').strip()
        otp    = data.get('otp', '').strip()

        conn = get_conn()
        cur  = qexec(conn, "SELECT * FROM otp_verifications WHERE mobile = %s AND otp = %s", (mobile, otp))
        row  = to_dict(cur, cur.fetchone())

        if not row:
            conn.close()
            return jsonify({'success': False, 'error': 'Invalid OTP'}), 400

        expires = datetime.strptime(str(row['expires_at'])[:19], '%Y-%m-%d %H:%M:%S')
        if datetime.now() > expires:
            conn.close()
            return jsonify({'success': False, 'error': 'OTP expired'}), 400

        qexec(conn, "UPDATE otp_verifications SET verified = %s WHERE mobile = %s AND otp = %s",
              (1 if not USE_POSTGRES else True, mobile, otp))
        qexec(conn, """INSERT INTO citizens (mobile, verified) VALUES (%s, %s)
                       ON CONFLICT (mobile) DO UPDATE SET verified = %s, last_login = %s""",
              (mobile, 1 if not USE_POSTGRES else True,
               1 if not USE_POSTGRES else True, datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
        conn.commit()
        conn.close()
        return jsonify({'success': True, 'message': 'OTP verified'})
    except Exception as e:
        print(f"[VERIFY OTP ERROR] {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

# ─── Complaints ───────────────────────────────────────────────────────────────
@app.route('/api/complaints', methods=['POST'])
def submit_complaint():
    try:
        data = request.get_json() or {}
        # Frontend citizen_name/raw_text bhi support karo
        if 'citizen_name' in data and 'name' not in data:
            data['name'] = data['citizen_name']
        if 'raw_text' in data and 'complaint' not in data:
            data['complaint'] = data['raw_text']
        required = ['name', 'mobile', 'complaint']
        for f in required:
            if not data.get(f):
                return jsonify({'success': False, 'error': f'{f} is required'}), 400

        ai   = classify_complaint(data['complaint'])
        c_id = gen_complaint_id()
        conn = get_conn()

        qexec(conn, """
            INSERT INTO complaints
            (complaint_id, citizen_name, mobile, district, area, raw_text,
             department, category, priority, ai_confidence, ai_summary,
             eta_days, officer_name, dept_full, latitude, longitude,
             location_accuracy, input_mode, language)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """, (c_id, data['name'], data['mobile'],
              data.get('district', ''), data.get('area', ''),
              data['complaint'],
              ai['department'], ai['category'], ai['priority'],
              ai['confidence'], ai['summary'],
              ai['eta'], ai['officer'], ai['dept_full'],
              data.get('latitude'), data.get('longitude'),
              data.get('location_accuracy'), data.get('input_mode', 'text'),
              ai['language']))

        qexec(conn, """
            INSERT INTO timeline_events (complaint_id, event_title, event_desc, status)
            VALUES (%s, %s, %s, %s)
        """, (c_id, 'शिकायत दर्ज', f'AI द्वारा {ai["department"]} विभाग को भेजा गया', 'done'))

        qexec(conn, """
            UPDATE departments SET complaint_count = complaint_count + 1
            WHERE name = %s
        """, (ai['department'],))

        conn.commit()
        conn.close()

        # Email to user
        if data.get('email'):
            track_url = f"{APP_URL}/?track={c_id}"
            send_email(data['email'], f'शिकायत #{c_id} दर्ज हो गई — GrievAI', f"""
            <h2>आपकी शिकायत सफलतापूर्वक दर्ज हो गई ✅</h2>
            <p><b>Complaint ID:</b> {c_id}</p>
            <p><b>विभाग:</b> {ai['dept_full']}</p>
            <p><b>अधिकारी:</b> {ai['officer']}</p>
            <p><b>ETA:</b> {ai['eta']}</p>
            <p><b>शिकायत:</b> {data['complaint']}</p>
            <a href="{track_url}" style="display:inline-block;padding:12px 24px;background:#4CAF50;color:#fff;border-radius:8px;text-decoration:none;">🔍 Track Complaint</a>
            """)

        # Alert to officers
        priority_emoji = {'critical': '🚨', 'high': '⚠️', 'medium': '📋', 'low': 'ℹ️'}
        for officer_email in ALERT_EMAILS:
            send_email(officer_email,
                f"{priority_emoji.get(ai['priority'], '📋')} [{ai['priority'].upper()}] नई शिकायत #{c_id}",
                f"""
                <h2>{priority_emoji.get(ai['priority'], '📋')} नई शिकायत — {ai['priority'].upper()}</h2>
                <p><b>ID:</b> {c_id}</p>
                <p><b>नागरिक:</b> {data['name']} | {data['mobile']}</p>
                <p><b>विभाग:</b> {ai['dept_full']}</p>
                <p><b>Priority:</b> {ai['priority'].upper()}</p>
                <p><b>AI Confidence:</b> {ai['confidence']}%</p>
                <p><b>शिकायत:</b> {data['complaint']}</p>
                """)

        return jsonify({'success': True, 'complaint_id': c_id, 'ai': ai})
    except Exception as e:
        print(f"[COMPLAINT ERROR] {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/complaints', methods=['GET'])
def get_complaints():
    try:
        conn = get_conn()
        cur  = qexec(conn, "SELECT * FROM complaints ORDER BY created_at DESC LIMIT 100")
        rows = all_dicts(cur)
        conn.close()
        return jsonify({'success': True, 'complaints': rows})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/complaints/<complaint_id>', methods=['GET'])
def get_complaint(complaint_id):
    try:
        conn = get_conn()
        cur  = qexec(conn, "SELECT * FROM complaints WHERE complaint_id = %s", (complaint_id,))
        row  = to_dict(cur, cur.fetchone())
        if not row:
            conn.close()
            return jsonify({'success': False, 'error': 'Complaint not found'}), 404

        cur2   = qexec(conn, "SELECT * FROM timeline_events WHERE complaint_id = %s ORDER BY event_time ASC", (complaint_id,))
        events = all_dicts(cur2)
        conn.close()
        return jsonify({'success': True, 'complaint': row, 'timeline': events})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

# ─── Departments ──────────────────────────────────────────────────────────────
@app.route('/api/departments', methods=['GET'])
def get_departments():
    try:
        conn = get_conn()
        cur  = qexec(conn, "SELECT * FROM departments ORDER BY complaint_count DESC")
        rows = all_dicts(cur)
        conn.close()
        return jsonify({'success': True, 'departments': rows})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

# ─── Analytics ────────────────────────────────────────────────────────────────
@app.route('/api/analytics', methods=['GET'])
def get_analytics():
    try:
        conn = get_conn()
        cur  = qexec(conn, "SELECT * FROM complaints")
        rows = all_dicts(cur)
        conn.close()
        stats = calculate_stats(rows)
        return jsonify({'success': True, **stats})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

# ─── FEEDBACK — FIXED ─────────────────────────────────────────────────────────
@app.route('/api/feedback', methods=['POST'])
def submit_feedback():
    try:
        data    = request.get_json() or {}
        rating  = data.get('rating')
        message = data.get('message', '').strip()
        name    = data.get('name', 'Anonymous').strip() or 'Anonymous'

        # Validation
        if rating is None:
            return jsonify({'success': False, 'error': 'Rating is required'}), 400
        try:
            rating = int(rating)
        except (ValueError, TypeError):
            return jsonify({'success': False, 'error': 'Rating must be a number'}), 400
        if not (1 <= rating <= 5):
            return jsonify({'success': False, 'error': 'Rating must be between 1 and 5'}), 400
        if not message:
            return jsonify({'success': False, 'error': 'Message is required'}), 400

        conn = get_conn()
        qexec(conn,
              "INSERT INTO feedback (rating, message, user_name) VALUES (%s, %s, %s)",
              (rating, message, name))
        conn.commit()
        conn.close()

        return jsonify({'success': True, 'message': 'Feedback submitted successfully'})
    except Exception as e:
        print(f"[FEEDBACK ERROR] {e}")
        return jsonify({'success': False, 'error': 'Server error. Please try again.'}), 500

@app.route('/api/feedback', methods=['GET'])
def get_feedback():
    try:
        conn = get_conn()
        cur  = qexec(conn, "SELECT * FROM feedback ORDER BY created_at DESC LIMIT 50")
        rows = all_dicts(cur)

        cur2 = qexec(conn, "SELECT AVG(rating) as avg, COUNT(*) as total FROM feedback")
        agg  = to_dict(cur2, cur2.fetchone())
        conn.close()

        avg   = round(float(agg['avg'] or 0), 1)
        total = int(agg['total'] or 0)

        return jsonify({
            'success': True,
            'feedback': rows,
            'averageRating': avg,
            'totalCount': total
        })
    except Exception as e:
        print(f"[FEEDBACK GET ERROR] {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

# ─── Photo Analyze (optional) ─────────────────────────────────────────────────
@app.route('/api/analyze-photo', methods=['POST'])
def analyze_photo():
    # Placeholder — returns mock AI result
    return jsonify({
        'success': True,
        'description': 'Photo received. Manual review required.',
        'keywords': ['infrastructure', 'damage'],
        'suggested_dept': 'Roads & PWD'
    })

# ─── Health Check ─────────────────────────────────────────────────────────────
@app.route('/health')
def health():
    return jsonify({'status': 'ok', 'version': '3.3',
                    'db': 'PostgreSQL' if USE_POSTGRES else 'SQLite'})

# ─── Main ──────────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    print("\n" + "="*50)
    print("  GrievAI Portal v3.3")
    print(f"  DB: {'PostgreSQL' if USE_POSTGRES else 'SQLite'}")
    print("="*50)
    init_db()
    port = int(os.environ.get('PORT', 8000))
    print(f"\n✅ Server: http://localhost:{port}\n")
    app.run(host='0.0.0.0', port=port, debug=False)

# ─── Auth Routes (login.html ke liye) ────────────────────────────────────────
import hashlib

def hash_password(pw):
    return hashlib.sha256(pw.encode()).hexdigest()

@app.route('/api/auth/register', methods=['POST'])
def auth_register():
    try:
        data     = request.get_json() or {}
        mobile   = data.get('mobile', '').strip()
        name     = data.get('name', '').strip()
        password = data.get('password', '').strip()
        email    = data.get('email', '').strip()
        otp      = data.get('otp', '').strip()

        if not mobile or not password:
            return jsonify({'success': False, 'error': 'Mobile aur password zaroori hai'}), 400

        # OTP verify karo
        conn = get_conn()
        cur  = qexec(conn, "SELECT * FROM otp_verifications WHERE mobile = %s AND otp = %s", (mobile, otp))
        row  = to_dict(cur, cur.fetchone())
        if not row:
            conn.close()
            return jsonify({'success': False, 'error': 'OTP galat hai'}), 400

        pw_hash = hash_password(password)
        try:
            qexec(conn, """INSERT INTO citizens (mobile, name, email, password_hash, verified)
                           VALUES (%s, %s, %s, %s, %s)
                           ON CONFLICT (mobile) DO UPDATE SET
                           name=%s, email=%s, password_hash=%s, verified=%s""",
                  (mobile, name, email, pw_hash, True if USE_POSTGRES else 1,
                   name, email, pw_hash, True if USE_POSTGRES else 1))
            conn.commit()
        except Exception as e:
            conn.close()
            return jsonify({'success': False, 'error': 'Mobile already registered hai'}), 400

        conn.close()
        return jsonify({'success': True, 'message': 'Registration successful', 'name': name, 'mobile': mobile})
    except Exception as e:
        print(f"[REGISTER ERROR] {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/auth/login', methods=['POST'])
def auth_login():
    try:
        data     = request.get_json() or {}
        mobile   = data.get('mobile', '').strip()
        password = data.get('password', '').strip()

        if not mobile or not password:
            return jsonify({'success': False, 'error': 'Mobile aur password daalo'}), 400

        conn    = get_conn()
        pw_hash = hash_password(password)
        cur     = qexec(conn, "SELECT * FROM citizens WHERE mobile = %s AND password_hash = %s", (mobile, pw_hash))
        row     = to_dict(cur, cur.fetchone())

        if not row:
            conn.close()
            return jsonify({'success': False, 'error': 'Mobile ya password galat hai'}), 401

        qexec(conn, "UPDATE citizens SET last_login = %s WHERE mobile = %s",
              (datetime.now().strftime('%Y-%m-%d %H:%M:%S'), mobile))
        conn.commit()
        conn.close()
        return jsonify({'success': True, 'message': 'Login successful',
                        'name': row.get('name', ''), 'mobile': mobile})
    except Exception as e:
        print(f"[LOGIN ERROR] {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/auth/reset-password', methods=['POST'])
def reset_password():
    try:
        data        = request.get_json() or {}
        mobile      = data.get('mobile', '').strip()
        otp         = data.get('otp', '').strip()
        new_password = data.get('new_password', '').strip()

        if not mobile or not otp or not new_password:
            return jsonify({'success': False, 'error': 'Saari details daalo'}), 400

        conn = get_conn()
        cur  = qexec(conn, "SELECT * FROM otp_verifications WHERE mobile = %s AND otp = %s", (mobile, otp))
        row  = to_dict(cur, cur.fetchone())
        if not row:
            conn.close()
            return jsonify({'success': False, 'error': 'OTP galat hai'}), 400

        pw_hash = hash_password(new_password)
        qexec(conn, "UPDATE citizens SET password_hash = %s WHERE mobile = %s", (pw_hash, mobile))
        conn.commit()
        conn.close()
        return jsonify({'success': True, 'message': 'Password reset ho gaya'})
    except Exception as e:
        print(f"[RESET ERROR] {e}")
        return jsonify({'success': False, 'error': str(e)}), 500
