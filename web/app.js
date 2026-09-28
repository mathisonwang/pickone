/* ============================================================
   PickOne · app.js
   纯原生 JS，IIFE 封装，无构建工具、无外部依赖。
   模块：
     0. 工具函数        1. Mock 数据      2. API 层
     3. 视图与 Tab      4. 输入区          5. 等待动画
     6. 结果渲染        7. 打卡闭环        8. 档案页
     9. 历史页         10. 初始化
   ============================================================ */
(function () {
  "use strict";

  /* ==================== 0. 工具函数 ==================== */
  var $ = function (id) { return document.getElementById(id); };
  var MOCK = /[?&]mock=1(&|$)/.test(location.search);
  var USER_ID = "demo";
  var FETCH_TIMEOUT = 15000;          // 单次 fetch 超时 15s
  var POLL_INTERVAL = 1500;           // 轮询间隔 1.5s
  var POLL_TIMEOUT = 300000;          // 轮询上限 300s

  function esc(s) {
    if (s === null || s === undefined) return "";
    return String(s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function clamp(n, lo, hi) { n = Number(n); if (isNaN(n)) n = lo; return Math.min(hi, Math.max(lo, n)); }
  function fmtDate(ts) {
    var d = new Date((ts || Date.now() / 1000) * 1000);
    return (d.getMonth() + 1) + "月" + d.getDate() + "日 " +
      String(d.getHours()).padStart(2, "0") + ":" + String(d.getMinutes()).padStart(2, "0");
  }
  var toastTimer = null;
  function toast(msg, ms) {
    var t = $("toast");
    t.textContent = msg;
    t.classList.remove("hidden");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { t.classList.add("hidden"); }, ms || 2600);
  }

  /* 维度中文名 */
  var DIM_LABELS = {
    rhythm: "节律缺口", social: "社交价值", commute: "通勤成本",
    cost: "花费", pleasure: "愉悦度", health: "健康体力", future: "未来影响"
  };
  var DIM_ORDER = ["rhythm", "social", "commute", "cost", "pleasure", "health", "future"];
  var CAT_LABELS = {
    workout: "🏃 运动健身", dance: "💃 跳舞", theme_park: "🎢 游乐园",
    food: "🍜 吃饭", social: "👯 社交", rest: "🛋 休息",
    study: "📚 学习", other: "📦 其他"
  };
  function catLabel(c) { return CAT_LABELS[c] || c || ""; }

  /* ==================== 1. Mock 数据 ==================== */
  var Mock = (function () {
    function opt(id, label, category, extra) {
      var o = {
        id: id, label: label, category: category, start_hint: "", duration_min: -1,
        place: "", commute_min: -1, cost_cny: -1, social_value: 5, pleasure: 5,
        health_load: 3, workout: false, depends_on: [], concerns: [], after: [], notes: ""
      };
      for (var k in (extra || {})) o[k] = extra[k];
      return o;
    }
    var now = Date.now() / 1000;

    var request = {
      raw_text: "今晚是去健身房跑步，还是去楼下那家火锅店，或者在家看电影？跑步单程40分钟，火锅一个人吃有点尴尬，电影又怕被剧透。",
      when: "今晚",
      options: [
        opt("opt1", "去健身房跑步", "workout", {
          start_hint: "19:00", duration_min: 60, place: "健身房",
          commute_min: 40, cost_cny: 25, social_value: 4, pleasure: 6,
          health_load: 5, workout: true,
          concerns: ["最近工作太累"], after: []
        }),
        opt("opt2", "去楼下那家火锅店", "food", {
          start_hint: "19:30", duration_min: 90, place: "楼下那家火锅店",
          commute_min: 5, cost_cny: 80, social_value: 6, pleasure: 8,
          health_load: 4, workout: false,
          concerns: ["一个人吃又有点尴尬"], after: []
        }),
        opt("opt3", "在家看电影", "rest", {
          start_hint: "21:00", duration_min: 120, place: "家",
          commute_min: 0, cost_cny: 0, social_value: 2, pleasure: 7,
          health_load: 2, workout: false,
          concerns: ["今晚首映怕被剧透"], after: []
        })
      ],
      hard_constraints: ["预算今晚不超过100块", "明天早上7点要起床上班"],
      missing_info: ["各选项的开始时间"],
      extracted_ok: true
    };

    function scored(o, dims, total, feasible, blocked, reasons, futureScore, risk) {
      return {
        option: o, dims: dims, total: total, feasible: feasible,
        blocked_reason: blocked || "",
        consequence: {
          option_id: o.id, horizon: "48h",
          effects: [
            { time: "tonight", desc: o.label + "：当晚体验直接兑现", impact: total >= 7 ? 2 : 1 },
            { time: "tomorrow_am", desc: total >= 8 ? "明天上午精神不错" : "明天上午可能有点累", impact: total >= 8 ? 1 : -1 },
            { time: "48h", desc: "对本周运动计划的影响已计入节律分", impact: o.workout ? 2 : 0 }
          ],
          future_score: futureScore, risk: risk || ""
        },
        reasons: reasons
      };
    }

    var decision = {
      decision_id: "mock-decision-0001",
      created_at: now,
      request: request,
      scored: [
        scored(request.options[0],
          { rhythm: 10.0, social: 4.0, commute: 5.6, cost: 9.4, pleasure: 6.0, health: 5.8, future: 7.0 },
          6.51, true, "",
          ["你已经 4 天没运动，本周目标 3 次只完成 0 次", "年卡折合每次 25 块，在 100 块预算内很宽裕", "单程通勤 40 分钟，跑完回家还赶得上 7 点前入睡"],
          7, "运动后肌肉酸痛、可能晚归影响第二天状态")
      ],
      cards: [
        {
          kind: "GO", option_id: "opt1",
          title: "就它了：去健身房跑步",
          why: [
            "你已经 4 天没运动，本周目标 3 次只完成 0 次",
            "节律缺口这项拿到 10.0/10，是它最大的加分项",
            "年卡折合每次 25 块，在 100 块预算内很宽裕",
            "出一身汗，今晚的疲惫感反而能睡得更沉"
          ],
          detail: "单程 40 分钟、约 25 元、约 60 分钟。19:00 出门，20:30 就能到家，7 点起床上班完全来得及。"
        }
      ],
      confidence: 0.31,
      most_uncertain: "去健身房跑步 的开始时间未知，无法核对 07:00 前到家",
      ask_user: "这个选择不值得你想半小时，先去试试。",
      reasoning_tree: {
        root: "用户在今晚 3 个选项间纠结，先理清约束",
        children: [
          {
            agent: "Extractor", summary: "抽出 3 个选项、2 条硬约束、3 个纠结点",
            children: [
              { agent: "Extractor", summary: "识别硬约束：预算不超过 100 块、明早 7 点要起床", children: [] },
              { agent: "Extractor", summary: "识别纠结点：一个人吃尴尬、怕被剧透、最近工作太累", children: [] }
            ]
          },
          {
            agent: "Timeline", summary: "对每个选项推演到未来 48 小时",
            children: [
              {
                agent: "Timeline·健身房跑步", summary: "19:00 出门，单程 40 分钟，跑完 20:30 到家",
                children: [
                  { agent: "Timeline·健身房跑步", summary: "本周运动达成 1/3，明早 7 点起床上班来得及", children: [] }
                ]
              },
              {
                agent: "Timeline·楼下那家火锅店", summary: "步行 5 分钟，人均 80，一个人吃略尴尬",
                children: [
                  { agent: "Timeline·楼下那家火锅店", summary: "本周运动缺口仍在，节律分继续垫底", children: [] }
                ]
              }
            ]
          },
          {
            agent: "Scorer", summary: "7 维加权打分：健身房跑步 6.51 > 楼下那家火锅店 5.61 > 在家看电影 4.72",
            children: [
              { agent: "Scorer", summary: "节律缺口是最大分差来源（10.0 vs 5.0）", children: [] }
            ]
          },
          {
            agent: "Censor", summary: "三个选项花费都在 100 块预算内，无硬约束拦截", children: []
          },
          {
            agent: "Explainer", summary: "只给一个结论：去健身房跑步，附它的 4 个优点",
            children: [
              { agent: "Explainer", summary: "不展示其他选项——展示本身就是内耗", children: [] }
            ]
          }
        ]
      },
      degraded: false,
      blocked: [],
      weights_used: { rhythm: 0.22, social: 0.18, commute: 0.16, cost: 0.10, pleasure: 0.16, health: 0.12, future: 0.06 }
    };

    var profile = {
      user_id: USER_ID, nickname: "演示用户",
      goals: [{ key: "workout_per_week", target: 3, unit: "次/周" }],
      history: [
        { date: "2026-09-24", activity: "去健身房跑步", category: "workout", duration_min: 60, mood: 5 },
        { date: "2026-09-26", activity: "在家休息", category: "rest", duration_min: 240, mood: 3 },
        { date: "2026-09-21", activity: "去楼下那家火锅店", category: "food", duration_min: 90, mood: 5 },
        { date: "2026-09-19", activity: "在家看电影", category: "rest", duration_min: 150, mood: 4 }
      ],
      weights: { rhythm: 0.24, social: 0.20, commute: 0.15, cost: 0.09, pleasure: 0.16, health: 0.10, future: 0.06 },
      facts: ["楼下火锅店的老板娘很热情", "不吃辣", "预算敏感，单晚超过 100 会犹豫", "偏好步行 15 分钟能到的地方"],
      updated_at: now
    };

    var demoCases = [
      { id: "c1", text: "今晚是去健身房跑步，还是去楼下那家火锅店，或者在家看电影？跑步单程40分钟，火锅一个人吃有点尴尬，电影又怕被剧透；预算今晚不超过100块，明早7点还要起床上班。" },
      { id: "c2", text: "周末想把运动捡起来：楼下健身房年卡1800，折合每次25块，但来回加洗澡要一个多小时；或者买台跑步机放家里2800块，就是怕像去年那台动感单车一样骑了三次就挂衣服。选哪个？" },
      { id: "c3", text: "晚饭怎么解决：加班到八点，点外卖30分钟送到、人均35，但重油重盐；还是去楼下超市买菜回家做，人均15块，但买菜做饭洗碗要一个半小时。明早7点要起床上班，选哪个？" }
    ];

    var historyList = [
      { decision_id: "mock-decision-0001", status: "done", created_at: now, raw_text: request.raw_text, top_label: "去健身房跑步", confidence: 0.31, degraded: false },
      { decision_id: "mock-decision-0000", status: "done", created_at: now - 86400 * 5, raw_text: "周末办健身卡还是买跑步机？", top_label: "买跑步机", confidence: 0.28, degraded: true }
    ];

    return { decision: decision, profile: profile, demoCases: demoCases, historyList: historyList };
  })();

  /* ==================== 2. API 层 ==================== */
  /* 所有请求走同源相对路径；mock=1 时返回内置假数据。
     统一 15s 超时 + 错误对象 {ok:false, error}。 */
  var API = (function () {
    function fetchTimeout(url, opts, ms) {
      var ctrl = ("AbortController" in window) ? new AbortController() : null;
      opts = opts || {};
      if (ctrl) opts.signal = ctrl.signal;
      var timer = setTimeout(function () { if (ctrl) ctrl.abort(); }, ms || FETCH_TIMEOUT);
      return fetch(url, opts).finally(function () { clearTimeout(timer); });
    }
    function httpError(e) {
      if (e && e.name === "AbortError") return "请求超时了（15 秒），网络或后端有点慢，再试一次？";
      return "连不上后端（" + ((e && e.message) || "网络错误") + "）";
    }
    function get(path) {
      return fetchTimeout(path).then(function (r) {
        return r.json().catch(function () { throw new Error("后端返回了非 JSON 内容（HTTP " + r.status + "）"); });
      }).then(function (d) {
        if (d && d.ok === false) throw new Error(d.error || "后端返回错误");
        return d;
      }, function (e) { throw new Error(httpError(e)); });
    }
    function post(path, body, ms) {
      return fetchTimeout(path, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body || {})
      }, ms || FETCH_TIMEOUT).then(function (r) {
        return r.json().catch(function () { throw new Error("后端返回了非 JSON 内容（HTTP " + r.status + "）"); });
      }).then(function (d) {
        if (d && d.ok === false) throw new Error(d.error || "后端返回错误");
        return d;
      }, function (e) { throw new Error(httpError(e)); });
    }

    /* mock 包装：模拟一点网络延迟 */
    function fake(v, ms) {
      return new Promise(function (res) { setTimeout(function () { res(typeof v === "object" ? JSON.parse(JSON.stringify(v)) : v); }, ms || 200); });
    }

    var api = {
      health: function () {
        if (MOCK) return fake({ ok: true, mxagent_available: true, version: "mock-1.0" }, 100);
        return get("/api/health");
      },
      demoCases: function () {
        if (MOCK) return fake({ items: Mock.demoCases });
        return get("/api/demo/cases");
      },
      advise: function (text, useLlm) {
        if (MOCK) {
          // 模拟异步任务：立即返回 running，几秒后轮询拿到 done
          Mock._adviseAt = Date.now();
          Mock._fastMode = (useLlm === false);
          return fake({ decision_id: Mock.decision.decision_id, status: "running" }, 300);
        }
        // 长请求：advise 本身可能同步返回完整 Decision，也可能只回 decision_id
        // use_llm=false → 离线快速模式（不调 LLM，秒回）
        return post("/api/advise", { text: text, user_id: USER_ID,
                                     use_llm: (useLlm !== false) }, 30000);
      },
      decision: function (id) {
        if (MOCK) {
          var started = Mock._adviseAt || 0;
          var elapsed = Date.now() - started;
          if (started && elapsed < 4000) {
            return fake({ decision_id: id, status: "running", error: null, result: null }, 250);
          }
          return fake({ decision_id: id, status: "done", error: null, result: Mock.decision }, 250);
        }
        return get("/api/decision/" + encodeURIComponent(id));
      },
      decisions: function () {
        if (MOCK) return fake({ items: Mock.historyList });
        return get("/api/decisions?user_id=" + encodeURIComponent(USER_ID) + "&limit=20");
      },
      feedback: function (body) {
        if (MOCK) return fake({ ok: true, weights: Mock.profile.weights });
        return post("/api/feedback", body);
      },
      profile: function () {
        if (MOCK) return fake(Mock.profile);
        return get("/api/profile?user_id=" + encodeURIComponent(USER_ID));
      },
      resetProfile: function (preset) {
        if (MOCK) {
          if (preset === "new") {
            Mock.profile.history = [];
            Mock.profile.facts = [];
            Mock.profile.weights = { rhythm: 0.22, social: 0.18, commute: 0.16, cost: 0.10, pleasure: 0.16, health: 0.12, future: 0.06 };
          } else {
            Mock.profile.history = [
              { date: "2026-09-24", activity: "去健身房跑步", category: "workout", duration_min: 60, mood: 5 },
              { date: "2026-09-26", activity: "在家休息", category: "rest", duration_min: 240, mood: 3 },
              { date: "2026-09-21", activity: "去楼下那家火锅店", category: "food", duration_min: 90, mood: 5 },
              { date: "2026-09-19", activity: "在家看电影", category: "rest", duration_min: 150, mood: 4 },
              { date: "2026-09-17", activity: "去健身房跑步", category: "workout", duration_min: 60, mood: 5 }
            ];
            Mock.profile.weights = { rhythm: 0.28, social: 0.22, commute: 0.13, cost: 0.07, pleasure: 0.15, health: 0.09, future: 0.06 };
          }
          return fake(Mock.profile);
        }
        return post("/api/profile/reset", { user_id: USER_ID, preset: preset });
      },
      logActivity: function (body) {
        if (MOCK) {
          Mock.profile.history.unshift(body);
          return fake({ ok: true });
        }
        return post("/api/log_activity", body);
      }
    };
    return api;
  })();

  /* ==================== 3. 视图与 Tab ==================== */
  var App = {
    currentDecision: null,
    lastSubmitText: "",
    poll: { timer: null, startedAt: 0, cancelled: false, tick: null, tipTimer: null, stageIdx: -1,
              fastTimer: null, fastRequested: false, lastText: "" }
  };

  function showView(name) {
    ["view-input", "view-wait", "view-result", "view-error"].forEach(function (v) {
      $(v).classList.toggle("hidden", v !== name);
    });
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  function setApiStatus(kind, text) {
    var pill = $("api-pill");
    pill.className = "pill pill-" + kind;
    pill.textContent = text;
  }

  function initTabs() {
    var tabs = document.querySelectorAll(".tab");
    tabs.forEach(function (tab) {
      tab.addEventListener("click", function () {
        tabs.forEach(function (t) { t.classList.remove("active"); });
        tab.classList.add("active");
        document.querySelectorAll(".tab-panel").forEach(function (p) {
          p.classList.toggle("hidden", p.id !== tab.dataset.tab);
        });
        if (tab.dataset.tab === "tab-profile") loadProfile();
        if (tab.dataset.tab === "tab-history") loadHistory();
      });
    });
  }

  /* ==================== 4. 输入区 ==================== */
  function initInput() {
    $("btn-submit").addEventListener("click", submitQuestion);
    $("btn-voice").addEventListener("click", toggleVoice);
    $("select-demo").addEventListener("change", function () {
      var v = this.value;
      if (!v) return;
      $("input-text").value = v;
      this.selectedIndex = 0;
      $("input-text").focus();
    });
    loadDemoCases();
  }

  function loadDemoCases() {
    API.demoCases().then(function (d) {
      var items = (d && (d.items || d.cases)) || [];
      if (!items.length) items = Mock.demoCases;
      var sel = $("select-demo");
      items.forEach(function (c) {
        var text = (typeof c === "string") ? c : (c.text || c.example || "");
        if (!text) return;
        var o = document.createElement("option");
        o.value = text;
        o.textContent = text.length > 26 ? text.slice(0, 26) + "…" : text;
        sel.appendChild(o);
      });
    }).catch(function () {
      // 后端没有该端点也没关系：用内置样例兜底
      Mock.demoCases.forEach(function (c) {
        var o = document.createElement("option");
        o.value = c.text;
        o.textContent = c.text.length > 26 ? c.text.slice(0, 26) + "…" : c.text;
        $("select-demo").appendChild(o);
      });
    });
  }

  /* ---- 语音输入（Web Speech API） ---- */
  var recog = null, recognizing = false;
  function speechSupported() {
    return ("webkitSpeechRecognition" in window) || ("SpeechRecognition" in window);
  }
  function initVoiceSupport() {
    if (speechSupported()) return;
    var btn = $("btn-voice");
    btn.disabled = true;
    btn.title = "当前浏览器不支持语音输入";
    $("voice-label").textContent = "不支持语音";
    btn.classList.add("btn-disabled-note");
  }
  function toggleVoice() {
    if (recognizing) { stopVoice(); return; }
    var SR = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!SR) { toast("当前浏览器不支持语音输入，请打字"); return; }
    try {
      recog = new SR();
      recog.lang = "zh-CN";
      recog.continuous = true;
      recog.interimResults = true;
      var baseText = $("input-text").value ? $("input-text").value.replace(/\s*$/, "") + " " : "";
      recog.onresult = function (ev) {
        var txt = "";
        for (var i = 0; i < ev.results.length; i++) txt += ev.results[i][0].transcript;
        $("input-text").value = baseText + txt;
      };
      recog.onerror = function (ev) {
        stopVoice();
        if (ev.error === "not-allowed" || ev.error === "service-not-allowed")
          toast("麦克风权限被拒绝，请打字输入");
        else toast("语音识别出错了，请打字输入");
      };
      recog.onend = function () { stopVoice(); };
      recog.start();
      recognizing = true;
      $("btn-voice").classList.add("recording");
      $("voice-label").textContent = "正在听…（点按停止）";
    } catch (e) {
      stopVoice();
      toast("语音识别启动失败，请打字输入");
    }
  }
  function stopVoice() {
    if (recog) { try { recog.onend = null; recog.stop(); } catch (e) {} recog = null; }
    recognizing = false;
    $("btn-voice").classList.remove("recording");
    $("voice-label").textContent = "语音说";
  }

  /* ---- 提交 ---- */
  function submitQuestion() {
    var text = $("input-text").value.trim();
    var errEl = $("input-error");
    errEl.classList.add("hidden");
    if (text.length < 4) {
      errEl.textContent = "再多说两句呗——至少得让我知道你在纠结什么 😅";
      errEl.classList.remove("hidden");
      return;
    }
    App.lastSubmitText = text;
    stopVoice();
    startAdvise(text);
  }

  function startAdvise(text) {
    showView("view-wait");
    startWaitAnim();
    App.poll.cancelled = false;
    App.poll.lastText = text;   // 快速模式按钮要用（重新提交同一问题）
    API.advise(text).then(function (resp) {
      if (App.poll.cancelled) return;
      // 情况 A：直接返回完整 Decision（含 cards）
      if (resp && resp.cards && resp.decision_id) { onDecisionDone(resp); return; }
      // 情况 B：返回外层包装 {decision_id,status,result}
      if (resp && resp.decision_id) {
        if (resp.status === "done" && resp.result) { onDecisionDone(resp.result); return; }
        if (resp.status === "failed") { onDecisionFailed(resp.error || "AI 推演失败了，再来一次试试？"); return; }
        pollDecision(resp.decision_id);
        return;
      }
      onDecisionFailed("后端返回了看不懂的数据格式");
    }).catch(function (e) {
      onDecisionFailed(e.message || "提交失败");
    });
  }

  /* ---- 轮询 ---- */
  function pollDecision(id) {
    var p = App.poll;
    function tick() {
      if (p.cancelled) return;
      if (Date.now() - p.startedAt > POLL_TIMEOUT) {
        onDecisionFailed("推演时间太长了（超过 5 分钟）。AI 可能还在后台跑，稍后去「历史决策」页看看结果。");
        return;
      }
      API.decision(id).then(function (d) {
        if (p.cancelled) return;
        var status = d.status || (d.result ? "done" : "running");
        if (status === "done" && d.result) { onDecisionDone(d.result); return; }
        if (status === "failed") { onDecisionFailed(d.error || "AI 推演失败了，再来一次试试？"); return; }
        if (status === "done" && !d.result) { onDecisionFailed("后端说完成了，但没给结果 🤔"); return; }
        p.timer = setTimeout(tick, POLL_INTERVAL);
      }).catch(function (e) {
        if (p.cancelled) return;
        // 单次轮询失败不立刻放弃，继续重试直到超时
        p.waitErr = e.message;
        $("wait-eta").textContent = "连接有点抖，正在自动重试…";
        p.timer = setTimeout(tick, POLL_INTERVAL);
      });
    }
    p.timer = setTimeout(tick, POLL_INTERVAL);
  }

  function onDecisionDone(dec) {
    stopWaitAnim();
    App.currentDecision = dec;
    renderResult(dec);
    showView("view-result");
  }
  function onDecisionFailed(msg) {
    stopWaitAnim();
    $("error-msg").textContent = msg + "。别急，多半是后端还没起来或太忙了。";
    showView("view-error");
  }

  /* ==================== 5. 等待动画 ==================== */
  var WAIT_STAGES = [
    { text: "理解你的纠结", emoji: "👂", sub: "把你说的每个选项、每条顾虑都拆出来" },
    { text: "推演今晚", emoji: "🌆", sub: "如果今晚选了它，时间线会怎么走" },
    { text: "推演未来 48 小时", emoji: "🔮", sub: "明天的精神状态、后天的心情，都算一遍" },
    { text: "权衡节律与通勤", emoji: "⚖️", sub: "对照你的档案：运动频次、通勤、预算" },
    { text: "安全校验", emoji: "🛡️", sub: "检查有没有踩到你的硬约束" },
    { text: "生成建议", emoji: "🃏", sub: "只压一个结论，写清它的好处" }
  ];
  var WAIT_TIPS = [
    "💡 纠结的本质往往是信息不够，AI 正在帮你补齐。",
    "💡 研究表明：多数“选错”其实没那么糟，不选才最消耗人。",
    "💡 AI 不只是排序，它真的把每个选项的明天推演了一遍。",
    "💡 你的档案越丰富，建议就越贴你——记得回来打卡。",
    "💡 好答案值得等 2 分钟。"
  ];

  function startWaitAnim() {
    var p = App.poll;
    p.cancelled = false;
    p.startedAt = Date.now();
    p.stageIdx = -1;
    p.fastRequested = false;

    // 步骤清单
    var stepsEl = $("wait-steps");
    stepsEl.innerHTML = WAIT_STAGES.map(function (s, i) {
      return '<div class="wait-step" id="wstep-' + i + '"><span class="dot">' + (i + 1) + '</span><span>' + esc(s.text) + '</span></div>';
    }).join("");

    // 阶段文案轮播（每 4.5s 一步，共 27s，之后停在最后一步）
    advanceStage();
    p.stageTimer = setInterval(advanceStage, 4500);
    function advanceStage() {
      if (p.stageIdx >= 0) {
        var prev = $("wstep-" + p.stageIdx);
        if (prev) { prev.classList.remove("active"); prev.classList.add("done"); prev.querySelector(".dot").textContent = "✓"; }
      }
      p.stageIdx = Math.min(p.stageIdx + 1, WAIT_STAGES.length - 1);
      var cur = WAIT_STAGES[p.stageIdx];
      var st = $("wait-stage");
      st.style.animation = "none"; void st.offsetWidth; st.style.animation = "";
      st.textContent = cur.emoji + " " + cur.text + "…";
      $("wait-sub").textContent = cur.sub;
      $("wait-emoji").textContent = cur.emoji;
      var el = $("wstep-" + p.stageIdx);
      if (el) el.classList.add("active");
    }

    // 进度条：0→92% 用 150s 缓入曲线，之后缓慢爬向 97%
    p.tick = setInterval(function () {
      var sec = (Date.now() - p.startedAt) / 1000;
      var pct = 92 * (1 - Math.exp(-sec / 45));
      if (sec > 150) pct = Math.min(97, 92 + (sec - 150) * 0.03);
      $("wait-progress").style.width = pct.toFixed(1) + "%";
      $("wait-timer").textContent = "已等待 " + Math.floor(sec) + " 秒";
      if (sec > 180) $("wait-eta").textContent = "快好了，再等等…";
    }, 1000);

    // 底部小贴士轮播
    var ti = 0;
    p.tipTimer = setInterval(function () {
      ti = (ti + 1) % WAIT_TIPS.length;
      $("wait-tip").textContent = WAIT_TIPS[ti];
    }, 7000);

    $("wait-eta").textContent = "通常 1~3 分钟";
    $("wait-progress").style.width = "4%";
    $("wait-timer").textContent = "已等待 0 秒";
  }

  function stopWaitAnim() {
    var p = App.poll;
    clearTimeout(p.timer); p.timer = null;
    clearInterval(p.stageTimer); p.stageTimer = null;
    clearInterval(p.tick); p.tick = null;
    clearInterval(p.tipTimer); p.tipTimer = null;
    clearInterval(p.fastTimer); p.fastTimer = null;
    if (p.fastBtn) p.fastBtn.classList.add("hidden");
  }

  function initWait() {
    var p = App.poll;   // 快速模式按钮与计时器都要用（漏定义会导致 init() 中断）
    $("btn-cancel-wait").addEventListener("click", function () {
      App.poll.cancelled = true;
      stopWaitAnim();
      showView("view-input");
      toast("好吧，那我们再想想");
    });

    // 超过 60s 后出现"先用快速模式"按钮：LLM 是共享资源，随时可能被别人占满，
    // 不能让人干等。点它 → 立即用离线启发式拿一个结果（后端 150s 硬超时也会这么做）。
    var fastBtn = document.createElement("button");
    fastBtn.id = "btn-fast-mode";
    fastBtn.className = "btn btn-ghost btn-sm hidden";
    fastBtn.type = "button";
    fastBtn.textContent = "⚡ 太久了，先用快速模式看看";
    fastBtn.addEventListener("click", function () {
      if (App.poll.fastRequested) return;
      App.poll.fastRequested = true;
      fastBtn.disabled = true;
      fastBtn.textContent = "⚡ 正在切换离线快速模式…";
      var text = App.poll.lastText || "";
      API.advise(text, false).then(function (r) {
        App.poll.decisionId = r.decision_id;
        App.poll.startedAt = Date.now();
        toast("已切换为离线快速模式，马上就好");
      }).catch(function (e) {
        App.poll.fastRequested = false;
        fastBtn.disabled = false;
        fastBtn.textContent = "⚡ 太久了，先用快速模式看看";
        toast("切换失败：" + e.message);
      });
    });
    var waitActions = document.querySelector("#view-wait .wait-actions");
    if (waitActions) waitActions.appendChild(fastBtn);

    // 等待超过 60s 才显示这个按钮（正常情况不需要它）
    p.fastBtn = fastBtn;
    p.fastTimer = setInterval(function () {
      if (p.cancelled) { clearInterval(p.fastTimer); return; }
      var sec = (Date.now() - p.startedAt) / 1000;
      if (sec > 60) fastBtn.classList.remove("hidden");
    }, 5000);
  }

  /* ==================== 6. 结果渲染 ==================== */
  var CARD_META = {
    GO: { cls: "card-go", badge: "GO · 就它了", kicker: "AI 主推" },
    SWITCH: { cls: "card-switch", badge: "SWITCH · 第三种可能", kicker: "你可能没想到的" },
    DROP: { cls: "card-drop", badge: "DROP · 不值得纠结", kicker: "其实这是伪选择" }
  };

  function renderResult(dec) {
    /* --- 置信度环 --- */
    var conf = clamp(dec.confidence, 0, 1);
    var C = 2 * Math.PI * 52;
    var arc = $("conf-arc");
    arc.classList.remove("lv-high", "lv-mid", "lv-low");
    arc.classList.add(conf >= 0.75 ? "lv-high" : conf >= 0.5 ? "lv-mid" : "lv-low");
    arc.style.strokeDashoffset = C;
    setTimeout(function () { arc.style.strokeDashoffset = (C * (1 - conf)).toFixed(1); }, 80);
    // 数字滚动
    var numEl = $("conf-percent"), t0 = null;
    function roll(ts) {
      if (!t0) t0 = ts;
      var k = Math.min((ts - t0) / 1200, 1);
      numEl.textContent = Math.round(conf * 100 * (1 - Math.pow(1 - k, 3))) + "%";
      if (k < 1) requestAnimationFrame(roll);
    }
    requestAnimationFrame(roll);

    $("conf-verdict").textContent =
      conf >= 0.75 ? "这次，AI 替你拍板了 ✅" :
      conf >= 0.5 ? "大方向定了，还有一点小变量" :
      "信息不多也能定，别在这上面耗了";
    $("conf-uncertain").innerHTML = dec.most_uncertain
      ? "最不确定：<b>" + esc(dec.most_uncertain) + "</b>" : "";
    $("conf-degraded-tip").classList.toggle("hidden", !dec.degraded);

    /* --- 收尾一句：不逼用户补信息 --- */
    $("ask-question").textContent = dec.ask_user || "这个选择不值得你想半小时，先去试试。";

    /* --- 只给一个选项（产品主张：展示多个选项本身就是内耗） --- */
    var cards = dec.cards || [];
    $("cards-area").innerHTML = cards.map(function (c, i) {
      var m = CARD_META[c.kind] || CARD_META.GO;
      return '<div class="dcard ' + m.cls + '" style="animation-delay:' + (i * 0.12) + 's">' +
        '<span class="card-badge">' + m.badge + '</span>' +
        '<div class="card-kicker">' + m.kicker + '</div>' +
        "<h3>" + esc(c.title) + "</h3>" +
        "<ul class=\"why-list\">" + (c.why || []).map(function (w) { return "<li>" + esc(w) + "</li>"; }).join("") + "</ul>" +
        '<div class="card-detail">' + esc(c.detail || "") + "</div>" +
        '<div class="flip-hint">👆 点这里看行动细节</div>' +
        "</div>";
    }).join("");
    document.querySelectorAll("#cards-area .dcard").forEach(function (el) {
      el.addEventListener("click", function () { el.classList.toggle("open"); });
    });

    renderCompare(dec);
    renderTree(dec);
    resetFeedback(dec);
  }

  /* --- 选项对比表：只展示被选中的那一个（展示多个选项本身就是内耗） --- */
  function renderCompare(dec) {
    var cards = dec.cards || [];
    var goCard = cards.filter(function (c) { return c.kind === "GO"; })[0];
    var scoredList = (dec.scored || []).slice().sort(function (a, b) { return (b.total || 0) - (a.total || 0); });
    // 只保留 GO 牌对应的那个选项；找不到就取第一名
    var picked = null;
    if (goCard && goCard.option_id) {
      picked = scoredList.filter(function (s) { return (s.option || {}).id === goCard.option_id; })[0] || null;
    }
    if (!picked && scoredList.length) picked = scoredList[0];
    if (!picked) { $("compare-area").innerHTML = '<p class="hint">这次没有可展示的选项。</p>'; return; }

    var s = picked, o = s.option || {}, dims = s.dims || {};
    var dimsHtml = DIM_ORDER.filter(function (k) { return dims[k] !== undefined; }).map(function (k) {
      var v = clamp(dims[k], 0, 10);
      return '<span class="dim-name">' + DIM_LABELS[k] + "</span>" +
        '<span class="dim-track"><span class="dim-fill" data-w="' + (v * 10) + '"></span></span>' +
        '<span class="dim-val">' + v.toFixed(1) + "</span>";
    }).join("");
    var html = '<div class="cmp-row">' +
      '<div class="cmp-head"><div><span class="cmp-name">' + esc(o.label || o.id || "?") + "</span>" +
      '<span class="cmp-cat">' + catLabel(o.category) + "</span></div>" +
      '<div class="cmp-total"><b>' + (s.total || 0).toFixed(1) + "</b>分</div></div>" +
      '<div class="cmp-dims">' + dimsHtml + "</div>" +
      "</div>";
    $("compare-area").innerHTML = html;
    // 延迟触发条形动画
    setTimeout(function () {
      document.querySelectorAll("#compare-area .dim-fill").forEach(function (el) {
        el.style.width = (el.dataset.w || 0) + "%";
      });
    }, 100);
  }
  /* --- 推理树 --- */
  function renderTree(dec) {
    var tree = dec.reasoning_tree;
    if (!tree || (!tree.root && !tree.children)) {
      $("tree-area").innerHTML = '<p class="hint">这次没有留下推理记录。</p>';
      return;
    }
    function nodeHtml(node, isRoot) {
      var kids = node.children || [];
      var head = '<div class="tnode-head">' +
        '<span class="tnode-arrow">' + (kids.length ? "▼" : "•") + "</span>" +
        (node.agent ? '<span class="tnode-agent">' + esc(node.agent) + "</span>" : "") +
        '<span class="tnode-summary">' + esc(node.summary || node.root || "") + "</span></div>";
      var kidsHtml = kids.length
        ? '<div class="tnode-kids">' + kids.map(function (k) { return nodeHtml(k, false); }).join("") + "</div>"
        : "";
      return '<div class="tnode' + (isRoot ? " tnode-root" : "") + '">' + head + kidsHtml + "</div>";
    }
    $("tree-area").innerHTML = nodeHtml(tree, true);
    $("tree-area").querySelectorAll(".tnode-head").forEach(function (h) {
      h.addEventListener("click", function () {
        var n = h.parentElement;
        if (n.querySelector(".tnode-kids")) n.classList.toggle("collapsed");
      });
    });
  }

  /* --- 结果页底部按钮（不再有"补充信息重新推演"：那是在诱导二次纠结） --- */
  function initAsk() {
    $("btn-new-question").addEventListener("click", function () {
      $("input-text").value = "";
      showView("view-input");
    });
  }

  /* ==================== 7. 打卡闭环 ==================== */
  var fbState = { went: true, satisfaction: 0 };

  function resetFeedback(dec) {
    fbState = { went: true, satisfaction: 0 };
    var sel = $("fb-chosen");
    var opts = (dec.scored || []).map(function (s) {
      var label = (s.option && s.option.label) || s.option_id || "?";
      return '<option value="' + esc(label) + '">' + esc(label) + "</option>";
    });
    if (!opts.length) opts = ['<option value="">（无选项）</option>'];
    sel.innerHTML = opts.join("");
    $("fb-note").value = "";
    $("fb-done").classList.add("hidden");
    document.querySelectorAll("#fb-went .seg-btn").forEach(function (b, i) {
      b.classList.toggle("active", i === 0);
    });
    paintStars(0);
    $("btn-feedback").disabled = false;
  }

  function paintStars(n) {
    document.querySelectorAll("#fb-stars .star").forEach(function (s) {
      s.classList.toggle("lit", Number(s.dataset.v) <= n);
    });
    var labels = ["点一下星星", "😖 糟透了", "😕 不太行", "😐 还行", "🙂 挺开心", "🤩 超值！"];
    $("fb-stars-label").textContent = labels[n] || "";
  }

  function initFeedback() {
    document.querySelectorAll("#fb-went .seg-btn").forEach(function (b) {
      b.addEventListener("click", function () {
        fbState.went = b.dataset.v === "1";
        document.querySelectorAll("#fb-went .seg-btn").forEach(function (x) { x.classList.remove("active"); });
        b.classList.add("active");
      });
    });
    document.querySelectorAll("#fb-stars .star").forEach(function (s) {
      s.addEventListener("click", function () {
        fbState.satisfaction = Number(s.dataset.v);
        paintStars(fbState.satisfaction);
      });
    });
    $("btn-feedback").addEventListener("click", function () {
      var dec = App.currentDecision;
      if (!dec || !dec.decision_id) { toast("还没有可打卡的决策"); return; }
      if (!fbState.satisfaction) { toast("先给个星级吧 ⭐"); return; }
      var btn = $("btn-feedback");
      btn.disabled = true;
      API.feedback({
        decision_id: dec.decision_id,
        chosen: $("fb-chosen").value,
        went: fbState.went,
        satisfaction: fbState.satisfaction,
        note: $("fb-note").value.trim()
      }).then(function () {
        $("fb-done").classList.remove("hidden");
        toast("打卡成功，谢谢你回来告诉我 🌱");
      }).catch(function (e) {
        btn.disabled = false;
        toast("打卡失败了：" + e.message);
      });
    });
  }

  /* ==================== 8. 档案页 ==================== */
  function loadProfile() {
    API.profile().then(renderProfile).catch(function (e) {
      ["prof-goals", "prof-facts", "prof-weights", "prof-history"].forEach(function (id) {
        $(id).innerHTML = '<span class="prof-empty">读不到档案：' + esc(e.message) + "</span>";
      });
    });
  }

  function renderProfile(p) {
    p = p || {};
    var goals = (p.goals || []).map(function (g) {
      if (typeof g === "string") return '<div class="prof-item">' + esc(g) + "</div>";
      return '<div class="prof-item">🎯 <b>' + esc(g.key || "") + "</b>：" + esc(g.target ?? "") + " " + esc(g.unit || "") + "</div>";
    }).join("");
    $("prof-goals").innerHTML = goals || '<span class="prof-empty">还没有目标。跳一次舞，AI 会帮你立一个。</span>';

    var facts = (p.facts || []).map(function (f) { return '<div class="prof-item">📌 ' + esc(f) + "</div>"; }).join("");
    $("prof-facts").innerHTML = facts || '<span class="prof-empty">还没有长期事实，多聊几次就有了。</span>';

    var w = p.weights || {};
    var keys = DIM_ORDER.filter(function (k) { return w[k] !== undefined; });
    Object.keys(w).forEach(function (k) { if (keys.indexOf(k) < 0) keys.push(k); });
    var maxW = Math.max.apply(null, keys.map(function (k) { return Number(w[k]) || 0; }).concat([0.01]));
    $("prof-weights").innerHTML = keys.map(function (k) {
      var v = Number(w[k]) || 0;
      return '<div class="weight-row"><span class="dim-name">' + (DIM_LABELS[k] || k) + "</span>" +
        '<span class="dim-track"><span class="dim-fill" data-w="' + ((v / maxW) * 100).toFixed(0) + '"></span></span>' +
        '<span class="weight-val">' + v.toFixed(2) + "</span></div>";
    }).join("") || '<span class="prof-empty">还没有学到权重。</span>';
    setTimeout(function () {
      document.querySelectorAll("#prof-weights .dim-fill").forEach(function (el) { el.style.width = el.dataset.w + "%"; });
    }, 60);

    var hist = (p.history || []).slice(0, 12).map(function (h) {
      return '<div class="prof-item">' + esc(h.date || "") + " · <b>" + esc(h.activity || "") + "</b> " +
        catLabel(h.category) + (h.duration_min > 0 ? " · " + h.duration_min + " 分钟" : "") +
        (h.mood ? " · " + "★".repeat(clamp(h.mood, 1, 5)) : "") + "</div>";
    }).join("");
    $("prof-history").innerHTML = hist || '<span class="prof-empty">还没有活动记录，下面记一笔试试。</span>';
  }

  function initProfileActions() {
    $("btn-reset-new").addEventListener("click", function () { doReset("new", this); });
    $("btn-reset-veteran").addEventListener("click", function () { doReset("veteran", this); });
    function doReset(preset, btn) {
      btn.disabled = true;
      API.resetProfile(preset).then(function (p) {
        renderProfile(p);
        toast(preset === "new" ? "已重置为新用户 🐣" : "已载入 3 周老用户 💪");
      }).catch(function (e) { toast("操作失败：" + e.message); })
        .finally(function () { btn.disabled = false; });
    }

    $("btn-log").addEventListener("click", function () {
      var activity = $("log-activity").value.trim();
      var msg = $("log-msg");
      msg.classList.remove("hidden", "ok", "err");
      if (!activity) { msg.textContent = "写点什么吧，比如“去健身房跑步”"; msg.classList.add("err"); return; }
      var dur = parseInt($("log-duration").value, 10);
      var body = {
        user_id: USER_ID,
        activity: activity,
        category: $("log-category").value,
        duration_min: isNaN(dur) || dur <= 0 ? -1 : dur,
        mood: parseInt($("log-mood").value, 10) || 3
      };
      $("btn-log").disabled = true;
      API.logActivity(body).then(function () {
        msg.textContent = "✅ 记下了！AI 的节律分更准了。";
        msg.classList.add("ok");
        $("log-activity").value = ""; $("log-duration").value = "";
        loadProfile();
      }).catch(function (e) {
        msg.textContent = "没记上：" + e.message;
        msg.classList.add("err");
      }).finally(function () { $("btn-log").disabled = false; });
    });
  }

  /* ==================== 9. 历史页 ==================== */
  function histItemHtml(d) {
    var ts = d.created_at || 0;
    var dd = new Date(ts * 1000);
    var status = d.status || "done";
    var conf = (d.confidence !== undefined && d.confidence !== null)
      ? "置信 " + Math.round(d.confidence * 100) + "%" : "";
    return '<div class="card glass hist-item" data-id="' + esc(d.decision_id || "") + '">' +
      '<div class="hist-date"><b>' + (isNaN(dd.getTime()) ? "--" : dd.getDate()) + "</b><span>" +
      (isNaN(dd.getTime()) ? "" : (dd.getMonth() + 1) + "月") + "</span></div>" +
      '<div class="hist-main"><div class="hist-text">' + esc(d.raw_text || d.top_label || "(无内容)") + "</div>" +
      '<div class="hist-meta"><span class="hist-status ' + esc(status) + '">' + esc(status) + "</span>" +
      (d.top_label ? "<span>推荐：" + esc(d.top_label) + "</span>" : "") +
      (conf ? "<span>" + conf + "</span>" : "") +
      (d.degraded ? "<span>⚠️ 降级</span>" : "") + "</div></div>" +
      '<div class="hist-go">›</div></div>';
  }

  function loadHistory() {
    API.decisions().then(function (d) {
      var items = (d && d.items) || [];
      $("history-list").innerHTML = items.length
        ? items.map(histItemHtml).join("")
        : '<div class="card glass"><p class="hint">还没有历史决策。去「帮我选」问第一个纠结吧。</p></div>';
      document.querySelectorAll("#history-list .hist-item").forEach(function (el) {
        el.addEventListener("click", function () { openHistoryDetail(el.dataset.id); });
      });
    }).catch(function (e) {
      $("history-list").innerHTML = '<div class="card glass"><p class="hint">读不到历史：' + esc(e.message) + "</p></div>";
    });
  }

  function openHistoryDetail(id) {
    if (!id) return;
    API.decision(id).then(function (d) {
      var dec = d.result || (d.cards ? d : null);
      var body = $("modal-body");
      if (!dec) {
        var st = d.status || "unknown";
        body.innerHTML = '<p class="hint">这条决策当前状态：' + esc(st) +
          (d.error ? "（" + esc(d.error) + "）" : "") + "。等它跑完再来看吧。</p>";
      } else {
        var cards = (dec.cards || []).map(function (c) {
          var m = CARD_META[c.kind] || CARD_META.GO;
          return '<div class="dcard ' + m.cls + '" style="margin-bottom:12px">' +
            '<span class="card-badge">' + m.badge + "</span><h3>" + esc(c.title) + "</h3>" +
            '<ul class="why-list">' + (c.why || []).map(function (w) { return "<li>" + esc(w) + "</li>"; }).join("") + "</ul>" +
            '<div class="card-detail" style="display:block">' + esc(c.detail || "") + "</div></div>";
        }).join("");
        body.innerHTML =
          '<p class="hint" style="margin-bottom:10px">' + esc(fmtDate(dec.created_at)) + " · " + esc(dec.request && dec.request.raw_text ? dec.request.raw_text.slice(0, 80) : "") + "</p>" +
          (cards || '<p class="hint">这次没有牌面。</p>');
      }
      $("modal-history").classList.remove("hidden");
    }).catch(function (e) { toast("打不开详情：" + e.message); });
  }

  function initModal() {
    $("btn-modal-close").addEventListener("click", function () { $("modal-history").classList.add("hidden"); });
    document.querySelectorAll("#modal-history .modal-mask").forEach(function (m) {
      m.addEventListener("click", function () { $("modal-history").classList.add("hidden"); });
    });
  }

  /* ==================== 10. 初始化 ==================== */
  function checkBackend() {
    API.health().then(function (h) {
      if (h && h.ok !== false) {
        setApiStatus("green", MOCK ? "演示模式" : "已连接");
      } else {
        setApiStatus("red", "后端异常");
      }
    }).catch(function () {
      setApiStatus("red", "后端未就绪");
    });
  }

  function init() {
    if (MOCK) $("mock-pill").classList.remove("hidden");
    initTabs();
    initInput();
    initVoiceSupport();
    initWait();
    initAsk();
    initFeedback();
    initProfileActions();
    initModal();
    checkBackend();
    // 每 60s 重探一次后端
    setInterval(checkBackend, 60000);
    showView("view-input");
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
