# -*- coding: utf-8 -*-
"""
在这台电脑把**打好的试用版**真跑一遍（自己机器上测那份"另一个版本"）。

  1. 把 试用包\应届海投管理-试用版\ 复制一份到 试用包\_自测副本\
     （不碰你在用的那份，更不碰你的数据）；
  2. 给副本换端口（默认 8769）和独立的单实例锁，所以能和主程序同时开着；
  3. 用 explorer 打开副本自己的「启动.cmd」（等同双击）——既测了启动脚本，
     也避开"AI 会话里直接起 pywebview 会 Failed to initialize Python.Runtime.dll"这个坑；
  4. 检查三件事：服务能应答、首页 HTML 正常、启动日志里有
     「页面已渲染（首屏真的画出来了）」（白屏防呆信号，没有它就可能白屏）；
  5. 只关掉命令行里带 _自测副本 的进程，主程序一律不碰。

用法：
    python tools\run_trial_check.py            # 默认端口 8769
    python tools\run_trial_check.py 8771       # 换端口
    python tools\run_trial_check.py --keep     # 留着副本，方便翻日志
"""
from __future__ import print_function

import io
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, u"试用包", u"应届海投管理-试用版")
WORK = os.path.join(ROOT, u"试用包", u"_自测副本")


def say(t):
    print(t)


def fetch(url, timeout=5):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def close_copy(port, log_path):
    """
    让副本自己关掉：先走程序自己的外壳通道（页面发给启动器的那条 close_window），
    不行再按端口找进程强杀。只认副本自己的 shport / 副本的端口，主程序一律不碰。
    """
    done = []
    text = ""
    if os.path.isfile(log_path):
        text = io.open(log_path, encoding="utf-8", errors="ignore").read()
    m = None
    for line in text.splitlines():
        if "shport=" in line:
            m = line.split("shport=")[-1].split("&")[0].strip()
    if m and m.isdigit():
        try:
            req = urllib.request.Request(
                "http://127.0.0.1:%s/" % m,
                data=json.dumps({"action": "close_window"}).encode("utf-8"),
                headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=5).read()
            done.append(u"已请副本自己关窗（外壳通道 %s）" % m)
            time.sleep(4)
        except Exception as exc:
            done.append(u"外壳通道没关成（%s）" % exc)
    try:
        out = subprocess.check_output(["netstat", "-ano", "-p", "TCP"],
                                      universal_newlines=True)
        for line in out.splitlines():
            if (":" + str(port)) in line and "LISTENING" in line:
                pid = line.split()[-1]
                if pid.isdigit():
                    subprocess.call(["taskkill", "/PID", pid, "/T", "/F"],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    done.append(u"按端口 %d 关掉了残留进程 %s" % (port, pid))
    except Exception as exc:
        done.append(u"按端口清理没做成（%s）" % exc)
    return done


def write_cfg(path, port):
    cfg = json.loads(io.open(path, encoding="utf-8").read())
    cfg["port"] = port
    cfg["relaunch_windowed"] = False
    cfg["mutex"] = "Local" + chr(92) + "ResumeTrackerV3_TrialSelfCheck"
    io.open(path, "w", encoding="utf-8", newline="\n").write(
        json.dumps(cfg, ensure_ascii=False, indent=2) + "\n")


def main():
    plain = [a for a in sys.argv[1:] if not a.startswith("--")]
    keep = "--keep" in sys.argv[1:]
    port = int(plain[0]) if plain else 8769

    if not os.path.isdir(SRC):
        say(u"没找到试用包目录：%s（先跑 tools" + chr(92) + "make_trial_package.py）" % SRC)
        return 1
    if os.path.isdir(WORK):
        shutil.rmtree(WORK, ignore_errors=True)
    shutil.copytree(SRC, WORK)
    say(u"① 已复制一份到 _自测副本（你原来那份没动）")

    write_cfg(os.path.join(WORK, "config.json"), port)
    say(u"② 副本端口改成 %d，单实例锁也换成独立的（能和主程序同时开）" % port)

    log_path = os.path.join(WORK, "logs", "launcher.log")
    subprocess.Popen(["explorer.exe", os.path.join(WORK, u"启动.cmd")])
    say(u"③ 已让 explorer 打开副本的启动脚本（等同双击），等窗口和服务……")

    data = None
    ok_state = False
    deadline = time.time() + 90
    while time.time() < deadline:
        try:
            data = json.loads(fetch("http://127.0.0.1:%d/api/state" % port, 4))
            ok_state = True
            break
        except Exception:
            time.sleep(2)
    say(u"   [%s] 服务能应答（/api/state）" % (u"PASS" if ok_state else u"FAIL"))

    if ok_state:
        try:
            html = fetch("http://127.0.0.1:%d/" % port, 8)
            ok_page = ("<title>" in html) and (len(html) > 5000)
            say(u"   [%s] 首页 HTML 正常（%d 字节）" % (u"PASS" if ok_page else u"FAIL", len(html)))
        except Exception as exc:
            ok_page = False
            say(u"   [FAIL] 首页打不开：%s" % exc)
    else:
        ok_page = False
        say(u"   [SKIP] 首页没测（服务没起来）")

    text = ""
    for _ in range(20):
        if os.path.isfile(log_path):
            text = io.open(log_path, encoding="utf-8", errors="ignore").read()
            if u"页面已渲染" in text or u"等了 8 秒" in text or u"Traceback" in text:
                break
        time.sleep(1)
    ok_render = u"页面已渲染（首屏真的画出来了）" in text
    blank = u"等了 8 秒" in text
    say(u"   [%s] 启动日志里有「页面已渲染」%s"
        % (u"PASS" if ok_render else u"FAIL", u"（出现过白屏嫌疑！）" if blank else u""))
    if not ok_render and u"Traceback" in text:
        say(u"   副本日志里有报错，末尾几行：")
        for line in text.strip().splitlines()[-4:]:
            say(u"     " + line.strip())
    if data:
        say(u"   副本数据：投递 %d · 宣讲会 %d · 阶段 %d（空白库，和你自己的数据无关）"
            % (len(data.get("applications") or []), len(data.get("talks") or []),
               len(data.get("stages") or [])))

    for line in close_copy(port, log_path):
        say(u"④ " + line)

    if not keep:
        time.sleep(2)
        shutil.rmtree(WORK, ignore_errors=True)
        say(u"⑤ 副本目录已清理（加 --keep 可以留着看日志）")

    ok = ok_state and ok_page and ok_render
    say(u"结果：%s" % (u"试用版在这台电脑能正常跑起来" if ok else u"有问题，见上面 FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
