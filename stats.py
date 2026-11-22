#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
历史订单统计模块 —— 全部基于 position-history 实测字段计算，不估算、不编造。

输入: position-history 的 list（含 closed/status/closingPnl/roi/opened/maxOpenInterest 等）
输出: 胜率 / 累计盈亏 / 盈亏比 / 平均持仓时长 / 品种分布 / 连胜连亏 / 最大单笔 等

字段来源（实事求是）:
  - closingPnl        实测已实现盈亏（bapi）
  - roi               实测收益率（bapi，小数）
  - opened/closed     实测开平时间（bapi）
  - maxOpenInterest   实测最大持仓量（bapi）
  所有衍生统计均为对上述字段的纯函数计算，非接口直接给出。
"""

import time


def _num(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def compute(orders):
    """orders: position-history list。返回统计 dict。"""
    closed = [o for o in (orders or [])
              if o.get("closed") and str(o.get("status", "")).lower().startswith("all")]
    if not closed:
        return {"total": 0, "has_data": False}

    pnls = [_num(o.get("closingPnl")) for o in closed]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    gross_win = sum(wins) if wins else 0.0
    gross_loss = abs(sum(losses)) if losses else 0.0

    # 持仓时长（分钟），仅对 closed 有效的记录计算
    durations = []
    for o in closed:
        try:
            op, cl = int(o["opened"]), int(o["closed"])
            if cl > op:
                durations.append((cl - op) / 60000.0)
        except (KeyError, TypeError, ValueError):
            pass

    # 品种盈亏分布
    sym_map = {}
    for o, p in zip(closed, pnls):
        s = o.get("symbol") or "?"
        d = sym_map.setdefault(s, {"pnl": 0.0, "count": 0, "wins": 0})
        d["pnl"] += p
        d["count"] += 1
        if p > 0:
            d["wins"] += 1
    by_symbol = sorted(
        [{"symbol": s, "pnl": d["pnl"], "count": d["count"],
          "winRate": (d["wins"] / d["count"] * 100) if d["count"] else 0}
         for s, d in sym_map.items()],
        key=lambda x: x["pnl"], reverse=True)

    # 连胜/连亏（按平仓时间排序）
    ordered = sorted(closed, key=lambda o: _num(o.get("closed")), reverse=True)
    streak, streak_kind = 0, None
    max_win_streak = max_loss_streak = 0
    for o in ordered:
        p = _num(o.get("closingPnl"))
        kind = "W" if p > 0 else "L" if p < 0 else None
        if kind is None:
            continue
        if kind == streak_kind:
            streak += 1
        else:
            streak_kind, streak = kind, 1
        if kind == "W":
            max_win_streak = max(max_win_streak, streak)
        else:
            max_loss_streak = max(max_loss_streak, streak)

    total = len(closed)
    return {
        "has_data": True,
        "total": total,
        "wins": len(wins),
        "losses": len(losses),
        "winRate": (len(wins) / total * 100) if total else 0.0,
        "totalPnl": sum(pnls),
        "grossWin": gross_win,
        "grossLoss": gross_loss,
        "profitFactor": (gross_win / gross_loss) if gross_loss > 0 else None,
        "avgPnl": (sum(pnls) / total) if total else 0.0,
        "best": max(pnls) if pnls else 0.0,
        "worst": min(pnls) if pnls else 0.0,
        "avgHoldMin": (sum(durations) / len(durations)) if durations else None,
        "maxHoldMin": max(durations) if durations else None,
        "bySymbol": by_symbol,
        "maxWinStreak": max_win_streak,
        "maxLossStreak": max_loss_streak,
        "asOf": int(time.time() * 1000),
    }
