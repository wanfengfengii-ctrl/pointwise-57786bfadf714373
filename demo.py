# -*- coding: utf-8 -*-
"""演示入口：涂层检验站完整业务流，重点演示重开后领回报存值。

流程：定标 → 开班冻结 → 进料 → 预览改道 → 断点出料失答 →
fail 实收中断 → 续收成功 → 停源 → 重开认回原据并封口 → get 领报。
"""

import os
import sys
import tempfile

from station import Station


def main():
    tmp = tempfile.mkdtemp(prefix="coat_demo_")
    dev = os.path.join(tmp, "device.db")
    stb = os.path.join(tmp, "station.db")
    print("设备库=%s\n站库=%s\n" % (dev, stb))

    st = Station(dev, stb)

    print("== 登记定标 V1：倍率 3/2，偏置 -1/2 ==")
    st.cal("V1", "3/2", "-1/2")

    print("== 开班 S1（甲口 PA / 乙口 PB），定标版本被冻结 ==")
    st.open("S1", "V1", "PA", "PB")

    print("== 进料：A 原读 4，计划甲口 ==")
    st.load("S1", "A", 4, "PA")
    print("   peek 队首:", st.peek("S1"))

    print("== 预览甲口后改道乙口 ==")
    st.route("S1", "PB")
    print("   peek 队首:", st.peek("S1"))

    print("== A 出料断点（出料失答）：片离槽，原据落站库未释放 ==")
    raw = st.take("S1", "A", "采-A", breakpoint=True)
    print("   take 原据 [班,片,读值,实出口,出料序]:", raw)
    print("   出料后槽内 peek:", st.peek("S1"))

    print("== fail 于实收提交前中断，无新增，原槽失读但可续收 ==")
    try:
        st.accept("S1", "PB", "A", fail=True)
    except Exception as exc:  # noqa: BLE001
        print("   中断:", exc)
    print("   此时回执尚不存在:", st.get("receipt", "采-A"))

    print("== 续收成功：乙收 A，定标值 4×3/2+(-1/2) = 11/2 ==")
    res = st.accept("S1", "PB", "A")
    print("   accept 回执 [原据, 版, 定标值, 首执]:", res)

    print("== 封口乙口报告 报号 R-001 ==")
    sealed = st.seal("S1", "PB", "R-001")
    print("   封口存值 [明细, 条数, 最小, 最大]:", sealed)
    st.close()

    print("\n== 模拟停机后重开：设备库 None（停源），仅接站库 ==")
    st2 = Station(None, stb)
    print("   重开认回 A 的接管原据（同参重领，不重出、出料序仍为 1）:")
    print("  ", st2.take("S1", "A", "采-A"))
    print("   领取实收回执 采-A:")
    print("  ", st2.get("receipt", "采-A"))
    print("== 重开领报：get('report','R-001') ==")
    print("  ", st2.get("report", "R-001"))
    print("   未知号领取为 None:", st2.get("report", "不存在"))
    st2.close()
    print("\n演示完成。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
