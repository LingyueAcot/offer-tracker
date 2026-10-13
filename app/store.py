# -*- coding: utf-8 -*-
"""
store.py —— 数据库访问的唯一出口（连接 + 事务 + 行映射）

【这个文件是什么】
2026-09-24 新增的那一层。原来 server.py 里 41 处手写 `conn = get_conn()` / `conn.close()`，
其中**只有 2 处在 `finally` 里**（init_db 和同步 worker），其余 39 处都是
「正常路径 commit、末尾 close」的直线写法，早期 return 靠手写 close 兜。
`ThreadingHTTPServer` 是多线程的，所以**任何没预料到的异常都会漏一条连接**，直到 GC 回收。

参考的是 `sqlite-utils` 的形状（七个参考项目里最直接可搬的一个）：
一个对象独占连接、其它代码都从它拿 cursor；**SQL 只从这一层出**。

【规矩】（改这里之前先读）
- **只有这一层可以 `sqlite3.connect`**。`services/`、`api/`、`core/` 都不许自己连库。
- 这里**不放业务规则**。它不知道「投递」「宣讲会」是什么，只负责把 SQL 稳稳地跑完、把连接还回去。
- 不 import server / api / services（否则又成环）。需要的路径由调用方传进来。
- 只用标准库。

【怎么用】

    # 一次事务（提交 / 回滚 / 关闭都由它兜住，异常也不会漏连接）
    with store.cursor() as cur:
        cur.execute("INSERT INTO notes ...")
        note_id = cur.lastrowid

    # 带锁的完整用法（锁和连接一起拿，顺序固定，避免死锁）
    with store.locked_cursor() as cur:
        ...

【为什么 `sys.exit` 不是问题】
`cursor()` 里提交失败不会吞异常 —— 宁可报出来，也不留一个「看起来成功了、
其实没写进去」的状态。这跟 backup.py 的「宁可没有备份，也不留一个坏备份」是同一个原则。
"""

import os
import sqlite3
import threading

# 连接超时（秒）。原来 server.py 里写死 20，搬过来保持不变。
CONNECT_TIMEOUT = 20.0


class Store(object):
    """
    连接持有者 + SQL 唯一出口。

    一个 Store 实例管一个库文件。默认那个实例见文件末尾的 `default_store()` ——
    它由 server.py 在启动时用真实路径装好，其它模块直接 `import store; store.db()` 用。
    """

    def __init__(self, db_path=None):
        self.db_path = db_path
        # 全局串行化用的可重入锁。原来 server.py 里就叫 LOCK，语义一字不动搬过来。
        # 用 RLock 是必需的：调用链里有「拿着锁再进一层同样要锁的函数」的情况。
        self.lock = threading.RLock()

    # ----------------------------------------------------------------- #
    # 连接
    # ----------------------------------------------------------------- #
    def connect(self):
        """
        新开一条连接。

        ⚠️ **优先用 `cursor()` / `conn()` 那几个上下文管理器，不要直接用它。**
        这个方法只是「开一条连接」，它**不负责关** —— 而那正是这一层要消灭的东西。
        直接用它的代价就是得自己保证 close（原来 server.py 41 处就是这么写的，
        其中 39 处没写 `finally`）。
        """
        if not self.db_path:
            raise RuntimeError(
                "store 还没配好库路径。server.py 启动时要先调 store.use(<db 路径>)。")
        conn = sqlite3.connect(self.db_path, timeout=CONNECT_TIMEOUT)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def cursor(self):
        """
        借一个 cursor，出块时自动提交 + 关闭（异常则回滚 + 关闭）。

            with store.cursor() as cur:
                cur.execute(...)

        两个刻意的设计：
        1. **出块时提交**。调用方不用再写 `conn.commit()` —— 但写了也不报错，
           因为出块时再 commit 一次是空操作。这样替换旧代码时可以逐处进行。
        2. **异常时回滚再抛**。不吞异常（吞了就成了「静默失败」），
           但保证连接一定还回去。
        """
        return _CursorContext(self, self.lock, already_locked=False)

    def locked_cursor(self):
        """
        跟 `cursor()` 一样，但**顺便把全局锁也拿上**。

        替换原来的 `with LOCK:` + `conn = get_conn()` 两行 —— 锁与连接的获取顺序
        固定成「先锁后连接」，所有调用点都一样，就不会有两条路径用相反顺序拿、互相等死。
        """
        return _CursorContext(self, self.lock, already_locked=True)

    def conn(self):
        """
        借**连接本身**（不是 cursor），出块时自动提交 + 关闭。

        给「必须拿到连接」的地方用 —— 目前只有 `init_db()`：它要把连接交给
        `schema.ensure_schema(conn, ...)`，那个函数自己开 cursor、还要执行多条 DDL。

        ⚠️ 用它的时候拿不到 cursor，要就自己 `conn.cursor()`；只想拿 cursor 就直接用 `cursor()`。
        """
        return _ConnContext(self, self.lock, already_locked=False)

    def locked_conn(self):
        """跟 `conn()` 一样，但顺便把全局锁也拿上。"""
        return _ConnContext(self, self.lock, already_locked=True)

    # ----------------------------------------------------------------- #
    # 行映射（原来散在 server.py 的 _app_dict / _event_dict / _talk_dict）
    # ----------------------------------------------------------------- #
    @staticmethod
    def row_dict(row):
        """sqlite3.Row -> dict；row 是 None 就返回 None。"""
        return dict(row) if row else None

    @staticmethod
    def rows_dict(rows):
        """一串 Row -> 一串 dict。"""
        return [dict(r) for r in rows]

    def app_dict(self, cur, app_id):
        row = cur.execute("SELECT * FROM applications WHERE id=?", (app_id,)).fetchone()
        return self.row_dict(row)

    def event_dict(self, cur, event_id):
        row = cur.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
        return self.row_dict(row)

    def talk_dict(self, cur, talk_id):
        row = cur.execute("SELECT * FROM talks WHERE id=?", (talk_id,)).fetchone()
        return self.row_dict(row)


class _ResourceContext(object):
    """
    「借两件东西」的公共部分：全局锁（可选）+ 一条连接。

    手写类而不是用 `contextlib.contextmanager`，是因为要同时管两件资源，
    手写更容易看清「谁先谁后、异常时各自怎么办」。
    下面两个子类只差「`__enter__` 交出去的是什么」。
    """

    __slots__ = ("_store", "_lock", "_need_lock", "_conn")

    def __init__(self, store, lock, already_locked):
        self._store = store
        self._lock = lock
        self._need_lock = already_locked
        self._conn = None

    def _open(self):
        """拿锁 + 连库。连不上时锁一定要还回去，否则整个程序僵住。"""
        if self._need_lock:
            self._lock.acquire()
        try:
            self._conn = self._store.connect()
        except BaseException:
            if self._need_lock:
                self._lock.release()
            raise

    def __exit__(self, exc_type, exc_value, tb):
        try:
            if self._conn is not None:
                if exc_type is None:
                    self._conn.commit()
                else:
                    try:
                        self._conn.rollback()
                    except Exception:
                        # 回滚都失败了（库坏了 / 已被关掉），没有别的补救办法；
                        # 不能因为回滚失败就把真正要报的那个异常吃掉。
                        pass
        finally:
            if self._conn is not None:
                try:
                    self._conn.close()
                except Exception:
                    pass
            if self._need_lock:
                self._lock.release()
        return False      # 不吞异常


class _CursorContext(_ResourceContext):
    """`store.cursor()` / `store.locked_cursor()` 返回的东西，交出一个 cursor。"""

    __slots__ = ("_cur",)

    def __enter__(self):
        self._open()
        try:
            self._cur = self._conn.cursor()
        except BaseException:
            # 建 cursor 失败也要把锁/连接还回去
            self._conn.close()
            if self._need_lock:
                self._lock.release()
            raise
        return self._cur


class _ConnContext(_ResourceContext):
    """`store.conn()` / `store.locked_conn()` 返回的东西，交出连接本身。"""

    __slots__ = ()

    def __enter__(self):
        self._open()
        return self._conn


# --------------------------------------------------------------------------- #
# 默认实例：server.py 启动时装好，其它模块直接用
#
# 为什么不在这里写死路径：路径的唯一定义处是 server.py 顶部的常量
# （`DB_PATH = os.path.join(BASE_DIR, "data", "applications.db")`）。
# 在这里再算一遍 BASE_DIR 就等于把「程序根目录在哪」这件事写了两份，
# 两份迟早会不一致 —— 所以由 server.py 调 `use()` 注入。
# --------------------------------------------------------------------------- #
_DEFAULT = Store()


def use(db_path):
    """server.py 启动时调一次，把真实库路径装进来。"""
    _DEFAULT.db_path = db_path
    return _DEFAULT


def db():
    """拿默认实例。`db().cursor()` / `db().locked_cursor()` / `db().lock`。"""
    return _DEFAULT


def lock():
    """拿默认实例的全局锁（等价于原来 server.py 里的 `LOCK`）。"""
    return _DEFAULT.lock


def read_schema_version():
    """读库里**真实记录**的结构版本（没有这条记录/表还没有 → 返回 None）。

    2026-10-11 加：给"这份程序能不能打开这个库"的判据用（`services/schema_compat.py`）。
    ⚠️ 要用**库里的值**、不能用程序常量 —— `services/state.py` 返回的是程序自己声明的版本，
    而库里那条记录才是"这个文件到底是什么结构"。以前 `schema.py` 用 `INSERT OR IGNORE` 写它，
    迁移完不会更新，所以两者会长期不一致（那处已改成 UPSERT）。

    ⚠️⚠️ **全新安装（库文件还不存在）必须返回 None，不能报错**（2026-10-12 发布包首启自检逮到的真 bug）：
    新装时 `<数据目录>` 甚至还没建，直接连会抛 `unable to open database file`，
    被"严格判据"当成"库损坏/被占用" → **全新安装永远起不来，界面一直停在启动画面**。
    注意区分三种"没有"：①库文件不存在＝全新安装（放行去建库）；②库在但没有 meta 表＝很老的库（放行去迁移）；
    ③库在、读取出错（损坏/被占用/权限）＝**上报**，让启动入口拒绝并说人话。
    """
    if not os.path.isfile(_DEFAULT.db_path or ""):
        return None
    with _DEFAULT.cursor() as cur:
        # 只把确定不存在的表当作老库；损坏、锁定和 I/O 错误必须传给启动入口。
        exists = cur.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'").fetchone()
        if not exists:
            return None
        cur.execute("SELECT value FROM meta WHERE key = 'schema_version'")
        row = cur.fetchone()
    if not row:
        return None
    value = row[0] if not isinstance(row, dict) else row.get("value")
    try:
        return int(value)
    except (TypeError, ValueError):
        return value          # 非整数就原样带出去，让判据去拒绝（别在这里静默成 None）
