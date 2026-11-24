# -*- coding: utf-8 -*-
"""端到端测试：分炉补报暂存、转料双向核对、合账试算/提交、依据漂移与回滚。"""
import io
import os
import re
import sys
import tempfile

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

_tmpdir = tempfile.mkdtemp(prefix="audit-merge-")
os.environ["AUDIT_DB_PATH"] = os.path.join(_tmpdir, "test.db")
os.environ["AUDIT_SECRET_KEY"] = "test-secret"

from app import create_app  # noqa: E402
from app import db, merge  # noqa: E402

app = create_app()
app.config["TESTING"] = True
client = app.test_client()

# 原账（每炉平衡）：
# F1 进 370 = B1 300 + B2 50 + 转出 20
# F2 进 50+50+20 = 120 = B3 90 + 转出 30
# F3 进 100+50+30 = 180 = B4 140 + B5 40
BASE_CSV = (
    "record_type,furnace,ref,primary_kg,recycled_kg,weight_kg\n"
    "furnace,F1,,0,370,\n"
    "furnace,F2,,50,50,\n"
    "furnace,F3,,100,50,\n"
    "batch,F1,B1,,,300\n"
    "batch,F1,B2,,,50\n"
    "batch,F2,B3,,,90\n"
    "batch,F3,B4,,,140\n"
    "batch,F3,B5,,,40\n"
    "transfer,F1,F2,,,20\n"
    "transfer,F2,F3,,,30\n"
)

# F1 补报：再生 370->360、B2 50->40，转料不变；进 360 = 300+40+20
SUPP_F1 = (
    "record_type,furnace,ref,primary_kg,recycled_kg,weight_kg\n"
    "furnace,F1,,0,360,\n"
    "batch,F1,B1,,,300\n"
    "batch,F1,B2,,,40\n"
    "transfer,F1,F2,,,20\n"
)
# F2 补报：含进出两侧转料，数字不变
SUPP_F2 = (
    "record_type,furnace,ref,primary_kg,recycled_kg,weight_kg\n"
    "furnace,F2,,50,50,\n"
    "batch,F2,B3,,,90\n"
    "transfer,F1,F2,,,20\n"
    "transfer,F2,F3,,,30\n"
)
# F3 补报：数字不变，含 F2->F3 转入
SUPP_F3 = (
    "record_type,furnace,ref,primary_kg,recycled_kg,weight_kg\n"
    "furnace,F3,,100,50,\n"
    "batch,F3,B4,,,140\n"
    "batch,F3,B5,,,40\n"
    "transfer,F2,F3,,,30\n"
)
# F3 补报（不符版）：转入 25、B4 135 自身配平；与 F2 所报 30 冲突
SUPP_F3_MISMATCH = SUPP_F3.replace("transfer,F2,F3,,,30", "transfer,F2,F3,,,25") \
    .replace("batch,F3,B4,,,140", "batch,F3,B4,,,135")

results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))
    print(("PASS " if cond else "FAIL ") + name + (f"  {detail}" if detail and not cond else ""))


def upload_supp(furnace, text, filename="s.csv"):
    return client.post(
        "/supplements/upload",
        data={"furnace_code": furnace,
              "csv_file": (io.BytesIO(text.encode("utf-8")), filename)},
        content_type="multipart/form-data", follow_redirects=True,
    )


def sql(query, args=()):
    conn = db.get_conn()
    try:
        return [dict(r) for r in conn.execute(query, args).fetchall()]
    finally:
        conn.close()


def preview_token(body_text):
    m = re.search(r'name="basis_token" value="([^"]+)"', body_text)
    return m.group(1) if m else None


# --- 0. 导入原账 ---
r = client.post(
    "/upload",
    data={"csv_file": (io.BytesIO(BASE_CSV.encode("utf-8")), "base.csv"), "replace": "1"},
    content_type="multipart/form-data", follow_redirects=True,
)
check("base_upload", r.status_code == 200 and b"error" not in r.data[:300])
base_lid = sql("SELECT id FROM ledgers WHERE is_current=1")[0]["id"]

# --- 1. 无账套/错误炉号/坏 CSV 的补报被拒 ---
r = upload_supp("F9", SUPP_F1)
check("unknown_furnace_rejected", "不存在炉号".encode() in r.data)
r = upload_supp("F1", SUPP_F1.replace("furnace,F1", "furnace,F2"))
check("furnace_mismatch_rejected", "补报炉号必须为 F1".encode() in r.data)
r = upload_supp("F1", SUPP_F1.replace("batch,F1,B2,,,40", "batch,F1,B2,,,41"))
check("unbalanced_supp_rejected", "物料平衡".encode() in r.data)
check("no_supp_after_rejects",
      sql("SELECT COUNT(*) c FROM supplements")[0]["c"] == 0)

# --- 2. F1 补报暂存成功，重传以新文件为准 ---
r = upload_supp("F1", SUPP_F1)
check("f1_staged", b"error" not in r.data[:400])
check("one_supp", sql("SELECT COUNT(*) c FROM supplements")[0]["c"] == 1)
r = upload_supp("F1", SUPP_F1, filename="s2.csv")
check("f1_reupload_ok", b"error" not in r.data[:400])
row = sql("SELECT * FROM supplements WHERE ledger_id=? AND furnace_code='F1'",
          (base_lid,))
check("reupload_replaces", len(row) == 1 and row[0]["filename"] == "s2.csv")

# --- 3. F2 补报后试算：F2↔F3 缺报，F1↔F2 已两端不报问题 ---
r = upload_supp("F2", SUPP_F2)
check("f2_staged", b"error" not in r.data[:400])
r = client.get("/merge/preview")
body = r.data.decode()
check("preview_200", r.status_code == 200)
check("missing_f3_flagged", "F2 ↔ F3" in body and "缺报" in body)
check("f1f2_no_issue", "F1 ↔ F2" not in body.split("②")[1].split("③")[0]
      or "缺报" not in body.split("F1 ↔ F2")[0][-200:])
check("commit_disabled_on_issue", preview_token(body) is None)

# --- 4. 缺报状态下强行提交（伪造不了 token；直接验证引擎层拦截）---
conn = db.get_conn()
merged = merge.build_merged_ledger(conn, base_lid)
check("engine_flags_missing",
      any(i.kind == "missing" and i.pair == ("F2", "F3") for i in merged.issues))
conn.close()

# --- 5. F3 重量不符版：mismatch 列出炉对与方向重量 ---
r = upload_supp("F3", SUPP_F3_MISMATCH)
check("f3_mismatch_staged", b"error" not in r.data[:400])
r = client.get("/merge/preview")
body = r.data.decode()
check("mismatch_flagged", "F2 ↔ F3" in body and "不符" in body
      and "25" in body and "30" in body)
check("mismatch_blocks_commit", preview_token(body) is None)

# --- 6. 换回一致的 F3：核对/平衡/试算全过，出现提交令牌 ---
r = upload_supp("F3", SUPP_F3)
check("f3_fixed", b"error" not in r.data[:400])
r = client.get("/merge/preview")
body = r.data.decode()
check("cross_check_pass", "转料核对通过" in body)
check("balance_pass", "平衡复核通过" in body)
check("trial_pass", "试算通过" in body)
token = preview_token(body)
check("token_issued", bool(token))

# --- 7. 合并账内容正确：补报炉替换、未补报场景沿用（这里全部补报，验证替换值）---
conn = db.get_conn()
merged = merge.build_merged_ledger(conn, base_lid)
check("merged_no_issue", merged.ok)
f1 = next(f for f in merged.furnaces if f.code == "F1")
check("merged_f1_replaced", f1.recycled_kg == 360 and f1.supplemented)
b2 = next(b for b in merged.batches if b.code == "B2")
check("merged_b2_replaced", b2.weight_kg == 40)
# 转料双向各一份、无重复
pairs = sorted((t.src, t.dst) for t in merged.transfers)
check("merged_transfers_dedup", pairs == [("F1", "F2"), ("F2", "F3")], str(pairs))
check("merged_balance_ok", not merged.balance_problems)
conn.close()

# --- 8. 试算期间签单变动 -> 提交退回，不产生新账套 ---
client.post("/certificates/sign",
            data={"batch_code": "B1", "min_recycled_kg": "100"},
            follow_redirects=True)
r = client.post("/merge/commit", data={"basis_token": token}, follow_redirects=True)
check("cert_drift_rejected", "有效签单发生变动".encode() in r.data)
check("no_new_ledger_on_drift",
      sql("SELECT COUNT(*) c FROM ledgers")[0]["c"] == 1)

# --- 9. 试算期间补报变动 -> 提交退回 ---
cert = sql("SELECT id FROM certificates WHERE active=1")[0]["id"]
client.post("/certificates/revoke", data={"cert_id": cert}, follow_redirects=True)
r = client.get("/merge/preview")
token2 = preview_token(r.data.decode())
# 实质变化的 F1（B2 40->30、再生 360->350，自身仍平衡）
f1_changed = SUPP_F1.replace("furnace,F1,,0,360,", "furnace,F1,,0,350,") \
    .replace("batch,F1,B2,,,40", "batch,F1,B2,,,30")
upload_supp("F1", f1_changed)
r = client.post("/merge/commit", data={"basis_token": token2}, follow_redirects=True)
check("supp_drift_rejected", "补报内容发生变动".encode() in r.data)
check("still_one_ledger", sql("SELECT COUNT(*) c FROM ledgers")[0]["c"] == 1)
# 恢复
upload_supp("F1", SUPP_F1)

# --- 10. 试算期间换账 -> 提交退回（依据 ledger_id 不匹配）---
# 先造第二个账套并换入：用一份无签单冲突的新账套
ALT_CSV = (
    "record_type,furnace,ref,primary_kg,recycled_kg,weight_kg\n"
    "furnace,F1,,0,100,\n"
    "batch,F1,BX,,,100\n"
)
r = client.post(
    "/upload",
    data={"csv_file": (io.BytesIO(ALT_CSV.encode("utf-8")), "alt.csv"), "replace": "1"},
    content_type="multipart/form-data", follow_redirects=True,
)
alt_lid = sql("SELECT id FROM ledgers WHERE is_current=1")[0]["id"]
check("alt_ledger_switched", alt_lid != base_lid)
r = client.post("/merge/commit", data={"basis_token": token2}, follow_redirects=True)
check("ledger_drift_rejected", "期间发生换账".encode() in r.data)
# 切回原账套继续后续合账
conn = db.get_conn()
with db.transaction(conn):
    conn.execute("UPDATE ledgers SET is_current=1 WHERE id=?", (base_lid,))
    conn.execute("UPDATE ledgers SET is_current=0 WHERE id=?", (alt_lid,))
conn.close()

# --- 11. 无效令牌 ---
r = client.post("/merge/commit", data={"basis_token": "not-a-token"},
                follow_redirects=True)
check("bad_token_rejected", "令牌无效".encode() in r.data)

# --- 12. 正式合账成功：新账套与核定一同生效，原账/补报保留 ---
r = client.get("/merge/preview")
token3 = preview_token(r.data.decode())
check("fresh_token", bool(token3))
r = client.post("/merge/commit", data={"basis_token": token3}, follow_redirects=True)
check("commit_ok", r.status_code == 200 and b"error" not in r.data[:500])
new_lid = sql("SELECT id FROM ledgers WHERE is_current=1")[0]["id"]
check("new_ledger_current", new_lid != base_lid and new_lid != alt_lid)

# 新账套数据
nf1 = sql("SELECT recycled_kg FROM furnaces WHERE ledger_id=? AND code='F1'",
          (new_lid,))[0]
nb2 = sql("SELECT weight_kg FROM batches WHERE ledger_id=? AND code='B2'",
          (new_lid,))[0]
check("new_ledger_values", nf1["recycled_kg"] == 360 and nb2["weight_kg"] == 40)
# 转料去重后恰好两笔
nt = sql("SELECT COUNT(*) c FROM transfers WHERE ledger_id=?", (new_lid,))[0]
check("new_ledger_transfer_count", nt["c"] == 2)
# 核定结果随新账生效且明细转料 id 对得上
sol = sql("SELECT * FROM solutions WHERE ledger_id=?", (new_lid,))[0]
check("solution_with_new_ledger", sol["ok"] == 1)
st = sql("SELECT COUNT(*) c FROM solution_transfers WHERE solution_id=?", (sol["id"],))[0]
check("solution_transfer_links", st["c"] == 2)
# 新账套无补报；旧账套三份补报保留
check("new_ledger_no_supp",
      sql("SELECT COUNT(*) c FROM supplements WHERE ledger_id=?", (new_lid,))[0]["c"] == 0)
check("old_supp_preserved",
      sql("SELECT COUNT(*) c FROM supplements WHERE ledger_id=?", (base_lid,))[0]["c"] == 3)
# 原账套数据未被改动
of1 = sql("SELECT recycled_kg FROM furnaces WHERE ledger_id=? AND code='F1'",
          (base_lid,))[0]
check("old_ledger_untouched", of1["recycled_kg"] == 370)

# --- 13. 旧账套补报不适用新账套：新账套试算提示无需合账 ---
r = client.get("/merge/preview", follow_redirects=True)
check("new_ledger_no_merge", "无需合账".encode() in r.data)

# --- 14. 未补报炉沿用原账：只补 F1 时合账，F2/F3 数据等于原账 ---
# 切回原账套，删掉 F2/F3 补报
conn = db.get_conn()
with db.transaction(conn):
    conn.execute("UPDATE ledgers SET is_current=1 WHERE id=?", (base_lid,))
    conn.execute("UPDATE ledgers SET is_current=0 WHERE id=?", (new_lid,))
conn.close()
for code in ("F2", "F3"):
    client.post("/supplements/delete",
                data={"furnace_code": code}, follow_redirects=True)
# 只补 F1 时：F1↔F2 仅 F1 端报 -> 缺报，合账被引擎拦截
r = client.get("/merge/preview")
check("partial_supp_missing", "F1 ↔ F2" in r.data.decode() and "缺报" in r.data.decode())
# 删掉 F1 补报后无任何补报 -> 拒绝试算
client.post("/supplements/delete", data={"furnace_code": "F1"}, follow_redirects=True)
r = client.get("/merge/preview", follow_redirects=True)
check("empty_preview_rejected", "无需合账".encode() in r.data)
check("supp_all_deleted", sql("SELECT COUNT(*) c FROM supplements")[0]["c"] == 0)

# --- 15. 审计日志覆盖新动作且失败留痕 ---
logs = sql("SELECT action, success FROM audit_log")
actions = {l["action"] for l in logs}
check("log_new_actions",
      {"supplement_upload", "supplement_delete", "merge_commit"}.issubset(actions),
      str(actions))
check("log_merge_failures",
      any(l["action"] == "merge_commit" and l["success"] == 0 for l in logs))

# --- 16. 首页渲染补报区 ---
upload_supp("F1", SUPP_F1)
r = client.get("/")
check("page_supp_section", "分炉补报".encode() in r.data and "F1".encode() in r.data)
check("page_merge_entry", "合账试算".encode() in r.data)

failed = [n for n, ok_, _ in results if not ok_]
print(f"\n{len(results) - len(failed)}/{len(results)} passed")
if failed:
    print("FAILED:", failed)
    sys.exit(1)
