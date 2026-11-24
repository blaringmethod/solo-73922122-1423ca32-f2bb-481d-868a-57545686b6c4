# -*- coding: utf-8 -*-
"""铝厂余料/再生料申报审核页 —— Flask 应用。

写操作统一在单事务内完成：先在内存中用求解器试算，成功才 COMMIT；
任何失败均 ROLLBACK，原账、签单与既有核定结果保持不变。
"""
from __future__ import annotations

import hashlib
import os
from typing import Optional, Tuple

from flask import (
    Flask, flash, redirect, render_template, request, url_for,
)
from itsdangerous import BadSignature, URLSafeSerializer

from . import csvutil, db, merge
from .solver import (
    CertRequirement, FurnaceBatchLink, FurnaceInput, SolveError, TransferInput,
    solve_allocation,
)


def create_app() -> Flask:
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024
    app.config["SECRET_KEY"] = os.environ.get("AUDIT_SECRET_KEY", "dev-audit-secret")
    # 合账试算依据令牌：试算页签发、提交时验真，防止试算期间数据被改动后误提交。
    basis_serializer = URLSafeSerializer(
        app.config["SECRET_KEY"], salt="merge-basis-v1"
    )

    conn = db.get_conn()
    db.init_db(conn)
    conn.close()

    # ------------------------------------------------------------------
    def _build_solver_inputs(conn, ledger_id):
        furnaces, batches, transfers = db.load_ledger(conn, ledger_id)
        f_inputs = [
            FurnaceInput(r["code"], r["primary_kg"], r["recycled_kg"])
            for r in furnaces
        ]
        links = [
            FurnaceBatchLink(r["furnace_code"], r["code"], r["weight_kg"])
            for r in batches
        ]
        t_inputs = [
            TransferInput(r["id"], r["src_furnace"], r["dst_furnace"], r["weight_kg"])
            for r in transfers
        ]
        reqs = [
            CertRequirement(r["batch_code"], r["min_recycled_kg"])
            for r in db.active_certificates(conn)
        ]
        return f_inputs, links, t_inputs, reqs

    def _run_and_persist(conn, ledger_id: int) -> Tuple[bool, Optional[str]]:
        """在当前事务内求解并覆盖保存该账套核定结果。"""
        f_inputs, links, t_inputs, reqs = _build_solver_inputs(conn, ledger_id)
        result = solve_allocation(f_inputs, links, t_inputs, reqs)
        if result.ok:
            db.save_solution(
                conn, ledger_id, True, None,
                batch_values=result.batch_recycled.items(),
                transfer_values=result.transfer_recycled.items(),
            )
            return True, None
        db.save_solution(conn, ledger_id, False, result.reason)
        return False, result.reason

    def _log_failure(action: str, detail: str) -> None:
        """业务事务回滚后，用独立事务补记一条失败审计日志（不影响回滚结果）。"""
        log_conn = db.get_conn()
        try:
            with db.transaction(log_conn):
                db.log_event(log_conn, action, detail, False)
        except Exception:  # pragma: no cover 日志失败不应掩盖原始错误
            pass
        finally:
            log_conn.close()

    def _merged_solver_inputs(merged: merge.MergedLedger, conn):
        """把合并账（内存）转成求解器输入；转料行号用顺序合成 id。"""
        f_inputs = [
            FurnaceInput(f.code, f.primary_kg, f.recycled_kg)
            for f in merged.furnaces
        ]
        links = [
            FurnaceBatchLink(b.furnace, b.code, b.weight_kg)
            for b in merged.batches
        ]
        t_inputs = [
            TransferInput(i + 1, t.src, t.dst, t.weight_kg)
            for i, t in enumerate(merged.transfers)
        ]
        reqs = [
            CertRequirement(r["batch_code"], r["min_recycled_kg"])
            for r in db.active_certificates(conn)
        ]
        return f_inputs, links, t_inputs, reqs

    def _load_supplement_views(conn, ledger_id: int):
        """组装首页暂存补报展示数据。"""
        views = []
        for row in db.list_supplements(conn, ledger_id):
            sf = db.supplement_furnace(conn, row["id"])
            views.append({
                "row": row,
                "primary_kg": sf["primary_kg"],
                "recycled_kg": sf["recycled_kg"],
                "batches": db.supplement_batches(conn, row["id"]),
                "transfers": db.supplement_transfers(conn, row["id"]),
            })
        return views

    # ------------------------------------------------------------------
    @app.route("/")
    def index():
        conn = db.get_conn()
        try:
            ledger_id = db.current_ledger_id(conn)
            view_ledger_id = request.args.get("ledger", type=int) or ledger_id

            ledgers = conn.execute(
                "SELECT * FROM ledgers ORDER BY id DESC LIMIT 20"
            ).fetchall()
            furnaces = batches = transfers = []
            sol_row = None
            sol_batches = sol_transfers = []
            ledger_row = None
            if view_ledger_id:
                ledger_row = conn.execute(
                    "SELECT * FROM ledgers WHERE id = ?", (view_ledger_id,)
                ).fetchone()
                if ledger_row:
                    furnaces, batches, transfers = db.load_ledger(conn, view_ledger_id)
                    sol_row = db.get_solution(conn, view_ledger_id)
                    if sol_row:
                        sol_batches, sol_transfers = db.solution_details(
                            conn, sol_row["id"]
                        )
            certs = conn.execute(
                "SELECT * FROM certificates ORDER BY active DESC, signed_at DESC, id DESC"
            ).fetchall()
            logs = conn.execute(
                "SELECT * FROM audit_log ORDER BY id DESC LIMIT 15"
            ).fetchall()

            # 现行账套的暂存补报（只对现行账套展示上传入口与合账操作）
            supp_views = (
                _load_supplement_views(conn, ledger_id) if ledger_id else []
            )
            supp_codes = {v["row"]["furnace_code"] for v in supp_views}

            cert_map = {c["batch_code"]: c for c in certs if c["active"]}
            sol_batch_map = {r["batch_code"]: r["recycled_kg"] for r in sol_batches}
            sol_transfer_map = {r["transfer_id"]: r["recycled_kg"] for r in sol_transfers}

            # 每炉物料平衡（账套原值，供页面核对“总进料=总出料”）
            balance = {}
            if ledger_row:
                for f in furnaces:
                    balance[f["code"]] = {
                        "primary": f["primary_kg"], "recycled": f["recycled_kg"],
                        "t_in": 0, "t_out": 0, "ship": 0,
                    }
                for t in transfers:
                    balance[t["src_furnace"]]["t_out"] += t["weight_kg"]
                    balance[t["dst_furnace"]]["t_in"] += t["weight_kg"]
                for b in batches:
                    balance[b["furnace_code"]]["ship"] += b["weight_kg"]
                for row in balance.values():
                    row["in_total"] = row["primary"] + row["recycled"] + row["t_in"]
                    row["out_total"] = row["ship"] + row["t_out"]

            return render_template(
                "index.html",
                ledgers=ledgers,
                ledger_row=ledger_row,
                current_ledger_id=ledger_id,
                view_ledger_id=view_ledger_id,
                furnaces=furnaces,
                batches=batches,
                transfers=transfers,
                balance=balance,
                certs=certs,
                cert_map=cert_map,
                sol_row=sol_row,
                sol_batch_map=sol_batch_map,
                sol_transfer_map=sol_transfer_map,
                logs=logs,
                supp_views=supp_views,
                supp_codes=supp_codes,
            )
        finally:
            conn.close()

    # ------------------------------------------------------------------
    @app.route("/upload", methods=["POST"])
    def upload():
        file = request.files.get("csv_file")
        replace = request.form.get("replace") == "1"
        if not file or not file.filename:
            flash("请选择要上传的 CSV 文件。", "error")
            return redirect(url_for("index"))
        try:
            content = file.read()
            parsed = csvutil.parse_ledger_csv(content)
        except csvutil.CsvError as exc:
            flash(f"CSV 校验未通过，未改动任何数据：{exc}", "error")
            _log_failure("upload", f"{file.filename if file else '?'} 校验失败：{exc}")
            return redirect(url_for("index"))

        conn = db.get_conn()
        try:
            with db.transaction(conn):
                if not replace and db.current_ledger_id(conn) is not None:
                    raise CsvConflict("当前已有生效账套。如确认换账，请勾选“替换当前账套”后重新上传。")

                cur = conn.execute(
                    "INSERT INTO ledgers(filename, imported_at, is_current, note) "
                    "VALUES (?, ?, 1, ?)",
                    (file.filename, db.utcnow_iso(),
                     f"{len(parsed.furnaces)}炉/{len(parsed.batches)}批/{len(parsed.transfers)}笔转料"),
                )
                new_id = cur.lastrowid
                conn.executemany(
                    "INSERT INTO furnaces(ledger_id, code, primary_kg, recycled_kg) "
                    "VALUES (?, ?, ?, ?)",
                    [(new_id, f["code"], f["primary_kg"], f["recycled_kg"])
                     for f in parsed.furnaces],
                )
                conn.executemany(
                    "INSERT INTO batches(ledger_id, furnace_code, code, weight_kg) "
                    "VALUES (?, ?, ?, ?)",
                    [(new_id, b["furnace"], b["code"], b["weight_kg"])
                     for b in parsed.batches],
                )
                conn.executemany(
                    "INSERT INTO transfers(ledger_id, src_furnace, dst_furnace, weight_kg) "
                    "VALUES (?, ?, ?, ?)",
                    [(new_id, t["src"], t["dst"], t["weight_kg"])
                     for t in parsed.transfers],
                )
                conn.execute("UPDATE ledgers SET is_current = 0 WHERE id <> ?", (new_id,))

                # 内存试算：无解则整笔回滚，原账/签单/核定结果原样保留
                f_inputs, links, t_inputs, reqs = _build_solver_inputs(conn, new_id)
                result = solve_allocation(f_inputs, links, t_inputs, reqs)
                if not result.ok:
                    raise CsvConflict(result.reason or "新账无可行解。")
                db.save_solution(
                    conn, new_id, True, None,
                    batch_values=result.batch_recycled.items(),
                    transfer_values=result.transfer_recycled.items(),
                )
                db.log_event(
                    conn, "upload",
                    f"导入 {file.filename}（账套#{new_id}）并核定成功", True,
                )
            flash(
                f"账套已导入并核定：{len(parsed.furnaces)} 座炉、"
                f"{len(parsed.batches)} 个出货批、{len(parsed.transfers)} 笔转料。",
                "ok",
            )
            return redirect(url_for("index", ledger=new_id))
        except CsvConflict as exc:
            flash(f"提交失败，原账、签单与核定结果均保留：{exc}", "error")
            _log_failure("upload", f"{file.filename} 被拒（回滚）：{exc}")
            return redirect(url_for("index"))
        except SolveError as exc:
            flash(f"提交失败，数据未改动：{exc}", "error")
            _log_failure("upload", f"{file.filename} 数据错误（回滚）：{exc}")
            return redirect(url_for("index"))
        finally:
            conn.close()

    # ------------------------------------------------------------------
    @app.route("/certificates/sign", methods=["POST"])
    def sign_cert():
        batch = (request.form.get("batch_code") or "").strip()
        raw_min = (request.form.get("min_recycled_kg") or "").strip()
        if not batch:
            flash("签发失败：出货批号不能为空。", "error")
            return redirect(url_for("index"))
        try:
            min_kg = int(raw_min)
            if min_kg <= 0:
                raise ValueError
        except ValueError:
            flash("签发失败：最低再生料重量必须是正整数公斤。", "error")
            return redirect(url_for("index"))

        conn = db.get_conn()
        try:
            with db.transaction(conn):
                cur_lid = db.current_ledger_id(conn)
                if cur_lid is None:
                    raise CsvConflict("尚未导入任何账套，无法签发。")
                existing = conn.execute(
                    "SELECT id FROM certificates WHERE batch_code = ? AND active = 1",
                    (batch,),
                ).fetchone()
                if existing:
                    raise CsvConflict(f"出货批 {batch} 已存在有效签单，每批最多一张；请先撤销原签单。")
                # 批号必须在当前账套内
                row = conn.execute(
                    "SELECT weight_kg FROM batches WHERE ledger_id = ? AND code = ?",
                    (cur_lid, batch),
                ).fetchone()
                if not row:
                    raise CsvConflict(f"当前账套不存在出货批 {batch}。")
                if min_kg > row["weight_kg"]:
                    raise CsvConflict(
                        f"批 {batch} 实称仅 {row['weight_kg']} kg，"
                        f"不能签发高于实称重量的最低再生料 {min_kg} kg。"
                    )
                conn.execute(
                    "INSERT INTO certificates(batch_code, min_recycled_kg, active, signed_at) "
                    "VALUES (?, ?, 1, ?)",
                    (batch, min_kg, db.utcnow_iso()),
                )
                ok, reason = _run_and_persist(conn, cur_lid)
                if not ok:
                    raise CsvConflict(reason or "加入该签单后无可行解。")
                db.log_event(conn, "sign", f"批 {batch} 签发最低再生料 {min_kg} kg", True)
            flash(f"已为批 {batch} 签发最低再生料 {min_kg} kg，并重新核定成功。", "ok")
            return redirect(url_for("index", ledger=cur_lid))
        except CsvConflict as exc:
            flash(f"签发失败，签单与核定结果保持原状：{exc}", "error")
            _log_failure("sign", f"批 {batch} 申请 {min_kg} kg 被拒（回滚）：{exc}")
            return redirect(url_for("index"))
        finally:
            conn.close()

    # ------------------------------------------------------------------
    @app.route("/certificates/revoke", methods=["POST"])
    def revoke_cert():
        cert_id = request.form.get("cert_id", type=int)
        conn = db.get_conn()
        try:
            with db.transaction(conn):
                cur_lid = db.current_ledger_id(conn)
                cert = conn.execute(
                    "SELECT * FROM certificates WHERE id = ? AND active = 1",
                    (cert_id,),
                ).fetchone()
                if not cert:
                    raise CsvConflict("签单不存在或已撤销。")
                conn.execute(
                    "UPDATE certificates SET active = 0, revoked_at = ? WHERE id = ?",
                    (db.utcnow_iso(), cert_id),
                )
                if cur_lid is not None:
                    ok, reason = _run_and_persist(conn, cur_lid)
                    if not ok:  # 撤销只会放松约束，理论上不会发生
                        raise CsvConflict(reason or "撤销后重核异常。")
                db.log_event(
                    conn, "revoke",
                    f"撤销批 {cert['batch_code']} 的签单（原下限 {cert['min_recycled_kg']} kg）",
                    True,
                )
            flash(f"已撤销批 {cert['batch_code']} 的有效签单，并重新核定。", "ok")
            return redirect(url_for("index", ledger=cur_lid))
        except CsvConflict as exc:
            flash(f"撤销失败，数据未改动：{exc}", "error")
            _log_failure("revoke", f"签单#{cert_id} 撤销被拒（回滚）：{exc}")
            return redirect(url_for("index"))
        finally:
            conn.close()

    # ------------------------------------------------------------------
    @app.route("/recheck", methods=["POST"])
    def recheck():
        conn = db.get_conn()
        try:
            with db.transaction(conn):
                cur_lid = db.current_ledger_id(conn)
                if cur_lid is None:
                    raise CsvConflict("尚未导入任何账套，无法重核。")
                ok, reason = _run_and_persist(conn, cur_lid)
                if not ok:
                    raise CsvConflict(reason or "重核无可行解。")
                db.log_event(conn, "recheck", f"账套#{cur_lid} 重核成功", True)
            flash("已按当前账套与全部有效签单重新核定，出货与回炉再生料重量已更新。", "ok")
            return redirect(url_for("index", ledger=cur_lid))
        except CsvConflict as exc:
            flash(f"重核失败，原有核定结果保留：{exc}", "error")
            _log_failure("recheck", f"账套重核被拒（回滚）：{exc}")
            return redirect(url_for("index"))
        finally:
            conn.close()

    # ------------------------------------------------------------------
    @app.route("/supplements/upload", methods=["POST"])
    def upload_supplement():
        """分炉补报上传：先暂存；同账套同炉重传以新文件整份替换。"""
        furnace_code = (request.form.get("furnace_code") or "").strip()
        file = request.files.get("csv_file")
        if not furnace_code:
            flash("补报失败：请先选择炉号。", "error")
            return redirect(url_for("index"))
        if not file or not file.filename:
            flash("补报失败：请选择要上传的 CSV 文件。", "error")
            return redirect(url_for("index"))

        conn = db.get_conn()
        try:
            cur_lid = db.current_ledger_id(conn)
            if cur_lid is None:
                flash("补报失败：尚未导入任何账套。", "error")
                return redirect(url_for("index"))
            known = {
                r["code"] for r in conn.execute(
                    "SELECT code FROM furnaces WHERE ledger_id = ?", (cur_lid,)
                ).fetchall()
            }
            if furnace_code not in known:
                flash(f"补报失败：现行账套中不存在炉号 {furnace_code}。", "error")
                return redirect(url_for("index"))
            try:
                content = file.read()
                parsed = csvutil.parse_supplement_csv(
                    content, furnace_code, known
                )
            except csvutil.CsvError as exc:
                flash(f"补报 CSV 校验未通过，暂存区未改动：{exc}", "error")
                _log_failure(
                    "supplement_upload",
                    f"账套#{cur_lid} 炉 {furnace_code} 文件 {file.filename} 校验失败：{exc}",
                )
                return redirect(url_for("index"))

            raw_text = content.decode("utf-8-sig")
            content_hash = hashlib.sha256(content).hexdigest()
            with db.transaction(conn):
                replaced = db.get_supplement(conn, cur_lid, furnace_code) is not None
                db.replace_supplement(
                    conn, cur_lid, furnace_code, file.filename,
                    raw_text, content_hash, parsed,
                )
                db.log_event(
                    conn, "supplement_upload",
                    f"账套#{cur_lid} 炉 {furnace_code} "
                    f"{'重传替换' if replaced else '新增'}补报 {file.filename}"
                    f"（{len(parsed.batches)} 批/{len(parsed.transfers)} 笔转料，暂存）",
                    True,
                )
            flash(
                f"炉 {furnace_code} 的补报已暂存"
                f"（{'已按新文件替换旧补报' if replaced else '新增'}）："
                f"{len(parsed.batches)} 个出货批、{len(parsed.transfers)} 笔转料。"
                "合账前可继续重传或删除。",
                "ok",
            )
            return redirect(url_for("index"))
        finally:
            conn.close()

    # ------------------------------------------------------------------
    @app.route("/supplements/delete", methods=["POST"])
    def delete_supplement():
        """删除某炉暂存补报（不影响原账套与已生效数据）。"""
        furnace_code = (request.form.get("furnace_code") or "").strip()
        conn = db.get_conn()
        try:
            cur_lid = db.current_ledger_id(conn)
            if cur_lid is None:
                flash("删除失败：尚未导入任何账套。", "error")
                return redirect(url_for("index"))
            with db.transaction(conn):
                existed = db.delete_supplement(conn, cur_lid, furnace_code)
                if not existed:
                    raise CsvConflict(f"现行账套没有炉 {furnace_code} 的暂存补报。")
                db.log_event(
                    conn, "supplement_delete",
                    f"账套#{cur_lid} 炉 {furnace_code} 的暂存补报已删除", True,
                )
            flash(f"炉 {furnace_code} 的暂存补报已删除，原账套数据不受影响。", "ok")
            return redirect(url_for("index"))
        except CsvConflict as exc:
            flash(f"删除失败：{exc}", "error")
            _log_failure("supplement_delete",
                         f"账套#{cur_lid} 炉 {furnace_code} 删除被拒：{exc}")
            return redirect(url_for("index"))
        finally:
            conn.close()

    # ------------------------------------------------------------------
    @app.route("/merge/preview")
    def merge_preview():
        """合账试算页（只读）：转料核对、平衡复核、求解器预核、签发依据令牌。"""
        conn = db.get_conn()
        try:
            cur_lid = db.current_ledger_id(conn)
            if cur_lid is None:
                flash("尚未导入任何账套，无法合账。", "error")
                return redirect(url_for("index"))
            merged = merge.build_merged_ledger(conn, cur_lid)
            supp_rows = db.list_supplements(conn, cur_lid)
            if not supp_rows:
                flash("现行账套暂无任何分炉补报，无需合账。", "error")
                return redirect(url_for("index"))

            basis = merge.build_basis(conn, cur_lid)
            token = basis_serializer.dumps(basis)
            balance_tbl = merge.merged_balance_table(merged)

            # 用全部有效签单试跑求解器（试算不写库；期间其他人仍可操作）
            trial_ok, trial_reason = True, None
            trial_batch_map, trial_transfer_map = {}, {}
            if merged.ok:
                f_inputs, links, t_inputs, reqs = _merged_solver_inputs(merged, conn)
                try:
                    result = solve_allocation(f_inputs, links, t_inputs, reqs)
                    trial_ok = result.ok
                    trial_reason = result.reason
                    if result.ok:
                        trial_batch_map = result.batch_recycled
                        # 合成转料 id 从 1 开始，与页面 enumerate 对齐
                        trial_transfer_map = result.transfer_recycled
                except SolveError as exc:
                    trial_ok, trial_reason = False, str(exc)

            ledger_row = conn.execute(
                "SELECT * FROM ledgers WHERE id = ?", (cur_lid,)
            ).fetchone()
            return render_template(
                "merge.html",
                ledger_row=ledger_row,
                merged=merged,
                balance_tbl=balance_tbl,
                basis=basis,
                token=token,
                trial_ok=trial_ok,
                trial_reason=trial_reason,
                trial_batch_map=trial_batch_map,
                trial_transfer_map=trial_transfer_map,
            )
        finally:
            conn.close()

    # ------------------------------------------------------------------
    @app.route("/merge/commit", methods=["POST"])
    def merge_commit():
        """合账提交：依据未变 + 核对通过 + 重核可行才整体生效，否则退回。"""
        token = request.form.get("basis_token") or ""
        try:
            basis = basis_serializer.loads(token)
        except (BadSignature, TypeError, ValueError):
            flash("合账失败：试算依据令牌无效或已过期，请重新打开试算页。", "error")
            _log_failure("merge_commit", "依据令牌无效")
            return redirect(url_for("index"))

        conn = db.get_conn()
        try:
            cur_lid = db.current_ledger_id(conn)
            with db.transaction(conn):
                # 1) 试算依据复核：现行账套 / 补报 / 有效签单任一不同即退回
                drift = merge.basis_matches(conn, cur_lid, basis)
                if drift:
                    raise CsvConflict(drift)

                # 2) 重新取数构建合并账（不用试算页的快照），再跑全部核对
                merged = merge.build_merged_ledger(conn, cur_lid)
                if merged.issues:
                    detail = "；".join(i.detail for i in merged.issues)
                    raise CsvConflict("转料双向核对未通过：" + detail)
                if merged.balance_problems:
                    raise CsvConflict(
                        "合并账物料平衡不满足："
                        + "；".join(merged.balance_problems) + "。"
                    )

                # 3) 用全部有效签单重核
                f_inputs, links, t_inputs, reqs = _merged_solver_inputs(
                    merged, conn
                )
                result = solve_allocation(f_inputs, links, t_inputs, reqs)
                if not result.ok:
                    raise CsvConflict(result.reason or "合并账无可行解。")

                # 4) 落库为新账套（原账套与补报保留；新账套从无补报开始）
                cur = conn.execute(
                    "INSERT INTO ledgers(filename, imported_at, is_current, note) "
                    "VALUES (?, ?, 1, ?)",
                    (
                        f"merge-of-ledger-{cur_lid}.csv",
                        db.utcnow_iso(),
                        f"合账：基于账套#{cur_lid}，补报炉 "
                        + "、".join(merged.supplemented_codes),
                    ),
                )
                new_id = cur.lastrowid
                conn.executemany(
                    "INSERT INTO furnaces(ledger_id, code, primary_kg, recycled_kg) "
                    "VALUES (?, ?, ?, ?)",
                    [(new_id, f.code, f.primary_kg, f.recycled_kg)
                     for f in merged.furnaces],
                )
                conn.executemany(
                    "INSERT INTO batches(ledger_id, furnace_code, code, weight_kg) "
                    "VALUES (?, ?, ?, ?)",
                    [(new_id, b.furnace, b.code, b.weight_kg)
                     for b in merged.batches],
                )
                # 转料逐行插入并记录新行号，供核定明细引用
                transfer_id_map = {}
                for i, t in enumerate(merged.transfers, start=1):
                    tr = conn.execute(
                        "INSERT INTO transfers(ledger_id, src_furnace, dst_furnace, "
                        "weight_kg) VALUES (?, ?, ?, ?)",
                        (new_id, t.src, t.dst, t.weight_kg),
                    )
                    transfer_id_map[i] = tr.lastrowid
                conn.execute(
                    "UPDATE ledgers SET is_current = 0 WHERE id <> ?", (new_id,)
                )

                # 5) 核定结果与新账一同生效（合成 id 已与求解输出对齐）
                db.save_solution(
                    conn, new_id, True, None,
                    batch_values=result.batch_recycled.items(),
                    transfer_values=(
                        (transfer_id_map[tid], val)
                        for tid, val in result.transfer_recycled.items()
                    ),
                )
                db.log_event(
                    conn, "merge_commit",
                    f"账套#{cur_lid} 合账生效为新账套#{new_id}（补报炉 "
                    f"{len(merged.supplemented_codes)} 座），核定成功",
                    True,
                )
            flash(
                f"合账完成：新账套 #{new_id} 与核定结果已一同生效；"
                f"原账套 #{cur_lid} 及其补报均保留可查。",
                "ok",
            )
            return redirect(url_for("index", ledger=new_id))
        except CsvConflict as exc:
            flash(f"本次合账已退回，补报、签单与已生效数据全部保留：{exc}", "error")
            _log_failure("merge_commit", f"账套#{cur_lid} 合账被拒（回滚）：{exc}")
            return redirect(url_for("index"))
        except SolveError as exc:
            flash(f"合账失败，数据未改动：{exc}", "error")
            _log_failure("merge_commit", f"账套#{cur_lid} 数据错误（回滚）：{exc}")
            return redirect(url_for("index"))
        except Exception as exc:  # 兜底：任何意外错误也必须回滚并提示，不留 500
            app.logger.exception("merge_commit unexpected error")
            flash(f"合账失败，数据未改动：{exc}", "error")
            _log_failure("merge_commit", f"账套#{cur_lid} 异常回滚：{exc}")
            return redirect(url_for("index"))
        finally:
            conn.close()

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    @app.errorhandler(413)
    def too_large(_e):
        flash("上传文件超过 8MB 限制。", "error")
        return redirect(url_for("index"))

    return app


class CsvConflict(Exception):
    """事务内业务冲突，触发回滚并向页面提示原因。"""
