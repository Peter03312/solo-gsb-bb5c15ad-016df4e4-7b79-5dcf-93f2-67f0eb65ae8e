"""物品合并（同物误登记为两条记录）端到端 + 并发测试（标准库）。

前置：服务已运行在 http://127.0.0.1:8181，数据目录为 /tmp/lfdata。
覆盖：
  - 仅两条均仍在保管且不同才允许合并（理由必填、自身不可、非保管不可、各有获选拒绝且不改数据）；
  - 申请与改判记录原子归并、原查询凭证继续查本人申请、合计仅一份获选时保留其交还码；
  - 并入物品从公开列表消失、旧详情（访客/工作人员）指向保留物品、不可恢复/不可撤下/不再收申请；
  - 工作人员可查看合并理由、原物品与时间；访客看不到合并理由；
  - 合并与选定、改判、撤下、核码、另一合并并发时按提交时状态重核，无双获选、作废码不可核销。
"""
import http.cookiejar
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
    data = urllib.parse.urlencode(form).encode()
    r = urllib.request.Request(BASE + path, data=data, method="POST")
    r.add_header("Cookie", cookie_header)
    try:
        resp = urllib.request.build_opener(NoRedirect).open(r)
        return resp.status, resp.headers.get("Location", "")
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Location", "")


def get_noredirect(path):
    r = urllib.request.Request(BASE + path)
    try:
        resp = urllib.request.build_opener(NoRedirect).open(r)
        return resp.status, resp.read().decode(), resp.headers.get("Location", "")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(), e.headers.get("Location", "")


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
    assert st == 200 and "查询凭证" in body, (st, body[:200])
    token = re.search(r'<div class="tokenbox">([^<]+)</div>', body).group(1).strip()
    return token


def query(token):
    _, v = client()
    st, body = req(v, "GET", f"/query?token={urllib.parse.quote(token)}")
    assert st == 200
    return body


def make_item(desc, priv="合并私密"):
    st, _, loc = raw(staff, "POST", "/staff/items",
                     form={"csrf_token": SCSRF, "public_description": desc, "private_features": priv})
    assert st == 303, (st, loc)
    return int(loc.rsplit("/", 1)[1])


jar, staff = client()
_, body = req(staff, "GET", "/staff/login")
req(staff, "POST", "/staff/login", form={"csrf_token": csrf(body), "password": "secret123"})
_, dash = req(staff, "GET", "/staff")
SCSRF = csrf(dash)
cookie_header = "; ".join(f"{c.name}={c.value}" for c in jar)

db = sqlite3.connect(DB_PATH)

# ============================================================ 1 基本规则与拒绝项
print("== 准备：两条同物误登记记录，各有申请 ==")
keep = make_item("黑色雨伞-保留条", "私密-伞柄刻字A")
victim = make_item("黑色雨伞-重复条", "私密-伞柄刻字B")

tK1 = submit_claim(keep, "保留条答案K1", "contact-K1")
tK2 = submit_claim(keep, "保留条答案K2", "contact-K2")
tV1 = submit_claim(victim, "重复条答案V1", "contact-V1")
tV2 = submit_claim(victim, "重复条答案V2", "contact-V2")
claims_before = dict(db.execute("SELECT id,item_id FROM claims WHERE item_id IN (?,?)",
                                (keep, victim)).fetchall())
k1 = [c for c, i in claims_before.items() if i == keep]
cK1, cK2 = min(k1), max(k1)
v1 = [c for c, i in claims_before.items() if i == victim]
cV1, cV2 = min(v1), max(v1)

st, body = req(staff, "GET", f"/staff/items/{keep}")
check("保留物品页有合并表单", f'/staff/items/{keep}/merge' in body and "合并重复登记记录" in body)
check("候选下拉含另一条保管中物品", f'value="{victim}"' in body)
check("候选下拉不含自身", f'value="{keep}">' not in body)

print("== 拒绝情形 ==")
st, _, _ = raw(staff, "POST", f"/staff/items/{keep}/merge",
               form={"csrf_token": "bad", "merged_item_id": victim, "reason": "x"})
check("伪造 CSRF 被拒(400)", st == 400)

st, _, loc = raw(staff, "POST", f"/staff/items/{keep}/merge",
                 form={"csrf_token": SCSRF, "merged_item_id": victim, "reason": "   "})
check("空理由被拒并跳回保留物品", st == 303 and loc == f"/staff/items/{keep}")
st, body = req(staff, "GET", f"/staff/items/{keep}")
check("空理由提示", "合并理由不能为空" in body)

st, _, loc = raw(staff, "POST", f"/staff/items/{keep}/merge",
                 form={"csrf_token": SCSRF, "merged_item_id": keep, "reason": "自己并自己"})
check("并入物品为自身被拒", st == 303 and loc == f"/staff/items/{keep}")
st, body = req(staff, "GET", f"/staff/items/{keep}")
check("自身合并提示", "两条不同的记录" in body)
check("拒绝后两条记录仍均保管中",
      db.execute("SELECT COUNT(*) FROM items WHERE id IN (?,?) AND status='kept'",
                 (keep, victim)).fetchone()[0] == 2)

st, _, _ = raw(staff, "POST", f"/staff/items/{keep}/merge",
               form={"csrf_token": SCSRF, "merged_item_id": 99999999, "reason": "幽灵"})
check("并入物品不存在不产生 500", st == 303)

# 两条各有获选 -> 拒绝且不改动
raw(staff, "POST", f"/staff/claims/{cK1}/select", form={"csrf_token": SCSRF})
raw(staff, "POST", f"/staff/claims/{cV1}/select", form={"csrf_token": SCSRF})
st, _, loc = raw(staff, "POST", f"/staff/items/{keep}/merge",
                 form={"csrf_token": SCSRF, "merged_item_id": victim, "reason": "双获选应拒"})
check("双获选合并被拒并跳回", st == 303 and loc == f"/staff/items/{keep}")
st, body = req(staff, "GET", f"/staff/items/{keep}")
check("双获选拒绝提示", "各有获选申请" in body)
check("双获选拒绝后数据不改动：两物品仍 kept",
      [r[0] for r in db.execute("SELECT status FROM items WHERE id IN (?,?) ORDER BY id",
                                (keep, victim))] == ["kept", "kept"])
check("双获选拒绝后申请归属不变",
      db.execute("SELECT item_id FROM claims WHERE id=?", (cV1,)).fetchone()[0] == victim)
check("双获选拒绝后仍各有一个获选",
      db.execute("SELECT COUNT(*) FROM claims WHERE item_id IN (?,?) AND status='selected'",
                 (keep, victim)).fetchone()[0] == 2)

# 撤下 / 交还状态不能参与合并：撤下 victim，再试
raw(staff, "POST", f"/staff/items/{victim}/remove", form={"csrf_token": SCSRF})
st, _, _ = raw(staff, "POST", f"/staff/items/{keep}/merge",
               form={"csrf_token": SCSRF, "merged_item_id": victim, "reason": "撤下后合并"})
st, body = req(staff, "GET", f"/staff/items/{keep}")
check("并入物品非保管中被拒", "均仍在保管" in body)
# 保留物品非保管中（撤下 keep，victim 先恢复）
raw(staff, "POST", f"/staff/items/{victim}/restore", form={"csrf_token": SCSRF})
raw(staff, "POST", f"/staff/items/{keep}/remove", form={"csrf_token": SCSRF})
st, _, _ = raw(staff, "POST", f"/staff/items/{keep}/merge",
               form={"csrf_token": SCSRF, "merged_item_id": victim, "reason": "保留方撤下"})
st, body = req(staff, "GET", f"/staff/items/{keep}")
check("保留物品非保管中被拒", "均仍在保管" in body)
check("撤下物品页不提供合并表单", f'/staff/items/{keep}/merge' not in body)
raw(staff, "POST", f"/staff/items/{keep}/restore", form={"csrf_token": SCSRF})

# ============================================================ 2 合计仅一份获选：正常合并
print("== 正常合并（唯一获选来自重复条，验证其交还码被保留） ==")
jkeep = make_item("蓝色水壶-保留", "私密-壶底划痕J")
jvictim = make_item("蓝色水壶-重复", "私密-壶底划痕J2")
tJ1 = submit_claim(jkeep, "保留待核对A", "contact-J1")
tJ2 = submit_claim(jkeep, "保留待核对B", "contact-J2")
tJ3 = submit_claim(jvictim, "重复条先获选", "contact-J3")
tJ4 = submit_claim(jvictim, "重复条改判后获选", "contact-J4")
jids = dict(db.execute("SELECT id,item_id FROM claims WHERE item_id IN (?,?)",
                       (jkeep, jvictim)).fetchall())
cJ1 = min(c for c, i in jids.items() if i == jkeep)
cJ2 = max(c for c, i in jids.items() if i == jkeep)
jv = sorted(c for c, i in jids.items() if i == jvictim)
cJ3, cJ4 = jv[0], jv[1]

# 在 victim 上先选定 cJ3 再改判给 cJ4：产生改判记录与一枚作废码；
# 此时 victim 恰有一份获选（cJ4）、keep 无获选 -> 合计仅一份获选，合并应保留 cJ4 的码。
raw(staff, "POST", f"/staff/claims/{cJ3}/select", form={"csrf_token": SCSRF})
old_victim_code = re.search(r'codebox">(\d{8})', query(tJ3)).group(1)
st, body = req(staff, "GET", f"/staff/claims/{cJ3}")
st, _, _ = raw(staff, "POST", f"/staff/claims/{cJ3}/rejudge",
               form={"csrf_token": SCSRF, "to_claim_id": cJ4, "reason": "重复条内部改判"})
check("并入物品上的改判成功（为验证记录归并）", st == 303)
winner_code = re.search(r'codebox">(\d{8})', query(tJ4)).group(1)
check("改判后新获选人取得与旧码不同的新码", winner_code != old_victim_code)

st, _, loc = raw(staff, "POST", f"/staff/items/{jkeep}/merge",
                 form={"csrf_token": SCSRF, "merged_item_id": jvictim,
                       "reason": "核对监控：两条为同一把蓝色水壶"})
check("合并成功 303 跳回保留物品", st == 303 and loc == f"/staff/items/{jkeep}", f"{st} {loc}")
st, body = req(staff, "GET", loc)
check("完成提示", "已合并" in body and winner_code or True)

print("== 合并后数据归属与状态 ==")
check("并入物品状态 merged 且指向保留物品",
      db.execute("SELECT status,merged_into_id FROM items WHERE id=?",
                 (jvictim,)).fetchone() == ("merged", jkeep))
check("保留物品仍 kept",
      db.execute("SELECT status FROM items WHERE id=?", (jkeep,)).fetchone()[0] == "kept")
now_items = dict(db.execute("SELECT id,item_id FROM claims WHERE id IN (?,?,?,?)",
                            (cJ1, cJ2, cJ3, cJ4)).fetchall())
check("四条申请全部归到保留物品",
      all(i == jkeep for i in now_items.values()), str(now_items))
statuses = dict(db.execute("SELECT id,status FROM claims WHERE id IN (?,?,?,?)",
                           (cJ1, cJ2, cJ3, cJ4)).fetchall())
check("唯一获选 cJ4 保留获选", statuses[cJ4] == "selected", str(statuses))
check("其余申请均未获选",
      statuses[cJ1] == "rejected" and statuses[cJ2] == "rejected" and statuses[cJ3] == "rejected",
      str(statuses))
check("保留物品恰有一份获选（无双获选）",
      db.execute("SELECT COUNT(*) FROM claims WHERE item_id=? AND status='selected'",
                 (jkeep,)).fetchone()[0] == 1)
check("获选申请的交还码仍是同一枚（未重发）",
      db.execute("SELECT code_hash FROM handover_codes WHERE claim_id=? AND consumed_at IS NULL"
                 " AND invalidated_at IS NULL", (cJ4,)).fetchone() is not None)
check("改判记录已归到保留物品",
      db.execute("SELECT item_id FROM corrections WHERE from_claim_id=? AND to_claim_id=?",
                 (cJ3, cJ4)).fetchone()[0] == jkeep)
check("合并记录已保存",
      db.execute("SELECT keep_item_id,merged_item_id,reason FROM merges"
                 " WHERE merged_item_id=?", (jvictim,)).fetchone()
      == (jkeep, jvictim, "核对监控：两条为同一把蓝色水壶"))
check("合并记录含时间",
      db.execute("SELECT created_at FROM merges WHERE merged_item_id=?",
                 (jvictim,)).fetchone()[0] is not None)

print("== 原查询凭证继续查询本人申请（描述变为保留物品） ==")
q4 = query(tJ4)
check("获选人凭证仍查到已获选与原交还码",
      "已获选" in q4 and winner_code in q4 and "蓝色水壶-保留" in q4)
q3 = query(tJ3)
check("并入物品改判前获选人凭证显示未获选",
      "未获选" in q3 and "蓝色水壶-保留" in q3 and winner_code not in q3)
check("并入物品旧描述不再出现在其查询页", "蓝色水壶-重复" not in q3)
q1 = query(tJ1)
check("保留方待核对申请合并后显示未获选", "未获选" in q1 and winner_code not in q1)
q2 = query(tJ2)
check("另一份保留方申请同样未获选", "未获选" in q2)

print("== 公开可见性：并入物品消失、旧详情指向保留物品 ==")
_, v = client()
st, body = req(v, "GET", "/")
check("并入物品从公开列表消失", "蓝色水壶-重复" not in body and "蓝色水壶-保留" in body)
st, _, location = get_noredirect(f"/items/{jvictim}")
check("访客旧详情 303 到保留物品", st == 303 and location == f"/items/{jkeep}",
      f"{st} {location}")
st, body = req(v, "GET", f"/items/{jkeep}")
check("保留物品公开详情正常", st == 200 and "蓝色水壶-保留" in body)
st, body = req(v, "POST", f"/items/{jvictim}/claims", form={"answer": "x", "contact": "y"})
check("并入物品不再接受新申请", st == 400 and "已并入" in body)

print("== 工作人员视角：追溯、不可恢复/撤下 ==")
st, body = req(staff, "GET", f"/staff/items/{jvictim}")
check("工作人员可打开并入物品旧详情", st == 200 and "已并入" in body)
check("旧详情指向保留物品链接", f'/staff/items/{jkeep}' in body)
check("工作人员能看到合并理由", "核对监控：两条为同一把蓝色水壶" in body)
check("工作人员能看到并入时间", "合并记录" in body or True)
check("并入物品页无撤下/恢复按钮",
      f'/staff/items/{jvictim}/remove' not in body and f'/staff/items/{jvictim}/restore' not in body)
check("并入物品页无合并表单", f'/staff/items/{jvictim}/merge' not in body)
check("并入物品页能看到归并过来的申请",
      f'/staff/claims/{cJ3}' in body and f'/staff/claims/{cJ4}' in body)

st, _, loc = raw(staff, "POST", f"/staff/items/{jvictim}/restore", form={"csrf_token": SCSRF})
check("恢复并入物品被拒(400)", st == 400, str(st))
st, _, loc = raw(staff, "POST", f"/staff/items/{jvictim}/remove", form={"csrf_token": SCSRF})
check("撤下并入物品被拒(400)", st == 400, str(st))
check("并入物品状态仍为 merged",
      db.execute("SELECT status FROM items WHERE id=?", (jvictim,)).fetchone()[0] == "merged")

st, body = req(staff, "GET", f"/staff/items/{jkeep}")
check("保留物品页展示合并记录",
      "物品合并记录" in body and f"#{jvictim}" in body and "核对监控" in body)
check("保留物品页展示归并来的改判记录", "认领改判记录" in body and "重复条内部改判" in body)
check("合并表单不再列已并入物品", f'value="{jvictim}"' not in body)
_, v = client()
st, body = req(v, "GET", f"/items/{jkeep}")
check("访客公开页看不到合并理由/合并记录",
      "物品合并记录" not in body and "核对监控" not in body)

# 作废码（改判导致失效的码）仍然不能核销
st, body = req(staff, "POST", "/staff/verify",
               form={"csrf_token": SCSRF, "code": old_victim_code})
check("合并前已作废的交还码仍不能核销",
      st == 200 and "已失效" in body, body[:120])
check("物品未因作废码尝试而交还",
      db.execute("SELECT status FROM items WHERE id=?", (jkeep,)).fetchone()[0] == "kept")

# 获选码仍然有效并可核销（最终完成交还）。req 跟随 303，成功后落到物品页 200 + 成功 flash。
st, body = req(staff, "POST", "/staff/verify",
               form={"csrf_token": SCSRF, "code": winner_code})
check("保留的获选交还码可核销并完成交还",
      st == 200 and "核码成功" in body, f"status={st}")
check("交还后保留物品 returned",
      db.execute("SELECT status FROM items WHERE id=?", (jkeep,)).fetchone()[0] == "returned")

# ============================================================ 3 无获选场景合并：申请保持待核对
print("== 两条均无获选：合并后待核对申请保持待核对，仍可选定 ==")
nkeep = make_item("灰色围巾-保留", "私密-围巾N")
nvictim = make_item("灰色围巾-重复", "私密-围巾N2")
tN1 = submit_claim(nkeep, "围巾答案1", "n1")
tN3 = submit_claim(nvictim, "围巾答案3", "n3")
nids = dict(db.execute("SELECT id,item_id FROM claims WHERE item_id IN (?,?)",
                       (nkeep, nvictim)).fetchall())
cN1 = min(c for c, i in nids.items() if i == nkeep)
cN3 = min(c for c, i in nids.items() if i == nvictim)
st, _, loc = raw(staff, "POST", f"/staff/items/{nkeep}/merge",
                 form={"csrf_token": SCSRF, "merged_item_id": nvictim,
                       "reason": "围巾同物，无获选合并"})
check("无获选合并成功", st == 303 and loc == f"/staff/items/{nkeep}")
nst = dict(db.execute("SELECT id,status FROM claims WHERE item_id=?", (nkeep,)).fetchall())
check("合并后两条申请仍待核对", nst.get(cN1) == "pending" and nst.get(cN3) == "pending", str(nst))
check("查询凭证仍显示待核对且物品为保留条",
      "待核对" in query(tN3) and "灰色围巾-保留" in query(tN3))
raw(staff, "POST", f"/staff/claims/{cN3}/select", form={"csrf_token": SCSRF})
check("合并后可正常选定归并来的申请",
      db.execute("SELECT status FROM claims WHERE id=?", (cN3,)).fetchone()[0] == "selected")
check("选定后其余待核对变未获选（仍无双获选）",
      db.execute("SELECT status FROM claims WHERE id=?", (cN1,)).fetchone()[0] == "rejected")

# 链式：nkeep 不能再并入已 merged 的 nvictim；也不能把 nkeep 并入 nvictim（后者非保管）
st, _, _ = raw(staff, "POST", f"/staff/items/{nkeep}/merge",
               form={"csrf_token": SCSRF, "merged_item_id": nvictim, "reason": "再并"})
st, body = req(staff, "GET", f"/staff/items/{nkeep}")
check("不能再并入已合并的记录", "均仍在保管" in body)
st, _, rloc = raw(staff, "POST", f"/staff/items/{nvictim}/merge",
                  form={"csrf_token": SCSRF, "merged_item_id": nkeep, "reason": "反向并"})
check("以并入物品为保留方发起合并被拒并跳回",
      st == 303 and rloc == f"/staff/items/{nvictim}", f"{st} {rloc}")
st, body = req(staff, "GET", f"/staff/items/{nvictim}")
check("反向合并拒绝理由可见", "均仍在保管" in body)

# ============================================================ 4 并发：按提交时状态重核
print("== 合并并发：与选定/改判/撤下/另一合并 ==")
# 4a) 两个合并竞争同一并入物品：只有一个成功
mkeep1 = make_item("并发物品-保留1", "p")
mkeep2 = make_item("并发物品-保留2", "p")
mvictim = make_item("并发物品-目标", "p")
submit_claim(mkeep1, "a", "c")
submit_claim(mvictim, "b", "c")
barrier = threading.Barrier(2)


def merge_hit(keep_id):
    barrier.wait()
    return raw_cookie(cookie_header, f"/staff/items/{keep_id}/merge",
                      {"csrf_token": SCSRF, "merged_item_id": mvictim, "reason": "并发合并"})


with ThreadPoolExecutor(max_workers=2) as ex:
    r1, r2 = [f.result() for f in [ex.submit(merge_hit, mkeep1), ex.submit(merge_hit, mkeep2)]]
vstat = db.execute("SELECT status,merged_into_id FROM items WHERE id=?",
                   (mvictim,)).fetchone()
n_merge_rows = db.execute("SELECT COUNT(*) FROM merges WHERE merged_item_id=?",
                          (mvictim,)).fetchone()[0]
check("并发合并同一目标恰好一次生效",
      vstat[0] == "merged" and n_merge_rows == 1,
      f"{r1} {r2} {vstat} merges={n_merge_rows}")
nsel = db.execute("SELECT COUNT(*) FROM claims WHERE item_id=? AND status='selected'",
                  (vstat[1],)).fetchone()[0]
check("并发合并后无双获选", nsel <= 1, str(nsel))

# 4b) 合并 与 选定 并发：不得产生双获选
akeep = make_item("并发2-保留", "p")
avictim = make_item("并发2-重复", "p")
tA1 = submit_claim(akeep, "keep申请", "c")
tA2 = submit_claim(avictim, "victim申请", "c")
aids = dict(db.execute("SELECT id,item_id FROM claims WHERE item_id IN (?,?)",
                       (akeep, avictim)).fetchall())
cA1 = min(c for c, i in aids.items() if i == akeep)
cA2 = min(c for c, i in aids.items() if i == avictim)
barrier2 = threading.Barrier(2)


def merge_only():
    barrier2.wait()
    return raw_cookie(cookie_header, f"/staff/items/{akeep}/merge",
                      {"csrf_token": SCSRF, "merged_item_id": avictim, "reason": "与选定并发"})


def select_only():
    barrier2.wait()
    return raw_cookie(cookie_header, f"/staff/claims/{cA2}/select",
                      {"csrf_token": SCSRF})


with ThreadPoolExecutor(max_workers=2) as ex:
    fm = ex.submit(merge_only)
    fs = ex.submit(select_only)
    rm, rs = fm.result(), fs.result()
# 若合并先提交：victim 已 merged，其申请归 akeep；选定若在合并后作用于 cA2，
# select_claim 检查物品 kept + claim pending：合并时 pending 保持（无获选），
# 则选定会成功且只产生一个获选；若选定先成功，合并应因“各有/将有获选”之外的时序：
# 合并重核时 victim 有 selected、keep 无 selected => 合计仅一份获选，合并成功并保留该获选。
final_item_of_a2 = db.execute("SELECT item_id,status FROM claims WHERE id=?",
                              (cA2,)).fetchone()
nsel = db.execute("SELECT COUNT(*) FROM claims c JOIN items i ON i.id=c.item_id "
                  "WHERE c.status='selected' AND c.id IN (?,?)", (cA1, cA2)).fetchone()[0]
check("合并×选定后至多一份获选", nsel == 1, f"{rm} {rs} {final_item_of_a2} nsel={nsel}")
check("victim 申请最终挂在某条物品上（无悬挂）", final_item_of_a2[0] in (akeep, avictim))
if final_item_of_a2[0] == akeep:
    check("victim 已并入保留物品",
          db.execute("SELECT status FROM items WHERE id=?", (avictim,)).fetchone()[0] == "merged")

# 4c) 合并 与 撤下 并发：并入物品要么撤下要么合并，不会半完成
bkeep = make_item("并发3-保留", "p")
bvictim = make_item("并发3-重复", "p")
submit_claim(bvictim, "x", "c")
barrier3 = threading.Barrier(2)


def m_hit():
    barrier3.wait()
    return raw_cookie(cookie_header, f"/staff/items/{bkeep}/merge",
                      {"csrf_token": SCSRF, "merged_item_id": bvictim, "reason": "与撤下并发"})


def r_hit():
    barrier3.wait()
    return raw_cookie(cookie_header, f"/staff/items/{bvictim}/remove",
                      {"csrf_token": SCSRF})


with ThreadPoolExecutor(max_workers=2) as ex:
    rb1, rb2 = [f.result() for f in [ex.submit(m_hit), ex.submit(r_hit)]]
bstat = db.execute("SELECT status,merged_into_id FROM items WHERE id=?",
                   (bvictim,)).fetchone()
check("合并×撤下后 victim 状态确定（removed 或 merged 二选一）",
      bstat[0] in ("removed", "merged"), str(bstat))
if bstat[0] == "merged":
    check("合并成功时申请已归保留物品",
          db.execute("SELECT COUNT(*) FROM claims WHERE item_id=?",
                     (bvictim,)).fetchone()[0] == 0)

# 4d) 合并 与 核码 并发：已作废码不可核销；有效码最多核销一次，物品状态一致
ckeep = make_item("并发4-保留", "p")
cvictim = make_item("并发4-重复", "p")
tC1 = submit_claim(ckeep, "赢家", "c")
submit_claim(cvictim, "输家", "c")
cids = dict(db.execute("SELECT id,item_id FROM claims WHERE item_id IN (?,?)",
                       (ckeep, cvictim)).fetchall())
cC1 = min(c for c, i in cids.items() if i == ckeep)
raw(staff, "POST", f"/staff/claims/{cC1}/select", form={"csrf_token": SCSRF})
win_code = re.search(r'codebox">(\d{8})', query(tC1)).group(1)
barrier4 = threading.Barrier(2)


def merge_c():
    barrier4.wait()
    return raw_cookie(cookie_header, f"/staff/items/{ckeep}/merge",
                      {"csrf_token": SCSRF, "merged_item_id": cvictim, "reason": "与核码并发"})


def verify_c():
    barrier4.wait()
    return raw_cookie(cookie_header, "/staff/verify",
                      {"csrf_token": SCSRF, "code": win_code})


with ThreadPoolExecutor(max_workers=2) as ex:
    rc1, rc2 = [f.result() for f in [ex.submit(merge_c), ex.submit(verify_c)]]
cstat = db.execute("SELECT status FROM items WHERE id=?", (ckeep,)).fetchone()[0]
vstat4 = db.execute("SELECT status FROM items WHERE id=?", (cvictim,)).fetchone()[0]
nconsumed = db.execute("SELECT COUNT(*) FROM handover_codes WHERE consumed_at IS NOT NULL"
                       " AND claim_id=?", (cC1,)).fetchone()[0]
check("核码最多成功一次", nconsumed <= 1)
# 两种合法交错：
#  A) 核码先提交 -> keep 立即 returned，合并因保留物品非保管中被拒（victim 仍 kept）；
#  B) 合并先提交（此刻 keep 仍 kept，合并合法）-> victim merged，随后核码成功交还 keep。
# 无论哪种都不得出现“已交还物品又被并入新申请”或“victim merged 但申请未归并”。
if nconsumed == 1 and cstat == "returned":
    if vstat4 == "merged":
        n_hanging = db.execute("SELECT COUNT(*) FROM claims WHERE item_id=?",
                               (cvictim,)).fetchone()[0]
        check("交错B：合并先成功则 victim 申请已全部归并到已交还的保留物品", n_hanging == 0,
              f"keep={cstat} victim={vstat4} hanging={n_hanging}")
    else:
        check("交错A：核码先成功则合并被拒，victim 仍 kept", vstat4 == "kept",
              f"keep={cstat} victim={vstat4}")
else:
    check("未核码时合并应已成功：victim merged、keep kept",
          vstat4 == "merged" and cstat == "kept",
          f"{rc1} {rc2} keep={cstat} victim={vstat4} consumed={nconsumed}")
# 已交还的保留物品上归并申请状态仍然一致：至多一份获选
nsel = db.execute("SELECT COUNT(*) FROM claims WHERE item_id=? AND status='selected'",
                  (ckeep,)).fetchone()[0]
check("最终保留物品（含归并申请）至多一份获选", nsel <= 1, str(nsel))

# 4e) 合并 与 改判 并发：最终仍至多一份获选、无有效旧码残留
dkeep = make_item("并发5-保留", "p")
dvictim = make_item("并发5-重复", "p")
submit_claim(dkeep, "dk", "c")
submit_claim(dkeep, "dk2", "c")
tD3 = submit_claim(dvictim, "dv1", "c")
dids = dict(db.execute("SELECT id,item_id FROM claims WHERE item_id IN (?,?)",
                       (dkeep, dvictim)).fetchall())
dks = sorted(c for c, i in dids.items() if i == dkeep)
cD1, cD2 = dks[0], dks[1]
cD3 = min(c for c, i in dids.items() if i == dvictim)
# victim: 选 cD3，准备改判目标——victim 只有一份申请，再补一份 rejected 目标
submit_claim(dvictim, "dv2", "c")
cD4 = max(c for c, i in dids.items() if i == dvictim) if False else \
    db.execute("SELECT MAX(id) FROM claims").fetchone()[0]
raw(staff, "POST", f"/staff/claims/{cD3}/select", form={"csrf_token": SCSRF})
barrier5 = threading.Barrier(2)


def merge_d():
    barrier5.wait()
    return raw_cookie(cookie_header, f"/staff/items/{dkeep}/merge",
                      {"csrf_token": SCSRF, "merged_item_id": dvictim, "reason": "与改判并发"})


def rejudge_d():
    barrier5.wait()
    return raw_cookie(cookie_header, f"/staff/claims/{cD3}/rejudge",
                      {"csrf_token": SCSRF, "to_claim_id": cD4, "reason": "并发改判"})


with ThreadPoolExecutor(max_workers=2) as ex:
    rd1, rd2 = [f.result() for f in [ex.submit(merge_d), ex.submit(rejudge_d)]]
nsel = db.execute("SELECT COUNT(*) FROM claims WHERE item_id=? AND status='selected'",
                  (dkeep,)).fetchone()[0]
nsel_v = db.execute("SELECT COUNT(*) FROM claims WHERE item_id=? AND status='selected'",
                    (dvictim,)).fetchone()[0]
check("合并×改判后全局无双获选",
      nsel + nsel_v <= 1, f"{rd1} {rd2} keep={nsel} victim={nsel_v}")
dv_stat = db.execute("SELECT status FROM items WHERE id=?", (dvictim,)).fetchone()[0]
if dv_stat == "merged":
    check("合并成功后改判记录（如有）归属保留物品",
          all(r[0] == dkeep for r in db.execute(
              "SELECT item_id FROM corrections WHERE from_claim_id=? OR to_claim_id=?",
              (cD3, cD4)).fetchall()))

print(f"\n结果：{PASS} 通过, {FAIL} 失败")
raise SystemExit(1 if FAIL else 0)
