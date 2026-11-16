import re
import time

import pytest

import app as la
from models import ClassSession, Record, db


@pytest.fixture()
def client():
    la.app.config["TESTING"] = True
    with la.app.app_context():
        db.drop_all()
        la.seed()
    with la.app.test_client() as c:
        yield c


def csrf(c, path="/login"):
    html = c.get(path).get_data(as_text=True)
    return re.search(r'name="csrf" value="([0-9a-f]+)"', html).group(1)


def login(c, user="admin", pw="test-admin-pw"):
    return c.post("/login", data={"username": user, "password": pw, "csrf": csrf(c)})


def start_session(c, title="Lecture 1"):
    login(c)
    r = c.post("/course/1/start", data={"title": title, "csrf": csrf(c, "/dashboard")})
    assert r.status_code == 302
    return int(r.headers["Location"].rsplit("/", 1)[1])


def scan(c, sid, token=None):
    """What a student's phone does: open the URL from the QR code."""
    with la.app.app_context():
        sess = db.session.get(ClassSession, sid)
        token = token or la.make_token(sess)
    return token, c.get(f"/mark?s={sid}&t={token}")


def submit(c, sid, token, reg="RA2311003011577", name="Test Student"):
    html = c.get(f"/mark?s={sid}&t={token}").get_data(as_text=True)
    tok = re.search(r'name="csrf" value="([0-9a-f]+)"', html).group(1)
    return c.post("/mark", data={"s": sid, "t": token, "reg_no": reg, "name": name, "csrf": tok})


# ------------------------------------------------------------------ basics
def test_healthz(client):
    d = client.get("/healthz").get_json()
    assert d["status"] == "ok" and "build" in d


def test_home_and_stats(client):
    assert b"Attendance in one scan" in client.get("/").data
    d = client.get("/api/stats").get_json()
    assert d["courses"] == 1 and d["marks"] == 0 and d["qr_rotate_seconds"] == 20


def test_login_wrong_password(client):
    r = login(client, pw="nope")
    assert r.status_code == 200 and b"Wrong username or password" in r.data
    assert client.get("/dashboard").status_code == 302


def test_register_and_dashboard(client):
    r = client.post("/register", data={"username": "prof", "password": "longenough1", "csrf": csrf(client, "/register")})
    assert r.status_code == 302
    assert b"Your courses" in client.get("/dashboard").data


def test_csrf_required(client):
    assert client.post("/login", data={"username": "admin", "password": "test-admin-pw"}).status_code == 400


def test_faculty_pages_need_login(client):
    for path in ("/dashboard", "/session/1", "/session/1/live.json", "/session/1/export.csv"):
        assert client.get(path).status_code == 302


# ------------------------------------------------------------------ rotating token
def test_token_rotates_and_expires(client):
    sid = start_session(client)
    with la.app.app_context():
        sess = db.session.get(ClassSession, sid)
        now = time.time()
        assert la.token_ok(sess, la.make_token(sess, now))
        assert la.token_ok(sess, la.make_token(sess, now - la.ROTATE))
        assert not la.token_ok(sess, la.make_token(sess, now - 10 * la.ROTATE))   # a shared photo goes stale
        assert not la.token_ok(sess, la.make_token(sess, now + 5 * la.ROTATE))
        assert la.make_token(sess, now) != la.make_token(sess, now + la.ROTATE)
        assert not la.token_ok(sess, "garbage") and not la.token_ok(sess, None)


def test_forged_token_rejected(client):
    sid = start_session(client)
    window = int(time.time() // la.ROTATE)
    assert client.get(f"/mark?s={sid}&t={window}.deadbeefdeadbeefdead").status_code == 410


def test_token_of_other_session_rejected(client):
    sid = start_session(client)
    client.post(f"/session/{sid}/end", data={"csrf": csrf(client, f"/session/{sid}/report")})
    sid2 = start_session(client, "Lecture 2")
    with la.app.app_context():
        old = la.make_token(db.session.get(ClassSession, sid))
    assert client.get(f"/mark?s={sid2}&t={old}").status_code == 410


# ------------------------------------------------------------------ marking attendance
def test_student_marks_attendance(client):
    sid = start_session(client)
    with la.app.test_client() as phone:
        token, page = scan(phone, sid)
        assert page.status_code == 200 and b"Mark me present" in page.data
        r = submit(phone, sid, token, reg="ra2311003011577", name="Naga  Sai")
        assert b"You are marked present" in r.data
    live = client.get(f"/session/{sid}/live.json").get_json()
    assert live["count"] == 1 and live["records"][0]["reg_no"] == "RA2311003011577"
    assert live["records"][0]["name"] == "Naga Sai"


def test_duplicate_mark_counts_once(client):
    sid = start_session(client)
    with la.app.test_client() as a, la.app.test_client() as b:
        t, _ = scan(a, sid)
        submit(a, sid, t)
        assert b"Already marked" in submit(b, sid, t).data      # different phone, same register number
    assert client.get(f"/session/{sid}/live.json").get_json()["count"] == 1


def test_same_phone_cannot_mark_for_friend(client):
    sid = start_session(client)
    with la.app.test_client() as phone:
        t, _ = scan(phone, sid)
        submit(phone, sid, t, reg="RA2311003011577")
        with phone.session_transaction() as s:              # the phone tries to POST again for a friend
            tok = s["csrf"]
        r = phone.post("/mark", data={"s": sid, "t": t, "reg_no": "RA2311003011599", "name": "Friend", "csrf": tok})
        assert r.status_code == 409 and b"different register number" in r.data
    assert client.get(f"/session/{sid}/live.json").get_json()["count"] == 1


def test_invalid_input_rejected(client):
    sid = start_session(client)
    with la.app.test_client() as phone:
        t, _ = scan(phone, sid)
        assert submit(phone, sid, t, reg="x!", name="A").status_code == 400
    assert client.get(f"/session/{sid}/live.json").get_json()["count"] == 0


def test_closed_session_refuses(client):
    sid = start_session(client)
    client.post(f"/session/{sid}/end", data={"csrf": csrf(client, f"/session/{sid}/report")})
    with la.app.test_client() as phone:
        _, page = scan(phone, sid)
        assert page.status_code == 404 and b"Session closed" in page.data


# ------------------------------------------------------------------ faculty views
def test_qr_endpoint_and_export(client):
    sid = start_session(client)
    d = client.get(f"/session/{sid}/qr.json").get_json()
    assert d["open"] and "<svg" in d["svg"]
    assert 0 < d["expires_in"] <= la.ROTATE
    assert "/mark?s=" in d["url"] and "&t=" in d["url"]
    with la.app.test_client() as phone:
        t, _ = scan(phone, sid)
        submit(phone, sid, t)
    client.post(f"/session/{sid}/end", data={"csrf": csrf(client, f"/session/{sid}/report")})
    csv_text = client.get(f"/session/{sid}/export.csv").get_data(as_text=True)
    assert "register_no,name,marked_at_utc" in csv_text and "RA2311003011577" in csv_text
    assert b"RA2311003011577" in client.get("/course/1").data


def test_other_faculty_cannot_see_session(client):
    sid = start_session(client)
    with la.app.test_client() as other:
        other.post("/register", data={"username": "prof2", "password": "longenough1", "csrf": csrf(other, "/register")})
        assert other.get(f"/session/{sid}").status_code == 404
        assert other.get(f"/session/{sid}/live.json").status_code == 404


def test_unique_constraint_in_db(client):
    sid = start_session(client)
    with la.app.app_context():
        db.session.add(Record(session_id=sid, reg_no="RA1", name="A"))
        db.session.commit()
        db.session.add(Record(session_id=sid, reg_no="RA1", name="A"))
        with pytest.raises(Exception):
            db.session.commit()
        db.session.rollback()
