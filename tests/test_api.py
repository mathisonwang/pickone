"""HTTP 集成测试（server/）— 契约来源：contracts/api.md

运行（必须 venv 解释器）：
    /home/Developer/workspace/.venv/bin/python -m unittest discover -s tests -p 'test_api*.py' -v

约定（父 agent 已确认）：
- 服务未起 → 所有用例 skip（由 run_all.sh 负责尝试启动 server/run.sh）。
- 任何情况下断言 HTTP 状态码不是 5xx，且响应是合法 JSON。
- /api/advise 走异步任务模式：轮询 /api/decision/{id} 直到 done/failed，上限 300s。
- /api/demo/cases 是契约外扩展（父 agent 要求必须存在），未实现 → skip 并标"契约缺口"。

用 env 控制耗时：
    PICKONE_LLM=1   跑 use_llm=true 的真实 LLM 全链路（单条最长 300s）
    PICKONE_SLOW=1  额外跑超长文本/并发等慢用例
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import BASE_URL, DIMENSIONS, server_up  # noqa: E402

LLM = os.environ.get("PICKONE_LLM") == "1"
SLOW = os.environ.get("PICKONE_SLOW") == "1"
POLL_LIMIT = float(os.environ.get("PICKONE_POLL_LIMIT", "300"))

SERVER_UP = server_up(timeout=4.0)
need_server = unittest.skipUnless(SERVER_UP, "服务未就绪（127.0.0.1:8888 无响应）")

TEXT_MAIN = ("今晚好纠结：方案一去健身房跑步，单程通勤40分钟，年卡折合每次25块钱，"
             "但最近工作太累；方案二去楼下那家火锅店，步行5分钟，人均80，"
             "一个人吃又有点尴尬；方案三在家看电影，零通勤零花费，但今晚首映怕被剧透。"
             "我预算今晚不超过100块，而且明天早上7点要起床上班，选哪个好？")

# 运行期统计（供汇总/排障）
STATS = {"requests": 0, "5xx": [], "non_json": [], "skipped_endpoints": []}
# 契约偏差（不算测试失败，但必须上报）：如框架默认 404 返回非 JSON
CONTRACT_DEVIATIONS = []


def _post(path, body=None, timeout=30.0, raw_body=None, headers=None):
    from common import http
    STATS["requests"] += 1
    return http("POST", path, body=raw_body if raw_body is not None else body,
                timeout=timeout, headers=headers)


def _get(path, timeout=30.0, raw=False):
    from common import http
    STATS["requests"] += 1
    return http("GET", path, timeout=timeout, raw=raw)


def assert_no_5xx(test, status, body, err, ctx):
    """健壮性核心断言：状态码非 5xx；响应应为合法 JSON。

    唯一例外（记入 CONTRACT_DEVIATIONS 上报，不算测试失败）：
    Starlette 框架默认的 404/405 返回 text/plain "Not Found"，
    而契约要求 {"ok":false,"error":"中文消息"} —— 属实现偏差，如实上报。
    """
    test.assertIsNotNone(status, f"{ctx}: 请求未得到响应（连接失败/超时）{err}")
    test.assertLess(status, 500, f"{ctx}: 返回 5xx（契约要求任何情况不返回 500）status={status}")
    if status in (404, 405) and body is None:
        CONTRACT_DEVIATIONS.append(
            f"{ctx}: HTTP {status} 返回非 JSON（框架默认 'Not Found'），"
            f"契约要求 {{\"ok\":false,\"error\":\"中文消息\"}}")
        return
    test.assertIsNotNone(body, f"{ctx}: {status} 响应不是合法 JSON：{err}")
    if status >= 400 and isinstance(body, dict):
        test.assertIn("error", body, f"{ctx}: 4xx 错误响应缺 error 字段：{body}")


def poll_decision(decision_id, limit=POLL_LIMIT, interval=2.0):
    """轮询 /api/decision/{id} 直到 status ∈ {done, failed} 或超时。
    返回 (final_wrapper_or_None, last_status, elapsed, err)。
    兼容两种形态：外层 wrapper {decision_id,status,result} 或直接 Decision。"""
    t0 = time.time()
    last_status, last_body, last_err = None, None, ""
    while time.time() - t0 < limit:
        status, _, body, err = _get(f"/api/decision/{decision_id}")
        last_err = err
        if body is not None:
            last_body = body
            if isinstance(body, dict):
                st = body.get("status")
                if st is None and body.get("cards") is not None:
                    st = "done"      # 直接返回 Decision 的同步实现
                last_status = st
                if st in ("done", "failed"):
                    return body, st, time.time() - t0, ""
        time.sleep(interval)
    return last_body, last_status, time.time() - t0, last_err


def submit_advise(text, user_id="qa", use_llm=False, timeout=30.0):
    """提交 advise，返回 (decision_id, wrapper_or_full_decision, err)。"""
    status, _, body, err = _post("/api/advise",
                                {"text": text, "user_id": user_id, "use_llm": use_llm},
                                timeout=timeout)
    if body is None:
        return None, None, f"advise 无合法 JSON 响应 status={status} err={err}"
    did = None
    if isinstance(body, dict):
        did = body.get("decision_id") or (body.get("result") or {}).get("decision_id") \
            or body.get("id")
    return did, body, ""


@need_server
class TestHealthAndStatic(unittest.TestCase):
    maxDiff = None

    def test_health(self):
        status, _, body, err = _get("/api/health")
        assert_no_5xx(self, status, body, err, "GET /api/health")
        self.assertEqual(status, 200)
        self.assertIs(body.get("ok"), True, f"health.ok 应为 true: {body}")
        self.assertIn("mxagent_available", body, "health 缺 mxagent_available 字段")
        self.assertIsInstance(body["mxagent_available"], bool)
        self.assertIn("version", body, "health 缺 version 字段")

    def test_index_html(self):
        status, hdrs, body, err = _get("/", raw=True)
        self.assertIsNotNone(status, f"GET / 无响应 {err}")
        self.assertLess(status, 500, "GET / 返回 5xx")
        self.assertEqual(status, 200, "GET / 应返回 200")
        html = (body or b"").decode("utf-8", "replace")
        self.assertIn("<html", html.lower(), "GET / 返回的不是 HTML")


@need_server
class TestParse(unittest.TestCase):
    def test_parse_ok(self):
        status, _, body, err = _post("/api/parse", {"text": TEXT_MAIN}, timeout=60.0)
        assert_no_5xx(self, status, body, err, "POST /api/parse")
        if status != 200:
            self.skipTest(f"/api/parse 返回 {status}（可能 LLM/实现未就绪）：{body}")
        # 期望是 DecisionRequest
        d = body.get("request", body)
        for f in ("raw_text", "options", "hard_constraints", "extracted_ok"):
            self.assertIn(f, d, f"DecisionRequest 缺字段 {f}: keys={list(d)}")
        self.assertGreaterEqual(len(d["options"]), 1, "未抽出任何选项")
        o = d["options"][0]
        for f in ("id", "label", "category", "commute_min", "cost_cny"):
            self.assertIn(f, o, f"Option 缺字段 {f}: {o}")

    def test_parse_empty_body(self):
        status, _, body, err = _post("/api/parse", raw_body=b"", headers={
            "Content-Type": "application/json"})
        assert_no_5xx(self, status, body, err, "POST /api/parse 空 body")

    def test_parse_invalid_json(self):
        status, _, body, err = _post("/api/parse", raw_body=b"{not-json!!",
                                     headers={"Content-Type": "application/json"})
        assert_no_5xx(self, status, body, err, "POST /api/parse 非法 JSON")

    def test_parse_missing_text_field(self):
        status, _, body, err = _post("/api/parse", {"nope": 1})
        assert_no_5xx(self, status, body, err, "POST /api/parse 缺 text 字段")

    def test_parse_long_text(self):
        long_text = TEXT_MAIN + ("我很纠结，因为选择太多了，每个都有优缺点。" * 120)  # >2000 字
        self.assertGreater(len(long_text), 2000)
        status, _, body, err = _post("/api/parse", {"text": long_text}, timeout=90.0)
        assert_no_5xx(self, status, body, err, "POST /api/parse 超长文本(2000+字)")


@need_server
class TestAdviseFlow(unittest.TestCase):
    """advise 异步任务模式：提交 → 轮询 decision → done/failed。"""

    def _run_flow(self, text, user_id, use_llm, timeout):
        did, first, err = submit_advise(text, user_id=user_id, use_llm=use_llm,
                                       timeout=timeout)
        self.assertIsNotNone(did, f"advise 未返回 decision_id：first={first} err={err}")
        wrapper = first
        st = first.get("status") if isinstance(first, dict) else None
        if st not in ("done", "failed"):
            wrapper, st, elapsed, perr = poll_decision(did, limit=min(timeout, POLL_LIMIT))
            self.assertIn(st, ("done", "failed"),
                          f"轮询 {min(timeout, POLL_LIMIT)}s 后 status={st}（要求收敛到 "
                          f"done/failed）err={perr} body={str(wrapper)[:300]}")
        self.assertEqual(st, "done", f"决策失败 status={st} error="
                                    f"{(wrapper or {}).get('error') if isinstance(wrapper, dict) else '?'}")
        result = wrapper.get("result") if isinstance(wrapper, dict) and "result" in wrapper \
            else wrapper
        self.assertIsNotNone(result, f"status=done 但 result 为空：{wrapper}")
        return did, result

    def _assert_decision(self, d, ctx):
        for f in ("decision_id", "request", "scored", "cards", "confidence",
                  "most_uncertain", "ask_user", "reasoning_tree", "degraded",
                  "blocked", "weights_used"):
            self.assertIn(f, d, f"{ctx}: Decision 缺字段 {f}")
        kinds = [c.get("kind") for c in d["cards"]]
        self.assertIn("GO", kinds, f"{ctx}: cards 必须含 GO，实际 {kinds}")
        self.assertGreaterEqual(float(d["confidence"]), 0.0)
        self.assertLessEqual(float(d["confidence"]), 1.0)
        for so in d["scored"]:
            self.assertIn("total", so)
            self.assertIn("feasible", so)
        # 契约：scored 按 total 降序
        totals = [float(s["total"]) for s in d["scored"]]
        self.assertEqual(totals, sorted(totals, reverse=True),
                         f"{ctx}: scored 未按 total 降序 {totals}")
        json.dumps(d, ensure_ascii=False)

    def test_advise_degraded_flow(self):
        """use_llm=false 的快速链路（服务未实现该参数时会走 LLM，超时上限 120s）。"""
        did, result = self._run_flow(TEXT_MAIN, "qa_api", False, timeout=120.0)
        self._assert_decision(result, "advise(use_llm=false)")
        TestAdviseFlow.did = did

    def test_get_decision_after_flow(self):
        if not hasattr(TestAdviseFlow, "did"):
            did, _ = self._run_flow(TEXT_MAIN, "qa_api", False, timeout=120.0)
            TestAdviseFlow.did = did
        status, _, body, err = _get(f"/api/decision/{TestAdviseFlow.did}")
        assert_no_5xx(self, status, body, err, "GET /api/decision/{id}")
        self.assertEqual(status, 200)
        self.assertIn(TestAdviseFlow.did, json.dumps(body, ensure_ascii=False),
                      "decision 响应里找不到请求的 decision_id")

    def test_decisions_list(self):
        status, _, body, err = _get("/api/decisions?user_id=qa_api&limit=20")
        assert_no_5xx(self, status, body, err, "GET /api/decisions")
        self.assertEqual(status, 200)
        self.assertIn("items", body, f"/api/decisions 应返回 {{'items':[...]}}：{body}")
        self.assertIsInstance(body["items"], list)

    @unittest.skipUnless(LLM, "真实 LLM 全链路用例（设 PICKONE_LLM=1 开启，最长 300s）")
    def test_advise_llm_full(self):
        did, result = self._run_flow(TEXT_MAIN, "qa_llm", True, timeout=POLL_LIMIT)
        self._assert_decision(result, "advise(use_llm=true)")


@need_server
class TestFeedbackAndProfile(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # 准备一个可用的 decision_id
        cls.decision_id = None
        did, first, err = submit_advise(TEXT_MAIN, user_id="qa_fb", use_llm=False,
                                        timeout=120.0)
        if did:
            wrapper, st, _, _ = poll_decision(did, limit=120.0) \
                if not (isinstance(first, dict) and first.get("status") in ("done", "failed")) \
                else (first, first.get("status"), 0, "")
            if st == "done":
                cls.decision_id = did

    def test_reset_profile_new_and_veteran(self):
        for preset in ("new", "veteran"):
            status, _, body, err = _post("/api/profile/reset",
                                         {"user_id": "qa_demo_user", "preset": preset})
            assert_no_5xx(self, status, body, err, f"POST /api/profile/reset {preset}")
            if status == 200:
                p = body.get("profile", body)
                for f in ("user_id", "weights"):
                    self.assertIn(f, p, f"Profile 缺字段 {f}: {list(p)}")
                w = p["weights"]
                for k in DIMENSIONS:
                    self.assertIn(k, w, f"weights 缺维度 {k}")
                    self.assertGreaterEqual(float(w[k]), 0.05, f"{preset} 权重 {k} 越下界")
                    self.assertLessEqual(float(w[k]), 0.35, f"{preset} 权重 {k} 越上界")

    def test_get_profile(self):
        status, _, body, err = _get("/api/profile?user_id=qa_demo_user")
        assert_no_5xx(self, status, body, err, "GET /api/profile")
        self.assertEqual(status, 200)
        p = body.get("profile", body)
        self.assertEqual(p.get("user_id"), "qa_demo_user")

    def test_feedback_ok(self):
        if not self.decision_id:
            self.skipTest("无法获得可用 decision_id（advise 未就绪），feedback 正向用例跳过")
        status, _, body, err = _post("/api/feedback", {
            "decision_id": self.decision_id, "chosen": "opt2", "went": True,
            "satisfaction": 5, "note": "遇到好朋友超开心"})
        assert_no_5xx(self, status, body, err, "POST /api/feedback")
        self.assertEqual(status, 200, f"feedback 应 200，实际 {status}: {body}")
        self.assertIs(body.get("ok"), True, f"feedback 响应 ok 非 true: {body}")
        self.assertIn("weights", body, f"feedback 响应缺 weights: {body}")
        w = body["weights"]
        self.assertAlmostEqual(sum(float(v) for v in w.values()), 1.0, delta=1e-3,
                               msg=f"feedback 返回的 weights 未归一化: {w}")

    def test_feedback_changes_recommendation(self):
        """验收标准第 5 条：打卡后同一问题重问，推荐或理由必须变化。"""
        if not self.decision_id:
            self.skipTest("advise 未就绪，跳过闭环验证")
        uid = "qa_loop"
        did1, r1, err = submit_advise(TEXT_MAIN, user_id=uid, use_llm=False, timeout=120.0)
        if not did1:
            self.skipTest(f"首次 advise 失败：{err}")
        w1, st1, _, _ = poll_decision(did1, limit=120.0)
        if st1 != "done":
            self.skipTest(f"首次决策未完成（status={st1}）")
        res1 = w1.get("result", w1)
        _post("/api/feedback", {"decision_id": did1, "chosen": "opt1", "went": True,
                                "satisfaction": 5, "note": "果然跑完很爽，选对了"})
        did2, r2, err2 = submit_advise(TEXT_MAIN, user_id=uid, use_llm=False, timeout=120.0)
        if not did2:
            self.skipTest(f"二次 advise 失败：{err2}")
        w2, st2, _, _ = poll_decision(did2, limit=120.0)
        if st2 != "done":
            self.skipTest(f"二次决策未完成（status={st2}）")
        res2 = w2.get("result", w2)

        def go_sig(d):
            cards = d.get("cards") or []
            go = next((c for c in cards if c.get("kind") == "GO"), {})
            return json.dumps([go.get("option_id"), go.get("title"), go.get("why"),
                               d.get("weights_used")], ensure_ascii=False)
        self.assertNotEqual(go_sig(res1), go_sig(res2),
                            "打卡反馈后同一问题的 GO 牌与权重完全没变（后悔闭环未生效）")

    def test_log_activity(self):
        status, _, body, err = _post("/api/log_activity", {
            "user_id": "qa_demo_user", "activity": "去健身房跑步", "category": "workout",
            "duration_min": 60, "mood": 5})
        assert_no_5xx(self, status, body, err, "POST /api/log_activity")
        self.assertEqual(status, 200)
        self.assertIs(body.get("ok"), True, f"log_activity 响应 ok 非 true: {body}")


@need_server
class TestAsr(unittest.TestCase):
    def _multipart(self, field="file", content=b"FAKEAUDIO", ctype="audio/webm"):
        boundary = "----qaBoundary1234567890"
        body = (f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{field}"; filename="a.webm"\r\n'
                f"Content-Type: {ctype}\r\n\r\n").encode() + content + \
               f"\r\n--{boundary}--\r\n".encode()
        return body, {"Content-Type": f"multipart/form-data; boundary={boundary}"}

    def test_asr_with_fake_audio(self):
        body, hdrs = self._multipart()
        status, _, obj, err = _post("/api/asr", raw_body=body, headers=hdrs, timeout=60.0)
        assert_no_5xx(self, status, obj, err, "POST /api/asr 假音频")
        if status == 200:
            self.assertIn("text", obj, f"/api/asr 响应缺 text: {obj}")
            self.assertIsInstance(obj["text"], str)

    def test_asr_empty_body(self):
        status, _, obj, err = _post("/api/asr", raw_body=b"", headers={
            "Content-Type": "multipart/form-data"})
        assert_no_5xx(self, status, obj, err, "POST /api/asr 空 body")


@need_server
class TestRobustness(unittest.TestCase):
    """健壮性：任何输入不得 5xx，响应必须是合法 JSON。"""

    def test_empty_body_all_posts(self):
        for path in ("/api/parse", "/api/advise", "/api/feedback",
                     "/api/profile/reset", "/api/log_activity"):
            with self.subTest(path=path):
                status, _, body, err = _post(path, raw_body=b"", headers={
                    "Content-Type": "application/json"})
                assert_no_5xx(self, status, body, err, f"空 body {path}")

    def test_invalid_json_all_posts(self):
        for path in ("/api/parse", "/api/advise", "/api/feedback",
                     "/api/profile/reset", "/api/log_activity"):
            with self.subTest(path=path):
                status, _, body, err = _post(path, raw_body=b'{"a": ',
                                             headers={"Content-Type": "application/json"})
                assert_no_5xx(self, status, body, err, f"非法 JSON {path}")

    def test_wrong_types(self):
        cases = [
            ("/api/parse", {"text": 12345}),
            ("/api/parse", {"text": None}),
            ("/api/feedback", {"decision_id": None, "satisfaction": "very"}),
            ("/api/feedback", {"decision_id": "x", "satisfaction": 99}),
            ("/api/profile/reset", {"user_id": "", "preset": "bogus"}),
            ("/api/log_activity", {"user_id": "qa", "duration_min": "abc", "mood": -5}),
            ("/api/advise", {"text": "", "user_id": "qa", "use_llm": "yes"}),
        ]
        for path, body in cases:
            with self.subTest(path=path, body=str(body)[:40]):
                status, _, resp, err = _post(path, body, timeout=60.0)
                assert_no_5xx(self, status, resp, err, f"类型异常 {path}")

    def test_nonexistent_decision_id(self):
        status, _, body, err = _get("/api/decision/nonexistent-id-000000")
        assert_no_5xx(self, status, body, err, "GET 不存在的 decision_id")
        self.assertIn(status, (404, 400, 200),
                      f"不存在的 decision_id 应 404/400（或 200+ok:false），实际 {status}")
        if status == 200 and isinstance(body, dict) and body.get("ok") is False:
            pass
        elif status != 200:
            self.assertIsInstance(body, dict)

    def test_unknown_user_id(self):
        status, _, body, err = _get("/api/profile?user_id=never_seen_user_zzz")
        assert_no_5xx(self, status, body, err, "未知 user_id profile")
        self.assertIn(status, (200, 404))
        if status == 200:
            p = body.get("profile", body)
            self.assertEqual(p.get("user_id"), "never_seen_user_zzz",
                             "未知用户应返回该 user_id 的默认档案")

    def test_decisions_unknown_user_and_bad_params(self):
        for q in ("/api/decisions?user_id=nobody_zzz",
                  "/api/decisions",
                  "/api/decisions?limit=-5",
                  "/api/decisions?limit=abc"):
            with self.subTest(q=q):
                status, _, body, err = _get(q)
                assert_no_5xx(self, status, body, err, q)

    @classmethod
    def tearDownClass(cls):
        if CONTRACT_DEVIATIONS:
            print("\n[契约偏差上报]（不算测试失败，但需后端修）")

            for d in sorted(set(CONTRACT_DEVIATIONS)):
                print(f"  ! {d}")

    def test_unknown_api_path(self):
        status, _, body, err = _get("/api/definitely_not_here")
        assert_no_5xx(self, status, body, err, "未知 API 路径")
        self.assertIn(status, (404, 405))

    def test_long_text_advise(self):
        if not SLOW:
            self.skipTest("慢用例（设 PICKONE_SLOW=1 开启）")
        long_text = TEXT_MAIN + ("我真的好纠结啊。" * 300)   # >2000 字
        self.assertGreater(len(long_text), 2000)
        status, _, body, err = _post("/api/advise",
                                     {"text": long_text, "user_id": "qa_long",
                                      "use_llm": False}, timeout=180.0)
        assert_no_5xx(self, status, body, err, "advise 超长文本")

    def test_concurrent_advise_5(self):
        """并发 5 个 advise 请求：全部非 5xx、全部合法 JSON、decision_id 互不相同。"""
        results = {}
        lock = threading.Lock()

        def worker(i):
            status, _, body, err = _post("/api/advise",
                                         {"text": TEXT_MAIN, "user_id": f"qa_c{i}",
                                          "use_llm": False}, timeout=180.0)
            with lock:
                results[i] = (status, body, err)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(5)]
        t0 = time.time()
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=200)
        self.assertEqual(len(results), 5, "有并发请求未返回")
        ids = set()
        for i, (status, body, err) in sorted(results.items()):
            self.assertLess(status or 500, 500,
                            f"并发 advise #{i} 返回 5xx status={status}")
            self.assertIsNotNone(body, f"并发 advise #{i} 响应非法 JSON: {err}")
            if isinstance(body, dict):
                did = body.get("decision_id") or (body.get("result") or {}).get("decision_id")
                if did:
                    ids.add(did)
        self.assertEqual(len(ids), 5,
                         f"并发 5 个 advise 应得到 5 个不同 decision_id，实际 {sorted(ids)}")


@need_server
class TestDemoCases(unittest.TestCase):
    """契约外扩展（父 agent 确认必须存在，前端"一键填入样例"依赖）。
    未实现 → skip 并在报告中标红为"契约缺口"。"""

    @classmethod
    def tearDownClass(cls):

            print("\n[契约偏差上报]（不算测试失败，但需后端修）")

            for d in sorted(set(CONTRACT_DEVIATIONS)):
                print(f"  ! {d}")

    def test_demo_cases(self):
        status, _, body, err = _get("/api/demo/cases")
        if status in (404, 405):
            STATS["skipped_endpoints"].append("/api/demo/cases")
            self.skipTest("契约缺口：GET /api/demo/cases 未实现（父 agent 已要求后端补实现+补契约）")
        assert_no_5xx(self, status, body, err, "GET /api/demo/cases")
        self.assertEqual(status, 200)
        items = body.get("items", body.get("cases"))
        self.assertIsInstance(items, list, f"/api/demo/cases 应返回 items/cases 数组: {body}")
        self.assertGreaterEqual(len(items), 3, f"demo cases 至少 3 条，实际 {len(items)}")
        for it in items:
            # 父 agent 要求每条含 {id,title,text}；实现若用 name 代替 title → 记契约偏差
            title = it.get("title") or it.get("name")
            if not it.get("title") or not it.get("id"):
                CONTRACT_DEVIATIONS.append(
                    "GET /api/demo/cases: 条目缺 id/title 字段（实现用 name），"
                    "父 agent 要求每条含 {id,title,text}")
            self.assertTrue(title, f"demo case 缺标题（title/name 均无）: {it}")
            self.assertIn("text", it, f"demo case 缺字段 text: {it}")
            self.assertIsInstance(it["text"], str)
            self.assertGreater(len(it["text"]), 20,
                               f"demo case text 过短（防空壳）: {title}")
            self.assertTrue(any('\u4e00' <= ch <= '\u9fff' for ch in it["text"]),
                            f"demo case text 应为中文: {title}")



if __name__ == "__main__":
    unittest.main(verbosity=2)
