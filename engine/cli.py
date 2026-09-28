"""engine/cli.py — 命令行自测入口。

用法：
    /home/Developer/workspace/.venv/bin/python engine/cli.py --demo
    /home/Developer/workspace/.venv/bin/python engine/cli.py --text "今晚去健身房跑步还是去楼下那家火锅店"
    /home/Developer/workspace/.venv/bin/python engine/cli.py --demo --no-llm
    /home/Developer/workspace/.venv/bin/python engine/cli.py --demo --profile veteran
    MXAGENT_BIN=/nonexistent/mxagent ... python engine/cli.py --demo

输出：合法 Decision JSON（含唯一 GO 牌、confidence、reasoning_tree）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys

# --- 确保能 import engine.*（无论从哪个目录启动） ---------------------------
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for _p in (_ROOT, _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from engine import advisor as _advisor          # noqa: E402
from engine import extractor as _extractor      # noqa: E402
from engine import profile as _profile          # noqa: E402
from engine.models import Decision              # noqa: E402

# ---------------------------------------------------------------- demo 案例

DEMO_CASES = [
    {
        "id": "workout_vs_hotpot_vs_movie",
        "title": "今晚健身房跑步 vs 楼下那家火锅店 vs 在家看电影",
        "text": ("今晚好纠结：方案一去健身房跑步，单程通勤40分钟，年卡折合每次25块钱，"
                 "但最近工作太累；方案二去楼下那家火锅店，步行5分钟，人均80，"
                 "一个人吃又有点尴尬；方案三在家看电影，零通勤零花费，"
                 "但今晚首映怕被剧透。我预算今晚不超过100块，"
                 "而且明天早上7点要起床上班，选哪个好？"),
    },
    {
        "id": "hotpot_vs_cook",
        "title": "吃火锅 vs 回家做饭",
        "text": ("晚饭吃什么：新开的火锅店人均120、朋友都说好想去试试，但本月预算紧张；"
                 "还是回家自己做，健康省钱但有点无聊，做完还要洗碗。"),
    },
    {
        "id": "guitar_vs_bake_vs_rest",
        "title": "学吉他 vs 学烘焙 vs 躺平",
        "text": ("周六下午干点啥：去琴行体验吉他课（新爱好、但担心自己三分钟热度）、"
                 "还是报个烘焙班（能做出东西送朋友）、还是在家躺着补觉（这周加班太累了）。"),
    },
]

#: 违反硬约束的自测案例（22:30 前到家，但健身房 23:10 才回）
DEADLINE_CASE = {
    "id": "deadline_violation",
    "title": "硬约束违反：22:30 前到家但健身房 23:10 才回",
    "text": ("今晚二选一：去健身房跑步，18:30 出发，预计要 240 分钟，单程通勤 40 分钟；"
             "还是去楼下那家火锅店，19:00 开始，90 分钟，单程通勤 5 分钟。"
             "我预算不超过 400 元，而且 22:30 之前必须到家。"),
}


def _print_decision(dec: Decision, verbose: bool = True) -> None:
    d = dec.to_dict()
    print(json.dumps(d, ensure_ascii=False, indent=2))
    if verbose:
        print("\n" + "=" * 66, file=sys.stderr)
        print(f"decision_id : {d['decision_id']}", file=sys.stderr)
        print(f"degraded    : {d['degraded']}", file=sys.stderr)
        print(f"confidence  : {d['confidence']}", file=sys.stderr)
        print(f"cards       : {[c['kind'] for c in d['cards']]}", file=sys.stderr)
        print(f"blocked     : {[(b['option_id'], b['reason'][:40]) for b in d['blocked']]}",
              file=sys.stderr)
        print(f"weights     : {d['weights_used']}", file=sys.stderr)
        for c in d["cards"]:
            print(f"\n[{c['kind']}] {c['title']}", file=sys.stderr)
            for w in c["why"]:
                print(f"   - {w}", file=sys.stderr)
            print(f"   detail: {c['detail']}", file=sys.stderr)
        print(f"\nmost_uncertain: {d['most_uncertain']}", file=sys.stderr)
        print(f"ask_user      : {d['ask_user']}", file=sys.stderr)
        print(f"reasoning_tree: root={d['reasoning_tree'].get('root', '')[:60]!r} "
              f"children={len(d['reasoning_tree'].get('children', []))}", file=sys.stderr)
        print("=" * 66, file=sys.stderr)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="engine/cli.py",
        description="PickOne — mxagent 决策引擎命令行自测入口")
    ap.add_argument("--demo", action="store_true",
                    help="跑内置 demo 案例（默认 disney_vs_a_vs_b）")
    ap.add_argument("--case", default="",
                    help="指定 demo 案例 id（disney_vs_a_vs_b / hotpot_vs_cook / "
                         "guitar_vs_bake_vs_rest / deadline_violation）")
    ap.add_argument("--all-cases", action="store_true", help="跑全部 demo 案例")
    ap.add_argument("--text", default="", help="直接给一段自然语言输入")
    ap.add_argument("--no-llm", action="store_true",
                    help="禁用 LLM（走启发式降级路径，degraded=true）")
    ap.add_argument("--profile", default="new", choices=["new", "veteran"],
                    help="使用哪个用户档案预设（默认 new）")
    ap.add_argument("--user-id", default="cli_demo", help="档案 user_id")
    ap.add_argument("--parse-only", action="store_true",
                    help="只做抽取（extract），不出 Decision")
    ap.add_argument("--quiet", action="store_true", help="只输出 JSON，不要人话摘要")
    args = ap.parse_args(argv)

    use_llm = not args.no_llm

    # ---- 选案例 ----
    cases = list(DEMO_CASES)
    if args.case == "deadline_violation" or args.all_cases:
        cases = cases + [DEADLINE_CASE]
    if args.case:
        cases = [c for c in cases if c["id"] == args.case] or cases[:1]

    texts: list[tuple[str, str]] = []
    if args.text.strip():
        texts.append(("cli_text", args.text.strip()))
    elif args.demo or args.all_cases or args.case:
        texts = [(c["id"], c["text"]) for c in cases]
    else:
        # 默认跑主 demo 案例
        texts = [(DEMO_CASES[0]["id"], DEMO_CASES[0]["text"])]

    # ---- 档案 ----
    prof = _profile.new_profile(args.user_id, args.profile)

    rc = 0
    for cid, text in texts:
        if not args.quiet:
            print(f"\n### 案例 {cid}（use_llm={use_llm}, profile={args.profile}）",
                  file=sys.stderr)
        try:
            req = _extractor.extract(text, use_llm=use_llm)
            if args.parse_only:
                print(json.dumps(req.to_dict(), ensure_ascii=False, indent=2))
                continue
            dec = _advisor.advise(req, prof, use_llm=use_llm)
            _print_decision(dec, verbose=not args.quiet)
        except Exception as e:  # noqa: BLE001 — CLI 也绝不崩
            import traceback
            print(f"[ERROR] 案例 {cid} 失败：{type(e).__name__}: {e}", file=sys.stderr)
            traceback.print_exc()
            rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(main())
