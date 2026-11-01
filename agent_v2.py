# -*- coding: utf-8 -*-
"""ReviewMind v2：把「固定两次调用」换成「while 循环 + 多工具」。

和 v1（agent_core.py）的差别只有四处，其余全部复用：
1. 结构：不是 第一次出 SQL → 第二次出结论，而是循环到模型自己说"我不需要再查了"
2. 每一轮都带 tools（v1 的第二次调用不带 tools，导致模型把工具调用写成正文）
3. 字段说明从提示词里搬进 list_tables 工具，口径规则从提示词里搬进 metric_glossary 工具
4. 新增 ask_clarification 工具：题目真的没法答时，允许它主动停下来问用户

刻意不做：上下文压缩、子 Agent、RAG、Web UI、长期记忆。
"""
import json
import time

from agent_core import MODEL, get_client, run_sql, SCHEMA_TEXT, _sanitize_conclusion

MAX_ROUNDS = 10

# ---------- 口径词典：v1 把这几条写在提示词里，v2 改成"要用的时候自己查" ----------
GLOSSARY = {
    "gmv": (
        "GMV / 销售额 / 收入：SUM(payment_value)，只统计 order_status = 'delivered'。",
        "payment_value 是订单级支付总额，已包含运费，不要再去加 freight_value。",
        "口径声明示例：口径：已签收订单的支付总额（含运费）。",
    ),
    "客单价": (
        "客单价 AOV = 该口径下的 GMV / 订单数（订单去重后的 COUNT）。",
        "做州级/类目级客单价时，分子分母必须各自按同一范围过滤后再相除。",
    ),
    "配送延迟": (
        "delivery_delay_days = 实际送达日期 - 预计送达日期，单位天。",
        "正数=迟到，负数=提前；平均延迟为负表示整体比承诺早到，这是正常现象。",
        "涉及延迟时必须排除 delivery_delay_days IS NULL 的订单。",
        "「延迟最严重」有歧义：可以指平均延迟天数最大，也可以指迟到订单占比最高，需说明采用哪种。",
    ),
    "评分": (
        "order_metrics.review_score 是订单级平均分（一个订单多条评论时取 AVG），因此会出现 1.5、2.5 这类小数。",
        "原始评论分只有 1–5 的整数；引用「评分分布」时如用整数档位，需说明排除了订单级均值与非空过滤。",
    ),
    "订单数": (
        "订单数一般统计全部状态（不按订单状态过滤），但关键字段为 NULL 的记录要排除。",
        "需要区分订单数与商品件数：件数在 order_item_metrics，一行 = 一个订单里的一件商品。",
    ),
    "取消": (
        "取消/未完成：order_status IN ('canceled','unavailable')。",
        "统计「取消率」时分子用这两种状态，分母用全部订单。",
    ),
    "数据范围": (
        "订单时间范围 2016-09-04 ~ 2018-10-17。",
        "2018-09 之后订单量断崖式下跌（当月仅 16 单），做趋势/同比时必须提示这是数据覆盖问题，不是业务下滑。",
    ),
    "类目": (
        "类目在 order_item_metrics.product_category，部分商品缺失类目（NULL）。",
        "按类目做收入 Top N 时，排除类目为 NULL 的行，并在结论里说明排除了多少。",
    ),
    "州": (
        "客户州：order_metrics.customer_state，两字母巴西州代码（SP、RJ、MG…）。",
        "每个州的订单量差异极大，Top N 里出现小样本州（如 PB、AC）时要主动提示样本量。",
    ),
}

# 同义词 → 词典 key，方便模型用人话查
_ALIAS = {
    "gmvs": "gmv", "销售额": "gmv", "收入": "gmv", "成交额": "gmv",
    "aov": "客单价", "平均订单": "客单价",
    "延迟": "配送延迟", "送达": "配送延迟", "物流时效": "配送延迟",
    "评价": "评分", "分数": "评分", "review": "评分",
    "销量": "订单数", "单量": "订单数", "件数": "订单数",
    "未完成": "取消", "失效": "取消",
    "时间": "数据范围", "年份": "数据范围", "区间": "数据范围",
    "品类": "类目", "category": "类目",
    "省份": "州", "state": "州",
}


def metric_glossary(metric):
    key = (metric or "").strip()
    low = key.lower()
    hit = None
    if low in GLOSSARY:
        hit = low
    for alias, target in _ALIAS.items():
        if alias in low or alias in key:
            hit = target
            break
    if hit is None:
        return "未收录该口径。可查：" + "、".join(GLOSSARY.keys())
    return "【" + hit + "】\n" + "\n".join(GLOSSARY[hit])


def list_tables():
    return SCHEMA_TEXT


# ---------- 工具说明书（v2 共 4 个） ----------
TOOLS_V2 = [
    {
        "type": "function",
        "function": {
            "name": "list_tables",
            "description": "查看数据库里有哪些视图、每个视图有哪些字段。不确定字段名时先调这个，不要猜。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "metric_glossary",
            "description": (
                "查指标口径（GMV、客单价、配送延迟、评分、订单数、取消、数据范围、类目、州）。"
                "凡是涉及金额、延迟、评分、趋势的问题，先查口径再写 SQL。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "metric": {"type": "string", "description": "指标名或问题里的关键词，如「GMV」「配送延迟」"}
                },
                "required": ["metric"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_sql",
            "description": (
                "对 Olist 电商数据库执行只读 SQL。只能查 order_metrics（订单粒度）和 "
                "order_item_metrics（订单商品粒度）两个视图，只允许 SELECT 或 WITH。"
            ),
            "parameters": {
                "type": "object",
                "properties": {"sql": {"type": "string", "description": "要执行的 SELECT 或 WITH 语句"}},
                "required": ["sql"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ask_clarification",
            "description": (
                "当你确定这道题没法直接答：口径存在多种合理解释且结果差异很大、问的维度数据里根本没有、"
                "或时间范围无法确定时，调用它向用户提出具体问题。调用后本轮回答即结束。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "question_to_user": {"type": "string", "description": "要问用户的具体问题，并说明每个选项会怎么算"},
                    "reason": {"type": "string", "description": "为什么必须澄清，一句话"},
                },
                "required": ["question_to_user", "reason"],
            },
        },
    },
]

SYSTEM_PROMPT_V2 = """你是一个电商经营数据分析助手，可以多轮调用工具来回答用户的问题。

工作方式：
- 不确定字段名 → 先 list_tables
- 涉及金额、延迟、评分、趋势、数据范围 → 先 metric_glossary 查口径
- 需要数据 → 用 run_sql 查，一次只查你真正需要的东西
- 拿到的数据不够回答 → 继续查，最多 10 轮
- 题目本身没法直接答（口径有歧义且差异大、维度不存在、时间范围不明）→ 用 ask_clarification 提问，不要自己编一个口径硬答

输出要求：
- 先给结论，再给关键数字，中文，简洁
- 引用数字必须来自工具返回的结果，不要自己累加或推算
- 明确写出采用的口径，第一句以「口径：」开头（如果本题是澄清或指出缺字段，则不需要）
- 金额单位是巴西雷亚尔 BRL
- 不要暴露工具调用过程，也不要输出任何标记或代码块
"""


def _usage(resp):
    u = getattr(resp, "usage", None)
    if u is None:
        return {}
    return {
        "prompt_tokens": getattr(u, "prompt_tokens", 0) or 0,
        "completion_tokens": getattr(u, "completion_tokens", 0) or 0,
        "total_tokens": getattr(u, "total_tokens", 0) or 0,
    }


def _rows_for_json(df):
    if df is None:
        return [], []
    cols = [str(c) for c in df.columns]
    rows = []
    for rec in df.astype(object).where(df.notna(), None).values.tolist():
        rows.append([None if v is None else (v.item() if hasattr(v, "item") else v) for v in rec])
    return cols, rows


def _exec_tool(name, args, state):
    """执行工具。任何失败都降级成字符串回灌，绝不抛异常。"""
    try:
        if name == "list_tables":
            return list_tables()
        if name == "metric_glossary":
            return metric_glossary(args.get("metric", ""))
        if name == "run_sql":
            sql = args.get("sql", "")
            text, df = run_sql(sql)
            state["sql"] = sql
            if df is not None:
                state["df"] = df
                state["ok_query"] = True
            return text
        if name == "ask_clarification":
            state["clarify"] = {
                "question_to_user": args.get("question_to_user", ""),
                "reason": args.get("reason", ""),
            }
            return "已向用户提问，本轮结束。"
        return f"错误：没有名为 {name} 的工具"
    except Exception as e:  # 工具内部崩了也要能继续
        return f"工具执行失败：{e}"


def ask_agent(question, history=None):
    t0 = time.time()
    trace = {
        "question": question,
        "model": MODEL,
        "agent": "v2",
        "tools_offered": [t["function"]["name"] for t in TOOLS_V2],
        "sql": None,
        "tool_calls": [],
        "result_columns": [],
        "result_rows": [],
        "result_preview": "",
        "conclusion": "",
        "latency_ms": 0,
        "usage": {},
        "error": None,
        "rounds": 0,
        "df": None,
    }
    state = {"sql": None, "df": None, "ok_query": False, "clarify": None}

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT_V2},
        {"role": "user", "content": question},
    ]

    try:
        client = get_client()
    except Exception as e:
        trace["error"] = "no_client"
        trace["conclusion"] = f"（无法连接模型：{e}）"
        trace["latency_ms"] = int((time.time() - t0) * 1000)
        return trace

    try:
        for turn in range(1, MAX_ROUNDS + 1):
            trace["rounds"] = turn
            resp = client.chat.completions.create(
                model=MODEL,
                messages=messages,
                tools=TOOLS_V2,
                temperature=0.1,
            )
            trace["usage"][f"call{turn}"] = _usage(resp)
            msg = resp.choices[0].message
            calls = getattr(msg, "tool_calls", None) or []

            if not calls:
                text, leaked = _sanitize_conclusion(msg.content or "")
                trace["conclusion"] = text
                if leaked:
                    trace["error"] = "tool_call_as_text"
                break

            messages.append({
                "role": "assistant",
                "content": msg.content or "",
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {"name": tc.function.name, "arguments": tc.function.arguments or "{}"},
                    }
                    for tc in calls
                ],
            })

            for tc in calls:
                name = tc.function.name
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except Exception:
                    args = {}
                out = _exec_tool(name, args, state)
                trace["tool_calls"].append({
                    "name": name,
                    "arguments": args,
                    "ok": not str(out).startswith(("错误", "工具执行失败", "SQL 执行失败")),
                    "error": None if not str(out).startswith(("错误", "工具执行失败", "SQL 执行失败")) else str(out)[:200],
                })
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": str(out)})

            if state["clarify"]:
                c = state["clarify"]
                trace["conclusion"] = (
                    f"需要先确认口径：{c['question_to_user']}\n（原因：{c['reason']}）"
                )
                break
        else:
            trace["error"] = "max_rounds"
            trace["conclusion"] = "（已达到最大工具轮次，未能给出结论）"

    except Exception as e:
        trace["error"] = f"exception: {type(e).__name__}"
        trace["conclusion"] = f"（执行出错：{e}）"

    if state["sql"]:
        trace["sql"] = state["sql"]
    if state["df"] is not None:
        cols, rows = _rows_for_json(state["df"])
        trace["result_columns"] = cols
        trace["result_rows"] = rows
        trace["result_preview"] = state["df"].head(20).to_string(index=False)
        trace["df"] = state["df"]

    if not state["ok_query"] and not state["clarify"] and trace["error"] is None:
        trace["error"] = "no_tool_call"

    trace["latency_ms"] = int((time.time() - t0) * 1000)
    return trace
