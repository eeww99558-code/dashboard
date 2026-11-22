#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
当前持仓还原模块

背景：币安跟单 detail.positionShow=false 的交易员，公开 bapi 不提供
实时持仓的保证金率/强平价/浮动盈亏。position-history 只返回【已了结】
仓位（All Closed / Partially Closed），纯新开、从未平过的仓位（如刚开的
BTC 空单）根本不在其中——这是面板曾漏掉新仓的根因。

本模块用【混合数据源】还原全部当前持仓：
  1. position-history 的未全平仓位（Partially Closed）→ 已部分平仓的剩余量
     剩余量 = maxOpenInterest - closedVolume；均价 = avgCost
  2. trade-history 最近若干页 → 重建 position-history 里没有的【新仓】
     判据：realizedProfit==0 为开仓、!=0 为平仓；按 (symbol, 方向) 净额累计
     新仓均价 = 开仓成交额 / 开仓量
  3. fapi 公共行情（ticker/price、premiumIndex）→ 现价 / 标记价

字段来源（实事求是）：
  - 持仓量 / 开仓均价 / 杠杆 / 逐仓全仓  实测（bapi）
  - 现价 / 标记价                       实测（fapi）
  - 已实现盈亏                          实测（position-history closingPnl）
  - 浮动盈亏 / 保证金 / 实际杠杆 / 强平价  推算（见面板注记）

不引入新依赖；复用 requests 与 monitor.PublicAPI。
"""

import time

import requests

import monitor as M

FAPI = "https://fapi.binance.com/fapi/v1"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "Chrome/120.0.0.0 Safari/537.36")
MMR = 0.005        # 维持保证金率近似值，仅用于强平价估算
NEW_TRADE_PAGES = 4  # 重建新仓拉取的成交页数（每页 100 条）


def _fget(path, params, timeout=8, retries=2):
    """fapi 公共行情 GET，带简单重试。"""
    url = FAPI + path
    last = None
    for i in range(retries + 1):
        try:
            r = requests.get(url, params=params, timeout=timeout,
                             headers={"User-Agent": UA})
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last = e
            if i < retries:
                time.sleep(1)
    raise last


def quote(symbol):
    """返回 (当前价, 标记价)，任一失败返回 None 占位。"""
    price = mark = None
    try:
        price = float(_fget("/ticker/price", {"symbol": symbol})["price"])
    except Exception:
        pass
    try:
        mark = float(_fget("/premiumIndex", {"symbol": symbol})["markPrice"])
    except Exception:
        pass
    return price, mark


def _num(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _open_rows_from_history(position_history):
    """来源1：position-history 中未全平仓位的剩余量。"""
    rows = []
    for p in position_history or []:
        status = str(p.get("status") or "").lower()
        if status in ("all closed", "closed"):
            continue
        qty = _num(p.get("maxOpenInterest")) - _num(p.get("closedVolume"))
        if qty <= 0:
            continue
        short = str(p.get("side") or "").lower() == "short"
        rows.append({
            "symbol": p.get("symbol") or "",
            "short": short,
            "qty": qty,
            "avgCost": _num(p.get("avgCost")),
            "leverage": _num(p.get("leverage"), 1) or 1,
            "isolated": p.get("isolated"),
            "realized": _num(p.get("closingPnl")),
            "src": "history",
        })
    return rows


def _rebuild_new_positions(trades, known):
    """
    来源2：从成交流重建 known 里没有的新仓。

    判据：realizedProfit==0 → 开仓（增仓）；!=0 → 平仓（减仓）。
    按 (symbol, short) 净额累计；开仓均价 = Σ(开仓 qty×price) / Σ(开仓 qty)。
    """
    agg = {}
    for t in trades or []:
        sym = t.get("symbol") or ""
        side = str(t.get("side") or "").upper()
        ps = str(t.get("positionSide") or "").upper()
        short = ps == "SHORT"
        qty = abs(_num(t.get("qty")))
        price = _num(t.get("price"))
        realized = abs(_num(t.get("realizedProfit")))
        if qty <= 0 or not sym:
            continue
        # 开仓：SELL/SHORT 或 BUY/LONG，且 realizedProfit≈0
        is_open = (realized < 1e-9 and
                   ((short and side == "SELL") or (not short and side == "BUY")))
        # 平仓：BUY/SHORT 或 SELL/LONG，且 realizedProfit!=0
        is_close = (realized >= 1e-9 and
                    ((short and side == "BUY") or (not short and side == "SELL")))
        if not (is_open or is_close):
            continue
        key = (sym, short)
        a = agg.setdefault(key, {"qty": 0.0, "open_qty": 0.0, "open_notional": 0.0})
        if is_open:
            a["qty"] += qty
            a["open_qty"] += qty
            a["open_notional"] += qty * price
        else:
            a["qty"] -= qty

    out = []
    for (sym, short), a in agg.items():
        if a["qty"] <= 1e-8:
            continue
        if (sym, short) in known:   # position-history 已覆盖，跳过避免重复
            continue
        avg = a["open_notional"] / a["open_qty"] if a["open_qty"] > 0 else 0
        if avg <= 0:
            continue
        out.append({
            "symbol": sym, "short": short, "qty": a["qty"], "avgCost": avg,
            "leverage": 1, "isolated": None, "realized": 0.0, "src": "rebuilt",
        })
    return out


def compute(pid, api=None, size=50, margin_balance=None, new_pages=NEW_TRADE_PAGES):
    """
    返回当前持仓列表，每项含截图式字段。

    margin_balance: 账户权益（detail.marginBalance），用于全仓实际杠杆估算。
    """
    api = api or M.PublicAPI()
    margin_balance = _num(margin_balance, 0) or None

    # 来源1：已了结仓位的剩余量
    ph = api.position_history(pid, size=size)
    rows = _open_rows_from_history(ph)
    known = {(r["symbol"], r["short"]) for r in rows}

    # 来源2：成交流重建新仓（限流退避已在 PublicAPI._request 统一处理）
    trades = []
    for pg in range(1, max(1, new_pages) + 1):
        try:
            page = api.trade_history(pid, page=pg, size=100)
        except Exception:
            break   # 彻底失败则沿用已拉到的页，不拖垮来源1
        trades.extend(page)
        if len(page) < 100:
            break
    if trades:
        rows += _rebuild_new_positions(trades, known)

    if not rows:
        return []

    # 统一计算衍生字段
    quotes = {}
    out = []
    for p in rows:
        sym = p["symbol"]
        if not sym:
            continue
        if sym not in quotes:
            quotes[sym] = quote(sym)
        price, mark = quotes[sym]
        avg = p["avgCost"]
        lev = p["leverage"]
        short = p["short"]
        qty = p["qty"]
        ref = mark or price or avg
        notional = qty * ref
        margin = notional / lev if lev > 0 else 0.0
        upnl = (ref - avg) * qty * (-1 if short else 1)
        equity = margin_balance if margin_balance and margin_balance > 0 else margin
        real_lev = notional / equity if equity > 0 else lev
        if short:
            liq = avg * (1 + max(0.0, 1.0 / lev - MMR))
        else:
            liq = avg * (1 - min(0.99, 1.0 / lev - MMR))
        out.append({
            "symbol": sym,
            "side": "做空" if short else "做多",
            "short": short,
            "marginType": "全仓" if str(p.get("isolated") or "").lower() == "cross" else
                          ("逐仓" if p.get("isolated") else "—"),
            "leverage": lev,
            "qty": qty,
            "avgCost": avg,
            "price": price,
            "mark": mark,
            "notional": notional,
            "margin": margin,
            "realLeverage": real_lev,
            "liquidation": liq,
            "unrealized": upnl,
            "unrealizedPct": (upnl / margin * 100) if margin > 0 else 0.0,
            "realized": p.get("realized", 0.0),
            "src": p.get("src", "history"),
        })
    return out
