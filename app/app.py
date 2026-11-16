"""LiveAttend - live QR-code attendance tracker.
Cloud & DevOps Mini Project: AWS + Git + Jenkins + Terraform + Ansible + Docker (one-click deployment).

How it works: faculty start a class session -> the projector shows a QR code that changes every few seconds
-> students scan it with their phones, enter their register number and are marked present instantly.
The rotating, server-signed token stops a photo of the QR code being shared with friends outside the room.
"""
import csv
import hashlib
import hmac
import io
import logging
import os
import re
import secrets
import socket
import time
from contextlib import contextmanager
from datetime import timezone
from functools import wraps

import qrcode
import qrcode.image.svg
from flask import (Flask, Response, abort, flash, g, jsonify, redirect, render_template, request, session, url_for)
from sqlalchemy import func, text
from sqlalchemy.exc import IntegrityError, OperationalError

from models import ClassSession, Course, Record, User, db, now

VERSION = os.getenv("APP_VERSION", "dev")
BUILD = os.getenv("BUILD_NUMBER", "local")
COMMIT = os.getenv("GIT_COMMIT", "local")[:7]
ROTATE = int(os.getenv("QR_ROTATE_SECONDS", "20"))   # the QR code changes every ROTATE seconds
GRACE = 2                                            # a scanned code stays valid for GRACE extra windows (slow phones)
REG_RE = re.compile(r"^[A-Z0-9]{5,20}$")

app = Flask(__name__)
app.config.update(
    SECRET_KEY=os.getenv("SECRET_KEY") or secrets.token_hex(32),
    SQLALCHEMY_DATABASE_URI=os.getenv("DATABASE_URL", "sqlite:///liveattend.db"),
    SQLALCHEMY_ENGINE_OPTIONS={"pool_pre_ping": True},
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
)
db.init_app(app)

# Application event log -> stdout -> Docker awslogs driver -> CloudWatch Logs (/liveattend/app)
logging.basicConfig(level=logging.INFO, format="[%(asctime)s] [liveattend] %(message)s")
log = logging.getLogger("liveattend")
if not os.getenv("SECRET_KEY"):
    log.warning("SECRET_KEY is not set: every worker uses its own random key, so logins break with more than one worker")


def client_ip():
    return (request.headers.get("X-Forwarded-For") or request.remote_addr or "")[:64]


# ------------------------------------------------------------------ rotating QR token
def make_token(sess, at=None):
    window = int((at if at is not None else time.time()) // ROTATE)
    sig = hmac.new(sess.secret.encode(), f"{sess.id}:{window}".encode(), hashlib.sha256).hexdigest()[:20]
    return f"{window}.{sig}"


def token_ok(sess, token):
    try:
        window, sig = token.split(".", 1)
        window = int(window)
    except (ValueError, AttributeError):
        return False
    current = int(time.time() // ROTATE)
    if not current - GRACE <= window <= current:
        return False
    good = hmac.new(sess.secret.encode(), f"{sess.id}:{window}".encode(), hashlib.sha256).hexdigest()[:20]
    return hmac.compare_digest(sig, good)


def qr_svg(data):
    img = qrcode.make(data, image_factory=qrcode.image.svg.SvgPathImage, box_size=12, border=2)
    buf = io.BytesIO()
    img.save(buf)
    return buf.getvalue().decode()


def mark_url(sess):
    base = (os.getenv("PUBLIC_URL") or request.host_url).rstrip("/")
    return f"{base}{url_for('mark', s=sess.id, t=make_token(sess))}"


# ------------------------------------------------------------------ helpers
def login_required(view):
    @wraps(view)
    def wrapper(*a, **kw):
        if not g.user:
            return redirect(url_for("login", next=request.path))
        return view(*a, **kw)
    return wrapper


@app.before_request
def load_user():
    uid = session.get("uid")
    g.user = db.session.get(User, uid) if uid else None
    if request.method == "POST":
        token = request.form.get("csrf")
        if not token or not session.get("csrf") or not secrets.compare_digest(token, session["csrf"]):
            abort(400, "invalid form token")


@app.context_processor
def inject():
    if "csrf" not in session:
        session["csrf"] = secrets.token_hex(16)
    return {"csrf": session["csrf"], "user": g.get("user"), "version": VERSION, "build": BUILD,
            "commit": COMMIT, "server": socket.gethostname()}


@app.after_request
def headers(resp):
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "same-origin"
    resp.headers["Cache-Control"] = "no-store" if request.path.endswith(".json") else resp.headers.get("Cache-Control", "no-cache")
    return resp


def own_session(sid):
    return (ClassSession.query.join(Course).filter(ClassSession.id == sid, Course.owner_id == g.user.id).first_or_404())


def iso(dt):
    return dt.replace(tzinfo=dt.tzinfo or timezone.utc).isoformat() if dt else None


@contextmanager
def startup_lock():
    """Several workers / pods start at once: on PostgreSQL a transaction-level advisory lock lets only one seed the database."""
    if db.engine.dialect.name != "postgresql":
        yield
        return
    with db.engine.begin() as conn:
        conn.execute(text("SELECT pg_advisory_xact_lock(7342)"))
        yield


def seed():
    """Create tables, the admin faculty account and a demo course (idempotent, safe to run concurrently)."""
    with startup_lock():
        try:
            db.create_all()
            admin = User.query.filter_by(username="admin").first()
            if not admin:
                admin = User(username="admin")
                admin.set_password(os.getenv("ADMIN_PASSWORD") or "admin-change-me")
                db.session.add(admin)
                db.session.commit()
            if not Course.query.filter_by(owner_id=admin.id).first():
                db.session.add(Course(code="21IPE333P", name="Essentials in Cloud and DevOps", owner=admin))
                db.session.commit()
        except (IntegrityError, OperationalError):
            db.session.rollback()    # another worker seeded at the same moment - nothing left to do


# ------------------------------------------------------------------ pages
@app.route("/")
def index():
    stats = {"courses": Course.query.count(), "sessions": ClassSession.query.count(),
             "marks": Record.query.count(), "live": ClassSession.query.filter(ClassSession.ended.is_(None)).count()}
    return render_template("index.html", stats=stats)


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        u = User.query.filter_by(username=request.form.get("username", "").strip()).first()
        if u and u.check_password(request.form.get("password", "")):
            session.clear()
            session["uid"] = u.id
            log.info("LOGIN ok user=%s ip=%s", u.username, client_ip())
            nxt = request.args.get("next") or ""
            return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else url_for("dashboard"))
        log.warning("LOGIN failed user=%s ip=%s", request.form.get("username", "")[:40], client_ip())
        flash("Wrong username or password")
    return render_template("login.html", mode="login")


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        name = request.form.get("username", "").strip()
        pw = request.form.get("password", "")
        if not (3 <= len(name) <= 40) or len(pw) < 8:
            flash("Username 3-40 characters, password at least 8")
        elif User.query.filter_by(username=name).first():
            flash("That username is taken")
        else:
            u = User(username=name)
            u.set_password(pw)
            db.session.add(u)
            db.session.commit()
            session.clear()
            session["uid"] = u.id
            log.info("REGISTER new faculty user=%s ip=%s", u.username, client_ip())
            return redirect(url_for("dashboard"))
    return render_template("login.html", mode="register")


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("index"))


@app.route("/dashboard")
@login_required
def dashboard():
    courses = Course.query.filter_by(owner_id=g.user.id).order_by(Course.created.desc()).all()
    live = (ClassSession.query.join(Course).filter(Course.owner_id == g.user.id, ClassSession.ended.is_(None)).all())
    return render_template("dashboard.html", courses=courses, live=live)


@app.route("/course/new", methods=["POST"])
@login_required
def course_new():
    code = request.form.get("code", "").strip()[:20]
    name = request.form.get("name", "").strip()[:120]
    if not code or not name:
        flash("Give the course a code and a name")
    else:
        db.session.add(Course(code=code, name=name, owner=g.user))
        db.session.commit()
        log.info("COURSE created code=%s by=%s", code, g.user.username)
    return redirect(url_for("dashboard"))


@app.route("/course/<int:cid>")
@login_required
def course_page(cid):
    course = Course.query.filter_by(id=cid, owner_id=g.user.id).first_or_404()
    total = sum(1 for s in course.sessions if not s.is_open)
    rows = (db.session.query(Record.reg_no, func.max(Record.name), func.count(Record.id))
            .join(ClassSession).filter(ClassSession.course_id == cid)
            .group_by(Record.reg_no).order_by(Record.reg_no).all())
    students = [{"reg_no": r, "name": n, "present": c, "pct": round(100 * c / total) if total else 0} for r, n, c in rows]
    return render_template("course.html", course=course, students=students, total=total)


@app.route("/course/<int:cid>/delete", methods=["POST"])
@login_required
def course_delete(cid):
    course = Course.query.filter_by(id=cid, owner_id=g.user.id).first_or_404()
    db.session.delete(course)
    db.session.commit()
    return redirect(url_for("dashboard"))


@app.route("/course/<int:cid>/start", methods=["POST"])
@login_required
def session_start(cid):
    course = Course.query.filter_by(id=cid, owner_id=g.user.id).first_or_404()
    title = request.form.get("title", "").strip()[:120] or now().strftime("Class on %d %b %Y, %H:%M UTC")
    sess = ClassSession(course=course, title=title, secret=secrets.token_hex(32))
    db.session.add(sess)
    db.session.commit()
    log.info("SESSION opened id=%s course=%s by=%s", sess.id, course.code, g.user.username)
    return redirect(url_for("session_host", sid=sess.id))


# ------------------------------------------------------------------ faculty: live session
@app.route("/session/<int:sid>")
@login_required
def session_host(sid):
    sess = own_session(sid)
    if not sess.is_open:
        return redirect(url_for("session_report", sid=sid))
    return render_template("session.html", sess=sess, rotate=ROTATE, qr=qr_svg(mark_url(sess)))


@app.route("/session/<int:sid>/qr.json")
@login_required
def session_qr(sid):
    sess = own_session(sid)
    if not sess.is_open:
        return jsonify(open=False)
    url = mark_url(sess)
    return jsonify(open=True, svg=qr_svg(url), url=url, expires_in=ROTATE - int(time.time()) % ROTATE)


@app.route("/session/<int:sid>/live.json")
@login_required
def session_live(sid):
    sess = own_session(sid)
    recs = [{"reg_no": r.reg_no, "name": r.name, "at": iso(r.marked_at)} for r in sess.records]
    return jsonify(open=sess.is_open, count=len(recs), records=recs[::-1])


@app.route("/session/<int:sid>/end", methods=["POST"])
@login_required
def session_end(sid):
    sess = own_session(sid)
    if sess.is_open:
        sess.ended = now()
        db.session.commit()
        log.info("SESSION closed id=%s present=%d", sess.id, len(sess.records))
    return redirect(url_for("session_report", sid=sid))


@app.route("/session/<int:sid>/report")
@login_required
def session_report(sid):
    return render_template("report.html", sess=own_session(sid))


@app.route("/session/<int:sid>/export.csv")
@login_required
def session_csv(sid):
    sess = own_session(sid)
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["register_no", "name", "marked_at_utc"])
    for r in sess.records:
        w.writerow([r.reg_no, r.name, iso(r.marked_at)])
    return Response(out.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename=attendance-session-{sess.id}.csv"})


# ------------------------------------------------------------------ students: scan the QR code
@app.route("/mark", methods=["GET", "POST"])
def mark():
    sid = request.values.get("s", type=int)
    token = request.values.get("t", "")
    sess = db.session.get(ClassSession, sid) if sid else None
    if not sess or not sess.is_open:
        return render_template("mark.html", state="closed"), 404
    if not token_ok(sess, token):
        return render_template("mark.html", state="expired", sess=sess), 410
    marked = session.get("marked", {})
    if request.method == "POST":
        reg = re.sub(r"\s+", "", request.form.get("reg_no", "")).upper()
        name = " ".join(request.form.get("name", "").split())[:60]
        if not REG_RE.match(reg) or len(name) < 2:
            flash("Enter your register number (letters and digits) and your full name")
            return render_template("mark.html", state="form", sess=sess, token=token), 400
        if marked.get(str(sess.id), reg) != reg:
            flash("This phone already marked attendance for a different register number")
            log.warning("MARK refused (second reg on same device) session=%s reg=%s ip=%s", sess.id, reg, client_ip())
            return render_template("mark.html", state="form", sess=sess, token=token), 409
        db.session.add(Record(session_id=sess.id, reg_no=reg, name=name, ip=client_ip()))
        try:
            db.session.commit()
            state = "done"
            log.info("MARK present session=%s reg=%s ip=%s", sess.id, reg, client_ip())
        except IntegrityError:
            db.session.rollback()
            state = "already"
        marked[str(sess.id)] = reg
        session["marked"] = marked
        return render_template("mark.html", state=state, sess=sess, reg=reg, name=name)
    if str(sess.id) in marked:
        return render_template("mark.html", state="already", sess=sess, reg=marked[str(sess.id)])
    return render_template("mark.html", state="form", sess=sess, token=token)


# ------------------------------------------------------------------ health + stats (used by Jenkins smoke test)
@app.route("/healthz")
def healthz():
    db.session.execute(db.select(1))
    return jsonify(status="ok", version=VERSION, build=BUILD, commit=COMMIT, server=socket.gethostname())


@app.route("/api/stats")
def api_stats():
    return jsonify(courses=Course.query.count(), sessions=ClassSession.query.count(), marks=Record.query.count(),
                   live_sessions=ClassSession.query.filter(ClassSession.ended.is_(None)).count(),
                   qr_rotate_seconds=ROTATE, version=VERSION)


with app.app_context():
    seed()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
