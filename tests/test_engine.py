"""引擎单元测试（engine/）— 契约来源：contracts/data_model.md + docs/ARCHITECTURE.md 第 4/5 节

运行方式（必须用 venv 解释器）：
    /home/Developer/workspace/.venv/bin/python -m unittest discover -s tests -p 'test_engine*.py' -v

设计原则：
- 断言只针对契约里写明的字段与行为；引擎接口不存在时用 skipUnless 优雅跳过（报"未就绪"），
  但**不放松任何已可执行断言**。
- 降级用例主路径走环境变量 MXAGENT_BIN=/nonexistent/mxagent（父 agent 确认的硬性交付），
  外加 use_llm=False 可编程开关用例。monkeypatch 不作为验收依据。
"""
from __future__ import annotations

import copy
import importlib
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from common import DEFAULT_WEIGHTS, DIMENSIONS, EPSILON  # noqa: E402

# ---------------------------------------------------------------------------
# 引擎导入探测（engine/ 可能尚未就绪 → 优雅跳过）
# ---------------------------------------------------------------------------
ENGINE_ERR = ""
models = scorer = profile_mod = advisor = None
try:
    from engine import models as _models  # type: ignore
    models = _models
except Exception as e:  # pragma: no cover
    ENGINE_ERR = f"engine.models 不可导入: {type(e).__name__}: {e}"

try:
    from engine import scorer as _scorer  # type: ignore
    scorer = _scorer
except Exception:
    pass

try:
    from engine import profile as _profile  # type: ignore
    profile_mod = _profile
except Exception:
    pass

try:
    from engine import advisor as _advisor  # type: ignore
    advisor = _advisor
except Exception:
    pass

MODELS_OK = models is not None
SCORER_OK = scorer is not None
PROFILE_OK = profile_mod is not None
ADVISOR_OK = advisor is not None


def _has(mod, *names) -> bool:
    return mod is not None and all(hasattr(mod, n) for n in names)


# ---------------------------------------------------------------------------
# 构造契约样例数据
# ---------------------------------------------------------------------------
def workout_options_kwargs() -> list[dict]:
    """今晚 健身房跑步 vs 楼下那家火锅店 vs 在家看电影（演示主案例，见 demo/cases.json case 1）。"""
    return [
        dict(id="opt1", label="去健身房跑步", category="workout", start_hint="18:30",
             duration_min=60, place="健身房", commute_min=40, cost_cny=25.0,
             social_value=4, pleasure=6, health_load=5, workout=True,
             depends_on=[], concerns=["最近工作太累"],
             after=[], notes="年卡折合每次 25 块"),
        dict(id="opt2", label="去楼下那家火锅店", category="food", start_hint="19:00",
             duration_min=90, place="楼下那家火锅店", commute_min=5, cost_cny=80.0,
             social_value=6, pleasure=8, health_load=4, workout=False,
             depends_on=[], concerns=["一个人吃又有点尴尬"],
             after=[], notes="老板娘很热情"),
        dict(id="opt3", label="在家看电影", category="rest", start_hint="21:00",
             duration_min=120, place="家", commute_min=0, cost_cny=0.0,
             social_value=2, pleasure=7, health_load=2, workout=False,
             depends_on=[], concerns=["今晚首映怕被剧透"],
             after=[], notes="零通勤零花费"),
    ]


def make_request(**over):
    kw = dict(
        raw_text="今晚到底去健身房跑步还是去楼下那家火锅店还是在家看电影？"
                 "跑步单程通勤40分钟，火锅步行5分钟人均80，电影零通勤零花费。"
                 "我预算今晚不超过100块，明天早上7点要起床上班。",
        when="今晚",
        options=[models.Option.from_dict(o) for o in workout_options_kwargs()],
        hard_constraints=["预算今晚不超过100块", "明天早上7点要起床"],
        missing_info=[],
        extracted_ok=True,
    )
    kw.update(over)
    return models.DecisionRequest(**kw)


# ===========================================================================
# 1. models: to_dict / from_dict 往返
# ===========================================================================
@unittest.skipUnless(MODELS_OK, f"engine 未就绪 — {ENGINE_ERR}")
class TestModels(unittest.TestCase):
    maxDiff = None

    def _roundtrip(self, obj, cls, label):
        d = obj.to_dict()
        s = json.dumps(d, ensure_ascii=False)          # 必须 JSON 可序列化
        back = cls.from_dict(json.loads(s))
        d2 = back.to_dict()
        self.assertEqual(d, d2, f"{label} 往返不一致")

    def test_option_roundtrip(self):
        for kw in workout_options_kwargs():
            self._roundtrip(models.Option.from_dict(kw), models.Option, "Option")

    def test_request_roundtrip(self):
        self._roundtrip(make_request(), models.DecisionRequest, "DecisionRequest")

    def test_consequence_roundtrip(self):
        c = models.Consequence(option_id="opt2", horizon="48h",
                               effects=[{"time": "tonight", "desc": "跳完很爽", "impact": 2}],
                               future_score=7, risk="")
        self._roundtrip(c, models.Consequence, "Consequence")

    def test_scored_option_roundtrip(self):
        so = models.ScoredOption(
            option=models.Option.from_dict(workout_options_kwargs()[1]),
            dims={k: 5.0 for k in DIMENSIONS}, total=6.0, feasible=True,
            blocked_reason="", consequence=None, reasons=["你已经 4 天没运动了"])
        self._roundtrip(so, models.ScoredOption, "ScoredOption")

    def test_card_roundtrip(self):
        c = models.Card(kind="GO", option_id="opt1", title="就它了：去健身房跑步",
                        why=["节律缺口大"], detail="跑完顺路买瓶水")
        self._roundtrip(c, models.Card, "Card")

    def test_decision_roundtrip(self):
        d = models.Decision(
            decision_id="d-test", created_at=1700000000.0, request=make_request(),
            scored=[models.ScoredOption(
                option=models.Option.from_dict(workout_options_kwargs()[0]),
                dims={k: 5.0 for k in DIMENSIONS}, total=5.0, feasible=True,
                blocked_reason="", consequence=None, reasons=[])],
            cards=[models.Card(kind="GO", option_id="opt1", title="t", why=[], detail="")],
            confidence=0.6, most_uncertain="是否有人同行", ask_user="有人一起吗？",
            reasoning_tree={"root": "决策", "children": []}, degraded=True,
            blocked=[], weights_used=dict(DEFAULT_WEIGHTS))
        self._roundtrip(d, models.Decision, "Decision")

    def test_profile_roundtrip(self):
        p = models.Profile(user_id="qa", nickname="测试",
                           goals=[{"key": "workout_per_week", "target": 3, "unit": "次/周"}],
                           history=[{"date": "2026-07-20", "activity": "去健身房跑步",
                                     "category": "workout", "duration_min": 60, "mood": 5}],
                           weights=dict(DEFAULT_WEIGHTS),
                           facts=["楼下火锅店的老板娘很热情"],
                           updated_at=1700000000.0)
        self._roundtrip(p, models.Profile, "Profile")

    def test_from_dict_missing_fields(self):
        """缺字段必须用默认值，不抛异常。"""
        for cls in (models.Option, models.DecisionRequest, models.Consequence,
                    models.ScoredOption, models.Card, models.Decision, models.Profile):
            with self.subTest(cls=cls.__name__):
                obj = cls.from_dict({})
                self.assertIsNotNone(obj)
                json.dumps(obj.to_dict(), ensure_ascii=False)

    def test_from_dict_unknown_fields(self):
        """未知字段忽略，不报错（前后端版本容忍）。"""
        for cls, sample in (
            (models.Option, workout_options_kwargs()[0]),
            (models.Card, {"kind": "GO", "option_id": "opt1", "title": "t",
                           "why": [], "detail": ""}),
            (models.Profile, {"user_id": "qa"}),
        ):
            with self.subTest(cls=cls.__name__):
                d = dict(sample)
                d["__unknown_field_xyz__"] = 123
                d["to_dict"] = "shadow"
                obj = cls.from_dict(d)
                out = obj.to_dict()
                self.assertNotIn("__unknown_field_xyz__", out,
                                 f"{cls.__name__} 不应把未知字段写回 to_dict()")

    def test_to_dict_json_serializable_no_set(self):
        d = make_request().to_dict()
        d["__decision_probe__"] = models.Decision(
            decision_id="x", created_at=0.0, request=make_request(), scored=[],
            cards=[], confidence=0.0, most_uncertain="", ask_user="",
            reasoning_tree={}, degraded=False, blocked=[],
            weights_used={}).to_dict()
        try:
            json.dumps(d, ensure_ascii=False)
        except TypeError as e:
            self.fail(f"to_dict() 产物不可 JSON 序列化（可能含 set/自定义对象）: {e}")


# ===========================================================================
# 2. scorer: 确定性 + 权重方向 + 硬约束
# ===========================================================================
@unittest.skipUnless(SCORER_OK, "engine.scorer 未就绪")
class TestScorer(unittest.TestCase):
    def _score(self, req, weights):
        """兼容多种 scorer 入口签名。"""
        for name in ("score", "score_request", "score_all", "score_options"):
            fn = getattr(scorer, name, None)
            if fn is None:
                continue
            try:
                return fn(req, weights)
            except TypeError:
                try:
                    return fn(req, profile_weights=weights)
                except TypeError:
                    return fn(req)
        self.skipTest("scorer 未提供可识别的打分入口（score/score_request/...）")

    def _totals(self, result):
        """结果 → {option_id: (total, feasible)}"""
        out = {}
        items = result if isinstance(result, list) else getattr(result, "scored", result)
        for so in items:
            oid = so.option.id if hasattr(so, "option") else so["option"]["id"]
            total = so.total if hasattr(so, "total") else so["total"]
            feas = so.feasible if hasattr(so, "feasible") else so["feasible"]
            out[oid] = (float(total), bool(feas))
        return out

    def test_deterministic_same_input_same_output(self):
        req = make_request()
        w = dict(DEFAULT_WEIGHTS)
        a = self._totals(self._score(copy.deepcopy(req), dict(w)))
        b = self._totals(self._score(copy.deepcopy(req), dict(w)))
        self.assertEqual(sorted(a.keys()), sorted(b.keys()))
        for oid in a:
            self.assertEqual(a[oid][0], b[oid][0],
                             f"scorer 不确定：{oid} 两次分数 {a[oid][0]} != {b[oid][0]}")

    def test_weight_direction_social(self):
        """提高 social 权重 → 高社交选项(opt2, social_value=6)相对 opt3(social_value=2)的分差必须增大。"""
        w0 = dict(DEFAULT_WEIGHTS)
        w1 = dict(DEFAULT_WEIGHTS)
        w1["social"] = 0.35
        base = self._totals(self._score(copy.deepcopy(make_request()), w0))
        after = self._totals(self._score(copy.deepcopy(make_request()), w1))
        gap0 = base["opt2"][0] - base["opt3"][0]
        gap1 = after["opt2"][0] - after["opt3"][0]
        self.assertGreater(gap1, gap0,
                           f"social 权重 0.18→0.35 后 opt2-opt3 分差未增大 ({gap0} → {gap1})")

    def test_weight_direction_commute(self):
        """降低 commute 权重 → 通勤 40min 的 opt1 相对通勤 5min 的 opt2 应变得更有利。"""
        w0 = dict(DEFAULT_WEIGHTS)
        w1 = dict(DEFAULT_WEIGHTS)
        w1["commute"] = 0.05
        base = self._totals(self._score(copy.deepcopy(make_request()), w0))
        after = self._totals(self._score(copy.deepcopy(make_request()), w1))
        gap0 = base["opt2"][0] - base["opt1"][0]
        gap1 = after["opt2"][0] - after["opt1"][0]
        self.assertGreater(gap0, gap1,
                           f"commute 权重降低后 opt2 相对 opt1 优势未缩小 ({gap0} → {gap1})")

    def test_dims_in_range_and_total_weighted(self):
        res = self._score(copy.deepcopy(make_request()), dict(DEFAULT_WEIGHTS))
        items = res if isinstance(res, list) else getattr(res, "scored", res)
        for so in items:
            dims = so.dims if hasattr(so, "dims") else so["dims"]
            for k, v in dims.items():
                self.assertGreaterEqual(float(v), 0.0, f"{k} 维度分 <0")
                self.assertLessEqual(float(v), 10.0, f"{k} 维度分 >10")
            total = float(so.total if hasattr(so, "total") else so["total"])
            self.assertGreaterEqual(total, 0.0)
            self.assertLessEqual(total, 10.0)

    def test_budget_hard_constraint_blocks(self):
        """必测（确定性）：cost_cny=500 且约束"预算不超过300" → feasible=false。"""
        req = make_request()
        req.options[0].cost_cny = 500.0
        res = self._score(copy.deepcopy(req), dict(DEFAULT_WEIGHTS))
        got = self._totals(res)
        self.assertFalse(got["opt1"][1],
                         "cost_cny=500 违反『预算不超过300』，feasible 必须为 false")

    def test_deadline_hard_constraint_blocks(self):
        """必测（确定性）：start_hint=21:30 + 120min + 单程30min → 到家 24:00 > 22:30。"""
        opt = models.Option.from_dict(dict(
            id="late", label="超晚的选项", category="social", start_hint="21:30",
            duration_min=120, place="远处", commute_min=30, cost_cny=100.0,
            social_value=5, pleasure=5, health_load=3, workout=False,
            depends_on=[], concerns=[], after=[], notes=""))
        good = models.Option.from_dict(workout_options_kwargs()[1])
        req = models.DecisionRequest(
            raw_text="今晚二选一，必须22:30前到家", when="今晚", options=[opt, good],
            hard_constraints=["22:30 前必须到家"], missing_info=[], extracted_ok=True)
        res = self._score(copy.deepcopy(req), dict(DEFAULT_WEIGHTS))
        got = self._totals(res)
        self.assertIn("late", got)
        self.assertFalse(got["late"][1],
                         "21:30+120min+30min 通勤 = 24:00 到家，违反 22:30 硬约束，"
                         "feasible 必须为 false（若引擎未实现时间推算，此项报红）")


# ===========================================================================
# 3. profile.apply_feedback：后悔闭环
# ===========================================================================
@unittest.skipUnless(PROFILE_OK, "engine.profile 未就绪")
class TestProfileFeedback(unittest.TestCase):
    def _make_profile(self, preset="new"):
        for name in ("new_profile", "make_profile", "create_profile", "default_profile"):
            fn = getattr(profile_mod, name, None)
            if fn:
                try:
                    return fn("qa_fb", preset=preset)
                except TypeError:
                    try:
                        return fn("qa_fb", preset)
                    except TypeError:
                        return fn("qa_fb")
        if MODELS_OK and hasattr(models, "Profile"):
            return models.Profile(user_id="qa_fb", nickname="qa", goals=[], history=[],
                                  weights=dict(DEFAULT_WEIGHTS), facts=[],
                                  updated_at=0.0)
        self.skipTest("engine.profile 未提供档案构造函数")

    def _apply(self, prof, fb):
        if hasattr(prof, "apply_feedback"):
            return prof.apply_feedback(fb)
        fn = getattr(profile_mod, "apply_feedback", None)
        if fn is None:
            self.skipTest("engine.profile 未提供 apply_feedback")
        try:
            return fn(prof, fb)
        except TypeError:
            return fn(profile=prof, feedback=fb)

    def _weights(self, prof):
        w = prof.weights if hasattr(prof, "weights") else prof.to_dict()["weights"]
        return {k: float(v) for k, v in w.items()}

    def test_feedback_moves_weights_toward_social_pleasure(self):
        """连续喂『推荐A但实际选B且满意度5』→ 权重朝 social/pleasure 移动，
        且每个权重在 [0.05,0.35]，总和归一化。"""
        prof = self._make_profile("new")
        w0 = self._weights(prof)
        self.assertIn("social", w0)
        self.assertIn("pleasure", w0)
        fb = {
            "decision_id": "d-fb-1",
            "recommended": "optA",
            "chosen": "optB",
            "went": True,
            "satisfaction": 5,
            "note": "选了 B，遇到好朋友，超开心",
            "chosen_dims": {"social": 9.0, "pleasure": 9.0, "commute": 2.0, "cost": 3.0},
            "recommended_dims": {"social": 4.0, "pleasure": 5.0, "commute": 8.0, "cost": 8.0},
        }
        for _ in range(6):
            self._apply(prof, copy.deepcopy(fb))
        w1 = self._weights(prof)
        self.assertGreater(w1["social"], w0["social"],
                           f"social 权重未上升 {w0['social']} → {w1['social']}")
        self.assertGreater(w1["pleasure"], w0["pleasure"],
                           f"pleasure 权重未上升 {w0['pleasure']} → {w1['pleasure']}")
        for k, v in w1.items():
            self.assertGreaterEqual(v, 0.05, f"权重 {k}={v} 越下界 0.05")
            self.assertLessEqual(v, 0.35, f"权重 {k}={v} 越上界 0.35")
        self.assertAlmostEqual(sum(w1.values()), 1.0, delta=1e-6,
                               msg=f"权重和未归一化：{sum(w1.values())} ({w1})")

    def test_low_satisfaction_moves_other_direction(self):
        """满意度 1 且实际选了推荐 → 不应让权重无界漂移；仍满足边界与归一化。"""
        prof = self._make_profile("new")
        fb = {"decision_id": "d-fb-2", "recommended": "optA", "chosen": "optA",
              "went": True, "satisfaction": 1, "note": "后悔了",
              "chosen_dims": {"social": 2.0, "pleasure": 2.0},
              "recommended_dims": {"social": 2.0, "pleasure": 2.0}}
        for _ in range(6):
            self._apply(prof, fb)
        w = self._weights(prof)
        for k, v in w.items():
            self.assertGreaterEqual(v, 0.05, f"权重 {k}={v} 越下界")
            self.assertLessEqual(v, 0.35, f"权重 {k}={v} 越上界")
        self.assertAlmostEqual(sum(w.values()), 1.0, delta=1e-6)


# ===========================================================================
# 4. advisor：唯一 GO 牌 / 降级 / 档案差异
# ===========================================================================
@unittest.skipUnless(ADVISOR_OK and hasattr(advisor, "advise"), "engine.advisor.advise 未就绪")
class TestAdvisor(unittest.TestCase):
    def _advise(self, req, prof=None, **kw):
        try:
            return advisor.advise(req, prof, **kw)
        except TypeError:
            return advisor.advise(request=req, profile=prof, **kw)

    def _new_profile(self):
        if PROFILE_OK:
            for name in ("new_profile", "make_profile", "create_profile", "load_profile"):
                fn = getattr(profile_mod, name, None)
                if fn:
                    try:
                        return fn("qa_new")
                    except TypeError:
                        return fn("qa_new", "new")
        return None

    def _assert_valid_decision(self, d):
        d = d.to_dict() if hasattr(d, "to_dict") else d
        json.dumps(d, ensure_ascii=False)
        for field in ("decision_id", "cards", "confidence", "reasoning_tree",
                      "degraded", "weights_used", "scored"):
            self.assertIn(field, d, f"Decision 缺字段 {field}")
        kinds = [c["kind"] for c in d["cards"]]
        self.assertIn("GO", kinds, "GO 牌必存在")
        self.assertEqual(kinds, ["GO"], "只应有一张 GO 牌（不产出 SWITCH / DROP）")
        self.assertGreaterEqual(d["confidence"], 0.0)
        self.assertLessEqual(d["confidence"], 1.0)
        return d

    def test_go_card_exists(self):
        d = self._assert_valid_decision(self._advise(make_request(), self._new_profile(),
                                                     use_llm=False))
        self.assertGreaterEqual(len(d["cards"]), 1)

    def test_only_one_card_even_when_gap_small(self):
        """产品主张（2026-09-27 修订）：**只给一个选项**。

        即使 top2 分差 < epsilon（伪选择），也**不**再出 DROP 牌——
        展示多个选项本身就是内耗。分差小时应在收尾句里说"不值得纠结"。
        """
        base = workout_options_kwargs()
        # 两个选项所有维度完全对称 → 分差必为 0
        req = models.DecisionRequest(
            raw_text="二选一，两个几乎一样", when="今晚",
            options=[
                models.Option.from_dict(dict(base[0])),
                models.Option.from_dict(dict(base[0], id="opt2b", label="楼下另一家健身房")),
            ],
            hard_constraints=[], missing_info=[], extracted_ok=True)
        d = self._assert_valid_decision(self._advise(req, self._new_profile(), use_llm=False))
        d = self._assert_valid_decision(self._advise(req, self._new_profile(), use_llm=False))
        totals = sorted((float(s["total"]) for s in d["scored"]), reverse=True)
        if len(totals) >= 2:
            self.assertLess(abs(totals[0] - totals[1]), EPSILON + 1e-6,
                            f"测试构造失败：top2 分差 {totals} 未小于 epsilon")
        kinds = [c["kind"] for c in d["cards"]]
        self.assertEqual(kinds, ["GO"],
                         f"只应有一张 GO 牌，实际 cards kinds={kinds}")
        # 分差小时，收尾句要劝退纠结（"不值得"/"半小时"/"先去"任一即可）
        closing = d.get("ask_user", "")
        self.assertTrue(
            any(k in closing for k in ("纠结", "不值得", "半小时", "先去")),
            f"伪选择时收尾句应劝退纠结，实际 ask_user={closing!r}")
        # 理由只讲优点：不得出现比较性或"被拦"表述
        for w in d["cards"][0]["why"]:
            self.assertNotIn("比", w, f"理由不应含比较性表述：{w!r}")
            self.assertNotIn("拦", w, f"理由不应提及其他选项被拦：{w!r}")

    def test_degraded_via_env_mxagent_bin(self):
        """硬性验收：MXAGENT_BIN=/nonexistent/mxagent → 合法 Decision + degraded=true，不抛异常。"""
        if not os.environ.get("MXAGENT_BIN", "").startswith("/nonexistent"):
            os.environ["MXAGENT_BIN"] = "/nonexistent/mxagent"
            # 引擎可能在 import 时读取常量 → 重载相关模块
            for m in ("engine.mxcli", "engine.llm", "engine.agent_run", "engine.runner"):
                try:
                    importlib.reload(importlib.import_module(m))
                except Exception:
                    pass
            try:
                importlib.reload(importlib.import_module("engine.advisor"))
                global advisor
                from engine import advisor as _adv2  # type: ignore
                advisor = _adv2
            except Exception:
                pass
        try:
            d = self._advise(make_request(), self._new_profile())
        except Exception as e:
            self.fail(f"MXAGENT_BIN 指向不存在的可执行文件时引擎抛异常（要求降级不抛）："
                      f"{type(e).__name__}: {e}")
        d = self._assert_valid_decision(d)
        self.assertTrue(d["degraded"],
                        "mxagent 不可用时必须 degraded=true（验收标准第 4 条）")

    def test_degraded_via_use_llm_false(self):
        d = self._assert_valid_decision(self._advise(make_request(), self._new_profile(),
                                                     use_llm=False))
        self.assertTrue(d["degraded"], "use_llm=False 时应 degraded=true")

    def test_veteran_vs_new_profile_differ(self):
        """同一问题（今晚 健身房跑步 vs 楼下火锅 vs 在家看电影），veteran 与 new 档案的推荐或理由必须不同。"""
        if not PROFILE_OK:
            self.skipTest("engine.profile 未就绪，无法构造 veteran 档案")
        new_p = self._new_profile()
        vet_p = None
        for name in ("veteran_profile", "make_profile", "create_profile", "load_profile",
                     "preset_profile"):
            fn = getattr(profile_mod, name, None)
            if fn is None:
                continue
            for args, kwargs in ((("qa_vet",), {"preset": "veteran"}), (("qa_vet", "veteran"),),):
                try:
                    vet_p = fn(*args, **kwargs)
                    break
                except TypeError:
                    continue
            if vet_p is not None:
                break
        if vet_p is None:
            self.skipTest("engine.profile 未提供 veteran 预设（契约要求 profile/reset 支持 veteran）")
        d_new = self._assert_valid_decision(self._advise(copy.deepcopy(make_request()),
                                                         new_p, use_llm=False))
        d_vet = self._assert_valid_decision(self._advise(copy.deepcopy(make_request()),
                                                         vet_p, use_llm=False))
        go_new = next(c for c in d_new["cards"] if c["kind"] == "GO")
        go_vet = next(c for c in d_vet["cards"] if c["kind"] == "GO")
        text_new = json.dumps([go_new["option_id"], go_new["title"], go_new["why"]],
                              ensure_ascii=False)
        text_vet = json.dumps([go_vet["option_id"], go_vet["title"], go_vet["why"]],
                              ensure_ascii=False)
        self.assertNotEqual(text_new, text_vet,
                            "veteran 与 new 档案对同一问题给出完全相同的 GO（推荐与理由均无差异）")


if __name__ == "__main__":
    unittest.main(verbosity=2)
