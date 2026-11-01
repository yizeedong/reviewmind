# -*- coding: utf-8 -*-
import os
import json
import sqlite3
from pathlib import Path
import pandas as pd
import streamlit as st
from dotenv import load_dotenv
from openai import OpenAI, DefaultHttpxClient

from schema_utils import DB_PATH, build_schema_text, is_safe_sql

# ---------- 初始化 ----------
env_path = Path(__file__).resolve().parent / ".env"
load_dotenv(env_path)

api_key = os.getenv("DEEPSEEK_API_KEY")
if not api_key:
    st.error("未读取到 DEEPSEEK_API_KEY，请检查 .env 文件")
    st.stop()

client = OpenAI(
    api_key=api_key,
    base_url="https://api.deepseek.com",
    http_client=DefaultHttpxClient(headers={"Accept-Encoding": "identity"}),
)

st.set_page_config(page_title="ReviewMind", layout="wide")
st.title("📊 ReviewMind：电商经营分析 Agent")
st.caption("用自然语言提问，自动生成 SQL、执行查询、给出结论。")

# ---------- 启动时读一次 schema，后续复用 ----------
SCHEMA_TEXT = build_schema_text()

# ---------- 工具：执行 SQL ----------
def run_sql(sql):
    # 第一道关：白名单校验（SELECT/WITH 开头 + 无危险关键字）
    ok, reason = is_safe_sql(sql)
    if not ok:
        return f"错误：{reason}", None

    conn = sqlite3.connect(DB_PATH)
    try:
        df = pd.read_sql_query(sql, conn)
        return df.to_string(index=False), df
    except Exception as e:
        return f"SQL 执行失败：{e}", None
    finally:
        conn.close()

# ---------- 工具说明书 ----------
tools = [
    {
        "type": "function",
        "function": {
            "name": "run_sql",
            "description": (
                "对 Olist 电商数据库执行只读 SQL 查询。"
                "只能查询 order_metrics（订单粒度）和 order_item_metrics（订单商品粒度）两个视图。"
                "只允许 SELECT 或 WITH 开头的查询，禁止任何写操作。"
                "字段详情和约束见 system prompt。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "sql": {"type": "string", "description": "要执行的 SELECT 或 WITH 语句"}
                },
                "required": ["sql"]
            }
        }
    }
]

# ---------- System Prompt ----------
SYSTEM_PROMPT = f"""你是一个电商经营数据分析助手。用户会用自然语言提出经营问题，你需要：
1. 生成一条 SQL 查询（只允许 SELECT 或 WITH）
2. 根据查询结果，用中文给出简洁、可解释的业务结论

{SCHEMA_TEXT}

# 回答风格
- 先给结论，再给关键数字
- 涉及金额时注明单位（数据源货币为巴西雷亚尔 BRL）
- 如果结果为空或字段有 NULL，明确说明，不要编造
- 不要暴露内部推理过程，只输出结论

# 回答纪律（重要，违反会误导用户）
1. 引用数字前，必须逐行核对 SQL 结果。
   - 给"区间"或"范围"时，先扫一遍真实数值的最小/最大值
   - 给"总计/合计"时，必须是 SQL 里 SUM 出来的原始值，不要自己累加
2. 做"最高/最低/最大/最严重"这类判断前，先确认数据支持这个结论，
   不要自相矛盾。
3. 如果问题里的指标口径有歧义（如"延迟最严重"可指平均延迟天数，
   也可指延迟率），先说明你采用的口径；如果结果差异大，同时给出两个指标。
4. 统计口径默认值（按问题里的关键词判断）：
   - 涉及"金额/收入/GMV/销售额" → 只统计 order_status = 'delivered'
   - 涉及"订单数/销量/评分/评价" → 统计全部订单（不按状态过滤），
     但关键字段为 NULL 的记录要排除
   - 涉及"配送延迟/配送时长" → 只统计 delivery_days IS NOT NULL
   结论第一句必须写："口径：xxx"
5. 分类概括（如"未完成状态""高价值类目"）必须明确列出包含哪些类别，
   不要只给一个百分比就下结论。
"""

# ---------- Agent 核心 ----------
def ask_agent(question, history=None):
    """输入自然语言问题，返回 SQL、结果 DataFrame、最终结论"""
    if history is None:
        history = []

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        *history,
        {"role": "user", "content": question}
    ]

    # 第一次调用：让模型决定是否生成 SQL
    response = client.chat.completions.create(
        model="deepseek-chat",
        messages=messages,
        tools=tools,
        temperature=0.1
    )
    msg = response.choices[0].message

    if not msg.tool_calls:
        return None, None, msg.content or "模型没有生成查询。"

    messages.append(msg)

    sql_used = None
    df_result = None

    for tool_call in msg.tool_calls:
        args = json.loads(tool_call.function.arguments)
        sql_used = args["sql"]
        result_text, df_result = run_sql(sql_used)
        messages.append({
            "role": "tool",
            "tool_call_id": tool_call.id,
            "content": result_text
        })

    # 第二次调用：根据工具结果生成结论
    final_response = client.chat.completions.create(
        model="deepseek-chat",
        messages=messages,
        temperature=0.3
    )
    conclusion = final_response.choices[0].message.content

    return sql_used, df_result, conclusion
# ---------- 结果渲染：自动判断出图 or 出表 ----------
def render_result(df):
    if df is None or df.empty:
        st.info("查询结果为空。")
        return

    # 1. 先展示表格
    st.dataframe(df, use_container_width=True)

    # 2. 判断是否适合画图
    if len(df.columns) >= 2 and 2 <= len(df) <= 50:
        col0 = df.columns[0]
        numeric_cols = [c for c in df.columns[1:] if pd.api.types.is_numeric_dtype(df[c])]
        
        if numeric_cols:
            # 🌟 新增：如果有多个指标，加一个选择框让用户选看哪个图
            if len(numeric_cols) > 1:
                selected_metric = st.selectbox("请选择要可视化的指标：", numeric_cols)
            else:
                selected_metric = numeric_cols[0]
                
            df_plot = df.set_index(col0)[[selected_metric]]
            
            looks_like_month = df[col0].astype(str).str.match(r"^\d{4}-\d{2}").all()
            if looks_like_month:
                st.line_chart(df_plot, use_container_width=True)
            else:
                st.bar_chart(df_plot, use_container_width=True)
# ---------- 会话状态 ----------
if "chat_history" not in st.session_state:
    st.session_state.chat_history = []

# ---------- 侧边栏 ----------
with st.sidebar:
    st.header("使用说明")
    st.write("在下方输入框用自然语言提问，比如：")
    st.write("- 2017 年每月 GMV 是多少？")
    st.write("- 收入最高的 10 个商品类目？")
    st.write("- 各支付方式的订单数和平均客单价？")
    st.write("- 配送延迟最严重的 10 个州？")
    st.write("- 1 星订单的订单状态分布？")

    if st.button("清空对话"):
        st.session_state.chat_history = []
        st.rerun()

# ---------- 主界面：历史消息 ----------
for item in st.session_state.chat_history:
    with st.chat_message("user"):
        st.write(item["question"])
    with st.chat_message("assistant"):
        if item.get("sql"):
            with st.expander("查看生成的 SQL"):
                st.code(item["sql"], language="sql")
        if item.get("df") is not None:
            render_result(item["df"])   # 只留这一行，用 item 里的 df
        st.write(item["conclusion"])

## ---------- 输入框 ----------
question = st.chat_input("请输入你的问题...")

if question:
    with st.chat_message("user"):
        st.write(question)

    with st.chat_message("assistant"):
        with st.spinner("分析中..."):
            sql, df, conclusion = ask_agent(question)

        if sql:
            with st.expander("查看生成的 SQL"):
                st.code(sql, language="sql")
        if df is not None:
            render_result(df)            # 只留这一行，用当前的 df
        st.write(conclusion)

    st.session_state.chat_history.append({
        "question": question,
        "sql": sql,
        "df": df,
        "conclusion": conclusion
    })