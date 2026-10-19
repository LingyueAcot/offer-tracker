# 参与贡献

先谢谢你愿意花时间。这个项目是**一个人自己用的工具开源出来**的，所以：

- 欢迎 **报 bug、提改进**（issue）和**小改动**（PR）；
- 大改动（重构、换技术栈、加依赖）**请先开 issue 聊**：我很可能因为"要守住零依赖 / 无构建步骤"而婉拒，
  先聊能省你写代码的时间。

## 两条不能破的性质

1. **零运行时依赖**：后端只用 Python 标准库；前端是单个 HTML，不引 CDN、不上 npm 构建。
   第三方只允许"外壳"用的 `pywebview`。
2. **没有构建步骤**：改完保存、刷新就生效。

（这两条不是洁癖：它换来的是"用户拿到就能跑、出问题我能一眼看出在哪"。）

## 环境

- **Windows 10/11 x64**（这个程序只支持 Windows）。
- **Python 3.12**。
- 想跑"要开程序"的自检，还需要电脑上有 **Chrome 或 Edge**（用无界面方式抓页面）。

```bat
pip install pywebview
:: 可选：要用「导入 Excel」才需要
pip install openpyxl
```

启动：双击 `app\启动（调试窗口）.cmd`（保留命令行输出，方便看报错）。

## 改完必须跑自检（这一步不能省）

**不用开程序**的那批（很快，随手跑）：

```bat
python tools\test_architecture.py   :: 依赖方向：core 只许标准库、SQL 只从 store 出、api 不许反向依赖
python tools\test_core.py
python tools\test_schema.py         :: 表结构 / 老库升级
python tools\test_store.py
python tools\test_backup.py
python tools\test_resume.py
python tools\test_resume_pptx.py
python tools\test_resume_photo.py
python tools\test_diagnostics.py
python tools\test_data_safety.py    :: 数据红线
python tools\test_window_state.py
python tools\test_shortcut.py
python tools\test_routes.py
node   tools\test_web_lib.js
```

**要开程序**的那批（先启动程序，默认端口 8768）：

```bat
node   tools\verify_desktop.js 8768 no-snake
node   tools\verify_shell.js 8768
python tools\test_desktop.py 8768
python tools\verify_actions.py         :: 真点控件 + 抓事件里的静默 JS 异常
python tools\test_desktop_exit.py      :: 关窗退出（会自己起临时实例）
```

每个脚本的最后一行是「结果：全部通过 / 存在失败项」，**以那一行为准**。
`[SKIP]` 表示"前提不满足、没测"，不是"测过了"。

## 提交规范

- commit message 写清**改了什么 + 拿什么证明的**（跑了哪几条自检、结果如何）。
- 换行符保持 **LF**。
- 一个 commit 只做一件事；大改动拆成几个能单独回退的提交。

## ★ 不要提交用户数据

- `app/data/`、`app/logs/`、`app/webview_profile/`、`app/browser_profile/`、`app/config.json`、
  `app/个人画像.json` —— 这些都在 `.gitignore` 里，**别为了"方便"把它们加进来**。
- 自检造的临时文件（`tools/.scratch/`）也别提交。
- CI 第一条检查就是"仓库里不许有用户数据"，提交了会直接红。

## 自检脚本里必须显式指定数据目录

程序按「环境变量 `RESUME_TRACKER_DATA_DIR` → `config.json` 的 `data_dir` → 程序旁的 `data/` →
`%LOCALAPPDATA%\ResumeTracker\`」决定数据放哪（实现见 `app/paths.py`）。

**任何"复制一份程序起来跑"的脚本，都要先把环境变量指到临时目录**，否则那份副本会被判成"新装"、
去写用户**真实**的数据目录：

```python
os.environ["RESUME_TRACKER_DATA_DIR"] = os.path.join(scratch, "data")   # 要在 import server 之前
```

想验"老库升级"这种要真实数据的场景：**把库复制一份**到副本的 `data/` 里，别去指真库。

## 报告问题

- 出问题时先点界面上的「导出诊断报告」：里面有版本、系统、DPI、日志尾部（**不含你的数据内容**），
  贴到 issue 里比自己描述管用得多。
- 安全相关的问题请走 `SECURITY.md` 里的渠道，别开公开 issue。
