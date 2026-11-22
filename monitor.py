#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
币安 U 本位合约跟单监控器 (Copy Trade Monitor)

功能：
  1. 轮询币安跟单公开接口，监控指定交易员（默认：熬鹰资本）的开仓/平仓
  2. 检测到新动作 -> 控制台日志 + Windows 弹窗 + 提示音
  3. 可选：自动跟单下单（默认 DRY_RUN=true，仅打印不真下）

安全边界：
  - 跟单默认关闭（copy.enabled=false），只监控不碰资金
  - API Key 只从环境变量读取，绝不写进配置文件
  - 只开"合约交易"权限，禁止开"提现"权限

依赖：pip install requests
运行：python monitor.py --config config.json
"""

import argparse
import hashlib
import hmac
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    import requests
except ImportError:
    sys.exit("缺少依赖：请执行 pip install requests")

HERE = Path(__file__).resolve().parent

# ---------------------------------------------------------------------------
# 接口常量（2026-11 实测有效）
# ---------------------------------------------------------------------------
BAPI = "https://www.binance.com/bapi/futures/v1/friendly/future/copy-trade"
BAPI_P = "https://www.binance.com/bapi/futures/v1/public/future/copy-trade"
FAPI = "https://fapi.binance.com"

# 熬鹰资本 portfolioId（2026-11-20 实测，90D ROI 193.5%）
DEFAULT_PORTFOLIO_ID = "5075281354358777856"
DEFAULT_NICKNAME = "熬鹰资本"

HEADERS = {
    "content-type": "application/json",
    "clienttype": "web",
    "lang": "en",
    "origin": "https://www.binance.com",
    "user-agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/120.0.0.0 Safari/537.36"),
}


def ts_ms():
    return int(time.time() * 1000)


def fmt_time(ms):
    if not ms:
        return "-"
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# 公开数据客户端（无需 API Key）
# ---------------------------------------------------------------------------
class PublicAPI:
    def __init__(self, timeout=15, min_interval=0.35, retries=4):
        self.s = requests.Session()
        self.s.headers.update(HEADERS)
        self.timeout = timeout
        self.min_interval = min_interval   # 全局最小请求间隔，缓解 11012005 限流
        self.retries = retries
        self._last_req = 0.0

    def _throttle(self):
        gap = time.time() - self._last_req
        if gap < self.min_interval:
            time.sleep(self.min_interval - gap)
        self._last_req = time.time()

    def _request(self, method, url, **kw):
        """统一节流 + 限流(11012005)递增退避重试。"""
        for i in range(self.retries + 1):
            self._throttle()
            try:
                r = self.s.request(method, url, timeout=self.timeout, **kw)
                r.raise_for_status()
                d = r.json()
                code = str(d.get("code"))
                if code == "000000":
                    return d.get("data") or {}
                if code == "11012005" and i < self.retries:
                    time.sleep(2 ** i)   # 2,4,8,16s 退避，等币安冷却窗口过去
                    continue
                raise RuntimeError(f"接口返回异常 code={code} msg={d.get('message')}")
            except requests.RequestException:
                if i < self.retries:
                    time.sleep(2 ** i)
                    continue
                raise
        raise RuntimeError("接口重试耗尽")

    def _post(self, path, body):
        return self._request("POST", BAPI + path, json=body)

    def _get(self, path, params=None, public=False):
        return self._request("GET", (BAPI_P if public else BAPI) + path,
                             params=params or {})

    def detail(self, pid):
        """账户详情（GET）。含 marginBalance/aumAmount/currentCopyCount/positionShow 等。"""
        return self._get("/lead-portfolio/detail", {"portfolioId": str(pid)})

    def performance(self, pid, time_range="30D"):
        """业绩指标（GET）。含 roi/pnl/mdd/winRate/winOrders/totalOrder/sharpRatio。"""
        return self._get("/lead-portfolio/performance",
                         {"portfolioId": str(pid), "timeRange": time_range}, public=True)

    def search_trader(self, nickname):
        """按昵称找交易员 portfolioId。返回匹配列表。"""
        data = self._post("/home-page/query-list", {
            "pageNumber": 1, "pageSize": 30, "timeRange": "90D",
            "dataType": "ROI", "favoriteOnly": False, "hideFull": False,
            "nickname": nickname, "order": "DESC", "userAsset": 0,
            "portfolioType": "PUBLIC",
        })
        return [{
            "portfolio_id": str(x.get("leadPortfolioId")),
            "nickname": x.get("nickname"),
            "roi": x.get("roi"),
            "copiers": x.get("currentCopyCount"),
        } for x in data.get("list", [])]

    def trade_history(self, pid, page=1, size=20):
        """逐笔成交。字段：time/symbol/side/price/quantity/positionSide/realizedProfit"""
        data = self._post("/lead-portfolio/trade-history", {
            "portfolioId": str(pid), "pageNumber": page, "pageSize": size,
        })
        return data.get("list", [])

    def position_history(self, pid, page=1, size=20):
        """仓位记录。字段：positionId/symbol/opened/closed/avgCost/avgClosePrice/side/status/leverage"""
        data = self._post("/lead-portfolio/position-history", {
            "portfolioId": str(pid), "pageNumber": page, "pageSize": size,
        })
        return data.get("list", [])


# ---------------------------------------------------------------------------
# 合约下单客户端（需 API Key，仅跟单模式使用）
# ---------------------------------------------------------------------------
class FuturesClient:
    def __init__(self, api_key, api_secret):
        self.key, self.secret = api_key, api_secret
        self.s = requests.Session()
        self.s.headers.update({"X-MBX-APIKEY": api_key})
        self._precision = {}

    def _signed(self, method, path, params=None):
        params = dict(params or {})
        params["timestamp"] = ts_ms()
        params["recvWindow"] = 5000
        query = "&".join(f"{k}={v}" for k, v in params.items())
        sig = hmac.new(self.secret.encode(), query.encode(), hashlib.sha256).hexdigest()
        url = f"{FAPI}{path}?{query}&signature={sig}"
        r = self.s.request(method, url, timeout=15)
        return r.json()

    def get_balance(self):
        d = self._signed("GET", "/fapi/v2/balance")
        if isinstance(d, dict) and "code" in d:
            raise RuntimeError(f"API 错误：{d}")
        return float(next((x["availableBalance"] for x in d if x["asset"] == "USDT"), 0))

    def load_precision(self, symbols):
        """拉 exchangeInfo 获取每个交易对的数量精度。"""
        r = self.s.get(f"{FAPI}/fapi/v1/exchangeInfo", timeout=15).json()
        for s in r.get("symbols", []):
            if s["symbol"] not in symbols:
                continue
            flt = next((f for f in s["filters"] if f["filterType"] == "LOT_SIZE"), None)
            if flt:
                self._precision[s["symbol"]] = {
                    "step": float(flt["stepSize"]),
                    "minQty": float(flt["minQty"]),
                }

    def _round_qty(self, symbol, qty):
        p = self._precision.get(symbol, {"step": 0.001, "minQty": 0.001})
        step = p["step"]
        decimals = len(str(step).rstrip("0").split(".")[-1]) if "." in str(step) else 0
        q = int(qty / step) * step
        return round(max(q, p["minQty"]), decimals)

    def place_market(self, symbol, side, qty, position_side, dry_run=True):
        """下单。side=BUY/SELL；position_side=LONG/SHORT。"""
        qty = self._round_qty(symbol, qty)
        order = {
            "symbol": symbol, "side": side, "type": "MARKET",
            "quantity": qty, "positionSide": position_side,
        }
        if dry_run:
            return {"dry_run": True, **order}
        d = self._signed("POST", "/fapi/v1/order", order)
        if "orderId" not in d:
            raise RuntimeError(f"下单失败：{d}")
        return d


# ---------------------------------------------------------------------------
# 事件检测：对比快照，只报新增
# ---------------------------------------------------------------------------
class Detector:
    """维护已见过的成交时间/仓位ID， detecting new events."""

    def __init__(self, state_file, logger):
        self.state_file = Path(state_file)
        self.log = logger
        self.state = self._load()

    def _load(self):
        if self.state_file.exists():
            try:
                return json.loads(self.state_file.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {"last_trade_time": 0, "seen_positions": []}

    def save(self):
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        # seen_positions 只保留最近 200 个，防止无限增长
        self.state["seen_positions"] = self.state["seen_positions"][-200:]
        self.state_file.write_text(
            json.dumps(self.state, ensure_ascii=False, indent=2), encoding="utf-8")

    def scan(self, trades, positions):
        """返回新事件列表。"""
        events = []
        # 首次运行：只建立基线，不报任何历史（避免一开屏刷几百笔）
        if not self.state.get("initialized"):
            if trades:
                self.state["last_trade_time"] = max(int(t["time"]) for t in trades)
            self.state["seen_positions"] = [
                str(p.get("positionId", "")) for p in positions if p.get("positionId")
            ]
            self.state["initialized"] = True
            self.save()
            return []

        last_t = self.state.get("last_trade_time", 0)

        # 新成交：只取 time > last_t 的
        new_trades = [t for t in trades if int(t.get("time", 0)) > last_t]
        if new_trades:
            max_t = max(int(t["time"]) for t in new_trades)
            for t in sorted(new_trades, key=lambda x: int(x["time"])):
                events.append({
                    "kind": "TRADE", "time": int(t["time"]),
                    "symbol": t.get("symbol"), "side": t.get("side"),
                    "positionSide": t.get("positionSide"),
                    "price": t.get("price"), "qty": t.get("qty"),
                    "base": t.get("baseAsset"),
                    "notional": t.get("quantity"),  # quantity 是 USDT 名义金额，qty 才是 base 数量
                    "realized": t.get("realizedProfit", 0),
                })
            self.state["last_trade_time"] = max_t
        elif not self.state.get("initialized"):
            # 首次运行：把当前最大时间设为基线，不报历史
            if trades:
                self.state["last_trade_time"] = max(int(t["time"]) for t in trades)

        # 新平仓：positionId 未见过 且 closed 有值
        seen = set(self.state.get("seen_positions", []))
        for p in positions:
            pid = str(p.get("positionId", ""))
            closed = p.get("closed")
            if not pid or not closed:
                continue
            if pid not in seen and str(p.get("status", "")).lower().startswith("all"):
                events.append({
                    "kind": "CLOSE", "time": int(closed),
                    "symbol": p.get("symbol"), "side": p.get("side"),
                    "openPrice": p.get("avgCost"), "closePrice": p.get("avgClosePrice"),
                    "pnl": p.get("closingPnl"), "roi": p.get("roi"),
                    "leverage": p.get("leverage"),
                })
                seen.add(pid)
        self.state["seen_positions"] = list(seen)
        self.save()
        return events


# ---------------------------------------------------------------------------
# 提醒（控制台 + Windows 弹窗 + 声音）
# ---------------------------------------------------------------------------
class Notifier:
    """分区详情模板：仿交易信号 Bot 的六段式告警。"""

    def __init__(self, cfg, logger, account=None):
        self.cfg = cfg
        self.log = logger
        self.account = account or {}
        self.toast = cfg.get("notify", {}).get("windows_toast", True)
        self.sound = cfg.get("notify", {}).get("sound", True)
        # 碎单合并：同品种同方向 alert_window 秒内只弹窗一次（日志仍全记）
        self.alert_window = int(cfg.get("notify", {}).get("alert_window_sec", 30))
        self._last_alert = {}
        self._pos_meta = {}

    def set_account(self, account):
        """每轮刷新账户数据，让告警里的账户动态是最新的。"""
        self.account = account or {}

    def set_positions(self, positions):
        """缓存仓位历史，用于补全成交事件里的杠杆/逐仓全仓字段。"""
        self._pos_meta = {}
        for p in positions or []:
            sym = p.get("symbol")
            if sym and sym not in self._pos_meta:
                self._pos_meta[sym] = {
                    "leverage": p.get("leverage"),
                    "isolated": p.get("isolated"),
                }

    @staticmethod
    def _fnum(v, dash="-"):
        try:
            return f"{float(v):,.2f}"
        except (TypeError, ValueError):
            return dash

    def _account_block(self):
        """账户动态段：只用公开接口真实拿到的字段，不编造强平价/保证金率。"""
        d = self.account.get("detail", {})
        p = self.account.get("performance", {})
        lines = ["📊 账户动态（公开接口实测）"]
        bal = d.get("marginBalance")
        aum = d.get("aumAmount")
        if bal is not None:
            lines.append(f"  • 保证金余额：{self._fnum(bal)} USDT")
        if aum is not None:
            lines.append(f"  • 账户资产 AUM：{self._fnum(aum)} USDT")
        copiers = d.get("currentCopyCount")
        if copiers is not None:
            lines.append(f"  • 当前跟单人数：{copiers}")
        if p:
            lines.append(f"  • 近{str(p.get('timeRange', '?')).replace('D', '天')} ROI："
                         f"{self._fnum(p.get('roi'))}%  |  收益率 {self._fnum(p.get('pnl'))} USDT")
            n = p.get("totalOrder")
            w = p.get("winOrders")
            if n:
                lines.append(f"  • 胜率：{self._fnum(p.get('winRate'))}%  ({w}/{n})")
            if p.get("mdd") is not None:
                lines.append(f"  • 最大回撤：{self._fnum(p.get('mdd'))}%")
        lines.append("  • 强平价/保证金率：未公开（该交易员 positionShow=false）")
        return "\n".join(lines)

    def _render(self, event, nickname):
        """六段式详情正文。"""
        t = fmt_time(event.get("time"))
        head = f"🔵 监控对象：{nickname}  [APP]\n@everyone\n"
        if event["kind"] == "TRADE":
            side, ps = event.get("side"), event.get("positionSide")
            is_open = (side == "BUY" and ps == "LONG") or (side == "SELL" and ps == "SHORT")
            status = "仓位已增加（加仓）" if is_open else "仓位已减少（减仓/平仓）"
            direction = "开多 📈" if (side == "BUY" and ps == "LONG") else \
                        "开空 📉" if (side == "SELL" and ps == "SHORT") else \
                        "平多 📉" if (side == "SELL" and ps == "LONG") else "平空 📈"
            meta = self._pos_meta.get(event["symbol"], {})
            lev = event.get("leverage") or meta.get("leverage") or "-"
            iso_raw = event.get("isolated") or meta.get("isolated") or "-"
            isolated = {"Cross": "全仓", "Isolated": "逐仓"}.get(str(iso_raw), iso_raw)
            notional = event.get("notional")
            if notional is None:
                try:
                    notional = float(event.get("qty") or 0) * float(event.get("price") or 0)
                except (TypeError, ValueError):
                    notional = 0
            body = (
                f"状态：{status}\n\n"
                f"🏷 标的：{event['symbol']} | 永续 | {lev}x | {isolated}\n"
                f"🔄 变动：{direction}\n\n"
                f"📦 本次成交\n"
                f"  • 方向：{side} / {ps}\n"
                f"  • 数量：{event['qty']} {event.get('base') or ''}\n"
                f"  • 价格：${self._fnum(event['price'])}\n"
                f"  • 名义价值：{self._fnum(notional)} USDT\n\n"
                f"{self._account_block()}\n\n"
                f"⏰ 监控时间：{t}"
            )
            title = f"{nickname} {direction} {event['symbol']}"
        else:
            pnl = float(event.get("pnl") or 0)
            # roi 是小数（0.092 = 9.2%），转成百分比
            try:
                roi_pct = f"{float(event.get('roi')) * 100:+.2f}%"
            except (TypeError, ValueError):
                roi_pct = "-"
            body = (
                f"状态：仓位已了结（平仓）\n\n"
                f"🏷 标的：{event['symbol']} | {event.get('side', '-')} | "
                f"{event.get('leverage', '-')}x\n\n"
                f"💰 平仓结果\n"
                f"  • 开仓均价：${self._fnum(event.get('openPrice'))}\n"
                f"  • 平仓均价：${self._fnum(event.get('closePrice'))}\n"
                f"  • 已实现盈亏：{pnl:+,.2f} USDT\n"
                f"  • 收益率：{roi_pct}\n\n"
                f"{self._account_block()}\n\n"
                f"⏰ 监控时间：{t}"
            )
            title = f"{nickname} 平仓 {event['symbol']} {pnl:+.0f}"
        return head + body, title

    def send(self, event, nickname):
        msg, title = self._render(event, nickname)
        # 日志用单行摘要，避免刷屏
        self.log.info("[ALERT] " + msg.replace("\n", " | "))
        # 弹窗/声音去重：熬鹰一次开仓常几十笔碎单，只提醒第一次
        alert_key = (event.get("symbol"), event["kind"],
                     event.get("side"), event.get("positionSide"))
        now = time.time()
        if now - self._last_alert.get(alert_key, 0) < self.alert_window:
            return
        self._last_alert[alert_key] = now
        if self.toast:
            self._win_toast(title, msg)
        if self.sound:
            self._beep()

    def _win_toast(self, title, msg):
        try:
            from win10toast import ToastNotifier
            ToastNotifier().show_toast(title, msg, duration=5, threaded=True)
            return
        except Exception:
            pass
        # 无 win10toast 时用 PowerShell 系统通知
        try:
            import subprocess
            ps = (f'Add-Type -AssemblyName System.Windows.Forms; '
                  f'[System.Windows.Forms.MessageBox]::Show('
                  f'"{msg}", "{title}", 0, 0x40000)')
            # 用 BurntToast（如安装）否则静默
            subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 f'Import-Module BurntToast -ErrorAction SilentlyContinue; '
                 f'if (Get-Command New-BurntToastNotification -ErrorAction SilentlyContinue) '
                 f'{{ New-BurntToastNotification -Text "{title}", "{msg}" }}'],
                capture_output=True, timeout=8)
        except Exception:
            pass

    def _beep(self):
        try:
            import winsound
            winsound.Beep(880, 180)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 跟单执行：把事件映射为自己的下单动作
# ---------------------------------------------------------------------------
class CopyEngine:
    def __init__(self, cfg, futures, logger):
        self.cfg = cfg
        self.f = futures
        self.log = logger
        self.notional = float(cfg.get("copy", {}).get("notional_usdt", 20))
        self.dry = bool(cfg.get("copy", {}).get("dry_run", True))
        # 熬鹰一开仓常是几十笔碎单，必须节流：同品种同方向 cooldown 秒内只跟一次
        self.cooldown = int(cfg.get("copy", {}).get("cooldown_sec", 60))
        self._seen_open = {}  # (symbol, positionSide) -> 上次跟单时间戳(秒)

    def _hot(self, key):
        """是否处于冷却期。"""
        last = self._seen_open.get(key, 0)
        return (time.time() - last) < self.cooldown

    def execute(self, event):
        if event["kind"] != "TRADE":
            return
        side, ps = event["side"], event["positionSide"]
        symbol = event["symbol"]
        price = float(event["price"] or 0)
        if price <= 0:
            return

        # 判断动作：BUY+LONG=开多, SELL+SHORT=开空, SELL+LONG=平多, BUY+SHORT=平空
        key = (symbol, ps)
        if side == "BUY" and ps == "LONG":
            if self._hot(key):
                return  # 冷却期内不重复跟，避免一开仓几十笔碎单全跟
            my_side = "BUY"
        elif side == "SELL" and ps == "SHORT":
            if self._hot(key):
                return
            my_side = "SELL"
        elif side == "SELL" and ps == "LONG":
            my_side = "SELL"  # 平多
        elif side == "BUY" and ps == "SHORT":
            my_side = "BUY"   # 平空
        else:
            return
        self._seen_open[key] = time.time()

        qty = self.notional / price
        res = self.f.place_market(symbol, my_side, qty, ps, self.dry)
        tag = "【模拟】" if self.dry else "【实盘】"
        self.log.info(f"{tag}跟单 {my_side} {ps} {symbol} qty≈{qty:.6g} "
                      f"(名义 {self.notional} USDT) -> {res}")


# ---------------------------------------------------------------------------
# 主程序
# ---------------------------------------------------------------------------
def load_config(path):
    default = {
        "portfolio_id": DEFAULT_PORTFOLIO_ID,
        "nickname": DEFAULT_NICKNAME,
        "poll_interval": 3,
        "history_size": 20,
        "state_file": str(HERE / "state.json"),
        "notify": {"windows_toast": True, "sound": True},
        "copy": {"enabled": False, "dry_run": True, "notional_usdt": 20},
    }
    p = Path(path)
    if p.exists():
        user = json.loads(p.read_text(encoding="utf-8"))
        default.update(user)
    return default


def setup_logger(log_file):
    logger = logging.getLogger("monitor")
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)
    try:
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    except Exception:
        pass
    return logger


def main():
    ap = argparse.ArgumentParser(description="币安跟单监控器")
    ap.add_argument("--config", default=str(HERE / "config.json"))
    ap.add_argument("--search", metavar="昵称", help="按昵称搜索交易员 portfolioId 后退出")
    ap.add_argument("--once", action="store_true", help="只检测一次后退出（用于测试）")
    args = ap.parse_args()

    cfg = load_config(args.config)
    logger = setup_logger(str(HERE / "monitor.log"))

    api = PublicAPI()

    # 搜索模式
    if args.search:
        logger.info(f"搜索交易员：{args.search}")
        for r in api.search_trader(args.search):
            logger.info(f"  {r['portfolio_id']}  {r['nickname']}  ROI={r['roi']}%  跟单人数={r['copiers']}")
        return

    pid = str(cfg["portfolio_id"])
    nickname = cfg.get("nickname", "交易员")
    logger.info(f"启动监控：{nickname} (portfolioId={pid})  轮询 {cfg['poll_interval']}s")

    # 跟单模式初始化
    copier = None
    if cfg.get("copy", {}).get("enabled"):
        key = os.getenv("BINANCE_API_KEY")
        secret = os.getenv("BINANCE_API_SECRET")
        if not key or not secret:
            logger.error("copy.enabled=true 但未设置环境变量 BINANCE_API_KEY / BINANCE_API_SECRET，"
                         "已自动降级为只提醒模式")
        else:
            f = FuturesClient(key, secret)
            try:
                bal = f.get_balance()
                logger.info(f"合约账户 USDT 可用余额：{bal:.2f}")
                symbols = set()
                for t in api.trade_history(pid, size=cfg["history_size"]):
                    symbols.add(t.get("symbol"))
                if symbols:
                    f.load_precision(symbols)
                copier = CopyEngine(cfg, f, logger)
                if cfg["copy"].get("dry_run", True):
                    logger.warning("当前为模拟跟单（dry_run=true），不会真实下单")
                else:
                    logger.warning("⚠ 实盘跟单已开启：将使用真实资金下单！")
            except Exception as e:
                logger.error(f"合约客户端初始化失败，降级为只提醒：{e}")

    detector = Detector(cfg["state_file"], logger)
    notifier = Notifier(cfg, logger)

    def refresh_account():
        """每次轮询刷新账户详情与业绩（轻量 GET，失败不影响监控主流程）。"""
        try:
            notifier.set_account({
                "detail": api.detail(pid),
                "performance": api.performance(pid, cfg.get("performance_range", "30D")),
            })
        except Exception as e:
            logger.debug(f"账户数据刷新失败（不影响开平仓监控）：{e}")

    refresh_account()
    size = int(cfg.get("history_size", 20))

    def tick():
        try:
            trades = api.trade_history(pid, size=size)
            positions = api.position_history(pid, size=size)
            notifier.set_positions(positions)
            events = detector.scan(trades, positions)
            if events:
                refresh_account()
            for ev in events:
                notifier.send(ev, nickname)
                if copier:
                    copier.execute(ev)
            if not events:
                logger.debug("无新动作")
        except Exception as e:
            logger.error(f"轮询异常：{type(e).__name__}: {e}")

    tick()
    if args.once:
        logger.info("单次检测完成")
        return

    logger.info("进入持续监控，Ctrl+C 退出")
    try:
        while True:
            time.sleep(int(cfg["poll_interval"]))
            tick()
    except KeyboardInterrupt:
        logger.info("已停止")


if __name__ == "__main__":
    main()
