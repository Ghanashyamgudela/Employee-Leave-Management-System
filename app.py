from flask import Flask, render_template, request, redirect, session, flash, send_file, jsonify
from flask_mysqldb import MySQL
import MySQLdb.cursors
from openpyxl import Workbook
from io import BytesIO
import smtplib
from email.mime.text import MIMEText
from datetime import datetime, timedelta
import secrets
import os

# optional PDF support
try:
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas
    REPORTLAB_AVAILABLE = True
except Exception:
    REPORTLAB_AVAILABLE = False

app = Flask(__name__)
app.secret_key = "xyz123"

# Load config from config.py (you already use this)
app.config.from_pyfile('config.py')
mysql = MySQL(app)

# ---------------- Helpers ----------------
def send_email(to_email, subject, body):
    """Send an email using SMTP settings from config.py"""
    try:
        msg = MIMEText(body)
        msg['Subject'] = subject
        msg['From'] = app.config.get('MAIL_USER')
        msg['To'] = to_email

        server = smtplib.SMTP(app.config.get('MAIL_SERVER', 'smtp.gmail.com'), int(app.config.get('MAIL_PORT', 587)))
        if app.config.get('MAIL_USE_TLS', True):
            server.starttls()
        server.login(app.config.get('MAIL_USER'), app.config.get('MAIL_PASS'))
        server.sendmail(msg['From'], [to_email], msg.as_string())
        server.quit()
        return True, None
    except Exception as e:
        return False, str(e)

def days_between_dates(start_date_str, end_date_str, half_day=False):
    fmt = "%Y-%m-%d"
    s = datetime.strptime(start_date_str, fmt)
    e = datetime.strptime(end_date_str, fmt)
    days = (e - s).days + 1
    if half_day:
        return 0.5
    return float(days)

def get_student_by_id(student_id):
    cur = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    cur.execute("SELECT * FROM student WHERE student_id=%s", (student_id,))
    return cur.fetchone()

def update_student_balance(student_id, column, new_value):
    cur = mysql.connection.cursor()
    cur.execute(f"UPDATE student SET {column}=%s WHERE student_id=%s", (new_value, student_id))
    mysql.connection.commit()

def restore_balance_for_leave(student_id, leave_type, days):
    # fetch student
    cur = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    cur.execute("SELECT * FROM student WHERE student_id=%s", (student_id,))
    student = cur.fetchone()
    if not student:
        return False
    if leave_type == "Paid Leave":
        new = (student.get('paid_leaves') or 0) + days
        update_student_balance(student_id, 'paid_leaves', new)
    elif leave_type == "Emergency Leave":
        new = (student.get('emergency_leaves') or 0) + days
        update_student_balance(student_id, 'emergency_leaves', new)
    elif leave_type == "Extra Leave":
        new = (student.get('extra_leaves') or 0) + days
        update_student_balance(student_id, 'extra_leaves', new)
    return True

def deduct_balance_for_leave(student_id, leave_type, days):
    cur = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    cur.execute("SELECT * FROM student WHERE student_id=%s", (student_id,))
    student = cur.fetchone()
    if not student:
        return False, "Student not found"

    paid = float(student.get('paid_leaves') or 0)
    emergency = float(student.get('emergency_leaves') or 0)
    extra = float(student.get('extra_leaves') or 0)

    if leave_type == "Paid Leave":
        if paid < days:
            return False, "Not enough Paid Leaves"
        update_student_balance(student_id, 'paid_leaves', paid - days)
        return True, None
    elif leave_type == "Emergency Leave":
        if emergency < days:
            return False, "Not enough Emergency Leaves"
        update_student_balance(student_id, 'emergency_leaves', emergency - days)
        return True, None
    elif leave_type == "Extra Leave":
        if extra < days:
            return False, "Not enough Extra Leaves"
        update_student_balance(student_id, 'extra_leaves', extra - days)
        return True, None
    return False, "Invalid leave type"

# ---------------- ROUTES (existing + new) ----------------

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        name = request.form['name']
        email = request.form['email']
        password = request.form['password']
        department = request.form.get('department')

        cur = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
        cur.execute("SELECT * FROM student WHERE email=%s", (email,))
        account = cur.fetchone()

        if account:
            flash("Email already registered! Please login.")
            return redirect('/register')

        token = secrets.token_urlsafe(32)

        # Add default balances if columns exist; otherwise DB will use defaults
        cur.execute("""
            INSERT INTO student (full_name, email, password, is_verified, verification_token, department)
            VALUES (%s, %s, %s, %s, %s, %s)
        """, (name, email, password, 0, token, department))
        mysql.connection.commit()

        verify_url = request.url_root.rstrip('/') + '/verify/' + token
        subject = "Verify Your Account"
        body = f"Hello {name},\n\nClick the link below to verify your account:\n{verify_url}\n\nThank you!"

        ok, err = send_email(email, subject, body)
        if ok:
            flash("Registration successful! Please check your email for verification link.")
        else:
            flash(f"Registration saved but failed to send verification email: {err}")

        return redirect('/admin/dashboard')

    return render_template('register.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form['email']
        password = request.form['password']

        cur = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
        cur.execute("SELECT * FROM student WHERE email=%s AND password=%s", (email, password))
        user = cur.fetchone()

        if user:
            if not user['is_verified']:
                flash("Your account is not verified. Please check your email and verify your account before logging in.")
                return redirect('/login')
            session['student_id'] = user['student_id']
            session['student_name'] = user['full_name']
            return redirect('/student/dashboard')
        else:
            flash("Invalid email or password")
            return redirect('/login')

    return render_template('login.html')


# ---------------- OTP LOGIN ----------------
@app.route('/otp-login', methods=['GET', 'POST'])
def otp_login():
    if request.method == 'POST':
        # button "send_otp" sends OTP; button "verify_otp" verifies
        email = request.form.get('email')
        cur = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
        cur.execute("SELECT * FROM student WHERE email=%s", (email,))
        user = cur.fetchone()
        if 'send_otp' in request.form:
            if not user:
                flash("No user found with that email", "danger")
                return redirect('/otp-login')
            otp = "{:06d}".format(secrets.randbelow(1000000))
            expires = (datetime.utcnow() + timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
            cur2 = mysql.connection.cursor()
            cur2.execute("UPDATE student SET verification_token=%s WHERE student_id=%s", (otp, user['student_id']))
            mysql.connection.commit()
            ok, err = send_email(email, "Your OTP Code", f"Your OTP is: {otp}\nIt expires in 10 minutes.")
            if ok:
                flash("OTP sent to your email", "info")
            else:
                flash(f"Failed to send OTP: {err}", "danger")
            return redirect('/otp-login')
        else:
            otp_entered = request.form.get('otp')
            if not user:
                flash("No user found with that email", "danger")
                return redirect('/otp-login')
            # compare with verification_token stored temporarily
            if str(user.get('verification_token')) == str(otp_entered):
                # log in
                session['student_id'] = user['student_id']
                session['student_name'] = user['full_name']
                # clear token
                cur2 = mysql.connection.cursor()
                cur2.execute("UPDATE student SET verification_token=NULL WHERE student_id=%s", (user['student_id'],))
                mysql.connection.commit()
                return redirect('/student/dashboard')
            else:
                flash("Invalid or expired OTP", "danger")
                return redirect('/otp-login')
    return render_template('otp_login.html')


@app.route('/student/dashboard')
def student_dashboard():
    if 'student_id' not in session:
        return redirect('/')

    cur = mysql.connection.cursor(MySQLdb.cursors.DictCursor)

    # Fetch student details (including balances)
    cur.execute("SELECT student_id, full_name, paid_leaves, emergency_leaves, extra_leaves, fine_amount FROM student WHERE student_id=%s", 
                (session['student_id'],))
    student = cur.fetchone()

    # Fetch student's leave history
    cur.execute("""
        SELECT id, reason, from_date, to_date, status, leave_type, leave_days, is_half_day, department
        FROM leave_requests
        WHERE student_id=%s
        ORDER BY from_date DESC
    """, (session['student_id'],))
    leaves = cur.fetchall()

    return render_template('student_dashboard.html', student=student, leaves=leaves)

# ---------------- APPLY LEAVE (updated) ----------------
from datetime import datetime, date

from datetime import datetime, date

@app.route('/student/apply_leave', methods=['GET', 'POST'])
def apply_leave():
    if 'student_id' not in session:
        return redirect('/')

    # Fetch student info
    cur = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    cur.execute("SELECT * FROM student WHERE student_id=%s", (session['student_id'],))
    student = cur.fetchone()

    if request.method == 'POST':
        reason = request.form.get('reason', '').strip()
        from_date = request.form.get('from_date')
        to_date = request.form.get('to_date')
        leave_type = request.form.get('leave_type', 'Paid Leave')
        half_day = request.form.get('half_day') == 'on'
        department = request.form.get('department') or student.get('department')

        # -------------------- DATE VALIDATION --------------------
        try:
            from_dt = datetime.strptime(from_date, "%Y-%m-%d").date()
            to_dt = datetime.strptime(to_date, "%Y-%m-%d").date()
        except ValueError:
            flash("Invalid date format")
            return redirect('/student/apply_leave')

        today = date.today()

        # ❌ Prevent past dates
        if from_dt < today or to_dt < today:
            flash("You cannot apply for leave for past dates.")
            return redirect('/student/apply_leave')

        # ❌ Prevent end date earlier than start date
        if to_dt < from_dt:
            flash("To Date cannot be earlier than From Date.")
            return redirect('/student/apply_leave')

        # ❌ Half-day only if same date
        if half_day and from_dt != to_dt:
            flash("Half-day leave is allowed only when start and end dates are the same.")
            return redirect('/student/apply_leave')

        # -------------------- CALCULATE LEAVE DAYS --------------------
        try:
            leave_days = days_between_dates(from_date, to_date, half_day)
        except:
            flash("Error calculating leave days.")
            return redirect('/student/apply_leave')

        ok, error = deduct_balance_for_leave(session['student_id'], leave_type, leave_days)
        if not ok:
            flash(error)
            return redirect('/student/apply_leave')

        try:
            cur2 = mysql.connection.cursor()
            cur2.execute("""
                INSERT INTO leave_requests
                (student_id, reason, from_date, to_date, status, leave_type, is_half_day, leave_days, department)
                VALUES(%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (
                session['student_id'], reason, from_date, to_date,
                "Pending", leave_type, int(half_day), leave_days, department
            ))
            mysql.connection.commit()
        except:
            mysql.connection.rollback()
            flash("Error saving leave request.")
            return redirect('/student/apply_leave')

        try:
            cur3 = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
            cur3.execute("SELECT email FROM admins WHERE email IS NOT NULL")
            admins = cur3.fetchall()

            for admin in admins:
                send_email(
                    admin["email"],
                    "New Leave Application",
                    f"{session.get('student_name')} applied for {leave_type} ({leave_days} days)."
                )
        except:
            pass  

        flash("Leave applied successfully!")
        return redirect('/student/dashboard')

    # -------------------- RENDER FORM --------------------
    return render_template(
        'apply_leave.html',
        student=student,
        current_date=date.today().isoformat() 
    )

    if 'student_id' not in session:
        return redirect('/')

    # Fetch student details
    cur = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    cur.execute("SELECT * FROM student WHERE student_id = %s", (session['student_id'],))
    student = cur.fetchone()

    if request.method == 'POST':
        reason = request.form.get('reason', '').strip()
        from_date = request.form.get('from_date')
        to_date = request.form.get('to_date')
        leave_type = request.form.get('leave_type', 'Paid Leave')
        half_day = request.form.get('half_day') == 'on'

        # ---------------- DATE VALIDATION ----------------
        # Convert date strings to date objects
        try:
            from_dt = datetime.strptime(from_date, "%Y-%m-%d").date()
            to_dt = datetime.strptime(to_date, "%Y-%m-%d").date()
        except Exception:
            flash("Invalid date format.")
            return redirect('/student/apply_leave')

        today = date.today()

        # Block past dates
        if from_dt < today or to_dt < today:
            flash("You cannot apply for leave for past dates.")
            return redirect('/student/apply_leave')

        # Block invalid range
        if to_dt < from_dt:
            flash("To Date cannot be earlier than From Date.")
            return redirect('/student/apply_leave')

        # Half-day only allowed for same-day leave
        if half_day and from_dt != to_dt:
            flash("Half-day can only be applied when From and To dates are the same.")
            return redirect('/student/apply_leave')

        # ---------------- CALCULATE LEAVE DAYS ----------------
        try:
            leave_days = days_between_dates(from_date, to_date, half_day)
        except Exception:
            flash("Error calculating leave days.")
            return redirect('/student/apply_leave')

        # ---------------- DEDUCT BALANCE ----------------
        ok, error = deduct_balance_for_leave(session['student_id'], leave_type, leave_days)
        if not ok:
            flash(error)
            return redirect('/student/apply_leave')

        # ---------------- INSERT INTO TABLE ----------------
        try:
            cur2 = mysql.connection.cursor()
            cur2.execute("""
                INSERT INTO leave_requests
                (student_id, reason, from_date, to_date, status, leave_type, is_half_day, leave_days, department)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (
                session['student_id'], reason, from_date, to_date,
                "Pending", leave_type, int(half_day), leave_days, department
            ))
            mysql.connection.commit()
        except Exception as e:
            mysql.connection.rollback()
            flash("An error occurred while submitting your leave request.")
            return redirect('/student/apply_leave')

        # ---------------- EMAIL ADMINS ----------------
        try:
            cur3 = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
            cur3.execute("SELECT email FROM admins WHERE email IS NOT NULL")
            admins = cur3.fetchall()

            for admin in admins:
                send_email(
                    admin['email'],
                    "New Leave Application",
                    f"{session.get('student_name')} applied for {leave_type} ({leave_days} days)."
                )
        except Exception:
            pass  # Don't break user experience if email fails

        flash("Leave applied successfully!")
        return redirect('/student/dashboard')

    # GET request → show template
    return render_template('apply_leave.html', student=student, current_date=date.today().isoformat())


# ---------------- STUDENT VIEW LEAVES ----------------
@app.route('/student/my_leaves')
def my_leaves():
    if 'student_id' not in session:
        return redirect('/')

    cur = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    cur.execute("SELECT * FROM leave_requests WHERE student_id=%s ORDER BY from_date DESC", (session['student_id'],))
    leaves = cur.fetchall()
    return render_template('my_leaves.html', leaves=leaves)


# ---------------- STUDENT CANCEL LEAVE ----------------
@app.route('/student/cancel_leave/<int:leave_id>')
def cancel_leave(leave_id):
    if 'student_id' not in session:
        return redirect('/')

    cur = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    cur.execute("SELECT * FROM leave_requests WHERE id=%s AND student_id=%s", (leave_id, session['student_id']))
    leave = cur.fetchone()
    if not leave:
        flash("Leave not found")
        return redirect('/student/dashboard')

    if leave['status'] == 'Cancelled':
        flash("Leave already cancelled")
        return redirect('/student/dashboard')

    # restore balance
    restore_balance_for_leave(session['student_id'], leave.get('leave_type') or 'Paid Leave', float(leave.get('leave_days') or 0))

    # update status
    cur2 = mysql.connection.cursor()
    cur2.execute("UPDATE leave_requests SET status=%s WHERE id=%s", ('Cancelled', leave_id))
    mysql.connection.commit()

    # notify admins and student
    try:
        cur3 = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
        cur3.execute("SELECT email FROM admins")
        admins = cur3.fetchall()
        for a in admins:
            if a.get('email'):
                send_email(a['email'], "Leave Cancelled", f"{session.get('student_name')} cancelled a leave.")
    except Exception:
        pass

    flash("Leave cancelled and balance restored")
    return redirect('/student/my_leaves')


# ---------------- ADMIN LOGIN / DASHBOARD ----------------
@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']

        cur = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
        cur.execute("SELECT * FROM admins WHERE username=%s AND password=%s", (username, password))
        admin = cur.fetchone()

        if admin:
            session['admin_id'] = admin['id']
            session['admin_name'] = admin['username']
            return redirect('/admin/dashboard')
        else:
            flash("Invalid admin credentials")
            return redirect('/admin/login')

    return render_template('admin_login.html')

@app.route('/admin/dashboard')
def admin_dashboard():
    if 'admin_id' not in session:
        return redirect('/admin/login')
    return render_template('admin_dashboard.html')


# ---------------- ADMIN: VIEW REQUESTS ----------------
@app.route('/admin/requests')
def admin_requests():
    if 'admin_id' not in session:
        return redirect('/admin/login')

    cur = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    cur.execute("""
        SELECT lr.id, s.full_name, s.department AS department, lr.reason, lr.from_date, lr.to_date, lr.status,
               lr.leave_type, lr.leave_days, lr.is_half_day
        FROM leave_requests lr
        JOIN student s ON lr.student_id = s.student_id
        ORDER BY lr.from_date DESC
    """)
    data = cur.fetchall()
    return render_template("view_leave_requests.html", data=data)


@app.route('/admin/requests/search')
def admin_requests_search():
    if 'admin_id' not in session:
        return jsonify([]), 403

    q = request.args.get('q', '').strip()
    cur = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    if q:
        like = f"%{q}%"
        cur.execute("""
            SELECT lr.id, s.full_name, s.department AS department, lr.reason, lr.from_date, lr.to_date, lr.status,
                   lr.leave_type, lr.leave_days, lr.is_half_day
            FROM leave_requests lr
            JOIN student s ON lr.student_id = s.student_id
            WHERE s.full_name LIKE %s OR s.department LIKE %s OR lr.reason LIKE %s OR lr.leave_type LIKE %s OR lr.status LIKE %s OR lr.from_date LIKE %s
            ORDER BY lr.from_date DESC
            LIMIT 500
        """, (like, like, like, like, like, like))
    else:
        cur.execute("""
            SELECT lr.id, s.full_name, s.department AS department, lr.reason, lr.from_date, lr.to_date, lr.status,
                   lr.leave_type, lr.leave_days, lr.is_half_day
            FROM leave_requests lr
            JOIN student s ON lr.student_id = s.student_id
            ORDER BY lr.from_date DESC
            LIMIT 500
        """)

    rows = cur.fetchall()
    # convert date objects to strings for JSON
    out = []
    for r in rows:
        nr = {}
        for k, v in r.items():
            try:
                from datetime import date as _date, datetime as _dt
                if isinstance(v, (_date, _dt)):
                    nr[k] = str(v)
                else:
                    nr[k] = v
            except Exception:
                nr[k] = v
        out.append(nr)
    return jsonify(out)


# ---------------- ADMIN: APPROVE / REJECT ----------------
@app.route('/admin/update/<int:id>/<string:action>')
def update_status(id, action):
    if 'admin_id' not in session:
        return redirect('/admin/login')

    cur = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    cur.execute("SELECT * FROM leave_requests WHERE id=%s", (id,))
    leave = cur.fetchone()
    if not leave:
        flash("Leave request not found")
        return redirect('/admin/requests')

    if action == "approve":
        cur2 = mysql.connection.cursor()
        cur2.execute("UPDATE leave_requests SET status=%s WHERE id=%s", ("Approved", id))
        mysql.connection.commit()

        # notify student
        cur3 = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
        cur3.execute("SELECT email FROM student WHERE student_id=%s", (leave['student_id'],))
        s = cur3.fetchone()
        if s and s.get('email'):
            send_email(s['email'], "Leave Approved", f"Your leave request from {leave['from_date']} to {leave['to_date']} has been approved.")

    else:
        # Rejection: restore balance
        restore_balance_for_leave(leave['student_id'], leave.get('leave_type') or 'Paid Leave', float(leave.get('leave_days') or 0))
        cur2 = mysql.connection.cursor()
        cur2.execute("UPDATE leave_requests SET status=%s WHERE id=%s", ("Rejected", id))
        mysql.connection.commit()

        # notify student
        cur3 = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
        cur3.execute("SELECT email FROM student WHERE student_id=%s", (leave['student_id'],))
        s = cur3.fetchone()
        if s and s.get('email'):
            send_email(s['email'], "Leave Rejected", f"Your leave request from {leave['from_date']} to {leave['to_date']} has been rejected.")

    return redirect('/admin/requests')


# ---------------- ADMIN REPORTS (counts + per-student summary) ----------------
@app.route('/admin/reports')
def admin_reports():
    if 'admin_id' not in session:
        return redirect('/admin/login')

    cur = mysql.connection.cursor(MySQLdb.cursors.DictCursor)

    cur.execute("SELECT COUNT(*) AS total_requests FROM leave_requests")
    total = cur.fetchone()['total_requests']

    cur.execute("SELECT COUNT(*) AS approved FROM leave_requests WHERE status='Approved'")
    approved = cur.fetchone()['approved']

    cur.execute("SELECT COUNT(*) AS rejected FROM leave_requests WHERE status='Rejected'")
    rejected = cur.fetchone()['rejected']

    cur.execute("SELECT COUNT(*) AS pending FROM leave_requests WHERE status='Pending'")
    pending = cur.fetchone()['pending']

    cur.execute("""
        SELECT s.full_name, COUNT(lr.id) AS total_leaves,
               SUM(lr.status='Approved') AS approved,
               SUM(lr.status='Rejected') AS rejected,
               SUM(lr.status='Pending') AS pending
        FROM leave_requests lr
        JOIN student s ON lr.student_id = s.student_id
        GROUP BY s.student_id
        ORDER BY s.full_name
    """)
    student_summary = cur.fetchall()

    return render_template('admin_reports.html',
                           total=total,
                           approved=approved,
                           rejected=rejected,
                           pending=pending,
                           student_summary=student_summary)


# ---------------- EXPORT REPORTS (Excel + optional PDF) ----------------
@app.route('/admin/export-report')
def admin_export_report():
    if 'admin_id' not in session:
        return redirect('/admin/login')

    month = request.args.get('month')  # expected format: YYYY-MM
    if not month:
        flash("Please provide month parameter as YYYY-MM")
        return redirect('/admin/reports')

    cur = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    # simple filter: from_date startswith month
    cur.execute("SELECT lr.id, s.full_name as student, s.department as department, lr.leave_type, lr.from_date, lr.to_date, lr.leave_days, lr.status FROM leave_requests lr JOIN student s ON lr.student_id=s.student_id WHERE lr.from_date LIKE %s ORDER BY lr.from_date", (month + '%',))
    rows = cur.fetchall()

    # create excel
    wb = Workbook()
    ws = wb.active
    ws.title = f"Leaves_{month}"
    header = ["ID","Student","Department","Type","From","To","Days","Status"]
    ws.append(header)
    for r in rows:
        ws.append([r.get('id'), r.get('student'), r.get('department'), r.get('leave_type'), str(r.get('from_date')), str(r.get('to_date')), r.get('leave_days'), r.get('status')])

    output = BytesIO()
    wb.save(output)
    output.seek(0)

    # generate PDF if possible
    pdf_bytes = None
    if REPORTLAB_AVAILABLE:
        pdf_buf = BytesIO()
        c = canvas.Canvas(pdf_buf, pagesize=letter)
        w, h = letter
        y = h - 50
        c.setFont("Helvetica-Bold", 14)
        c.drawString(50, y, f"Leave Report - {month}")
        y -= 30
        c.setFont("Helvetica", 10)
        for r in rows:
            line = f"{r.get('student')}: {r.get('leave_type')} {r.get('from_date')} -> {r.get('to_date')} ({r.get('leave_days')}d) [{r.get('status')}]"
            c.drawString(50, y, line)
            y -= 12
            if y < 50:
                c.showPage()
                y = h - 50
        c.save()
        pdf_buf.seek(0)
        pdf_bytes = pdf_buf.read()

    # create a zip-like response? For simplicity, return excel file directly
    # If desired, you can also return both files in separate endpoints
    return send_file(
        output,
        as_attachment=True,
        download_name=f"leave_report_{month}.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )


# ---------------- ADMIN: Chart data ----------------
@app.route('/admin/chart-data')
def admin_chart_data():
    if 'admin_id' not in session:
        return jsonify({}), 403

    cur = mysql.connection.cursor()
    cur.execute("SELECT leave_type, SUM(leave_days) AS total_days FROM leave_requests GROUP BY leave_type")
    rows = cur.fetchall()
    data = {}
    for r in rows:
        data[r[0]] = float(r[1] or 0)
    return jsonify(data)


@app.route('/admin/charts')
def admin_charts():
    if 'admin_id' not in session:
        return redirect('/admin/login')
    return render_template('admin_charts.html')  # create template to fetch chart-data and render Chart.js


# ---------------- CARRY-FORWARD (superadmin only) ----------------
@app.route('/admin/carry-forward', methods=['POST'])
def admin_carry_forward():
    if 'admin_id' not in session:
        return redirect('/admin/login')

    # Check admin role if your admins table has role field (optional). We'll allow any admin for now.
    MAX_CARRY = 10.0
    YEARLY_PAID = 12.0

    cur = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    cur.execute("SELECT student_id, paid_leaves, extra_leaves FROM student")
    students = cur.fetchall()
    for s in students:
        sid = s['student_id']
        paid = float(s.get('paid_leaves') or 0)
        extra = float(s.get('extra_leaves') or 0)
        carry = min(paid, MAX_CARRY) if paid > 0 else 0
        # add carry to extra_leaves, reset paid_leaves to YEARLY_PAID
        new_extra = extra + carry
        cur2 = mysql.connection.cursor()
        cur2.execute("UPDATE student SET extra_leaves=%s, paid_leaves=%s WHERE student_id=%s", (new_extra, YEARLY_PAID, sid))
    mysql.connection.commit()
    flash("Carry forward executed")
    return redirect('/admin/dashboard')


# ---------------- ADMIN: impose fine on student ----------------
@app.route('/admin/impose-fine/<int:student_id>', methods=['POST'])
def admin_impose_fine(student_id):
    if 'admin_id' not in session:
        return redirect('/admin/login')
    amount = float(request.form.get('amount', 0))
    if amount <= 0:
        flash("Invalid fine amount")
        return redirect('/admin/students')
    cur = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    cur.execute("SELECT fine_amount FROM student WHERE student_id=%s", (student_id,))
    s = cur.fetchone()
    if not s:
        flash("Student not found")
        return redirect('/admin/students')
    new_fine = float(s.get('fine_amount') or 0) + amount
    cur2 = mysql.connection.cursor()
    cur2.execute("UPDATE student SET fine_amount=%s WHERE student_id=%s", (new_fine, student_id))
    mysql.connection.commit()
    flash(f"Fine of {amount} imposed.")
    # optional: send email to student about fine
    cur3 = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    cur3.execute("SELECT email FROM student WHERE student_id=%s", (student_id,))
    stud = cur3.fetchone()
    if stud and stud.get('email'):
        send_email(stud['email'], "Fine Imposed", f"A fine of {amount} has been imposed on your account. Total fines: {new_fine}")
    return redirect('/admin/students')


# ---------------- ADMIN: students list (unchanged) ----------------
@app.route('/admin/students')
def admin_view_students():
    if 'admin_id' not in session:
        return redirect('/admin/login')
    q = request.args.get('q', '').strip()
    cur = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    if q:
        like = f"%{q}%"
        cur.execute("SELECT * FROM student WHERE full_name LIKE %s OR email LIKE %s OR department LIKE %s ORDER BY full_name ASC", (like, like, like))
    else:
        cur.execute("SELECT * FROM student ORDER BY full_name ASC")
    students = cur.fetchall()
    return render_template('admin_students.html', students=students, q=q)


@app.route('/admin/students/search')
def admin_students_search():
    if 'admin_id' not in session:
        return jsonify([]), 403
    q = request.args.get('q', '').strip()
    cur = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    if q:
        like = f"%{q}%"
        cur.execute("SELECT student_id, full_name, email, department FROM student WHERE full_name LIKE %s OR email LIKE %s OR department LIKE %s ORDER BY full_name ASC LIMIT 200", (like, like, like))
    else:
        cur.execute("SELECT student_id, full_name, email, department FROM student ORDER BY full_name ASC LIMIT 200")
    rows = cur.fetchall()
    # DictCursor returns rows as dict-like; jsonify can handle lists of dicts
    return jsonify(rows)


# ---------------- Download students/leaves Excel (existing) ----------------
@app.route('/admin/download-employee-excel')
def admin_download_students_excel():
    if 'admin_id' not in session:
        return redirect('/admin/login')

    cur = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    cur.execute("SELECT * FROM leave_requests ORDER BY student_id ASC")
    rows = cur.fetchall()

    wb = Workbook()
    ws = wb.active
    ws.title = "Leaves"
    if rows:
        ws.append(list(rows[0].keys()))
        for r in rows:
            ws.append(list(r.values()))

    output = BytesIO()
    wb.save(output)
    output.seek(0)
    return send_file(
        output,
        as_attachment=True,
        download_name="leave_requests.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )


# ---------------- EMAIL VERIFICATION ROUTE (existing) ----------------
@app.route('/verify/<token>')
def verify_email(token):
    cur = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    cur.execute("SELECT * FROM student WHERE verification_token=%s", (token,))
    user = cur.fetchone()

    if user:
        if user['is_verified']:
            flash("Your account is already verified.")
        else:
            cur2 = mysql.connection.cursor()
            cur2.execute("UPDATE student SET is_verified=1, verification_token=NULL WHERE student_id=%s", (user['student_id'],))
            mysql.connection.commit()
            flash("Your account is verified! You can now login.")
        return redirect('/login')

    flash("Invalid or expired verification link.")
    return redirect('/')

# ---------------- ADMIN: VIEW LEAVE REQUESTS (compatible) ----------------
# (already present above as /admin/requests)
@app.route('/student/profile')
def student_profile():
    if 'student_id' not in session:
        return redirect('/') 
    cur = mysql.connection.cursor(MySQLdb.cursors.DictCursor) 
    cur.execute("SELECT * FROM student WHERE student_id=%s", (session['student_id'],)) 
    student = cur.fetchone() 
    return render_template('profile.html', student=student)

# ---------------- LOGOUT ----------------
@app.route('/logout')
def logout():
    session.clear()
    return redirect('/')

@app.route('/choose')
def choose_user():
    return render_template('choose_user.html')

@app.route('/')
def home():
    return redirect('/choose')
@app.route('/admin/enhanced-charts')
def admin_enhanced_charts():
    if 'admin_id' not in session:
        return redirect('/admin/login')
    return render_template('admin_enhanced_charts.html')
@app.route('/admin/enhanced-chart-data')
def admin_enhanced_chart_data():
    if 'admin_id' not in session:
        return {}, 403

    cur = mysql.connection.cursor(MySQLdb.cursors.DictCursor)

    # 1️⃣ Overall leave type totals (Pie chart)
    cur.execute("SELECT leave_type, SUM(leave_days) AS total_days FROM leave_requests GROUP BY leave_type")
    leave_type_data = {row['leave_type']: float(row['total_days']) for row in cur.fetchall()}

    # 2️⃣ Monthly leave trends (stacked bar)
    cur.execute("""
        SELECT DATE_FORMAT(from_date, '%Y-%m') AS month,
               SUM(status='Approved') AS approved,
               SUM(status='Pending') AS pending,
               SUM(status='Rejected') AS rejected
        FROM leave_requests
        GROUP BY month
        ORDER BY month
    """)
    monthly_data = cur.fetchall()
    months = [row['month'] for row in monthly_data]
    approved = [row['approved'] for row in monthly_data]
    pending = [row['pending'] for row in monthly_data]
    rejected = [row['rejected'] for row in monthly_data]

    # 3️⃣ Leaves per student (stacked bar)
    cur.execute("""
        SELECT s.full_name AS student,
               SUM(lr.status='Approved') AS approved,
               SUM(lr.status='Pending') AS pending,
               SUM(lr.status='Rejected') AS rejected
        FROM leave_requests lr
        JOIN student s ON lr.student_id = s.student_id
        GROUP BY s.student_id
        ORDER BY s.full_name
    """)
    student_data = cur.fetchall()
    students = [row['student'] for row in student_data]
    student_approved = [row['approved'] for row in student_data]
    student_pending = [row['pending'] for row in student_data]
    student_rejected = [row['rejected'] for row in student_data]

    return {
        "leave_type": leave_type_data,
        "monthly": {
            "months": months,
            "approved": approved,
            "pending": pending,
            "rejected": rejected
        },
        "student": {
            "students": students,
            "approved": student_approved,
            "pending": student_pending,
            "rejected": student_rejected
        }
    }


if __name__ == "__main__":
    app.run(debug=True)
