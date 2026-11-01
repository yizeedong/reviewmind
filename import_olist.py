# -*- coding: utf-8 -*-
import sqlite3
from pathlib import Path

import pandas as pd

RAW_DIR = Path("data/raw")
DB_PATH = Path("olist.db")

FILES = {
    "customers": "olist_customers_dataset.csv",
    "orders": "olist_orders_dataset.csv",
    "order_items": "olist_order_items_dataset.csv",
    "order_payments": "olist_order_payments_dataset.csv",
    "reviews": "olist_order_reviews_dataset.csv",
    "products": "olist_products_dataset.csv",
    "sellers": "olist_sellers_dataset.csv",
    "category_translation": "product_category_name_translation.csv",
}


def main():
    missing_files = [
        filename for filename in FILES.values()
        if not (RAW_DIR / filename).exists()
    ]

    if missing_files:
        print("缺少文件：")
        for filename in missing_files:
            print("-", filename)
        return

    if DB_PATH.exists():
        print("olist.db 已存在，请先改名或删除旧文件后再导入。")
        return

    conn = sqlite3.connect(DB_PATH)

    for table_name, filename in FILES.items():
        file_path = RAW_DIR / filename
        df = pd.read_csv(file_path)

        df.to_sql(table_name, conn, if_exists="replace", index=False)
        print(f"{table_name}: {len(df):,} 行")

    conn.executescript("""
    CREATE INDEX idx_orders_order_id ON orders(order_id);
    CREATE INDEX idx_order_items_order_id ON order_items(order_id);
    CREATE INDEX idx_order_items_product_id ON order_items(product_id);
    CREATE INDEX idx_payments_order_id ON order_payments(order_id);
    CREATE INDEX idx_reviews_order_id ON reviews(order_id);
    CREATE INDEX idx_products_product_id ON products(product_id);
    CREATE INDEX idx_customers_customer_id ON customers(customer_id);

    CREATE VIEW order_metrics AS
    SELECT
        o.order_id,
        o.order_status,
        o.order_purchase_timestamp,
        o.order_approved_at,
        o.order_delivered_customer_date,
        o.order_estimated_delivery_date,
        c.customer_city,
        c.customer_state,
        COALESCE(p.payment_value, 0) AS payment_value,
        p.payment_type,
        p.payment_installments,
        r.review_score,
        r.review_comment_title,
        r.review_comment_message,
        ROUND(
            julianday(o.order_delivered_customer_date)
            - julianday(o.order_purchase_timestamp),
            1
        ) AS delivery_days,
        ROUND(
            julianday(o.order_delivered_customer_date)
            - julianday(o.order_estimated_delivery_date),
            1
        ) AS delivery_delay_days
    FROM orders o
    LEFT JOIN customers c ON o.customer_id = c.customer_id
    LEFT JOIN (
        SELECT
            order_id,
            SUM(payment_value) AS payment_value,
            MAX(payment_type) AS payment_type,
            MAX(payment_installments) AS payment_installments
        FROM order_payments
        GROUP BY order_id
    ) p ON o.order_id = p.order_id
    LEFT JOIN (
        SELECT
            order_id,
            AVG(review_score) AS review_score,
            MAX(review_comment_title) AS review_comment_title,
            MAX(review_comment_message) AS review_comment_message
        FROM reviews
        GROUP BY order_id
    ) r ON o.order_id = r.order_id;

    CREATE VIEW order_item_metrics AS
    SELECT
        oi.order_id,
        oi.order_item_id,
        oi.product_id,
        oi.seller_id,
        oi.price,
        oi.freight_value,
        COALESCE(t.product_category_name_english,
                 p.product_category_name) AS product_category,
        o.order_status,
        o.order_purchase_timestamp,
        o.order_delivered_customer_date,
        r.review_score,
        ROUND(
            julianday(o.order_delivered_customer_date)
            - julianday(o.order_purchase_timestamp),
            1
        ) AS delivery_days
    FROM order_items oi
    LEFT JOIN orders o ON oi.order_id = o.order_id
    LEFT JOIN products p ON oi.product_id = p.product_id
    LEFT JOIN category_translation t
        ON p.product_category_name = t.product_category_name
    LEFT JOIN (
        SELECT order_id, AVG(review_score) AS review_score
        FROM reviews
        GROUP BY order_id
    ) r ON oi.order_id = r.order_id;
    """)

    tables = pd.read_sql_query(
        "SELECT name FROM sqlite_master WHERE type IN ('table', 'view') ORDER BY name",
        conn
    )
    print("\n数据库对象：")
    print(tables.to_string(index=False))

    print("\n订单分析视图样例：")
    print(pd.read_sql_query(
        "SELECT * FROM order_metrics LIMIT 3", conn
    ).to_string(index=False))

    conn.close()
    print(f"\n导入完成：{DB_PATH.resolve()}")


if __name__ == "__main__":
    main()