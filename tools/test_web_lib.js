// tools/test_web_lib.js —— web/lib.js 的回归测试 + 页面脚本的结构检查
//
// 用法：node tools\test_web_lib.js        （纯逻辑，不用开程序、不碰真实数据）
//
// 【为什么有它】
// 2026-09-22 把 index.html <script> 里的纯展示函数搬进了 web/lib.js。搬的时候踩过一次：
// 用「数花括号」找函数结尾被字符串里的花括号骗到，多删了一个 }，
// 结果 renderPositionRow 被套进别的函数里 —— 文本还在、按名字点数也查不出来，
// 但页面里 "renderPositionRow is not defined"，列表和看板直接变成离线页。
// 所以这份测试里有一条**结构检查**：页面上以两个空格缩进声明的每个顶层函数，
// 加载后必须真的成为全局函数（在 Node 里用最小浏览器桩跑一遍页面脚本就能验）。
"use strict";
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const WEB = path.join(__dirname, "..", "app", "web");
const LIB = path.join(WEB, "lib.js");
const HTML = path.join(WEB, "index.html");

let ok = true;
function check(label, cond, extra) {
  if (!cond) ok = false;
  console.log((cond ? "[PASS] " : "[FAIL] ") + label + (extra ? "  " + extra : ""));
}

// ---------------------------------------------------------------- 最小浏览器桩
function fakeElement() {
  const store = {};
  return new Proxy(store, {
    get(target, prop) {
      if (prop === "classList") return { add() {}, remove() {}, toggle() {}, contains() { return false; } };
      if (prop === "style") return new Proxy({}, { get: () => "", set: () => true });
      if (prop === "dataset") return {};
      if (prop === "value" || prop === "innerHTML" || prop === "textContent" || prop === "checked") {
        return target[prop] === undefined ? "" : target[prop];
      }
      if (prop === "tagName") return "DIV";
      if (prop === "children" || prop === "childNodes") return [];
      if (prop === "querySelectorAll") return () => [];
      if (prop === "closest") return () => null;
      if (prop in target) return target[prop];
      return function () { return fakeElement(); };
    },
    set(target, prop, value) { target[prop] = value; return true; },
  });
}

function makeSandbox() {
  const storage = new Map();
  const doc = {
    addEventListener() {}, removeEventListener() {},
    getElementById: () => fakeElement(),
    querySelector: () => fakeElement(),
    querySelectorAll: () => [],
    createElement: () => fakeElement(),
    createTextNode: () => fakeElement(),
    documentElement: fakeElement(),
    body: fakeElement(),
    head: fakeElement(),
    execCommand: () => true,
  };
  const sandbox = {
    document: doc,
    location: { protocol: "http:", hash: "", search: "", href: "http://127.0.0.1:8768/" },
    navigator: { clipboard: { writeText: () => Promise.resolve() }, language: "zh-CN" },
    localStorage: {
      getItem: (k) => (storage.has(k) ? storage.get(k) : null),
      setItem: (k, v) => storage.set(k, String(v)),
      removeItem: (k) => storage.delete(k),
      clear: () => storage.clear(),
    },
    // fetch 永远不 settle：load() 的链子停在那儿，页面不会真的去渲染
    fetch: () => new Promise(() => {}),
    setTimeout: () => 0, clearTimeout() {}, setInterval: () => 0, clearInterval() {},
    requestAnimationFrame: () => 0, cancelAnimationFrame() {},
    addEventListener() {}, removeEventListener() {},
    alert() {}, confirm: () => true, prompt: () => null,
    console: { log() {}, warn() {}, error() {} },
    URL: { createObjectURL: () => "blob:x", revokeObjectURL() {} },
    Blob: function () {}, FileReader: function () { this.readAsText = () => {}; },
    Date, Math, JSON, Promise, Error, RegExp, Object, Array, String, Number, Boolean, isNaN, parseInt, parseFloat,
  };
  sandbox.window = sandbox;
  sandbox.globalThis = sandbox;
  return sandbox;
}

function readPageScript() {
  const lines = fs.readFileSync(HTML, "utf8").split(/\r?\n/);
  const starts = lines.map((l, i) => [l, i]).filter(([l]) => l.trim().startsWith("<script"));
  const ends = lines.map((l, i) => [l, i]).filter(([l]) => l.trim() === "</script>");
  const a = starts[starts.length - 1][1];
  const b = ends[ends.length - 1][1];
  return { source: lines.slice(a + 1, b).join("\n"), offset: a + 2 };
}

// ---------------------------------------------------------------- 1) 边界检查
const libSrc = fs.readFileSync(LIB, "utf8");
const FORBIDDEN = ["document.", "window.", "localStorage", "fetch(", "alert(", "S.", "S[",
                   "filters.", "filters[", "expanded", "addEventListener", "onclick"];
const dirty = FORBIDDEN.filter((k) => libSrc.includes(k));
check("lib.js 只放纯函数（不碰 DOM / 全局状态 / 网络）", dirty.length === 0,
      dirty.length ? "出现了：" + dirty.join("、") : "检查了 " + FORBIDDEN.length + " 个关键词");

// ---------------------------------------------------------------- 2) 函数行为
const ctx = vm.createContext(makeSandbox());
vm.runInContext(libSrc, ctx, { filename: "lib.js" });
const g = ctx;

check("esc() 转义 HTML 特殊字符",
      g.esc('<a href="x">&\'') === "&lt;a href=&quot;x&quot;&gt;&amp;&#39;", g.esc("<&>"));
check("fmtDate() 只取日期部分", g.fmtDate("2026-09-16 14:30:00") === "2026-09-16", g.fmtDate("2026-09-16 14:30:00"));
check("fmtDate() 空值给空串", g.fmtDate("") === "" && g.fmtDate(null) === "");
check("today() 是 YYYY-MM-DD", /^\d{4}-\d{2}-\d{2}$/.test(g.today()), g.today());
check("pad() 补零", g.pad(7) === "07" && g.pad(12) === "12");
// 注意：daysSince() 自己看时钟，而这里也看时钟 —— **正好卡在零点那一刻会失败**
// （2026-09-22 深夜真撞上了一次：算"今天"时还是 22 号，调 daysSince 时已经 23 号）。
// 所以先把基准时间抓下来，跑完再确认有没有跨天；跨了就按跨天后的期望值判。
const dBase = new Date();
const dToday = g.today();
// 昨天要按**本地时间**算：toISOString() 是 UTC，本地刚过零点时会差一天
// （2026-09-22 深夜实测：本地 00:0x、UTC 还是前一天 16:0x，减 24 小时再按 UTC 取日期就成了"前天"）
const dYesterdayDate = new Date(dBase.getTime() - 86400000);
const dYesterday = dYesterdayDate.getFullYear() + "-" + g.pad(dYesterdayDate.getMonth() + 1)
  + "-" + g.pad(dYesterdayDate.getDate());
const dTodayDays = g.daysSince(dToday);
const dYesterdayDays = g.daysSince(dYesterday);
const rolledOver = g.today() !== dToday;
check("daysSince() 今天=0、昨天=1" + (rolledOver ? "（跑的过程中跨了零点，按跨天算）" : ""),
      rolledOver ? (dTodayDays === 1 && dYesterdayDays === 2)
                 : (dTodayDays === 0 && dYesterdayDays === 1),
      "今天=" + dTodayDays + " 昨天=" + dYesterdayDays);
check("statusRankOf()：Offer 排在 进行中 前面",
      g.statusRankOf("Offer") < g.statusRankOf("进行中"), "Offer=" + g.statusRankOf("Offer") + " 进行中=" + g.statusRankOf("进行中"));
check("bestStatusOf() 取最靠前的状态（看 derived_status）",
      g.bestStatusOf([{ derived_status: "进行中" }, { derived_status: "Offer" }]) === "Offer",
      g.bestStatusOf([{ derived_status: "进行中" }, { derived_status: "Offer" }]));
check("bestStatusOf() 空数组回落「未登记」", g.bestStatusOf([]) === "未登记");
check("leadItemOf() 挑出最靠前的那条，并在同档时取最近登记的",
      g.leadItemOf([{ id: 1, derived_status: "进行中", last_event_at: "2026-09-01" },
                    { id: 2, derived_status: "Offer", last_event_at: "2026-09-02" },
                    { id: 3, derived_status: "已拒绝", last_event_at: "2026-09-03" }]).id === 2);
check("companyGroups() 同一家公司归一组",
      g.companyGroups([{ company: "甲公司", id: 1 }, { company: "甲公司", id: 2 }]).length === 1);
check("safeUrl() 挡掉 javascript:",
      g.safeUrl("javascript:alert(1)") === "" || !/^javascript:/i.test(g.safeUrl("javascript:alert(1)")),
      JSON.stringify(g.safeUrl("javascript:alert(1)")));
check("dashCard() 产出卡片 HTML", /dash-card/.test(g.dashCard("标题", "<b>x</b>")));
const series = [{ label: "2026-09-01", value: 3 }, { label: "2026-09-02", value: 5 }];
check("lineChartSvg() 产出 svg", /<svg/.test(g.lineChartSvg(series)));
check("lineChartSvg() 每条数据都有标签（少一个就会 TypeError）",
      /<svg/.test(g.lineChartSvg([{ label: "2026-09-03", value: 0 }])));
check("donutSvg() 产出 svg（buckets + total + 标签）",
      /<svg/.test(g.donutSvg({ offer: 1, active: 2, reject: 0, drop: 0 }, 3, "3")));
check("ageText()：0=今天、1=昨天、n=已 n 天、null=空",
      g.ageText(0) === "今天登记" && g.ageText(1) === "昨天登记" && g.ageText(5) === "已 5 天" && g.ageText(null) === "");
check("ageClass()：新登记与拖延不是同一档", g.ageClass(0) !== g.ageClass(30),
      g.ageClass(0) + " / " + g.ageClass(30));
// 现状：Offer/已拒绝/已放弃/未登记 各有专门类名，只有「进行中」返回空串（别想当然）
check("statusClass()：四种状态有专门类名，「进行中」是空串",
      g.statusClass("Offer") === "offer" && g.statusClass("已拒绝") === "reject"
      && g.statusClass("已放弃") === "drop" && g.statusClass("未登记") === "none"
      && g.statusClass("进行中") === "",
      [g.statusClass("Offer"), g.statusClass("已拒绝"), g.statusClass("已放弃"),
       g.statusClass("未登记"), JSON.stringify(g.statusClass("进行中"))].join(" / "));
check("resultClass() 对任意结果都给得出字符串",
      ["进行中", "已完成", "未通过", "已放弃", ""].every((s) => typeof g.resultClass(s) === "string"));
check("fitShort()：强对口与不对口给的不是同一个东西",
      g.fitShort("strong") !== g.fitShort("low") && g.fitShort("strong").length > 0);
check("weekdayLabel() 认识周三", String(g.weekdayLabel("2026-09-16")).indexOf("三") >= 0, String(g.weekdayLabel("2026-09-16")));

// ---------------------------------------------------------------- 3) 页面结构检查
const page = readPageScript();
const declared = [...page.source.matchAll(/^ {2}function\s+([A-Za-z_$][\w$]*)\s*\(/gm)].map((m) => m[1]);
check("页面里还有顶层函数声明", declared.length > 50, "找到 " + declared.length + " 个");

// 页面那个 <script> 是个 IIFE（结尾 })();），里面的函数**不是全局的**。
// 所以真正的规则是：lib.js 里的函数**不能回调页面函数** —— 会 undefined，
// 再被 load() 的 catch 吞成「离线」，整页提示连不上后台（2026-09-22 真的这么炸过一次）。
// 检查前先把注释去掉，免得注释里提到的函数名被当成真调用。
function stripComments(src) {
  return src.replace(/\/\*[\s\S]*?\*\//g, "").replace(/^[ \t]*\/\/.*$/gm, "");
}
const libCode = stripComments(libSrc);
const libNames = [...libSrc.matchAll(/^[ \t]{0,2}function\s+([A-Za-z_$][\w$]*)\s*\(/gm)].map((m) => m[1]);
// 注意：不能只找「名字(」—— items.map(renderPositionRow) 这种把函数当参数传的写法没有括号，
// 上一版就是这么漏掉的。按 JS 标识符边界匹配（前后不能是字母/数字/_/$）。
// 两条规则：① items.map(fn) 这种不带括号的引用也算（上一版漏在这里）；
//           ② 只有 $ 特殊——它是 DOM 助手，代码里永远写成 $(...)，
//              而 fmtDate 的正则 /.*$/ 和替换串 "$1-$2" 里的 $ 不算引用。
const esc4re = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
const isRef = (n, text) => (n === "$"
  ? text.includes("$(")
  : new RegExp("(?<![\\w$])" + esc4re(n) + "(?![\\w$])").test(text));
const leaked = declared.filter((n) => isRef(n, libCode));
check("lib.js 不回调页面里的函数（页面是 IIFE，回调会 undefined 并整页变离线）",
      leaked.length === 0, leaked.length ? "引用了：" + leaked.join("、") : "检查了 " + declared.length + " 个页面函数");

const dup = libNames.filter((n) => declared.includes(n));
check("lib.js 与页面没有重复定义", dup.length === 0, dup.length ? dup.join("、") : "");
check("lib.js 里有一批纯函数", libNames.length >= 30, libNames.length + " 个");

// ---------------------------------------------------------------- 4) 关键调用点还在
// 这三个渲染函数是页面**状态**的一部分（要读 expanded / S / filters），当初拆 lib.js 时特意留在页面里，
// 搬走就会像 2026-09-22 那次一样整页变「离线」。renderCard 是旧的单卡片渲染，
// 早就没人调用、2026-09-22 顺手删了，所以不在这份名单里。
const pageCalls = ["renderPositionRow", "renderList", "renderTalkDays"].filter((n) => !new RegExp("\\b" + n + "\\s*\\(").test(page.source));
check("页面里还在用列表渲染函数（搬走容易漏）", pageCalls.length === 0, pageCalls.join("、"));
check("页面通过相对路径加载 lib.js", /<script src="lib\.js"><\/script>/.test(fs.readFileSync(HTML, "utf8")));

// 行为回归：执行真正的页面脚本，固定日期并检查主列表和折叠区。
const pageBox = makeSandbox();
const nodes = new Map();
pageBox.document.getElementById = id => {
  if(!nodes.has(id)) nodes.set(id, fakeElement());
  return nodes.get(id);
};
const requests = [], intervals = [];
pageBox.fetch = (url) => { requests.push(url); return new Promise(() => {}); };
pageBox.setInterval = (fn, ms) => { intervals.push(ms); return 1; };
const pageCtx = vm.createContext(pageBox);
vm.runInContext(libSrc, pageCtx);
const exposed = page.source.replace(/\}\)\(\);\s*$/, `
window.testPage = {renderTalks, startDesktopBridge, refreshCalendarDay,
  setState: function(value){ S=value; },
  setDesktop: function(){ DESKTOP=true; SHELL_PORT="4711"; },
  // ⚠️ 设完"今天是哪天"要顺手调一次 refreshCalendarDay()：页面里的 renderedDay 是**加载时按真实日期**
  //    记下来的，只改 today() 而不刷新它，等到测试把日期滚到"真实今天"那一天（例如 2026-10-11 当天跑），
  //    refreshCalendarDay() 会判定没有跨日而直接返回 —— 用例就会莫名其妙变红（2026-10-11 实测踩到）。
  setDay: function(day){ today=function(){return day;}; refreshCalendarDay(); },
  selectTalks: function(){filters.view="talks";},
  applied: function(){filters.talkFit="applied";}};
})();`);
vm.runInContext(exposed, pageCtx);
const p = pageCtx.testPage;
p.setDay("2026-10-10");
const fixtures = [
  {id:1, company:"Yesterday", talk_date:"2026-10-09", status:"待参加"},
  {id:2, company:"Today", talk_date:"2026-10-10", status:"待参加"},
  {id:3, company:"Tomorrow", talk_date:"2026-10-11", status:"待参加"},
  {id:4, company:"Applied", talk_date:"2026-10-10", status:"已投递"},
  {id:5, company:"Undated", talk_date:"", status:"待参加"}
];
const before = JSON.stringify(fixtures);
p.setState({applications:[], talks:fixtures, stages:[], stats:{}});
p.renderTalks();
let html = nodes.get("talks").innerHTML;
let split = html.indexOf('<div class="talk-archive');
check("10月10日主列表从今天开始，昨天移入往期", split > html.indexOf("Today") && html.indexOf("Yesterday") > split);
check("未来及未定日期保留，已投递仍折叠", html.indexOf("Tomorrow") < split && html.indexOf("Undated") < split && html.indexOf("Applied") > split);
p.selectTalks();
p.setDay("2026-10-11");
p.refreshCalendarDay();
html = nodes.get("talks").innerHTML;
split = html.indexOf('<div class="talk-archive');
check("跨日自动刷新后昨日场次移入折叠区", html.indexOf("Tomorrow") < split && html.indexOf("Today") > split);
check("日期归档不改写原始记录", JSON.stringify(fixtures) === before);
p.applied(); p.renderTalks();
html = nodes.get("talks").innerHTML;
check("已投递筛选仍能查到往期投递", html.includes("Applied") && !html.includes("Yesterday"));
p.setDesktop();
const count = requests.length;
p.startDesktopBridge();
check("桌面加载立即发带归属的心跳", requests.length === count+1 && requests.at(-1).includes("/api/heartbeat?shport=4711"));
check("心跳间隔小于关闭宽限", intervals.includes(5000));

console.log("\n结果：" + (ok ? "全部通过" : "存在失败项"));
process.exit(ok ? 0 : 1);
