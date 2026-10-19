// 桌面版界面自检：用无界面 Chrome 打开各个视图，看该有的东西有没有渲染出来。
//
// 用法：
//   node tools\verify_desktop.js                检查 2.0（8766，导航里有「时间链」）
//   node tools\verify_desktop.js 8768 no-snake  检查 v3（8768，时间链已移除）
//
// 第 1 个参数是端口（默认 8766）；第 2 个参数写 no-snake 表示这一版没有时间链。
// 两个版本共用这一份脚本，所以别把端口或「有没有时间链」写死在代码里。
//
// 共 26 项。**凡是"应该长什么样"的判断，都先拉 /api/state 算出预期再对，不写死数量。**
// 其中 3 项（首页「进行中」的条目内容）在库里没有对应数据时无从渲染，会打 [SKIP]。
//
// 【数渲染结果之前必须先剥掉 <script>/<style>】`--dump-dom` 会把源码一起带出来
//   （AGENTS.md 里记着这个坑）。2026-09-22 实测被它坑了多少：
//     投递列表 64 条真的被数成 **68**（多了 4 处源码里的 'data-app-row="'），
//     宣讲会 94 张被数成 **104**（多了 CSS 里 9 处 .talk-card 和源码里 1 处 data-talk-card），
//     看板 3 个分区被数成 **4**（多了源码里那行 '<section class="board-sec">'）。
//   所以下面统一用 rendered() 剥一遍再数。源码只用在"规则护栏"上（核对页面的排除清单这类常量）。
//
// 依赖数据的项：
//   「进行中」那一块 = 库里所有 result=进行中、阶段不在 PROGRESS_EXCLUDE 里的进展。
//   一条都没有时这块按设计整块隐藏，那 3 项没有东西可渲染 —— 会打 [SKIP]，不假装通过。
//   预期值**不是写死的**：脚本自己拉 /api/state，按页面同一套规则（排除清单 / 上限 / 排序）算出来，
//   再和渲染结果逐条对。（2026-09-22 之前这里写死「应该有 AI测评」，那条被标成已完成后连着红了两轮，
//   每次都要人工解释「是数据不是代码」，所以改成对着数据算。）
const { execFileSync } = require("child_process");
const os = require("os");
const path = require("path");

const CHROME = "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe";
const PORT = process.argv[2] || "8766";
const EXPECT_SNAKE = (process.argv[3] || "snake") !== "no-snake";
const BASE = "http://127.0.0.1:" + PORT + "/";

console.log("检查目标：" + BASE + "  时间链=" + (EXPECT_SNAKE ? "应有" : "已移除") + "\n");

// 【快速失败】先探一次活（2026-10-11 加）：端口空着时每次 dumpDom 都要**等连接超时**，
//   5~6 次就是 3~6 分钟 —— 比自检本身慢一个数量级（实测端口活着时单次 dump 只要 1.4 秒）。
//   探不通就直接退出，别让它慢慢超时。
function reachable(url, ms) {
  // ⚠️ 不能用 Atomics.wait 做同步等待：那会**阻塞 Node 的事件循环**，异步的 http.get 永远不会回调，
  //    结果活着的实例也被判成"连不上"（2026-10-11 我踩过）。改成起一个**子进程**同步探测。
  try {
    execFileSync(process.execPath, ["-e",
      "require('http').get(process.argv[1],function(r){r.resume();process.exit(0)})" +
      ".on('error',function(){process.exit(1)})" +
      ".setTimeout(" + ms + ",function(){process.exit(1)})",
      url], { stdio: "ignore", timeout: ms + 2000 });
    return true;
  } catch (err) {
    return false;
  }
}

if (!reachable(BASE + "api/state", 5000)) {
  console.log("[FAIL] 连不上 " + BASE + "api/state —— 程序没在跑（或端口不对）。");
  console.log("       先启动程序再跑本自检；否则每次 dumpDom 都要等连接超时，几分钟都跑不完。");
  process.exit(1);
}

function dumpDom(url) {
  const profile = path.join(
    os.tmpdir(),
    "ddom_" + Date.now() + "_" + Math.random().toString(36).slice(2)
  );
  const args = [
    "--headless=new",
    "--disable-gpu",
    "--enable-unsafe-swiftshader",
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--no-first-run",
    "--log-level=3",
    "--virtual-time-budget=9000",
    "--user-data-dir=" + profile,
    "--dump-dom",
    url,
  ];
  return execFileSync(CHROME, args, { encoding: "utf8", maxBuffer: 80 * 1024 * 1024 });
}

let ok = true, passed = 0, failed = 0, skipped = 0;
function check(label, cond, extra) {
  if (cond) { passed++; } else { failed++; ok = false; }
  console.log((cond ? "[PASS] " : "[FAIL] ") + label + (extra ? "  " + extra : ""));
}
// 数据不满足前提、这检查没法做时用它。**不算通过也不算失败**，但要打印出来（总数里也算一项），
// 免得「没测到」被当成「测过了」。
function skip(label, why) {
  skipped++;
  console.log("[SKIP] " + label + (why ? "  " + why : ""));
}

function unesc(s) {
  return String(s == null ? "" : s)
    .replace(/&lt;/g, "<").replace(/&gt;/g, ">")
    .replace(/&quot;/g, '"').replace(/&#39;/g, "'")
    .replace(/&amp;/g, "&");
}

// 剥掉 <script>/<style>：数「渲染出来的东西」之前一定要先过这一道（见文件头说明）
function rendered(html) {
  return String(html == null ? "" : html)
    .replace(/<script[\s\S]*?<\/script>/gi, "")
    .replace(/<style[\s\S]*?<\/style>/gi, "");
}

function numAsc(x, y) { return Number(x) - Number(y); }

function idSet(ids) { return ids.slice().sort(numAsc).join(","); }

// 差集，用来在失败时打印"哪些对不上"，而不是只说一句不一样
function diffIds(want, got) {
  const w = {}, g = {};
  want.forEach(function (id) { w[id] = 1; });
  got.forEach(function (id) { g[id] = 1; });
  const miss = want.filter(function (id) { return !g[id]; });
  const extra = got.filter(function (id) { return !w[id]; });
  const out = [];
  if (miss.length) out.push("少 " + miss.join("/"));
  if (extra.length) out.push("多 " + extra.join("/"));
  return out.join("；");
}

(async function main() {

  // 0) 先把库里的状态拿下来：首页「进行中」那块要对着它核对（拿不到就在后面报 FAIL）
  let state = null, stateErr = "";
  try {
    const res = await fetch(BASE + "api/state", { signal: AbortSignal.timeout(8000) });
    if (!res.ok) throw new Error("HTTP " + res.status);
    state = await res.json();
  } catch (e) {
    stateErr = (e && e.message) ? e.message : String(e);
  }

  // 1) 首页：左边导航 + 今天该干什么 + 备忘录
  const dash = dumpDom(BASE + "#dash");
  // 只数导航里带 data-view 的按钮（不含底部主题/说明/退出那三个）。
  // 注意：当前选中的那个会多一个 on 类名（class="rail-item on"），所以不能写死 class="rail-item"
  const railViews = dash.match(/class="rail-item[^"]*" data-view="([a-z]+)"/g) || [];
  const viewCount = railViews.length;
  // 2026-10-10：加了「简历」视图，所以正式版从 4 项变 5 项（带时间链的旧版 5 → 6）。
  const wantViews = EXPECT_SNAKE ? 6 : 5;
  check("左侧导航有 " + wantViews + " 个视图入口", viewCount === wantViews,
    "实际=" + viewCount + " [" + railViews.map(function (s) {
      return (s.match(/data-view="([a-z]+)"/) || [])[1];
    }).join(", ") + "]");
  check("首页有「今天该干什么」", dash.indexOf("今天该干什么") >= 0);
  check("首页有宣讲会待办区", dash.indexOf('id="todo-talks"') >= 0);
  check("首页有备忘录输入框", dash.indexOf('id="note-input"') >= 0);
  check("桌面模式下「退出」按钮会出现", dash.indexOf('id="btn-quit" hidden') < 0);
  check("首页有数据总览图表", (dash.match(/class="dash-card/g) || []).length >= 4,
    "图表卡=" + (dash.match(/class="dash-card/g) || []).length);
  check("首页底部有调研/停滞提示", dash.indexOf("没调研") >= 0);

  // 1a) 首页两块的**先后顺序**（2026-09-25 按用户习惯调的：数据总览在上、"今天该干什么"在下）。
  // ⚠️ 这里必须用「只会出现在真正标签里」的完整字符串当锚点。第一版我用的是
  // "dash-title" / "home-title" 这两个词，结果量到的是 **CSS 里的 `.home-title{...}`**
  // —— 它排在 <style> 里，位置比正文早，于是明明换好了顺序却报"不对"。
  // 正是 AGENTS.md 记的那个坑：数渲染结果之前先剥 <script>/<style>，锚点要用渲染出来的属性。
  // dumpDom 拿到的是整页，这里自己剥一遍（verify_desktop.js 的 rendered() 只剥 script，不够）。
  const dashNoStyle = dash.replace(/<style[\s\S]*?<\/style>/g, "");
  const posDashTitle = dashNoStyle.indexOf('class="dash-title">数据总览');
  const posHomeTitle = dashNoStyle.indexOf('class="home-title"');
  check("首页顺序：数据总览 在「今天该干什么」上面",
    posDashTitle >= 0 && posHomeTitle > posDashTitle,
    "数据总览@" + posDashTitle + " · 今天该干什么@" + posHomeTitle);
  // 换位置最容易顺手弄丢元素，所以把两块的锚点都点一遍
  check("换顺序后两块的元素一个不少",
    dash.indexOf('id="dash"') >= 0 && dash.indexOf('id="dash-note"') >= 0
    && dash.indexOf('id="todo-talks"') >= 0 && dash.indexOf('id="note-list"') >= 0
    && dash.indexOf('id="home-foot"') >= 0 && dash.indexOf('id="home-sub"') >= 0);
  // 界面上要能看出是哪个版、什么版本号（2026-09-25 加，同学反馈问题时靠它确认）
  check("数据总览标题下写着版别和版本号",
    /id="dash-note"[^>]*>[^<]*(个人版|正式版)\s*\d/.test(dash),
    unesc((dash.match(/id="dash-note"[^>]*>([^<]*)</) || [])[1] || ""));

  // 1b) 首页「进行中」自动列表（AI测评 / 面试 / Offer 这类有时效性的事）—— 对着数据核对
  function sliceById(html, id) {
    const start = html.indexOf('id="' + id + '"');
    if (start < 0) return "";
    const open = html.indexOf(">", start) + 1;
    const end = html.indexOf('id="note-list"', open);
    return end < 0 ? html.slice(open) : html.slice(open, end);
  }
  const progress = sliceById(dash, "progress-box");
  check("首页有「进行中」区块", progress.length > 0 && dash.indexOf("进行中") >= 0);

  check("能拿到 /api/state 来核对这块（拿不到就无从判断，宁可算失败）", !!state,
    state ? "投递 " + ((state.applications || []).length) + " 条"
          : "拉 " + BASE + "api/state 失败：" + stateErr);

  // 页面里的两条规则要跟这里的一致，否则预期值会算错（改页面时两边一起改）。
  // --dump-dom 会把 <script> 源码一起带出来（AGENTS.md 里记着这个坑）——这里反过来当护栏用。
  const EXCLUDE = ["投递", "简历初筛", "已拒绝", "已放弃"];
  const LIMIT = 5;
  const wantExclude = "var PROGRESS_EXCLUDE = [" + EXCLUDE.map(function (s) {
    return '"' + s + '"';
  }).join(", ") + "];";
  const wantLimit = "var PROGRESS_LIMIT = " + LIMIT + ";";
  const hasRule = dash.indexOf("var PROGRESS_EXCLUDE") >= 0;
  check("页面里的收录规则和自检预期一致（排除清单 / 条数上限）",
    hasRule && dash.indexOf(wantExclude) >= 0 && dash.indexOf(wantLimit) >= 0,
    hasRule ? "排除 " + EXCLUDE.length + " 个阶段 · 上限 " + LIMIT + " 条"
            : "页面里没有 PROGRESS_EXCLUDE（老版本没有这块？）");

  // 按页面同一套规则算「应该长什么样」
  function expectedProgress(st) {
    const out = [];
    ((st && st.applications) || []).forEach(function (a) {
      ((a && a.events) || []).forEach(function (ev) {
        if ((ev.result || "") !== "进行中") return;
        if (EXCLUDE.indexOf(ev.stage || "") >= 0) return;
        const created = String(ev.created_at || "");
        const day = created.slice(0, 10);
        const firstPosition = String(a.position || "").split("\n")[0] || "";
        out.push({
          id: String(a.id),
          stage: ev.stage || "",
          who: String(a.company || "") + (firstPosition ? " · " + firstPosition : ""),
          created: created,
          date: /^\d{4}-\d{2}-\d{2}$/.test(day) ? day : null,
        });
      });
    });
    // 页面按「悬得最久」排（days 大的在前 = 日期早的在前），同一天里更晚登记的在前；
    // 认不出日期的（days=null → 当成 -1）排最后。这里不重算天数，用日期等价地比。
    out.sort(function (x, y) {
      if (x.date === null || y.date === null) {
        if (x.date === y.date) return y.created.localeCompare(x.created);
        return x.date === null ? 1 : -1;
      }
      if (x.date !== y.date) return x.date < y.date ? -1 : 1;
      return y.created.localeCompare(x.created);
    });
    return out;
  }

  // 把渲染出来的条目抠出来
  const lines = [];
  const lineRe = /<div class="prog-line"([^>]*)>([\s\S]*?)<\/div>/g;
  let m;
  while ((m = lineRe.exec(progress))) {
    const attrs = m[1], inner = m[2];
    lines.push({
      id: (attrs.match(/data-id="(\d+)"/) || [])[1] || "",
      clickable: attrs.indexOf('data-act="goto-app"') >= 0,
      stage: unesc((inner.match(/<span class="t">([^<]*)<\/span>/) || [])[1] || ""),
      who: unesc((inner.match(/<span class="n">([^<]*)<\/span>/) || [])[1] || ""),
      ageCls: (inner.match(/<span class="m([^"]*)">/) || [])[1] || "",
      age: unesc((inner.match(/<span class="m[^"]*">([^<]*)<\/span>/) || [])[1] || ""),
    });
  }
  const note = unesc((progress.match(/<span class="sub-note" id="progress-note">([^<]*)<\/span>/) || [])[1] || "");
  const more = unesc((progress.match(/<div class="prog-more">([^<]*)<\/div>/) || [])[1] || "");
  const boxM = dash.match(/<div class="progress-box" id="progress-box"([^>]*)>/);
  const boxHidden = !!boxM && boxM[1].indexOf("hidden") >= 0;

  const expected = state ? expectedProgress(state) : [];
  const N = expected.length;
  const shown = expected.slice(0, LIMIT);
  const wantNote = N ? "共 " + N + " 条 · 等得最久的排最前" : "";
  const wantMore = N > LIMIT ? "还有 " + (N - LIMIT) + " 条，去「投递」看全部" : "";

  check("「进行中」区块的显隐 / 条数 / 文案和库里算出来的一致",
    !!state && boxHidden === (N === 0) && lines.length === Math.min(N, LIMIT)
      && note === wantNote && more === wantMore,
    "库里 " + N + " 条" + (N ? "（该渲染 " + Math.min(N, LIMIT) + " 条）" : "（整块该隐藏）")
      + " · 实际：hidden=" + boxHidden + " 条数=" + lines.length + " 文案=" + JSON.stringify(note));

  const noDataWhy = "库里没有「进行中」的进展（阶段不在排除清单里的），这块按设计整块隐藏";

  if (N) {
    const got = lines.map(function (l) { return l.stage + "｜" + l.who + "｜#" + l.id; });
    const want = shown.map(function (e) { return e.stage + "｜" + e.who + "｜#" + e.id; });
    const same = got.join("\n") === want.join("\n");
    check("列出来的就是库里等得最久的那几条（阶段 / 公司 / 顺序逐条对上）", same,
      same ? "第一条：" + got[0] : "实际：" + got.join("；") + "　期望：" + want.join("；"));
  } else {
    skip("列出来的就是库里等得最久的那几条（阶段 / 公司 / 顺序逐条对上）", noDataWhy);
  }

  if (N) {
    // 天数文案和它的样式类必须自洽：今天/昨天 → 无类名；已 N 天 → N>=7 是 stale、N>=3 是 aging
    const bad = lines.filter(function (l) {
      let days = null;
      if (l.age === "今天登记") days = 0;
      else if (l.age === "昨天登记") days = 1;
      else if (/^已 \d+ 天$/.test(l.age)) days = parseInt(l.age.slice(2), 10);
      if (days === null) return true;
      const wantCls = days >= 7 ? " stale" : (days >= 3 ? " aging" : "");
      return l.ageCls !== wantCls;
    });
    check("每条都显示「已悬多久」，文案和样式对得上", bad.length === 0,
      bad.length ? "不对的：" + bad.map(function (l) { return l.stage + "=" + JSON.stringify(l.age) + "（类名" + JSON.stringify(l.ageCls) + "）"; }).join("；")
                 : "例：" + lines[0].stage + " = " + lines[0].age);
  } else {
    skip("每条都显示「已悬多久」，文案和样式对得上", noDataWhy);
  }

  if (N) {
    const dead = lines.filter(function (l) { return !l.clickable; });
    check("每条都能点（跳到投递列表并展开那一行）", dead.length === 0,
      dead.length ? "点不动的：" + dead.map(function (l) { return l.stage; }).join("、")
                  : "共 " + lines.length + " 条都带 data-act=goto-app");
  } else {
    skip("每条都能点（跳到投递列表并展开那一行）", noDataWhy);
  }

  // 2) 投递列表：一条不落 + 同公司合并（都对着 /api/state 算）
  const list = dumpDom(BASE + "#list");
  const listDom = rendered(list);
  const apps = state ? (state.applications || []) : [];
  const gotRows = (listDom.match(/data-app-row="(\d+)"/g) || []).map(function (s) {
    return s.replace(/\D/g, "");
  });
  const wantRows = apps.map(function (a) { return String(a.id); });
  const rowsSame = gotRows.length === wantRows.length && idSet(gotRows) === idSet(wantRows);
  check("投递列表把库里的投递一条不落地渲染出来了（逐条对 id）", !!state && rowsSame,
    "库里 " + wantRows.length + " 条 · 渲染 " + gotRows.length + " 条"
      + (rowsSame ? "" : "　对不上的：" + diffIds(wantRows, gotRows)));
  const gotCards = (listDom.match(/class="card company-card"/g) || []).length;
  const companies = {};
  apps.forEach(function (a) { companies[(a.company || "未命名公司").trim()] = 1; });
  const wantCards = Object.keys(companies).length;
  check("同公司合并成一张卡（卡片数 = 库里不同公司数）", !!state && gotCards === wantCards,
    "库里 " + wantCards + " 家 · 渲染 " + gotCards + " 张");
  check("投递页有搜索框", listDom.indexOf('id="search"') >= 0);

  // 3) 看板：分区按库里各状态的条数来（没有数据的分区本来就不该出现）
  const board = dumpDom(BASE + "#board");
  const boardDom = rendered(board);
  const BUCKETS = [
    { key: "offer",  cls: "offer",  label: "Offer" },
    { key: "active", cls: "active", label: "投递进程中" },
    { key: "reject", cls: "reject", label: "已拒绝" },
    { key: "drop",   cls: "drop",   label: "已放弃 / 未登记" },
  ];
  function bucketOf(s) {
    if (s === "Offer") return "offer";
    if (s === "已拒绝") return "reject";
    if (s === "已放弃" || s === "未登记") return "drop";
    return "active";
  }
  const wantBuckets = { offer: [], active: [], reject: [], drop: [] };
  apps.forEach(function (a) { wantBuckets[bucketOf(a.derived_status)].push(a); });
  const secs = [];
  const secRe = /<section class="board-sec">([\s\S]*?)<\/section>/g;
  let sm;
  while ((sm = secRe.exec(boardDom))) {
    const inner = sm[1];
    secs.push({
      cls: (inner.match(/class="board-sec-head (\w+)"/) || [])[1] || "",
      label: unesc((inner.match(/<h3 class="board-sec-head \w+"><span class="dot"><\/span>([^<]*)<span class="cnt">/) || [])[1] || ""),
      cnt: (inner.match(/class="cnt">(\d+) 条<\/span>/) || [])[1] || "",
      cards: (inner.match(/data-act="open-card"/g) || []).length,
      ids: (inner.match(/data-id="(\d+)"/g) || []).map(function (s) { return s.replace(/\D/g, ""); }),
    });
  }
  const wantSecs = BUCKETS.filter(function (b) { return wantBuckets[b.key].length; });
  const secsOk = !!state && secs.length === wantSecs.length && secs.every(function (sec, i) {
    const want = wantBuckets[wantSecs[i].key];
    return sec.cls === wantSecs[i].cls && sec.label === wantSecs[i].label
      && sec.cnt === String(want.length) && sec.cards === want.length
      && idSet(sec.ids) === idSet(want.map(function (a) { return String(a.id); }));
  });
  // 失败时要说清"差在哪"，别只甩一句"不一样"（2026-09-22 故意改坏时踩过：分区标题被改了，
  // 但打印出来的数字一模一样，看日志的人根本不知道为什么红）
  function whySecs() {
    const out = [];
    if (secs.length !== wantSecs.length) out.push("分区数 " + secs.length + "≠" + wantSecs.length);
    wantSecs.forEach(function (b, i) {
      const sec = secs[i], want = wantBuckets[b.key];
      if (!sec) { out.push("少了 " + b.cls + " 分区"); return; }
      if (sec.cls !== b.cls) out.push("第 " + (i + 1) + " 个分区是 " + sec.cls + "，该是 " + b.cls);
      if (sec.label !== b.label) out.push(sec.cls + " 标题「" + sec.label + "」该是「" + b.label + "」");
      if (sec.cnt !== String(want.length)) out.push(sec.cls + " 写着 " + sec.cnt + " 条，该是 " + want.length + " 条");
      const wantIds = want.map(function (a) { return String(a.id); });
      if (idSet(sec.ids) !== idSet(wantIds)) out.push(sec.cls + " 的卡片 id：" + diffIds(wantIds, sec.ids));
    });
    return out.join("；");
  }
  const secDesc = secs.map(function (s) { return s.cls + "「" + s.label + "」=" + s.cnt + "条/" + s.cards + "张"; }).join(" · ") || "一个都没有";
  check("看板分区和条数对着库里各状态来（没数据的分区不出现）", secsOk,
    "库里 " + BUCKETS.map(function (b) { return b.label + "=" + wantBuckets[b.key].length; }).join(" · ")
      + " → 该有 " + wantSecs.length + " 个分区；实际 " + secDesc
      + (secsOk ? "" : "　不一致：" + whySecs()));
  const miniCards = (boardDom.match(/<div class="mini-card[^"]*" data-act="open-card" data-id="(\d+)">/g) || [])
    .map(function (s) { return (s.match(/data-id="(\d+)"/) || [])[1]; });
  check("看板卡片数 = 库里投递数，而且每张都能点开", !!state && miniCards.length === apps.length,
    "库里 " + apps.length + " 条 · 渲染 " + miniCards.length + " 张"
      + (idSet(miniCards) === idSet(wantRows) ? "（id 都对得上）" : "　对不上的：" + diffIds(wantRows, miniCards)));

  // 4) 时间链：2.0 有，v3 已移除。
  //    两种情况都要访问 #snake 这个深链接——v3 那边要确认它不会白屏。
  const snake = dumpDom(BASE + "#snake");
  if (EXPECT_SNAKE) {
    check("时间链有节点", (snake.match(/snake-item/g) || []).length >= 10,
      "节点=" + (snake.match(/snake-item/g) || []).length);
  } else {
    check("时间链已移除：导航里没有入口", snake.indexOf('data-view="snake"') < 0);
    check("导航改为 " + wantViews + " 项后其余入口仍在",
      snake.indexOf('data-view="dash"') >= 0 && snake.indexOf('data-view="talks"') >= 0);
    // 上次停在时间链的话，localStorage 里存的 view 就是 snake，这里要确认它会回退到首页
    check("直接访问 #snake 不白屏（回退到首页）", snake.indexOf("今天该干什么") >= 0);
  }

  // 5) 宣讲会：卡片数 / 按天分组 / 过期归档，全部对着 /api/state 算
  const talks = dumpDom(BASE + "#talks");
  const talksDom = rendered(talks);
  const ARCHIVED = ["已投递", "已面试", "已过期", "已取消"];
  // 归档状态清单写在页面里，自检这边也用它算预期——页面改了会漂，所以顺便核对一遍（同 PROGRESS_EXCLUDE）
  const wantArchivedRule = "var ARCHIVED_TALK_STATUSES = [" + ARCHIVED.map(function (s) {
    return '"' + s + '"';
  }).join(", ") + "];";
  check("页面里的归档状态清单和自检预期一致", talks.indexOf(wantArchivedRule) >= 0,
    "归档状态：" + ARCHIVED.join(" / "));

  // 「投递类别」的归类规则散在两处：页面那份（只用来显示提示语）和 core/talks.py 那份
  // （**真正写库用的是它**）。两处跑偏的后果很隐蔽：界面提示"归为私企"、库里写的是"央国企"。
  // 页面源文件只在 --dump-dom 里能拿到（index.html 是静态文件，直接读也行）——这里读页面文件本身，
  // 拿两边的关键词表逐字对，比"看一眼觉得一样"可靠。
  try {
    const fs = require("fs");
    const pageSrc = fs.readFileSync(
      path.join(__dirname, "..", "app", "web", "index.html"), "utf8");
    const coreSrc = fs.readFileSync(
      path.join(__dirname, "..", "app", "core", "talks.py"), "utf8");
    // 从 core 那份里抠关键词：UNIT_NATURE_RULES 是 ((类别, (词...)), ...)，
    // 类别名**不带引号**、关键词带引号。所以按行取「第二个括号里」的内容，
    // 别整块 match 引号 —— 那样会把类别名（如果有引号）也算进去（第一次就这么错了：
    // core 抠到 25 个、页面 22 个，差的正好是三个类别名）。
    const coreBlock = coreSrc.split("UNIT_NATURE_RULES = (")[1] || "";
    const coreWords = [];
    coreBlock.split("\n\n")[0].split("\n").forEach(function (line) {
      const inner = line.match(/\(\s*"[^"]*"\s*,\s*\(([^)]*)\)\s*\)/);
      if (!inner) return;
      (inner[1].match(/"([^"]+)"/g) || []).forEach(function (s) {
        coreWords.push(s.slice(1, -1));
      });
    });
    // 从页面那份里抠（UNIT_NATURE_RULES 是个 [[label,[words]],...] 的数组）
    const pageBlock = pageSrc.split("var UNIT_NATURE_RULES = [")[1] || "";
    const pageWords = (pageBlock.split("\n  ];")[0].match(/"([^"]+)"/g) || [])
      .map(function (s) { return s.slice(1, -1); })
      // 页面那份第一个词是类别名（央国企/外企/私企），core 那份的类别名不在引号里，
      // 所以把页面这边的类别名剔除，只留关键词。
      .filter(function (w) { return ["央国企", "外企", "私企"].indexOf(w) === -1; });
    const missing = coreWords.filter(function (w) { return pageWords.indexOf(w) < 0; });
    const extra = pageWords.filter(function (w) { return coreWords.indexOf(w) < 0; });
    check("单位性质归类规则：页面和 core/talks.py 逐字一致",
      coreWords.length > 0 && pageWords.length > 0 && !missing.length && !extra.length,
      coreWords.length ? ("core " + coreWords.length + " 个词 · 页面 " + pageWords.length + " 个词"
        + (missing.length ? " · 页面缺 " + missing.join("/") : "")
        + (extra.length ? " · 页面多 " + extra.join("/") : ""))
        : "没从 core/talks.py 抠到 UNIT_NATURE_RULES（它改名了？）");
    // 「不是企业性质」的类别名单（图表要排除它）也要两边一致
    const coreNoCat = (coreSrc.split("NOT_A_COMPANY_CATEGORY = (")[1] || "").split(")")[0];
    const pageNoCat = (pageSrc.split("var NOT_A_COMPANY_CATEGORY = [")[1] || "").split("]")[0];
    const coreNoWords = (coreNoCat.match(/"([^"]+)"/g) || []).map(function (s) { return s.slice(1, -1); });
    const pageNoWords = (pageNoCat.match(/"([^"]+)"/g) || []).map(function (s) { return s.slice(1, -1); });
    check("「非企业性质类别」名单：页面和 core/talks.py 一致",
      coreNoWords.length > 0 && coreNoWords.join("|") === pageNoWords.join("|"),
      "core [" + coreNoWords.join("/") + "] · 页面 [" + pageNoWords.join("/") + "]");
    // 「已投递宣讲」筛选按键要真的渲染出来（2026-09-25 加，用户要求，
    // 挨着"对口"那几个按键）。搜渲染出来的属性，别搜中文提示文字
    // ——源码里也写着那四个字，搜文字永远命中（AGENTS.md 记过这个坑）。
    const appliedChip = talksDom.indexOf('data-fit="applied"') >= 0;
    check("宣讲会页有「已投递宣讲」筛选按键（挨着对口按键）", appliedChip,
      appliedChip ? 'data-fit="applied"' : "没渲染出 data-fit=\"applied\"");
  } catch (err) {
    check("单位性质归类规则：页面和 core/talks.py 逐字一致", false, "读文件失败：" + err.message);
  }
  const allTalks = state ? (state.talks || []) : [];
  const now = new Date();
  const localDay = [now.getFullYear(), String(now.getMonth()+1).padStart(2,"0"), String(now.getDate()).padStart(2,"0")].join("-");
  const archivedTalks = allTalks.filter(function(t){
    const date = (t.talk_date || "").slice(0,10);
    return ARCHIVED.indexOf(t.status || "待参加") >= 0
      || (/^\d{4}-\d{2}-\d{2}$/.test(date) && date < localDay);
  });
  const activeTalks = allTalks.length - archivedTalks.length;
  const talkCards = (talksDom.match(/class="talk-card/g) || []).length;
  check("宣讲会卡片数 = 库里的宣讲会数", !!state && talkCards === allTalks.length,
    "库里 " + allTalks.length + " 场 · 渲染 " + talkCards + " 张");
  const dayBlocks = (talksDom.match(/class="talk-day"/g) || []).length;
  check("永远按天分组展示（没有宣讲会时给空状态）",
    !!state && (allTalks.length ? dayBlocks > 0 && talksDom.indexOf("talk-day") >= 0
                                : talksDom.indexOf("还没有宣讲会记录") >= 0),
    allTalks.length ? "天数分组块=" + dayBlocks : "库里 0 场 → 应显示空状态");
  const archM = talksDom.match(/<div class="talk-archive[^"]*">[\s\S]*?（(\d+) 场，点击展开）/);
  const headBar = unesc((talksDom.match(/class="talk-headbar-count">([^<]*)</) || [])[1] || "");
  const wantHead = "进行中 " + activeTalks + " 场"
    + (archivedTalks.length ? " · 已归档 " + archivedTalks.length + " 场" : "") + " · 按天展示";
  check("过期/取消的收进折叠区（有归档才出现，场次和库一致）",
    !!state && headBar === wantHead
      && (archivedTalks.length ? (!!archM && Number(archM[1]) === archivedTalks.length)
                               : talksDom.indexOf("talk-archive") < 0),
    "库里 进行中 " + activeTalks + " 场 · 已归档 " + archivedTalks.length + " 场"
      + "；标题栏写的是「" + headBar + "」"
      + (archivedTalks.length ? "，折叠区写的是 " + (archM ? archM[1] + " 场" : "（没有折叠区）") : ""));

  console.log("\n共 " + (passed + failed + skipped) + " 项：通过 " + passed + " · 失败 " + failed
    + (skipped ? " · 跳过 " + skipped : ""));
  console.log("结果：" + (ok ? "全部通过" : "存在失败项"));
  process.exit(ok ? 0 : 1);

})();
