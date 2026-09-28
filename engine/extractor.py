"""engine/extractor.py — 自然语言 → DecisionRequest。

两条路径：
1. **LLM 路径**（use_llm=True 且 mxagent 可用）：调 mxagent 结构化抽取，
   结果过 schema.py 容错解析 + schema 校验。
2. **正则兜底路径**（降级 / use_llm=False）：纯 stdlib 正则抽取，
   **必须能吃下 demo/cases 里全部案例**（含"今晚健身房跑步 vs 楼下火锅 vs 在家看电影"那条）。

兜底路径的产品质量要求（不可妥协）：
- 抽不出选项时也要给出结构合法的 DecisionRequest + 有用的 missing_info；
- extracted_ok=False 表示走了兜底（前端可提示"未启用 AI 理解"）。
"""

from __future__ import annotations

import re
from typing import Any

from .models import CATEGORIES, DecisionRequest, Option

# ---------------------------------------------------------------- 关键词表

_CATEGORY_RULES = [
    ("dance", ("跳舞", "舞蹈", "舞室", "练舞", "街舞", "芭蕾", "爵士舞", "摇摆舞")),
    ("workout", ("健身", "跑步", "游泳", "瑜伽", "爬山", "撸铁", "运动", "锻炼",
                 "跑步机", "健身房")),
    ("theme_park", ("迪士尼", "乐园", "游乐", "环球影城", "方特", "长隆", "欢乐谷")),
    ("food", ("火锅", "烧烤", "吃饭", "吃餐", "下馆子", "小馆子", "甜品", "面包店",
              "夜宵", "宵夜", "料理", "餐厅", "馆子", "吃的")),
    ("social", ("朋友", "聚", "社交", "约会", "见人", "串门", "拜访")),
    ("rest", ("睡", "休息", "躺", "补觉", "宅", "放空", "歇")),
    ("study", ("学习", "看书", "上课", "作业", "复习", "琴行", "吉他", "烘焙班",
               "课程", "培训", "考试", "自习")),
]

_WORKOUT_WORDS = ("跳舞", "舞蹈", "健身", "撸铁", "运动", "锻炼", "跑步", "游泳",
                  "瑜伽", "爬山", "球")

#: 选项引导词（"方案一/方案1/选项A/去…/还是…"）
_OPT_LEAD_RE = re.compile(
    r"^\s*(?:方案|选项|选择|第)?\s*[一二三四五六123456ABCabc]\s*[)）\.、:：是]?\s*")

#: 强分隔符（切选项）
_SPLIT_RE = re.compile(
    r"[，,。；;！!？?~\n]+|\s{2,}|(?:或者|还是|要么|vs\.?|VS|对比|相比较)")

#: "纠结点"引导词
_CONCERN_LEAD = ("但", "但是", "不过", "可是", "然而", "就是", "只是", "担心",
                 "怕", "纠结", "顾虑", "缺点是", "问题", "麻烦")

#: 时间表达
_WHEN_RE = re.compile(
    r"(今晚|今晚上|今天晚上|明天晚上|明天|后天|周六下午|周六|周日|星期天|周末|"
    r"本周|这周|下周|中午|下午|晚上|上午|早上|凌晨|\d{1,2}\s*[:：点]\s*\d{1,2})")
_WHEN_WORDS = ("今晚", "今晚上", "今天晚上", "明天晚上", "明天", "后天",
               "周六下午", "周六", "周日", "星期天", "周末", "本周", "这周",
               "下周", "中午", "下午", "晚上", "上午", "早上", "凌晨")

_NUM_RE = re.compile(r"(\d+(?:\.\d+)?)")
_COMMUTE_RE = re.compile(
    r"(?:单程|通勤|路程|距离|车程|路上|过去)[^0-9]{0,8}(\d+(?:\.\d+)?)\s*"
    r"(?:分钟|分|min|mins|minutes|小时|个钟头|h)?")
_COMMUTE_WALK_RE = re.compile(r"步行\s*(\d+)\s*分钟")
_COST_RE = re.compile(
    r"(?:门票|价钱|价格|花费|开销|费用|人均|预算|要|得|需|大概|估计)?\s*"
    r"(\d+(?:\.\d+)?)\s*(?:元|块|块钱|rmb|RMB|¥|￥)")
_DURATION_RE = re.compile(
    r"(?:要|得|需|大概|估计|持续|花)?\s*(\d+(?:\.\d+)?)\s*"
    r"(?:个)?\s*(?:小时|钟头|h|H)(?:半|\+30)?")
_DURATION_MIN_RE = re.compile(
    r"(?:要|得|需|大概|估计|持续|花)?\s*(\d+)\s*(?:分钟|分|min|mins)(?!钟)")

_START_RE = re.compile(
    r"(\d{1,2})\s*[:：点]\s*(\d{1,2})|(\d{1,2})\s*点\s*(?:半|\d{1,2})?")
_DEADLINE_RE = re.compile(
    r"(\d{1,2})\s*[:：点]\s*(\d{1,2})\s*(?:之)?前")
#: 时间在前、死线词在后（"明天早上7点要起床" / "7点得起床上班"）。
#: 必须带情态词（必须/要/得…），否则 "18:30 出发" 这种**开始时间**会被误判成死线。
_DEADLINE_AFTER_RE = re.compile(
    r"(\d{1,2})\s*[:：点]\s*(\d{1,2})?\s*(?:左右|前后)?\s*"
    r"(?:必须|得要|需要|得|要|需)\s*"
    r"(?:起床|起床上班|上班|上岗|到公司|到单位|出门|出发|赶车|赶高铁|赶飞机)")
_BUDGET_RE = re.compile(
    r"(?:预算|不超过|不超|控制在|最多|至多|上限)[^0-9]{0,10}(\d+(?:\.\d+)?)\s*"
    r"(?:元|块|块钱)?")

#: 常见"顺带可做"引导
_AFTER_LEAD = ("结束", "下课", "散场", "逛完", "看完", "顺路", "顺便", "之后",
               "然后", "还能", "可以")


# ---------------------------------------------------------------- 工具

def _to_int(v, default=-1):
    try:
        if v is None:
            return default
        return int(float(v))
    except (TypeError, ValueError):
        return default


def _to_float(v, default=-1.0):
    try:
        if v is None:
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def _norm_category(text: str) -> str:
    t = str(text or "")
    for cat, words in _CATEGORY_RULES:
        if any(w in t for w in words):
            return cat
    return "other"


def _is_workout(text: str) -> bool:
    return any(w in str(text or "") for w in _WORKOUT_WORDS)


def _clean_label(text: str) -> str:
    """把一段话压成简短选项名（<=16 字）。

    只取第一个逗号/分号之前的"动作 + 对象"部分，
    后面的通勤/花费/纠结点描述都归到对应字段，不进 label。
    """
    s = str(text or "").strip()
    # 0) 剥掉开头背景（"今晚二选一：" / "晚饭吃什么：" / "周六下午干点啥："）
    s = re.sub(r"^[^，,。；;：:！!？?\n]{0,16}[：:]\s*", "", s)
    s = re.sub(r"^(?:今晚|今天晚上|明天|后天|周六|周日|周末|这周|本周|下周|"
               r"中午|下午|晚上|上午|早上)?\s*(?:二选一|三选一|选一个|挑一个|"
               r"纠结|难以决定)\s*[：:，,]?\s*", "", s)
    # 1) 只取第一个分隔符之前的部分
    s = re.split(r"[，,。；;！!？?\n]", s, maxsplit=1)[0]
    # 2) 去掉引导词（"方案一" / "还是" / "或者"）
    s = _OPT_LEAD_RE.sub("", s)
    s = re.sub(r"^(?:还是|或者|要么)\s*", "", s)
    # 3) 去掉纠结点尾巴
    for lead in _CONCERN_LEAD:
        idx = s.find(lead)
        if idx > 1:
            s = s[:idx]
            break
    # 4) 去掉括号补充（"（新爱好、但担心…）"），含未闭合的半截括号
    s = re.sub(r"[（(][^）)]{0,40}[）)]?\s*$", "", s)
    s = s.strip(" （(【[）)]）。，,；;：:！!？?的呢吧啊呀~～")
    if len(s) > 16:
        s = s[:16].rstrip() + "…"
    return s


def _extract_concerns(segment: str) -> list[str]:
    """从一段话里抽纠结点（"但太远了" → ["太远了"]）。"""
    out: list[str] = []
    s = str(segment or "")
    for lead in _CONCERN_LEAD:
        start = 0
        while True:
            idx = s.find(lead, start)
            if idx < 0:
                break
            tail = s[idx + len(lead):]
            # 纠结点到下一个分隔符为止
            m = re.split(r"[，,。；;！!？?\n]", tail, maxsplit=1)
            frag = (m[0] if m else "").strip(" 、的呢吧啊呀~～")
            # 去掉括号残留与过长碎片
            frag = re.sub(r"[（(][^）)]{0,40}$", "", frag).strip(" （)）】]")
            if 2 <= len(frag) <= 30:
                out.append(frag)
            start = idx + len(lead)
    # 去重 + 去掉被其他条目包含的短条目
    seen = set()
    uniq = []
    for c in out:
        if c not in seen:
            seen.add(c)
            uniq.append(c)
    return [c for c in uniq if not any(c != o and c in o for o in uniq)][:4]


def _extract_after(segment: str) -> list[str]:
    """抽"结束后顺带可做"。"""
    out: list[str] = []
    s = str(segment or "")
    for lead in _AFTER_LEAD:
        idx = s.find(lead)
        if idx < 0:
            continue
        tail = s[idx:]
        m = re.split(r"[，,。；;！!？?\n]", tail, maxsplit=1)
        frag = (m[0] if m else "").strip(" 、的呢吧啊呀~～")
        if 3 <= len(frag) <= 40:
            out.append(frag)
    seen = set()
    uniq = []
    for a in out:
        if a not in seen:
            seen.add(a)
            uniq.append(a)
    return uniq[:3]


def _extract_commute(segment: str) -> int:
    s = str(segment or "")
    m = _COMMUTE_WALK_RE.search(s)
    if m:
        return _to_int(m.group(1), -1)
    m = _COMMUTE_RE.search(s)
    if m:
        v = _to_float(m.group(1), -1.0)
        if v < 0:
            return -1
        # "半小时" → 30 分钟
        if re.search(r"小时|钟头|\bh\b", s[m.start():m.end() + 2]):
            return int(v * 60)
        return int(v)
    return -1


def _extract_cost(segment: str) -> float:
    """抽花费（元）。只采信带货币单位或明确价格语的数字。"""
    s = str(segment or "")
    # 优先：明确货币单位
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:元|块|块钱|rmb|RMB|¥|￥)", s)
    if m:
        v = _to_float(m.group(1), -1.0)
        if v > 0:
            return v
    # 其次：价格语（"人均120" / "门票299" / "预算300"）
    m = re.search(r"(?:人均|门票|票价|花费|开销|费用|预算|价格|价钱)\s*"
                  r"(?:大概|估计|约|要|得|需)?\s*(\d+(?:\.\d+)?)", s)
    if m:
        v = _to_float(m.group(1), -1.0)
        if v > 0:
            return v
    return -1.0


def _extract_duration(segment: str) -> int:
    """抽预计时长（分钟）。必须排除"通勤 60 分钟"这种路程描述。"""
    s = str(segment or "")
    # 先把"单程/通勤/路程/车程 N 分钟"从时长匹配里排除掉
    s_wo_commute = re.sub(
        r"(?:单程|通勤|路程|距离|车程|路上|过去|步行|地铁|公交|开车)\s*\d+(?:\.\d+)?\s*"
        r"(?:分钟|分|min|mins|小时|个钟头|h)", " ", s)
    m = _DURATION_RE.search(s_wo_commute)
    if m:
        v = _to_float(m.group(1), -1.0)
        if v > 0:
            base = int(v * 60)
            if "半" in s_wo_commute[m.start():m.end() + 2]:
                base += 30
            return base
    m = _DURATION_MIN_RE.search(s_wo_commute)
    if m:
        v = _to_int(m.group(1), -1)
        if v > 0:
            return v
    return -1


def _extract_start(segment: str) -> str:
    s = str(segment or "")
    m = _START_RE.search(s)
    if not m:
        return ""
    if m.group(1) and m.group(2):
        h, mi = int(m.group(1)), int(m.group(2))
    elif m.group(3):
        h = int(m.group(3))
        tail = s[m.end():m.end() + 3]
        mi = 30 if tail.strip().startswith("半") else 0
        m2 = re.match(r"\s*(\d{1,2})", tail)
        if m2:
            mi = int(m2.group(1))
    else:
        return ""
    if not (0 <= h < 24 and 0 <= mi < 60):
        return ""
    return f"{h:02d}:{mi:02d}"


def _extract_place(segment: str, label: str) -> str:
    """抽地点名（楼下那家火锅店 / 健身房 / 迪士尼…）。找不到返回空串。"""
    s = str(segment or "")
    # 1) "XX店" / "XX馆" / "健身房" 这类命名实体
    m = re.search(r"([一-龥A-Za-z0-9]{2,8}(?:店|馆|园|院|场|厅|室|部|中心|广场|"
                  r"商场|大厦|机构|公司|学校|大学|健身房|舞室|琴行))", s)
    if m:
        return m.group(1)
    # 2) "去X跑步/跳舞/上课/玩" → X
    m = re.search(r"(?:去|在|到)([一-龥A-Za-z0-9]{2,8})(?:跑步|健身|游泳|瑜伽|爬山|"
                  r"撸铁|运动|跳舞|舞蹈|上课|玩|吃|逛|聚餐|见面|训练|培训)", s)
    if m:
        return m.group(1)
    # 3) 从 label 里挖（"去健身房跑步" → "健身房"）
    m = re.search(r"([一-龥A-Za-z0-9]{2,8}(?:机构|公司|学校|健身房|舞室|琴行|店|馆))",
                  str(label or ""))
    if m:
        return m.group(1)
    return ""


def _social_value(segment: str, category: str) -> int:
    s = str(segment or "")
    v = 5
    if any(w in s for w in ("好朋友", "最好的朋友", "闺蜜", "兄弟", "哥们", "老友",
                            "能见到", "会遇到", "一起", "有人陪", "聚")):
        v = 8
    elif any(w in s for w in ("朋友", "见人", "社交")):
        v = 7
    if any(w in s for w in ("一个人", "独自", "单身去", "没人陪", "孤单", "有点尬")):
        v -= 3
    if category == "rest":
        v = min(v, 4)
    return max(0, min(10, v))


def _pleasure(segment: str, category: str) -> int:
    s = str(segment or "")
    v = 5
    for w, delta in (("很期待", 3), ("超期待", 3), ("想去", 2), ("想去试", 2),
                     ("好想", 2), ("喜欢", 2), ("开心", 2), ("爽", 2),
                     ("无聊", -2), ("没意思", -2), ("累", -1), ("纠结", -1),
                     ("三分钟热度", -2), ("担心", -1), ("怕", -1)):
        if w in s:
            v += delta
    if category == "rest":
        v += 1
    return max(0, min(10, v))


def _health_load(segment: str, category: str) -> int:
    s = str(segment or "")
    v = 5
    if category == "dance":
        v = 5
    elif category == "theme_park":
        v = 7
    elif category == "rest":
        v = 2
    elif category == "food":
        v = 4
    for w, delta in (("太远", 1), ("累", 1), ("疲惫", 2), ("熬夜", 2),
                     ("加班", 1), ("舒服", -2), ("轻松", -2), ("省力", -2)):
        if w in s:
            v += delta
    return max(0, min(10, v))


# ---------------------------------------------------------------- 分段

def _split_options(text: str) -> list[str]:
    """把用户输入切成候选选项段。

    策略（比逐逗号切更接近语义）：
    1. 剥掉开头背景/求助前缀（"今晚好纠结：" / "晚饭吃什么："）。
    2. 按**强分隔符**切：分号、句号、问号、叹号、换行，以及"还是 / 或者 / 要么"。
    3. 若某段开头不是"动作短语"（去/吃/跳/回/躺/报…），
       把它并回上一段（它是上一个选项的续述，如"做完还要洗碗"）。
    4. 过滤纯约束段（"我预算不超过300"）与重复段。

    例：
      "方案一去健身房跑步，单程通勤40分钟；方案二去楼下那家火锅店，步行5分钟；方案三在家看电影"
      → ["方案一去健身房跑步，单程通勤40分钟", "方案二去楼下那家火锅店，步行5分钟", "方案三在家看电影"]
    """
    raw = str(text or "").strip()
    if not raw:
        return []

    # ---- 1) 剥开头背景/求助前缀（只在第一个分隔符之前剥） ----
    head_m = re.match(r"^([^，,。；;：:！!？?\n]*)([：:，,])", raw)
    if head_m and re.search(
            r"(好纠结|纠结|怎么办|怎么选|选哪个|干点啥|吃什么|做点什么|"
            r"求推荐|干嘛|干啥|难以决定|无法决定|给点建议)", head_m.group(1)):
        raw = raw[head_m.end():]

    # ---- 2) 按强分隔符 + "还是/或者/要么" 切 ----
    # 顿号"、"在括号外也是选项分隔符（"吉他课、烘焙班、躺平"），
    # 但括号内的"、"是补充说明，不能切 → 先把括号内容临时保护起来。
    protected: list[str] = []

    def _protect(m):
        protected.append(m.group(0))
        return f"\x00{len(protected) - 1}\x00"

    masked = re.sub(r"[（(][^）)]{0,60}[）)]", _protect, raw)
    pieces = re.split(r"[。；;！!？?\n、]+|(?:还是|或者|要么)(?=\s*\S)", masked)

    def _restore(p: str) -> str:
        return re.sub(r"\x00(\d+)\x00",
                      lambda m: protected[int(m.group(1))], p)

    pieces = [_restore(p).strip(" ，,、;；") for p in pieces
              if p and _restore(p).strip(" ，,、;；")]

    # ---- 3) 合并"非动作开头"的续述段 ----
    _LEAD = r"^(?:方案|选项|选择|第)?\s*[一二三四五六123456]?\s*[)）\.、:：]?\s*"
    _VERB = (r"(?:去|来|回|到|在|吃|喝|跳|学|上|报|逛|看|玩|睡|躺|宅|休息|补觉|"
             r"见|约|聚|做|跑|游|唱|写|读|练|试|体验|参加|出席|待|窝|"
             r"迪士尼|火锅|烧烤|甜品|面包|吉他|烘焙|跳舞|舞蹈|健身|跑步|游泳|"
             r"瑜伽|爬山|撸铁|琴行|乐园|电影院|电影)")
    _OPT_START = re.compile(_LEAD + r"(?:" + _VERB + r")")

    def _starts_with_action(p: str) -> bool:
        s = _OPT_LEAD_RE.sub("", str(p or ""))
        return bool(_OPT_START.match(s))

    merged: list[str] = []
    for p in pieces:
        if _is_constraint_sentence(p):
            # 约束/背景句单独成段（后面会被 _is_constraint_sentence 过滤掉，
            # 但先切开才不会污染上一个选项的属性）
            merged.append(p)
        elif merged and not _starts_with_action(p):
            # 续述段：并回上一段（"做完还要洗碗" 属于"回家自己做"）
            merged[-1] = merged[-1] + "，" + p
        else:
            merged.append(p)

    # ---- 4) 过滤垃圾段 + 去重 ----
    def _junk(p: str) -> bool:
        if len(p) < 2:
            return True
        if p in _WHEN_WORDS:
            return True
        if re.fullmatch(r"[\d:：点分前之前必须不超过预算元块个半小钟头\s]+", p):
            return True
        return False

    out = [b for b in merged if not _junk(b)]
    seen = set()
    uniq = []
    for b in out:
        key = re.sub(r"\W", "", b)[:12]
        if key and key not in seen:
            seen.add(key)
            uniq.append(b)
    return uniq


def _is_constraint_sentence(segment: str) -> bool:
    """判断一段话是不是"纯约束/纯背景"而不是选项。

    例："我预算今晚不超过300元" / "必须22:30前到家" / "选哪个好？"
    """
    s = str(segment or "").strip()
    if not s:
        return True
    # 疑问/求助尾巴
    if re.search(r"(选哪个|怎么选|哪个好|怎么办|求推荐|纠结死了|好纠结)\s*[？?]?\s*$", s) \
            and not re.search(r"[去吃跳学玩睡见逛报做]", s):
        return True
    # 纯约束句：含预算/必须/不超过，且没有动作动词
    if re.search(r"(预算|必须|不超过|不超|控制在|最多|至多|上限)", s) \
            and not re.search(r"(去|吃|跳|学|玩|睡|见|逛|报|做|回|躺|跑|游|唱|写|读|练|试|体验|参加|聚|约)", s):
        return True
    # 纯时间/死线句
    if re.search(r"(之前|以前)?.{0,6}(必须|要|得).{0,6}(到家|回家|回去)", s) \
            and not re.search(r"(去|吃|跳|学|玩|睡|见|逛|报|做|回|躺)", s):
        return True
    # 太短且没有动作
    if len(s) <= 4 and not re.search(r"[去吃跳学玩睡见逛报做回躺]", s):
        return True
    return False


def _looks_like_option(segment: str) -> bool:
    """判断一段话是否是"选项"而不是背景/约束描述。"""
    s = str(segment or "")
    if len(s) < 2:
        return False
    if _is_constraint_sentence(s):
        return False
    # 必须是"动作 + 对象"或含明确活动词
    if not re.search(r"(去|来|回|到|吃|喝|跳|学|上|报|逛|看|玩|睡|躺|宅|休息|补觉|"
                     r"见|约|聚|做|跑|游|唱|写|读|练|试|体验|参加|出席|待|窝|"
                     r"迪士尼|火锅|烘焙|吉他|跳舞|舞蹈|健身|跑步|游泳|瑜伽|爬山|"
                     r"撸铁|电影)", s):
        return False
    return True


# ---------------------------------------------------------------- 主入口

def extract_heuristic(text: str) -> DecisionRequest:
    """正则兜底抽取（确定性，不调 LLM）。"""
    raw = str(text or "").strip()
    if not raw:
        return DecisionRequest(raw_text="", when="", options=[],
                               hard_constraints=[], missing_info=[
                                   "输入为空：请告诉你在纠结什么",
                                   "请分别列出 2~4 个候选选项",
                               ], extracted_ok=False)

    # ---- when ----
    when = ""
    m = _WHEN_RE.search(raw)
    if m:
        when = m.group(1)

    # ---- 硬约束（自然语言字符串，原样保留） ----
    hard: list[str] = []
    for seg in re.split(r"[，,。；;！!？?\n]", raw):
        seg = seg.strip()
        if not seg:
            continue
        if _DEADLINE_RE.search(seg) and re.search(r"(到家|回家|回去|归)", seg):
            hard.append(seg)
        elif _DEADLINE_AFTER_RE.search(seg) and re.search(
                r"(起床|起床上班|上班|到公司|到单位|出门|出发|赶车|赶高铁|赶飞机)", seg):
            hard.append(seg)
        elif _BUDGET_RE.search(seg):
            hard.append(seg)
        elif re.search(r"(不能|禁止|不宜|过敏|忌口)", seg) and len(seg) <= 40:
            hard.append(seg)
    # 去重
    seen = set()
    hard = [h for h in hard if not (h in seen or seen.add(h))]

    # ---- 选项 ----
    segments = _split_options(raw)
    # 把"纯约束/纯背景"段剔掉（"我预算今晚不超过300元" 不是选项）
    segments = [s for s in segments if not _is_constraint_sentence(s)]
    options: list[Option] = []
    for i, seg in enumerate(segments[:8], start=1):
        if not _looks_like_option(seg):
            continue
        label = _clean_label(seg)
        if len(label) < 2:
            continue
        cat = _norm_category(seg)
        options.append(Option(
            id=f"opt{i}",
            label=label,
            category=cat,
            start_hint=_extract_start(seg),
            duration_min=_extract_duration(seg),
            place=_extract_place(seg, label),
            commute_min=_extract_commute(seg),
            cost_cny=_extract_cost(seg),
            social_value=_social_value(seg, cat),
            pleasure=_pleasure(seg, cat),
            health_load=_health_load(seg, cat),
            workout=_is_workout(seg),
            depends_on=[],
            concerns=_extract_concerns(seg),
            after=_extract_after(seg),
            notes="",
        ))

    # ---- missing_info ----
    missing: list[str] = []
    if not options:
        missing.append("没能识别出候选选项，请分别列出你在纠结的几个选项")
    else:
        for o in options:
            if o.commute_min < 0:
                missing.append(f"{o.label} 的通勤时间")
            if o.cost_cny < 0:
                missing.append(f"{o.label} 的花费")
            if not o.start_hint:
                missing.append(f"{o.label} 的开始时间")
    if not hard:
        missing.append("有没有预算上限或必须到家的时间（补充后能拦住不合适的选项）")
    # 去重 + 限量
    seen = set()
    uniq = []
    for x in missing:
        if x not in seen:
            seen.add(x)
            uniq.append(x)

    return DecisionRequest(raw_text=raw, when=when, options=options,
                           hard_constraints=hard, missing_info=uniq[:6],
                           extracted_ok=False)


# ---------------------------------------------------------------- LLM 路径

_EXTRACT_TEMPLATE = """你是"PickOne"App 的选项抽取器。请从用户输入中抽取结构化信息。

{task}

用户原话：
---
{raw}
---

要求：
1. 只输出一个 JSON 对象，不要 markdown 代码块，不要任何解释文字。
2. options 列出用户纠结的**每一个**候选选项（2~6 个），label 用中文短名（<=20字）。
3. category 只能取：dance / workout / theme_park / food / social / rest / study / other
   （workout = 运动健身类：跑步/健身/游泳/瑜伽/爬山/撸铁；跳舞归 dance）。
4. 数值字段：用户明确说了才填，没说就填 -1（duration_min/commute_min/cost_cny）。
   social_value / pleasure / health_load 是 0~10 的整数主观估计，必填。
5. workout：该选项是否算一次锻炼（跳舞/健身/跑步等）。
6. concerns：用户对这个选项提到的纠结点（原话摘录，如"太远太累"）。
7. after：用户提到"结束后顺带可做"的事（如"附近好吃的"）。
8. hard_constraints：用户说出的硬约束**原话**（如"22:30 前必须到家"、"预算不超过300"），
   保持自然语言，不要结构化。
9. missing_info：你认为缺失的关键信息（如"各选项的开始时间"）。
10. when：用户说的时间（"今晚"/"周六下午"），没有则空字符串。

JSON 结构：
{{"when":"","options":[{{"label":"","category":"other","start_hint":"","duration_min":-1,"place":"","commute_min":-1,"cost_cny":-1,"social_value":5,"pleasure":5,"health_load":5,"workout":false,"concerns":[],"after":[],"notes":""}}],"hard_constraints":[],"missing_info":[]}}"""


def _request_from_llm(obj: dict, raw_text: str) -> DecisionRequest:
    """把 LLM 输出的 dict 转成 DecisionRequest（容忍脏数据）。"""
    opts: list[Option] = []
    for i, o in enumerate(obj.get("options") or []):
        if not isinstance(o, dict):
            continue
        label = str(o.get("label") or "").strip()
        if not label:
            continue
        cat = str(o.get("category") or "").strip()
        if cat not in CATEGORIES:
            cat = _norm_category(label + " " + str(o.get("notes") or ""))
        opts.append(Option(
            id=str(o.get("id") or f"opt{i + 1}"),
            label=label[:40],
            category=cat,
            start_hint=str(o.get("start_hint") or "")[:10],
            duration_min=_to_int(o.get("duration_min"), -1),
            place=str(o.get("place") or "")[:30],
            commute_min=_to_int(o.get("commute_min"), -1),
            cost_cny=_to_float(o.get("cost_cny"), -1.0),
            social_value=_to_int(o.get("social_value"), 5),
            pleasure=_to_int(o.get("pleasure"), 5),
            health_load=_to_int(o.get("health_load"), 5),
            workout=bool(o.get("workout", False)),
            depends_on=[str(x) for x in (o.get("depends_on") or [])
                        if isinstance(x, (str, int))],
            concerns=[str(x)[:40] for x in (o.get("concerns") or [])
                      if isinstance(x, (str, int)) and str(x).strip()],
            after=[str(x)[:40] for x in (o.get("after") or [])
                   if isinstance(x, (str, int)) and str(x).strip()],
            notes=str(o.get("notes") or "")[:120],
        ))
    hard = [str(x)[:80] for x in (obj.get("hard_constraints") or [])
            if isinstance(x, (str, int)) and str(x).strip()]
    missing = [str(x)[:80] for x in (obj.get("missing_info") or [])
               if isinstance(x, (str, int)) and str(x).strip()]
    when = str(obj.get("when") or "")[:20]
    if not opts:
        return None  # type: ignore[return-value]
    return DecisionRequest(raw_text=raw_text, when=when, options=opts,
                           hard_constraints=hard, missing_info=missing,
                           extracted_ok=True)


def extract(text: str, use_llm: bool = True) -> DecisionRequest:
    """自然语言 → DecisionRequest。**任何情况都不抛异常。**

    use_llm=True 且 mxagent 可用 → LLM 抽取；失败自动回落到正则兜底。
    """
    raw = str(text or "")
    if use_llm:
        try:
            from . import mxcli

            if mxcli.available():
                from . import schema as _schema

                task = _EXTRACT_TEMPLATE.format(
                    task=_schema.schema_prompt("request"), raw=raw[:4000])
                obj, res = mxcli.run_json(task, "request", level=0, timeout=120.0)
                if obj is not None and isinstance(obj, dict):
                    req = _request_from_llm(obj, raw)
                    if req is not None and req.options:
                        return req
        except Exception:
            pass
    return extract_heuristic(raw)


__all__ = ["extract", "extract_heuristic"]
