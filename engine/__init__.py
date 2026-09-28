"""PickOne — mxagent 决策引擎。

纯 stdlib 数据模型在任意解释器可导入；调用 mxagent 的部分必须用
/home/Developer/workspace/.venv/bin/python 运行。

模块划分（docs/ARCHITECTURE.md 第 2 节）：
    models      数据类（纯 stdlib）
    schema      LLM 结构化输出的 schema + 容错解析
    mxcli       mxagent CLI 子进程封装（唯一允许调 LLM 的地方）
    extractor   自然语言 → DecisionRequest
    scorer      确定性 7 维加权打分（不调 LLM）
    timeline    48h 后果推演（mxagent --level 2 委派）
    censor      安全/健康/预算硬约束闸门
    explainer   唯一 GO 牌 + 只讲优点 + 置信度 + reasoning_tree
    advisor     编排入口 advise(request, profile) -> Decision
    profile     用户档案读写 + 满意度回灌
    cli         命令行自测入口
    agents/     mxagent config yaml + censor 源码
"""

from .models import (
    Option,
    DecisionRequest,
    Consequence,
    ScoredOption,
    Card,
    Decision,
    Profile,
    DIMENSIONS,
    DIM_LABELS,
    DEFAULT_WEIGHTS,
    WEIGHT_MIN,
    WEIGHT_MAX,
    LEARNING_RATE,
    EPSILON,
    CATEGORIES,
)

__all__ = [
    "Option",
    "DecisionRequest",
    "Consequence",
    "ScoredOption",
    "Card",
    "Decision",
    "Profile",
    "DIMENSIONS",
    "DIM_LABELS",
    "DEFAULT_WEIGHTS",
    "WEIGHT_MIN",
    "WEIGHT_MAX",
    "LEARNING_RATE",
    "EPSILON",
    "CATEGORIES",
    "advise",
    "extract",
    "load_profile",
    "save_profile",
    "reset_profile",
    "log_activity",
    "apply_feedback",
]

__version__ = "0.2.0"


def advise(*args, **kwargs):
    """延迟导入 advisor，保证 models/schema/scorer 在无 LLM 依赖时可用。"""
    from .advisor import advise as _advise

    return _advise(*args, **kwargs)


def extract(*args, **kwargs):
    """延迟导入 extractor。"""
    from .extractor import extract as _extract

    return _extract(*args, **kwargs)


def load_profile(*args, **kwargs):
    from .profile import load_profile as _f

    return _f(*args, **kwargs)


def save_profile(*args, **kwargs):
    from .profile import save_profile as _f

    return _f(*args, **kwargs)


def reset_profile(*args, **kwargs):
    from .profile import reset_profile as _f

    return _f(*args, **kwargs)


def log_activity(*args, **kwargs):
    from .profile import log_activity as _f

    return _f(*args, **kwargs)


def apply_feedback(*args, **kwargs):
    from .profile import apply_feedback as _f

    return _f(*args, **kwargs)