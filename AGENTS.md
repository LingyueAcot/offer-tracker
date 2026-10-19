# AGENTS.md —— 给「在这个仓库里干活的人 / AI」的项目地图

> 面向人的入口是 `README.md`；这份讲**规矩与边界**：改哪儿、哪些线不能碰、改完拿什么证明。
> 数据怎么走、谁负责什么：看 `docs/架构.md`。

## 这是什么

Windows 桌面程序：记录求职投递进度、看宣讲会行程、随手记备忘，外加一个简历编辑器（导出 PDF / PPTX）。
**纯本地**：数据是一个 SQLite 文件，没有账号、没有云端；只有「同步宣讲会」和「检查更新」会联网。

两条从第一天就守着的性质，**所有技术选择都服从它们**：

1. **零运行时依赖**：后端只用 Python 标准库（`http.server` + `sqlite3`），前端是单个 HTML。
   第三方只有外壳用的 `pywebview`（Windows 上用系统 WebView2 渲染）。
2. **没有构建步骤**：改 `app/web/index.html` 保存、刷新页面就生效。

## 目录地图

| 路径 | 是什么 | 能改吗 |
| --- | --- | --- |
| `app/` | 程序本体：`launcher.py`（窗口宿主）、`server.py`（后端编排）、`store.py`（唯一 SQL 出口）、`schema.py`（唯一改表结构处）、`core/`（纯函数）、`services/`（用例编排）、`api/`（HTTP 路由按域拆）、`web/`（前端） | **主战场** |
| `app/data/` `app/logs/` | **用户数据与日志**（运行产物，不在仓库里） | **绝对不要动 / 不要提交** |
| `tools/` | 自检脚本（改完代码必须跑） | 能改，改完自己也要跑 |
| `docs/` | 架构、代码规范、数据契约、桌面化改造记录 | 能改 |
| `.github/workflows/` | CI（只跑"不需要开程序 / 浏览器"的那批门） | 能改 |

## ★ 数据红线（第一条）

**程序文件和数据彻底分开。** 具体到操作：

- **不要**把 `app/data/`、`app/logs/`、`app/个人画像.json` 提交进仓库；
  CI 里第一条检查就是"仓库里不许有用户数据"。
- **不要**拿真实数据当试验田。自检一律用临时目录或副本。
- 任何会写数据的自检 / 脚本，**必须显式指定数据目录**（环境变量 `RESUME_TRACKER_DATA_DIR`）：
  程序按「环境变量 → `config.json` 的 `data_dir` → 程序旁的 `data/` → `%LOCALAPPDATA%\ResumeTracker\`」
  的顺序决定数据放哪（细节见 `app/paths.py`）。不指定的话，一份副本会被判成"新装"、
  跑去写**用户真实的数据目录** —— 这是踩过的坑，别重演。
- 表结构变更**只允许**写在 `app/schema.py`（只加不改，老库下次启动自动补字段）。

## 代码边界（有自动检查盯着，不靠自觉）

- **`app/core/` 只放纯函数**：不许 import `server`、`sqlite3`、`urllib`、`subprocess`、`socket`、`threading`，
  也不许 `open()` 文件。同样的输入必须给同样的输出。
- **`app/store.py` 是唯一的 SQL 出口**：只有这一层可以 `sqlite3.connect`。
- **`app/api/` 只放路由**：每个域一个 mixin；**不许模块级 `from server import`**（会成环）。
  新业务优先放 `app/services/`（"读 → 算 → 写 → 记审计"整套用例）。
- **`app/server.py` 只留启动编排 + HTTP 管道 + 四条域链**，别往里堆业务。

以上每一条都由 `python tools/test_architecture.py` 用 AST 读源码检查。

## 改完代码要跑什么（**看到全过再说"做完了"**）

不用开程序（很快，随手跑）：

```bat
python tools\test_architecture.py   :: 依赖方向（先跑这个）
python tools\test_core.py           :: core 纯函数
python tools\test_schema.py         :: 表结构 / 老库升级
python tools\test_store.py          :: 数据访问层
python tools\test_backup.py         :: 启动备份
python tools\test_resume.py         :: 简历域
python tools\test_resume_pptx.py    :: PPTX 导出
python tools\test_resume_photo.py   :: 证件照校验
python tools\test_diagnostics.py    :: 诊断报告
python tools\test_data_safety.py    :: 数据红线（静态）
python tools\test_window_state.py   :: 窗口位置 / DPI / 多屏（纯逻辑）
python tools\test_shortcut.py       :: 桌面快捷方式与图标
python tools\test_routes.py         :: HTTP 路由（自己起隔离副本）
node   tools\test_web_lib.js        :: 前端纯函数
```

要开程序（先启动程序，端口默认 8768）：

```bat
node   tools\verify_desktop.js 8768 no-snake   :: 界面
node   tools\verify_shell.js 8768              :: 自绘外壳
python tools\test_desktop.py 8768              :: 接口
python tools\verify_actions.py                 :: 真点控件 + 抓静默 JS 异常（自己起隔离副本）
python tools\test_desktop_exit.py              :: 关窗退出（自己起临时实例）
```

改动 → 要跑什么（按性质，别凭感觉）：

| 改了哪 | 至少跑 |
| --- | --- |
| 界面 / 样式 / 文案 | `verify_desktop.js` + `verify_shell.js` |
| 前端纯函数（`web/lib.js`） | `test_web_lib.js` + `verify_desktop.js` |
| 接口 / 数据模型 | `test_desktop.py` + 拿**数据副本**验老库能升上来 |
| 表结构（`app/schema.py`） | `test_schema.py` + 与改动前的启动备份对表结构 / 行数 |
| 模块结构 / 依赖方向 | `test_architecture.py`（先跑） |
| 窗口 / 外壳行为 | `verify_shell.js` + `test_window_state.py` + `test_shortcut.py` |
| 文档 | 检查里面的命令和链接是不是真的；**没跑过的命令不要写成跑过** |

## 提交之前

- 每个自检脚本最后一行会打「结果：全部通过 / 存在失败项」，**以那一行为准**。
- 数据不满足前提时脚本会用 `[SKIP]` 如实标出来 —— 那是"没测"，不是"测过了"。
- commit message 写清「改了什么 + 拿什么证明的」。
- 换行符保持 **LF**（历史上出过一次 CRLF 事故：diff 变成"整文件每一行都改了"，
  真正改了什么完全看不出来）。

## 已知的坑（都拆到对应目录的文档里）

| 想看什么 | 去哪 |
| --- | --- |
| 外壳 / 窗口 / 启动链路 / 图标（环回端口 timeout、js_api 不可靠、拖动假死、无边框黑边、DPI…） | `docs/桌面化改造方案.md` |
| 数据怎么走、谁负责什么 | `docs/架构.md` |
| 代码规范与取舍、以后怎么拆 | `docs/代码规范.md` |
| 简历的数据契约 | `docs/简历功能-数据契约.md` |
| 诊断报告与运行记录 | `docs/诊断与运行记录方案.md` |
| 第三方组件与来源 | `docs/第三方来源清单.md` |
| 界面怎么用、出问题怎么办 | `app/使用说明.md` |
| 接口清单（加接口前必看） | `app/数据接口说明.md` |
