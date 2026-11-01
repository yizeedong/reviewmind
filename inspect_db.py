import sqlite3

db_path = r"C:\Users\董一泽\Desktop\reviewmind\olist.db"
conn = sqlite3.connect(db_path)
cur = conn.cursor()

# 1. 列出所有表和视图
cur.execute("""
    SELECT name, type
    FROM sqlite_master
    WHERE type IN ('table', 'view')
    ORDER BY type, name
""")
objects = cur.fetchall()
print("=== 数据库对象 ===")
for name, obj_type in objects:
    print(f"{obj_type:6} {name}")

# 2. 逐个打印字段
print("\n=== 字段详情 ===")
for name, obj_type in objects:
    print(f"\n--- {obj_type}: {name} ---")
    cur.execute(f"PRAGMA table_info({name})")
    for col in cur.fetchall():
        # col = (cid, name, type, notnull, default, pk)
        _, col_name, col_type, notnull, _, pk = col
        flag = " PK" if pk else ""
        print(f"  {col_name} {col_type}{flag}")

conn.close()