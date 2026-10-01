"""盘点「暂未找到 / 暂缓交还」端到端 + 并发测试（标准库）。

前置：服务已运行在 http://127.0.0.1:8181，数据目录为 /tmp/lfdata。
覆盖：登记入口与必填原因、访客凭证「暂缓交还」提示且不展示交还码、核码拒绝且
不消耗原码、原资格/申请/码有效期不变、复核解除（逐条）、未过期原码恢复可见可
核销、过期码按重发规则处理、原因仅工作人员可见、撤下再恢复不清除、撤下期间解除、
改判期间状态延续、合并迁移记录且保留物品继续暂停、已撤下/已交还/已并入拒绝登记、
重复登记/解除、记录与物品不匹配、并发登记/解除/核码只生效一次。
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
REASON = "B 区储物柜盘点未见实物，已查监控待定位"
NOTE = "已在相邻柜格找到实物并核对无误"
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


def make_item(desc="暂未找到测试物品", priv="p"):
    st, _, loc = raw(staff, "POST", "/staff/items",
                     form={"csrf_token": SCSRF, "public_description": desc,
                           "private_features": priv})
    assert st == 303, (st, loc)
    return int(loc.rsplit("/", 1)[1])


def claim_ids(item_id):
    return [r[0] for r in db.execute(
        "SELECT id FROM claims WHERE item_id=? ORDER BY id", (item_id,)).fetchall()]


def code_id_of(code):
    h = hashlib.sha256(code.encode()).hexdigest()
    return db.execute("SELECT id FROM handover_codes WHERE code_hash=?", (h,)).fetchone()[0]


def select_claim(claim_id):
    st, _, loc = raw(staff, "POST", f"/staff/claims/{claim_id}/select",
                     form={"csrf_token": SCSRF})
    assert st == 303, (st, loc)


def verify_code(code):
    st, vbody = req(staff, "GET", "/staff/verify")
    # 不跟随 303：成功核销为 303 跳转；失败为 200 重渲染核码表单
    st, body, loc = raw(staff, "POST", "/staff/verify",
                        form={"csrf_token": csrf(vbody), "code": code})
    return st, body, loc


def staff_code(claim_id):
    """工作人员在申请详情页始终可见交还码明文（暂缓期间访客凭证不显示）。"""
    st, body = req(staff, "GET", f"/staff/claims/{claim_id}")
    assert st == 200
    m = re.search(r'<div class="codebox">(\d{8})</div>', body)
    return m.group(1) if m else None


def nf_register(item_id, reason=REASON):
    return raw(staff, "POST", f"/staff/items/{item_id}/not-found",
               form={"csrf_token": SCSRF, "reason": reason})


def nf_resolve(item_id, rid, note="已找到实物并核对无误"):
    return raw(staff, "POST", f"/staff/items/{item_id}/not-found/{rid}/resolve",
               form={"csrf_token": SCSRF, "found_note": note})


def nf_pending_id(item_id):
    row = db.execute(
        "SELECT id FROM item_not_found WHERE item_id=? AND resolved_at IS NULL"
        " ORDER BY id DESC LIMIT 1", (item_id,)).fetchone()
    return row[0] if row else None


jar, staff = client()
_, body = req(staff, "GET", "/staff/login")
req(staff, "POST", "/staff/login", form={"csrf_token": csrf(body), "password": "secret123"})
_, dash = req(staff, "GET", "/staff")
SCSRF = csrf(dash)
cookie_header = "; ".join(f"{c.name}={c.value}" for c in jar)

db = sqlite3.connect(DB_PATH)

# ============================================ A：基本登记 / 暂缓 / 解除
print("== A：登记暂缓、访客提示、核码拒绝、解除恢复 ==")
iidA = make_item("N-A 物品")
tA = submit_claim(iidA, "主人答案A", "contact-A")
tA2 = submit_claim(iidA, "陪跑答案A", "contact-A2")
cA, cA2 = claim_ids(iidA)[-2:]
select_claim(cA)
codeA = code_in(query(tA))
check("登记前获选人可见交还码", bool(codeA))
codeA_expiry = db.execute("SELECT expires_at FROM handover_codes WHERE code_hash=?",
                          (hashlib.sha256(codeA.encode()).hexdigest(),)).fetchone()[0]

st, body = req(staff, "GET", f"/staff/items/{iidA}")
check("保管中详情页提供暂未找到登记表单",
      f'/staff/items/{iidA}/not-found' in body and "盘点暂未找到" in body)

# CSRF / 空原因
st, _, _ = raw(staff, "POST", f"/staff/items/{iidA}/not-found",
               form={"csrf_token": "bad", "reason": "x"})
check("登记伪造 CSRF 被拒(400)", st == 400)
st, _, loc = nf_register(iidA, "   ")
check("登记空原因被拒并跳回", st == 303 and loc == f"/staff/items/{iidA}")
st, body = req(staff, "GET", f"/staff/items/{iidA}")
check("空原因提示", "未找到原因不能为空" in body)
check("空原因后未写入记录", nf_pending_id(iidA) is None)

# 正常登记
st, _, loc = nf_register(iidA)
check("登记成功并跳回物品页", st == 303 and loc == f"/staff/items/{iidA}", f"{st} {loc}")
ridA = nf_pending_id(iidA)
check("写入未解除记录", ridA is not None)
st, body = req(staff, "GET", loc)
check("跳转页提示暂缓交还", "暂缓交还" in body)

# 物品状态仍 kept，资格/申请/码有效期不变
check("登记不改物品状态（仍 kept）",
      db.execute("SELECT status FROM items WHERE id=?", (iidA,)).fetchone()[0] == "kept")
check("获选资格不变",
      db.execute("SELECT status FROM claims WHERE id=?", (cA,)).fetchone()[0] == "selected"
      and db.execute("SELECT status FROM claims WHERE id=?", (cA2,)).fetchone()[0] == "rejected")
check("原码仍在且未被消耗/作废/改期",
      db.execute(
          "SELECT consumed_at, invalidated_at, expires_at FROM handover_codes WHERE id=?",
          (code_id_of(codeA),)).fetchone()
      == (None, None, codeA_expiry))

# 访客凭证：暂缓提示、不展示码、看不到原因
qa = query(tA)
check("获选人看到暂缓交还提示", "交还暂缓" in qa)
check("获选人页面不展示交还码", codeA not in qa and 'class="codebox"' not in qa)
check("获选人仍看到已获选状态", "已获选" in qa)
check("获选人页面不暴露内部原因", REASON not in qa)
qa2 = query(tA2)
check("其他申请人页面无码无原因", 'class="codebox"' not in qa2 and REASON not in qa2)
_, v = client()
st, pbody = req(v, "GET", f"/items/{iidA}")
check("访客公开物品页不显示暂缓内部信息",
      "暂未找到" not in pbody and "暂缓" not in pbody and REASON not in pbody)
check("暂缓期间物品仍在公开列表",
      f"/items/{iidA}" in req(v, "GET", "/")[1])

# 工作人员可见
st, body = req(staff, "GET", f"/staff/items/{iidA}")
check("工作人员物品页显示暂缓横幅与原因",
      "暂停交还" in body and REASON in body and "盘点暂未找到" in body)
check("工作人员物品页显示解除表单（携带记录 id）",
      f'/staff/items/{iidA}/not-found/{ridA}/resolve' in body)
st, body = req(staff, "GET", f"/staff/claims/{cA}")
check("工作人员申请页显示暂缓横幅", "暂缓交还中" in body)
check("工作人员申请页仍可见原码（工作人员不被隐藏）", codeA in body)
st, body = req(staff, "GET", "/staff")
check("登记台总览显示暂缓交还标记", "暂缓交还" in body)

# 核码拒绝且不消耗原码
st, vbody, _ = verify_code(codeA)
check("暂缓期间核码被拒并给出原因", "暂缓交还" in vbody and "不能核码" in vbody)
check("原码未被消耗",
      db.execute("SELECT consumed_at FROM handover_codes WHERE id=?",
                 (code_id_of(codeA),)).fetchone()[0] is None)
check("物品仍保管中",
      db.execute("SELECT status FROM items WHERE id=?", (iidA,)).fetchone()[0] == "kept")

# 重复登记被拒
st, _, _ = nf_register(iidA, "重复登记原因")
st, body = req(staff, "GET", f"/staff/items/{iidA}")
check("重复登记被拒", "不能重复登记" in body)
check("仍只有一条未解除记录",
      db.execute("SELECT COUNT(*) FROM item_not_found WHERE item_id=? AND resolved_at IS NULL",
                 (iidA,)).fetchone()[0] == 1)

# 解除：CSRF / 空复核说明
st, _, _ = raw(staff, "POST", f"/staff/items/{iidA}/not-found/{ridA}/resolve",
               form={"csrf_token": "bad", "found_note": "x"})
check("解除伪造 CSRF 被拒(400)", st == 400)
st, _, loc = nf_resolve(iidA, ridA, "   ")
check("解除空复核说明被拒", st == 303 and loc == f"/staff/items/{iidA}")
st, body = req(staff, "GET", f"/staff/items/{iidA}")
check("空复核说明提示", "复核说明不能为空" in body)
check("记录仍未解除", nf_pending_id(iidA) == ridA)

# 正常解除
st, _, loc = nf_resolve(iidA, ridA, NOTE)
check("解除成功", st == 303 and loc == f"/staff/items/{iidA}", f"{st} {loc}")
st, body = req(staff, "GET", loc)
check("解除提示原码恢复", "重新可见、可核销" in body)
row = db.execute("SELECT resolved_at, found_note FROM item_not_found WHERE id=?",
                 (ridA,)).fetchone()
check("记录已解除并保存复核说明与时间", row[0] is not None and row[1] == NOTE)
check("物品已无未解除记录", nf_pending_id(iidA) is None)

qa = query(tA)
check("解除后获选人重新看到原码", code_in(qa) == codeA)
st, vbody, _ = verify_code(codeA)
check("解除后原码可核销交还", st == 303)
check("物品已交还",
      db.execute("SELECT status FROM items WHERE id=?", (iidA,)).fetchone()[0] == "returned")
st, body = req(staff, "GET", f"/staff/items/{iidA}")
check("工作人员页可追溯登记原因/复核说明/时间",
      "暂未找到登记记录" in body and REASON in body and NOTE in body)

# ============================================ B：已交还/已撤下/已并入拒绝登记
print("== B：已撤下 / 已交还 / 已并入拒绝登记；重复解除；记录不匹配 ==")
# 已交还
st, _, _ = nf_register(iidA, "已交还想登记")
st, body = req(staff, "GET", f"/staff/items/{iidA}")
check("已交还物品不能登记", "已交还" in body)

# 已撤下
iidB = make_item("N-B 撤下物品")
st, _, _ = raw(staff, "POST", f"/staff/items/{iidB}/remove", form={"csrf_token": SCSRF})
assert st == 303
st, _, _ = nf_register(iidB)
st, body = req(staff, "GET", f"/staff/items/{iidB}")
check("已撤下物品不能登记", "仅保管中" in body)

# 已并入
iidK0 = make_item("N-B 保留")
iidM0 = make_item("N-B 并入")
st, body = req(staff, "GET", f"/staff/items/{iidK0}")
st, _, loc = raw(staff, "POST", f"/staff/items/{iidK0}/merge",
                 form={"csrf_token": csrf(body), "merged_item_id": iidM0, "reason": "重复"})
assert st == 303, (st, loc)
st, _, _ = nf_register(iidM0)
st, body = req(staff, "GET", f"/staff/items/{iidM0}")
check("已并入物品不能登记", "仅保管中" in body)

# 重复解除 + 记录与物品不匹配
iidX = make_item("N-X 物品")
tX = submit_claim(iidX, "X答案", "cx")
cX = claim_ids(iidX)[0]
select_claim(cX)
st, _, _ = nf_register(iidX)
ridX = nf_pending_id(iidX)
# 用另一物品的 id 去解除本记录（记录与物品不匹配）
st, _, _ = nf_resolve(iidA, ridX, NOTE)
st, body = req(staff, "GET", f"/staff/items/{iidA}")
check("记录与物品不匹配时解除被拒", "不匹配" in body)
check("被拒后记录仍未解除", nf_pending_id(iidX) == ridX)
# 不存在的记录 id
st, _, _ = nf_resolve(iidX, 99999999, NOTE)
st, body = req(staff, "GET", f"/staff/items/{iidX}")
check("记录不存在时不 500 且提示", "暂未找到记录不存在" in body)
# 正常解除后重复解除
st, _, _ = nf_resolve(iidX, ridX, NOTE)
assert st == 303
st, _, _ = nf_resolve(iidX, ridX, "再次解除")
st, body = req(staff, "GET", f"/staff/items/{iidX}")
check("重复解除被拒", "不能重复解除" in body)

# ============================================ C：撤下 / 恢复不清除；撤下期间可解除
print("== C：撤下再恢复不清除未找到状态；撤下期间可解除 ==")
iidC = make_item("N-C 物品")
tC = submit_claim(iidC, "C答案", "cc")
cC = claim_ids(iidC)[0]
select_claim(cC)
codeC = code_in(query(tC))
st, _, _ = nf_register(iidC)
ridC = nf_pending_id(iidC)
st, _, _ = raw(staff, "POST", f"/staff/items/{iidC}/remove", form={"csrf_token": SCSRF})
check("暂缓期间可以撤下", st == 303)
check("撤下后记录仍未解除（撤下不清除状态）", nf_pending_id(iidC) == ridC)
# 撤下期间仍可解除
st, _, loc = nf_resolve(iidC, ridC, "撤下期间找到")
check("撤下期间允许解除", st == 303 and loc == f"/staff/items/{iidC}")
check("解除后无未解除记录", nf_pending_id(iidC) is None)
# 恢复为保管中，原码仍有效（同一枚、未过期）
st, _, _ = raw(staff, "POST", f"/staff/items/{iidC}/restore", form={"csrf_token": SCSRF})
assert st == 303
qa = query(tC)
check("恢复后获选人看到同一枚原码", code_in(qa) == codeC)
st, vbody, _ = verify_code(codeC)
check("撤下恢复解除后原码可核销", st == 303, vbody)

# 撤下再恢复不清除（不解除直接恢复）：仍暂停
iidC2 = make_item("N-C2 物品")
tC2 = submit_claim(iidC2, "C2答案", "cc2")
cC2 = claim_ids(iidC2)[0]
select_claim(cC2)
codeC2 = code_in(query(tC2))
st, _, _ = nf_register(iidC2)
ridC2 = nf_pending_id(iidC2)
raw(staff, "POST", f"/staff/items/{iidC2}/remove", form={"csrf_token": SCSRF})
raw(staff, "POST", f"/staff/items/{iidC2}/restore", form={"csrf_token": SCSRF})
check("撤下再恢复后仍有未解除记录", nf_pending_id(iidC2) == ridC2)
check("恢复后获选人仍看不到码", code_in(query(tC2)) is None and "交还暂缓" in query(tC2))
st, vbody, _ = verify_code(codeC2)
check("恢复后核码仍被拒", "暂缓交还" in vbody)
st, _, _ = nf_resolve(iidC2, ridC2, NOTE)
assert st == 303
st, _, _ = verify_code(codeC2)
check("解除后原码可核销", st == 303)

# ============================================ D：过期码：暂缓隐藏，解除后过期走重发
print("== D：过期码在暂缓期间不显示，解除后仍按重发规则处理 ==")
iidD = make_item("N-D 过期码物品")
tD = submit_claim(iidD, "D答案", "dd")
cD = claim_ids(iidD)[0]
select_claim(cD)
codeD = code_in(query(tD))
st, _, _ = nf_register(iidD)
ridD = nf_pending_id(iidD)
# 把原码改为已过期
db.execute("UPDATE handover_codes SET expires_at = '2000-01-01T00:00:00+00:00' WHERE id=?",
           (code_id_of(codeD),))
db.commit()
qd = query(tD)
check("过期码暂缓期间只显示暂缓（不显示过期重发提示）",
      "交还暂缓" in qd and "已过期" not in qd and 'class="codebox"' not in qd)
st, vbody, _ = verify_code(codeD)
check("过期码暂缓期间核码被暂缓原因拒绝（非过期原因）", "暂缓交还" in vbody)
st, _, _ = nf_resolve(iidD, ridD, NOTE)
assert st == 303
qd = query(tD)
check("解除后过期码按现有规则提示过期/重发", "已过期" in qd and 'class="codebox"' not in qd)
st, vbody, _ = verify_code(codeD)
check("解除后过期码核码按过期拒绝", "已过期" in vbody)
st, _, _ = raw(staff, "POST", f"/staff/claims/{cD}/resend", form={"csrf_token": SCSRF})
assert st == 303
codeD2 = code_in(query(tD))
check("重发后新码可见", bool(codeD2) and codeD2 != codeD)
st, vbody, _ = verify_code(codeD2)
check("新码可核销", st == 303)

# ============================================ E：暂缓期间改判，状态延续、新码也暂缓
print("== E：暂缓期间可改判，状态延续到新获选人，解除后新码可核销 ==")
iidE = make_item("N-E 改判物品")
tE1 = submit_claim(iidE, "E1答案", "ce1")
tE2 = submit_claim(iidE, "E2答案", "ce2")
e1, e2 = claim_ids(iidE)[-2:]
select_claim(e1)
codeE1 = code_in(query(tE1))
st, _, _ = nf_register(iidE)
ridE = nf_pending_id(iidE)
# 暂缓期间改判
st, _, loc = raw(staff, "POST", f"/staff/claims/{e1}/rejudge",
                 form={"csrf_token": SCSRF, "to_claim_id": e2, "reason": "暂缓期间发现误选"})
check("暂缓期间改判成功", st == 303 and loc == f"/staff/claims/{e2}", f"{st} {loc}")
check("改判后未找到状态延续（物品级）", nf_pending_id(iidE) == ridE)
check("原获选人码已作旧",
      db.execute("SELECT invalidated_at FROM handover_codes WHERE id=?",
                 (code_id_of(codeE1),)).fetchone()[0] is not None)
codeE2 = staff_code(e2)  # 暂缓期间访客凭证不显示码；工作人员在申请页仍可见
check("新获选人取得新码但凭证只显示暂缓",
      bool(codeE2) and code_in(query(tE2)) is None and "交还暂缓" in query(tE2))
st, vbody, _ = verify_code(codeE2)
check("新获选人的码暂缓期间同样拒绝核销", "暂缓交还" in vbody)
st, _, _ = nf_resolve(iidE, ridE, NOTE)
assert st == 303
check("解除后新获选人看到其码", code_in(query(tE2)) == codeE2)
st, _, _ = verify_code(codeE2)
check("解除后新码可核销", st == 303)

# ============================================ F：合并迁移未找到记录，保留物品继续暂停
print("== F：合并迁移未找到记录，保留物品继续暂停直至逐条解除 ==")
iidFK = make_item("N-F 保留物品")  # 无申请
iidFV = make_item("N-F 并入物品")
tFV = submit_claim(iidFV, "FV答案", "cfv")
cFV = claim_ids(iidFV)[0]
select_claim(cFV)
codeFV = code_in(query(tFV))
st, _, _ = nf_register(iidFV)
ridFV = nf_pending_id(iidFV)
st, body = req(staff, "GET", f"/staff/items/{iidFK}")
st, _, loc = raw(staff, "POST", f"/staff/items/{iidFK}/merge",
                 form={"csrf_token": csrf(body), "merged_item_id": iidFV,
                       "reason": "暂缓记录合并测试"})
check("带未解除记录的物品允许并入", st == 303 and loc == f"/staff/items/{iidFK}", f"{st} {loc}")
check("未找到记录 item_id 迁移到保留物品",
      db.execute("SELECT item_id FROM item_not_found WHERE id=?", (ridFV,)).fetchone()[0] == iidFK)
check("保留物品处于暂缓状态",
      db.execute("SELECT 1 FROM item_not_found WHERE item_id=? AND resolved_at IS NULL",
                 (iidFK,)).fetchone() is not None)
qfv = query(tFV)
check("并入物品获选人凭证仍显示暂缓（描述为保留物品）、无码",
      "交还暂缓" in qfv and "N-F 保留物品" in qfv and 'class="codebox"' not in qfv
      and REASON not in qfv)
st, vbody, _ = verify_code(codeFV)
check("合并后核码仍被暂缓拒绝", "暂缓交还" in vbody)
st, body = req(staff, "GET", f"/staff/items/{iidFK}")
check("保留物品页可对迁移记录复核解除",
      f'/staff/items/{iidFK}/not-found/{ridFV}/resolve' in body and REASON in body)
# 用旧物品地址解除应被拒（记录已随合并归到保留物品：记录与旧物品不匹配；
# 旧物品也已 merged，两种情况都拒绝且不改动数据）
st, _, _ = nf_resolve(iidFV, ridFV, NOTE)
st, body = req(staff, "GET", f"/staff/items/{iidFV}")
check("并入后的旧物品不能解除记录（拒绝且不改数据）",
      "不能解除" in body
      and db.execute("SELECT item_id, resolved_at FROM item_not_found WHERE id=?",
                     (ridFV,)).fetchone() == (iidFK, None))
# 在保留物品正常解除
st, _, loc = nf_resolve(iidFK, ridFV, "合并后找到")
check("保留物品上解除成功", st == 303 and loc == f"/staff/items/{iidFK}")
st, body = req(staff, "GET", f"/staff/items/{iidFK}")
check("解除后保留物品不再暂缓（无暂缓横幅）", 'class="warnbox"' not in body)
check("并入获选人重新看到原码", code_in(query(tFV)) == codeFV)
st, _, _ = verify_code(codeFV)
check("解除后原码可核销（交还到保留物品）", st == 303)

# 两条各带未解除记录合并：保留物品须逐条解除
iidGK = make_item("N-G 保留有记录")
iidGV = make_item("N-G 并入有记录")
tGK = submit_claim(iidGK, "GK答案", "cgk")
cGK = claim_ids(iidGK)[0]
select_claim(cGK)
# 先给保留物品登记暂缓（这样它有获选），并入物品不能再有获选（否则双获选拒绝）
st, _, _ = nf_register(iidGK)
ridGK = nf_pending_id(iidGK)
# 并入物品：无获选、仅登记暂缓
st, _, _ = nf_register(iidGV)
ridGV = nf_pending_id(iidGV)
st, body = req(staff, "GET", f"/staff/items/{iidGK}")
st, _, loc = raw(staff, "POST", f"/staff/items/{iidGK}/merge",
                 form={"csrf_token": csrf(body), "merged_item_id": iidGV, "reason": "两条暂缓"})
check("两条各带未解除记录也可合并", st == 303 and loc == f"/staff/items/{iidGK}")
check("合并后保留物品有两条未解除记录",
      db.execute("SELECT COUNT(*) FROM item_not_found WHERE item_id=? AND resolved_at IS NULL",
                 (iidGK,)).fetchone()[0] == 2)
codeGK = staff_code(cGK)  # 保留物品暂缓中：从工作人员申请页取码
st, vbody, _ = verify_code(codeGK)
check("两条未解除时核码拒绝", "暂缓交还" in vbody)
# 只解除一条：仍暂停
st, _, _ = nf_resolve(iidGK, ridGK, "先找到保留条")
check("解除一条后仍暂停（仍有未解除记录）",
      db.execute("SELECT COUNT(*) FROM item_not_found WHERE item_id=? AND resolved_at IS NULL",
                 (iidGK,)).fetchone()[0] == 1)
st, body = req(staff, "GET", f"/staff/items/{iidGK}")
check("解除一条提示仍有其他未解除记录", "继续暂停交还" in body)
st, vbody, _ = verify_code(codeGK)
check("仅解除一条时核码仍拒绝", "暂缓交还" in vbody)
# 解除迁移来的另一条
st, _, _ = nf_resolve(iidGK, ridGV, "再找到并入条")
assert st == 303
check("两条都解除后无未解除记录",
      db.execute("SELECT COUNT(*) FROM item_not_found WHERE item_id=? AND resolved_at IS NULL",
                 (iidGK,)).fetchone()[0] == 0)
st, _, _ = verify_code(codeGK)
check("全部逐条解除后原码可核销", st == 303)

# ============================================ H：并发
print("== H：并发登记 / 并发解除 / 登记与核码并发 ==")
iidH = make_item("N-H 并发物品")
tH = submit_claim(iidH, "H答案", "ch")
cH = claim_ids(iidH)[0]
select_claim(cH)
codeH = code_in(query(tH))
barrier = threading.Barrier(2)


def hit_register():
    barrier.wait()
    return raw_cookie(cookie_header, f"/staff/items/{iidH}/not-found",
                      {"csrf_token": SCSRF, "reason": "并发登记"})


with ThreadPoolExecutor(max_workers=2) as ex:
    r1, r2 = [f.result() for f in [ex.submit(hit_register), ex.submit(hit_register)]]
check("并发登记后物品仍 kept 且仅一条未解除记录",
      db.execute("SELECT status FROM items WHERE id=?", (iidH,)).fetchone()[0] == "kept"
      and db.execute("SELECT COUNT(*) FROM item_not_found WHERE item_id=? AND resolved_at IS NULL",
                     (iidH,)).fetchone()[0] == 1,
      str((r1, r2)))

ridH = nf_pending_id(iidH)
barrier2 = threading.Barrier(2)


def hit_resolve():
    barrier2.wait()
    return raw_cookie(cookie_header,
                      f"/staff/items/{iidH}/not-found/{ridH}/resolve",
                      {"csrf_token": SCSRF, "found_note": "并发解除"})


with ThreadPoolExecutor(max_workers=2) as ex:
    r1, r2 = [f.result() for f in [ex.submit(hit_resolve), ex.submit(hit_resolve)]]
check("并发解除只生效一次（恰一条解除记录）",
      db.execute("SELECT COUNT(*) FROM item_not_found WHERE id=? AND resolved_at IS NOT NULL",
                 (ridH,)).fetchone()[0] == 1,
      str((r1, r2)))
check("解除后无未解除记录", nf_pending_id(iidH) is None)

# 登记与核码并发：无论谁先，结果必须一致——未消耗原码且无交还，或物品正常交还
iidH2 = make_item("N-H2 登记核码并发")
tH2 = submit_claim(iidH2, "H2答案", "ch2")
cH2 = claim_ids(iidH2)[0]
select_claim(cH2)
codeH2 = code_in(query(tH2))
barrier3 = threading.Barrier(2)


def hit_reg2():
    barrier3.wait()
    return raw_cookie(cookie_header, f"/staff/items/{iidH2}/not-found",
                      {"csrf_token": SCSRF, "reason": "与核码并发"})


def hit_verify2():
    barrier3.wait()
    return raw_cookie(cookie_header, "/staff/verify",
                      {"csrf_token": SCSRF, "code": codeH2})


with ThreadPoolExecutor(max_workers=2) as ex:
    rr, rv = [f.result() for f in [ex.submit(hit_reg2), ex.submit(hit_verify2)]]
st_item = db.execute("SELECT status FROM items WHERE id=?", (iidH2,)).fetchone()[0]
consumed = db.execute("SELECT consumed_at FROM handover_codes WHERE id=?",
                      (code_id_of(codeH2),)).fetchone()[0]
n_pending = db.execute(
    "SELECT COUNT(*) FROM item_not_found WHERE item_id=? AND resolved_at IS NULL",
    (iidH2,)).fetchone()[0]
# IMMEDIATE 事务串行化：二者互斥。要么登记先（物品 kept、有未解除记录、码未消耗），
# 要么核码先（物品 returned、码已消耗、无未解除记录，因为登记只允许 kept）。
consistent = (
    (st_item == "kept" and n_pending == 1 and consumed is None)
    or (st_item == "returned" and n_pending == 0 and consumed is not None)
)
check("登记与核码并发结果一致（不出现既暂缓又交还 / 码被消耗却暂缓）",
      consistent, f"status={st_item} pending={n_pending} consumed={consumed} reg={rr} ver={rv}")

print(f"\n结果：{PASS} 通过, {FAIL} 失败")
raise SystemExit(1 if FAIL else 0)
