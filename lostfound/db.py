"""SQLite 连接管理与初始化。

使用 WAL 并开启外键约束；写操作通过 ``immediate_tx`` 先获取保留锁，
配合条件 UPDATE 保证「同一物品并发确认最多成功一次」。
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(
        str(db_path), timeout=10, isolation_level=None, check_same_thread=False
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


@contextmanager
def immediate_tx(conn: sqlite3.Connection):
    """开启 IMMEDIATE 事务：进入即取写锁，避免并发写互相饿死。

    正常退出提交，异常回滚。
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except Exception:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def init_db(db_path: str | Path) -> None:
    """创建库文件并应用幂等建表脚本（即数据初始化）。

    旧版数据库（items 表无 merged_into_id 列）会在单事务内表重建升级，
    以放开 status 的 CHECK 约束并新增列；升级保留全部既有数据且本身幂等。
    """
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    ddl = SCHEMA_PATH.read_text(encoding="utf-8")
    conn = connect(path)
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(items)")}
        if cols and "merged_into_id" not in cols:
            # 表重建期间临时关闭外键（SQLite 官方迁移范式），重建后再开启并校验
            conn.execute("PRAGMA foreign_keys=OFF")
            try:
                # 1) 建新表（merges 等新表）；executescript 会先提交挂起事务
                conn.executescript(ddl)
                # 2) 单事务表重建 items（executescript 不隐式包裹事务，故显式 BEGIN/COMMIT）
                conn.executescript(
                    """
                    BEGIN IMMEDIATE;
                    CREATE TABLE items_new (
                        id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                        public_description TEXT    NOT NULL,
                        private_features   TEXT    NOT NULL,
                        status             TEXT    NOT NULL DEFAULT 'kept'
                                             CHECK (status IN
                                               ('kept', 'removed', 'returned', 'merged')),
                        created_at         TEXT    NOT NULL,
                        updated_at         TEXT    NOT NULL,
                        merged_into_id     INTEGER REFERENCES items(id)
                    );
                    INSERT INTO items_new
                        (id, public_description, private_features, status, created_at, updated_at)
                    SELECT id, public_description, private_features, status, created_at, updated_at
                    FROM items;
                    DROP TABLE items;
                    ALTER TABLE items_new RENAME TO items;
                    COMMIT;
                    """
                )
                violations = conn.execute("PRAGMA foreign_key_check").fetchall()
                if violations:
                    raise RuntimeError(f"数据库升级后外键校验失败: {violations!r}")
            finally:
                conn.execute("PRAGMA foreign_keys=ON")
        else:
            conn.executescript(ddl)
    finally:
        conn.close()
