# -*- coding: utf-8 -*-
"""核验入口：任一项断言失败即以非零退出码结束。

覆盖：定标冻结、空槽/改道、出料断点失答、重开认回原据不重出、
fail 提交前中断无新增且可续收、停源续收、每片一次/同值各收、
封口极值、未知领取 None、冲突/非法/注入抛异常。
"""

import os
import sys
import tempfile
import traceback

from station import Station, StationError

failures = []


def check(name, cond):
    if cond:
        print("PASS  %s" % name)
    else:
        failures.append(name)
        print("FAIL  %s" % name)


def expect_raise(name, fn):
    try:
        fn()
    except StationError:
        print("PASS  %s（按预期抛异常）" % name)
        return
    except Exception as exc:  # noqa: BLE001
        failures.append(name)
        print("FAIL  %s（抛了非约定异常 %r）" % (name, exc))
        return
    failures.append(name)
    print("FAIL  %s（应抛异常却成功）" % name)


def main():
    tmp = tempfile.mkdtemp(prefix="coat_verify_")
    dev = os.path.join(tmp, "device.db")
    stb = os.path.join(tmp, "station.db")

    st = Station(dev, stb)

    # ---- 定标登记 ------------------------------------------------------
    st.cal("V1", "3/2", "-1/2")
    st.cal("V1", "3/2", "-1/2")  # 同参幂等
    expect_raise("同版异参定标冲突",
                 lambda: st.cal("V1", "2", "0"))
    expect_raise("非法分数字符串",
                 lambda: st.cal("VX", "abc", "0"))
    expect_raise("零分母分数",
                 lambda: st.cal("VX", "1/0", "0"))
    expect_raise("浮点参数拒绝",
                 lambda: st.cal("VX", 1.5, "0"))

    # ---- 开班冻结 ------------------------------------------------------
    expect_raise("未登记版本开班",
                 lambda: st.open("S1", "V9", "PA", "PB"))
    st.open("S1", "V1", "PA", "PB")
    st.open("S1", "V1", "PA", "PB")  # 同参重开
    expect_raise("重开参数冲突",
                 lambda: st.open("S1", "V1", "PX", "PB"))
    expect_raise("两口同名非法",
                 lambda: st.open("SX", "V1", "P", "P"))
    st.open("S2", "V1", "PA", "PB")  # 空口班，留做空极值

    # ---- 进料/预览/改道 ------------------------------------------------
    st.load("S1", "A", 4, "PA")
    st.load("S1", "B", 2, "PA")
    st.load("S1", "C", 4, "PA")   # 与 A 同读值，同值各收
    expect_raise("读值非整数",
                 lambda: st.load("S1", "X", "4", "PA"))
    expect_raise("布尔读值拒绝",
                 lambda: st.load("S1", "X", True, "PA"))
    expect_raise("非法计划口",
                 lambda: st.load("S1", "X", 1, "PZ"))
    expect_raise("重投片号冲突（不得重出/释放后继件）",
                 lambda: st.load("S1", "A", 9, "PA"))

    check("预览队首 A", st.peek("S1") == ["A", 4, "PA"])
    st.route("S1", "PB")  # 甲口预览改道乙口
    check("改道后预览 A→PB", st.peek("S1") == ["A", 4, "PB"])
    expect_raise("改到非法口", lambda: st.route("S1", "PZ"))

    # ---- 出料断点失答 --------------------------------------------------
    raw_a = st.take("S1", "A", "sA", breakpoint=True)
    check("断点出料原据 [班,片,读值,实出口,序]",
          raw_a == ["S1", "A", 4, "PB", 1])
    check("出料后槽失答：队首已变为 B",
          st.peek("S1") == ["B", 2, "PA"])
    # 同参重领，不重出不增序
    check("断点片同参重领返回同一原据",
          st.take("S1", "A", "sA", breakpoint=True) == raw_a)
    expect_raise("重领养号冲突",
                 lambda: st.take("S1", "A", "sOTHER"))
    expect_raise("非队首禁止出料",
                 lambda: st.take("S1", "C", "sC"))
    expect_raise("未知领取",
                 lambda: st.take("S1", "NOPE", "sN"))

    # B 从甲口正常出料接管
    raw_b = st.take("S1", "B", "sB")
    check("B 出料序递增为 2", raw_b == ["S1", "B", 2, "PA", 2])

    # C 改道乙口后出料（与 A 同值）
    st.route("S1", "PB")
    check("改道队首 C→PB", st.peek("S1") == ["C", 4, "PB"])
    raw_c = st.take("S1", "C", "sC")
    check("C 出料序递增为 3", raw_c == ["S1", "C", 4, "PB", 3])

    # ---- fail 实收中断，无新增，可续收 ---------------------------------
    expect_raise("fail 于实收提交前中断",
                 lambda: st.accept("S1", "PB", "A", fail=True))
    check("fail 无新增回执", st.get("receipt", "sA") is None)
    expect_raise("仅实出口可收（A 在 PB，PA 拒收）",
                 lambda: st.accept("S1", "PA", "A"))
    res_a = st.accept("S1", "PB", "A")  # 续收成功
    check("乙收 A 定标值 4*3/2-1/2=11/2，首执 True",
          res_a == [["S1", "A", 4, "PB", 1], "V1", "11/2", True])

    res_b = st.accept("S1", "PA", "B")
    check("甲收 B 定标值 2*3/2-1/2=5/2，甲口首执 True",
          res_b == [["S1", "B", 2, "PA", 2], "V1", "5/2", True])
    expect_raise("每片仅收一次",
                 lambda: st.accept("S1", "PB", "A"))

    # 后继 D 仍在槽未释放
    st.load("S1", "D", 6, "PA")

    # ---- 停源：设备库 None，乙可停源续收 -------------------------------
    st.close()
    stopped = Station(None, stb)
    expect_raise("停源禁止进料",
                 lambda: stopped.load("S1", "E", 1, "PA"))
    expect_raise("停源禁止登记定标",
                 lambda: stopped.cal("V2", "1", "0"))
    expect_raise("停源禁止新开班次",
                 lambda: stopped.open("S9", "V1", "PA", "PB"))
    check("停源后继件 D 未释放仍在槽",
          stopped.peek("S1") == ["D", 6, "PA"])
    # 重开认回：原据可恢复，同参重领不增序
    check("重开认回 A 原据（序仍为 1，未重出）",
          stopped.take("S1", "A", "sA") == ["S1", "A", 4, "PB", 1])
    # 乙停源续收已接管片 C（与 A 同值各收）
    res_c = stopped.accept("S1", "PB", "C")
    check("同值各收：C 亦为 11/2，乙口首执 False",
          res_c == [["S1", "C", 4, "PB", 3], "V1", "11/2", False])
    expect_raise("停源下每片一次仍生效",
                 lambda: stopped.accept("S1", "PB", "C"))

    # ---- 封口与极值 ----------------------------------------------------
    rep_pb = stopped.seal("S1", "PB", "R-PB")
    check("乙口封口按出料序明细/条数/极值",
          rep_pb == [[["S1", "A", 4, "PB", 1],
                      ["S1", "C", 4, "PB", 3]], 2, "11/2", "11/2"])
    rep_pa = stopped.seal("S1", "PA", "R-PA")
    check("甲口封口 5/2",
          rep_pa == [[["S1", "B", 2, "PA", 2]], 1, "5/2", "5/2"])
    rep_empty = stopped.seal("S2", "PB", "R-EMPTY")
    check("空口条数 0、极值 None", rep_empty == [[], 0, None, None])
    check("同号同参复领报告", stopped.get("report", "R-PB") == rep_pb)
    expect_raise("同号异参冲突",
                 lambda: stopped.seal("S1", "PA", "R-PB"))

    # ---- 存值领取 / 未知 / 注入 ----------------------------------------
    check("未知报告为 None", stopped.get("report", "NOPE") is None)
    check("未知回执为 None", stopped.get("receipt", "NOPE") is None)
    check("回执领取含首执 True",
          stopped.get("receipt", "sA") ==
          [["S1", "A", 4, "PB", 1], "V1", "11/2", True])
    check("回执领取含首执 False",
          stopped.get("receipt", "sC") ==
          [["S1", "C", 4, "PB", 3], "V1", "11/2", False])
    expect_raise("get 种类非法", lambda: stopped.get("other", "x"))
    inject = "x' OR '1'='1"
    check("注入文本参数化、无意外命中", stopped.get("receipt", inject) is None)
    check("注入攻击后回执表完好",
          stopped.get("receipt", "sB") ==
          [["S1", "B", 2, "PA", 2], "V1", "5/2", True])
    expect_raise("构造站库路径非法", lambda: Station(dev, ""))

    stopped.close()

    print("-" * 60)
    if failures:
        print("核验失败 %d 项: %s" % (len(failures), ", ".join(failures)))
        return 1
    print("全部核验通过")
    return 0


if __name__ == "__main__":
    try:
        rc = main()
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        rc = 1
    sys.exit(rc)
