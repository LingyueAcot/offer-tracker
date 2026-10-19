# -*- coding: utf-8 -*-
"""
tools/verify_actions.py —— 界面**动作**自检：抓「点了没反应」这类静默 JS 异常

【为什么要有它】
`verify_desktop.js` 看的是"渲染出来没有"，`test_web_lib.js` 跑的是纯函数与页面脚本的语法/
初始化。但**事件处理器里抛的异常它们都抓不到**：页面照样渲染得好好的，点了按钮却什么都不发生
（异常被浏览器吞进 console，用户只觉得"按键失效"）。
2026-10-11 的「基本信息 → 删除」就是这么坏的：注释把 `var infoAdd` 声明吞掉 → 每次点击都在
`if(infoAdd)` 处抛 `ReferenceError` → 删除、添加全废，而所有既有自检全绿。

【它怎么做到】
1. 复制一份 app（**不含** data/logs，和 tools/test_routes.py 同一套配方）→ 全新库；
2. 起一份隔离实例（随机空闲端口）；
3. 通过接口造一份**确定的**测试简历（8 条信息行 + 两个板块，不依赖用户数据）；
4. 往**副本**的 web/index.html 里注入一段驱动脚本（只动副本，真程序文件不碰）；
5. 无界面 Chrome 打开页面：驱动脚本先挂 `window.onerror` / `unhandledrejection`，
   再用 `.click()` **真的点**一串控件（加信息/删信息/置顶/换栏/加删经历/保存），
   每一步记录"状态有没有按预期变化"，最后把结果写进渲染出来的 `<div id="rz-action-probe">`；
6. 抓回 DOM（先剥 `<script>/<style>`，按 AGENTS.md 的规矩），解析并断言：
   **没有 JS 异常** 且 **每个动作都真的生效**；
7. 收尾：杀进程、删副本目录。

用法：
    python tools\\verify_actions.py            # 自己挑端口、自己收尾
    python tools\\verify_actions.py 8793       # 指定端口

退出码 0 = 全部通过；1 = 有失败项。
"""
import html as html_module
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
APP = os.path.join(ROOT, "app")
if not os.path.isfile(os.path.join(APP, "server.py")):
    print("[FAIL] 找不到 app/server.py —— 脚本位置推算不对，拒绝继续")
    sys.exit(1)

CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
PYTHON = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Python", "Python312",
                      "pythonw.exe")
if not os.path.isfile(PYTHON):
    PYTHON = sys.executable

PASSED, FAILED, SKIPPED = [], [], []


def check(label, ok, extra=""):
    line = ("[PASS] " if ok else "[FAIL] ") + label + (("  " + str(extra)) if extra else "")
    print(line, flush=True)
    (PASSED if ok else FAILED).append(label)


def free_port(preferred):
    for port in [preferred] + list(range(8801, 8830)):
        with socket.socket() as probe:
            try:
                probe.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    return preferred


def wait_state(port, seconds=40):
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            with urllib.request.urlopen("http://127.0.0.1:%d/api/state" % port, timeout=3) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception:
            time.sleep(1.5)
    return None


def call(port, method, path, payload=None):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    request = urllib.request.Request("http://127.0.0.1:%d%s" % (port, path), data=data,
                                     method=method,
                                     headers={"Content-Type": "application/json; charset=utf-8"})
    with urllib.request.urlopen(request, timeout=30) as resp:
        body = resp.read().decode("utf-8")
    return json.loads(body) if body.strip() else {}


DRIVER = r"""
<script>
(function(){
  var errors = [];
  window.addEventListener("error", function(e){
    errors.push("error: " + (e.message || e.type));
  }, true);
  window.addEventListener("unhandledrejection", function(e){
    errors.push("rejection: " + (e.reason && e.reason.message ? e.reason.message : e.reason));
  });
  function rows(){ return document.querySelectorAll('#rz-form [data-rz-info-label]').length; }
  // 每个条目都有一个删除按钮，数它最可靠（data-rz-item-text 这种属性并不存在）
  function items(key){ return document.querySelectorAll('#rz-form [data-rz-del="' + key + '"]').length; }
  function finish(lines){
    var box = document.createElement('div');
    box.id = 'rz-action-probe';
    box.textContent = lines.concat(['JS异常=' + (errors.length ? errors.join(' ; ') : '无')]).join(' | ');
    document.body.appendChild(box);
  }
  var tries = 0;
  var timer = setInterval(function(){
    tries++;
    var del = document.querySelector('#rz-form [data-rz-info-del]');
    var addInfo = document.querySelector('#rz-form [data-rz-info-add]');
    var top = document.querySelector('#rz-form [data-rz-move="top"]');
    var side = document.querySelector('#rz-form [data-rz-side]');
    var addItem = document.querySelector('#rz-form [data-rz-add]');
    if(!del && tries < 60){ return; }
    clearInterval(timer);
    var log = [];
    if(!del){ log.push('探针前置失败=没渲染出信息行'); finish(log); return; }

    // ① 删除一条信息行
    var before = rows();
    del.click();
    log.push('删除信息行=' + before + '->' + rows());
    // ② 再加回来
    var addNow = document.querySelector('#rz-form [data-rz-info-add]');
    if(addNow && !addNow.disabled){ addNow.click(); log.push('加信息行=' + rows()); }
    else { log.push('加信息行=跳过(按钮不可用)'); }
    // ③ 板块置顶
    if(top && !top.disabled){
      var key = top.getAttribute('data-rz-key');
      top.click();
      var order = null;
      try { order = (RZ.layout.section_order[rzSectionSide(key)] || []).indexOf(key); } catch(e0){}
      log.push('板块置顶 ' + key + '=' + (order === 0 ? '到第一位' : ('位置' + order)));
    } else { log.push('板块置顶=跳过(按钮不可用)'); }
    // ④ 换栏（把这个板块挪到另一栏）
    var sideNow = document.querySelector('#rz-form [data-rz-side]');
    if(sideNow){
      var k2 = sideNow.getAttribute('data-rz-side');
      var was = sideNow.value;
      sideNow.value = (was === 'left') ? 'right' : 'left';
      sideNow.dispatchEvent(new Event('change', {bubbles:true}));
      var sideAfter = document.querySelector('#rz-form [data-rz-side="' + k2 + '"]');
      log.push('换栏 ' + k2 + '=' + was + '->' + (sideAfter ? sideAfter.value : '?'));
      if(sideAfter){ sideAfter.value = was; sideAfter.dispatchEvent(new Event('change', {bubbles:true})); }
    } else { log.push('换栏=跳过(没有位置下拉)'); }
    // ⑤ 加一条经历、再删掉它
    var firstDel = document.querySelector('#rz-form [data-rz-del]');
    var addOne = firstDel ? document.querySelector('#rz-form [data-rz-add="' + firstDel.getAttribute('data-rz-del') + '"]') : null;
    if(addOne){
      var k3 = addOne.getAttribute('data-rz-add');
      var n1 = items(k3);
      addOne.click();
      log.push('加经历 ' + k3 + '=' + n1 + '->' + items(k3));
      var delOne = document.querySelector('#rz-form [data-rz-del="' + k3 + '"]');
      if(delOne){ delOne.click(); log.push('删经历 ' + k3 + '=' + items(k3)); }
    } else { log.push('加经历=跳过(没有按钮)'); }
    // ⑥ 自定义板块：新增 → 改名 → 软删除 → 恢复
    var newSec = document.querySelector('#rz-form [data-rz-newsec]');
    if(newSec){
      var before2 = document.querySelectorAll('#rz-form [data-rz-title^="cs_"]').length;
      newSec.click();
      var after2 = document.querySelectorAll('#rz-form [data-rz-title^="cs_"]').length;
      log.push('新增板块=' + before2 + '->' + after2);
      var titleInput = document.querySelector('#rz-form [data-rz-title^="cs_"]');
      if(titleInput){
        titleInput.value = '自检板块';
        titleInput.dispatchEvent(new Event('input', {bubbles:true}));
        log.push('改名=已改');
      }
      var delSec = document.querySelector('#rz-form [data-rz-delsec]');
      if(delSec){ delSec.click(); log.push('删板块=已点'); }
      var restoreSec = document.querySelector('#rz-form [data-rz-restoresec]');
      if(restoreSec){ restoreSec.click(); log.push('恢复板块=已点'); }
    } else { log.push('新增板块=跳过(没有按钮)'); }

    // ⑦ 保存一次（走真实接口）
    var save = document.querySelector('#rz-save');
    if(save){ save.click(); log.push('点保存=已点'); }
    setTimeout(function(){ finish(log); }, 1200);
  }, 250);
})();
</script>
</body>
"""


def main():
    port = free_port(int(sys.argv[1]) if len(sys.argv) > 1 else 8793)
    scratch = os.path.join(tempfile.gettempdir(), "dsh_actions_%d" % port)
    if os.path.isdir(scratch):
        shutil.rmtree(scratch, ignore_errors=True)
    os.makedirs(scratch)
    copy = os.path.join(scratch, "app")
    shutil.copytree(APP, copy, ignore=shutil.ignore_patterns(
        "data", "logs", "browser_profile", "webview_profile", "__pycache__"))
    with open(os.path.join(copy, "config.json"), "w", encoding="utf-8") as handle:
        handle.write(json.dumps({"port": port, "desktop": True}, ensure_ascii=False))
    print("隔离副本：%s（真库、真程序文件都没碰）" % copy, flush=True)

    # ⚠️ 2026-10-11：显式指定数据目录。副本里没有 data\，不指定的话 app/paths.py 会把它当"新装"，
    #    于是这份隔离副本会去写**用户真实的数据目录**（自检碰真数据是红线）。
    env = dict(os.environ)
    env["RESUME_TRACKER_DATA_DIR"] = os.path.join(scratch, "data")
    env["RESUME_TRACKER_NO_SHELL"] = "1"   # 自检不许真开资源管理器（2026-10-19）
    server = subprocess.Popen([PYTHON, "server.py"], cwd=copy, env=env,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        state = wait_state(port)
        if not state:
            check("隔离实例能起来", False, "等 40 秒没等到 /api/state")
            return 1
        check("隔离实例能起来", True, "投递 %d · 简历 %d" % (len(state.get("applications", [])),
                                                        len(state.get("resumes", []))))

        # 造一份确定的测试简历：8 条信息行 + 两个板块各一条
        info = [{"label": "电话", "value": "13800000000"},
                {"label": "邮箱", "value": "someone@example.com"},
                {"label": "性别", "value": "男"}, {"label": "现所在地", "value": "广州"},
                {"label": "政治面貌", "value": "中共党员"}, {"label": "民族", "value": "汉"},
                {"label": "籍贯", "value": "广东示例市"}, {"label": "最高学历", "value": "硕士研究生"}]
        created = call(port, "POST", "/api/resumes", {
            "title": "自检用简历",
            "content": {"basics": {"name": "自检", "headline": "材料工程硕士 | 2027届"},
                        "info": info,
                        "education": [{"school": "某某大学", "major": "材料工程", "degree": "硕士",
                                       "start": "2024.09", "end": "2027.06", "bullets": ["课程：高分子"]}],
                        "projects": [{"name": "自检项目", "role": "研究生", "start": "2024.09",
                                      "end": "2025.06", "bullets": ["做了些实验"]}],
                        "skills": [{"text": "Origin / Python"}],
                        "awards": [{"text": "国家奖学金（2026）"}]},
            "layout": {"show_photo": False}})
        resume_id = created.get("id") or created.get("resume_id")
        check("能建出自检用简历", bool(resume_id), "id=%s" % resume_id)
        if not resume_id:
            return 1

        # 只改**副本**的页面：注入驱动脚本
        page = os.path.join(copy, "web", "index.html")
        html = open(page, encoding="utf-8").read()
        if "rz-action-probe" not in html:
            open(page, "w", encoding="utf-8", newline="").write(html.replace("</body>", DRIVER, 1))
        check("驱动脚本已注入副本页面", "rz-action-probe" in open(page, encoding="utf-8").read())

        # 无界面 Chrome 抓 DOM（照 verify_desktop.js 的调法：execFileSync/子进程 + 独立 profile）
        profile = os.path.join(scratch, "chrome-profile")
        dom = subprocess.run(
            [CHROME, "--headless=new", "--disable-gpu", "--enable-unsafe-swiftshader",
             "--no-sandbox", "--disable-dev-shm-usage", "--no-first-run", "--log-level=3",
             "--virtual-time-budget=20000", "--user-data-dir=" + profile, "--dump-dom",
             "http://127.0.0.1:%d/#resume" % port],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180).stdout or ""
        stripped = dom
        for tag in ("script", "style"):
            while "<%s" % tag in stripped:
                start = stripped.find("<%s" % tag)
                end = stripped.find("</%s>" % tag, start)
                if end < 0:
                    break
                stripped = stripped[:start] + stripped[end + len(tag) + 3:]
        marker = '<div id="rz-action-probe">'
        if marker not in stripped:
            check("驱动脚本跑出了结果", False, "没找到探针元素（页面没渲染 / 脚本初始化就炸了）")
            return 1
        text = html_module.unescape(
            stripped.split(marker, 1)[1].split("</div>", 1)[0].strip())
        print("探针结果：" + text, flush=True)

        fields = []
        for piece in text.split("|"):
            if "=" in piece:
                fields.append((piece.split("=", 1)[0].strip(), piece.split("=", 1)[1].strip()))

        def field(label, default=""):
            """按前缀取（探针的键有时带板块名，例如 "换栏 education"）"""
            for name, value in fields:
                if name.startswith(label):
                    return value
            return default

        check("动作过程中没有 JS 异常", field("JS异常") == "无", field("JS异常"))
        check("删除信息行真的生效", field("删除信息行").split("->")[0].strip()
              != field("删除信息行").split("->")[-1].strip(), field("删除信息行"))
        add_info = field("加信息行", "0")
        check("加信息行真的生效", add_info.startswith("跳过") or int(add_info.split("->")[-1]) >= 8,
              field("加信息行"))
        check("板块置顶真的生效", "跳过" in field("板块置顶")
              or field("板块置顶").endswith("到第一位"), field("板块置顶"))
        check("换栏真的生效", "跳过" in field("换栏") or "->" in field("换栏"), field("换栏"))
        custom = field("新增板块", "跳过")
        ok_custom = custom.startswith("跳过")
        if not ok_custom and "->" in custom:
            left, right = [part.strip() for part in custom.split("->")[-2:]]
            ok_custom = right.isdigit() and int(right) >= 1
        check("新增自定义板块真的生效", ok_custom, field("新增板块"))
        check("自定义板块能改名", field("改名", "跳过").startswith("跳过") or field("改名") == "已改",
              field("改名"))

        grow = field("加经历", "跳过")
        ok_grow = grow.startswith("跳过")
        if not ok_grow and "->" in grow:
            left, right = [part.strip() for part in grow.split("->")[-2:]]
            ok_grow = right.isdigit() and left.isdigit() and int(right) == int(left) + 1
        check("加经历真的生效", ok_grow, field("加经历"))
    finally:
        try:
            server.terminate()
            server.wait(timeout=10)
        except Exception:
            pass
        shutil.rmtree(scratch, ignore_errors=True)

    print("")
    total = len(PASSED) + len(FAILED) + len(SKIPPED)
    print("共 %d 项：通过 %d · 失败 %d · 跳过 %d" % (total, len(PASSED), len(FAILED), len(SKIPPED)))
    print("结果：" + ("全部通过" if not FAILED else "存在失败项"))
    return 0 if not FAILED else 1


if __name__ == "__main__":
    sys.exit(main())
