#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
仓位生命周期还原 —— 把「开 → 加 → 减 → 平」讲清楚。

数据源（全部公开 bapi 实测字段，零编造）：
  - position-history：仓位骨架（opened/closed/avgCost/avgClosePrice/
    maxOpenInterest/closedVolume/closingPnl/roi/leverage/positionId/status）
  - trade-history：逐笔成交（time/side/positionSide/qty/price/realizedProfit）
    用 realizedProfit 是否为 0 区分「开仓类」与「平仓类」成交。

判定规则（诚实、可解释）：
  - 开仓单：realizedProfit ≈ 0
      · side/positionSide = SELL/SHORT 或 BUY/LONG
  - 平仓单：realizedProfit != 0
  - 首笔开仓后同方向的后续开仓单 = 加仓（时间聚合成动作）
  - 平仓单若该仓 closedVolume < maxOpenInterest 且未 All Closed = 减仓
  - 该仓最后一笔平仓 / status=All Closed = 平仓

输出：每笔仓位一个 lifecycle 对象，含 actions 时间线 + 分类计数 + 汇总资金。
窗口外的老仓位成交已滚出接口，用骨架合成 开/平 两个端点动作，并标注 source。
"""

import datetime as _dt


def _num(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _is_open_trade(t):
    return abs(_num(t.get("realizedProfit"))) < 1e-9


def _fmt_time(ms):
    if not ms:
        return None
    try:
        return _dt.datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, OSError, OverflowError):
        return None


def _side_cn(position_side):
    return "空" if str(position_side or "").upper() == "SHORT" else "多"


def _cluster(trades, gap_ms):
    """把同一动作内相隔 gap_ms 以内的连续成交聚合成一批，避免同秒碎单刷屏。"""
    out = []
    for t in trades:
        ts = int(t.get("time") or 0)
        if out and ts - out[-1]["_last_ts"] <= gap_ms:
            b = out[-1]
            b["_last_ts"] = ts
            b["qty"] += abs(_num(t.get("qty")))
            b["notional"] += abs(_num(t.get("quantity")))
            b["count"] += 1
            # 成交量加权均价
            w = abs(_num(t.get("qty")))
            b["_pw"] += w * _num(t.get("price"))
        else:
            out.append({
                "_last_ts": ts, "time": ts, "qty": abs(_num(t.get("qty"))),
                "notional": abs(_num(t.get("quantity"))), "count": 1,
                "_pw": abs(_num(t.get("qty"))) * _num(t.get("price")),
            })
    for b in out:
        b["price"] = (b["_pw"] / b["qty"]) if b["qty"] else 0.0
        b.pop("_pw", None)
        b.pop("_last_ts", None)
    return out


def _classify_actions(batches, pos, gap_ms):
    """把聚类后的批次标注为 开仓/加仓/减仓/平仓。"""
    actions = []
    opened_cnt = 0
    for b in batches:
        is_open = b.get("_open", False)
        if is_open:
            opened_cnt += 1
            kind = "开仓" if opened_cnt == 1 else "加仓"
        else:
            # 平仓类：结合骨架与已平量判断是否最后一笔
            kind = "平仓"
        actions.append({
            "kind": kind,
            "time": b["time"],
            "timeStr": _fmt_time(b["time"]),
            "qty": round(b["qty"], 6),
            "price": round(b["price"], 4),
            "notional": round(b["notional"], 2),
            "count": b["count"],
            "realized": round(b.get("realized", 0.0), 2),
        })
    return actions


def _build_from_trades(p, trades, window_ms, gap_ms):
    """窗口内有成交：用逐笔还原开/加/平，按骨架推算减仓端点。"""
    sym = p.get("symbol")
    op = int(p.get("opened") or 0)
    cl = int(p.get("closed") or 0)
    end = cl or int(p.get("updateTime") or op)
    ps = str(p.get("side") or "").upper()

    # 该仓位窗口内、同标的、方向一致的成交
    win = [t for t in trades
           if t.get("symbol") == sym
           and str(t.get("positionSide") or "").upper() == ps
           and (op - 30000) <= int(t.get("time") or 0) <= (end + 30000)]
    if not win:
        return None
    win.sort(key=lambda t: int(t.get("time") or 0))

    open_t = [t for t in win if _is_open_trade(t)]
    close_t = [t for t in win if not _is_open_trade(t)]
    if not open_t and not close_t:
        return None

    # 分别聚类开/平，再按时间合并
    ob = _cluster(open_t, gap_ms)
    cb = _cluster(close_t, gap_ms)
    for b in ob:
        b["_open"] = True
    for b in cb:
        b["_open"] = False
        # 已实现盈亏：窗口内平仓单的 realizedProfit 之和按 qty 加权分摊到批量
        b["realized"] = sum(_num(t.get("realizedProfit")) for t in close_t
                            if abs(int(t.get("time") or 0) - b["time"]) <= gap_ms or
                            (b["_last_ts"] if "_last_ts" in b else b["time"]) - int(t.get("time") or 0) <= gap_ms)
    merged = sorted(ob + cb, key=lambda b: b["time"])
    actions = _classify_actions(merged, p, gap_ms)

    # 若有部分平仓骨架：把「平仓」类中最后一笔之前的标注为「减仓」
    peak = _num(p.get("maxOpenInterest"))
    closedv = _num(p.get("closedVolume"))
    is_closed = str(p.get("status", "")).lower().startswith("all")
    close_actions = [a for a in actions if a["kind"] in ("平仓", "减仓")]
    if close_actions and not is_closed and closedv < peak:
        # 部分平仓：全部平仓类都视为减仓（仓位未清零）
        for a in close_actions:
            a["kind"] = "减仓"
    return {
        "actions": actions,
        "source": "逐笔成交还原",
        "detail": f"窗口内 {len(win)} 笔成交（开 {len(open_t)} / 平 {len(close_t)}）聚合成 {len(actions)} 个动作",
    }


def _build_from_skeleton(p):
    """窗口外老仓位：成交已滚出，用 position-history 骨架合成开/平端点。"""
    peak = _num(p.get("maxOpenInterest"))
    closedv = _num(p.get("closedVolume"))
    is_closed = str(p.get("status", "")).lower().startswith("all")
    avg = _num(p.get("avgCost"))
    avgc = _num(p.get("avgClosePrice"))
    actions = [{
        "kind": "开仓", "time": int(p.get("opened") or 0),
        "timeStr": _fmt_time(p.get("opened")), "qty": round(peak, 6),
        "price": round(avg, 4), "notional": round(peak * avg, 2),
        "count": None, "realized": 0.0,
    }]
    if closedv > 1e-9:
        actions.append({
            "kind": "平仓" if is_closed else "减仓",
            "time": int(p.get("closed") or p.get("updateTime") or 0),
            "timeStr": _fmt_time(p.get("closed") or p.get("updateTime")),
            "qty": round(closedv, 6), "price": round(avgc, 4),
            "notional": round(closedv * avgc, 2),
            "count": None, "realized": round(_num(p.get("closingPnl")), 2),
        })
    return {
        "actions": actions,
        "source": "骨架端点（逐笔已滚出接口窗口）",
        "detail": "该仓位较早，逐笔成交已超出公开接口最近页，仅还原开平端点",
    }


def _enrich(p, trades, window_ms, gap_ms):
    built = _build_from_trades(p, trades, window_ms, gap_ms) if trades else None
    if not built:
        built = _build_from_skeleton(p)
    actions = built["actions"]

    # 端点补齐：时间线必须有「开仓」锚点；全平的仓位必须有「平仓」锚点。
    # 窗口内成交可能只剩减仓/平仓单，开仓端点用骨架补上，保证故事线完整。
    peak = _num(p.get("maxOpenInterest"))
    closedv = _num(p.get("closedVolume"))
    avg = _num(p.get("avgCost"))
    avgc = _num(p.get("avgClosePrice"))
    is_closed = str(p.get("status", "")).lower().startswith("all")
    if not any(a["kind"] in ("开仓", "加仓") for a in actions):
        actions.insert(0, {
            "kind": "开仓", "time": int(p.get("opened") or 0),
            "timeStr": _fmt_time(p.get("opened")), "qty": round(peak, 6),
            "price": round(avg, 4), "notional": round(peak * avg, 2),
            "count": None, "realized": 0.0, "anchor": True,
        })
    if is_closed and not any(a["kind"] == "平仓" for a in actions):
        actions.append({
            "kind": "平仓", "time": int(p.get("closed") or p.get("updateTime") or 0),
            "timeStr": _fmt_time(p.get("closed") or p.get("updateTime")),
            "qty": round(closedv, 6), "price": round(avgc, 4),
            "notional": round(closedv * avgc, 2),
            "count": None, "realized": round(_num(p.get("closingPnl")), 2),
            "anchor": True,
        })
    kinds = {}
    for a in actions:
        kinds[a["kind"]] = kinds.get(a["kind"], 0) + 1
    peak = _num(p.get("maxOpenInterest"))
    closedv = _num(p.get("closedVolume"))
    remain = max(0.0, peak - closedv)
    pnl = _num(p.get("closingPnl"))
    roi = _num(p.get("roi"))
    opened = int(p.get("opened") or 0)
    closed = int(p.get("closed") or 0)
    return {
        "positionId": p.get("positionId"),
        "symbol": p.get("symbol"),
        "side": _side_cn(p.get("side")),
        "sideRaw": str(p.get("side") or ""),
        "leverage": p.get("leverage"),
        "marginType": "全仓" if str(p.get("isolated", "")).lower() == "cross" else "逐仓",
        "status": p.get("status"),
        "opened": opened, "openedStr": _fmt_time(opened),
        "closed": closed or None, "closedStr": _fmt_time(closed) if closed else None,
        "holdMs": (closed - opened) if closed else None,
        "avgCost": round(_num(p.get("avgCost")), 4),
        "avgClosePrice": round(_num(p.get("avgClosePrice")), 4) if closed else None,
        "peakQty": round(peak, 6),
        "closedQty": round(closedv, 6),
        "remainQty": round(remain, 6),
        "pnl": round(pnl, 2),
        "roiPct": round(roi * 100, 2),
        "actions": actions,
        "kinds": kinds,
        "source": built["source"],
        "detail": built["detail"],
    }


def compute(position_rows, trade_rows, window_ms=None, gap_ms=None, max_items=30):
    """
    position_rows: position-history list
    trade_rows   : 深拉的多页 trade-history list（可为 None/空）
    window_ms    : 仓位窗口前后放宽（默认 60s）
    gap_ms       : 同一动作碎单聚合间隔（默认 90s）
    """
    window_ms = int(window_ms or 60000)
    gap_ms = int(gap_ms or 90000)
    rows = list(position_rows or [])
    rows.sort(key=lambda p: int(p.get("opened") or 0), reverse=True)
    out = [_enrich(p, trade_rows, window_ms, gap_ms) for p in rows[:max_items]]

    # 资金汇总
    total_pnl = round(sum(x["pnl"] for x in out), 2)
    total_notional = round(sum(a["notional"] for x in out for a in x["actions"]), 2)
    total_actions = sum(len(x["actions"]) for x in out)
    return {
        "items": out,
        "count": len(out),
        "totalPnl": total_pnl,
        "totalNotional": total_notional,
        "totalActions": total_actions,
        "asOf": _fmt_time(max([int(p.get("opened") or 0) for p in rows] or [0])),
    }
