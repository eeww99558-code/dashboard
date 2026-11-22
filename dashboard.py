#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
币安跟单监控 · 可视化面板

运行：python dashboard.py（或双击 启动面板.bat）
打开：http://127.0.0.1:8899

不引入新依赖（只用标准库 + 已有 requests），复用 monitor.py 的接口层。
与命令行监控器共用 config.json，但使用独立的 dashboard_state.json 基线，互不干扰。
"""

import json
import logging
import sys
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import monitor as M
import positions as P
import stats as S
import lifecycle as L

HERE = Path(__file__).resolve().parent
import os
# 云端（Altare 等 PaaS）通过 PORT 注入端口并绑定 0.0.0.0；本地默认 8899/127.0.0.1
PORT = int(os.environ.get("PORT", "8899"))
BIND_HOST = os.environ.get("BIND_HOST", "0.0.0.0" if os.environ.get("ALTARE") or os.environ.get("PORT") else "127.0.0.1")

log = logging.getLogger("dashboard")
log.setLevel(logging.INFO)
_h = logging.StreamHandler(sys.stdout)
_h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S"))
log.addHandler(_h)

HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>币安跟单监控面板</title>
<style>
  :root{
    /* 60 主导深色底 / 30 面板层 / 10 强调——交易终端风 */
    --bg:#0b0f17; --panel:#121826; --panel2:#171f31; --card:#1a2234;
    --ink:#e8eef9; --ink2:#aab6cc; --muted:#6f7d96;
    --line:#263149; --line2:#303d59;
    --brand:#4d8dff; --brand-soft:rgba(77,141,255,.14);
    --up:#ff4d5e; --up-soft:rgba(255,77,94,.13);   /* 中国习惯：涨红 */
    --down:#12c48b; --down-soft:rgba(18,196,139,.13); /* 跌绿 */
    --warn:#f5a623;
    --shadow:0 4px 20px rgba(0,0,0,.35);
    --mono:"SF Mono",Consolas,"Roboto Mono",Menlo,monospace;
  }
  *{box-sizing:border-box;margin:0;padding:0}
  body{background:var(--bg);color:var(--ink);
    font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif;
    font-size:14px;line-height:1.5;background-image:radial-gradient(1200px 500px at 15% -10%,rgba(77,141,255,.07),transparent 60%)}
  /* 左侧固定导航（参考专业终端） */
  .app{display:flex;min-height:100vh}
  .side{width:64px;flex:0 0 64px;background:#0d1421;border-right:1px solid var(--line);
    position:sticky;top:0;height:100vh;display:flex;flex-direction:column;align-items:center;
    padding:14px 0;gap:4px;z-index:5}
  .side .brand{width:38px;height:38px;border-radius:10px;background:linear-gradient(135deg,#4d8dff,#2b5fd6);
    display:grid;place-items:center;color:#fff;font-weight:800;font-size:18px;margin-bottom:14px;
    box-shadow:0 4px 16px rgba(77,141,255,.35)}
  .nav{width:100%;display:flex;flex-direction:column;align-items:center;gap:2px}
  .nav a{width:52px;padding:9px 0;border-radius:9px;display:flex;flex-direction:column;
    align-items:center;gap:3px;color:var(--muted);text-decoration:none;font-size:9.5px;
    transition:.15s;border:1px solid transparent}
  .nav a .ic{font-size:17px;line-height:1}
  .nav a:hover{background:var(--panel2);color:var(--ink2)}
  .nav a.on{background:var(--brand-soft);color:var(--brand);border-color:rgba(77,141,255,.3)}
  .side-foot{margin-top:auto;font-size:9px;color:var(--muted);writing-mode:vertical-rl;letter-spacing:2px}
  .main{flex:1;min-width:0;padding:18px 20px 60px;max-width:1600px}
  .wrap{max-width:100%;margin:0}
  header{display:flex;align-items:center;gap:14px;margin-bottom:18px;flex-wrap:wrap}
  .logo{width:42px;height:42px;border-radius:11px;
    background:linear-gradient(135deg,#4d8dff,#2b5fd6);
    display:grid;place-items:center;color:#fff;font-weight:800;font-size:20px;
    box-shadow:0 4px 16px rgba(77,141,255,.35)}
  h1{font-size:20px;font-weight:800;letter-spacing:.3px}
  .sub{color:var(--muted);font-size:11.5px;margin-top:2px}
  .sub b{color:var(--brand)}
  .badge{margin-left:auto;display:flex;align-items:center;gap:8px;padding:7px 14px;
    background:var(--panel);border:1px solid var(--line);border-radius:22px;
    font-size:12px;color:var(--ink2)}
  .dot{width:9px;height:9px;border-radius:50%;background:var(--muted)}
  .dot.ok{background:var(--down);box-shadow:0 0 0 4px var(--down-soft)}
  .dot.err{background:var(--up)} .dot.warn{background:var(--warn)}
  /* 交易员身份条 */
  .trader-card{background:linear-gradient(135deg,#141d2f 0%,#111827 100%);
    border:1px solid var(--line);border-radius:16px;padding:18px 20px;margin-bottom:16px;
    box-shadow:var(--shadow)}
  .trader{display:flex;align-items:center;gap:14px;flex-wrap:wrap}
  .avatar{width:52px;height:52px;border-radius:50%;object-fit:cover;background:var(--panel2);
    border:2px solid var(--line2)}
  .tname{font-size:19px;font-weight:800}
  .tmeta{color:var(--muted);font-size:11.5px;margin-top:3px;font-family:var(--mono)}
  .tag{display:inline-block;padding:3px 9px;border-radius:6px;background:var(--brand-soft);
    color:var(--brand);font-size:11px;font-weight:600;margin-left:8px}
  .tag.warn{background:rgba(245,166,35,.14);color:var(--warn)}
  /* 核心 KPI 大数卡 */
  .kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));
    gap:12px;margin-top:18px}
  .kpi{background:var(--panel2);border:1px solid var(--line);border-radius:12px;padding:14px 16px;
    position:relative;overflow:hidden}
  .kpi::after{content:'';position:absolute;left:0;top:0;bottom:0;width:3px;background:var(--brand);opacity:.7}
  .kpi.up::after{background:var(--up)} .kpi.down::after{background:var(--down)}
  .kpi .k{color:var(--muted);font-size:11px;letter-spacing:.3px}
  .kpi .v{font-size:22px;font-weight:800;margin-top:6px;font-variant-numeric:tabular-nums;
    font-family:var(--mono);letter-spacing:-.5px}
  .kpi .s{font-size:10.5px;color:var(--muted);margin-top:3px}
  .note{color:var(--muted);font-size:11px;margin-top:12px;font-family:var(--mono)}
  /* 通用卡片 */
  .card{background:var(--panel);border:1px solid var(--line);border-radius:14px;
    padding:18px 20px;margin-bottom:16px;box-shadow:var(--shadow)}
  .sec-title{font-weight:700;font-size:15px;margin-bottom:14px;display:flex;align-items:center;gap:9px}
  .sec-title .ic{width:22px;height:22px;border-radius:6px;background:var(--brand-soft);
    color:var(--brand);display:grid;place-items:center;font-size:13px}
  .sec-title small{font-weight:400;color:var(--muted);font-size:11px;margin-left:2px}
  .sec-title .right{margin-left:auto;font-size:11px;color:var(--muted)}
  /* 当前持仓卡（截图式，字段对齐告警模板） */
  .pos{border:1px solid var(--line2);border-radius:13px;padding:0;margin-bottom:14px;
    background:linear-gradient(135deg,#192234 0%,#151d2e 100%);overflow:hidden}
  .pos-head{display:flex;align-items:center;gap:10px;flex-wrap:wrap;padding:13px 16px;
    border-bottom:1px solid var(--line);background:rgba(255,255,255,.02)}
  .pos-sym{font-size:17px;font-weight:800;letter-spacing:.4px;font-family:var(--mono)}
  .pos-side{font-size:11px;font-weight:800;padding:4px 10px;border-radius:6px}
  .pos-side.short{background:var(--up-soft);color:var(--up)}
  .pos-side.long{background:var(--down-soft);color:var(--down)}
  .pos-chip{font-size:10.5px;color:var(--ink2);background:var(--panel2);
    border:1px solid var(--line);padding:3px 9px;border-radius:6px}
  .pos-chip.warn{color:var(--warn);border-color:rgba(245,166,35,.4)}
  .pos-head .px{margin-left:auto;font-size:11.5px;color:var(--muted);font-family:var(--mono)}
  .pos-head .px b{color:var(--ink);font-size:14px}
  .pos-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:1px;
    background:var(--line)}
  .pf{background:#151d2e;padding:11px 14px}
  .pf .k{color:var(--muted);font-size:10.5px;letter-spacing:.2px}
  .pf .v{font-size:14.5px;font-weight:700;margin-top:4px;font-variant-numeric:tabular-nums;
    font-family:var(--mono)}
  .pf.hl{background:#1a2636}
  .up{color:var(--up)!important} .down{color:var(--down)!important}
  .pos-foot{display:flex;gap:18px;flex-wrap:wrap;padding:11px 16px;background:#121a2a;
    border-top:1px solid var(--line);font-size:11.5px}
  .pos-foot b{font-family:var(--mono);color:var(--ink)}
  /* 统计仪表盘 */
  .stat-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(132px,1fr));gap:11px}
  .st{background:var(--panel2);border:1px solid var(--line);border-radius:11px;padding:13px 14px}
  .st .k{color:var(--muted);font-size:10.5px}
  .st .v{font-size:19px;font-weight:800;margin-top:5px;font-family:var(--mono);
    font-variant-numeric:tabular-nums}
  .bar-row{margin-top:14px}
  .bar-lab{display:flex;justify-content:space-between;font-size:11px;color:var(--ink2);margin-bottom:5px}
  .bar{height:8px;border-radius:5px;background:var(--line);overflow:hidden;display:flex}
  .bar .w{background:var(--up)} .bar .l{background:var(--down)}
  /* 表格 */
  table{width:100%;border-collapse:collapse;font-size:12px;font-variant-numeric:tabular-nums;
    font-family:var(--mono)}
  th,td{text-align:left;padding:9px 10px;border-bottom:1px solid var(--line);white-space:nowrap}
  th{color:var(--muted);font-weight:600;font-size:10.5px;background:var(--panel2);
    position:sticky;top:0;z-index:1;letter-spacing:.3px}
  tbody tr:hover{background:rgba(77,141,255,.06)}
  td.num{text-align:right}
  .scroll{max-height:420px;overflow:auto;border-radius:10px;border:1px solid var(--line)}
  .scroll-lg{max-height:560px;overflow:auto;border-radius:10px;border:1px solid var(--line)}
  .empty{color:var(--muted);text-align:center;padding:30px 10px;font-size:12.5px;font-family:inherit}
  /* 告警 */
  .ev{border:1px solid var(--line);border-left:3px solid var(--muted);border-radius:10px;
    padding:12px 14px;margin-bottom:10px;background:var(--panel2)}
  .ev.open-long{border-left-color:var(--up)} .ev.open-short{border-left-color:var(--down)}
  .ev.close{border-left-color:var(--muted)}
  .ev-head{display:flex;align-items:center;gap:9px;flex-wrap:wrap;margin-bottom:6px}
  .ev-tag{font-size:11px;font-weight:800;padding:3px 8px;border-radius:6px;background:var(--panel)}
  .ev.open-long .ev-tag{background:var(--up-soft);color:var(--up)}
  .ev.open-short .ev-tag{background:var(--down-soft);color:var(--down)}
  .ev-sym{font-weight:700;font-family:var(--mono)}
  .ev-time{margin-left:auto;color:var(--muted);font-size:11px;font-family:var(--mono)}
  .ev-body{color:var(--ink2);font-size:12px;font-variant-numeric:tabular-nums;font-family:var(--mono)}
  .ev-body b{color:var(--ink)}
  .calc-note{font-size:10.5px;color:var(--muted);margin-top:10px;line-height:1.6}
  /* ===== 仓位生命周期时间线（主角）===== */
  .legend{display:flex;gap:14px;flex-wrap:wrap;font-size:11px;color:var(--ink2);margin-bottom:16px}
  .legend span{display:inline-flex;align-items:center;gap:5px}
  .legend i{width:10px;height:10px;border-radius:3px;display:inline-block}
  .lg-open{background:var(--brand)} .lg-add{background:#a06bff}
  .lg-reduce{background:var(--warn)} .lg-close{background:var(--muted)}
  .life{border:1px solid var(--line2);border-radius:13px;margin-bottom:15px;overflow:hidden;
    background:linear-gradient(135deg,#182133 0%,#141c2c 100%)}
  .life-head{display:flex;align-items:center;gap:10px;flex-wrap:wrap;padding:13px 16px;
    border-bottom:1px solid var(--line);background:rgba(255,255,255,.02)}
  .life-sym{font-size:16px;font-weight:800;font-family:var(--mono);letter-spacing:.4px}
  .life-side{font-size:10.5px;font-weight:800;padding:3px 9px;border-radius:5px}
  .life-side.short{background:var(--up-soft);color:var(--up)}
  .life-side.long{background:var(--down-soft);color:var(--down)}
  .life-meta{font-size:10.5px;color:var(--muted);font-family:var(--mono)}
  .life-pnl{margin-left:auto;text-align:right;font-family:var(--mono)}
  .life-pnl .v{font-size:19px;font-weight:800;line-height:1.1}
  .life-pnl .p{font-size:10.5px;color:var(--muted)}
  .life-sum{display:flex;gap:20px;flex-wrap:wrap;padding:10px 16px;font-size:11px;
    color:var(--ink2);border-bottom:1px solid var(--line)}
  .life-sum b{font-family:var(--mono);color:var(--ink)}
  .life-sum .src{color:var(--muted)}
  .tl{display:flex;align-items:stretch;padding:18px 16px 20px;overflow-x:auto;gap:0}
  .tl-step{position:relative;flex:1;min-width:150px;text-align:center;padding:0 8px}
  .tl-step::before{content:'';position:absolute;top:15px;left:-50%;width:100%;height:2px;
    background:var(--line2);z-index:0}
  .tl-step:first-child::before{display:none}
  .tl-step.done::before{background:linear-gradient(90deg,var(--line2),var(--brand))}
  .tl-dot{position:relative;z-index:1;width:32px;height:32px;border-radius:50%;margin:0 auto 9px;
    display:grid;place-items:center;font-size:13px;font-weight:800;color:#fff;
    border:3px solid var(--panel);box-shadow:0 0 0 2px var(--line2)}
  .tl-dot.open{background:var(--brand)} .tl-dot.add{background:#a06bff}
  .tl-dot.reduce{background:var(--warn)} .tl-dot.close{background:#7b879e}
  .tl-kind{font-size:12px;font-weight:700;margin-bottom:4px}
  .tl-time{font-size:10px;color:var(--muted);font-family:var(--mono);margin-bottom:6px;line-height:1.4}
  .tl-detail{font-size:10.5px;color:var(--ink2);font-family:var(--mono);line-height:1.6}
  .tl-detail b{color:var(--ink)}
  .anchor-tag{font-size:9px;color:var(--muted);border:1px dashed var(--line2);
    border-radius:4px;padding:0 4px;margin-left:4px}
  .empty-life{color:var(--muted);text-align:center;padding:26px;font-size:12.5px}
</style>
</head>
<body>
<div class="app">
  <!-- 左侧固定导航 -->
  <aside class="side">
    <div class="brand">鹰</div>
    <nav class="nav">
      <a href="#overview" class="on"><span class="ic">◈</span>总览</a>
      <a href="#positions"><span class="ic">◎</span>持仓</a>
      <a href="#lifecycle"><span class="ic">⟿</span>生命周期</a>
      <a href="#stats"><span class="ic">▤</span>统计</a>
      <a href="#orders"><span class="ic">▦</span>订单</a>
      <a href="#recent"><span class="ic">↗</span>成交</a>
      <a href="#events"><span class="ic">◉</span>告警</a>
    </nav>
    <div class="side-foot">COPY TRADING TERMINAL</div>
  </aside>

  <main class="main">
  <div class="wrap">
  <header>
    <div>
      <h1>熬鹰资本 · 实盘跟单监控</h1>
      <div class="sub">币安合约 U 本位 · 公开 bapi 数据 · 自动刷新 · <b>v6 专业终端版</b></div>
    </div>
    <div class="badge"><span class="dot" id="dot"></span><span id="status">连接中…</span></div>
  </header>

  <!-- 交易员身份 + 账户核心指标 -->
  <div class="trader-card" id="overview">
    <div class="trader">
      <img class="avatar" id="avatar" alt="">
      <div>
        <div class="tname"><span id="nickname">—</span><span class="tag" id="tagLive">LIVE</span></div>
        <div class="tmeta">portfolioId <span id="pid">—</span></div>
      </div>
    </div>
    <div class="kpis" id="kpis"></div>
    <div class="note" id="lastUpd">—</div>
  </div>

  <!-- 当前持仓（完全对齐告警模板字段） -->
  <div class="card" id="positions">
    <div class="sec-title"><span class="ic">◎</span> 当前全部持仓 <small id="posCount">实时盯盘 · 公开数据还原</small>
      <span class="right" id="posTotals"></span></div>
    <div id="positionsBox"><div class="empty">加载中…</div></div>
    <div class="calc-note" id="posNote"></div>
  </div>

  <!-- 仓位生命周期时间线（主角） -->
  <div class="card" id="lifecycle">
    <div class="sec-title"><span class="ic">⟿</span> 仓位生命周期 <small>每笔仓位：开仓 → 加仓 → 减仓 → 平仓</small>
      <span class="right" id="lifeCount"></span></div>
    <div class="legend">
      <span><i class="lg-open"></i>开仓：首次建立仓位</span>
      <span><i class="lg-add"></i>加仓：同方向继续加码</span>
      <span><i class="lg-reduce"></i>减仓：部分平掉、仍有持仓</span>
      <span><i class="lg-close"></i>平仓：全部了结、锁定盈亏</span>
    </div>
    <div id="lifeBox"><div class="empty-life">加载中…</div></div>
  </div>

  <!-- 历史战绩统计 -->
  <div class="card" id="stats">
    <div class="sec-title"><span class="ic">▤</span> 历史战绩统计 <small id="statRange">已平仓记录计算</small></div>
    <div class="stat-grid" id="statGrid"></div>
    <div class="bar-row" id="winBar"></div>
    <div class="calc-note" id="statNote"></div>
  </div>

  <!-- 历史订单 -->
  <div class="card" id="orders">
    <div class="sec-title"><span class="ic">▦</span> 历史订单 <small>已平仓逐笔明细 · 含持仓时长</small>
      <span class="right" id="orderCount"></span></div>
    <div class="scroll-lg"><table>
      <thead><tr>
        <th>开仓时间</th><th>平仓时间</th><th>标的</th><th>方向</th><th class="num">杠杆</th>
        <th class="num">开仓价</th><th class="num">平仓价</th><th class="num">数量</th>
        <th class="num">持仓</th><th class="num">盈亏(U)</th><th class="num">ROI</th>
      </tr></thead>
      <tbody id="ordersBody"></tbody>
    </table></div>
  </div>

  <!-- 最近成交 -->
  <div class="card" id="recent">
    <div class="sec-title"><span class="ic">↗</span> 最近成交 <small id="recentCount">公开接口最近 100 笔</small></div>
    <div class="scroll-lg"><table>
      <thead><tr>
        <th>时间</th><th>标的</th><th>方向</th><th class="num">价格</th><th class="num">数量</th>
        <th class="num">名义(U)</th><th class="num">已实现(U)</th><th class="num">手续费</th><th>主动</th>
      </tr></thead>
      <tbody id="recentBody"></tbody>
    </table></div>
  </div>

  <!-- 实时告警 -->
  <div class="card" id="events">
    <div class="sec-title"><span class="ic">◉</span> 实时告警 <small id="evCount"></small></div>
    <div id="eventsBox"><div class="empty">等待新事件（首次运行只建基线）</div></div>
  </div>
  </div>
  </main>
</div>

<script>
const $ = id => document.getElementById(id);
const fmt = (v,d=2) => v==null ? '—' : Number(v).toLocaleString('en-US',{minimumFractionDigits:d,maximumFractionDigits:d});
const tm = ms => ms ? new Date(ms).toLocaleString('zh-CN',{hour12:false}) : '—';

function direction(side,ps){
  if(side==='BUY'&&ps==='LONG')   return {txt:'开多',cls:'open-long'};
  if(side==='SELL'&&ps==='SHORT') return {txt:'开空',cls:'open-short'};
  if(side==='SELL'&&ps==='LONG')  return {txt:'平多',cls:'close'};
  if(side==='BUY'&&ps==='SHORT')  return {txt:'平空',cls:'close'};
  return {txt:side+'/'+ps,cls:'close'};
}

function renderStats(d){
  const det=d.detail||{}, p=d.performance||{};
  const items=[
    ['账户权益', fmt(det.marginBalance)+' U','',''],
    ['账户 AUM', fmt(det.aumAmount)+' U','',''],
    [p.timeRange?('近'+p.timeRange.replace('D','天')+' ROI'):'ROI',
      (Number(p.roi)>=0?'+':'')+fmt(p.roi)+'%', Number(p.roi)>=0?'up':'down',''],
    ['区间盈亏', (Number(p.pnl)>=0?'+':'')+fmt(p.pnl)+' U', Number(p.pnl)>=0?'up':'down',''],
    ['胜率', fmt(p.winRate)+'%','',''],
    ['最大回撤', fmt(p.mdd)+'%','up',''],
    ['跟单人数', (det.currentCopyCount??'—'),'',''],
    ['分润比例', det.profitSharingRate?det.profitSharingRate+'%':'—','']
  ];
  $('kpis').innerHTML=items.map(([k,v,c])=>
    `<div class="kpi ${c}"><div class="k">${k}</div><div class="v ${c}">${v}</div></div>`).join('');
  $('tagLive').textContent = det.positionShow ? '持仓展示:开' : '持仓展示:关';
  $('tagLive').className = 'tag'+(det.positionShow?'':' warn');
}

const dur = ms => {
  if(!ms||ms<=0) return '—';
  const m=Math.round(ms/60000);
  if(m<60) return m+'分';
  const h=Math.floor(m/60), mm=m%60;
  if(h<24) return h+'时'+mm+'分';
  return Math.floor(h/24)+'天'+ (h%24) +'时';
};

function renderPositions(list, note){
  $('posNote').textContent = note||'';
  const box=$('positionsBox');
  if(!list||!list.length){box.innerHTML='<div class="empty">当前无未平仓仓位</div>';
    $('posCount').textContent='持仓数 0'; $('posTotals').textContent=''; return;}
  // 汇总：总保证金 / 总浮盈亏 / 总已实现
  const tMargin=list.reduce((s,p)=>s+(Number(p.margin)||0),0);
  const tUpnl=list.reduce((s,p)=>s+(Number(p.unrealized)||0),0);
  const tReal=list.reduce((s,p)=>s+(Number(p.realized)||0),0);
  const tCls=tUpnl>=0?'up':'down';
  $('posCount').textContent='持仓数 '+list.length;
  $('posTotals').innerHTML=`总保证金 <b style="font-family:var(--mono)">${fmt(tMargin,0)} U</b> · `+
    `总浮动 <b class="${tCls}" style="font-family:var(--mono)">${tUpnl>=0?'+':''}${fmt(tUpnl,0)} U</b> · `+
    `总已实现 <b class="${tReal>=0?'up':'down'}" style="font-family:var(--mono)">${tReal>=0?'+':''}${fmt(tReal,0)} U</b>`;
  box.innerHTML=list.map(p=>{
    const upnl=p.unrealized||0;
    const pnlCls=Math.abs(upnl)<1e-6?'':upnl>0?'up':'down';
    const short=p.short;
    const lev=p.leverage||1;
    // 强平距离：现价到强平价的百分比（做空：强平在上方）
    const px=p.mark||p.price||p.avgCost, liq=p.liquidation;
    let dist='—';
    if(px&&liq&&px>0){
      const d=short?(liq-px)/px*100:(px-liq)/px*100;
      dist=(d>=0?'':'-')+Math.abs(d).toFixed(2)+'%';
    }
    const base=(p.symbol||'').replace('USDT','');
    return `<div class="pos">
      <div class="pos-head">
        <span class="pos-sym">${p.symbol||''}</span>
        <span class="pos-side ${short?'short':'long'}">${p.side||''} ${short?'🟥':'🟩'}</span>
        <span class="pos-chip">永续 · ${p.marginType||''} · ${fmt(lev,0)}x</span>
        ${p.src==='rebuilt'?'<span class="pos-chip warn">成交流重建·杠杆估算</span>':''}
        <span class="px">现价 <b>$${fmt(p.price)}</b> · 标记 <b>$${fmt(p.mark)}</b></span>
      </div>
      <div class="pos-grid">
        <div class="pf"><div class="k">持仓量</div><div class="v">${fmt(p.qty,2)} ${base}</div></div>
        <div class="pf"><div class="k">开仓均价</div><div class="v">$${fmt(p.avgCost)}</div></div>
        <div class="pf"><div class="k">名义价值</div><div class="v">${fmt(p.notional,0)} U</div></div>
        <div class="pf"><div class="k">保证金(全仓)</div><div class="v">${fmt(p.margin,0)} U</div></div>
        <div class="pf"><div class="k">实际杠杆</div><div class="v">${fmt(p.realLeverage,2)}x</div></div>
        <div class="pf"><div class="k">强平价(估)</div><div class="v">$${fmt(liq)}</div></div>
        <div class="pf"><div class="k">强平距离</div><div class="v">${dist}</div></div>
        <div class="pf hl"><div class="k">浮动收益</div><div class="v ${pnlCls}">${upnl>=0?'+':''}${fmt(upnl,0)} U</div></div>
        <div class="pf hl"><div class="k">浮动收益率</div><div class="v ${pnlCls}">${p.unrealizedPct>=0?'+':''}${fmt(p.unrealizedPct,2)}%</div></div>
        <div class="pf"><div class="k">已实现盈亏</div><div class="v ${(p.realized||0)>=0?'up':'down'}">${(p.realized||0)>=0?'+':''}${fmt(p.realized,0)} U</div></div>
      </div>
    </div>`;
  }).join('');
}

function renderStatDashboard(s){
  const grid=$('statGrid');
  if(!s||!s.has_data){grid.innerHTML='<div class="empty">暂无已平仓记录</div>';return;}
  const items=[
    ['累计已实现', (s.totalPnl>=0?'+':'')+fmt(s.totalPnl,0)+' U', s.totalPnl>=0?'up':'down'],
    ['胜率', fmt(s.winRate,1)+'%', s.winRate>=50?'up':'down'],
    ['盈亏比', s.profitFactor==null?'—':fmt(s.profitFactor,2), s.profitFactor>=1?'up':'down'],
    ['已平仓笔数', String(s.total), ''],
    ['盈利 / 亏损', s.wins+' / '+s.losses, ''],
    ['单笔最佳', '+'+fmt(s.best,0)+' U', 'up'],
    ['单笔最差', fmt(s.worst,0)+' U', 'down'],
    ['平均盈亏', (s.avgPnl>=0?'+':'')+fmt(s.avgPnl,0)+' U', s.avgPnl>=0?'up':'down'],
    ['平均持仓', s.avgHoldMin==null?'—':dur(s.avgHoldMin*60000), ''],
    ['最长持仓', s.maxHoldMin==null?'—':dur(s.maxHoldMin*60000), ''],
    ['最高连胜', s.maxWinStreak+' 笔', 'up'],
    ['最高连亏', s.maxLossStreak+' 笔', 'down'],
  ];
  grid.innerHTML=items.map(([k,v,c])=>
    `<div class="st"><div class="k">${k}</div><div class="v ${c}">${v}</div></div>`).join('');
  const w=s.wins, l=s.losses, tot=w+l||1;
  $('winBar').innerHTML=`<div class="bar-lab"><span>胜 ${w} 笔（${fmt(s.winRate,1)}%）</span>`+
    `<span>亏 ${l} 笔（${fmt(100-s.winRate,1)}%）</span></div>`+
    `<div class="bar"><div class="w" style="width:${w/tot*100}%"></div>`+
    `<div class="l" style="width:${l/tot*100}%"></div></div>`;
  const top=(s.bySymbol||[]).slice(0,5).map(x=>`${x.symbol.replace('USDT','')} ${x.pnl>=0?'+':''}${fmt(x.pnl,0)}`).join(' · ');
  $('statNote').textContent='🎯 盈亏贡献 Top5：'+(top||'—')+'　｜　统计基于公开 position-history 的 '+s.total+' 笔已平仓记录，数据截止 '+tm(s.asOf);
}

function renderEvents(events){
  $('evCount').textContent = events.length ? `共 ${events.length} 条` : '';
  if(!events.length){$('eventsBox').innerHTML='<div class="empty">等待新事件（首次运行只建基线）</div>';return;}
  $('eventsBox').innerHTML=events.map(e=>{
    if(e.kind==='TRADE'){
      const d=direction(e.side,e.positionSide);
      return `<div class="ev ${d.cls}"><div class="ev-head">
        <span class="ev-tag">${d.txt}</span><span class="ev-sym">${e.symbol||''}</span>
        <span class="ev-time">${tm(e.time)}</span></div>
        <div class="ev-body">方向 <b>${e.side}/${e.positionSide||''}</b> ·
        数量 <b>${e.qty??'—'} ${e.base||''}</b> ·
        价格 <b>$${fmt(e.price)}</b> ·
        名义 <b>${fmt(e.notional)} U</b></div></div>`;
    }
    let roi='—'; try{roi=(Number(e.roi)*100).toFixed(2)+'%'}catch(_){}
    const pnl=Number(e.pnl||0);
    return `<div class="ev close"><div class="ev-head">
      <span class="ev-tag">平仓</span><span class="ev-sym">${e.symbol||''}</span>
      <span class="ev-time">${tm(e.time)}</span></div>
      <div class="ev-body">${e.side||''} ${e.leverage||''}x ·
      开 <b>$${fmt(e.openPrice)}</b> → 平 <b>$${fmt(e.closePrice)}</b> ·
      盈亏 <b class="${pnl>=0?'up':'down'}">${pnl>=0?'+':''}${fmt(pnl)} U</b> ·
      ROI <b class="${pnl>=0?'up':'down'}">${roi}</b></div></div>`;
  }).join('');
}

function renderRecent(rows){
  $('recentCount').textContent = `公开接口最近 ${rows.length} 笔`;
  if(!rows.length){$('recentBody').innerHTML='<tr><td colspan="9" class="empty">暂无数据</td></tr>';return;}
  $('recentBody').innerHTML=rows.map(t=>{
    const d=direction(t.side,t.positionSide);
    const realize=Number(t.realizedProfit||0);
    const fee=Number(t.fee||0);
    const ru = Math.abs(realize)<1e-9?'':realize>0?'up':'down';
    return `<tr>
      <td>${tm(t.time)}</td>
      <td>${t.symbol||''}</td>
      <td class="${d.cls==='open-long'?'up':d.cls==='open-short'?'down':''}">${d.txt}</td>
      <td class="num">${fmt(t.price)}</td>
      <td class="num">${fmt(t.qty,4)}</td>
      <td class="num">${fmt(t.quantity,0)}</td>
      <td class="num ${ru}">${Math.abs(realize)<1e-9?'—':(realize>=0?'+':'')+fmt(realize)}</td>
      <td class="num">${fmt(fee)}</td>
      <td>${t.activeBuy?'主动':'被动'}</td></tr>`;
  }).join('');
}

function renderOrders(rows){
  $('orderCount').textContent = rows.length ? `共 ${rows.length} 条` : '';
  if(!rows.length){$('ordersBody').innerHTML='<tr><td colspan="11" class="empty">暂无平仓记录</td></tr>';return;}
  $('ordersBody').innerHTML=rows.map(o=>{
    const short=String(o.side||'').toLowerCase()==='short';
    const pnl=Number(o.closingPnl||0);
    let roi='—'; try{roi=(Number(o.roi)*100).toFixed(2)+'%'}catch(_){}
    const pc=pnl>=0?'up':'down';
    const qty=Number(o.maxOpenInterest||0);
    const holding=(o.closed&&o.opened)?dur(o.closed-o.opened):'—';
    const partial=String(o.status||'').toLowerCase().startsWith('part');
    return `<tr>
      <td>${tm(o.opened)}</td>
      <td>${o.closed?tm(o.closed):'持仓中'}</td>
      <td>${o.symbol||''}</td>
      <td class="${short?'up':'down'}">${short?'做空':'做多'}</td>
      <td class="num">${o.leverage||'—'}x</td>
      <td class="num">${fmt(o.avgCost)}</td>
      <td class="num">${o.avgClosePrice?fmt(o.avgClosePrice):'—'}</td>
      <td class="num">${fmt(qty,3)}</td>
      <td class="num">${holding}</td>
      <td class="num ${pc}">${pnl>=0?'+':''}${fmt(pnl,0)}</td>
      <td class="num ${pc}">${roi}</td></tr>`;
  }).join('');
}

const tlIcon={'开仓':'◈','加仓':'+','减仓':'−','平仓':'✓'};
const tlCls={'开仓':'open','加仓':'add','减仓':'reduce','平仓':'close'};
function renderLife(life){
  const box=$('lifeBox');
  const items=(life&&life.items)||[];
  $('lifeCount').textContent = items.length ? `${life.count} 笔仓位 · 累计已实现 ${(life.totalPnl>=0?'+':'')}${fmt(life.totalPnl,0)} U` : '';
  if(!items.length){box.innerHTML='<div class="empty-life">暂无仓位记录</div>';return;}
  box.innerHTML=items.map(p=>{
    const pnl=p.pnl||0, pc=pnl>=0?'up':'down';
    const short=p.sideRaw==='Short';
    const steps=(p.actions||[]).map(a=>{
      const cls=tlCls[a.kind]||'close';
      const icon=tlIcon[a.kind]||'•';
      const cnt=a.count?` · ${a.count}笔成交`:'';
      const anchor=a.anchor?'<span class="anchor-tag">端点</span>':'';
      const rp=(a.realized&&Math.abs(a.realized)>0.01)?`<br>已实现 <b class="${a.realized>=0?'up':'down'}">${a.realized>=0?'+':''}${fmt(a.realized,0)} U</b>`:'';
      return `<div class="tl-step done">
        <div class="tl-dot ${cls}">${icon}</div>
        <div class="tl-kind">${a.kind}${anchor}</div>
        <div class="tl-time">${a.timeStr||'—'}</div>
        <div class="tl-detail">价 <b>$${fmt(a.price)}</b><br>量 <b>${fmt(a.qty,3)}</b>${cnt}${rp}</div>
      </div>`;
    }).join('');
    const hold=p.holdMs?dur(p.holdMs):'持仓中';
    return `<div class="life">
      <div class="life-head">
        <span class="life-sym">${p.symbol||''}</span>
        <span class="life-side ${short?'short':'long'}">${p.side}${short?' 🟥':' 🟩'}</span>
        <span class="life-meta">${p.leverage||'—'}x · ${p.marginType||''} · ${p.status||''}</span>
        <span class="life-pnl"><div class="v ${pc}">${pnl>=0?'+':''}${fmt(pnl,0)} U</div>
          <div class="p">${p.roiPct>=0?'+':''}${fmt(p.roiPct,2)}% ·${p.closed?'已平':'未平'}</div></span>
      </div>
      <div class="life-sum">
        <span>开仓 <b>${p.openedStr||'—'}</b></span>
        <span>${p.closed?'平仓 <b>'+p.closedStr+'</b>':'<b>持仓中</b>'}</span>
        <span>持仓时长 <b>${hold}</b></span>
        <span>峰值 <b>${fmt(p.peakQty,3)}</b></span>
        <span>已平 <b>${fmt(p.closedQty,3)}</b></span>
        ${!p.closed?`<span>剩余 <b>${fmt(p.remainQty,3)}</b></span>`:''}
        <span class="src">${p.source||''}</span>
      </div>
      <div class="tl">${steps}</div>
    </div>`;
  }).join('');
}

async function load(){
  try{
    const r=await fetch('/api/state',{cache:'no-store'});
    const d=await r.json();
    $('dot').className='dot '+(d.status==='ok'?'ok':d.status==='error'?'err':'');
    $('status').textContent = d.status==='ok'?'实时连接':d.status==='error'?'接口异常':d.status==='degraded'?'接口降频(价格实时)':'连接中…';
    $('nickname').textContent=d.nickname||'—';
    $('pid').textContent=d.pid||'—';
    if(d.account&&(d.account.detail||d.account.performance)) renderStats(d.account);
    renderPositions(d.positions||[], d.posNote||'');
    renderEvents(d.events||[]);
    renderRecent(d.recent||[]);
    renderOrders(d.orders||[]);
    renderStatDashboard(d.stats||{});
    renderLife(d.lifecycle||{});
    // 导航高亮跟随滚动
    const secs=['overview','positions','lifecycle','stats','orders','recent','events'];
    let cur='overview';
    for(const id of secs){const el=$(id);if(el&&el.getBoundingClientRect().top<160)cur=id;}
    document.querySelectorAll('.nav a').forEach(a=>a.classList.toggle('on',a.getAttribute('href')==='#'+cur));
    let upd = d.last ? '面板心跳：'+tm(d.last)+' · 第'+(d.round||0)+'轮' : '尚未获取数据';
    if(d.data_last) upd += ' · 最新成交：'+tm(d.data_last);
    $('lastUpd').textContent = upd;
    if(d.error) $('lastUpd').textContent += ' · '+d.error;
  }catch(e){$('status').textContent='面板离线';$('dot').className='dot err';}
}
load(); setInterval(load,5000);
</script>
</body>
</html>"""


class Dashboard:
    """后台轮询 + 内存缓存，HTTP 只负责吐 JSON。"""

    def __init__(self, cfg):
        self.cfg = cfg
        self.pid = str(cfg["portfolio_id"])
        self.nickname = cfg.get("nickname", "交易员")
        self.api = M.PublicAPI()
        self.det = M.Detector(str(HERE / "dashboard_state.json"), log)
        self.events = deque(maxlen=80)
        self.recent = []
        self.orders = []
        self.stats = {}
        self.lifecycle = {}
        self.account = {}
        self.positions = []
        self.pos_note = ""
        self.status = "connecting"
        self.error = ""
        self.last = 0
        self._fail_streak = 0
        self.size = int(cfg.get("history_size", 100))
        self.order_size = int(cfg.get("order_size", 50))
        self.interval = max(2, int(cfg.get("poll_interval", 3)))
        # 账户详情/业绩变化慢，降频拉取，避免触发币安限流（code=11012005）
        self.account_every = max(1, int(cfg.get("account_refresh_rounds", 10)))
        self.position_every = max(1, int(cfg.get("position_refresh_rounds", 3)))
        self.new_trade_pages = max(1, int(cfg.get("new_trade_pages", 4)))
        # 生命周期聚合需要深拉多页成交，重且慢，低频刷新（默认每 6 轮）
        self.lifecycle_every = max(1, int(cfg.get("lifecycle_refresh_rounds", 6)))
        self.lifecycle_pages = max(1, int(cfg.get("lifecycle_trade_pages", 15)))
        self._round = 0

    def _safe(self, fn, *args):
        """单接口容错：限流重试一次，仍失败返回 None，不影响其他接口和主循环。"""
        for attempt in range(2):
            try:
                return fn(*args)
            except Exception as e:
                msg = str(e)
                if attempt == 0 and ("11012005" in msg or "busy" in msg.lower()):
                    time.sleep(2)
                    continue
                log.warning(f"接口 {getattr(fn, '__name__', '?')} 失败：{type(e).__name__}: {e}")
                return None

    def refresh(self):
        self._round += 1
        trades = positions = None
        # 所有外部接口都走 _safe；再做一层整体兜底，避免任何异常冒泡杀死 loop 线程
        try:
            trades = self._safe(self.api.trade_history, self.pid, 1, self.size)
            positions = self._safe(self.api.position_history, self.pid, 1, self.order_size)
        except Exception as e:
            log.warning(f"拉取成交/仓位异常：{type(e).__name__}: {e}")

        data_ok = trades is not None
        if trades is not None:
            self.recent = trades[:100]
            if positions is not None:
                # 历史订单（已平仓记录）单独展示
                self.orders = positions
                try:
                    self.stats = S.compute(positions)
                except Exception as e:
                    log.warning(f"统计计算异常：{type(e).__name__}: {e}")
                try:
                    new = self.det.scan(trades, positions)
                    for ev in reversed(new):
                        self.events.appendleft(ev)
                    if new:
                        log.info(f"面板捕获 {len(new)} 个新事件")
                except Exception as e:
                    log.warning(f"事件扫描异常：{type(e).__name__}: {e}")
        # 账户数据降频刷新，失败则沿用上次缓存
        if self._round == 1 or self._round % self.account_every == 0:
            if not self.account:
                detail = self._safe(self.api.detail, self.pid)
                perf = self._safe(self.api.performance, self.pid,
                                  self.cfg.get("performance_range", "30D"))
                if detail is not None or perf is not None:
                    self.account = {
                        "detail": detail or {},
                        "performance": perf or {},
                    }
            else:
                detail = self._safe(self.api.detail, self.pid)
                perf = self._safe(self.api.performance, self.pid,
                                  self.cfg.get("performance_range", "30D"))
                if detail is not None:
                    self.account["detail"] = detail
                if perf is not None:
                    self.account["performance"] = perf
        # 当前持仓还原（双来源：已平仓剩余 + 成交流重建新仓）——重活，低频
        if self._round == 1 or self._round % self.position_every == 0:
            mb = (self.account.get("detail") or {}).get("marginBalance")
            ps = self._safe(P.compute, self.pid, None, self.size, mb,
                            self.new_trade_pages)
            if ps is not None:
                self.positions = ps
                self.pos_note = (
                    "持仓来自两个公开源：已了结仓位的剩余量(position-history) + "
                    "新开仓位(成交流重建，标「成交流重建」)；现价/标记价来自 fapi 公共行情，"
                    "每轮实时刷新；浮盈亏/保证金/实际杠杆为推算值。重建仓无杠杆字段，"
                    "强平价按单仓估算，仅供参考。"
                )
        # 仓位生命周期（开→加→减→平时间线）——深拉多页成交，最重，最低频
        if self._round == 1 or self._round % self.lifecycle_every == 0:
            try:
                deep = []
                for pg in range(1, self.lifecycle_pages + 1):
                    chunk = self._safe(self.api.trade_history, self.pid, pg, 100)
                    if not chunk:
                        break
                    deep += chunk
                if deep and positions:
                    self.lifecycle = L.compute(positions, deep, max_items=20)
                    log.info(f"生命周期时间线更新：{self.lifecycle.get('count')} 笔仓位 / "
                             f"{len(deep)} 笔成交")
            except Exception as e:
                log.warning(f"生命周期聚合异常：{type(e).__name__}: {e}")
        # 现价/标记价轻量高频刷新：fapi 公共行情不触发 bapi 的 11012005 限流
        try:
            self._refresh_quotes_light()
        except Exception as e:
            log.warning(f"轻量报价异常：{type(e).__name__}: {e}")
        # 心跳时间每轮都推进：即使 bapi 限流，面板也要显示"仍在运行"，数据时间单独给出
        self.last = M.ts_ms()
        if data_ok:
            self.status, self.error = "ok", ""
            self._fail_streak = 0
        else:
            self._fail_streak += 1
            self.status = "degraded"
            self.error = "跟单接口限流/暂不可用，价格与心跳仍实时刷新"

    def _refresh_quotes_light(self):
        """用 fapi 公共行情实时刷新持仓现价/标记价与浮盈亏，不依赖 bapi 限流接口。"""
        if not self.positions:
            return
        symbols = {p.get("symbol") for p in self.positions if p.get("symbol")}
        quotes = {s: P.quote(s) for s in symbols}
        mb = (self.account.get("detail") or {}).get("marginBalance")
        try:
            equity = float(mb) if mb and float(mb) > 0 else None
        except (TypeError, ValueError):
            equity = None
        for p in self.positions:
            price, mark = quotes.get(p.get("symbol"), (None, None))
            if price is not None:
                p["price"] = price
            if mark is not None:
                p["mark"] = mark
            ref = p.get("mark") or p.get("price") or p.get("avgCost") or 0
            qty = float(p.get("qty") or 0)
            avg = float(p.get("avgCost") or 0)
            short = bool(p.get("short"))
            lev = float(p.get("leverage") or 1) or 1
            p["notional"] = qty * ref
            p["margin"] = qty * ref / lev if lev > 0 else 0
            p["unrealized"] = (ref - avg) * qty * (-1 if short else 1)
            p["unrealizedPct"] = (p["unrealized"] / p["margin"] * 100) if p["margin"] > 0 else 0
            if equity and equity > 0 and qty * ref > 0:
                p["realLeverage"] = qty * ref / equity

    def loop(self):
        while True:
            try:
                self.refresh()
            except Exception as e:
                # 关键：任何单次异常都不得让后台线程退出（这是面板"卡死不再变化"的根因）
                self._fail_streak += 1
                self.last = M.ts_ms()
                self.status = "degraded"
                self.error = f"刷新异常：{type(e).__name__}: {e}"
                log.exception(f"刷新循环异常（已捕获，线程继续）：{e}")
            delay = min(self.interval * (2 ** min(self._fail_streak, 5)), 60)
            time.sleep(delay)

    def payload(self):
        # 数据时间 = 公开接口里最新成交的时间（可能落后于心跳，属正常）
        data_last = 0
        if self.recent:
            try:
                data_last = max(int(t.get("time", 0)) for t in self.recent)
            except (TypeError, ValueError):
                data_last = 0
        return {
            "status": self.status, "error": self.error, "last": self.last,
            "data_last": data_last, "round": self._round,
            "nickname": self.nickname, "pid": self.pid,
            "account": self.account,
            "positions": self.positions,
            "posNote": self.pos_note,
            "events": list(self.events)[:40],
            "recent": self.recent,
            "orders": self.orders,
            "stats": self.stats,
            "lifecycle": self.lifecycle,
        }


class Handler(BaseHTTPRequestHandler):
    dash = None

    def _send(self, body, ctype):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/health":
            self._send(b'{"status":"ok"}', "application/json; charset=utf-8")
        elif path == "/api/state":
            body = json.dumps(self.dash.payload(), ensure_ascii=False).encode("utf-8")
            self._send(body, "application/json; charset=utf-8")
        else:
            self._send(HTML.encode("utf-8"), "text/html; charset=utf-8")

    def log_message(self, *args):
        pass


def main():
    cfg = M.load_config(str(HERE / "config.json"))
    dash = Dashboard(cfg)
    Handler.dash = dash
    threading.Thread(target=dash.loop, daemon=True).start()
    srv = ThreadingHTTPServer((BIND_HOST, PORT), Handler)
    log.info(f"面板已启动：http://{BIND_HOST}:{PORT}  （Ctrl+C 停止）")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        log.info("已停止")


if __name__ == "__main__":
    main()
