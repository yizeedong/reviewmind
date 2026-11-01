# -*- coding: utf-8 -*-
"""ReviewMind 界面层。

只负责渲染；所有模型调用、SQL 执行、提示词都在 agent_core.py 里，
这样评测工具（sqlaudit）可以直接调用同一套逻辑，不用启动 Streamlit。
"""
import pandas as pd
import streamlit as st

from agent_core import ask_agent

st.set_page_config(page_title="ReviewMind", layout="wide")
st.title("📊 ReviewMind：电商经营分析 Agent")
st.caption("用自然语言提问，自动生成 SQL、执行查询、给出结论。")


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
            # 如果有多个指标，加一个选择框让用户选看哪个图
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
            render_result(item["df"])
        st.write(item["conclusion"])

# ---------- 输入框 ----------
question = st.chat_input("请输入你的问题...")

if question:
    with st.chat_message("user"):
        st.write(question)

    with st.chat_message("assistant"):
        with st.spinner("分析中..."):
            try:
                trace = ask_agent(question)
            except RuntimeError as e:
                st.error(str(e))
                st.stop()

        sql = trace.get("sql")
        df = trace.get("df")
        conclusion = trace.get("conclusion") or ""

        if sql:
            with st.expander("查看生成的 SQL"):
                st.code(sql, language="sql")
        if df is not None:
            render_result(df)
        st.write(conclusion)

    st.session_state.chat_history.append(
        {"question": question, "sql": sql, "df": df, "conclusion": conclusion}
    )
