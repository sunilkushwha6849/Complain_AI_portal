@app.route('/api/auth/reset-password', methods=['POST'])
def reset_password():
    try:
        data = request.get_json() or {}
        email = data.get('email', '').strip().lower()
        token = data.get('token', '').strip()
        new_password = data.get('new_password', '').strip()

        if not email or not token or not new_password:
            return jsonify({'success': False, 'error': 'सभी फील्ड भरें'}), 400
        if len(new_password) < 6:
            return jsonify({'success': False, 'error': 'Password कम से कम 6 अक्षर'}), 400

        conn = get_conn()
        cur = qexec(conn, "SELECT * FROM otp_verifications WHERE mobile = %s AND otp = %s", (email, token))
        row = to_dict(cur, cur.fetchone())

        if not row:
            conn.close()
            return jsonify({'success': False, 'error': 'Invalid या Expired link'}), 400

        expires_str = row['expires_at']
        if isinstance(expires_str, str):
            expires = datetime.strptime(expires_str[:19], '%Y-%m-%d %H:%M:%S')
        else:
            expires = expires_str

        if datetime.now() > expires:
            conn.close()
            return jsonify({'success': False, 'error': 'Link expire हो गया है'}), 400

        pw_hash = hash_password(new_password)
        qexec(conn, "UPDATE citizens SET password_hash = %s WHERE email = %s", (pw_hash, email))
        qexec(conn, "DELETE FROM otp_verifications WHERE mobile = %s", (email,))
        conn.commit()
        conn.close()

        return jsonify({'success': True, 'message': 'पासवर्ड सफलतापूर्वक बदल गया! अब लॉगिन करें।'})
    except Exception as e:
        print(f"[RESET PASSWORD ERROR] {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500
