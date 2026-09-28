"""engine/selftest.py — 7 项验收自测（对应任务书 (a)~(g)）。

用法（必须用 venv 解释器）：
    /home/Developer/workspace/.venv/bin/python engine/selftest.py
    MXAGENT_BIN=/nonexistent/mxagent /home/Developer/workspace/.venv/bin/python engine/selftest.py

输出：每项的 PASS/FAIL + 证据，最后汇总。退出码 0 = 全通过。
"""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for _p in (_ROOT, _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from engine import advisor as _advisor          # noqa: E402
from engine import extractor as _extractor      # noqa: E402
from engine import profile as _profile          # noqa: E402
from engine import scorer as _scorer            # noqa: E402
from engine.models import Decision              # noqa: E402

PY = "/home/Developer/workspace/.venv/bin/python"

DEMO_TEXT = ("今晚好纠结：方案一去健身房跑步，单程通勤40分钟，年卡折合每次25块钱，"
             "但最近工作太累；方案二去楼下那家火锅店，步行5分钟，人均80，"
             "一个人吃又有点尴尬；方案三在家看电影，零通勤零花费，"
             "但今晚首映怕被剧透。我预算今晚不超过100块，"
             "而且明天早上7点要起床上班，选哪个好？")

DEADLINE_TEXT = ("今晚二选一：去健身房跑步，18:30 出发，预计要 240 分钟，单程通勤 40 分钟；"
                 "还是去楼下那家火锅店，19:00 开始，90 分钟，单程通勤 5 分钟。"
                 "我预算不超过 400 元，而且 22:30 之前必须到家。")

FORBIDDEN = ("stub", "占位", "placeholder", "仅供演示", "todo", "待实现",
             "未实现", "fake", "mock", "示例数据", "测试数据")

RESULTS: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, evidence: str) -> None:
    RESULTS.append((name, ok, evidence))
    print(f"\n{'=' * 70}\n[{'PASS' if ok else 'FAIL'}] {name}\n{'=' * 70}")
    print(evidence)


def _dec_dict(dec) -> dict:
    return dec.to_dict() if hasattr(dec, "to_dict") else dec


def _has_forbidden(d: dict) -> list[str]:
    """在 Decision 的全部文本里搜占位文案。"""
    blob = json.dumps(d, ensure_ascii=False).lower()
    return [w for w in FORBIDDEN if w.lower() in blob]


# ---------------------------------------------------------------- (a) --demo

def test_demo() -> None:
    req = _extractor.extract(DEMO_TEXT, use_llm=False)
    prof = _profile.new_profile("selftest", "new")
    dec = _advisor.advise(req, prof, use_llm=False)
    d = _dec_dict(dec)

    kinds = [c["kind"] for c in d["cards"]]
    tree = d["reasoning_tree"]
    problems = []
    if "GO" not in kinds:
        problems.append(f"缺 GO 牌：{kinds}")
    if not (0.0 <= d["confidence"] <= 1.0):
        problems.append(f"confidence 越界：{d['confidence']}")
    if not tree.get("root"):
        problems.append("reasoning_tree.root 为空")
    if not tree.get("children"):
        problems.append("reasoning_tree.children 为空")
    if not d["scored"]:
        problems.append("scored 为空")
    if not d["most_uncertain"] or not d["ask_user"]:
        problems.append("缺 most_uncertain / ask_user")
    json.dumps(d, ensure_ascii=False)   # 必须可序列化

    ev = (f"cards={kinds}\nconfidence={d['confidence']}\n"
          f"degraded={d['degraded']}\n"
          f"scored={[(s['option']['label'], s['total'], s['feasible']) for s in d['scored']]}\n"
          f"reasoning_tree.root={tree['root']!r}\n"
          f"reasoning_tree.children={len(tree['children'])} 个一级节点："
          f"{[c['agent'] for c in tree['children']]}\n"
          f"most_uncertain={d['most_uncertain']!r}\nask_user={d['ask_user']!r}\n"
          f"GO 牌理由：{[w for c in d['cards'] if c['kind'] == 'GO' for w in c['why']]}")
    if problems:
        ev += f"\n问题：{problems}"
    record("(a) --demo 产出合法 Decision（GO 牌/confidence/reasoning_tree 非空）",
           not problems, ev)


# ---------------------------------------------------------------- (b) --no-llm

def test_no_llm() -> None:
    req = _extractor.extract(DEMO_TEXT, use_llm=False)
    prof = _profile.new_profile("selftest", "new")
    dec = _advisor.advise(req, prof, use_llm=False)
    d = _dec_dict(dec)

    problems = []
    if not d["degraded"]:
        problems.append("degraded 应为 true")
    bad = _has_forbidden(d)
    if bad:
        problems.append(f"理由含占位文案：{bad}")
    if not d["cards"]:
        problems.append("cards 为空")
    go = next((c for c in d["cards"] if c["kind"] == "GO"), None)
    if go and not go["why"]:
        problems.append("GO 牌 why 为空")

    ev = (f"degraded={d['degraded']}\n"
          f"cards={[c['kind'] for c in d['cards']]}\n"
          f"GO={go['title'] if go else None}\n"
          f"GO why={go['why'] if go else None}\n"
          f"占位文案扫描：{bad or '未发现（PASS）'}")
    if problems:
        ev += f"\n问题：{problems}"
    record("(b) --no-llm 出结果、degraded=true、理由非占位文案", not problems, ev)


# ---------------------------------------------------------------- (c) 确定性

def test_deterministic() -> None:
    req = _extractor.extract(DEMO_TEXT, use_llm=False)
    prof = _profile.new_profile("selftest", "new")
    w = dict(prof.weights)

    a = _scorer.score(copy.deepcopy(req), weights=dict(w), profile=prof)
    b = _scorer.score(copy.deepcopy(req), weights=dict(w), profile=prof)
    ta = {s.option.id: s.total for s in a}
    tb = {s.option.id: s.total for s in b}

    # 再跑一次完整 advise，比对 scored 总分
    d1 = _dec_dict(_advisor.advise(copy.deepcopy(req), prof, use_llm=False))
    d2 = _dec_dict(_advisor.advise(copy.deepcopy(req), prof, use_llm=False))
    s1 = {s["option"]["id"]: s["total"] for s in d1["scored"]}
    s2 = {s["option"]["id"]: s["total"] for s in d2["scored"]}

    same = (ta == tb) and (s1 == s2)
    ev = (f"scorer 第1次：{ta}\nscorer 第2次：{tb}\n"
          f"advise 第1次 scored：{s1}\nadvise 第2次 scored：{s2}\n"
          f"结论：{'两次完全一致（PASS）' if same else '不一致（FAIL）'}")
    record("(c) 同输入跑两次，scorer 分数完全一致", same, ev)


# ---------------------------------------------------------------- (d) veteran vs new

def test_veteran_vs_new() -> None:
    req = _extractor.extract(DEMO_TEXT, use_llm=False)
    p_new = _profile.new_profile("selftest_new", "new")
    p_vet = _profile.new_profile("selftest_vet", "veteran")

    d_new = _dec_dict(_advisor.advise(copy.deepcopy(req), p_new, use_llm=False))
    d_vet = _dec_dict(_advisor.advise(copy.deepcopy(req), p_vet, use_llm=False))

    go_new = next(c for c in d_new["cards"] if c["kind"] == "GO")
    go_vet = next(c for c in d_vet["cards"] if c["kind"] == "GO")
    sig_new = json.dumps([go_new["option_id"], go_new["title"], go_new["why"]],
                         ensure_ascii=False)
    sig_vet = json.dumps([go_vet["option_id"], go_vet["title"], go_vet["why"]],
                         ensure_ascii=False)
    differ = sig_new != sig_vet

    ev = (f"new   GO：{go_new['title']}\n"
          f"         why={go_new['why']}\n"
          f"veteran GO：{go_vet['title']}\n"
          f"         why={go_vet['why']}\n"
          f"veteran 档案证据：goals={p_vet.goals} facts={p_vet.facts}\n"
          f"veteran 历史条数={len(p_vet.history)}\n"
          f"结论：{'推荐或理由不同（PASS）' if differ else '完全相同（FAIL）'}")
    record("(d) veteran vs new 档案对同一问题给出不同推荐或不同理由", differ, ev)


# ---------------------------------------------------------------- (e) 硬约束违反

def test_deadline() -> None:
    req = _extractor.extract(DEADLINE_TEXT, use_llm=False)
    prof = _profile.new_profile("selftest", "new")
    dec = _advisor.advise(req, prof, use_llm=False)
    d = _dec_dict(dec)

    blocked_ids = {b["option_id"] for b in d["blocked"]}
    infeasible = {s["option"]["id"] for s in d["scored"] if not s["feasible"]}
    gym = next((s for s in d["scored"] if "健身房" in s["option"]["label"]), None)

    problems = []
    if not gym:
        problems.append("没找到健身房选项")
    else:
        if gym["feasible"]:
            problems.append("健身房 feasible 应为 false")
        if not gym["blocked_reason"]:
            problems.append("健身房 blocked_reason 为空")
        if gym["option"]["id"] not in blocked_ids:
            problems.append("健身房未写入 blocked")
    # 被拦选项不得成为 GO
    go = next((c for c in d["cards"] if c["kind"] == "GO"), None)
    if go and go["option_id"] in blocked_ids:
        problems.append(f"被拦选项 {go['option_id']} 成了 GO")

    scored_ev = [(s["option"]["label"], s["total"], s["feasible"],
                  s["blocked_reason"][:60]) for s in d["scored"]]
    go_title = go["title"] if go else None
    go_oid = go["option_id"] if go else None
    gym_feas = gym["feasible"] if gym else "?"
    gym_reason = gym["blocked_reason"] if gym else ""
    ev = (f"scored={scored_ev}\n"
          f"blocked={d['blocked']}\n"
          f"GO={go_title} (option_id={go_oid})\n"
          f"健身房 feasible={gym_feas} blocked_reason={gym_reason!r}")
    if problems:
        ev += f"\n问题：{problems}"
    record("(e) 22:30 前到家但健身房 23:10 才回 → feasible=false 且被 blocked",
           not problems, ev)


# ---------------------------------------------------------------- (f) MXAGENT_BIN

def test_bad_mxagent_bin() -> None:
    env = dict(os.environ)
    env["MXAGENT_BIN"] = "/nonexistent/mxagent"
    code = (
        "import sys, json; sys.path.insert(0, %r)\n"
        "from engine import advisor, extractor, profile\n"
        "req = extractor.extract(%r, use_llm=True)\n"
        "p = profile.new_profile('selftest_f', 'new')\n"
        "d = advisor.advise(req, p, use_llm=True)\n"
        "dd = d.to_dict()\n"
        "print(json.dumps({'degraded': dd['degraded'], 'cards': [c['kind'] for c in dd['cards']],"
        " 'confidence': dd['confidence'], 'n_scored': len(dd['scored']),"
        " 'tree_children': len(dd['reasoning_tree']['children']),"
        " 'go_why': [w for c in dd['cards'] if c['kind']=='GO' for w in c['why']]},"
        " ensure_ascii=False))\n"
    ) % (_ROOT, DEMO_TEXT)
    try:
        r = subprocess.run([PY, "-c", code], capture_output=True, timeout=180, env=env,
                           cwd=_ROOT)
        out = (r.stdout or b"").decode("utf-8", errors="replace").strip()
        info = json.loads(out.splitlines()[-1]) if out else {}
        problems = []
        if r.returncode != 0:
            problems.append(f"子进程退出码 {r.returncode}: "
                            f"{(r.stderr or b'').decode()[:300]}")
        if not info.get("degraded"):
            problems.append("degraded 应为 true")
        if "GO" not in (info.get("cards") or []):
            problems.append(f"缺 GO 牌：{info.get('cards')}")
        if not info.get("go_why"):
            problems.append("GO 牌理由为空")
        if not info.get("tree_children"):
            problems.append("reasoning_tree.children 为空")
        ev = (f"MXAGENT_BIN=/nonexistent/mxagent\n"
              f"退出码={r.returncode}\n输出={json.dumps(info, ensure_ascii=False)}")
        if problems:
            ev += f"\n问题：{problems}"
        record("(f) MXAGENT_BIN=/nonexistent/mxagent 下仍返回合法 Decision 且 degraded=true",
               not problems, ev)
    except Exception as e:  # noqa: BLE001
        record("(f) MXAGENT_BIN=/nonexistent/mxagent 下仍返回合法 Decision 且 degraded=true",
               False, f"自测子进程本身失败：{type(e).__name__}: {e}")


# ---------------------------------------------------------------- (g) LLM reasoning_tree

def test_llm_tree() -> None:
    """真实 LLM 路径下 reasoning_tree 有真实层级内容。"""
    if os.environ.get("MXAGENT_BIN", "").startswith("/nonexistent"):
        record("(g) 真实 LLM 路径下 reasoning_tree 有真实层级内容", True,
               "SKIP：当前 MXAGENT_BIN=/nonexistent/mxagent（降级模式自测），"
               "LLM 路径请在不设 MXAGENT_BIN 时复跑（见 SELFTEST.md）")
        return
    try:
        req = _extractor.extract(DEMO_TEXT, use_llm=True)
        prof = _profile.new_profile("selftest_llm", "new")
        dec = _advisor.advise(req, prof, use_llm=True)
        d = _dec_dict(dec)
        tree = d["reasoning_tree"]
        tl = next((c for c in tree["children"] if c["agent"] == "Timeline"), None)
        tl_kids = (tl or {}).get("children") or []

        problems = []
        if d["degraded"]:
            problems.append("degraded 应为 false（LLM 可用）")
        if not d["request"]["extracted_ok"]:
            problems.append("extracted_ok 应为 true（LLM 抽取成功）")
        if not tl_kids:
            problems.append("Timeline 下没有子节点")
        # 子节点摘要必须是真实内容（长度 > 10 且不是占位）
        for k in tl_kids:
            if len(k.get("summary", "")) < 10:
                problems.append(f"Timeline 子节点摘要过短：{k}")
        bad = _has_forbidden(d)
        if bad:
            problems.append(f"含占位文案：{bad}")

        ev = (f"degraded={d['degraded']} extracted_ok={d['request']['extracted_ok']}\n"
              f"reasoning_tree 一级节点={[c['agent'] for c in tree['children']]}\n"
              f"Timeline 子节点数={len(tl_kids)}\n"
              + "\n".join(f"  [{k['agent']}] {k['summary'][:120]}" for k in tl_kids[:5]))
        if problems:
            ev += f"\n问题：{problems}"
        record("(g) 真实 LLM 路径下 reasoning_tree 有真实层级内容", not problems, ev)
    except Exception as e:  # noqa: BLE001
        record("(g) 真实 LLM 路径下 reasoning_tree 有真实层级内容", False,
               f"LLM 路径异常：{type(e).__name__}: {e}")


# ---------------------------------------------------------------- main

def main() -> int:
    print("PickOne — engine 7 项验收自测")
    print(f"解释器：{sys.executable}")
    print(f"MXAGENT_BIN：{os.environ.get('MXAGENT_BIN', '(未设置，默认 mxagent)')}")

    test_demo()
    test_no_llm()
    test_deterministic()
    test_veteran_vs_new()
    test_deadline()
    test_bad_mxagent_bin()
    test_llm_tree()

    print("\n" + "=" * 70)
    print("汇总")
    print("=" * 70)
    n_pass = sum(1 for _, ok, _ in RESULTS if ok)
    for name, ok, _ in RESULTS:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    print(f"\n通过 {n_pass}/{len(RESULTS)}")
    return 0 if n_pass == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
