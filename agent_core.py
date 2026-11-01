# -*- coding: utf-8 -*-
"""ReviewMind 的 Agent 核心。

这个文件不含任何界面代码，目的是让同一套逻辑既能被 Streamlit 调用，
也能被脚本 / 评测工具直接调用（见 sqlaudit 项目）。

对外只暴露一个函数：ask_agent(question) -> 轨迹字典。
"""
import json
import os
import sqlite3
import time
from pathlib import Path

import httpx
import pandas as pd
from dotenv import load_dotenv
from openai import OpenAI, DefaultHttpxClient

from schema_utils import DB_PATH, build_schema_text, is_safe_sql

MODEL = "deepseek-chat"
BASE_URL = "https://api.deepseek.com"

# 单次请求最多等 60 秒、连接 10 秒；失败重试 2 次
# （实测不加超时时，一次网络卡顿会让整题挂 8 分半，界面上就是"一直转圈"）
REQUEST_TIMEOUT = httpx.Timeout(60.0, connect=10.0)

env_path = Path(__file__).resolve().parent / ".env"
load_dotenv(env_path)

_client = None


def get_client():
    """惰性创建客户端：没有 API Key 时给出明确报错，而不是在导入阶段就崩。"""
    global _client
    if _client is None:
        api_key = os.getenv("DEEPSEEK_API_KEY")
        if not api_key:
            raise RuntimeError("未读取到 DEEPSEEK_API_KEY，请检查 .env 文件")
        _client = OpenAI(
            api_key=api_key,
            base_url=BASE_URL,
            timeout=REQUEST_TIMEOUT,
            max_retries=2,
            http_client=DefaultHttpxClient(
                headers={"Accept-Encoding": "identity"}, timeout=REQUEST_TIMEOUT
            ),
        )
    return _client


# 启动时读一次 schema，后续复用
SCHEMA_TEXT = build_schema_text()


# ---------- 工具：执行 SQL ----------
def run_sql(sql):
    """返回 (给模型看的文本, DataFrame 或 None)。失败一律降级成字符串，不抛异常。"""
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
TOOLS = [
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
                "required": ["sql"],
            },
        },
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


def _rows_for_json(df):
    """把 DataFrame 转成可写进 JSON 的二维列表（NaN -> None）。"""
    if df is None:
        return []
    return df.astype(object).where(pd.notna(df), None).values.tolist()


def _usage_dict(resp):
    u = getattr(resp, "usage", None)
    if u is None:
        return {}
    return {
        "prompt_tokens": getattr(u, "prompt_tokens", None),
        "completion_tokens": getattr(u, "completion_tokens", None),
        "total_tokens": getattr(u, "total_tokens", None),
    }


def ask_agent(question, history=None):
    """输入自然语言问题，返回一条完整轨迹。

    轨迹字段：
        question        原始问题
        model           模型名
        tools_offered   提供给模型的工具名列表
        sql             实际执行的第一条 SQL（可能为 None）
        tool_calls      每次工具调用记录：[{name, arguments, ok, error}]
        result_columns  结果列名
        result_rows     结果行（二维列表，可 JSON 序列化）
        result_preview  给模型看的结果文本
        conclusion      最终结论
        latency_ms      端到端耗时
        usage           token 用量
        error           异常信息（正常为 None）
        df              DataFrame（仅内存中用，不写盘）
    """
    history = history or []
    trace = {
        "question": question,
        "model": MODEL,
        "tools_offered": [t["function"]["name"] for t in TOOLS],
        "sql": None,
        "tool_calls": [],
        "result_columns": [],
        "result_rows": [],
        "result_preview": "",
        "conclusion": "",
        "latency_ms": 0,
        "usage": {},
        "error": None,
        "df": None,
    }

    started = time.time()
    try:
        client = get_client()
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            *history,
            {"role": "user", "content": question},
        ]

        # 第一次调用：让模型决定是否生成 SQL
        response = client.chat.completions.create(
            model=MODEL, messages=messages, tools=TOOLS, temperature=0.1
        )
        msg = response.choices[0].message
        trace["usage"]["call1"] = _usage_dict(response)

        if not msg.tool_calls:
            trace["conclusion"] = msg.content or "模型没有生成查询。"
            trace["error"] = "no_tool_call"
            trace["latency_ms"] = int((time.time() - started) * 1000)
            return trace

        messages.append(msg)

        for tool_call in msg.tool_calls:
            raw_args = tool_call.function.arguments
            try:
                args = json.loads(raw_args)
            except Exception as e:
                args = {}
                trace["tool_calls"].append(
                    {"name": "run_sql", "arguments": raw_args, "ok": False, "error": f"参数解析失败：{e}"}
                )
                messages.append(
                    {"role": "tool", "tool_call_id": tool_call.id, "content": f"参数不是合法 JSON：{e}"}
                )
                continue

            sql = args.get("sql", "")
            if trace["sql"] is None:
                trace["sql"] = sql
            result_text, df = run_sql(sql)
            ok = df is not None
            trace["tool_calls"].append(
                {
                    "name": "run_sql",
                    "arguments": args,
                    "ok": ok,
                    "error": None if ok else result_text,
                }
            )
            if ok and trace["df"] is None:
                trace["df"] = df
                trace["result_columns"] = [str(c) for c in df.columns]
                trace["result_rows"] = _rows_for_json(df)
                trace["result_preview"] = result_text
            messages.append({"role": "tool", "tool_call_id": tool_call.id, "content": result_text})

        # 第二次调用：根据工具结果生成结论
        final_response = client.chat.completions.create(
            model=MODEL, messages=messages, temperature=0.3
        )
        trace["conclusion"] = final_response.choices[0].message.content or ""
        trace["usage"]["call2"] = _usage_dict(final_response)

    except Exception as e:  # 任何异常都落进轨迹，方便评测归因
        trace["error"] = f"{type(e).__name__}: {e}"

    trace["latency_ms"] = int((time.time() - started) * 1000)
    return trace


def trace_to_jsonable(trace):
    """去掉 DataFrame，得到可以写进 JSON 的轨迹。"""
    out = {k: v for k, v in trace.items() if k != "df"}
    return out
