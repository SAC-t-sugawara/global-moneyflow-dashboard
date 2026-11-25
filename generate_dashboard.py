#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Global Money Flow & Market Dashboard
====================================
APIキー不要の無料データソースから世界のマネーフロー指標を取得し、
インタラクティブな単一HTML（public/index.html）を生成するスクリプト。

  * Yahoo Finance (yfinance)  : リスク資産 / DXY / VIX
  * FRED 公開CSV              : 米M2、米MMF残高、ハイイールド債スプレッド、米10年債利回り
  * DefiLlama API             : ステーブルコイン総時価総額

使い方:
    python generate_dashboard.py                 # 通常実行（ネットワーク必須）
    python generate_dashboard.py --demo          # ダミーデータで動作確認
    python generate_dashboard.py --lookback-days 1095 --output out/index.html
"""

from __future__ import annotations

import argparse
import io
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterable, Optional

import pandas as pd
import plotly.graph_objects as go
import requests
from plotly.subplots import make_subplots

# =============================================================================
# A. 設定
# =============================================================================
JST = timezone(timedelta(hours=9))

DEFAULT_LOOKBACK_DAYS = 730          # 2年
DEFAULT_OUTPUT = os.path.join("public", "index.html")

# 実行時に configure_window() で上書きされる
START_DATE: str = (datetime.now(JST) - timedelta(days=DEFAULT_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
LOOKBACK_DAYS: int = DEFAULT_LOOKBACK_DAYS

FRED_CSV_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"
DEFILLAMA_STABLECOIN_URL = "https://stablecoins.llama.fi/stablecoincharts/all"

HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; global-moneyflow-dashboard/1.0)",
    "Accept": "*/*",
}

# リスク資産（2年前=100で正規化して比較）
RISK_ASSETS: list[tuple[str, str]] = [
    ("ビットコイン (BTC)", "BTC-USD"),
    ("米国株 (S&P 500)", "^GSPC"),
    ("日本株 (日経平均)", "^N225"),
    ("欧州株 (EURO STOXX 50)", "^STOXX50E"),
    ("ゴールド (Gold)", "GC=F"),
]
RISK_COLORS = ["#f7931a", "#2563eb", "#dc2626", "#059669", "#eab308"]

# ストレス指標
DXY_TICKER = "DX-Y.NYB"
VIX_TICKER = "^VIX"
# メモ: Yahoo Finance の "^MOVE" は ICE BofA MOVE 指数ではなく
#       「Northern Trust iBoxx 5-Year Target」であり指標として使えない。
#       代わりに FRED の米10年債利回り(DGS10)から20営業日の変動幅(bp)を自前で算出する。
US10Y_FRED_SERIES = "DGS10"


def configure_window(lookback_days: int) -> None:
    """取得期間（起点日）を設定する。"""
    global START_DATE, LOOKBACK_DAYS
    LOOKBACK_DAYS = int(lookback_days)
    START_DATE = (datetime.now(JST) - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d")


def _start_ts() -> pd.Timestamp:
    return pd.to_datetime(START_DATE)


# =============================================================================
# B. データ取得
# =============================================================================
def _clean(s: pd.Series) -> pd.Series:
    """日付インデックスを正規化（タイムゾーン除去・日次丸め・重複除去・ソート）。"""
    idx = pd.to_datetime(s.index)
    if getattr(idx, "tz", None) is not None:
        idx = idx.tz_localize(None)
    s = s.copy()
    s.index = idx.normalize()
    s = s[~s.index.duplicated(keep="last")].dropna().sort_index()
    return s


def fetch_yf_series(ticker: str, retries: int = 3, wait: float = 5.0) -> pd.Series:
    """Yahoo Finance から終値の時系列を取得する。"""
    try:
        import yfinance as yf
    except ImportError:
        print("[WARN] yfinance が未インストールです: pip install -r requirements.txt")
        return pd.Series(dtype=float)

    for attempt in range(1, retries + 1):
        try:
            hist = yf.Ticker(ticker).history(start=START_DATE, auto_adjust=True)
            if hist is None or hist.empty or "Close" not in hist:
                print(f"[WARN] yfinance: {ticker} のデータが空です ({attempt}/{retries})")
            else:
                s = _clean(hist["Close"])
                if not s.empty:
                    print(f"[ OK ] yfinance {ticker}: {len(s)} 件")
                    return s
        except Exception as e:  # noqa: BLE001
            print(f"[WARN] yfinance failed for {ticker}: {e} ({attempt}/{retries})")
        if attempt < retries:
            time.sleep(wait * attempt)
    return pd.Series(dtype=float)


def fetch_fred_csv(series_id: str) -> pd.Series:
    """FRED から APIキー不要の公開CSVを直接取得する。"""
    url = FRED_CSV_URL.format(series_id=series_id)
    try:
        res = requests.get(url, headers=HTTP_HEADERS, timeout=30)
        res.raise_for_status()
        df = pd.read_csv(io.StringIO(res.text))
        dates = pd.to_datetime(df.iloc[:, 0], errors="coerce")
        vals = pd.to_numeric(df.iloc[:, 1], errors="coerce")
        s = pd.Series(vals.values, index=dates).dropna()
        s = _clean(s)
        s = s[s.index >= _start_ts()]
        print(f"[ OK ] FRED {series_id}: {len(s)} 件")
        return s
    except Exception as e:  # noqa: BLE001
        print(f"[WARN] FRED failed for {series_id}: {e}")
        return pd.Series(dtype=float)


def fetch_stablecoin_mcap() -> pd.Series:
    """DefiLlama からステーブルコイン総時価総額（10億ドル単位）を取得する。"""
    try:
        res = requests.get(DEFILLAMA_STABLECOIN_URL, headers=HTTP_HEADERS, timeout=30)
        res.raise_for_status()
        rows = res.json()
        records: list[tuple[pd.Timestamp, float]] = []
        for row in rows or []:
            try:
                dt = pd.to_datetime(int(row["date"]), unit="s").normalize()
            except (KeyError, TypeError, ValueError):
                continue
            mcap = (row.get("totalCirculatingUSD") or {}).get("peggedUSD")
            if mcap is None:
                mcap = (row.get("totalCirculating") or {}).get("peggedUSD")
            if mcap is None:
                continue
            records.append((dt, float(mcap) / 1e9))
        if not records:
            print("[WARN] DefiLlama: 有効なレコードがありませんでした")
            return pd.Series(dtype=float)
        s = pd.Series(dict(records)).sort_index()
        s = s[s.index >= _start_ts()]
        print(f"[ OK ] DefiLlama ステーブルコイン: {len(s)} 件")
        return s
    except Exception as e:  # noqa: BLE001
        print(f"[WARN] DefiLlama failed: {e}")
        return pd.Series(dtype=float)


def fetch_bond_volatility() -> pd.Series:
    """米10年債利回りの20営業日変動幅（bp）を MOVE の代替プロキシとして算出する。"""
    y10 = fetch_fred_csv(US10Y_FRED_SERIES)
    if y10.empty:
        return pd.Series(dtype=float)
    diff_bp = y10.diff() * 100.0                      # % → bp
    vol = diff_bp.rolling(window=20, min_periods=10).std()
    return vol.dropna()


# =============================================================================
# C. 前処理
# =============================================================================
def normalize_100(s: pd.Series) -> pd.Series:
    """開始時点を100とした相対パフォーマンスに変換する。"""
    if s is None or s.empty:
        return pd.Series(dtype=float)
    first = s.iloc[0]
    if not first or pd.isna(first):
        return pd.Series(dtype=float)
    return (s / first) * 100.0


def _pct_change(s: pd.Series, periods: int = 1) -> Optional[float]:
    if s is None or len(s) <= periods:
        return None
    base = s.iloc[-1 - periods]
    last = s.iloc[-1]
    if base is None or pd.isna(base) or base == 0:
        return None
    return (last / base - 1.0) * 100.0


def _fmt(v: Optional[float], unit: str = "") -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return "—"
    av = abs(v)
    if av >= 1000:
        return f"{v:,.0f}{unit}"
    if av >= 10:
        return f"{v:,.1f}{unit}"
    return f"{v:,.2f}{unit}"


def collect_data(demo: bool = False) -> dict[str, pd.Series]:
    """全データソースからデータを集めて dict で返す。"""
    if demo:
        print("[INFO] デモモード: ダミーデータで生成します（ネットワーク未使用）")
        return _demo_data()

    print("市場データの取得...")
    data: dict[str, pd.Series] = {}

    for label, ticker in RISK_ASSETS:
        data[label] = fetch_yf_series(ticker)

    data["米ドル指数 (DXY)"] = fetch_yf_series(DXY_TICKER)
    data["VIX (株式恐怖指数)"] = fetch_yf_series(VIX_TICKER)

    data["米国M2 ($B)"] = fetch_fred_csv("WM2NS")                   # 週次・10億ドル
    data["米国MMF待機資金 ($B)"] = fetch_fred_csv("MMMFFAQ027S") / 1000.0  # 百万→十億ドル
    data["ハイイールド債スプレッド (%)"] = fetch_fred_csv("BAMLH0A0HYM2")  # 日次・%
    data["米国債ボラティリティ (bp)"] = fetch_bond_volatility()
    data["ステーブルコイン時価総額 ($B)"] = fetch_stablecoin_mcap()
    return data


def _demo_data() -> dict[str, pd.Series]:
    """ネットワークなしでパイプラインを検証するためのダミーデータ。"""
    import numpy as np

    rng = np.random.default_rng(42)
    days = pd.bdate_range(end=datetime.now(JST).date(), periods=LOOKBACK_DAYS)
    weekly = pd.date_range(end=datetime.now(JST).date(), periods=LOOKBACK_DAYS // 7, freq="W-WED")
    quarterly = pd.date_range(end=datetime.now(JST).date(), periods=LOOKBACK_DAYS // 91, freq="QE")

    def walk(index, start, drift, vol):
        steps = rng.normal(drift, vol, len(index))
        return pd.Series(start * np.exp(np.cumsum(steps)), index=index)

    d: dict[str, pd.Series] = {}
    d["ビットコイン (BTC)"] = walk(days, 60000, 0.0012, 0.03)
    d["米国株 (S&P 500)"] = walk(days, 5800, 0.0004, 0.009)
    d["日本株 (日経平均)"] = walk(days, 39000, 0.0005, 0.012)
    d["欧州株 (EURO STOXX 50)"] = walk(days, 4900, 0.0003, 0.010)
    d["ゴールド (Gold)"] = walk(days, 2600, 0.0005, 0.008)
    d["米ドル指数 (DXY)"] = walk(days, 104.0, 0.0000, 0.003)
    d["VIX (株式恐怖指数)"] = (walk(days, 15.0, 0.0, 0.06)).clip(lower=9)
    d["米国M2 ($B)"] = walk(weekly, 21000.0, 0.0015, 0.001)
    d["米国MMF待機資金 ($B)"] = walk(quarterly, 6800.0, 0.01, 0.02)
    d["ハイイールド債スプレッド (%)"] = (walk(days, 3.2, 0.0, 0.02)).clip(lower=1.5)
    d["米国債ボラティリティ (bp)"] = (walk(days, 8.0, 0.0, 0.05)).clip(lower=2)
    d["ステーブルコイン時価総額 ($B)"] = walk(days, 160.0, 0.0008, 0.004)
    return d


# =============================================================================
# D. グラフ生成
# =============================================================================
def build_figure(data: dict[str, pd.Series]) -> tuple[go.Figure, list[dict]]:
    """4段構成のインタラクティブグラフと、ヘッダー用の指標カードを生成する。"""
    fig = make_subplots(
        rows=4,
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.075,
        subplot_titles=(
            "1. リスク資産の相対推移（起点=100として比較）",
            "2. 現金逃避・市場ストレス（米ドル指数 DXY / 株式VIX / 米国債ボラティリティ）",
            "3. 暗号資産市場の資金量（ステーブルコイン時価総額・$B）＆ 信用スプレッド（%）",
            "4. マクロ流動性・待機現金（米国M2マネーサプライ / 米国MMF残高・$B）",
        ),
        specs=[
            [{"secondary_y": False}],
            [{"secondary_y": True}],
            [{"secondary_y": True}],
            [{"secondary_y": True}],
        ],
    )

    cards: list[dict] = []

    def add_card(label: str, raw: pd.Series, unit: str, digit_note: str = "") -> None:
        if raw is None or raw.empty:
            return
        cards.append(
            {
                "label": label,
                "value": _fmt(raw.iloc[-1], unit),
                "d1": _pct_change(raw, 1),
                "d_period": _pct_change(raw, len(raw) - 1),
                "note": digit_note,
            }
        )

    # --- 1. リスク資産（起点=100で正規化） -----------------------------------
    for (label, _ticker), color in zip(RISK_ASSETS, RISK_COLORS):
        s = data.get(label)
        if s is None or s.empty:
            continue
        norm = normalize_100(s)
        if norm.empty:
            continue
        fig.add_trace(
            go.Scatter(
                x=norm.index,
                y=norm.values,
                name=label,
                mode="lines",
                line=dict(width=2, color=color),
                hovertemplate="%{y:.1f}<extra>" + label + "</extra>",
            ),
            row=1,
            col=1,
        )
        add_card(label, s, "")

    # --- 2. DXY / VIX / 米国債ボラティリティ ---------------------------------
    dxy = data.get("米ドル指数 (DXY)")
    vix = data.get("VIX (株式恐怖指数)")
    bondvol = data.get("米国債ボラティリティ (bp)")

    if dxy is not None and not dxy.empty:
        fig.add_trace(
            go.Scatter(x=dxy.index, y=dxy.values, name="米ドル指数 (DXY)",
                       line=dict(color="#111827", width=2)),
            row=2, col=1, secondary_y=False,
        )
        add_card("米ドル指数 (DXY)", dxy, "")
    if vix is not None and not vix.empty:
        fig.add_trace(
            go.Scatter(x=vix.index, y=vix.values, name="VIX (株式恐怖指数)",
                       line=dict(color="#ef4444", width=1.5, dash="dot")),
            row=2, col=1, secondary_y=True,
        )
        add_card("VIX (株式恐怖指数)", vix, "")
    if bondvol is not None and not bondvol.empty:
        fig.add_trace(
            go.Scatter(x=bondvol.index, y=bondvol.values, name="米国債ボラティリティ (bp・20日)",
                       line=dict(color="#8b5cf6", width=1.5)),
            row=2, col=1, secondary_y=True,
        )
        fig.update_yaxes(title_text="DXY", row=2, col=1, secondary_y=False,
                         showgrid=False, title_font=dict(size=11))
        fig.update_yaxes(title_text="VIX / 債券ボラ(bp)", row=2, col=1, secondary_y=True,
                         showgrid=False, title_font=dict(size=11))
    else:
        fig.update_yaxes(title_text="DXY", row=2, col=1, secondary_y=False,
                         showgrid=False, title_font=dict(size=11))

    # --- 3. ステーブルコイン & ハイイールド債スプレッド -----------------------
    stable = data.get("ステーブルコイン時価総額 ($B)")
    hy = data.get("ハイイールド債スプレッド (%)")

    if stable is not None and not stable.empty:
        fig.add_trace(
            go.Scatter(x=stable.index, y=stable.values, name="ステーブルコイン時価総額 ($B)",
                       fill="tozeroy", fillcolor="rgba(16,185,129,0.15)",
                       line=dict(color="#10b981", width=2)),
            row=3, col=1, secondary_y=False,
        )
        add_card("ステーブルコイン時価総額", stable, " B$")
    if hy is not None and not hy.empty:
        fig.add_trace(
            go.Scatter(x=hy.index, y=hy.values, name="ハイイールド債スプレッド (%)",
                       line=dict(color="#f97316", width=1.8)),
            row=3, col=1, secondary_y=True,
        )
        add_card("ハイイールド債スプレッド", hy, " %")
        fig.update_yaxes(title_text="ステーブルコイン ($B)", row=3, col=1, secondary_y=False,
                         showgrid=False, title_font=dict(size=11))
        fig.update_yaxes(title_text="HYスプレッド (%)", row=3, col=1, secondary_y=True,
                         showgrid=False, title_font=dict(size=11))
    elif stable is not None and not stable.empty:
        fig.update_yaxes(title_text="ステーブルコイン ($B)", row=3, col=1, secondary_y=False,
                         showgrid=False, title_font=dict(size=11))

    # --- 4. 米国M2 & MMF -----------------------------------------------------
    m2 = data.get("米国M2 ($B)")
    mmf = data.get("米国MMF待機資金 ($B)")

    if m2 is not None and not m2.empty:
        fig.add_trace(
            go.Scatter(x=m2.index, y=m2.values, name="米国M2 ($B)",
                       line=dict(color="#0284c7", width=2)),
            row=4, col=1, secondary_y=False,
        )
        add_card("米国M2マネーサプライ", m2, " B$")
    if mmf is not None and not mmf.empty:
        fig.add_trace(
            go.Scatter(x=mmf.index, y=mmf.values, name="米国MMF残高 ($B)",
                       mode="lines+markers", marker=dict(size=5),
                       line=dict(color="#6366f1", width=2)),
            row=4, col=1, secondary_y=True,
        )
        add_card("米国MMF残高", mmf, " B$")
        fig.update_yaxes(title_text="米国M2 ($B)", row=4, col=1, secondary_y=False,
                         showgrid=False, title_font=dict(size=11))
        fig.update_yaxes(title_text="MMF ($B)", row=4, col=1, secondary_y=True,
                         showgrid=False, title_font=dict(size=11))
    elif m2 is not None and not m2.empty:
        fig.update_yaxes(title_text="米国M2 ($B)", row=4, col=1, secondary_y=False,
                         showgrid=False, title_font=dict(size=11))

    if not fig.data:
        fig.add_annotation(
            text="データを取得できませんでした（ネットワークまたは各APIの状態を確認してください）",
            xref="paper", yref="paper", x=0.5, y=0.5, showarrow=False,
            font=dict(size=15, color="#dc2626"),
        )

    updated_at = datetime.now(JST).strftime("%Y-%m-%d %H:%M JST")
    fig.update_layout(
        title=None,
        height=1250,
        template="plotly_white",
        hovermode="x unified",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0),
        margin=dict(l=70, r=60, t=60, b=40),
        font=dict(family="'Segoe UI', 'Hiragino Sans', 'Yu Gothic', Meiryo, sans-serif", size=12),
    )

    # 期間切り替えボタン（共有X軸の最下段に配置）
    fig.update_xaxes(
        rangeselector=dict(
            buttons=[
                dict(count=1, label="1ヶ月", step="month", stepmode="backward"),
                dict(count=3, label="3ヶ月", step="month", stepmode="backward"),
                dict(count=6, label="6ヶ月", step="month", stepmode="backward"),
                dict(count=1, label="1年", step="year", stepmode="backward"),
                dict(step="all", label="全期間"),
            ],
            bgcolor="#f3f4f6",
            activecolor="#111827",
            font=dict(color="#111827", size=11),
        ),
        row=4,
        col=1,
    )
    fig.update_xaxes(showgrid=True, gridcolor="rgba(0,0,0,0.06)")
    fig.update_yaxes(showgrid=True, gridcolor="rgba(0,0,0,0.06)")
    fig.update_annotations(font_size=13)
    return fig, cards


# =============================================================================
# E. HTML出力
# =============================================================================
HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Global Money Flow &amp; Market Dashboard</title>
<style>
  :root {
    --bg: #f7f8fa;
    --panel: #ffffff;
    --ink: #111827;
    --muted: #6b7280;
    --line: #e5e7eb;
    --up: #059669;
    --down: #dc2626;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; padding: 24px 20px 40px;
    background: var(--bg); color: var(--ink);
    font-family: 'Segoe UI', 'Hiragino Sans', 'Yu Gothic', Meiryo, -apple-system, sans-serif;
  }
  .wrap { max-width: 1360px; margin: 0 auto; }
  header { margin-bottom: 18px; }
  h1 { font-size: 22px; margin: 0 0 6px; letter-spacing: .01em; }
  .stamp { color: var(--muted); font-size: 12.5px; }
  .stamp b { color: var(--ink); font-weight: 600; }
  .cards {
    display: grid; gap: 10px; margin: 18px 0 20px;
    grid-template-columns: repeat(auto-fill, minmax(178px, 1fr));
  }
  .card {
    background: var(--panel); border: 1px solid var(--line); border-radius: 10px;
    padding: 11px 13px; box-shadow: 0 1px 2px rgba(16,24,40,.04);
  }
  .card .label { font-size: 11.5px; color: var(--muted); line-height: 1.3; min-height: 30px; }
  .card .value { font-size: 19px; font-weight: 700; margin: 2px 0 5px; font-variant-numeric: tabular-nums; }
  .card .deltas { font-size: 11.5px; color: var(--muted); display: flex; flex-direction: column; gap: 2px; }
  .up { color: var(--up); font-weight: 600; }
  .down { color: var(--down); font-weight: 600; }
  .chart { background: var(--panel); border: 1px solid var(--line); border-radius: 12px; padding: 8px 8px 4px; }
  footer { margin-top: 16px; color: var(--muted); font-size: 11.5px; line-height: 1.7; }
  footer code { background: #eef0f3; padding: 1px 5px; border-radius: 4px; font-size: 11px; }
  @media (max-width: 640px) { body { padding: 16px 10px 30px; } h1 { font-size: 18px; } }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>Global Money Flow &amp; Market Dashboard</h1>
    <div class="stamp">最終更新: <b>__UPDATED_AT__</b> ／ 観測期間: __PERIOD__ ／ 起点=100 で正規化</div>
  </header>
  <section class="cards">__CARDS__</section>
  <section class="chart">__PLOT__</section>
  <footer>
    データ出所: Yahoo Finance (yfinance) / FRED（米セントルイス連銀）/ DefiLlama。すべてAPIキー不要の公開データを利用しています。<br>
    本ページは情報提供のみを目的とした定点観測用ダッシュボードであり、投資助言ではありません。数値はデータ提供元の定義・改定により変動します。
    米国債ボラティリティは Yahoo Finance の <code>^MOVE</code> が ICE BofA MOVE 指数ではないため、FRED の米10年債利回り(DGS10)から20営業日の変動幅(bp)を算出した代替指標を用いています。
  </footer>
</div>
</body>
</html>
"""

CARD_TEMPLATE = """    <div class="card">
      <div class="label">__LABEL__</div>
      <div class="value">__VALUE__</div>
      <div class="deltas">
        <span>前回比: __D1__</span>
        <span>期間比: __DPERIOD__</span>
      </div>
    </div>"""


def _delta_html(v: Optional[float]) -> str:
    if v is None:
        return "—"
    cls = "up" if v > 0 else ("down" if v < 0 else "")
    sign = "+" if v > 0 else ""
    return f'<span class="{cls}">{sign}{v:,.2f}%</span>'


def render_cards(cards: list[dict]) -> str:
    if not cards:
        return '    <div class="card"><div class="label">データなし</div><div class="value">—</div></div>'
    blocks = []
    for c in cards:
        blocks.append(
            CARD_TEMPLATE.replace("__LABEL__", c["label"])
            .replace("__VALUE__", c["value"])
            .replace("__D1__", _delta_html(c["d1"]))
            .replace("__DPERIOD__", _delta_html(c["d_period"]))
        )
    return "\n".join(blocks)


def write_dashboard(
    fig: go.Figure,
    cards: list[dict],
    output_path: str = DEFAULT_OUTPUT,
    include_plotlyjs: str = "cdn",
) -> str:
    """単一HTMLを書き出す。"""
    out_dir = os.path.dirname(output_path) or "."
    os.makedirs(out_dir, exist_ok=True)

    plot_html = fig.to_html(
        full_html=False,
        include_plotlyjs=include_plotlyjs,
        config={"displayModeBar": True, "responsive": True, "displaylogo": False},
    )

    updated_at = datetime.now(JST).strftime("%Y-%m-%d %H:%M JST")
    period = f"{START_DATE} 〜 {datetime.now(JST).strftime('%Y-%m-%d')}"
    html_doc = (
        HTML_TEMPLATE.replace("__UPDATED_AT__", updated_at)
        .replace("__PERIOD__", period)
        .replace("__CARDS__", render_cards(cards))
        .replace("__PLOT__", plot_html)
    )

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html_doc)
    return output_path


def build_dashboard(output_path: str = DEFAULT_OUTPUT, demo: bool = False,
                    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
                    include_plotlyjs: str = "cdn") -> str:
    configure_window(lookback_days)
    data = collect_data(demo=demo)
    fig, cards = build_figure(data)
    path = write_dashboard(fig, cards, output_path, include_plotlyjs)
    size_kb = os.path.getsize(path) / 1024
    print(f"正常に生成されました: {path} ({size_kb:,.0f} KB)")
    return path


# =============================================================================
# F. CLI
# =============================================================================
def parse_args(argv: Optional[Iterable[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Global Money Flow & Market Dashboard 生成")
    p.add_argument("--output", default=DEFAULT_OUTPUT, help=f"出力HTMLパス（既定: {DEFAULT_OUTPUT}）")
    p.add_argument("--lookback-days", type=int, default=DEFAULT_LOOKBACK_DAYS, help="取得期間（日数, 既定: 730）")
    p.add_argument("--demo", action="store_true", help="ダミーデータで動作確認（ネットワーク未使用）")
    p.add_argument("--inline-plotlyjs", action="store_true", help="Plotly.js をHTMLに同梱（完全オフライン用・約4MB）")
    return p.parse_args(list(argv) if argv is not None else None)


def main(argv: Optional[Iterable[str]] = None) -> int:
    args = parse_args(argv)
    build_dashboard(
        output_path=args.output,
        demo=args.demo,
        lookback_days=args.lookback_days,
        include_plotlyjs=True if args.inline_plotlyjs else "cdn",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
