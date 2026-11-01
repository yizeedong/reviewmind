# schema_utils.py
import re
import sqlite3
from pathlib import Path

# 数据库路径跟随仓库位置，克隆到任何机器都不用改代码
DB_PATH = str(Path(__file__).resolve().parent / "olist.db")

# 只把这些对象暴露给 Agent。原始表不直接给，避免它绕过视图乱查。
EXPOSED_OBJECTS = {
    "order_metrics":       "订单粒度（一行一个订单）。适合 GMV、订单数、支付方式、配送、评分。",
    "order_item_metrics":  "订单商品粒度（一行一个商品明细）。适合类目、商品、卖家、价格、运费。",
    #"category_translation":"类目翻译表。用 product_category 关联 product_category_name 得到英文类目名。",
}

# 手写语义注释，键是 (对象名, 字段名)
COLUMN_NOTES = {
    ("order_metrics", "payment_value"):           "REAL，该订单所有支付记录求和后的总额（不要再去 join order_payments 求和）",
    ("order_metrics", "review_score"):            "INTEGER，1~5 分，可能为 NULL（未评价）",
    ("order_metrics", "delivery_days"):           "REAL，购买到签收的天数，未签收为 NULL",
    ("order_metrics", "delivery_delay_days"):     "REAL，实际签收 - 预计签收；正数=延迟，负数=提前",
    ("order_metrics", "order_purchase_timestamp"):"TEXT，ISO 格式，可直接用 strftime 或 substr 处理",
    ("order_item_metrics", "product_category"):   "TEXT，英文类目名（已翻译），可能为 NULL",
    ("order_item_metrics", "review_score"):       "INTEGER，所属订单的评分，可能为 NULL",
    ("order_item_metrics", "delivery_days"):      "REAL，所属订单购买到签收的天数",
    ("order_item_metrics", "price"):              "REAL，单件商品价格（不是行金额，乘以数量需自己处理；Olist 每行就是一件）",
    ("order_item_metrics", "freight_value"):      "REAL，该商品行的运费",
}

def build_schema_text(db_path: str = DB_PATH) -> str:
    """读数据库 schema，拼成给模型看的文本。结构自动、语义手写。"""
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    lines = ["# 可用的数据库对象\n"]
    for name, purpose in EXPOSED_OBJECTS.items():
        lines.append(f"## {name}")
        lines.append(f"粒度/用途：{purpose}")
        lines.append("字段：")

        cur.execute(f"PRAGMA table_info({name})")
        for col in cur.fetchall():
            _, col_name, col_type, _, _, pk = col
            type_str = col_type if col_type else "(无类型)"
            pk_str = " [PK]" if pk else ""
            note = COLUMN_NOTES.get((name, col_name), "")
            note_str = f"  # {note}" if note else ""
            lines.append(f"  - {col_name} {type_str}{pk_str}{note_str}")
        lines.append("")

    lines.append("# 重要约束")
    lines.append("- order_metrics 和 order_item_metrics 粒度不同，不要跨粒度直接 sum 支付金额")
    lines.append("- 时间字段是 ISO 格式 TEXT，用 strftime('%Y-%m', order_purchase_timestamp) 之类分组")
    lines.append("- 统计评分时先过滤 review_score IS NOT NULL")
    lines.append("- 只允许 SELECT 或 WITH 查询")

    conn.close()
    return "\n".join(lines)


_FORBIDDEN_KEYWORDS = [
    "drop", "delete", "update", "insert", "alter",
    "attach", "detach", "pragma", "vacuum", "reindex",
    "create", "replace", "truncate",
]

def is_safe_sql(sql: str) -> tuple[bool, str]:
    """SQL 白名单校验。返回 (是否安全, 原因)。"""
    if not sql or not sql.strip():
        return False, "SQL 为空"
    s = sql.strip().lower()

    if not (s.startswith("select") or s.startswith("with")):
        return False, "只允许 SELECT 或 WITH 开头的查询"

    for kw in _FORBIDDEN_KEYWORDS:
        if re.search(rf"\b{kw}\b", s):
            return False, f"禁止使用关键字 {kw.upper()}"

    return True, "OK"


if __name__ == "__main__":
    # 1. 打印 schema 文本
    print(build_schema_text())
    print("\n" + "=" * 60 + "\n")

    # 2. 测试 is_safe_sql
    test_cases = [
        "SELECT * FROM order_metrics",
        "DROP TABLE orders",
        "WITH t AS (SELECT 1) SELECT * FROM t",
        "select * from order_items where product_id = 'drop_1'",
    ]
    for sql in test_cases:
        ok, reason = is_safe_sql(sql)
        print(f"{ok!s:5} | {reason:30} | {sql}")