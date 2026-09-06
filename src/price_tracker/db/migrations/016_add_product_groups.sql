-- Product groups: named sets a user compares against each other.
--
-- Membership is its own table rather than a group_id column on products: a
-- monitor belongs in "Monitors" and in "Christmas gifts" at the same time, and
-- forcing a choice would mean tracking the same URL twice.
--
-- Both foreign keys cascade, so deleting a product or a group cleans up the
-- membership rows with it (001_initial.sql turns foreign_keys on).
CREATE TABLE IF NOT EXISTS product_groups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (user_id, name)
);

CREATE TABLE IF NOT EXISTS product_group_members (
    group_id INTEGER NOT NULL REFERENCES product_groups(id) ON DELETE CASCADE,
    product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    added_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (group_id, product_id)
);

CREATE INDEX IF NOT EXISTS idx_group_members_product ON product_group_members(product_id);
