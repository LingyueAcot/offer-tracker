// 自绘外壳自检：只在「无边框外壳模式」下该成立的东西。
//
// 用法：node work\verify_shell.js [端口]     端口默认 8768（v3）
//
// 为什么单独一个脚本：普通浏览器打开时（verify_desktop.js 那种）
// 页面**不该**有标题栏；只有启动器加了 ?shell=borderless 参数时才有。
// 两件事分开验，免得互相干扰。
const { execFileSync } = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");

const CHROME = "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe";
const PORT = process.argv[2] || "8768";
const BASE = "http://127.0.0.1:" + PORT + "/?shell=borderless";

// 【快速失败】先探活（2026-10-11 加，与 verify_desktop.js 同一套）：端口空着时每次取页面都要等
//   连接超时，整道门能拖到几分钟。实测端口活着时单次只要约 1.4 秒。
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
// ⚠️ 探活要用**干净的** base：BASE 已经带了 /?shell=borderless，直接拼 api/state 会拼错
const PROBE = "http://127.0.0.1:" + PORT + "/api/state";
if (!reachable(PROBE, 5000)) {
  console.log("[FAIL] 连不上 " + PROBE + " —— 程序没在跑（或端口不对）；先启动程序再跑。");
  process.exit(1);
}

// 取一个 JSON 接口（同样是起子进程，别阻塞事件循环）。拿不到就抛，调用方自己决定怎么算。
function fetchJson(url) {
  const out = execFileSync(process.execPath, ["-e",
    "require('http').get(process.argv[1],function(r){var b='';r.on('data',function(d){b+=d});" +
    "r.on('end',function(){process.stdout.write(b)})}).on('error',function(){process.exit(1)})",
    url], { encoding: "utf8", timeout: 8000, maxBuffer: 32 * 1024 * 1024 });
  return JSON.parse(out);
}
// v3 程序目录（静态检查要用：直接读源码文件）
const V3_DIR = path.join(__dirname, "..", "app");

let chromeWarned = false;
function dumpDom(url) {
  const profile = path.join(
    os.tmpdir(),
    "sdom_" + Date.now() + "_" + Math.random().toString(36).slice(2)
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
  // ⚠️ 必须给 timeout：Chrome 偶尔不返回（实测卡过 7 分钟无输出），
  //    而 execFileSync 默认**永远等**——整道门就那么挂着，看日志也不知道卡在哪（2026-10-11 修）。
  try {
    return execFileSync(CHROME, args, {
      encoding: "utf8",
      maxBuffer: 80 * 1024 * 1024,
      timeout: 60000,
    });
  } catch (err) {
    if (!chromeWarned) {
      chromeWarned = true;
      console.log("[WARN] 取页面失败或超时（" + (err.code || err.message) + "）：" +
                  "后面依赖 DOM 的检查会失败，先确认 Chrome 能不能无界面打开这个地址。");
    }
    return "";
  }
}

let ok = true;
function check(label, cond, extra) {
  if (!cond) ok = false;
  console.log((cond ? "[PASS] " : "[FAIL] ") + label + (extra ? "  " + extra : ""));
}

// ---------- 静态检查：页面发出的窗口动作名，启动器必须都认 ----------
// 这一条是被真实 bug 逼出来的：页面发 close_window、启动器只认 close，
// 结果点 X 毫无反应，而且只表现为日志里一句「收到不认识的窗口命令」。
// 两边的名字是跨语言的字符串约定，没有编译期检查，只能靠这一条兜着。
(function checkActionNames() {
  const v3 = path.join(__dirname, "..", "app");
  let htmlSrc = "";
  let pySrc = "";
  try {
    htmlSrc = fs.readFileSync(path.join(v3, "web", "index.html"), "utf8");
    pySrc = fs.readFileSync(path.join(v3, "launcher.py"), "utf8");
  } catch (err) {
    check("能读到页面和启动器源码", false, String(err));
    return;
  }

  const sent = new Set();
  let m;
  const reSend = /(?:shSend|shellCall|shellCall2)\(\s*"([a-z_]+)"/g;
  while ((m = reSend.exec(htmlSrc))) sent.add(m[1]);

  const accepted = new Set();
  const reEq = /action == "([a-z_]+)"/g;
  while ((m = reEq.exec(pySrc))) accepted.add(m[1]);
  const reIn = /action in \(([^)]+)\)/g;
  while ((m = reIn.exec(pySrc))) {
    (m[1].match(/"([a-z_]+)"/g) || []).forEach(function (s) {
      accepted.add(s.replace(/"/g, ""));
    });
  }

  const missing = Array.from(sent).filter(function (a) { return !accepted.has(a); });
  check("页面发出的动作名启动器都认（共 " + sent.size + " 个）",
    missing.length === 0,
    missing.length ? "启动器不认识：" + missing.join("、")
                   : Array.from(sent).sort().join("、"));
})();

console.log("");
console.log("检查目标：" + BASE + "#dash\n");

const dom = dumpDom(BASE + "#dash");
check("页面渲染出来了（DOM 非空）", dom.length > 10000, "DOM=" + dom.length + " 字符");
check("body 上加了 shell-borderless 类", dom.indexOf('class="shell-borderless"') >= 0);
check("自绘标题栏出现", dom.indexOf('id="shtitle"') >= 0);
check("三个窗口按钮都在",
  dom.indexOf('id="sh-min"') >= 0 &&
  dom.indexOf('id="sh-max"') >= 0 &&
  dom.indexOf('id="sh-close"') >= 0);

// 图标用内联 SVG 画的，不用 ─ □ ✕ 这类字符：
// 那三个字符来自不同字库，大小和粗细对不齐（用户反馈过「最大化图标大小不对」）。
const winAt = dom.indexOf('class="shtitle-win"');
const winChunk = winAt >= 0 ? dom.slice(winAt, winAt + 3000) : "";
check("窗口按钮用 SVG 图标",
  (winChunk.match(/<svg/g) || []).length === 3,
  "svg=" + (winChunk.match(/<svg/g) || []).length);
check("没有残留的字符图标（─ □ ✕）",
  !/─|□|✕/.test(winChunk));

// 下面三项要确认「元素真的被搬进标题栏了」，不只看它还在不在页面上。
const dragAt = dom.indexOf('id="shtitle-drag"');
const toolsAt = dom.indexOf('id="shtitle-tools"');
const searchAt = dom.indexOf('id="search"');
const exportAt = dom.indexOf('id="btn-export"');
const newAt = dom.indexOf('id="btn-new"');

check("搜索框已搬进标题栏",
  dragAt >= 0 && searchAt > dragAt && searchAt - dragAt < 400,
  "标题栏@" + dragAt + " 搜索框@" + searchAt);
check("导出按钮已搬进标题栏",
  toolsAt >= 0 && exportAt > toolsAt && exportAt - toolsAt < 600,
  "工具区@" + toolsAt + " 导出@" + exportAt);
check("新增投递已搬进标题栏",
  toolsAt >= 0 && newAt > toolsAt && newAt - toolsAt < 600,
  "工具区@" + toolsAt + " 新增@" + newAt);
check("原来的整条顶栏已从页面移除", dom.indexOf('class="topbar"') < 0);

// 外壳模式下业务功能一个都不能少（2026-10-10：加「简历」视图后 4 → 5 项）
check("左侧导航仍是 5 项",
  (dom.match(/class="rail-item[^"]*" data-view="/g) || []).length === 5,
  "实际=" + (dom.match(/class="rail-item[^"]*" data-view="/g) || []).length);
check("首页内容照常渲染", dom.indexOf("今天该干什么") >= 0);

// 投递列表：**对着数据算**，不许写死数量（2026-10-11 改）。
// 原来写的是 `>= 40` —— 于是空库（便携包首启、CI 里的干净库）必红，
// 而这条门的意义是"外壳模式下业务功能没少"，不是"你库里必须有 40 条投递"。
// 规矩见 AGENTS.md：「能对着数据算的就别写死」「数据不满足前提时用 [SKIP]」。
// 数"渲染出来几个"之前，先把 <script>/<style> 剥掉。
// ⚠️ 这是项目里记录过的坑（AGENTS.md 第二节）：`--dump-dom` 会把页面**源码**一起带出来，
//    而 `data-app-row=` 这种字样在 JS 源码里就写着 —— 直接数会虚高（曾经投递 64 数成 68）。
function rendered(html) {
  return String(html)
    .replace(/<script[\s\S]*?<\/script>/gi, "")
    .replace(/<style[\s\S]*?<\/style>/gi, "");
}

(function checkAppList() {
  let expected = null;
  try {
    expected = fetchJson("http://127.0.0.1:" + PORT + "/api/state").applications.length;
  } catch (err) {
    expected = null;
  }
  const rendered_rows = (rendered(dumpDom(BASE + "#list")).match(/data-app-row=/g) || []).length;
  if (expected === null) {
    check("[SKIP] 投递列表照常渲染（拿不到 /api/state，无法对账）", true, "渲染 " + rendered_rows + " 行");
  } else if (expected === 0) {
    check("[SKIP] 投递列表照常渲染（库里 0 条投递，没东西可渲染）", true, "渲染 " + rendered_rows + " 行");
  } else {
    // 宽松上限：列表默认只渲染前 N 条，所以"渲染数 ≥1 且 ≤ 库里条数"
    check("投递列表照常渲染（对账：库里 " + expected + " 条）",
      rendered_rows >= 1 && rendered_rows <= expected, "实际 " + rendered_rows + " 行");
  }
})();

// 注意：不带 shport 时页面应该给出「外壳控制端口缺失」的可见提示
//（正常由启动器在地址里带上 shport，这里是故意测缺失的情况）。
//
// 判据必须用 id="sh-warn" 这种**渲染出来的属性**，不能搜提示文字本身 ——
// 页面脚本的源码里就写着那句中文，dump-dom 会把 <script> 的内容也带出来，
// 搜文字会永远命中，白测一场（这里踩过一次）。
check("没有控制端口时给出可见提示", dom.indexOf('id="sh-warn"') >= 0);

// 带上 shport（启动器正常会带）之后，提示应该消失，标题栏照常装配
const withPort = dumpDom(BASE + "&shport=12345#dash");
check("带 shport 时不再提示缺端口", withPort.indexOf('id="sh-warn"') < 0);
check("带 shport 时标题栏照常装配", withPort.indexOf('id="shtitle"') >= 0 &&
  withPort.indexOf('id="sh-min"') >= 0);
const wpAt = withPort.indexOf('class="shtitle-win"');
const wpChunk = wpAt >= 0 ? withPort.slice(wpAt, wpAt + 3000) : "";
check("带 shport 时窗口按钮仍是 SVG（3 个）",
  (wpChunk.match(/<svg/g) || []).length === 3,
  "svg=" + (wpChunk.match(/<svg/g) || []).length);

// 拖边缘改大小的把手：去掉非客户区之后，Windows 不再管边缘，改由页面做
const grips = withPort.match(/class="shgrip shgrip-[a-z]{1,2}" data-grip="[a-z]{1,2}"/g) || [];
check("拖边缘的把手共 8 个（4 边 + 4 角）", grips.length === 8, "实际=" + grips.length);
check("把手在普通浏览器下不显示（CSS 里默认 display:none）",
  fs.readFileSync(path.join(V3_DIR, "web", "index.html"), "utf8")
    .indexOf(".shgrip{display:none}") >= 0);

console.log("\n结果：" + (ok ? "全部通过" : "存在失败项"));
process.exit(ok ? 0 : 1);
