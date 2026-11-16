"""End-to-end smoke test against the LIVE deployment (run by Jenkins after Ansible).

Plays both roles: faculty logs in and opens a session, a 'student phone' scans the QR URL and marks attendance,
faculty sees the student appear live. Uses a throw-away course that is deleted at the end.
Usage: smoke_test.py <base_url> <file-with-admin-password>
"""
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar

base = sys.argv[1].rstrip("/")
password = open(sys.argv[2]).read().strip()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **kw):
        return None


def browser():
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()), NoRedirect)


def call(b, path, data=None):
    body = urllib.parse.urlencode(data).encode() if data is not None else None
    try:
        r = b.open(urllib.request.Request(base + path, data=body), timeout=15)
    except urllib.error.HTTPError as e:
        r = e
    return r.status if hasattr(r, "status") else r.code, r.read().decode()


def csrf(b, path):
    return re.search(r'name="csrf" value="([0-9a-f]+)"', call(b, path)[1]).group(1)


def check(label, cond):
    print(("  PASS  " if cond else "  FAIL  ") + label)
    if not cond:
        sys.exit(1)


faculty, phone = browser(), browser()
status, _ = call(faculty, "/login", {"username": "admin", "password": password, "csrf": csrf(faculty, "/login")})
check("faculty login", status == 302)

call(faculty, "/course/new", {"code": "SMOKE", "name": "Jenkins smoke test", "csrf": csrf(faculty, "/dashboard")})
dash = call(faculty, "/dashboard")[1]
cid = max(int(x) for x in re.findall(r"/course/(\d+)", dash))
status, _ = call(faculty, f"/course/{cid}/start", {"title": "smoke", "csrf": csrf(faculty, "/dashboard")})
check("session started", status == 302)
sid = max(int(x) for x in re.findall(r"/session/(\d+)", call(faculty, f"/course/{cid}")[1]))

qr = json.loads(call(faculty, f"/session/{sid}/qr.json")[1])
check("rotating QR served", qr["open"] and "<svg" in qr["svg"])
path = "/mark?" + urllib.parse.urlparse(qr["url"]).query
status, page = call(phone, path)
check("student scan shows the form", status == 200 and "Mark me present" in page)
status, page = call(phone, "/mark", {"s": sid, "t": urllib.parse.parse_qs(urllib.parse.urlparse(qr["url"]).query)["t"][0],
                                     "reg_no": "SMOKE00001", "name": "Smoke Test", "csrf": csrf(phone, path)})
check("attendance marked", status == 200 and "marked present" in page)
live = json.loads(call(faculty, f"/session/{sid}/live.json")[1])
check("faculty sees the student live", live["count"] == 1 and live["records"][0]["reg_no"] == "SMOKE00001")
status, _ = call(phone, "/mark?s=%d&t=1.forged" % sid)
check("forged QR token rejected", status == 410)

call(faculty, f"/session/{sid}/end", {"csrf": csrf(faculty, f"/session/{sid}/report")})
call(faculty, f"/course/{cid}/delete", {"csrf": csrf(faculty, f"/course/{cid}")})
print("SMOKE TEST PASSED")
