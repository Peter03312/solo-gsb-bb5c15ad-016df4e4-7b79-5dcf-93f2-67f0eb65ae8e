-- 失物认领系统数据库结构（SQLite）
-- 幂等：重复执行不会破坏已有数据。

-- 物品：公开描述对访客可见；私密特征仅工作人员可见。
CREATE TABLE IF NOT EXISTS items (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    public_description TEXT    NOT NULL,
    private_features   TEXT    NOT NULL,
    -- kept=保管中  removed=已撤下  returned=已交还  merged=已并入另一条记录
    status             TEXT    NOT NULL DEFAULT 'kept'
                         CHECK (status IN ('kept', 'removed', 'returned', 'merged')),
    created_at         TEXT    NOT NULL,
    updated_at         TEXT    NOT NULL,
    -- status='merged' 时指向保留物品；其余状态为 NULL
    merged_into_id     INTEGER REFERENCES items(id)
);

-- 认领申请：token_hash 存放查询凭证的 SHA-256，凭证明文只出现一次。
CREATE TABLE IF NOT EXISTS claims (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id    INTEGER NOT NULL REFERENCES items(id),
    answer     TEXT    NOT NULL,           -- 访客提交的辨认答案（仅工作人员可见）
    contact    TEXT    NOT NULL,           -- 联系方式（仅工作人员可见）
    token_hash TEXT    NOT NULL UNIQUE,
    -- pending=待核对  selected=已获选  rejected=未获选
    status     TEXT    NOT NULL DEFAULT 'pending'
                 CHECK (status IN ('pending', 'selected', 'rejected')),
    created_at TEXT    NOT NULL,
    decided_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_claims_item ON claims(item_id);

-- 一次性交还码：code_hash 用于核码查找，code_enc 为对称加密密文，
-- 仅授权页面（持凭证的获选人 / 工作人员）解密回显。
CREATE TABLE IF NOT EXISTS handover_codes (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    claim_id       INTEGER NOT NULL REFERENCES claims(id),
    code_hash      TEXT    NOT NULL,
    code_enc       TEXT    NOT NULL,
    issued_at      TEXT    NOT NULL,
    expires_at     TEXT    NOT NULL,
    consumed_at    TEXT,                   -- 核销时间
    invalidated_at TEXT                    -- 被新码作旧的时间
);
CREATE INDEX IF NOT EXISTS idx_codes_claim ON handover_codes(claim_id);
CREATE INDEX IF NOT EXISTS idx_codes_hash ON handover_codes(code_hash);

-- 认领改判记录：工作人员发现认领误选时，把原获选申请改判为同一物品的另一份
-- 既有（未获选）申请。记录申请双方、更正理由与时间，供工作人员追溯。
CREATE TABLE IF NOT EXISTS corrections (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id       INTEGER NOT NULL REFERENCES items(id),
    from_claim_id INTEGER NOT NULL REFERENCES claims(id),  -- 原获选申请（改判后未获选）
    to_claim_id   INTEGER NOT NULL REFERENCES claims(id),  -- 目标申请（改判后已获选）
    reason        TEXT    NOT NULL,
    created_at    TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_corrections_item ON corrections(item_id);

-- 物品合并记录：同一件失物被误登记为两条记录时，工作人员指定保留物品与
-- 并入物品并填写理由。并入物品的全部申请与改判记录原子地归到保留物品，
-- 并入物品状态变为 merged、不再公开、不能恢复也不再接受申请。
CREATE TABLE IF NOT EXISTS merges (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    keep_item_id     INTEGER NOT NULL REFERENCES items(id),  -- 保留物品
    merged_item_id   INTEGER NOT NULL REFERENCES items(id),  -- 并入物品（合并后 merged）
    reason           TEXT    NOT NULL,
    created_at       TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_merges_keep ON merges(keep_item_id);
CREATE INDEX IF NOT EXISTS idx_merges_merged ON merges(merged_item_id);

-- 交还登记撤销记录：工作人员核码后发现物品实际仍在馆内时，可在已交还物品详情页
-- 填写必填原因撤销该次交还登记。每条记录对应一次「已核销且尚未撤销」的核销记录
-- （handover_codes 行）；撤销在单个事务内把物品恢复为保管中（申请归属与获选状态
-- 不变）、已核销码永久失效，并按现有有效期规则向当前获选人签发一枚新交还码。
-- code_id 上的唯一约束保证一枚已核销码至多被撤销一次。
CREATE TABLE IF NOT EXISTS return_revocations (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id      INTEGER NOT NULL REFERENCES items(id),
    claim_id     INTEGER NOT NULL REFERENCES claims(id),       -- 当前获选申请（撤销后归属不变）
    code_id      INTEGER NOT NULL UNIQUE REFERENCES handover_codes(id),  -- 被撤销的本次核销记录
    reason       TEXT    NOT NULL,
    consumed_at  TEXT    NOT NULL,                             -- 原核销时间（快照）
    created_at   TEXT    NOT NULL                              -- 撤销时间
);
CREATE INDEX IF NOT EXISTS idx_revocations_item ON return_revocations(item_id);
