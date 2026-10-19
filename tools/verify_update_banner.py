# -*- coding: utf-8 -*-
r"""验收门：「更新失败」那条提示**必须能被用户关掉**，而且**自动检查失败不许弹**。

为什么单独一门（2026-10-19 用户实测）：检查更新失败时，那条提示只有「重试」，
「忽略」在没有版本号时是隐藏的 → **用户被困住**（原话「无法关闭」）。而且后台自动检查
一失败就弹，等于每次启动都被打扰。

做法照 `verify_actions.py` 的路子：把页面拷一份、在 `</body>` 前注入探针 JS，
用无界面 Chrome 跑一遍再读 DOM。**不用启动后端**——页面在 `file://` 下读状态本来就会失败，
正好就是"失败提示"这个场景。

用法：python tools\verify_update_banner.py
"""
from __future__ import print_function

import io
import os
import shutil
import subprocess
import sys
import tempfile

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PAGE = os.path.join(ROOT, "app", "web", "index.html")
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"

ok = True
passed = failed = 0


def check(label, cond, extra=""):
    global ok, passed, failed
    if cond:
        passed += 1
    else:
        failed += 1
        ok = False
    print(("  [PASS] " if cond else "  [FAIL] ") + label + ("  " + extra if extra else ""))


DRIVER = r"""
<script>
(function(){
  var errors = [];
  window.addEventListener("error", function(e){ errors.push("error:" + (e.message || e.type)); }, true);
  window.addEventListener("unhandledrejection", function(e){
    errors.push("rejection:" + (e.reason && e.reason.message ? e.reason.message : e.reason));
  }, true);
  function done(lines){
    var box = document.createElement('div');
    box.id = 'rz-update-probe';
    box.textContent = lines.concat(['JS异常=' + (errors.length ? errors.join(' ; ') : '无')]).join(' | ');
    document.body.appendChild(box);
  }
  function $(id){ return document.getElementById(id); }
  // 页面在 file:// 下读 /api/state 会失败，所以"自动检查"这条路走不到。
  // 改为**主动点一次「检查更新」**：它会去请求后端 → 失败 → 正是要测的那条提示。
  setTimeout(function(){
    var btn = $('btn-update');
    if(btn){ btn.click(); }
    var tries = 0;
    var t = setInterval(function(){
      tries++;
      var box = $('update-box');
      var shown = box && !box.hidden;
      if(!shown && tries < 60){ return; }
      clearInterval(t);
      var dis = $('update-dismiss'), retry = $('update-retry');
      var shownFirst = box ? !box.hidden : null;
      var dismissVisible = dis ? !dis.hidden : false;
      var retryVisible = retry ? !retry.hidden : false;
      var title = ($('update-title') || {}).textContent || "";
      if(dis){ dis.click(); }
      var hiddenAfter = box ? box.hidden : null;
      // 再点一次「检查更新」应该复位"已收起"
      if(btn){ btn.click(); }
      setTimeout(function(){
        done(['首次显示=' + shownFirst, '关闭按钮可见=' + dismissVisible,
              '重试可见=' + retryVisible, '标题=' + title, '点关闭后隐藏=' + hiddenAfter,
              '检查前复位=' + (box ? !box.hidden : null)]);
      }, 900);
    }, 100);
  }, 1500);
})();
</script>
"""


def main():
    if not os.path.isfile(PAGE):
        print("找不到页面：%s" % PAGE)
        return 1
    if not os.path.isfile(CHROME):
        print("找不到 Chrome：%s（这条门需要这台电脑 Chrome）" % CHROME)
        return 1
    scratch = tempfile.mkdtemp(prefix="rz-update-banner-")
    try:
        html = io.open(PAGE, encoding="utf-8").read()
        # ⚠️ 页面从 file:// 打开时 `<script src="lib.js">` 加载不到（相对路径会失败），
        #    页面初始化就会抛 `esc is not defined` —— 第一次跑这条门正是这么全红的。
        #    所以把 lib.js **内联**进去（这正是本门要测的：只测更新提示的逻辑，不依赖静态文件服务）。
        lib = os.path.join(os.path.dirname(PAGE), "lib.js")
        if not os.path.isfile(lib):
            print("找不到 lib.js：%s" % lib)
            return 1
        html = html.replace('<script src="lib.js"></script>',
                            "<script>\n"
                            # ⚠️ lib.js 里有**字面量 `</script`**（注释里写的），内联时必须转义，
                            #    否则 HTML 解析器会在那里提前结束脚本 → 后面的 esc 全没定义
                            #    （这条门第一次跑就是这么全红的）。
                            + io.open(lib, encoding="utf-8").read().replace("</script", "<\\/script")
                            + "\n</script>", 1)
        page = os.path.join(scratch, "page.html")
        io.open(page, "w", encoding="utf-8", newline="").write(html.replace("</body>", DRIVER, 1))
        profile = os.path.join(scratch, "chrome-profile")
        proc = subprocess.run(
            [CHROME, "--headless=new", "--disable-gpu", "--enable-unsafe-swiftshader",
             "--no-sandbox", "--disable-dev-shm-usage", "--no-first-run", "--log-level=3",
             "--virtual-time-budget=20000", "--user-data-dir=" + profile, "--dump-dom",
             "file:///" + page.replace("\\", "/")],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180)
        dom = proc.stdout or ""
        # ⚠️ 不能按 "rz-update-probe" 直接 split：那个字符串也出现在**注入的脚本源码**里，
        #    会切到源码本身（第一次就是这么全红的）。只认渲染出来的那个 div。
        line = ""
        marker = 'id="rz-update-probe"'
        if marker in dom:
            tail = dom.split(marker, 1)[1]
            if ">" in tail:
                line = tail.split(">", 1)[1].split("</div>", 1)[0]
        if not line:
            check("探针跑出结果（页面脚本要能执行）", False, "DOM 里没找到探针，长度 %d" % len(dom))
        else:
            def val(key):
                for seg in line.replace("<", " ").split("|"):
                    seg = seg.strip()
                    if seg.startswith(key + "="):
                        return seg.split("=", 1)[1].strip()
                return ""
            check("失败提示真的出现了（前提成立）", val("首次显示") == "true", line[:120])
            check("**有「关闭」按钮且可见**（用户能自己脱困）", val("关闭按钮可见") == "true")
            check("「重试」也在（允许重试）", val("重试可见") == "true")
            check("标题是「检查失败」（说清是检查而不是更新失败）", "检查失败" in val("标题"))
            check("**点「关闭」之后提示真的隐藏**", val("点关闭后隐藏") == "true")
            check("再点「检查更新」时提示会重新允许出现", val("检查前复位") == "true")
            check("页面上没有静默 JS 异常", val("JS异常") == "无", val("JS异常"))
    finally:
        shutil.rmtree(scratch, ignore_errors=True)

    print("\n共 %d 项：通过 %d · 失败 %d" % (passed + failed, passed, failed))
    print("结果：" + ("全部通过" if ok else "存在失败项"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
