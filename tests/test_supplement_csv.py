# -*- coding: utf-8 -*-
"""分炉补报 CSV 解析测试：沿用 6 列格式、单炉口径、进出转料与单炉平衡。"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.csvutil import CsvError, parse_supplement_csv  # noqa: E402

HEADER = "record_type,furnace,ref,primary_kg,recycled_kg,weight_kg\n"
KNOWN = {"F1", "F2"}
results = []


def check(name, cond):
    results.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name)


def expect_error(name, text, needle, furnace="F1", known=None):
    try:
        parse_supplement_csv(text.encode("utf-8"), furnace, known or KNOWN)
    except CsvError as exc:
        check(name, needle in str(exc))
        return
    check(name, False)


# 1. 正常补报：含本炉出货、转出与对端转入；平衡：进 100+20=120，出 90+30=120
ok = HEADER + (
    "furnace,F1,,0,100,\n"
    "batch,F1,B1,,,90\n"
    "transfer,F1,F2,,,30\n"
    "transfer,F2,F1,,,20\n"
)
p = parse_supplement_csv(ok.encode("utf-8"), "F1", KNOWN)
check("parse_ok", p.furnace["code"] == "F1" and p.furnace["recycled_kg"] == 100)
check("parse_counts", len(p.batches) == 1 and len(p.transfers) == 2)
check("parse_incoming_transfer", p.transfers[1]["src"] == "F2" and p.transfers[1]["dst"] == "F1")

# 2. furnace 行炉号与所选炉不一致
expect_error("furnace_mismatch",
             HEADER + "furnace,F2,,0,10,\nbatch,F2,B1,,,10\n",
             "补报炉号必须为 F1")

# 3. 缺 furnace 行
expect_error("missing_furnace_row",
             HEADER + "batch,F1,B1,,,10\n", "缺少炉 F1 的 furnace 行")

# 4. 两条 furnace 行
expect_error("two_furnace_rows",
             HEADER + "furnace,F1,,0,10,\nfurnace,F1,,0,10,\n", "只能包含一条 furnace")

# 5. batch 归属炉不是本炉
expect_error("batch_other_furnace",
             HEADER + "furnace,F1,,0,10,\nbatch,F2,B1,,,10\n", "归属炉 F2 与所选补报炉 F1")

# 6. 转料与本炉无关
expect_error("transfer_unrelated",
             HEADER + "furnace,F1,,0,10,\ntransfer,F2,F2,,,10\n".replace("F2,F2", "F2,F9"),
             "与补报炉 F1 无关")

# 7. 对端炉不在现行账套
expect_error("unknown_peer",
             HEADER + "furnace,F1,,0,10,\ntransfer,F1,F9,,,10\n", "不在现行账套")

# 8. 批号重复
expect_error("dup_batch",
             HEADER + "furnace,F1,,0,20,\nbatch,F1,B1,,,10\nbatch,F1,B1,,,10\n",
             "出货批号 B1 重复")

# 9. 单炉不平衡：进 100，出 110
expect_error("furnace_unbalanced",
             HEADER + "furnace,F1,,0,100,\nbatch,F1,B1,,,110\n", "物料平衡")

# 10. 含转入时的平衡：进 100+40=140，出 140
balanced_in = HEADER + (
    "furnace,F1,,0,100,\n"
    "batch,F1,B1,,,140\n"
    "transfer,F2,F1,,,40\n"
)
p2 = parse_supplement_csv(balanced_in.encode("utf-8"), "F1", KNOWN)
check("incoming_balanced", len(p2.transfers) == 1)

# 11. 空文件 / 非 UTF-8 / 缺列
expect_error("empty", "", "为空")
expect_error("missing_col", "record_type,furnace\nfurnace,F1\n", "缺少必需列")

# 12. BOM + CRLF
p3 = parse_supplement_csv(("﻿" + ok).replace("\n", "\r\n").encode("utf-8"), "F1", KNOWN)
check("bom_crlf", p3.furnace["code"] == "F1")

# 13. 留空外购列按 0；平衡 进 0+20=20 出 20
zero_input = HEADER + (
    "furnace,F1,,,\n"
    "batch,F1,B1,,,20\n"
    "transfer,F2,F1,,,20\n"
)
p4 = parse_supplement_csv(zero_input.encode("utf-8"), "F1", KNOWN)
check("blank_input_zero", p4.furnace["primary_kg"] == 0 and p4.furnace["recycled_kg"] == 0)

failed = [n for n, ok_ in results if not ok_]
print(f"\n{len(results) - len(failed)}/{len(results)} passed")
sys.exit(1 if failed else 0)
