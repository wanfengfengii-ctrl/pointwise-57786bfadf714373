# -*- coding: utf-8 -*-
"""涂层检验站：基于 SQLite 的测厚原据/接管/实收/封口状态机。

业务规则（对应题面）：
- Station(设备库, 站库)：设备库为 None 表示停源，cal/open(新班)/load 拒绝执行；
  站库侧的认领、续收、封口、查询不受影响。
- cal(版, 倍率, 偏置) 在设备库登记定标；open 冻结已登记版本的倍率/偏置快照。
- load 进 FIFO 空槽队列；peek 看队首；route 改队首计划口。
- take 实出口出料并接管：原据落站库，返回 [班, 片, 读值, 实出口, 递增出料序]；
  断点态表示出料失答，片已离槽但未释放；重开同参重领返回原据、不重出不增序。
- accept 仅实出口收已接管片，每片一次，同值各收；fail 在实收提交前中断、无新增。
- seal 按出料序存实收明细及条数/最小/最大；空口极值为 None。
- get(report|receipt, 号) 领存值，未知号为 None；同号同参复领，冲突/非法/注入抛异常。
- 精确分数一律以字符串表示（如 '3/2'、'-1/2'、'11/2'、'5'）。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from fractions import Fraction

__all__ = ["Station", "StationError"]

_QUEUED = "queued"      # 在槽
_TAKEN = "taken"        # 已出料接管、已提交
_PENDING = "pending"    # 断点：出料失答，原据已落站库待认回
_DONE = "done"          # 已实收释放


class StationError(Exception):
    """冲突、非法参数、注入/持久化失败统一抛出的异常。"""


def _frac(value) -> Fraction:
    """只接受整数或 '整数/整数' 形式的精确分数字符串，拒绝浮点与非法文本。"""
    if isinstance(value, bool):
        raise StationError("定标参数不接受布尔值: %r" % (value,))
    if isinstance(value, int):
        return Fraction(value)
    if isinstance(value, str):
        s = value.strip()
        parts = s.split("/")
        if len(parts) not in (1, 2) or any(p == "" for p in parts):
            raise StationError("非法分数字符串: %r" % (value,))
        try:
            nums = [int(p) for p in parts]
        except ValueError:
            raise StationError("非法分数字符串: %r" % (value,))
        if len(nums) == 2:
            if nums[1] == 0:
                raise StationError("分数分母为零: %r" % (value,))
            return Fraction(nums[0], nums[1])
        return Fraction(nums[0])
    raise StationError("定标参数必须是整数或分数字符串: %r" % (value,))


def _frac_str(value) -> str:
    return str(_frac(value))


def _is_str(value, name: str) -> str:
    if not isinstance(value, str) or value == "":
        raise StationError("%s 必须是非空字符串" % name)
    return value


class Station:
    def __init__(self, device_db, station_db):
        self._lock = threading.RLock()
        # 设备库：None 表示停源
        if device_db is None:
            self._dev = None
        else:
            if not isinstance(device_db, str) or device_db == "":
                raise StationError("设备库路径必须是字符串或 None")
            self._dev = sqlite3.connect(device_db)
            self._dev.execute(
                "CREATE TABLE IF NOT EXISTS calib ("
                "version TEXT PRIMARY KEY, mult TEXT NOT NULL, offset TEXT NOT NULL)"
            )
            self._dev.commit()

        if not isinstance(station_db, str) or station_db == "":
            raise StationError("站库路径必须是非空字符串")
        self._st = sqlite3.connect(station_db)
        self._st.executescript(
            """
            CREATE TABLE IF NOT EXISTS shift (
                shift TEXT PRIMARY KEY,
                version TEXT NOT NULL,
                mult TEXT NOT NULL,
                offset TEXT NOT NULL,
                port_a TEXT NOT NULL,
                port_b TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS piece (
                shift TEXT NOT NULL,
                piece TEXT NOT NULL,
                reading INTEGER NOT NULL,
                planned_port TEXT NOT NULL,
                state TEXT NOT NULL,
                seq INTEGER,
                actual_port TEXT,
                sample_no TEXT,
                fail INTEGER NOT NULL DEFAULT 0,
                raw_json TEXT
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_piece_pk
                ON piece(shift, piece);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_piece_sample
                ON piece(sample_no) WHERE sample_no IS NOT NULL;
            CREATE TABLE IF NOT EXISTS receipt (
                shift TEXT NOT NULL,
                piece TEXT NOT NULL,
                sample_no TEXT NOT NULL UNIQUE,
                port TEXT NOT NULL,
                seq INTEGER NOT NULL,
                reading INTEGER NOT NULL,
                calibrated TEXT NOT NULL,
                version TEXT NOT NULL,
                first INTEGER NOT NULL,
                fail INTEGER NOT NULL DEFAULT 0,
                raw_json TEXT NOT NULL,
                PRIMARY KEY (shift, piece)
            );
            CREATE TABLE IF NOT EXISTS report (
                report_no TEXT PRIMARY KEY,
                shift TEXT NOT NULL,
                port TEXT NOT NULL,
                details TEXT NOT NULL,
                cnt INTEGER NOT NULL,
                minv TEXT,
                maxv TEXT
            );
            """
        )
        self._st.commit()

    # ---- 内部工具 -------------------------------------------------------

    def _require_device(self, action: str):
        if self._dev is None:
            raise StationError("设备库缺失（源已停），禁止%s" % action)

    def _shift(self, shift):
        cur = self._st.execute(
            "SELECT version, mult, offset, port_a, port_b FROM shift WHERE shift=?",
            (shift,),
        )
        row = cur.fetchone()
        if row is None:
            raise StationError("未知班次: %r" % (shift,))
        return {
            "version": row[0],
            "mult": row[1],
            "offset": row[2],
            "ports": (row[3], row[4]),
        }

    def _piece(self, shift, piece):
        cur = self._st.execute(
            "SELECT piece, reading, planned_port, state, seq, actual_port, "
            "sample_no, fail, raw_json FROM piece WHERE shift=? AND piece=?",
            (shift, piece),
        )
        row = cur.fetchone()
        if row is None:
            raise StationError("未知片号: 班=%r 片=%r" % (shift, piece))
        return row

    def _head(self, shift):
        cur = self._st.execute(
            "SELECT piece, reading, planned_port FROM piece "
            "WHERE shift=? AND state=? ORDER BY rowid LIMIT 1",
            (shift, _QUEUED),
        )
        return cur.fetchone()

    # ---- 设备库：定标登记 ----------------------------------------------

    def cal(self, version, mult, offset):
        """登记定标。同版同参幂等，同版异参冲突抛异常。"""
        with self._lock:
            self._require_device("登记定标")
            version = _is_str(version, "版号")
            m = _frac_str(mult)
            o = _frac_str(offset)
            cur = self._dev.execute(
                "SELECT mult, offset FROM calib WHERE version=?", (version,)
            )
            row = cur.fetchone()
            if row is not None:
                if row[0] != m or row[1] != o:
                    raise StationError(
                        "定标版本 %r 已登记且参数冲突: 已存(%s,%s) 新参(%s,%s)"
                        % (version, row[0], row[1], m, o)
                    )
                return
            self._dev.execute(
                "INSERT INTO calib(version, mult, offset) VALUES(?,?,?)",
                (version, m, o),
            )
            self._dev.commit()

    # ---- 班次：冻结/重开 -----------------------------------------------

    def open(self, shift, version, a, b):
        """开班并冻结已登记定标版本；重开同班须同参，冲突抛异常。"""
        with self._lock:
            shift = _is_str(shift, "班号")
            version = _is_str(version, "版号")
            a = _is_str(a, "甲口")
            b = _is_str(b, "乙口")
            if a == b:
                raise StationError("甲口与乙口必须不同")

            cur = self._st.execute(
                "SELECT version, port_a, port_b FROM shift WHERE shift=?", (shift,)
            )
            row = cur.fetchone()
            if row is not None:
                if (row[0], row[1], row[2]) != (version, a, b):
                    raise StationError(
                        "班次 %r 重开参数冲突: 已存(%r,%r,%r) 新参(%r,%r,%r)"
                        % (shift, row[0], row[1], row[2], version, a, b)
                    )
                return

            # 新开班次必须能在设备库认领到已登记版本
            self._require_device("开班")
            dcur = self._dev.execute(
                "SELECT mult, offset FROM calib WHERE version=?", (version,)
            )
            drow = dcur.fetchone()
            if drow is None:
                raise StationError("定标版本 %r 未登记，无法冻结开班" % version)
            self._st.execute(
                "INSERT INTO shift(shift, version, mult, offset, port_a, port_b) "
                "VALUES(?,?,?,?,?,?)",
                (shift, version, drow[0], drow[1], a, b),
            )
            self._st.commit()

    # ---- 槽位 -----------------------------------------------------------

    def load(self, shift, piece, reading, planned_port):
        """进空槽（FIFO 队尾）。读值必须为整数，计划口须为本班两口之一。"""
        with self._lock:
            self._require_device("进料")
            info = self._shift(shift)
            piece = _is_str(piece, "片号")
            if isinstance(reading, bool) or not isinstance(reading, int):
                raise StationError("读值必须是整数: %r" % (reading,))
            planned_port = _is_str(planned_port, "计划口")
            if planned_port not in info["ports"]:
                raise StationError(
                    "计划口 %r 不属于班次 %r 的两口 %r"
                    % (planned_port, shift, info["ports"])
                )
            cur = self._st.execute(
                "SELECT 1 FROM piece WHERE shift=? AND piece=?", (shift, piece)
            )
            if cur.fetchone() is not None:
                raise StationError(
                    "片号 %r 在班 %r 已存在，重投不得重出或释放后继件"
                    % (piece, shift)
                )
            self._st.execute(
                "INSERT INTO piece(shift, piece, reading, planned_port, state) "
                "VALUES(?,?,?,?,?)",
                (shift, piece, reading, planned_port, _QUEUED),
            )
            self._st.commit()

    def peek(self, shift):
        """回队首 [片, 读值, 计划口]；空槽为 None。"""
        with self._lock:
            self._shift(shift)
            row = self._head(shift)
            if row is None:
                return None
            return [row[0], row[1], row[2]]

    def route(self, shift, port):
        """队首改道；空槽或非法口抛异常。"""
        with self._lock:
            info = self._shift(shift)
            port = _is_str(port, "目标口")
            if port not in info["ports"]:
                raise StationError("改道口 %r 不属于本班两口 %r" % (port, info["ports"]))
            row = self._head(shift)
            if row is None:
                raise StationError("空槽无队首，无法改道")
            self._st.execute(
                "UPDATE piece SET planned_port=? WHERE shift=? AND piece=?",
                (port, shift, row[0]),
            )
            self._st.commit()

    # ---- 出料接管 -------------------------------------------------------

    def take(self, shift, piece, sample_no, breakpoint=False):
        """实出口出料并接管，回原据 [班, 片, 读值, 实出口, 递增出料序]。

        breakpoint=True 模拟出料失答：片离槽、原据以断点态落站库，未释放。
        已接管片（含断点）同参重领返回同一原据、不增序；采号冲突抛异常。
        """
        with self._lock:
            info = self._shift(shift)
            piece = _is_str(piece, "片号")
            sample_no = _is_str(sample_no, "采号")
            row = None
            try:
                row = self._piece(shift, piece)
            except StationError:
                row = None

            if row is not None:
                (_p, _r, _pp, state, seq, actual_port, old_sample,
                 _fail, raw_json) = row
                if state in (_TAKEN, _PENDING, _DONE):
                    # 重开认回：同参复领原据，不得重出；异参/重投冲突
                    if old_sample != sample_no:
                        raise StationError(
                            "接管采号冲突: 已存 %r 新参 %r（重投不得重出或"
                            "释放后继件）" % (old_sample, sample_no)
                        )
                    return json.loads(raw_json)
                # state == queued：必须是队首才准出料
                head = self._head(shift)
                if head is None or head[0] != piece:
                    raise StationError("片 %r 不在队首，禁止出料" % piece)
            else:
                head = self._head(shift)
                if head is None or head[0] != piece:
                    raise StationError("未知领取：片 %r 不在班 %r 槽内" % (piece, shift))

            # 采号全局唯一（同片重领已在上面放行）
            conflict = self._st.execute(
                "SELECT 1 FROM piece WHERE sample_no=? AND piece<>? LIMIT 1",
                (sample_no, piece),
            ).fetchone()
            if conflict is not None:
                raise StationError("采号 %r 已被其它片占用" % sample_no)

            cur = self._st.execute(
                "SELECT COALESCE(MAX(seq),0) FROM piece WHERE shift=?", (shift,)
            )
            seq = cur.fetchone()[0] + 1
            queued = self._st.execute(
                "SELECT reading, planned_port FROM piece "
                "WHERE shift=? AND piece=?",
                (shift, piece),
            ).fetchone()
            reading, actual_port = queued[0], queued[1]
            if actual_port not in info["ports"]:
                raise StationError("实出口数据损坏: %r" % actual_port)
            state = _PENDING if breakpoint else _TAKEN
            raw = [shift, piece, reading, actual_port, seq]
            self._st.execute(
                "UPDATE piece SET state=?, seq=?, actual_port=?, sample_no=?, "
                "fail=?, raw_json=? WHERE shift=? AND piece=?",
                (state, seq, actual_port, sample_no, 1 if breakpoint else 0,
                 json.dumps(raw, ensure_ascii=False), shift, piece),
            )
            self._st.commit()
            return list(raw)

    # ---- 实收 -----------------------------------------------------------

    def accept(self, shift, port, piece, fail=False):
        """实出口收已接管片，回 [原据, 版, 定标值, 首执]。

        - 仅实出口可收，每片一次，同值各收；
        - fail=True 在实收提交前中断，无新增，片保持接管态可续收；
        - 首执：该口在本班第一条成功提交的实收为 True，其后为 False。
        """
        with self._lock:
            info = self._shift(shift)
            port = _is_str(port, "收口")
            if port not in info["ports"]:
                raise StationError("收口 %r 不属于本班两口 %r" % (port, info["ports"]))
            (_p, reading, _pp, state, seq, actual_port, sample_no,
             _fail, raw_json) = self._piece(shift, piece)

            if state == _DONE:
                raise StationError("片 %r 已实收，每片仅收一次" % piece)
            if state not in (_TAKEN, _PENDING):
                raise StationError("片 %r 尚未接管，实出口不得收" % piece)
            if port != actual_port:
                raise StationError(
                    "仅实出口可收: 片 %r 实出口 %r，收口 %r 拒收"
                    % (piece, actual_port, port)
                )

            # fail：全部校验通过后、提交前中断，无新增
            if fail:
                raise StationError(
                    "fail 中断：片 %r 实收提交前中断，无新增，可续收" % piece
                )

            existed = self._st.execute(
                "SELECT 1 FROM receipt WHERE shift=? AND port=? LIMIT 1",
                (shift, port),
            ).fetchone()
            first = existed is None
            calibrated = str(
                _frac(reading) * _frac(info["mult"]) + _frac(info["offset"])
            )
            raw = json.loads(raw_json)
            self._st.execute(
                "INSERT INTO receipt(shift, piece, sample_no, port, seq, reading, "
                "calibrated, version, first, fail, raw_json) "
                "VALUES(?,?,?,?,?,?,?,?,?,0,?)",
                (shift, piece, sample_no, port, seq, reading,
                 calibrated, info["version"], 1 if first else 0,
                 json.dumps(raw, ensure_ascii=False)),
            )
            self._st.execute(
                "UPDATE piece SET state=? WHERE shift=? AND piece=?",
                (_DONE, shift, piece),
            )
            self._st.commit()
            return [raw, info["version"], calibrated, first]

    # ---- 封口报告 -------------------------------------------------------

    def seal(self, shift, port, report_no):
        """封口存 [实收明细按出料序, 条数, 最小, 最大]；空口极值 None。"""
        with self._lock:
            info = self._shift(shift)
            port = _is_str(port, "封口口")
            if port not in info["ports"]:
                raise StationError("封口口 %r 不属于本班两口 %r" % (port, info["ports"]))
            report_no = _is_str(report_no, "报号")

            existed = self._st.execute(
                "SELECT shift, port, details, cnt, minv, maxv FROM report "
                "WHERE report_no=?",
                (report_no,),
            ).fetchone()
            if existed is not None:
                if existed[0] != shift or existed[1] != port:
                    raise StationError(
                        "报号 %r 已存在且参数冲突" % report_no
                    )
                return [json.loads(existed[2]), existed[3], existed[4], existed[5]]

            rows = self._st.execute(
                "SELECT raw_json, calibrated FROM receipt "
                "WHERE shift=? AND port=? ORDER BY seq",
                (shift, port),
            ).fetchall()
            details = [json.loads(r[0]) for r in rows]
            cnt = len(rows)
            if cnt == 0:
                minv = maxv = None
            else:
                vals = [_frac(r[1]) for r in rows]
                minv = str(min(vals))
                maxv = str(max(vals))
            self._st.execute(
                "INSERT INTO report(report_no, shift, port, details, cnt, minv, maxv) "
                "VALUES(?,?,?,?,?,?,?)",
                (report_no, shift, port,
                 json.dumps(details, ensure_ascii=False), cnt, minv, maxv),
            )
            self._st.commit()
            return [details, cnt, minv, maxv]

    # ---- 存值领取 -------------------------------------------------------

    def get(self, kind, no):
        """get('report', 报号) / get('receipt', 采号)；未知号为 None。"""
        with self._lock:
            if kind not in ("report", "receipt"):
                raise StationError("get 种类仅支持 report 或 receipt: %r" % kind)
            no = _is_str(no, "号")
            if kind == "report":
                row = self._st.execute(
                    "SELECT details, cnt, minv, maxv FROM report WHERE report_no=?",
                    (no,),
                ).fetchone()
                if row is None:
                    return None
                return [json.loads(row[0]), row[1], row[2], row[3]]

            rows = self._st.execute(
                "SELECT raw_json, version, calibrated, first FROM receipt "
                "WHERE sample_no=?",
                (no,),
            ).fetchall()
            if not rows:
                return None
            if len(rows) > 1:
                raise StationError("采号 %r 命中多条回执，数据冲突" % no)
            r = rows[0]
            return [json.loads(r[0]), r[1], r[2], bool(r[3])]

    def close(self):
        with self._lock:
            self._st.close()
            if self._dev is not None:
                self._dev.close()
