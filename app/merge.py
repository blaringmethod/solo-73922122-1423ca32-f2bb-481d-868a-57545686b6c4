# -*- coding: utf-8 -*-
"""分炉补报合账引擎。

职责（纯内存计算 + 只读数据库访问，不写任何数据）：

1. 以现行账套为底，逐炉叠加暂存补报：
   - 已补报炉：外购原生/再生料、出货批、以该炉为源炉的转料全部以补报为准；
   - 未补报炉：沿用原账套对应数据。
2. 转料双向核对（“按方向和重量核对笔数”）：
   - 补报里出现的每一对炉，若只有一端补报 -> 缺报，列出炉对；
   - 两端都补报 -> 逐方向比较笔数与每笔重量（多重集），不符列出炉对与差异。
3. 对合并后的账按既有规则复核每炉总进料 == 总出料。
4. 用全部有效签单驱动求解器重核（由 web 层调用，本模块只产出合并后的输入）。
5. 生成试算依据指纹；提交时重新取数比对，依据不同即退回本次合账。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from . import db


@dataclass
class TransferIssue:
    """转料核对问题：kind = missing（缺报）/ mismatch（笔数或重量不符）。"""
    kind: str
    pair: Tuple[str, str]
    detail: str

    @property
    def pair_text(self) -> str:
        return f"{self.pair[0]} ↔ {self.pair[1]}"


@dataclass
class MergedFurnace:
    code: str
    primary_kg: int
    recycled_kg: int
    supplemented: bool


@dataclass
class MergedBatch:
    furnace: str
    code: str
    weight_kg: int
    supplemented: bool


@dataclass
class MergedTransfer:
    src: str
    dst: str
    weight_kg: int
    # 来源炉是否已补报（决定该笔取自补报还是原账）
    supplemented: bool


@dataclass
class MergedLedger:
    ledger_id: int
    furnaces: List[MergedFurnace] = field(default_factory=list)
    batches: List[MergedBatch] = field(default_factory=list)
    transfers: List[MergedTransfer] = field(default_factory=list)
    issues: List[TransferIssue] = field(default_factory=list)
    balance_problems: List[str] = field(default_factory=list)
    supplemented_codes: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.issues and not self.balance_problems


def _pair_key(a: str, b: str) -> Tuple[str, str]:
    return tuple(sorted((a, b)))  # type: ignore[return-value]


def cross_check_transfers(
    supp_rows: Sequence,
    load_transfers,
) -> List[TransferIssue]:
    """按炉对汇总补报转料并双向核对。

    每炉补报含该炉完整的进出转料明细。对补报中出现的每一炉对：
    - 只有一端补报 -> 缺报；
    - 两端都补报 -> 逐方向比较笔数与每笔重量（多重集），不符即列出。
    """
    # pair -> {炉号: {方向(src->dst): [重量, ...]}}
    pair_sides: Dict[Tuple[str, str], Dict[str, Dict[Tuple[str, str], List[int]]]] = {}

    for row in supp_rows:
        code = row["furnace_code"]
        for t in load_transfers(row["id"]):
            key = _pair_key(t["src_furnace"], t["dst_furnace"])
            slot = pair_sides.setdefault(key, {c: {} for c in key})
            direction = (t["src_furnace"], t["dst_furnace"])
            slot.setdefault(code, {}).setdefault(direction, []).append(t["weight_kg"])

    issues: List[TransferIssue] = []
    for key in sorted(pair_sides):
        a, b = key
        sides = pair_sides[key]
        reported = [c for c in key if sides.get(c)]
        if len(reported) == 1:
            only = reported[0]
            other = b if only == a else a
            n = sum(len(v) for v in sides[only].values())
            issues.append(TransferIssue(
                kind="missing",
                pair=key,
                detail=(
                    f"仅 {only} 端补报了与 {other} 之间的转料（{n} 笔），"
                    f"{other} 端缺报，须两端同时补报。"
                ),
            ))
            continue

        diffs: List[str] = []
        for direction in ((a, b), (b, a)):
            src, dst = direction
            wa = sorted(sides[a].get(direction, []))
            wb = sorted(sides[b].get(direction, []))
            if wa != wb:
                diffs.append(
                    f"方向 {src}->{dst} 笔数/重量不符："
                    f"{a} 端报 {wa or '无'}，{b} 端报 {wb or '无'}"
                )
        if diffs:
            issues.append(TransferIssue(
                kind="mismatch", pair=key, detail="；".join(diffs) + "。",
            ))
    return issues


def build_merged_ledger(conn, ledger_id: int) -> MergedLedger:
    """读现行账套与全部暂存补报，构造合并账（含核对问题与平衡复核）。"""
    furnaces, batches, transfers = db.load_ledger(conn, ledger_id)
    supp_rows = db.list_supplements(conn, ledger_id)
    supp_map = {r["furnace_code"]: r for r in supp_rows}

    merged = MergedLedger(
        ledger_id=ledger_id,
        supplemented_codes=[r["furnace_code"] for r in supp_rows],
    )

    # ---- 炉（外购料）：补报替换，否则沿用原账 ----
    for f in furnaces:
        code = f["code"]
        if code in supp_map:
            sf = db.supplement_furnace(conn, supp_map[code]["id"])
            merged.furnaces.append(MergedFurnace(
                code=code, primary_kg=sf["primary_kg"],
                recycled_kg=sf["recycled_kg"], supplemented=True,
            ))
        else:
            merged.furnaces.append(MergedFurnace(
                code=code, primary_kg=f["primary_kg"],
                recycled_kg=f["recycled_kg"], supplemented=False,
            ))

    # ---- 出货批：补报炉的批整份替换，否则沿用原账 ----
    for row in supp_rows:
        for sb in db.supplement_batches(conn, row["id"]):
            merged.batches.append(MergedBatch(
                furnace=row["furnace_code"], code=sb["code"],
                weight_kg=sb["weight_kg"], supplemented=True,
            ))
    for b in batches:
        if b["furnace_code"] not in supp_map:
            merged.batches.append(MergedBatch(
                furnace=b["furnace_code"], code=b["code"],
                weight_kg=b["weight_kg"], supplemented=False,
            ))

    # ---- 转料：按方向以“源炉补报”为准；两端都补报也只取源侧一份（去重）；
    #      源炉未补报的方向沿用原账。 ----
    # 方向 (src,dst) -> 补报重量（仅登记源炉自己补报里的行）
    supp_direction: Dict[Tuple[str, str], List[int]] = {}
    for row in supp_rows:
        code = row["furnace_code"]
        for t in db.supplement_transfers(conn, row["id"]):
            if t["src_furnace"] == code:
                supp_direction.setdefault(
                    (t["src_furnace"], t["dst_furnace"]), []
                ).append(t["weight_kg"])

    for (src, dst), weights in sorted(supp_direction.items()):
        for w in weights:
            merged.transfers.append(MergedTransfer(
                src=src, dst=dst, weight_kg=w, supplemented=True,
            ))
    supp_dirs = set(supp_direction)
    for t in transfers:
        direction = (t["src_furnace"], t["dst_furnace"])
        if direction in supp_dirs:
            continue  # 该方向已由源炉补报整份替换
        if t["src_furnace"] in supp_map:
            # 源炉已补报却未报该方向：视为补报口径已删除这笔，原账行不沿用
            continue
        merged.transfers.append(MergedTransfer(
            src=t["src_furnace"], dst=t["dst_furnace"],
            weight_kg=t["weight_kg"], supplemented=False,
        ))

    # ---- 转料双向核对 ----
    merged.issues = cross_check_transfers(
        supp_rows, lambda sid: db.supplement_transfers(conn, sid)
    )

    # ---- 合并账每炉物料平衡复核（既有规则：总进料 == 总出料）----
    balance = {f.code: {"in": 0, "out": 0} for f in merged.furnaces}
    for f in merged.furnaces:
        balance[f.code]["in"] += f.primary_kg + f.recycled_kg
    for t in merged.transfers:
        balance[t.src]["out"] += t.weight_kg
        balance[t.dst]["in"] += t.weight_kg
    for b in merged.batches:
        balance[b.furnace]["out"] += b.weight_kg
    for f in merged.furnaces:
        tin, tout = balance[f.code]["in"], balance[f.code]["out"]
        if tin != tout:
            merged.balance_problems.append(
                f"炉 {f.code}：总进料 {tin} kg ≠ 总出料 {tout} kg（差 {tin - tout:+d} kg）"
            )

    # ---- 引用完整性：合并后批号唯一、炉号/转料端点存在 ----
    seen_batches: Dict[str, str] = {}
    for b in merged.batches:
        if b.code in seen_batches and seen_batches[b.code] != b.furnace:
            merged.balance_problems.append(
                f"出货批 {b.code} 在合并账中同时归属炉 {seen_batches[b.code]} 与炉 "
                f"{b.furnace}（补报批号冲突），批号必须唯一。"
            )
        seen_batches.setdefault(b.code, b.furnace)
    fcodes = {f.code for f in merged.furnaces}
    for t in merged.transfers:
        if t.src not in fcodes or t.dst not in fcodes:
            merged.balance_problems.append(
                f"转料 {t.src}->{t.dst} 引用了合并账中不存在的炉号。"
            )

    return merged


def merged_balance_table(merged: MergedLedger) -> Dict[str, Dict[str, int]]:
    """供页面展示的每炉实物平衡表。"""
    tbl = {f.code: {
        "primary": f.primary_kg, "recycled": f.recycled_kg,
        "t_in": 0, "t_out": 0, "ship": 0,
        "supplemented": f.supplemented,
    } for f in merged.furnaces}
    for t in merged.transfers:
        tbl[t.src]["t_out"] += t.weight_kg
        tbl[t.dst]["t_in"] += t.weight_kg
    for b in merged.batches:
        tbl[b.furnace]["ship"] += b.weight_kg
    for row in tbl.values():
        row["in_total"] = row["primary"] + row["recycled"] + row["t_in"]
        row["out_total"] = row["ship"] + row["t_out"]
    return tbl


def supplement_basis_digest(conn, ledger_id: int) -> str:
    """该账套全部暂存补报的稳定指纹（含内容哈希与解析后的明细）。"""
    parts: List[str] = []
    for row in db.list_supplements(conn, ledger_id):
        sid = row["id"]
        sf = db.supplement_furnace(conn, sid)
        batches = db.supplement_batches(conn, sid)
        transfers = db.supplement_transfers(conn, sid)
        seg = [
            f"furnace={row['furnace_code']}",
            f"hash={row['content_hash']}",
            f"input={sf['primary_kg']},{sf['recycled_kg']}",
            "batches=" + ";".join(
                f"{b['code']}:{b['weight_kg']}" for b in batches
            ),
            "transfers=" + ";".join(
                f"{t['src_furnace']}>{t['dst_furnace']}:{t['weight_kg']}"
                for t in transfers
            ),
        ]
        parts.append("|".join(seg))
    raw = f"ledger={ledger_id}::" + "||".join(parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def build_basis(conn, ledger_id: int) -> dict:
    """试算依据：现行账套 + 补报指纹 + 全部有效签单指纹。"""
    return {
        "ledger_id": ledger_id,
        "supplement_digest": supplement_basis_digest(conn, ledger_id),
        "cert_digest": db.active_certificate_digest(conn),
    }


def basis_matches(conn, ledger_id: int, basis: dict) -> Optional[str]:
    """提交时复核试算依据；不一致返回中文原因，一致返回 None。"""
    current = db.current_ledger_id(conn)
    if current != basis.get("ledger_id") or current != ledger_id:
        return (
            f"试算依据账套 #{basis.get('ledger_id')} 与现行账套 "
            f"#{current} 不同（期间发生换账），本次合账退回。"
        )
    now_supp = supplement_basis_digest(conn, ledger_id)
    if now_supp != basis.get("supplement_digest"):
        return "试算后补报内容发生变动（新增、重传或删除），本次合账退回，请重新试算。"
    now_cert = db.active_certificate_digest(conn)
    if now_cert != basis.get("cert_digest"):
        return "试算后有效签单发生变动（签发或撤销），本次合账退回，请重新试算。"
    return None
