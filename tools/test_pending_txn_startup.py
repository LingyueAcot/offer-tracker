# -*- coding: utf-8 -*-
r"""B5 验收：有未完成的升级事务时，程序**不许**带着半新半旧启动。

为什么不用 Astra 的探针脚本：它复制 app\ 时用的是**旧的文件清单**，新加的
`app/services/update_txn.py` 没被拷进去 → 报 ImportError（探针自己的问题）。
这里用**完整拷贝**做一次真验收。

判据（每条都要能失败）：
  1. 预置 phase=apply 的未完成事务 → 真实入口退出码 2，且**没有建库**（= 没走到 init_db）
  2. 预置 phase=backup（还没进替换阶段）的事务 → **允许启动**（会建库）
  3. 没有事务 → 正常启动
  4. 事务文件的 target 与本安装不符 → 拒绝
  5. 事务文件是坏 JSON → 拒绝
  6. 静态检查：窗口入口 `launcher.main()` 里也调了同一个判据（否则窗口照开）

用法：python test_pending_txn_startup.py [--keep]
"""
from __future__ import print_function

import argparse
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(os.path.dirname(HERE), "app")
PY = sys.executable

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


def make_install(root, name="安装"):
    """完整拷贝 app\\（不含用户数据），返回安装根。"""
    pkg = os.path.join(root, name)
    shutil.copytree(APP, os.path.join(pkg, "app"),
                    ignore=shutil.ignore_patterns("data", "logs", "cache",
                                                  "webview_profile", "browser_profile", "__pycache__"))
    return pkg


def write_txn(pkg, **fields):
    where = os.path.join(pkg, "更新数据")
    os.makedirs(where, exist_ok=True)
    data = {"id": "test", "phase": "apply", "target": pkg, "applied": ["app/launcher.py"],
            "payload": "", "backup": "", "backup_hashes": {}, "files": {}, "removed": []}
    data.update(fields)
    with io.open(os.path.join(where, "update-txn.json"), "w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(data, ensure_ascii=False))
    return where


def run_server(pkg, data_dir, extra_env=None):
    env = dict(os.environ)
    env["RESUME_TRACKER_DATA_DIR"] = data_dir
    env["RESUME_TRACKER_NO_SHELL"] = "1"   # 自检不许真开资源管理器（2026-10-19）
    env["PYTHONIOENCODING"] = "utf-8"
    if extra_env:
        env.update(extra_env)
    try:
        proc = subprocess.run([PY, os.path.join(pkg, "app", "server.py")], env=env,
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=60, cwd=pkg)
    except subprocess.TimeoutExpired:
        return None, "启动后一直没退出（说明真的跑起来了）"
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args()
    root = tempfile.mkdtemp(prefix="rz-b5-")
    print("试验场：%s\n" % root)
    try:
        # 1) 未完成替换 → 拒绝
        print("== 1) 预置未完成（phase=apply）事务：必须拒绝启动 ==")
        a = make_install(root, "装A")
        write_txn(a, phase="apply")
        data_a = os.path.join(root, "data-a")
        code, out = run_server(a, data_a)
        check("退出码是 2（拒绝启动）", code == 2, "实际 %s" % code)
        check("输出里有人话指引", "升级" in out and "数据没被动" in out)
        check("没有建库（说明没走到 init_db）",
              not os.path.isfile(os.path.join(data_a, "applications.db")))

        # 2) 还没进替换阶段 → 放行
        print("\n== 2) phase=backup（尚未替换）：放行 ==")
        b = make_install(root, "装B")
        write_txn(b, phase="backup", applied=[])
        data_b = os.path.join(root, "data-b")
        code, out = run_server(b, data_b)
        check("允许启动（会建库）", os.path.isfile(os.path.join(data_b, "applications.db")),
              "退出码 %s" % code)

        # 3) 没有事务 → 正常
        print("\n== 3) 没有事务：照常启动 ==")
        c = make_install(root, "装C")
        data_c = os.path.join(root, "data-c")
        code, out = run_server(c, data_c)
        check("照常建库", os.path.isfile(os.path.join(data_c, "applications.db")), "退出码 %s" % code)

        # 4) 事务 target 与本安装不符 → 拒绝
        print("\n== 4) 事务记录的 target 不是本安装：拒绝 ==")
        d = make_install(root, "装D")
        write_txn(d, phase="apply", target=os.path.join(root, "别的安装"))
        data_d = os.path.join(root, "data-d")
        code, out = run_server(d, data_d)
        check("拒绝（退出码 2）", code == 2, "实际 %s" % code)
        check("没建库", not os.path.isfile(os.path.join(data_d, "applications.db")))

        # 5) 坏 JSON → 拒绝
        print("\n== 5) 事务文件是坏 JSON：拒绝 ==")
        e = make_install(root, "装E")
        where = os.path.join(e, "更新数据")
        os.makedirs(where, exist_ok=True)
        with io.open(os.path.join(where, "update-txn.json"), "w", encoding="utf-8", newline="\n") as handle:
            handle.write("{这不是 JSON")
        data_e = os.path.join(root, "data-e")
        code, out = run_server(e, data_e)
        check("拒绝（退出码 2）", code == 2, "实际 %s" % code)

        # 6) ★ 两个候选目录都要查：事务只落在 %LOCALAPPDATA% 那一份时也必须拦住
        #    （2026-10-12 Astra 复验指出的漏检：旧版只查一个目录）
        print("\n== 6) 事务只落在 %LOCALAPPDATA% 那一份：也必须拒绝 ==")
        f = make_install(root, "装F")
        fake_local = os.path.join(root, "fake-local")
        ext_dir = os.path.join(fake_local, "ResumeTracker", "更新数据")
        os.makedirs(ext_dir, exist_ok=True)
        with io.open(os.path.join(ext_dir, "update-txn.json"), "w", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps({"id": "x", "phase": "apply", "target": f,
                                     "applied": ["app/launcher.py"]}, ensure_ascii=False))
        data_f = os.path.join(root, "data-f")
        code, out = run_server(f, data_f, extra_env={"LOCALAPPDATA": fake_local})
        check("外部目录里的未完成事务也被拦住（退出码 2）", code == 2, "实际 %s" % code)
        check("同样没建库", not os.path.isfile(os.path.join(data_f, "applications.db")))

        # 7) 本地是"尚未替换"、外部是"已替换"→ 以更严重的为准，必须拒绝
        print("\n== 7) 本地安全阶段 + 外部已替换：必须拒绝（取更严重的那个）==")
        g = make_install(root, "装G")
        write_txn(g, phase="backup", applied=[])
        fake_local2 = os.path.join(root, "fake-local2")
        ext2 = os.path.join(fake_local2, "ResumeTracker", "更新数据")
        os.makedirs(ext2, exist_ok=True)
        with io.open(os.path.join(ext2, "update-txn.json"), "w", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps({"id": "y", "phase": "apply", "target": g,
                                     "applied": ["app/server.py"]}, ensure_ascii=False))
        data_g = os.path.join(root, "data-g")
        code, out = run_server(g, data_g, extra_env={"LOCALAPPDATA": fake_local2})
        check("拒绝（退出码 2）", code == 2, "实际 %s" % code)
        check("没建库", not os.path.isfile(os.path.join(data_g, "applications.db")))

        # 8) 静态检查：窗口入口也挂同一个判据
        print("\n== 8) 静态检查：窗口入口 launcher.main() 也检查 ==")
        text = io.open(os.path.join(APP, "launcher.py"), encoding="utf-8").read()
        main_body = text.split("def main():", 1)[-1][:600]
        check("launcher.main() 里调用了 update_txn.check_startup", "update_txn.check_startup" in main_body)
        check("拒绝时写启动失败通道（能看到原因）", "record_startup_error" in main_body)
        txn_src = io.open(os.path.join(APP, "services", "update_txn.py"), encoding="utf-8").read()
        check("检查逻辑真的遍历两个候选目录（txn_dirs）", "txn_dirs" in txn_src and "for base in txn_dirs" in txn_src)
    finally:
        if args.keep:
            print("\n（--keep：保留 %s）" % root)
        else:
            shutil.rmtree(root, ignore_errors=True)

    print("\n共 %d 项：通过 %d · 失败 %d" % (passed + failed, passed, failed))
    print("结果：" + ("全部通过" if ok else "存在失败项"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
