"""engine/schema.py — LLM 结构化输出的 schema 定义 + 容错解析。

设计约束（docs/ARCHITECTURE.md 第 3.1 / 6 节）：
- mxagent 非交互输出前有 "Assistant :" 行，终端会把长 JSON 折行并在右侧补空格，
  所以解析前必须逐行 rstrip 并去掉 ```json 围栏。
- 解析失败绝不抛异常：返回 None，由调用方走降级路径。
- 纯 stdlib；jsonschema 存在则额外做一次严格校验（不存在则用内置轻量校验）。
"""

from __future__ import annotations

import json
import re
from typing import Any

# ---------------------------------------------------------------- 通用工具

_FENCE_RE = re.compile(r"^\s*```(?:json|JSON)?\s*$")
_ASSISTANT_RE = re.compile(r"^\s*Assistant\s*:\s*$")


def strip_terminal_noise(text: str) -> str:
    """去掉 mxagent CLI 输出里的终端噪音。

    - 逐行 rstrip（终端折行时右侧补空格）
    - 去掉 "Assistant :" 前缀行
    - 去掉 ```json / ``` 围栏行
    - **把字符串字面量内部的真实换行/制表符替换成空格**：mxagent 的 clean 样式会按
      终端宽度折行，LLM 输出的 JSON 字符串值中间会被插入真实 \\n，导致
      "Invalid control character" 而整体解析失败。这是实测最常见的失败原因。
    """
    if not text:
        return ""
    lines = []
    for raw in str(text).replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw.rstrip()
        if _ASSISTANT_RE.match(line):
            continue
        if _FENCE_RE.match(line):
            continue
        lines.append(line)
    joined = "\n".join(lines).strip()
    return _sanitize_control_chars_in_strings(joined)


def _sanitize_control_chars_in_strings(text: str) -> str:
    """把 JSON 字符串字面量内部的裸控制字符（\\n/\\t/\\r）换成空格。

    只处理"字符串内部"：结构性的换行（数组/对象之间）保留，便于
    _first_balanced_object 之类的兜底解析。
    """
    if not text:
        return text
    out = []
    in_str = False
    esc = False
    for ch in text:
        if in_str:
            if esc:
                out.append(ch)
                esc = False
                continue
            if ch == "\\":
                out.append(ch)
                esc = True
                continue
            if ch == '"':
                out.append(ch)
                in_str = False
                continue
            if ch in "\n\r\t":
                out.append(" ")
                continue
            out.append(ch)
            continue
        if ch == '"':
            out.append(ch)
            in_str = True
            continue
        out.append(ch)
    return "".join(out)


def _first_balanced_object(text: str) -> str | None:
    """从文本里抓第一个平衡的 {...}（跳过字符串字面量与转义）。"""
    if not text:
        return None
    start = -1
    depth = 0
    in_str = False
    esc = False
    for i, ch in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
            continue
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
            continue
        if ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start >= 0:
                    return text[start:i + 1]
    return None


def _first_balanced_array(text: str) -> str | None:
    """从文本里抓第一个平衡的 [...]。"""
    if not text:
        return None
    start = -1
    depth = 0
    in_str = False
    esc = False
    for i, ch in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
            continue
        if ch == "[":
            if depth == 0:
                start = i
            depth += 1
            continue
        if ch == "]":
            if depth > 0:
                depth -= 1
                if depth == 0 and start >= 0:
                    return text[start:i + 1]
    return None


def _visual_len(s: str) -> int:
    """终端视觉宽度（中文/全角算 2 列）。"""
    import unicodedata

    w = 0
    for ch in s:
        w += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return w


def _unwrap_terminal_wrap(text: str, max_col: int = 80) -> str:
    """把终端折行重新接起来（去掉折点插入的空格）。

    mxagent 的 clean 风格按终端宽度折行，**并在折点插入一个空格**：
        `"sta` + `rt_hint"`  →  key 被污染成 "sta rt_hint"
        `"顺路宵 ` + `夜"`    →  中文值被污染成 "顺路宵 夜"
    这个空格会破坏 schema 校验（key 对不上）与中文值的可读性。

    判定：某行视觉宽度接近 max_col（被折的）且不以自然边界结尾时，
    去掉折点空格后与下一行拼接。
    """
    if not text:
        return text
    lines = text.split("\n")
    if len(lines) <= 1:
        return text

    out: list[str] = []
    i = 0
    while i < len(lines):
        cur = lines[i]
        while (i + 1 < len(lines) and _visual_len(cur) >= max_col - 3
               and cur and cur[-1] not in ",]} \t:"
               and lines[i + 1].strip()):
            nxt = lines[i + 1]
            # 折点空格：终端在折行处补的那个空格。cur 已 rstrip，
            # 所以空格只可能"本该在 cur 末尾但被 rstrip 掉"或"在 nxt 开头"。
            # 两种情况都按"折点无空格"拼接。
            cur = cur + nxt.lstrip()
            i += 1
        out.append(cur)
        i += 1
    return "\n".join(out)


def _repair_spaces_in_strings(text: str) -> str:
    """去掉 JSON 字符串字面量内部、被终端折行插入的多余空格。

    只去掉"两侧都是非空格字符"的单个空格（折行插入的空格一定是这种形态），
    保留有意义的空格（如英文句子 "a b" 两侧至少一侧是边界的情况不在此列，
    但中文场景下这已经足够安全）。
    """
    if not text or " " not in text:
        return text
    out = []
    in_str = False
    esc = False
    n = len(text)
    for i, ch in enumerate(text):
        if in_str:
            if esc:
                out.append(ch)
                esc = False
                continue
            if ch == "\\":
                out.append(ch)
                esc = True
                continue
            if ch == '"':
                out.append(ch)
                in_str = False
                continue
            if ch == " ":
                prev = out[-1] if out else ""
                nxt = text[i + 1] if i + 1 < n else ""
                # 折行插入的空格：前后都是非边界字符 → 去掉
                if prev and nxt and prev not in ",]}: \t\n" and nxt not in ",]}: \t\n":
                    continue
            out.append(ch)
            continue
        if ch == '"':
            out.append(ch)
            in_str = True
            continue
        out.append(ch)
    return "".join(out)


def parse_json_loose(text: str) -> Any | None:
    """容错解析 LLM 输出 → Python 对象；失败返回 None（不抛异常）。

    顺序：整体 strip → 剥围栏 → 接终端折行 → 直接 json.loads
          → 抓第一个平衡 {...} → 抓 [...] → 去尾随逗号重试
    """
    if text is None:
        return None
    cleaned = strip_terminal_noise(text)
    if not cleaned:
        return None

    # 候选顺序：**修复版优先**。原因：mxagent clean 样式按终端宽度折行时会在折点
    # 插入一个空格，直接 json.loads 仍能成功（空格在字符串字面量内），但会把
    # "start_hint" 污染成 "sta rt_hint"、"commute_min" 污染成 "commut e_min"，
    # 导致 schema 校验失败 / 字段丢失。所以先用去空格版，失败再回落原样。
    unwrapped = _unwrap_terminal_wrap(cleaned)
    repaired = _repair_spaces_in_strings(cleaned)
    repaired2 = _repair_spaces_in_strings(unwrapped)

    candidates: list[str] = []
    for c in (repaired2, repaired, unwrapped, cleaned):
        if c and c not in candidates:
            candidates.append(c)

    for cand in candidates:
        # 1) 直接解析
        try:
            return json.loads(cand)
        except Exception:
            pass
        # 2) 抓第一个平衡对象
        obj = _first_balanced_object(cand)
        if obj:
            try:
                return json.loads(obj)
            except Exception:
                pass
            # 3) 去尾随逗号等常见问题再试
            fixed = re.sub(r",\s*([}\]])", r"\1", obj)
            try:
                return json.loads(fixed)
            except Exception:
                pass
        # 4) 抓第一个平衡数组
        arr = _first_balanced_array(cand)
        if arr:
            try:
                return json.loads(arr)
            except Exception:
                pass
    return None


# ---------------------------------------------------------------- schema 定义

#: DecisionRequest 的 response schema（extractor 用）
REQUEST_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["options"],
    "properties": {
        "when": {"type": "string"},
        "options": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["label"],
                "properties": {
                    "label": {"type": "string"},
                    "category": {
                        "type": "string",
                        "enum": ["dance", "workout", "theme_park", "food", "social",
                                 "rest", "study", "other"],
                    },
                    "start_hint": {"type": "string"},
                    "duration_min": {"type": "integer"},
                    "place": {"type": "string"},
                    "commute_min": {"type": "integer"},
                    "cost_cny": {"type": "number"},
                    "social_value": {"type": "integer", "minimum": 0, "maximum": 10},
                    "pleasure": {"type": "integer", "minimum": 0, "maximum": 10},
                    "health_load": {"type": "integer", "minimum": 0, "maximum": 10},
                    "workout": {"type": "boolean"},
                    "depends_on": {"type": "array", "items": {"type": "string"}},
                    "concerns": {"type": "array", "items": {"type": "string"}},
                    "after": {"type": "array", "items": {"type": "string"}},
                    "notes": {"type": "string"},
                },
            },
        },
        "hard_constraints": {"type": "array", "items": {"type": "string"}},
        "missing_info": {"type": "array", "items": {"type": "string"}},
    },
}

#: Timeline 推演结果的 response schema（timeline 用）
TIMELINE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["options"],
    "properties": {
        "options": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["label"],
                "properties": {
                    "label": {"type": "string"},
                    "effects": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "time": {"type": "string"},
                                "desc": {"type": "string"},
                                "impact": {"type": "integer", "minimum": -2, "maximum": 2},
                            },
                        },
                    },
                    "future_score": {"type": "integer", "minimum": 0, "maximum": 10},
                    "risk": {"type": "string"},
                },
            },
        },
    },
}

#: Explainer 主观权重估计的 response schema（explainer 用，数值必须 clamp）
WEIGHTS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "weights": {
            "type": "object",
            "properties": {
                "rhythm": {"type": "number"},
                "social": {"type": "number"},
                "commute": {"type": "number"},
                "cost": {"type": "number"},
                "pleasure": {"type": "number"},
                "health": {"type": "number"},
                "future": {"type": "number"},
            },
        },
        "notes": {"type": "array", "items": {"type": "string"}},
    },
}

#: 唯一 GO 牌的 response schema（explainer 用）
CARDS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "cards": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["kind", "title"],
                "properties": {
                    "kind": {"type": "string", "enum": ["GO", "SWITCH", "DROP"]},
                    "option_id": {"type": ["string", "null"]},
                    "title": {"type": "string"},
                    "why": {"type": "array", "items": {"type": "string"}},
                    "detail": {"type": "string"},
                },
            },
        },
        "most_uncertain": {"type": "string"},
        "ask_user": {"type": "string"},
    },
}

SCHEMAS = {
    "request": REQUEST_SCHEMA,
    "timeline": TIMELINE_SCHEMA,
    "weights": WEIGHTS_SCHEMA,
    "cards": CARDS_SCHEMA,
}


# ---------------------------------------------------------------- 校验

def _type_ok(value: Any, spec: str) -> bool:
    if spec == "object":
        return isinstance(value, dict)
    if spec == "array":
        return isinstance(value, list)
    if spec == "string":
        return isinstance(value, str)
    if spec == "boolean":
        return isinstance(value, bool)
    if spec == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if spec == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if spec == "null":
        return value is None
    return True


def _validate(value: Any, schema: Any, path: str = "$") -> list[str]:
    """极简 JSON Schema 子集校验（type/required/properties/items/enum/minimum/maximum）。

    返回错误列表；空列表 = 通过。jsonschema 可用时优先用它。
    """
    errors: list[str] = []
    if not isinstance(schema, dict):
        return errors

    stype = schema.get("type")
    if isinstance(stype, str):
        if not _type_ok(value, stype):
            errors.append(f"{path}: 期望 {stype}，实际 {type(value).__name__}")
            return errors
    elif isinstance(stype, list):
        if not any(_type_ok(value, t) for t in stype):
            errors.append(f"{path}: 类型不匹配 {stype}")
            return errors

    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: {value!r} 不在枚举 {schema['enum']} 内")

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{path}: {value} < minimum {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{path}: {value} > maximum {schema['maximum']}")

    if isinstance(value, dict):
        for key in schema.get("required", []) or []:
            if key not in value:
                errors.append(f"{path}: 缺必填字段 {key}")
        props = schema.get("properties") or {}
        for key, sub in props.items():
            if key in value:
                errors.extend(_validate(value[key], sub, f"{path}.{key}"))

    if isinstance(value, list) and isinstance(schema.get("items"), dict):
        for i, item in enumerate(value):
            errors.extend(_validate(item, schema["items"], f"{path}[{i}]"))

    return errors


def validate(value: Any, schema_name: str) -> tuple[bool, list[str], Any]:
    """按名字校验；返回 (ok, errors, value)。绝不抛异常。"""
    schema = SCHEMAS.get(schema_name)
    if schema is None:
        return True, [], value
    # 优先 jsonschema（venv 已装）
    try:
        import jsonschema  # type: ignore

        try:
            jsonschema.validate(value, schema)
            return True, [], value
        except jsonschema.ValidationError as e:  # type: ignore[attr-defined]
            return False, [f"$.{list(e.absolute_path)}: {e.message}"], value
        except Exception:
            pass
    except Exception:
        pass
    errs = _validate(value, schema)
    return (not errs), errs, value


def schema_prompt(schema_name: str) -> str:
    """生成给 LLM 的"只输出 JSON"提示片段。"""
    try:
        body = json.dumps(SCHEMAS[schema_name], ensure_ascii=False)
    except Exception:
        body = "{}"
    return ("请只输出一个 JSON 对象，不要 markdown 代码块，不要任何解释文字。"
            f"JSON 必须符合这个 schema：{body}")


__all__ = [
    "REQUEST_SCHEMA", "TIMELINE_SCHEMA", "WEIGHTS_SCHEMA", "CARDS_SCHEMA",
    "SCHEMAS", "parse_json_loose", "strip_terminal_noise", "validate",
    "schema_prompt",
]
