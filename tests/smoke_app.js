/* 冒烟测试（临时）：在 Node 里模拟最小 DOM/fetch，加载 app.js 并驱动 mock 全流程。
   运行：node tests/smoke_app.js  （在 choice/ 目录下） */
const fs = require("fs");
const vm = require("vm");

const ids = {};
function mkEl(id) {
  const el = {
    id: id,
    classList: {
      _s: new Set(),
      add() { for (const c of arguments) this._s.add(c); },
      remove() { for (const c of arguments) this._s.delete(c); },
      toggle(c, f) { if (f === undefined) f = !this._s.has(c); f ? this._s.add(c) : this._s.delete(c); },
      contains(c) { return this._s.has(c); }
    },
    style: {}, dataset: {}, innerHTML: "", textContent: "", value: "",
    appendChild() {}, addEventListener(ev, fn) { (this._h = this._h || {})[ev] = fn; },
    querySelector() { return mkEl(null); }, querySelectorAll() { return []; },
    focus() {}, selectedIndex: 0, disabled: false
  };
  if (id) ids[id] = el;
  return el;
}

global.document = {
  readyState: "complete",
  getElementById(id) { if (!ids[id]) ids[id] = mkEl(id); return ids[id]; },
  querySelectorAll() { return []; },
  querySelector() { return mkEl(null); },
  createElement(t) { return mkEl(null); },
  addEventListener() {}
};
global.window = { scrollTo() {} };
global.location = { search: "?mock=1" };
const fetchCalls = [];
global.fetch = (url, opts) => {
  fetchCalls.push([url, opts && opts.method]);
  return Promise.reject(new Error("mock mode should not fetch " + url));
};
global.requestAnimationFrame = cb => setTimeout(() => cb(Date.now() + 2000), 1);

// 预注册 HTML 中的全部 id，便于断言
const html = fs.readFileSync("web/index.html", "utf8");
for (const m of html.matchAll(/id="([^"]+)"/g)) mkEl(m[1]);

vm.runInThisContext(fs.readFileSync("web/app.js", "utf8"), { filename: "app.js" });
console.log("[SMOKE] app.js loaded in mock mode, no exception");

setTimeout(() => {
  ids["input-text"].value = "今晚是去健身房跑步，还是去楼下那家火锅店，或者在家看电影？";
  const h = ids["btn-submit"]._h && ids["btn-submit"]._h.click;
  if (!h) { console.error("[SMOKE] FAIL: submit handler not registered"); process.exit(1); }
  h.call(ids["btn-submit"]);
  // mock 内部有 4s 推演延迟 + 1.5s 轮询间隔，这里等 7s 真实时间
  setTimeout(() => {
    const cards = ids["cards-area"].innerHTML;
    const cmp = ids["compare-area"].innerHTML;
    const tree = ids["tree-area"].innerHTML;
    const conf = ids["conf-percent"].textContent;
    const checks = [
      ["GO 牌渲染", cards.includes("card-go") && cards.includes("就它了")],
      ["不渲染 SWITCH（只给一个结论）", !cards.includes("card-switch")],
      ["不渲染 DROP（只给一个结论）", !cards.includes("card-drop")],
      ["置信度渲染", String(conf).includes("%")],
      ["对比表渲染", cmp.includes("cmp-row") && cmp.includes("节律缺口")],
      ["推理树渲染", tree.includes("tnode") && tree.includes("Timeline")],
      ["degraded 提示隐藏", ids["conf-degraded-tip"].classList.contains("hidden")],
      ["结果视图切换", !ids["view-result"].classList.contains("hidden")],
      ["无真实 fetch 发生", fetchCalls.length === 0]
    ];
    let fail = 0;
    for (const [name, ok] of checks) {
      console.log("[SMOKE]", ok ? "PASS" : "FAIL", "-", name);
      if (!ok) fail++;
    }
    console.log(fail ? "[SMOKE] RESULT: FAIL" : "[SMOKE] RESULT: ALL PASS");
    process.exit(fail ? 1 : 0);
  }, 7000);
}, 100);
