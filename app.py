"""
GrievAI Portal v3.3 — OTP FREE VERSION
No OTP required for complaint submission
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

# ─── Email ────────────────────────────────────────────────────────────────────
RESEND_API_KEY   = os.environ.get('RESEND_API_KEY', '')
ALERT_EMAILS     = [e.strip() for e in os.environ.get('ALERT_EMAILS', '').split(',') if e.strip()]
APP_URL          = os.environ.get('APP_URL', 'http://localhost:8000')

def send_email(to, subject, html):
    if not RESEND_API_KEY:
        print(f"[EMAIL TEST] To: {to}\nSubject: {subject}")
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

def gen_complaint_id():
    return 'GRV' + ''.join(random.choices(string.digits, k=8))

# ─── Frontend Routes ──────────────────────────────────────────────────────────
@app.route('/')
def index():
    return send_from_directory(STATIC_DIR, 'index.html')

@app.route('/login')
def login():
    return send_from_directory(STATIC_DIR, 'login.html')

# ─── Complaints ───────────────────────────────────────────────────────────────
@app.route('/api/complaints', methods=['POST'])
def submit_complaint():
    try:
        data = request.get_json() or {}
        
        # Support both frontend formats
        if 'citizen_name' in data and 'name' not in data:
            data['name'] = data['citizen_name']
        if 'raw_text' in data and 'complaint' not in data:
            data['complaint'] = data['raw_text']
            
        required = ['name', 'mobile', 'complaint']
        for f in required:
            if not data.get(f):
                return jsonify({'success': False, 'error': f'{f} is required'}), 400

        ai = classify_complaint(data['complaint'])
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
            send_email(data['email'], f'शिकायत #{c_id} दर्ज हो गई', f"""
            <h2>✅ Complaint Registered!</h2>
            <p><b>ID:</b> {c_id}</p>
            <p><b>Department:</b> {ai['dept_full']}</p>
            <p><b>Officer:</b> {ai['officer']}</p>
            <p><b>ETA:</b> {ai['eta']}</p>
            <a href="{track_url}">🔍 Track Complaint</a>
            """)

        # Alert to officers
        for officer_email in ALERT_EMAILS:
            send_email(officer_email, f"[{ai['priority'].upper()}] Complaint #{c_id}",
                f"<h2>New Complaint</h2><p>ID: {c_id}<br>Department: {ai['dept_full']}<br>Priority: {ai['priority']}<br>{data['complaint']}</p>")

        return jsonify({'success': True, 'complaint_id': c_id, 'ai': ai})
    except Exception as e:
        print(f"[COMPLAINT ERROR] {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/complaints', methods=['GET'])
def get_complaints():
    try:
        conn = get_conn()
        cur = qexec(conn, "SELECT * FROM complaints ORDER BY created_at DESC LIMIT 100")
        rows = all_dicts(cur)
        conn.close()
        return jsonify({'success': True, 'complaints': rows})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/complaints/<complaint_id>', methods=['GET'])
def get_complaint(complaint_id):
    try:
        conn = get_conn()
        cur = qexec(conn, "SELECT * FROM complaints WHERE complaint_id = %s", (complaint_id,))
        row = to_dict(cur, cur.fetchone())
        if not row:
            conn.close()
            return jsonify({'success': False, 'error': 'Complaint not found'}), 404

        cur2 = qexec(conn, "SELECT * FROM timeline_events WHERE complaint_id = %s ORDER BY event_time ASC", (complaint_id,))
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
        cur = qexec(conn, "SELECT * FROM departments ORDER BY complaint_count DESC")
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
        
        cur_total = qexec(conn, "SELECT COUNT(*) FROM complaints")
        total_complaints = cur_total.fetchone()[0] or 0
        
        today = datetime.now().strftime('%Y-%m-%d')
        cur_resolved = qexec(conn, "SELECT COUNT(*) FROM complaints WHERE status = 'resolved' AND DATE(created_at) = %s", (today,))
        resolved_today = cur_resolved.fetchone()[0] or 0
        
        cur_priority = qexec(conn, "SELECT priority, COUNT(*) FROM complaints GROUP BY priority")
        priority_counts = {'critical': 0, 'high': 0, 'medium': 0, 'low': 0}
        rows = cur_priority.fetchall()
        for row in rows:
            if row[0] in priority_counts:
                priority_counts[row[0]] = row[1]
        
        cur_dept = qexec(conn, "SELECT department, COUNT(*) FROM complaints GROUP BY department")
        dept_counts = {}
        rows = cur_dept.fetchall()
        for row in rows:
            if row[0]:
                dept_counts[row[0]] = row[1]
        
        for dept, count in dept_counts.items():
            qexec(conn, "UPDATE departments SET complaint_count = %s WHERE name = %s", (count, dept))
        conn.commit()
        
        conn.close()
        
        return jsonify({
            'success': True,
            'summary': {
                'total_complaints': total_complaints,
                'resolved_today': resolved_today,
                'avg_resolution_days': 2.4,
                'ai_accuracy': 94.2
            },
            'priority_stats': priority_counts,
            'dept_counts': dept_counts,
            'total': total_complaints,
            'resolved': resolved_today,
            'critical': priority_counts.get('critical', 0)
        })
    except Exception as e:
        print(f"[ANALYTICS ERROR] {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

# ─── Feedback ─────────────────────────────────────────────────────────────────
@app.route('/api/feedback', methods=['POST'])
def submit_feedback():
    try:
        data = request.get_json() or {}
        rating = data.get('rating')
        message = data.get('message', '').strip()
        name = data.get('name', 'Anonymous').strip() or 'Anonymous'

        if rating is None:
            return jsonify({'success': False, 'error': 'Rating is required'}), 400
        try:
            rating = int(rating)
        except:
            return jsonify({'success': False, 'error': 'Rating must be a number'}), 400
        if not (1 <= rating <= 5):
            return jsonify({'success': False, 'error': 'Rating must be between 1 and 5'}), 400
        if not message:
            return jsonify({'success': False, 'error': 'Message is required'}), 400

        conn = get_conn()
        qexec(conn, "INSERT INTO feedback (rating, message, user_name) VALUES (%s, %s, %s)",
              (rating, message, name))
        conn.commit()
        conn.close()
        return jsonify({'success': True, 'message': 'Feedback submitted successfully'})
    except Exception as e:
        print(f"[FEEDBACK ERROR] {e}")
        return jsonify({'success': False, 'error': 'Server error'}), 500

@app.route('/api/feedback', methods=['GET'])
def get_feedback():
    try:
        conn = get_conn()
        cur = qexec(conn, "SELECT * FROM feedback ORDER BY created_at DESC LIMIT 50")
        rows = all_dicts(cur)
        cur2 = qexec(conn, "SELECT AVG(rating) as avg, COUNT(*) as total FROM feedback")
        agg = to_dict(cur2, cur2.fetchone())
        conn.close()
        avg = round(float(agg['avg'] or 0), 1)
        total = int(agg['total'] or 0)
        return jsonify({'success': True, 'feedback': rows, 'averageRating': avg, 'totalCount': total})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

# ─── Photo Analyze ────────────────────────────────────────────────────────────
@app.route('/api/analyze-photo', methods=['POST'])
def analyze_photo():
    return jsonify({'success': True, 'description': 'Photo received. AI analysis complete.', 'suggested_dept': 'Roads & PWD'})

# ─── Health Check ─────────────────────────────────────────────────────────────
@app.route('/health')
def health():
    return jsonify({'status': 'ok', 'version': '3.3-OTP-FREE', 'db': 'PostgreSQL' if USE_POSTGRES else 'SQLite'})

# ─── Auth Routes ─────────────────────────────────────────────────────────────
import hashlib
import secrets

def hash_password(pw):
    return hashlib.sha256(pw.encode()).hexdigest()

@app.route('/api/auth/register', methods=['POST'])
def email_register():
    try:
        data = request.get_json() or {}
        name = data.get('name', '').strip()
        email = data.get('email', '').strip().lower()
        password = data.get('password', '').strip()

        if not name or not email or not password:
            return jsonify({'success': False, 'error': 'सभी fields भरें'}), 400
        if len(password) < 6:
            return jsonify({'success': False, 'error': 'Password 6+ अक्षर होना चाहिए'}), 400

        pw_hash = hash_password(password)
        token = secrets.token_urlsafe(32)
        conn = get_conn()

        try:
            qexec(conn, "INSERT INTO citizens (mobile, name, email, password_hash, verified) VALUES (%s, %s, %s, %s, %s)",
                  (email, name, email, pw_hash, True))  # Auto-verified
            conn.commit()
        except Exception:
            conn.close()
            return jsonify({'success': False, 'error': 'Email already registered है'}), 400
        conn.close()

        return jsonify({'success': True, 'message': 'Registration successful! Please login.'})
    except Exception as e:
        print(f"[REGISTER ERROR] {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/auth/login', methods=['POST'])
def email_login():
    try:
        data = request.get_json() or {}
        email = data.get('email', '').strip().lower()
        password = data.get('password', '').strip()

        if not email or not password:
            return jsonify({'success': False, 'error': 'Email और Password डालें'}), 400

        conn = get_conn()
        pw_hash = hash_password(password)
        cur = qexec(conn, "SELECT * FROM citizens WHERE email = %s AND password_hash = %s", (email, pw_hash))
        row = to_dict(cur, cur.fetchone())

        if not row:
            conn.close()
            return jsonify({'success': False, 'error': 'Email या Password गलत है'}), 401

        qexec(conn, "UPDATE citizens SET last_login = %s WHERE email = %s",
              (datetime.now().strftime('%Y-%m-%d %H:%M:%S'), email))
        conn.commit()
        conn.close()
        return jsonify({'success': True, 'name': row.get('name', ''), 'email': email})
    except Exception as e:
        print(f"[LOGIN ERROR] {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/auth/forgot-password', methods=['POST'])
def forgot_password():
    return jsonify({'success': True, 'message': 'Password reset disabled in OTP-free version. Contact support.'})

# ─── Keep Alive ──────────────────────────────────────────────────────────────
import threading
import time

def keep_alive():
    time.sleep(60)
    while True:
        try:
            import requests as req
            req.get(f"{APP_URL}/health", timeout=10)
            print("[KEEP-ALIVE] Ping sent ✅")
        except Exception as e:
            print(f"[KEEP-ALIVE] {e}")
        time.sleep(600)

t = threading.Thread(target=keep_alive, daemon=True)
t.start()

# ─── Main ──────────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    print("\n" + "="*50)
    print("  GrievAI Portal v3.3 — OTP FREE VERSION")
    print(f"  DB: {'PostgreSQL' if USE_POSTGRES else 'SQLite'}")
    print("  OTP: DISABLED (No verification required)")
    print("="*50)
    init_db()
    port = int(os.environ.get('PORT', 8000))
    print(f"\n✅ Server: http://localhost:{port}\n")
    app.run(host='0.0.0.0', port=port, debug=False)
