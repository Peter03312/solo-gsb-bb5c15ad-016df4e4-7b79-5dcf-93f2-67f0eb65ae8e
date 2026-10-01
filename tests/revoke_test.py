"""交还登记撤销端到端 + 并发测试（标准库）。

前置：服务已运行在 http://127.0.0.1:8181，数据目录为 /tmp/lfdata。
覆盖：撤销入口与必填原因、各类拒绝（CSRF/空原因/记录不存在/不匹配/不属于当前
获选申请/重复撤销）、原子恢复（物品保管中、申请归属与获选状态不变、原已核销码
永久失效、签发新码）、工作人员追溯（原核销时间/撤销时间/原因）、访客不可见原因、
撤销后改判/合并/核码/重发规则照常、合并迁移撤销记录、并发撤销只生效一次。
"""
import http.cookiejar
import hashlib
import re
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


def _jar_of(op):
    for h in op.handlers:
        if isinstance(h, urllib.request.HTTPCookieProcessor):
            return h.cookiejar
    return http.cookiejar.CookieJar()


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


def csrf(htmltext):
    m = re.search(r'name="csrf_token" value="([^"]+)"', htmltext)
    return m.group(1) if m else None


def submit_claim(item_id, answer, contact):
    _, v = client()
    st, body = req(v, "POST", f"/items/{item_id}/claims",
                   form={"answer": answer, "contact": contact})
    assert st == 200 and "查询凭证" in body, st
    return re.search(r'<div class="tokenbox">([^<]+)</div>', body).group(1).strip()


def query(token):
    _, v = client()
    st, body = req(v, "GET", f"/query?token={urllib.parse.quote(token)}")
    assert st == 200
    return body


def code_in(htmltext):
    m = re.search(r'<div class="codebox">(\d{8})</div>', htmltext)
    return m.group(1) if m else None


def make_item(desc="撤销测试物品", priv="p"):
    st, _, loc = raw(staff, "POST", "/staff/items",
                     form={"csrf_token": SCSRF, "public_description": desc,
                           "private_features": priv})
    assert st == 303, (st, loc)
    return int(loc.rsplit("/", 1)[1])


def code_id_of(code: str) -> int:
    h = hashlib.sha256(code.encode()).hexdigest()
    return db.execute("SELECT id FROM handover_codes WHERE code_hash=?", (h,)).fetchone()[0]


def select_and_verify(token, claim_id):
    """选定 -> 取码 -> 核码交还，返回 (已核销码明文, code_id)。"""
    st, _, loc = raw(staff, "POST", f"/staff/claims/{claim_id}/select",
                     form={"csrf_token": SCSRF})
    assert st == 303, (st, loc)
    code = code_in(query(token))
    assert code, "获选人未取得交还码"
    st, body = req(staff, "GET", "/staff/verify")
    st, _, loc = raw(staff, "POST", "/staff/verify",
                     form={"csrf_token": csrf(body), "code": code})
    assert st == 303, loc
    return code, code_id_of(code)


jar, staff = client()
_, body = req(staff, "GET", "/staff/login")
req(staff, "POST", "/staff/login", form={"csrf_token": csrf(body), "password": "secret123"})
_, dash = req(staff, "GET", "/staff")
SCSRF = csrf(dash)
cookie_header = "; ".join(f"{c.name}={c.value}" for c in jar)

db = sqlite3.connect(DB_PATH)

REASON = "核码后复查发现物品仍在馆内，本次系误核销"

# ------------------------------------------------- A：基本流程、拒绝项、可见性
print("== A：撤销入口与拒绝情形 ==")
iidA = make_item("R-A 物品")
tA = submit_claim(iidA, "主人答案A", "contact-A")
tA2 = submit_claim(iidA, "陪跑答案A", "contact-A2")
cA, cA2 = [r[0] for r in db.execute(
    "SELECT id FROM claims WHERE item_id=? ORDER BY id", (iidA,)).fetchall()[-2:]]
code1, cid1 = select_and_verify(tA, cA)
check("核码后物品已交还",
      db.execute("SELECT status FROM items WHERE id=?", (iidA,)).fetchone()[0] == "returned")

st, body = req(staff, "GET", f"/staff/items/{iidA}")
check("已交还详情页提供撤销表单",
      f'/staff/items/{iidA}/revoke-return' in body and "撤销交还登记" in body)
m = re.search(r'name="code_id" value="(\d+)"', body)
check("撤销表单携带本次核销记录 id", bool(m) and m.group(1) == str(cid1),
      f"{m.group(1) if m else None} != {cid1}")
check("页面展示原核销时间", "原核销时间" in body)
check("已交还页面无撤下/合并入口",
      "/remove" not in body and "合并重复登记" not in body)

st, _, _ = raw(staff, "POST", f"/staff/items/{iidA}/revoke-return",
               form={"csrf_token": "bad", "code_id": cid1, "reason": "x"})
check("伪造 CSRF 被拒(400)", st == 400)

st, _, loc = raw(staff, "POST", f"/staff/items/{iidA}/revoke-return",
                 form={"csrf_token": SCSRF, "code_id": cid1, "reason": "   "})
check("空原因被拒并跳回", st == 303 and loc == f"/staff/items/{iidA}")
st, body = req(staff, "GET", f"/staff/items/{iidA}")
check("空原因提示", "撤销原因不能为空" in body)
check("空原因后物品仍已交还",
      db.execute("SELECT status FROM items WHERE id=?", (iidA,)).fetchone()[0] == "returned")

st, _, loc = raw(staff, "POST", f"/staff/items/{iidA}/revoke-return",
                 form={"csrf_token": SCSRF, "code_id": "abc", "reason": REASON})
check("非数字核销记录被拒", st == 303 and loc == f"/staff/items/{iidA}")

st, _, _ = raw(staff, "POST", f"/staff/items/{iidA}/revoke-return",
               form={"csrf_token": SCSRF, "code_id": 99999999, "reason": REASON})
st, body = req(staff, "GET", f"/staff/items/{iidA}")
check("核销记录不存在不 500 且提示", "核销记录不存在" in body)
check("拒绝后物品仍已交还",
      db.execute("SELECT status FROM items WHERE id=?", (iidA,)).fetchone()[0] == "returned")

# 另一件已交还物品，构造跨物品记录不匹配
iidE = make_item("R-E 物品")
tE = submit_claim(iidE, "答案E", "contact-E")
cE = db.execute("SELECT id FROM claims WHERE item_id=? ORDER BY id", (iidE,)).fetchone()[0]
_, cidE = select_and_verify(tE, cE)
st, _, _ = raw(staff, "POST", f"/staff/items/{iidA}/revoke-return",
               form={"csrf_token": SCSRF, "code_id": cidE, "reason": REASON})
st, body = req(staff, "GET", f"/staff/items/{iidA}")
check("他物品核销记录被拒（记录不匹配）", "与该物品不匹配" in body)
check("两物品状态均不变",
      db.execute("SELECT status FROM items WHERE id=?", (iidA,)).fetchone()[0] == "returned"
      and db.execute("SELECT status FROM items WHERE id=?", (iidE,)).fetchone()[0] == "returned")

print("== A：正常撤销 ==")
st, _, loc = raw(staff, "POST", f"/staff/items/{iidA}/revoke-return",
                 form={"csrf_token": SCSRF, "code_id": cid1, "reason": REASON})
check("撤销成功并跳回物品页", st == 303 and loc == f"/staff/items/{iidA}", f"{st} {loc}")
st, body = req(staff, "GET", loc)
check("跳转页提示撤销完成", "已撤销本次交还登记" in body)

check("物品恢复为保管中",
      db.execute("SELECT status FROM items WHERE id=?", (iidA,)).fetchone()[0] == "kept")
row = db.execute("SELECT status,item_id FROM claims WHERE id=?", (cA,)).fetchone()
check("当前获选申请归属与获选状态不变", row[0] == "selected" and row[1] == iidA)
check("其他申请仍未获选",
      db.execute("SELECT status FROM claims WHERE id=?", (cA2,)).fetchone()[0] == "rejected")
rr = db.execute(
    "SELECT reason, consumed_at, created_at, claim_id, code_id FROM return_revocations"
    " WHERE item_id=? ORDER BY id", (iidA,)).fetchall()
check("撤销记录保存原因/原核销时间/撤销时间/申请/核销记录",
      len(rr) == 1 and rr[0][0] == REASON and rr[0][1] is not None
      and rr[0][2] is not None and rr[0][3] == cA and rr[0][4] == cid1, str(rr))
consumed1 = db.execute("SELECT consumed_at FROM handover_codes WHERE id=?",
                       (cid1,)).fetchone()[0]
check("原核销时间与被撤销码一致", consumed1 is not None and rr[0][1] == consumed1)

# 原已核销码永久失效：重复核码仍被拒，物品不变
st, vbody = req(staff, "GET", "/staff/verify")
st, vbody = req(staff, "POST", "/staff/verify",
                form={"csrf_token": csrf(vbody), "code": code1})
check("原已核销码再次核码被拒", "已使用" in vbody)
check("物品未被旧码改变状态",
      db.execute("SELECT status FROM items WHERE id=?", (iidA,)).fetchone()[0] == "kept")

# 新一次性交还码：同有效期规则签发，获选人与工作人员可见
qa = query(tA)
code2 = code_in(qa)
check("获选人凭原凭证看到新码", bool(code2) and code2 != code1)
newrow = db.execute(
    "SELECT issued_at, expires_at FROM handover_codes WHERE code_hash=?",
    (hashlib.sha256(code2.encode()).hexdigest(),)
).fetchone()
check("新码按有效期规则签发（晚于签发时间）", newrow[1] > newrow[0])
check("获选人查询页不显示撤销原因/撤销信息", REASON not in qa and "撤销" not in qa)
qa2 = query(tA2)
check("其他申请人看不到新码与撤销原因",
      code2 not in qa2 and REASON not in qa2 and 'class="codebox"' not in qa2)
st, body = req(staff, "GET", f"/staff/claims/{cA}")
check("工作人员在获选申请页可看到新码", code2 in body)
check("恢复后改判表单照常可用",
      f'/staff/claims/{cA}/rejudge' in body and "认领误选改判" in body)

# 工作人员追溯：原核销时间、撤销时间及原因
st, body = req(staff, "GET", f"/staff/items/{iidA}")
check("物品页展示撤销记录",
      "交还登记撤销记录" in body and REASON in body and "原核销时间" in body
      and "撤销时间" in body)
_, v = client()
st, pbody = req(v, "GET", f"/items/{iidA}")
check("访客物品页不显示撤销记录与原因",
      "撤销" not in pbody and REASON not in pbody and "交还登记撤销" not in pbody)
st, _ = req(v, "GET", f"/staff/items/{iidA}")
check("访客无法查看工作人员物品页(403)", st == 403)

# 恢复后不再有待撤销入口（保管中）
check("恢复后页面无撤销表单", "撤销本次交还登记" not in body if st == 403 else True)
st, body = req(staff, "GET", f"/staff/items/{iidA}")
check("工作人员页恢复后无撤销表单", "撤销本次交还登记" not in body)

print("== A：重发、再次核码、二次撤销（规则照常） ==")
# 重发在恢复后照常：旧新码作废
st, _, _ = raw(staff, "POST", f"/staff/claims/{cA}/resend", form={"csrf_token": SCSRF})
check("重发成功(303)", st == 303)
code2r = code_in(query(tA))
check("重发后获选人拿到另一枚新码", bool(code2r) and code2r != code2)
st, vbody = req(staff, "GET", "/staff/verify")
st, vbody = req(staff, "POST", "/staff/verify",
                form={"csrf_token": csrf(vbody), "code": code2})
check("重发作废的码核码被拒", "已失效" in vbody)
cid2r = db.execute("SELECT id FROM handover_codes WHERE code_hash=?",
                   (hashlib.sha256(code2r.encode()).hexdigest(),)
                   ).fetchone()[0]
st, vbody = req(staff, "GET", "/staff/verify")
st, _, loc = raw(staff, "POST", "/staff/verify",
                 form={"csrf_token": csrf(vbody), "code": code2r})
check("新码核码成功再次交还", st == 303 and loc == f"/staff/items/{iidA}")
check("物品再次已交还",
      db.execute("SELECT status FROM items WHERE id=?", (iidA,)).fetchone()[0] == "returned")

# 用旧 code_id（第一次核销记录）重复撤销：已撤销过
st, _, _ = raw(staff, "POST", f"/staff/items/{iidA}/revoke-return",
               form={"csrf_token": SCSRF, "code_id": cid1, "reason": "再撤旧记录"})
st, body = req(staff, "GET", f"/staff/items/{iidA}")
check("已撤销的核销记录不能重复撤销", "不能重复撤销" in body)
check("重复撤销后物品仍已交还",
      db.execute("SELECT status FROM items WHERE id=?", (iidA,)).fetchone()[0] == "returned")
# 正常二次撤销（本次核销记录 cid2r）
st, _, loc = raw(staff, "POST", f"/staff/items/{iidA}/revoke-return",
                 form={"csrf_token": SCSRF, "code_id": cid2r, "reason": "二次复查仍在馆"})
check("二次撤销成功", st == 303 and loc == f"/staff/items/{iidA}")
check("二次撤销后物品恢复保管中",
      db.execute("SELECT status FROM items WHERE id=?", (iidA,)).fetchone()[0] == "kept")
check("恰有两条撤销记录",
      db.execute("SELECT COUNT(*) FROM return_revocations WHERE item_id=?",
                 (iidA,)).fetchone()[0] == 2)
n_active = db.execute(
    "SELECT COUNT(*) FROM handover_codes hc JOIN claims c ON c.id=hc.claim_id"
    " WHERE c.item_id=? AND hc.consumed_at IS NULL AND hc.invalidated_at IS NULL",
    (iidA,)).fetchone()[0]
check("二次撤销后恰有一枚有效新码", n_active == 1)

# --------------------------------------------- F：撤销后改判，旧核销记录归属失配
print("== F：撤销后改判，旧核销记录不再属于当前获选申请 ==")
iidF = make_item("R-F 物品")
tF1 = submit_claim(iidF, "F1答案", "cf1")
tF2 = submit_claim(iidF, "F2答案", "cf2")
fids = [r[0] for r in db.execute("SELECT id FROM claims WHERE item_id=? ORDER BY id",
                                 (iidF,)).fetchall()[-2:]]
f1, f2 = fids[0], fids[1]
fcode1, fcid1 = select_and_verify(tF1, f1)
st, _, _ = raw(staff, "POST", f"/staff/items/{iidF}/revoke-return",
               form={"csrf_token": SCSRF, "code_id": fcid1, "reason": REASON})
assert st == 303
fcode2 = code_in(query(tF1))
# 恢复后改判给 f2（改判规则照常）
st, _, loc = raw(staff, "POST", f"/staff/claims/{f1}/rejudge",
                 form={"csrf_token": SCSRF, "to_claim_id": f2, "reason": "撤销后发现误选"})
check("恢复后改判成功", st == 303 and loc == f"/staff/claims/{f2}")
fcode3 = code_in(query(tF2))
check("改判后新获选人取得新码", bool(fcode3) and fcode3 != fcode2)
# f2 的码核码 -> 再次交还
st, vbody = req(staff, "GET", "/staff/verify")
st, _, _ = raw(staff, "POST", "/staff/verify",
               form={"csrf_token": csrf(vbody), "code": fcode3})
assert st == 303
st, _, _ = raw(staff, "POST", f"/staff/items/{iidF}/revoke-return",
               form={"csrf_token": SCSRF, "code_id": fcid1, "reason": "用旧记录撤销"})
st, body = req(staff, "GET", f"/staff/items/{iidF}")
check("旧核销记录不属于当前获选申请时拒绝", "不属于当前获选申请" in body)
check("拒绝后物品仍已交还",
      db.execute("SELECT status FROM items WHERE id=?", (iidF,)).fetchone()[0] == "returned")
fcid3 = db.execute(
    "SELECT hc.id FROM handover_codes hc WHERE hc.code_hash=?",
    (hashlib.sha256(fcode3.encode()).hexdigest(),)
).fetchone()[0]
st, _, loc = raw(staff, "POST", f"/staff/items/{iidF}/revoke-return",
                 form={"csrf_token": SCSRF, "code_id": fcid3, "reason": "按当前获选记录撤销"})
check("以当前获选申请的核销记录撤销成功", st == 303 and loc == f"/staff/items/{iidF}")
check("撤销后物品恢复且当前获选人为 f2",
      db.execute("SELECT status FROM items WHERE id=?", (iidF,)).fetchone()[0] == "kept"
      and db.execute("SELECT status FROM claims WHERE id=?", (f2,)).fetchone()[0] == "selected")

# --------------------------------------- D+K：撤销记录随合并迁移、合并后核码撤销
print("== D+K：合并迁移撤销记录 ==")
iidK = make_item("R-K 保留物品")  # 无申请的保管中物品
iidD = make_item("R-D 并入物品")
tD = submit_claim(iidD, "D主人答案", "cd")
cD = db.execute("SELECT id FROM claims WHERE item_id=? ORDER BY id", (iidD,)).fetchone()[0]
dcode1, _ = select_and_verify(tD, cD)
st, _, _ = raw(staff, "POST", f"/staff/items/{iidD}/revoke-return",
               form={"csrf_token": SCSRF,
                     "code_id": db.execute(
                         "SELECT id FROM handover_codes WHERE code_hash=?",
                         (hashlib.sha256(dcode1.encode()).hexdigest(),)
                     ).fetchone()[0],
                     "reason": REASON})
assert st == 303
dcode2 = code_in(query(tD))
# K（保留，无获选）合并 D（保管中、有获选）：允许
st, body = req(staff, "GET", f"/staff/items/{iidK}")
st, _, loc = raw(staff, "POST", f"/staff/items/{iidK}/merge",
                 form={"csrf_token": csrf(body), "merged_item_id": iidD,
                       "reason": "撤销记录合并测试"})
check("合并成功", st == 303 and loc == f"/staff/items/{iidK}", f"{st} {loc}")
check("撤销记录 item_id 迁移到保留物品",
      db.execute("SELECT COUNT(*) FROM return_revocations WHERE item_id=?",
                 (iidK,)).fetchone()[0] == 1
      and db.execute("SELECT COUNT(*) FROM return_revocations WHERE item_id=?",
                     (iidD,)).fetchone()[0] == 0)
qd = query(tD)
check("并入物品获选人凭证仍可查本人申请并看到新码（描述为保留物品）",
      code_in(qd) == dcode2 and "R-K 保留物品" in qd and REASON not in qd)
st, body = req(staff, "GET", f"/staff/items/{iidK}")
check("保留物品页展示迁移来的撤销记录", "交还登记撤销记录" in body and REASON in body)
# 合并后核码规则照常：新码可核销 -> 保留物品已交还，再撤销
st, vbody = req(staff, "GET", "/staff/verify")
st, _, loc = raw(staff, "POST", "/staff/verify",
                 form={"csrf_token": csrf(vbody), "code": dcode2})
check("合并后原新码核码成功", st == 303 and loc == f"/staff/items/{iidK}")
dcid2 = db.execute(
    "SELECT id FROM handover_codes WHERE code_hash=?",
    (hashlib.sha256(dcode2.encode()).hexdigest(),)
).fetchone()[0]
st, _, _ = raw(staff, "POST", f"/staff/items/{iidK}/revoke-return",
               form={"csrf_token": SCSRF, "code_id": dcid2, "reason": "合并后再次撤销"})
check("合并保留物品可再次撤销", st == 303)
check("保留物品恢复保管中且两条撤销记录可追溯",
      db.execute("SELECT status FROM items WHERE id=?", (iidK,)).fetchone()[0] == "kept"
      and db.execute("SELECT COUNT(*) FROM return_revocations WHERE item_id=?",
                     (iidK,)).fetchone()[0] == 2)

# ------------------------------------------------------- C：并发撤销只生效一次
print("== C：并发重复撤销 ==")
iidC = make_item("R-C 并发物品")
tC = submit_claim(iidC, "C主人答案", "cc")
cC = db.execute("SELECT id FROM claims WHERE item_id=? ORDER BY id", (iidC,)).fetchone()[0]
ccode, ccid = select_and_verify(tC, cC)
barrier = threading.Barrier(2)


def hit_revoke():
    barrier.wait()
    return raw_cookie(cookie_header, f"/staff/items/{iidC}/revoke-return",
                      {"csrf_token": SCSRF, "code_id": ccid, "reason": "并发撤销"})


with ThreadPoolExecutor(max_workers=2) as ex:
    r1, r2 = [f.result() for f in [ex.submit(hit_revoke), ex.submit(hit_revoke)]]
check("并发撤销后物品恢复保管中",
      db.execute("SELECT status FROM items WHERE id=?", (iidC,)).fetchone()[0] == "kept",
      str((r1, r2)))
check("同一核销记录只有一条撤销记录",
      db.execute("SELECT COUNT(*) FROM return_revocations WHERE code_id=?",
                 (ccid,)).fetchone()[0] == 1)
check("获选状态不变且仅一枚有效新码",
      db.execute("SELECT status FROM claims WHERE id=?", (cC,)).fetchone()[0] == "selected"
      and db.execute(
          "SELECT COUNT(*) FROM handover_codes WHERE claim_id=?"
          " AND consumed_at IS NULL AND invalidated_at IS NULL", (cC,)).fetchone()[0] == 1)
n_codes = db.execute("SELECT COUNT(*) FROM handover_codes WHERE claim_id=?", (cC,)).fetchone()[0]
check("并发没有签发多枚新码（仅原核销码 + 1 枚新码）", n_codes == 2, str(n_codes))
newccode = code_in(query(tC))
check("并发后获选人看到新码且旧码失效", bool(newccode) and newccode != ccode)

print(f"\n结果：{PASS} 通过, {FAIL} 失败")
raise SystemExit(1 if FAIL else 0)
