"""改判（认领误选更正）端到端 + 并发测试（标准库）。

前置：服务已运行在 http://127.0.0.1:8181，数据目录为 /tmp/lfdata。
覆盖：正常改判、各类拒绝且不改状态、旧码失效/新码可见性、改判记录追溯、
访客不可见、二次改判、改判与改判/核码并发只生效一次。
"""
import http.cookiejar
import re
import secrets as _secrets
import sqlite3
import threading
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

BASE = "http://127.0.0.1:8181"
DB_PATH = "/tmp/lfdata/lostfound.db"
PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def client():
    cj = http.cookiejar.CookieJar()
    return cj, urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))


def req(op, method, path, form=None):
    data = urllib.parse.urlencode(form).encode() if form is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method)
    try:
        resp = op.open(r)
        return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def raw(op, method, path, form):
    """不跟随 303，返回 (status, body, location)。"""
    data = urllib.parse.urlencode(form).encode() if form is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method)
    opener = urllib.request.build_opener(
        NoRedirect, urllib.request.HTTPCookieProcessor(_jar_of(op))
    )
    try:
        resp = opener.open(r)
        return resp.status, resp.read().decode(), resp.headers.get("Location", "")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(), e.headers.get("Location", "")


def raw_cookie(cookie_header, path, form):
    """线程内手工带 Cookie 的无跳转 POST（不共享 CookieJar，线程安全）。"""
    data = urllib.parse.urlencode(form).encode()
    r = urllib.request.Request(BASE + path, data=data, method="POST")
    r.add_header("Cookie", cookie_header)
    try:
        resp = urllib.request.build_opener(NoRedirect).open(r)
        return resp.status, resp.headers.get("Location", "")
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Location", "")


def _jar_of(op):
    for h in op.handlers:
        if isinstance(h, urllib.request.HTTPCookieProcessor):
            return h.cookiejar
    return http.cookiejar.CookieJar()


def csrf(htmltext):
    m = re.search(r'name="csrf_token" value="([^"]+)"', htmltext)
    return m.group(1) if m else None


def submit_claim(item_id, answer, contact):
    _, v = client()
    st, body = req(v, "POST", f"/items/{item_id}/claims", form={"answer": answer, "contact": contact})
    assert st == 200 and "查询凭证" in body, st
    token = re.search(r'<div class="tokenbox">([^<]+)</div>', body).group(1).strip()
    return token


def query(token):
    _, v = client()
    st, body = req(v, "GET", f"/query?token={urllib.parse.quote(token)}")
    assert st == 200
    return body


jar, staff = client()
_, body = req(staff, "GET", "/staff/login")
req(staff, "POST", "/staff/login", form={"csrf_token": csrf(body), "password": "secret123"})
_, dash = req(staff, "GET", "/staff")
SCSRF = csrf(dash)
cookie_header = "; ".join(f"{c.name}={c.value}" for c in jar)

db = sqlite3.connect(DB_PATH)

# --------------------------------------------------------------- 准备物品
st, _, loc = raw(staff, "POST", "/staff/items",
                 form={"csrf_token": SCSRF, "public_description": "黑色皮夹一只",
                       "private_features": "改判私密-夹层名片"})
assert st == 303, (st, loc)
iid = int(loc.rsplit("/", 1)[1])

tX = submit_claim(iid, "原获选答案", "contact-X")
tY = submit_claim(iid, "陪跑答案", "contact-Y")
tZ = submit_claim(iid, "真正主人答案", "contact-Z")
ids = [r[0] for r in db.execute("SELECT id FROM claims WHERE item_id=? ORDER BY id", (iid,))]
# 只取本物品新插入的三条
cX, cY, cZ = ids[-3], ids[-2], ids[-1]

print("== 选定与改判入口 ==")
st, _, loc = raw(staff, "POST", f"/staff/claims/{cX}/select", form={"csrf_token": SCSRF})
check("选定原申请(303)", st == 303 and loc == f"/staff/claims/{cX}")
old_code = re.search(r'<div class="codebox">(\d{8})</div>', query(tX)).group(1)
check("原获选人取得交还码", bool(old_code))

st, body = req(staff, "GET", f"/staff/claims/{cX}")
check("已获选申请页提供改判表单", f'/staff/claims/{cX}/rejudge' in body and "认领误选改判" in body)
check("改判目标下拉仅列同物品未获选申请",
      f'value="{cY}"' in body and f'value="{cZ}"' in body and f'value="{cX}"' not in body)
st, body = req(staff, "GET", f"/staff/claims/{cZ}")
check("未获选申请页不提供改判表单", "/rejudge" not in body)

print("== 拒绝情形：不改动任何状态 ==")
st, _, _ = raw(staff, "POST", f"/staff/claims/{cX}/rejudge",
               form={"csrf_token": "bad", "to_claim_id": cZ, "reason": "x"})
check("伪造 CSRF 被拒(400)", st == 400)

st, _, loc = raw(staff, "POST", f"/staff/claims/{cX}/rejudge",
                 form={"csrf_token": SCSRF, "to_claim_id": cZ, "reason": "   "})
check("空理由被拒并跳回", st == 303 and loc == f"/staff/claims/{cX}")
st, body = req(staff, "GET", f"/staff/claims/{cX}")
check("空理由提示", "更正理由不能为空" in body)
check("空理由后原申请仍获选",
      db.execute("SELECT status FROM claims WHERE id=?", (cX,)).fetchone()[0] == "selected")

st, _, _ = raw(staff, "POST", f"/staff/claims/{cX}/rejudge",
               form={"csrf_token": SCSRF, "to_claim_id": cX, "reason": "自改"})
st, body = req(staff, "GET", f"/staff/claims/{cX}")
check("目标为自身被拒", "不能是原获选申请本身" in body)

# 同物品的待核对(pending)申请不能作为目标：直接构造一条
pending_hash = "pending-target-" + _secrets.token_hex(8)
db.execute(
    "INSERT INTO claims (item_id, answer, contact, token_hash, status, created_at)"
    " VALUES (?, '后到待核对', 'contact-P', ?, 'pending', '2026-09-01T00:00:00+00:00')",
    (iid, pending_hash))
db.commit()
cP = db.execute("SELECT id FROM claims WHERE token_hash=?", (pending_hash,)).fetchone()[0]
st, _, _ = raw(staff, "POST", f"/staff/claims/{cX}/rejudge",
               form={"csrf_token": SCSRF, "to_claim_id": cP, "reason": "改给待核对"})
st, body = req(staff, "GET", f"/staff/claims/{cX}")
check("待核对目标被拒（目标必须未获选）", "必须为未获选状态" in body)
check("下拉不含待核对申请", f'value="{cP}"' not in body)
check("拒绝后原申请仍获选",
      db.execute("SELECT status FROM claims WHERE id=?", (cX,)).fetchone()[0] == "selected")

# 跨物品目标
st, _, loc = raw(staff, "POST", "/staff/items",
                 form={"csrf_token": SCSRF, "public_description": "另一物品", "private_features": "p"})
other_iid = int(loc.rsplit("/", 1)[1])
submit_claim(other_iid, "别物品的申请", "contact-O")
cO = db.execute("SELECT id FROM claims WHERE item_id=? ORDER BY id", (other_iid,)).fetchone()[0]
st, _, _ = raw(staff, "POST", f"/staff/claims/{cX}/rejudge",
               form={"csrf_token": SCSRF, "to_claim_id": cO, "reason": "跨物品"})
st, body = req(staff, "GET", f"/staff/claims/{cX}")
check("非同一物品目标被拒", "不属于同一物品" in body)
check("跨物品拒绝后原申请仍获选",
      db.execute("SELECT status FROM claims WHERE id=?", (cX,)).fetchone()[0] == "selected")

st, _, _ = raw(staff, "POST", f"/staff/claims/{cX}/rejudge",
               form={"csrf_token": SCSRF, "to_claim_id": 99999999, "reason": "幽灵目标"})
check("目标不存在不产生 500", st in (303, 404), str(st))

print("== 正常改判 ==")
st, _, loc = raw(staff, "POST", f"/staff/claims/{cX}/rejudge",
                 form={"csrf_token": SCSRF, "to_claim_id": cZ, "reason": "核对监控确认误选"})
check("改判成功并跳转到新获选申请", st == 303 and loc == f"/staff/claims/{cZ}", f"{st} {loc}")
st, body = req(staff, "GET", loc)
check("跳转页提示改判完成", "已改判" in body)

rows = dict(db.execute("SELECT id,status FROM claims WHERE item_id=?", (iid,)).fetchall())
check("原申请变为未获选", rows[cX] == "rejected", str(rows))
check("目标申请变为已获选", rows[cZ] == "selected", str(rows))
check("其余申请仍未获选", rows[cY] == "rejected", str(rows))
check("物品仍恰有一个已获选",
      db.execute("SELECT COUNT(*) FROM claims WHERE item_id=? AND status='selected'", (iid,)).fetchone()[0] == 1)

n_active_old = db.execute(
    "SELECT COUNT(*) FROM handover_codes WHERE claim_id=? "
    "AND consumed_at IS NULL AND invalidated_at IS NULL", (cX,)).fetchone()[0]
check("原申请未核销交还码全部失效", n_active_old == 0)
st, body = req(staff, "GET", "/staff/verify")
st, body = req(staff, "POST", "/staff/verify", form={"csrf_token": csrf(body), "code": old_code})
check("旧码核码被拒", "已失效" in body)
check("物品未因旧码被交还",
      db.execute("SELECT status FROM items WHERE id=?", (iid,)).fetchone()[0] == "kept")

zq = query(tZ)
m = re.search(r'<div class="codebox">(\d{8})</div>', zq)
check("新获选人取得新一次性交还码", bool(m) and m.group(1) != old_code)
new_code = m.group(1) if m else ""
xq = query(tX)
check("原获选人查询页显示未获选且无交还码", "未获选" in xq and 'class="codebox"' not in xq)
check("原获选人看不到新码", new_code not in xq)
yq = query(tY)
check("其他申请人看不到新码", new_code not in yq and 'class="codebox"' not in yq)

# 工作人员可在双方申请页看到新码（仅工作人员与新获选人可见）
st, body = req(staff, "GET", f"/staff/claims/{cZ}")
check("工作人员可在新获选申请页看到新码", new_code in body)
st, body = req(staff, "GET", f"/staff/claims/{cX}")
check("工作人员在原申请页看不到该新码", new_code not in body)

print("== 更正记录追溯（仅工作人员） ==")
cr = db.execute("SELECT from_claim_id,to_claim_id,reason FROM corrections WHERE item_id=?",
                (iid,)).fetchall()
check("记录保存申请双方与理由",
      len(cr) == 1 and tuple(cr[0]) == (cX, cZ, "核对监控确认误选") and cr[0] is not None,
      str(cr))
check("记录含时间",
      db.execute("SELECT created_at FROM corrections WHERE item_id=?", (iid,)).fetchone()[0] is not None)
st, body = req(staff, "GET", f"/staff/items/{iid}")
check("工作人员物品页展示改判记录",
      "认领改判记录" in body and f"#{cX}" in body and f"#{cZ}" in body and "核对监控确认误选" in body)
_, v = client()
st, body = req(v, "GET", f"/items/{iid}")
check("访客物品页不显示改判记录与理由",
      "认领改判记录" not in body and "更正理由" not in body and "核对监控" not in body)
st, _ = req(v, "GET", "/staff/items/%d" % iid)
check("访客无法查看工作人员物品页(403)", st == 403)

print("== 二次改判（新获选人也可再改回） ==")
st, _, loc = raw(staff, "POST", f"/staff/claims/{cZ}/rejudge",
                 form={"csrf_token": SCSRF, "to_claim_id": cX, "reason": "再次核对，改回"})
check("二次改判成功", st == 303 and loc == f"/staff/claims/{cX}")
rows = dict(db.execute("SELECT id,status FROM claims WHERE item_id=?", (iid,)).fetchall())
check("二次改判后原申请恢复获选、目标变为未获选",
      rows[cX] == "selected" and rows[cZ] == "rejected", str(rows))
n_active_z = db.execute(
    "SELECT COUNT(*) FROM handover_codes WHERE claim_id=? "
    "AND consumed_at IS NULL AND invalidated_at IS NULL", (cZ,)).fetchone()[0]
check("上一获选人的新码随之失效", n_active_z == 0)
check("两条改判记录均保留",
      db.execute("SELECT COUNT(*) FROM corrections WHERE item_id=?", (iid,)).fetchone()[0] == 2)
# 先把状态改回 X 获选 -> Z，用于下面的并发：先并发到 Y/Z 中选一个
# 当前 cX selected。

print("== 改判并发：只允许一次状态变更 ==")
barrier = threading.Barrier(2)


def hit(tgt):
    barrier.wait()
    return raw_cookie(cookie_header, f"/staff/claims/{cX}/rejudge",
                      {"csrf_token": SCSRF, "to_claim_id": tgt, "reason": "并发改判"})


with ThreadPoolExecutor(max_workers=2) as ex:
    fs = [ex.submit(hit, cY), ex.submit(hit, cZ)]
    r1, r2 = [f.result() for f in fs]
rows = dict(db.execute("SELECT id,status FROM claims WHERE item_id=?", (iid,)).fetchall())
nsel = sum(1 for s in rows.values() if s == "selected")
check("两次并发改判后仍恰有一个获选且原申请已落选",
      nsel == 1 and rows[cX] == "rejected", f"{r1} {r2} {rows}")
active = db.execute(
    "SELECT hc.claim_id FROM handover_codes hc JOIN claims c ON c.id=hc.claim_id "
    "WHERE c.item_id=? AND hc.consumed_at IS NULL AND hc.invalidated_at IS NULL",
    (iid,)).fetchall()
check("并发改判只签发一枚有效新码", len(active) == 1, str(active))

print("== 已交还 / 已撤下拒绝改判 ==")
# 已交还物品
st, _, loc = raw(staff, "POST", "/staff/items",
                 form={"csrf_token": SCSRF, "public_description": "待交还物品", "private_features": "q"})
rid = int(loc.rsplit("/", 1)[1])
ta = submit_claim(rid, "甲答案", "ca")
tb = submit_claim(rid, "乙答案", "cb")
ra, rb = [r[0] for r in db.execute("SELECT id FROM claims WHERE item_id=? ORDER BY id", (rid,))][-2:]
raw(staff, "POST", f"/staff/claims/{ra}/select", form={"csrf_token": SCSRF})
code = re.search(r'codebox">(\d{8})', query(ta)).group(1)
st, body = req(staff, "GET", "/staff/verify")
st, _, loc = raw(staff, "POST", "/staff/verify", form={"csrf_token": csrf(body), "code": code})
check("核码交还成功", st == 303 and loc == f"/staff/items/{rid}")
st, _, _ = raw(staff, "POST", f"/staff/claims/{ra}/rejudge",
               form={"csrf_token": SCSRF, "to_claim_id": rb, "reason": "交还后改判"})
st, body = req(staff, "GET", f"/staff/claims/{ra}")
check("已交还物品拒绝改判", "不能改判" in body)
check("已交还页面无改判表单", "/rejudge" not in body)
check("已交还后状态不变",
      db.execute("SELECT status FROM claims WHERE id=?", (ra,)).fetchone()[0] == "selected")

# 已撤下物品
st, _, loc = raw(staff, "POST", "/staff/items",
                 form={"csrf_token": SCSRF, "public_description": "待撤下物品", "private_features": "r"})
mid = int(loc.rsplit("/", 1)[1])
submit_claim(mid, "丙答案", "cc")
submit_claim(mid, "丁答案", "cd")
ma, mb = [r[0] for r in db.execute("SELECT id FROM claims WHERE item_id=? ORDER BY id", (mid,))][-2:]
raw(staff, "POST", f"/staff/claims/{ma}/select", form={"csrf_token": SCSRF})
raw(staff, "POST", f"/staff/items/{mid}/remove", form={"csrf_token": SCSRF})
st, _, _ = raw(staff, "POST", f"/staff/claims/{ma}/rejudge",
               form={"csrf_token": SCSRF, "to_claim_id": mb, "reason": "撤下后改判"})
st, body = req(staff, "GET", f"/staff/claims/{ma}")
check("已撤下物品拒绝改判", "不能改判" in body)
check("撤下后状态不变",
      db.execute("SELECT status FROM claims WHERE id=?", (ma,)).fetchone()[0] == "selected"
      and db.execute("SELECT status FROM items WHERE id=?", (mid,)).fetchone()[0] == "removed")

print(f"\n结果：{PASS} 通过, {FAIL} 失败")
raise SystemExit(1 if FAIL else 0)
