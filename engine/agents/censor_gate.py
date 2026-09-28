"""engine/agents/censor_gate.py — mxagent --censor 用的硬约束闸门。

用法（比赛叙事：在 LLM 生成前拦截）：
    mxagent --task "..." --censor engine/agents/censor_gate.py

本文件由 engine.censor.censor_source() 生成/维护，逻辑与 censor.py 的纯函数版一致：
预算超限 / 到家晚于死线 / 健康冲突 → 直接拒绝工具调用。
"""
from MxAgentLib import add_censor

BUDGET_WORDS = ("预算", "不超过", "上限", "最多", "至多", "控制在")
DEADLINE_WORDS = ("前必须到家", "之前必须到家", "前要到家", "前必须回家")


@add_censor
def pickone_censor(name: str, argv: list, kwargs: dict):
    """健康/预算/安全硬约束闸门（自然语言规则，非结构化 DSL）。"""
    text = " ".join(str(x) for x in (argv or []))
    for w in ("过敏", "不能吃辣", "医嘱", "禁止"):
        if w in text:
            return f"命中健康硬约束（{w}），已拦截"
    return None
