"""退役电芯成组放行 API。

流程：录入电芯 -> 预选（只读，返回候选与检测版本）-> 确认（单事务重查版本、
占用与隔离状态，原子占用整组并写入不可改写的检测快照）-> 建批（单事务重查
2~4 个已确认组的存在、同体系、当前隔离与独占归属，原子登记批次与成员关系）。

安全隔离：任意电芯可随时提交隔离（不可撤销，复测不解除）。预选排除隔离电芯；
确认事务在原事务中重查隔离状态，命中即整组拒绝；建批事务同样重查每个成员组的
当前隔离，任一组成组已阻断即整批拒绝。已入组电芯被隔离后，该组在组查询中显示
为已阻断（附电芯 ID 与原因），原检测快照与组占用保留，未受影响的组保持可放行；
已建批次随之显示为不可交付（附受影响的组号、电芯 ID 与原因），批次成员关系与
各组原快照不改写。隔离与确认、建批都走同一写路径（写锁 + BEGIN IMMEDIATE），
并发时以提交先后决定是拒绝候选/整批拒绝，还是阻断新组/新批次。

每个请求使用各自线程的 SQLite 连接：查询只能看到已提交的完整快照，
确认/隔离/建批事务未提交的中间状态对其他请求不可见。

同一快照口径：任何一次查询内，各电芯的占用、组编号与隔离标志必须来自**同一个
已提交快照**；批次查询中各组快照与当前隔离影响也在同一个只读事务内读取。
因此所有读路径都把涉及的多条 SELECT 包进同一个只读事务
（db.read_snapshot，或直接用 LEFT JOIN 一条语句完成），杜绝跨快照拼出
不存在的混合状态，例如批次内一部分组反映隔离前、另一部分组反映隔离后。
"""
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

from . import db
from .schemas import (
    BatchCreateRequest,
    CellCreate,
    ConfirmRequest,
    IsolateRequest,
    PreselectRequest,
    TestRevision,
)
from .selection import choose_group

app = FastAPI(title="退役电芯成组放行 API", version="1.2.0")


# 一次取出电芯档案与隔离标志：LEFT JOIN 单条语句天然共享同一读快照，
# occupied/group_id 与 isolated 不可能来自两个不同的已提交状态。
CELL_SELECT = (
    "SELECT c.cell_id, c.chemistry, c.capacity, c.resistance, c.test_version,"
    " c.occupied, c.group_id, i.reason IS NOT NULL AS isolated"
    " FROM cells c LEFT JOIN isolations i ON i.cell_id = c.cell_id"
)


def _row_to_cell(row) -> dict:
    return {
        "cell_id": row["cell_id"],
        "chemistry": row["chemistry"],
        "capacity": row["capacity"],
        "resistance": row["resistance"],
        "test_version": row["test_version"],
        "occupied": bool(row["occupied"]),
        "group_id": row["group_id"],
        "isolated": bool(row["isolated"]),
    }


def _isolation_payload(row) -> dict:
    return {
        "cell_id": row["cell_id"],
        "reason": row["reason"],
        "isolated_at": row["created_at"],
    }


# ---------- 录入与复测 ----------

@app.post("/cells", status_code=201)
def create_cell(body: CellCreate):
    """录入一节待选电芯（最多 12 节）。"""
    conn = db.get_conn()
    with db.write_lock:
        conn.execute("BEGIN IMMEDIATE")
        try:
            count = conn.execute("SELECT COUNT(*) AS n FROM cells").fetchone()["n"]
            if count >= db.MAX_CELLS:
                raise HTTPException(409, f"待选电芯已达上限 {db.MAX_CELLS} 节，无法继续录入")
            try:
                conn.execute(
                    "INSERT INTO cells (cell_id, chemistry, capacity, resistance, test_version)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (body.cell_id, body.chemistry, body.capacity, body.resistance,
                     body.test_version),
                )
            except Exception as exc:
                if "UNIQUE" in str(exc).upper():
                    raise HTTPException(409, f"电芯 {body.cell_id} 已存在")
                raise
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    with db.read_snapshot(conn):
        row = conn.execute(
            CELL_SELECT + " WHERE c.cell_id = ?", (body.cell_id,)).fetchone()
        return _row_to_cell(row)


@app.get("/cells")
def list_cells():
    """电芯列表：整批电芯的占用、组编号与隔离标志只反映同一已提交快照。"""
    conn = db.get_conn()
    with db.read_snapshot(conn):
        rows = conn.execute(CELL_SELECT + " ORDER BY c.cell_id").fetchall()
        return {"count": len(rows), "cells": [_row_to_cell(r) for r in rows]}


@app.get("/cells/{cell_id}")
def get_cell(cell_id: str):
    conn = db.get_conn()
    # 单条 LEFT JOIN 已保证占用与隔离标志同快照；外面再包只读事务固定整次请求快照
    with db.read_snapshot(conn):
        row = conn.execute(
            CELL_SELECT + " WHERE c.cell_id = ?", (cell_id,)).fetchone()
        if row is None:
            raise HTTPException(404, f"电芯 {cell_id} 不存在")
        return _row_to_cell(row)


@app.put("/cells/{cell_id}/test")
def revise_test(cell_id: str, body: TestRevision):
    """复测修订：更新容量/内阻并提升检测版本。

    未确认的电芯：修订后旧的预选候选即过期（确认时版本对不上会被整组拒绝）。
    已确认的电芯：修订只影响电芯当前档案，已确认组的检测快照保持不变。
    """
    conn = db.get_conn()
    with db.write_lock:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute("SELECT * FROM cells WHERE cell_id = ?", (cell_id,)).fetchone()
            if row is None:
                raise HTTPException(404, f"电芯 {cell_id} 不存在")
            conn.execute(
                "UPDATE cells SET capacity = ?, resistance = ?, test_version = ?"
                " WHERE cell_id = ?",
                (body.capacity, body.resistance, body.test_version, cell_id),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    with db.read_snapshot(conn):
        row = conn.execute(
            CELL_SELECT + " WHERE c.cell_id = ?", (cell_id,)).fetchone()
        return _row_to_cell(row)


# ---------- 安全隔离 ----------

@app.post("/cells/{cell_id}/isolate")
def isolate_cell(cell_id: str, body: IsolateRequest):
    """提交安全隔离：记录一节电芯的隔离原因，隔离不可撤销（复测也不解除）。

    电芯不存在返回 404；重复隔离不新增记录，返回首次隔离的原记录（200）。
    与确认共用同一写路径：并发时以提交先后决定是拒绝候选还是阻断新组。
    """
    conn = db.get_conn()
    with db.write_lock:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT 1 FROM cells WHERE cell_id = ?", (cell_id,)).fetchone()
            if row is None:
                raise HTTPException(404, f"电芯 {cell_id} 不存在")
            existing = conn.execute(
                "SELECT * FROM isolations WHERE cell_id = ?", (cell_id,)).fetchone()
            created = existing is None
            if created:
                now = datetime.now(timezone.utc).isoformat()
                conn.execute(
                    "INSERT INTO isolations (cell_id, reason, created_at)"
                    " VALUES (?, ?, ?)",
                    (cell_id, body.reason, now),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    rec = conn.execute(
        "SELECT * FROM isolations WHERE cell_id = ?", (cell_id,)).fetchone()
    payload = _isolation_payload(rec)
    if created:
        return JSONResponse(status_code=201, content=payload)
    return payload  # 重复隔离：返回首次隔离的原记录


@app.get("/isolations")
def list_isolations():
    """隔离记录列表（只反映已提交的隔离，用于受影响范围追溯）。"""
    conn = db.get_conn()
    rows = conn.execute(
        "SELECT * FROM isolations ORDER BY created_at, cell_id").fetchall()
    return {"count": len(rows), "isolations": [_isolation_payload(r) for r in rows]}


# ---------- 预选 ----------

@app.post("/preselect")
def preselect(body: PreselectRequest):
    """按串联要求预选一组同体系、未占用、未隔离电芯（只读，不占用）。

    候选的占用与隔离状态在同一个只读事务（同一已提交快照）内读取：
    确认/隔离事务未提交的中间状态不可见，候选也不会跨快照拼装。
    """
    conn = db.get_conn()
    with db.read_snapshot(conn):
        rows = conn.execute(
            CELL_SELECT
            + " WHERE c.occupied = 0"
              " AND c.cell_id NOT IN (SELECT cell_id FROM isolations)"
        ).fetchall()
        free = [_row_to_cell(r) for r in rows]
        if body.chemistry is not None:
            free = [c for c in free if c["chemistry"] == body.chemistry]

        # 按化学体系分组择优
        by_chem: dict[str, list[dict]] = {}
        for c in free:
            by_chem.setdefault(c["chemistry"], []).append(c)

        best = None  # (limited_capacity, ids_tuple, combo, chemistry)
        for chem, cells in by_chem.items():
            combo, limited = choose_group(
                cells, body.series_count, body.min_capacity,
                body.max_capacity_diff, body.max_resistance_diff,
            )
            if combo is None:
                continue
            ids = tuple(sorted(c["cell_id"] for c in combo))
            if (best is None
                    or limited > best[0]
                    or (limited == best[0] and ids < best[1])):
                best = (limited, ids, combo, chem)

        if best is None:
            return JSONResponse(
                {"feasible": False, "reason": _infeasible_reason(free, body)})

        limited, ids, combo, chem = best
        combo_sorted = sorted(combo, key=lambda c: c["cell_id"])
        return {
            "feasible": True,
            "chemistry": chem,
            "series_count": body.series_count,
            "limited_capacity": limited,
            "cell_ids": [c["cell_id"] for c in combo_sorted],
            "cells": [
                {
                    "cell_id": c["cell_id"],
                    "capacity": c["capacity"],
                    "resistance": c["resistance"],
                    "test_version": c["test_version"],
                }
                for c in combo_sorted
            ],
        }


def _infeasible_reason(free: list[dict], body: PreselectRequest) -> str:
    if not free:
        if body.chemistry:
            return f"没有化学体系为 {body.chemistry} 的未占用电芯"
        return "没有可用的未占用电芯"
    by_chem: dict[str, list[dict]] = {}
    for c in free:
        if c["capacity"] >= body.min_capacity:
            by_chem.setdefault(c["chemistry"], []).append(c)
    if not by_chem:
        return (f"没有容量不低于最低单芯容量 {body.min_capacity} 的未占用电芯"
                f"（未占用共 {len(free)} 节）")
    max_n = max(len(v) for v in by_chem.values())
    if max_n < body.series_count:
        chem = max(by_chem, key=lambda k: len(by_chem[k]))
        return (f"同一化学体系下容量达标的未占用电芯不足 {body.series_count} 节"
                f"（体系 {chem} 最多 {max_n} 节）")
    return (f"候选电芯数量足够，但任意 {body.series_count} 节同体系组合均超出"
            f"容量差上限 {body.max_capacity_diff} 或内阻差上限 {body.max_resistance_diff}")


# ---------- 确认与快照查询 ----------

@app.post("/groups/confirm", status_code=201)
def confirm_group(body: ConfirmRequest):
    """确认候选：单事务内重查检测版本、占用与隔离状态，全部一致才原子占用整组并写快照。

    任何一节版本已变化（确认前被复测）、已被别组占用或已被安全隔离
    -> 整组拒绝，不产生组记录，也不留部分占用。
    """
    ids = [c.cell_id for c in body.cells]
    if len(set(ids)) != len(ids):
        raise HTTPException(422, "确认列表中存在重复电芯 ID")
    expected = {c.cell_id: c.test_version for c in body.cells}

    conn = db.get_conn()
    with db.write_lock:
        conn.execute("BEGIN IMMEDIATE")
        try:
            rows = conn.execute(
                f"SELECT * FROM cells WHERE cell_id IN ({','.join('?' * len(ids))})",
                ids,
            ).fetchall()
            found = {r["cell_id"]: r for r in rows}

            missing = [i for i in ids if i not in found]
            if missing:
                raise HTTPException(409, f"电芯不存在：{missing}")
            iso_rows = conn.execute(
                f"SELECT cell_id FROM isolations WHERE cell_id IN"
                f" ({','.join('?' * len(ids))})",
                ids,
            ).fetchall()
            isolated = [r["cell_id"] for r in iso_rows]
            if isolated:
                raise HTTPException(
                    409, f"电芯已被安全隔离，候选整组拒绝：{sorted(isolated)}")
            occupied = [i for i in ids if found[i]["occupied"]]
            if occupied:
                raise HTTPException(409, f"电芯已被其他组占用，候选过期：{occupied}")
            stale = [i for i in ids if found[i]["test_version"] != expected[i]]
            if stale:
                raise HTTPException(
                    409,
                    "检测版本已变化（确认前发生复测），候选过期："
                    + str([{i: {"expected": expected[i], "actual": found[i]["test_version"]}}
                           for i in stale]),
                )
            chemistries = {found[i]["chemistry"] for i in ids}
            if len(chemistries) != 1:
                raise HTTPException(409, f"化学体系不一致，无法成组：{sorted(chemistries)}")

            chemistry = chemistries.pop()
            limited = min(found[i]["capacity"] for i in ids)
            now = datetime.now(timezone.utc).isoformat()
            cur = conn.execute(
                "INSERT INTO groups (chemistry, series_count, limited_capacity, created_at)"
                " VALUES (?, ?, ?, ?)",
                (chemistry, len(ids), limited, now),
            )
            group_id = cur.lastrowid
            for i in ids:
                r = found[i]
                conn.execute(
                    "INSERT INTO group_cells (group_id, cell_id, capacity, resistance, test_version)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (group_id, i, r["capacity"], r["resistance"], r["test_version"]),
                )
                conn.execute(
                    "UPDATE cells SET occupied = 1, group_id = ? WHERE cell_id = ?",
                    (group_id, i),
                )
            conn.commit()
        except Exception:
            conn.rollback()  # 整组拒绝：不留下任何部分占用
            raise
    with db.read_snapshot(conn):
        return _group_payload(conn, group_id)


def _group_payload(conn, group_id: int) -> dict:
    g = conn.execute("SELECT * FROM groups WHERE group_id = ?", (group_id,)).fetchone()
    rows = conn.execute(
        "SELECT * FROM group_cells WHERE group_id = ? ORDER BY cell_id", (group_id,)
    ).fetchall()
    # 受影响追溯：组内当前被隔离的电芯（原检测快照不受影响，照常返回）
    blocked = conn.execute(
        "SELECT i.cell_id, i.reason, i.created_at FROM isolations i"
        " JOIN group_cells gc ON gc.group_id = ? AND gc.cell_id = i.cell_id"
        " ORDER BY i.cell_id",
        (group_id,),
    ).fetchall()
    return {
        "group_id": g["group_id"],
        "chemistry": g["chemistry"],
        "series_count": g["series_count"],
        "limited_capacity": g["limited_capacity"],
        "created_at": g["created_at"],
        "blocked": len(blocked) > 0,  # True=已阻断不可放行；False=未受影响，保持可放行
        "blocked_cells": [
            {"cell_id": r["cell_id"], "reason": r["reason"], "isolated_at": r["created_at"]}
            for r in blocked
        ],
        "cells": [
            {
                "cell_id": r["cell_id"],
                "capacity": r["capacity"],
                "resistance": r["resistance"],
                "test_version": r["test_version"],
            }
            for r in rows
        ],
    }


@app.get("/groups")
def list_groups():
    """组列表：同一已提交快照内的完整组（确认事务未提交的组不可见）。

    每组附带当前阻断状态：组内电芯被隔离则 blocked=true 并列出电芯 ID 与原因。
    """
    conn = db.get_conn()
    with db.read_snapshot(conn):
        rows = conn.execute("SELECT group_id FROM groups ORDER BY group_id").fetchall()
        groups = [_group_payload(conn, r["group_id"]) for r in rows]
    return {"count": len(groups), "groups": groups}


@app.get("/groups/{group_id}")
def get_group(group_id: int):
    """按组编号查询不可改写的检测快照与当前阻断状态（未提交或已回滚的组不可见）。"""
    conn = db.get_conn()
    with db.read_snapshot(conn):
        g = conn.execute(
            "SELECT 1 FROM groups WHERE group_id = ?", (group_id,)).fetchone()
        if g is None:
            raise HTTPException(404, f"组 {group_id} 不存在")
        return _group_payload(conn, group_id)


# ---------- 交付批次登记与查询 ----------

@app.post("/batches", status_code=201)
def create_batch(body: BatchCreateRequest):
    """把 2~4 个已确认组登记为一个可追溯交付批次。

    单事务（BEGIN IMMEDIATE，与确认/隔离共用同一写路径）内重查全部成员：
    组全部存在、同一化学体系、当前没有任一成员电芯被隔离阻断、且尚未归属
    其他批次，才原子写入批次与成员归属；任一条件不满足即整批拒绝、回滚，
    不产生批次也不留部分归属。
    """
    group_ids = list(body.group_ids)
    if len(set(group_ids)) != len(group_ids):
        raise HTTPException(422, "组号列表中存在重复组号")

    conn = db.get_conn()
    with db.write_lock:
        conn.execute("BEGIN IMMEDIATE")
        try:
            rows = conn.execute(
                f"SELECT group_id, chemistry FROM groups WHERE group_id IN"
                f" ({','.join('?' * len(group_ids))})",
                group_ids,
            ).fetchall()
            found = {r["group_id"]: r for r in rows}

            missing = [g for g in group_ids if g not in found]
            if missing:
                raise HTTPException(409, f"组不存在：{missing}")
            chemistries = {found[g]["chemistry"] for g in group_ids}
            if len(chemistries) != 1:
                raise HTTPException(
                    409, f"批次只接收同一化学体系的组，体系不一致：{sorted(chemistries)}")
            # 独占归属重查：组只能属于一个批次
            owned = conn.execute(
                f"SELECT group_id, batch_id FROM batch_groups WHERE group_id IN"
                f" ({','.join('?' * len(group_ids))})",
                group_ids,
            ).fetchall()
            if owned:
                raise HTTPException(
                    409,
                    "组已归属其他批次，整批拒绝："
                    + str([{r["group_id"]: r["batch_id"]} for r in owned]),
                )
            # 当前隔离重查：任一成员组已阻断即整批拒绝
            blocked_rows = conn.execute(
                f"SELECT g.group_id AS group_id, i.cell_id AS cell_id, i.reason AS reason"
                f" FROM groups g JOIN group_cells gc ON gc.group_id = g.group_id"
                f" JOIN isolations i ON i.cell_id = gc.cell_id"
                f" WHERE g.group_id IN ({','.join('?' * len(group_ids))})"
                f" ORDER BY g.group_id, i.cell_id",
                group_ids,
            ).fetchall()
            if blocked_rows:
                detail = [
                    {"group_id": r["group_id"], "cell_id": r["cell_id"], "reason": r["reason"]}
                    for r in blocked_rows
                ]
                raise HTTPException(
                    409, f"成员组当前已被隔离阻断，整批拒绝：{detail}")

            now = datetime.now(timezone.utc).isoformat()
            cur = conn.execute(
                "INSERT INTO batches (chemistry, created_at) VALUES (?, ?)",
                (chemistries.pop(), now),
            )
            batch_id = cur.lastrowid
            for g in group_ids:
                conn.execute(
                    "INSERT INTO batch_groups (batch_id, group_id) VALUES (?, ?)",
                    (batch_id, g),
                )
            # UNIQUE(group_id) 是独占归属的最终防线：绕过应用锁抢注时在此失败
            conn.commit()
        except Exception:
            conn.rollback()  # 整批拒绝：不留下任何部分归属
            raise
    with db.read_snapshot(conn):
        return _batch_payload(conn, batch_id)


def _batch_payload(conn, batch_id: int) -> dict:
    """组装批次响应：固定成员关系、各组确认时的检测快照，以及按**当前**隔离
    记录计算的整批可交付状态（受影响的组号、电芯 ID 与原因）。

    调用方须把本函数内所有 SELECT 包在同一个 read_snapshot 内，保证成员组快照与
    隔离影响来自同一已提交状态，不会出现一批之内半旧半新的阻断结论。
    """
    b = conn.execute("SELECT * FROM batches WHERE batch_id = ?", (batch_id,)).fetchone()
    members = conn.execute(
        "SELECT group_id FROM batch_groups WHERE batch_id = ? ORDER BY group_id",
        (batch_id,),
    ).fetchall()
    groups = [_group_payload(conn, r["group_id"]) for r in members]
    blocked_groups = [
        {
            "group_id": g["group_id"],
            "cells": g["blocked_cells"],
        }
        for g in groups if g["blocked"]
    ]
    return {
        "batch_id": b["batch_id"],
        "chemistry": b["chemistry"],
        "created_at": b["created_at"],
        "group_ids": [g["group_id"] for g in groups],  # 成员关系固定
        "deliverable": len(blocked_groups) == 0,  # 当前是否可交付
        "blocked_groups": blocked_groups,
        "groups": groups,
    }


@app.get("/batches")
def list_batches():
    """批次列表：成员快照与当前隔离影响取自同一已提交快照（建批未提交不可见）。"""
    conn = db.get_conn()
    with db.read_snapshot(conn):
        rows = conn.execute("SELECT batch_id FROM batches ORDER BY batch_id").fetchall()
        batches = [_batch_payload(conn, r["batch_id"]) for r in rows]
    return {"count": len(batches), "batches": batches}


@app.get("/batches/{batch_id}")
def get_batch(batch_id: int):
    """按批次号查询：固定成员关系 + 各组确认时检测快照 + 当前可交付状态。

    后续隔离只改变 deliverable/blocked_groups（及组内 blocked_cells），
    不改写成员关系与原快照；未提交或已回滚的批次不可见。
    """
    conn = db.get_conn()
    with db.read_snapshot(conn):
        b = conn.execute(
            "SELECT 1 FROM batches WHERE batch_id = ?", (batch_id,)).fetchone()
        if b is None:
            raise HTTPException(404, f"批次 {batch_id} 不存在")
        return _batch_payload(conn, batch_id)
