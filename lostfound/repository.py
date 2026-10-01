"""数据访问层：所有 SQL 均为参数化查询；关键写操作为条件式原子更新。"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from . import security
from .db import immediate_tx
# 状态中文标签（单一事实来源，模板也使用）
ITEM_STATUS_LABELS = {"kept": "保管中", "removed": "已撤下", "returned": "已交还",
                      "merged": "已并入"}
CLAIM_STATUS_LABELS = {"pending": "待核对", "selected": "已获选", "rejected": "未获选"}


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


# ----------------------------------------------------------------- 物品

def create_item(conn: sqlite3.Connection, public_desc: str, private_features: str) -> int:
    ts = now_iso()
    with immediate_tx(conn):
        cur = conn.execute(
            "INSERT INTO items (public_description, private_features, status, created_at, updated_at)"
            " VALUES (?, ?, 'kept', ?, ?)",
            (public_desc, private_features, ts, ts),
        )
    return int(cur.lastrowid)


def get_item(conn: sqlite3.Connection, item_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()


def list_public_items(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """访客列表：仅公开保管中物品，不暴露任何私密信息。

    has_selected 用于提示该物品已有人获选、不再接受新申请。
    """
    return conn.execute(
        """
        SELECT i.id, i.public_description, i.created_at,
               EXISTS (SELECT 1 FROM claims c
                        WHERE c.item_id = i.id AND c.status = 'selected') AS has_selected
        FROM items i
        WHERE i.status = 'kept'
        ORDER BY i.id DESC
        """
    ).fetchall()


def list_items_for_staff(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT i.*,
               (SELECT COUNT(*) FROM claims c WHERE c.item_id = i.id) AS claim_count,
               (SELECT COUNT(*) FROM claims c
                 WHERE c.item_id = i.id AND c.status = 'pending') AS pending_count
        FROM items i
        ORDER BY i.id DESC
        """
    ).fetchall()


def set_item_status(conn: sqlite3.Connection, item_id: int, status: str) -> None:
    with immediate_tx(conn):
        conn.execute(
            "UPDATE items SET status = ?, updated_at = ? WHERE id = ?",
            (status, now_iso(), item_id),
        )


def remove_item(conn: sqlite3.Connection, item_id: int) -> None:
    """撤下物品：同时拒绝其全部待核对申请（撤下后不可再申请）。

    已交还不可撤下；已撤下重复操作为幂等无变化；已并入的记录不可撤下。
    """
    ts = now_iso()
    with immediate_tx(conn):
        row = conn.execute("SELECT status FROM items WHERE id = ?", (item_id,)).fetchone()
        if row is None:
            raise LookupError("物品不存在")
        if row["status"] == "returned":
            raise ValueError("该物品已交还，不能撤下")
        if row["status"] == "merged":
            raise ValueError("该物品已并入另一条记录，不能撤下")
        conn.execute(
            "UPDATE items SET status = 'removed', updated_at = ? WHERE id = ? AND status = 'kept'",
            (ts, item_id),
        )
        conn.execute(
            "UPDATE claims SET status = 'rejected', decided_at = ?"
            " WHERE item_id = ? AND status = 'pending'",
            (ts, item_id),
        )


def restore_item(conn: sqlite3.Connection, item_id: int) -> None:
    """恢复已撤下物品为保管中。已并入的记录不可恢复。"""
    with immediate_tx(conn):
        row = conn.execute("SELECT status FROM items WHERE id = ?", (item_id,)).fetchone()
        if row is None:
            raise LookupError("物品不存在")
        if row["status"] == "merged":
            raise ValueError("该物品已并入另一条记录，不能恢复")
        conn.execute(
            "UPDATE items SET status = 'kept', updated_at = ? WHERE id = ? AND status = 'removed'",
            (now_iso(), item_id),
        )


# ----------------------------------------------------------------- 申请

def create_claim(
    conn: sqlite3.Connection, item_id: int, answer: str, contact: str
) -> str:
    """提交认领。仅当物品仍在保管且尚无人获选时接受；返回明文查询凭证（仅此一次）。"""
    token = security.new_query_token()
    token_hash = security.sha256_hex(token)
    ts = now_iso()
    with immediate_tx(conn):
        row = conn.execute(
            "SELECT status FROM items WHERE id = ?", (item_id,)
        ).fetchone()
        if row is None:
            raise LookupError("物品不存在")
        if row["status"] != "kept":
            if row["status"] == "merged":
                raise ValueError("该物品已并入另一条记录，不再接受新的认领申请")
            raise ValueError("该物品已撤下或已交还，不再接受新的认领申请")
        blocked = conn.execute(
            "SELECT 1 FROM claims WHERE item_id = ? AND status = 'selected' LIMIT 1",
            (item_id,),
        ).fetchone()
        if blocked is not None:
            raise ValueError("该物品已确定获选人，不再接受新的认领申请")
        conn.execute(
            "INSERT INTO claims (item_id, answer, contact, token_hash, status, created_at)"
            " VALUES (?, ?, ?, ?, 'pending', ?)",
            (item_id, answer, contact, token_hash, ts),
        )
    return token


def get_claim_by_token(conn: sqlite3.Connection, token: str) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT c.*, i.public_description AS item_description, i.status AS item_status,
               EXISTS (SELECT 1 FROM claims c2
                        WHERE c2.item_id = i.id AND c2.status = 'selected') AS item_has_selected
        FROM claims c JOIN items i ON i.id = c.item_id
        WHERE c.token_hash = ?
        """,
        (security.sha256_hex(token),),
    ).fetchone()


def list_claims_for_item(conn: sqlite3.Connection, item_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM claims WHERE item_id = ? ORDER BY id ASC", (item_id,)
    ).fetchall()


def get_claim(conn: sqlite3.Connection, claim_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM claims WHERE id = ?", (claim_id,)).fetchone()


def select_claim(conn: sqlite3.Connection, claim_id: int, code_ttl_seconds: int, key: bytes) -> str:
    """选定一份认领并签发首个一次性交还码。

    原子性保证：
      1. 条件 UPDATE 仅在物品处于 kept 且申请仍为 pending 时生效；
      2. 同事务内将同一物品的其他 pending 申请标记为 rejected。
    并发下至多一个请求 rowcount=1，其余得到 None（未获选）。
    """
    ts = now_iso()
    expires = (
        datetime.now(timezone.utc) + timedelta(seconds=code_ttl_seconds)
    ).replace(microsecond=0).isoformat()
    code = security.new_handover_code()
    with immediate_tx(conn):
        row = conn.execute(
            """
            SELECT c.id AS claim_id, c.item_id AS item_id, i.status AS item_status
            FROM claims c JOIN items i ON i.id = c.item_id
            WHERE c.id = ?
            """,
            (claim_id,),
        ).fetchone()
        if row is None:
            raise LookupError("申请不存在")
        if row["item_status"] != "kept":
            raise ValueError("物品已撤下或已交还，无法选定认领")

        cur = conn.execute(
            "UPDATE claims SET status = 'selected', decided_at = ?"
            " WHERE id = ? AND status = 'pending'",
            (ts, claim_id),
        )
        if cur.rowcount != 1:
            existing = get_claim(conn, claim_id)
            if existing is not None and existing["status"] == "rejected":
                raise ValueError("该申请已因他人获选而标记为未获选")
            raise ValueError("该申请已经处理，不能重复选定")

        conn.execute(
            "UPDATE claims SET status = 'rejected', decided_at = ?"
            " WHERE item_id = ? AND status = 'pending' AND id <> ?",
            (ts, row["item_id"], claim_id),
        )
        conn.execute(
            "INSERT INTO handover_codes (claim_id, code_hash, code_enc, issued_at, expires_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (claim_id, security.sha256_hex(code), security.encrypt_field(key, code), ts, expires),
        )
    return code


# ----------------------------------------------------------------- 改判

def rejudge_claim(
    conn: sqlite3.Connection,
    from_claim_id: int,
    to_claim_id: int,
    reason: str,
    code_ttl_seconds: int,
    key: bytes,
) -> str:
    """改判认领：原获选申请变为未获选，同一物品的目标申请变为已获选。

    前置条件（任一不满足均拒绝，不改动任何状态）：
      - 原申请当前必须为 selected；
      - 目标申请必须存在、属于同一物品且当前为 rejected（未获选）；
      - 物品仍处于 kept（未撤下、未交还）；
      - 更正理由非空（由调用方负责校验，这里再次防御）。

    单次 IMMEDIATE 事务内的条件 UPDATE 保证与重发、撤下、核码或另一改判
    并发时，只有一次符合条件的状态变更能够生效：
      - 原申请所有未核销交还码立即失效；
      - 为新获选人签发一枚新的一次性交还码；
      - 保存改判记录（双方、理由、时间）。
    """
    reason = (reason or "").strip()
    if not reason:
        raise ValueError("更正理由不能为空")
    if from_claim_id == to_claim_id:
        raise ValueError("改判目标不能是原获选申请本身")

    ts = now_iso()
    expires = (
        datetime.now(timezone.utc) + timedelta(seconds=code_ttl_seconds)
    ).replace(microsecond=0).isoformat()
    code = security.new_handover_code()
    with immediate_tx(conn):
        frm = conn.execute(
            "SELECT c.id, c.status, c.item_id, i.status AS item_status FROM claims c"
            " JOIN items i ON i.id = c.item_id WHERE c.id = ?",
            (from_claim_id,),
        ).fetchone()
        if frm is None:
            raise LookupError("原获选申请不存在")
        tgt = conn.execute(
            "SELECT c.id, c.status, c.item_id FROM claims c WHERE c.id = ?",
            (to_claim_id,),
        ).fetchone()
        if tgt is None:
            raise LookupError("目标申请不存在")
        if frm["item_id"] != tgt["item_id"]:
            raise ValueError("目标申请与原获选申请不属于同一物品，不能改判")
        if frm["item_status"] != "kept":
            raise ValueError("物品已撤下或已交还，不能改判")
        if frm["status"] != "selected":
            raise ValueError("原申请当前不是已获选状态，不能改判")
        if tgt["status"] != "rejected":
            raise ValueError("目标申请必须为未获选状态")

        cur = conn.execute(
            "UPDATE claims SET status = 'rejected', decided_at = ?"
            " WHERE id = ? AND status = 'selected'",
            (ts, from_claim_id),
        )
        if cur.rowcount != 1:
            raise ValueError("原申请已非获选状态（可能已被并发处理），改判未执行")
        cur = conn.execute(
            "UPDATE claims SET status = 'selected', decided_at = ?"
            " WHERE id = ? AND status = 'rejected' AND item_id = ?",
            (ts, to_claim_id, frm["item_id"]),
        )
        if cur.rowcount != 1:
            raise RuntimeError("目标申请状态已变化，回滚本次改判")

        # 原获选人的全部未核销交还码立即失效（含已过期但未核销的）
        conn.execute(
            "UPDATE handover_codes SET invalidated_at = ?"
            " WHERE claim_id = ? AND consumed_at IS NULL AND invalidated_at IS NULL",
            (ts, from_claim_id),
        )
        conn.execute(
            "INSERT INTO handover_codes (claim_id, code_hash, code_enc, issued_at, expires_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (to_claim_id, security.sha256_hex(code), security.encrypt_field(key, code), ts, expires),
        )
        conn.execute(
            "INSERT INTO corrections (item_id, from_claim_id, to_claim_id, reason, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (frm["item_id"], from_claim_id, to_claim_id, reason, ts),
        )
        upd = conn.execute(
            "UPDATE items SET updated_at = ? WHERE id = ? AND status = 'kept'",
            (ts, frm["item_id"]),
        )
        if upd.rowcount != 1:
            raise RuntimeError("物品状态异常，回滚本次改判")
    return code


def list_corrections_for_item(conn: sqlite3.Connection, item_id: int) -> list[sqlite3.Row]:
    """物品的改判记录（工作人员追溯），含双方答案/联系方式摘要。"""
    return conn.execute(
        """
        SELECT cr.*,
               fa.answer  AS from_answer,  fa.contact AS from_contact,
               ta.answer  AS to_answer,    ta.contact AS to_contact
        FROM corrections cr
        JOIN claims fa ON fa.id = cr.from_claim_id
        JOIN claims ta ON ta.id = cr.to_claim_id
        WHERE cr.item_id = ?
        ORDER BY cr.id ASC
        """,
        (item_id,),
    ).fetchall()


# ----------------------------------------------------------------- 物品合并

def list_mergeable_items(conn: sqlite3.Connection, keep_item_id: int) -> list[sqlite3.Row]:
    """可并入保留物品的候选：其他仍在保管、未被并入的物品。"""
    return conn.execute(
        "SELECT id, public_description, created_at FROM items"
        " WHERE status = 'kept' AND id <> ? ORDER BY id ASC",
        (keep_item_id,),
    ).fetchall()


def merge_items(
    conn: sqlite3.Connection,
    keep_item_id: int,
    merged_item_id: int,
    reason: str,
) -> dict[str, Any]:
    """把误登记的重复物品记录（merged_item_id）并入保留物品（keep_item_id）。

    仅在下列条件全部满足时执行，任一不满足均拒绝且不改动任何数据：
      - 保留/并入物品都存在、是两条不同记录，且当前都仍在保管（status='kept'）；
      - 合并理由非空；
      - 两条记录当前不得各有一份获选申请（否则会出现双获选）。

    单次 IMMEDIATE 事务内原子完成：
      - 并入物品的全部申请改挂到保留物品；合计仅一份获选时保留其交还码，
        其余待核对申请标记未获选；没有获选时双方待核对申请均保持待核对；
      - 并入物品的全部改判记录归到保留物品；
      - 并入物品置为 merged 并指向保留物品：从公开列表消失、旧详情指向
        保留物品、不能恢复、不再接受新申请；
      - 保存合并记录（保留物品、并入物品、理由、时间）。
    返回 {'keep_item_id': ..., 'merged_item_id': ..., 'selected_claim_id': int|None}。
    """
    reason = (reason or "").strip()
    if not reason:
        raise ValueError("合并理由不能为空")
    if keep_item_id == merged_item_id:
        raise ValueError("保留物品与并入物品必须是两条不同的记录")

    ts = now_iso()
    with immediate_tx(conn):
        keep = conn.execute(
            "SELECT id, status FROM items WHERE id = ?", (keep_item_id,)
        ).fetchone()
        if keep is None:
            raise LookupError("保留物品不存在")
        victim = conn.execute(
            "SELECT id, status FROM items WHERE id = ?", (merged_item_id,)
        ).fetchone()
        if victim is None:
            raise LookupError("并入物品不存在")
        if keep["status"] != "kept" or victim["status"] != "kept":
            raise ValueError("仅两条均仍在保管的记录可以合并")

        keep_selected = conn.execute(
            "SELECT id FROM claims WHERE item_id = ? AND status = 'selected'",
            (keep_item_id,),
        ).fetchall()
        victim_selected = conn.execute(
            "SELECT id FROM claims WHERE item_id = ? AND status = 'selected'",
            (merged_item_id,),
        ).fetchall()
        if keep_selected and victim_selected:
            raise ValueError("两条记录各有获选申请，不能合并：请先改判或处理后再合并")

        # 并入物品的全部申请归到保留物品（含获选/未获选/待核对、交还码随申请保留）
        conn.execute(
            "UPDATE claims SET item_id = ? WHERE item_id = ?",
            (keep_item_id, merged_item_id),
        )
        # 并入物品的改判记录归到保留物品（申请双方已随上面的 UPDATE 同属保留物品）
        conn.execute(
            "UPDATE corrections SET item_id = ? WHERE item_id = ?",
            (keep_item_id, merged_item_id),
        )
        # 并入物品的交还撤销记录同样归到保留物品（被撤销的码与申请已随上属保留物品）
        conn.execute(
            "UPDATE return_revocations SET item_id = ? WHERE item_id = ?",
            (keep_item_id, merged_item_id),
        )
        # 合计仅一份获选：其他待核对申请标记未获选（与选定逻辑一致）
        winner_id = None
        if keep_selected or victim_selected:
            winner_id = (keep_selected[0]["id"] if keep_selected
                         else victim_selected[0]["id"])
            conn.execute(
                "UPDATE claims SET status = 'rejected', decided_at = ?"
                " WHERE item_id = ? AND status = 'pending' AND id <> ?",
                (ts, keep_item_id, winner_id),
            )

        conn.execute(
            "INSERT INTO merges (keep_item_id, merged_item_id, reason, created_at)"
            " VALUES (?, ?, ?, ?)",
            (keep_item_id, merged_item_id, reason, ts),
        )
        cur = conn.execute(
            "UPDATE items SET status = 'merged', merged_into_id = ?, updated_at = ?"
            " WHERE id = ? AND status = 'kept'",
            (keep_item_id, ts, merged_item_id),
        )
        if cur.rowcount != 1:
            raise RuntimeError("并入物品状态已变化（可能已被并发处理），回滚本次合并")
        conn.execute(
            "UPDATE items SET updated_at = ? WHERE id = ? AND status = 'kept'",
            (ts, keep_item_id),
        )
        # 防御性不变量：合并后保留物品至多一份获选
        n_selected = conn.execute(
            "SELECT COUNT(*) FROM claims WHERE item_id = ? AND status = 'selected'",
            (keep_item_id,),
        ).fetchone()[0]
        if n_selected > 1:
            raise RuntimeError("合并后出现多份获选申请，回滚本次合并")
    return {
        "keep_item_id": keep_item_id,
        "merged_item_id": merged_item_id,
        "selected_claim_id": winner_id,
    }


def get_merge_for_merged(conn: sqlite3.Connection, merged_item_id: int) -> sqlite3.Row | None:
    """并入物品对应的合并记录（旧详情指向保留物品、工作人员查看理由/时间）。"""
    return conn.execute(
        """
        SELECT m.*, i.public_description AS keep_description
        FROM merges m JOIN items i ON i.id = m.keep_item_id
        WHERE m.merged_item_id = ?
        ORDER BY m.id DESC LIMIT 1
        """,
        (merged_item_id,),
    ).fetchone()


def list_merges_for_item(conn: sqlite3.Connection, item_id: int) -> list[sqlite3.Row]:
    """物品相关的全部合并记录（无论作为保留方还是并入方），用于详情页展示。"""
    return conn.execute(
        """
        SELECT m.*,
               ki.public_description AS keep_description,
               mi.public_description AS merged_description
        FROM merges m
        JOIN items ki ON ki.id = m.keep_item_id
        JOIN items mi ON mi.id = m.merged_item_id
        WHERE m.keep_item_id = ? OR m.merged_item_id = ?
        ORDER BY m.id ASC
        """,
        (item_id, item_id),
    ).fetchall()


# ----------------------------------------------------------------- 交还码

def get_active_code_for_claim(conn: sqlite3.Connection, claim_id: int) -> sqlite3.Row | None:
    """未核销、未作废旧码的最新一枚交还码（可能已过期）。"""
    return conn.execute(
        "SELECT * FROM handover_codes WHERE claim_id = ? AND consumed_at IS NULL"
        " AND invalidated_at IS NULL ORDER BY id DESC LIMIT 1",
        (claim_id,),
    ).fetchone()


def get_claim_with_context(conn: sqlite3.Connection, claim_id: int) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT c.*, i.public_description, i.private_features, i.status AS item_status
        FROM claims c JOIN items i ON i.id = c.item_id
        WHERE c.id = ?
        """,
        (claim_id,),
    ).fetchone()


def resend_code(conn: sqlite3.Connection, claim_id: int, ttl_seconds: int, key: bytes) -> str:
    """重发交还码：旧码立即失效，签发一枚新码。"""
    ts = now_iso()
    expires = (
        datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)
    ).replace(microsecond=0).isoformat()
    code = security.new_handover_code()
    with immediate_tx(conn):
        claim = conn.execute(
            "SELECT c.id, c.status, i.status AS item_status FROM claims c"
            " JOIN items i ON i.id = c.item_id WHERE c.id = ?",
            (claim_id,),
        ).fetchone()
        if claim is None:
            raise LookupError("申请不存在")
        if claim["status"] != "selected":
            raise ValueError("只有已获选的申请可以重发交还码")
        if claim["item_status"] == "returned":
            raise ValueError("物品已交还，无需重发")
        conn.execute(
            "UPDATE handover_codes SET invalidated_at = ?"
            " WHERE claim_id = ? AND consumed_at IS NULL AND invalidated_at IS NULL",
            (ts, claim_id),
        )
        conn.execute(
            "INSERT INTO handover_codes (claim_id, code_hash, code_enc, issued_at, expires_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (claim_id, security.sha256_hex(code), security.encrypt_field(key, code), ts, expires),
        )
    return code


def decrypt_code(key: bytes, code_row: sqlite3.Row) -> str | None:
    """授权页面回显交还码明文（数据库只存哈希与密文）。"""
    return security.decrypt_field(key, code_row["code_enc"])


def consume_code(conn: sqlite3.Connection, code: str) -> dict[str, Any]:
    """工作人员核码并完成交还。

    单次 IMMEDIATE 事务内做存在性/状态判断与核销，保证：
      - 重复核码（同一码两次提交、两枚码并发核销）不得再次交还；
      - 已交还、已过期、已作废旧码均拒绝。
    返回 {'ok': True, 'claim_id': ..., 'item_id': ...} 或
    {'ok': False, 'reason': '...'}。
    """
    code_hash = security.sha256_hex(code.strip())
    ts = now_iso()
    result: dict[str, Any] = {"ok": False, "reason": "交还码不正确"}
    with immediate_tx(conn):
        hc = conn.execute(
            """
            SELECT hc.*, c.item_id, c.status AS claim_status, i.status AS item_status
            FROM handover_codes hc
            JOIN claims c ON c.id = hc.claim_id
            JOIN items i ON i.id = c.item_id
            WHERE hc.code_hash = ?
            """,
            (code_hash,),
        ).fetchone()
        if hc is None:
            return result
        if hc["consumed_at"] is not None:
            return {"ok": False, "reason": "该交还码已使用，不能重复核销"}
        if hc["invalidated_at"] is not None:
            return {"ok": False, "reason": "该交还码已失效（已重发新码或认领已改判）"}
        if hc["expires_at"] < ts:
            return {"ok": False, "reason": "该交还码已过期，请为获选人重发新码"}
        if hc["item_status"] != "kept" or hc["claim_status"] != "selected":
            return {"ok": False, "reason": "物品或认领状态异常，无法交还"}
        cur = conn.execute(
            "UPDATE handover_codes SET consumed_at = ? WHERE id = ? AND consumed_at IS NULL",
            (ts, hc["id"]),
        )
        if cur.rowcount != 1:
            return {"ok": False, "reason": "该交还码已被核销（并发请求）"}
        upd = conn.execute(
            "UPDATE items SET status = 'returned', updated_at = ?"
            " WHERE id = ? AND status = 'kept'",
            (ts, hc["item_id"]),
        )
        if upd.rowcount != 1:
            raise RuntimeError("物品状态异常，回滚本次核销")
        result = {"ok": True, "claim_id": hc["claim_id"], "item_id": hc["item_id"]}
    return result


# ----------------------------------------------------------------- 交还登记撤销

def get_return_for_item(conn: sqlite3.Connection, item_id: int) -> sqlite3.Row | None:
    """已交还物品当前待撤销的「本次核销记录」。

    即：该物品当前获选申请下一枚已核销、且在 return_revocations 中尚无撤销记录
    的交还码（含其核销时间）。物品非已交还、或本次核销已撤销时返回 None。
    """
    return conn.execute(
        """
        SELECT hc.id AS code_id, hc.consumed_at AS consumed_at,
               c.id AS claim_id, i.status AS item_status
        FROM handover_codes hc
        JOIN claims c ON c.id = hc.claim_id
        JOIN items i ON i.id = c.item_id
        WHERE c.item_id = ? AND c.status = 'selected'
          AND hc.consumed_at IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM return_revocations rr WHERE rr.code_id = hc.id)
        ORDER BY hc.id DESC LIMIT 1
        """,
        (item_id,),
    ).fetchone()


def revoke_return(
    conn: sqlite3.Connection,
    item_id: int,
    code_id: int,
    reason: str,
    code_ttl_seconds: int,
    key: bytes,
) -> dict[str, Any]:
    """撤销该物品的本次交还登记（工作人员核码后发现物品实际仍在馆内）。

    仅在下列条件全部于同一 IMMEDIATE 事务内核验通过时执行，任一不满足均拒绝且
    不改动任何数据：
      - 物品当前仍为 returned（已交还）；
      - code_id 指定的核销记录存在、确属该物品，且核销时对应的申请当前仍为
        selected（属于当前获选申请）；
      - 该核销记录尚未被撤销（重复请求 / 并发撤销时只有一次生效）。

    原子完成：
      - 物品恢复为 kept（保管中），申请归属与获选状态保持不变；
      - 已核销码永久失效（consumed_at 保留，不再允许核销）；
      - 按现有有效期规则向当前获选人签发一枚新的一次性交还码；
      - 保存撤销记录（物品、获选申请、被撤销核销记录、必填原因、原核销时间、
        撤销时间）供工作人员追溯。
    返回 {'item_id': ..., 'claim_id': ..., 'code_id': ..., 'code': 新码明文}。
    """
    reason = (reason or "").strip()
    if not reason:
        raise ValueError("撤销原因不能为空")
    ts = now_iso()
    expires = (
        datetime.now(timezone.utc) + timedelta(seconds=code_ttl_seconds)
    ).replace(microsecond=0).isoformat()
    code = security.new_handover_code()
    with immediate_tx(conn):
        item = conn.execute(
            "SELECT id, status FROM items WHERE id = ?", (item_id,)
        ).fetchone()
        if item is None:
            raise LookupError("物品不存在")
        if item["status"] != "returned":
            raise ValueError("物品当前不是已交还状态，不能撤销交还登记")

        hc = conn.execute(
            """
            SELECT hc.id, hc.consumed_at, hc.claim_id, c.item_id, c.status AS claim_status
            FROM handover_codes hc JOIN claims c ON c.id = hc.claim_id
            WHERE hc.id = ?
            """,
            (code_id,),
        ).fetchone()
        if hc is None:
            raise LookupError("核销记录不存在")
        if hc["item_id"] != item_id:
            raise ValueError("核销记录与该物品不匹配，不能撤销")
        if hc["consumed_at"] is None:
            raise ValueError("该交还码尚未核销，记录不匹配")
        if hc["claim_status"] != "selected":
            raise ValueError("该核销记录不属于当前获选申请，不能撤销")
        already = conn.execute(
            "SELECT 1 FROM return_revocations WHERE code_id = ? LIMIT 1", (code_id,)
        ).fetchone()
        if already is not None:
            raise ValueError("本次交还登记已撤销，不能重复撤销")

        upd = conn.execute(
            "UPDATE items SET status = 'kept', updated_at = ?"
            " WHERE id = ? AND status = 'returned'",
            (ts, item_id),
        )
        if upd.rowcount != 1:
            raise ValueError("物品状态已变化（可能已被并发处理），撤销未执行")

        conn.execute(
            "INSERT INTO handover_codes (claim_id, code_hash, code_enc, issued_at, expires_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (hc["claim_id"], security.sha256_hex(code),
             security.encrypt_field(key, code), ts, expires),
        )
        conn.execute(
            "INSERT INTO return_revocations"
            " (item_id, claim_id, code_id, reason, consumed_at, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (item_id, hc["claim_id"], code_id, reason, hc["consumed_at"], ts),
        )
    return {
        "item_id": item_id,
        "claim_id": hc["claim_id"],
        "code_id": code_id,
        "code": code,
    }


def list_return_revocations_for_item(conn: sqlite3.Connection, item_id: int) -> list[sqlite3.Row]:
    """物品的交还撤销记录（仅工作人员追溯），含获选申请答案/联系方式摘要。"""
    return conn.execute(
        """
        SELECT rr.*,
               cl.answer  AS claim_answer,
               cl.contact AS claim_contact
        FROM return_revocations rr
        JOIN claims cl ON cl.id = rr.claim_id
        WHERE rr.item_id = ?
        ORDER BY rr.id ASC
        """,
        (item_id,),
    ).fetchall()
