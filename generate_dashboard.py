#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Global Money Flow & Market Dashboard (v4改 LIVE BOARD)
=========================================
APIキー不要の無料データソースから世界のマネーフロー指標を取得し、
「数値カード＋自動分析＋期間別パフォーマンス＋5タブチャート」を備えた
インタラクティブな単一HTML（public/index.html）を生成する。

  * Yahoo Finance (yfinance) : リスク資産 / DXY / VIX / 世界指数 / 資産クラスETF
  * FRED 公開データ          : 米M2、米MMF残高、ハイイールド債スプレッド、米10年債利回り
                               CSV → テキスト → 保存済みキャッシュ の順にフォールバック
  * DefiLlama API            : ステーブルコイン総時価総額

使い方:
    python generate_dashboard.py                 # 通常実行（ネットワーク必須）
    python generate_dashboard.py --demo          # ダミーデータで動作確認
    python generate_dashboard.py --lookback-days 1095 --output out/index.html
"""

from __future__ import annotations

import argparse
import json
import html as html_lib
import io
import os
import re
import sys
import time
import math
from datetime import datetime, timedelta, timezone
from typing import Optional

import pandas as pd
import plotly.graph_objects as go
import requests
from plotly.subplots import make_subplots  # noqa: F401 (v2互換のため残置)
import plotly.offline as po

# =============================================================================
# A. 設定
# =============================================================================
JST = timezone(timedelta(hours=9))

DEFAULT_LOOKBACK_DAYS = 730          # 2年
DEFAULT_OUTPUT = os.path.join("public", "index.html")
CACHE_DIR = os.path.join("data", "cache")

START_DATE: str = (datetime.now(JST) - timedelta(days=DEFAULT_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
LOOKBACK_DAYS: int = DEFAULT_LOOKBACK_DAYS

FRED_CSV_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"
FRED_TXT_URL = "https://fred.stlouisfed.org/data/{series_id}.txt"
DEFILLAMA_STABLECOIN_URL = "https://stablecoins.llama.fi/stablecoincharts/all"

# FRED はデータセンターからのアクセスに 500 を返すことがあるため、
# ブラウザに近いヘッダーを付け、失敗時はリトライと代替エンドポイントを使う。
HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "text/csv,text/plain,text/html,*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://fred.stlouisfed.org/",
}

FONT_STACK = ("'Hiragino Kaku Gothic ProN', 'Hiragino Sans', 'Yu Gothic UI', "
              "'Yu Gothic', 'Meiryo', 'Noto Sans JP', 'Segoe UI', sans-serif")

# データ取得状況（HTMLの「データ取得状況」欄に表示する）
FETCH_STATUS: list[dict] = []


def _record(label: str, source: str, ok: bool, detail: str = "") -> None:
    FETCH_STATUS.append({
        "label": label,
        "source": source,
        "ok": ok,
        "detail": detail if detail else ("正常に取得" if ok else "取得失敗"),
    })


# (キー, 表示名, ティッカー)
RISK_ASSETS: list[tuple[str, str]] = [
    ("BTC", "ビットコイン (BTC)"),
    ("SPX", "米国株 (S&P 500)"),
    ("N225", "日本株 (日経平均)"),
    ("SX5E", "欧州株 (EURO STOXX 50)"),
    ("GOLD", "ゴールド (Gold)"),
]
RISK_TICKERS = {
    "BTC": "BTC-USD",
    "SPX": "^GSPC",
    "N225": "^N225",
    "SX5E": "^STOXX50E",
    "GOLD": "GC=F",
}
RISK_COLORS = ["#f7931a", "#2563eb", "#dc2626", "#059669", "#d4a017"]

DXY_TICKER = "DX-Y.NYB"
VIX_TICKER = "^VIX"
# Yahoo Finance の "^MOVE" は ICE BofA MOVE 指数ではないため使わない。
# 代わりに米10年債利回り(DGS10)の20営業日変動幅(bp)を代理指標として算出する。
US10Y_FRED_SERIES = "DGS10"

# ---- v4: 世界指数ティッカー ----
WORLD_INDEX_TICKERS: dict[str, str] = {
    "英国 (FTSE 100)": "^FTSE",
    "ドイツ (DAX)": "^GDAXI",
    "中国 (上海総合)": "000001.SS",
    "香港 (ハンセン)": "^HSI",
    "インド (Nifty 50)": "^NSEI",
    "ブラジル (BOVESPA)": "^BVSP",
    "オーストラリア (ASX)": "^AORD",
    "カナダ (TSX)": "^GSPTSE",
}

# ---- v4: 資産クラスETFティッカー ----
ASSET_CLASS_TICKERS: dict[str, str] = {
    "米長期債 (TLT)": "TLT",
    "米中期債 (IEF)": "IEF",
    "ハイールド債 (HYG)": "HYG",
    "投資適格債 (LQD)": "LQD",
    "不動産 (VNQ)": "VNQ",
    "新興国株式 (EEM)": "EEM",
    "先進国株式 ex-US (EFA)": "EFA",
    "原油 WTI (CL=F)": "CL=F",
}


def configure_window(lookback_days: int) -> None:
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


def fetch_yf_series(label: str, ticker: str, retries: int = 3, wait: float = 5.0) -> pd.Series:
    """Yahoo Finance から終値の時系列を取得する。"""
    try:
        import yfinance as yf
    except ImportError:
        print("[WARN] yfinance が未インストールです: pip install -r requirements.txt")
        _record(label, "Yahoo Finance", False, "yfinance 未インストール")
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
                    _record(label, "Yahoo Finance", True)
                    return s
        except Exception as e:  # noqa: BLE001
            print(f"[WARN] yfinance failed for {ticker}: {e} ({attempt}/{retries})")
        if attempt < retries:
            time.sleep(wait * attempt)

    _record(label, "Yahoo Finance", False, f"{retries}回試行して取得できませんでした")
    return pd.Series(dtype=float)


def _parse_fred_csv(text: str) -> pd.Series:
    """fredgraph.csv 形式（observation_date,VALUE）をパースする。"""
    head = text.lstrip()[:200].lower()
    if not head.startswith("observation_date"):
        raise ValueError("CSV形式ではありません（エラーページの可能性）")
    df = pd.read_csv(io.StringIO(text))
    if df.shape[1] < 2:
        raise ValueError("CSVの列数が不足しています")
    dates = pd.to_datetime(df.iloc[:, 0], errors="coerce")
    vals = pd.to_numeric(df.iloc[:, 1], errors="coerce")
    return pd.Series(vals.values, index=dates).dropna()


def _parse_fred_table(text: str) -> pd.Series:
    """fred.stlouisfed.org/data/{ID}.txt 形式（HTML表／テキスト）をパースする。

    行頭が YYYY-MM-DD で始まる行だけをデータ行として扱うため、
    「Last Updated 2026-11-24 12:01 PM」のようなヘッダー行は無視される。
    """
    chunks = re.split(r"(?i)</?tr[^>]*>|\n", text)
    pairs: list[tuple[pd.Timestamp, float]] = []
    pat = re.compile(r"^\s*(\d{4}-\d{2}-\d{2})\s+(-?\d+(?:\.\d+)?)\s*$")
    for chunk in chunks:
        line = html_lib.unescape(re.sub(r"<[^>]+>", " ", chunk))
        m = pat.match(line)
        if m:
            pairs.append((pd.Timestamp(m.group(1)), float(m.group(2))))
    if not pairs:
        raise ValueError("テキスト形式から有効なデータを抽出できませんでした")
    return pd.Series(dict(pairs)).dropna()


def _cache_path(series_id: str) -> str:
    return os.path.join(CACHE_DIR, f"{series_id}.csv")


def _save_cache(series_id: str, s: pd.Series) -> None:
    """取得できた系列を保存しておく（次回のフォールバック用）。"""
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        df = pd.DataFrame({series_id: s.values}, index=s.index)
        df.index.name = "observation_date"
        df.to_csv(_cache_path(series_id))
    except Exception as e:  # noqa: BLE001
        print(f"[WARN] cache write failed for {series_id}: {e}")


def _load_cache(series_id: str) -> pd.Series:
    path = _cache_path(series_id)
    if not os.path.exists(path):
        return pd.Series(dtype=float)
    try:
        df = pd.read_csv(path, index_col=0)
        s = _clean(pd.to_numeric(df.iloc[:, 0], errors="coerce").dropna())
        return s[s.index >= _start_ts()]
    except Exception as e:  # noqa: BLE001
        print(f"[WARN] cache read failed for {series_id}: {e}")
        return pd.Series(dtype=float)


def fetch_fred(series_id: str, label: Optional[str] = None,
               attempts_csv: int = 3, attempts_txt: int = 2) -> pd.Series:
    """FRED から APIキー不要の公開データを取得する。

    取得順:
      1) graph/fredgraph.csv （リトライ付き）
      2) data/{ID}.txt        （HTML表形式フォールバック）
      3) data/cache/{ID}.csv  （前回取得分。リポジトリに残しておけば継続表示できる）
    """
    label = label or series_id
    last_error = "unknown"

    for attempt in range(1, attempts_csv + 1):
        try:
            res = requests.get(FRED_CSV_URL.format(series_id=series_id),
                               headers=HTTP_HEADERS, timeout=30)
            res.raise_for_status()
            s = _clean(_parse_fred_csv(res.text))
            s = s[s.index >= _start_ts()]
            if s.empty:
                raise ValueError("取得期間内にデータがありません")
            print(f"[ OK ] FRED(CSV) {series_id}: {len(s)} 件")
            _record(label, "FRED CSV", True)
            _save_cache(series_id, s)
            return s
        except Exception as e:  # noqa: BLE001
            last_error = f"{type(e).__name__}: {e}"
            print(f"[WARN] FRED(CSV) failed for {series_id}: {last_error} ({attempt}/{attempts_csv})")
            if attempt < attempts_csv:
                time.sleep(4.0 * attempt)

    for attempt in range(1, attempts_txt + 1):
        try:
            res = requests.get(FRED_TXT_URL.format(series_id=series_id),
                               headers=HTTP_HEADERS, timeout=30)
            res.raise_for_status()
            s = _clean(_parse_fred_table(res.text))
            s = s[s.index >= _start_ts()]
            if s.empty:
                raise ValueError("取得期間内にデータがありません")
            print(f"[ OK ] FRED(txt) {series_id}: {len(s)} 件")
            _record(label, "FRED テキスト", True, "CSVが使えずテキスト形式で取得")
            _save_cache(series_id, s)
            return s
        except Exception as e:  # noqa: BLE001
            last_error = f"{type(e).__name__}: {e}"
            print(f"[WARN] FRED(txt) failed for {series_id}: {last_error} ({attempt}/{attempts_txt})")
            if attempt < attempts_txt:
                time.sleep(4.0)

    cached = _load_cache(series_id)
    if not cached.empty:
        print(f"[ OK ] FRED(cache) {series_id}: {len(cached)} 件（保存済みデータ）")
        _record(label, "保存済みキャッシュ", True,
                f"最新取得に失敗したため前回データを表示（{last_error[:40]}）")
        return cached

    _record(label, "FRED", False, f"取得失敗（{last_error[:60]}）")
    return pd.Series(dtype=float)


def fetch_stablecoin_mcap(label: str = "ステーブルコイン時価総額") -> pd.Series:
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
            raise ValueError("有効なレコードがありませんでした")
        s = pd.Series(dict(records)).sort_index()
        s = s[s.index >= _start_ts()]
        print(f"[ OK ] DefiLlama ステーブルコイン: {len(s)} 件")
        _record(label, "DefiLlama", True)
        return s
    except Exception as e:  # noqa: BLE001
        print(f"[WARN] DefiLlama failed: {e}")
        _record(label, "DefiLlama", False, f"取得失敗（{e}）")
        return pd.Series(dtype=float)


# =============================================================================
# C. 指標計算
# =============================================================================
def normalize_100(s: pd.Series) -> pd.Series:
    """開始時点を100とした相対パフォーマンスに変換する。"""
    if s is None or s.empty:
        return pd.Series(dtype=float)
    first = s.iloc[0]
    if not first or pd.isna(first):
        return pd.Series(dtype=float)
    return (s / first) * 100.0


def _pct_change_n(s: pd.Series, periods: int) -> Optional[float]:
    if s is None or len(s) <= periods:
        return None
    base = s.iloc[-1 - periods]
    if base is None or pd.isna(base) or base == 0:
        return None
    return (s.iloc[-1] / base - 1.0) * 100.0


def _prior_point(s: pd.Series, days: int) -> Optional[float]:
    """「直近データ日から days 日前」に最も近い過去の観測値。

    四半期・週次データで「1ヶ月比」が四半期前との比較になってしまうのを防ぐため、
    対象日から許容幅（期間の35%または10日）を超れて離れた点は使わない。
    """
    if s is None or len(s) < 2:
        return None
    target = s.index[-1] - pd.Timedelta(days=days)
    prior = s[s.index <= target]
    if prior.empty:
        return None
    tol_days = max(days * 0.35, 10)
    if (target - prior.index[-1]).days > tol_days:
        return None
    return float(prior.iloc[-1])


def period_return(s: pd.Series, days: int) -> Optional[float]:
    """「直近データ日から days 日前」との騰落率（%）。比較可能な点が無ければ None。"""
    base = _prior_point(s, days)
    if base is None or pd.isna(base) or base == 0:
        return None
    return (float(s.iloc[-1]) / base - 1.0) * 100.0


def period_change_abs(s: pd.Series, days: int) -> Optional[float]:
    """絶対値の変化幅（M2/MMF/ステーブルコインなど水準指標向け）。"""
    base = _prior_point(s, days)
    if base is None or pd.isna(base):
        return None
    return float(s.iloc[-1]) - base


def annualized_vol(s: pd.Series, window: int = 60) -> Optional[float]:
    """直近 window 本の日次リターンから年率ボラティリティ（%）を算出。"""
    if s is None or len(s) < window + 2:
        return None
    r = s.pct_change().dropna().tail(window)
    if r.empty:
        return None
    return float(r.std() * (252 ** 0.5) * 100.0)


def max_drawdown(s: pd.Series) -> Optional[float]:
    """期間内の最大ドローダウン（%、マイナス値）。"""
    if s is None or len(s) < 5:
        return None
    peak = s.cummax()
    dd = (s / peak - 1.0) * 100.0
    return float(dd.min())


def sma(s: pd.Series, window: int) -> pd.Series:
    return s.rolling(window=window, min_periods=max(5, window // 2)).mean()


def deviation_from_sma(s: pd.Series, window: int = 200) -> Optional[float]:
    m = sma(s, window)
    if m is None or m.empty or pd.isna(m.iloc[-1]) or m.iloc[-1] == 0:
        return None
    return float((s.iloc[-1] / m.iloc[-1] - 1.0) * 100.0)


def _fmt(v: Optional[float], unit: str = "", digits: Optional[int] = None) -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return "—"
    if digits is not None:
        return f"{v:,.{digits}f}{unit}"
    av = abs(v)
    if av >= 1000:
        return f"{v:,.0f}{unit}"
    if av >= 10:
        return f"{v:,.1f}{unit}"
    return f"{v:,.2f}{unit}"


def _fmt_signed(v: Optional[float], unit: str = "%") -> str:
    if v is None:
        return "—"
    return f"{v:+,.2f}{unit}"


# =============================================================================
# D. データ収集
# =============================================================================
def collect_data(demo: bool = False) -> dict[str, pd.Series]:
    """全データソースからデータを集めて dict で返す。"""
    if demo:
        print("[INFO] デモモード: ダミーデータで生成します（ネットワーク未使用）")
        data = _demo_data()
        for key in data:
            _record(key, "デモデータ", True, "ダミー数値")
        return data

    print("市場データの取得...")
    data: dict[str, pd.Series] = {}

    for key, label in RISK_ASSETS:
        data[label] = fetch_yf_series(label, RISK_TICKERS[key])

    data["米ドル指数 (DXY)"] = fetch_yf_series("米ドル指数 (DXY)", DXY_TICKER)
    data["VIX (株式恐怖指数)"] = fetch_yf_series("VIX (株式恐怖指数)", VIX_TICKER)

    # ---- v4: 世界指数 ----
    for label, ticker in WORLD_INDEX_TICKERS.items():
        data[label] = fetch_yf_series(label, ticker)

    # ---- v4: 資産クラスETF ----
    for label, ticker in ASSET_CLASS_TICKERS.items():
        data[label] = fetch_yf_series(label, ticker)

    data["米国M2 ($B)"] = fetch_fred("WM2NS", "米国M2")
    data["米国MMF残高 ($B)"] = fetch_fred("MMMFFAQ027S", "米国MMF") / 1000.0
    data["ハイイールド債スプレッド (%)"] = fetch_fred("BAMLH0A0HYM2", "ハイイールド債スプレッド")
    data["米国債ボラティリティ (bp)"] = _bond_vol(fetch_fred(US10Y_FRED_SERIES, "米10年債利回り"))
    data["ステーブルコイン時価総額 ($B)"] = fetch_stablecoin_mcap()
    return data


def _bond_vol(y10: pd.Series) -> pd.Series:
    """米10年債利回りの20営業日変動幅（bp）を MOVE の代替プロキシとして算出する。"""
    if y10 is None or y10.empty:
        return pd.Series(dtype=float)
    diff_bp = y10.diff() * 100.0
    return diff_bp.rolling(window=20, min_periods=10).std().dropna()


def _demo_data() -> dict[str, pd.Series]:
    """ネットワークなしでパイプラインを検証するためのダミーデータ。"""
    import numpy as np

    rng = np.random.default_rng(42)
    end = datetime.now(JST).date()
    days = pd.bdate_range(end=end, periods=LOOKBACK_DAYS)
    weekly = pd.date_range(end=end, periods=max(LOOKBACK_DAYS // 7, 4), freq="W-WED")
    quarterly = pd.date_range(end=end, periods=max(LOOKBACK_DAYS // 91, 3), freq="QE")

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
    d["VIX (株式恐怖指数)"] = walk(days, 15.0, 0.0, 0.045).clip(lower=11, upper=45)
    d["米国M2 ($B)"] = walk(weekly, 21000.0, 0.0015, 0.001)
    d["米国MMF高 ($B)"] = walk(quarterly, 6800.0, 0.01, 0.02)
    def reverted(index, level, vol, lo, hi):
        """水準が行き過ぎないよう平均回帰させたダミー系列。"""
        x = [level]
        for _ in range(len(index) - 1):
            nxt = x[-1] + (level - x[-1]) * 0.03 + rng.normal(0, vol)
            x.append(min(max(nxt, lo), hi))
        return pd.Series(x, index=index)

    d["ハイイールド債スプレッド (%)"] = reverted(days, 3.4, 0.06, 2.2, 6.5)
    d["米国債ボラティリティ (bp)"] = reverted(days, 8.0, 0.3, 3.0, 18.0)
    d["ステーブルコイン時価総額 ($B)"] = walk(days, 160.0, 0.0008, 0.004)

    # ---- v4: 世界指数ダミーデータ ----
    d["英国 (FTSE 100)"] = walk(days, 7800, 0.0003, 0.008)
    d["ドイツ (DAX)"] = walk(days, 18500, 0.0004, 0.009)
    d["中国 (上海総合)"] = walk(days, 3100, 0.0002, 0.011)
    d["香港 (ハンセン)"] = walk(days, 17500, 0.0001, 0.014)
    d["インド (Nifty 50)"] = walk(days, 23500, 0.0006, 0.009)
    d["ブラジル (BOVESPA)"] = walk(days, 125000, 0.0003, 0.013)
    d["オーストラリア (ASX)"] = walk(days, 8200, 0.0003, 0.008)
    d["カナダ (TSX)"] = walk(days, 24500, 0.0003, 0.008)

    # ---- v4: 資産クラスETFダミーデータ ----
    d["米長期債 (TLT)"] = walk(days, 95.0, -0.0002, 0.007)
    d["米中期債 (IEF)"] = walk(days, 102.0, -0.0001, 0.005)
    d["ハイールド債 (HYG)"] = walk(days, 78.0, 0.0002, 0.006)
    d["投資適格債 (LQD)"] = walk(days, 112.0, 0.0001, 0.005)
    d["不動産 (VNQ)"] = walk(days, 85.0, 0.0002, 0.010)
    d["新興国株式 (EEM)"] = walk(days, 42.0, 0.0003, 0.012)
    d["先進国株式 ex-US (EFA)"] = walk(days, 78.0, 0.0003, 0.009)
    d["原油 WTI (CL=F)"] = walk(days, 72.0, 0.0002, 0.018)

    return d


# =============================================================================
# E. 自動分析
# =============================================================================
PERIOD_LABELS = [
    ("1w", "1週間", 7),
    ("1m", "1ヶ月", 30),
    ("3m", "3ヶ月", 91),
    ("6m", "6ヶ月", 182),
    ("1y", "1年", 365),
    ("2y", "2年", 730),
]


def _last_date(s: pd.Series) -> str:
    if s is None or s.empty:
        return "—"
    return s.index[-1].strftime("%Y-%m-%d")


def _level_comment(vix: Optional[float], hy: Optional[float]) -> str:
    """VIX と HY スプレッドから市場ストレスの状態を判定する。"""
    flags = []
    if vix is not None:
        if vix >= 30:
            flags.append("VIXは30超で強い警戒")
        elif vix >= 20:
            flags.append("VIXは20台でやや警戒")
        else:
            flags.append("VIXは20未満で落ち着き")
    if hy is not None:
        if hy >= 6:
            flags.append("ハイイールド債スプレッドは6%超で信用不安が強い")
        elif hy >= 4:
            flags.append("ハイイールド債スプレッドは4%台でやや拡大")
        else:
            flags.append("ハイイールド債スプレッドは4%未満で信用環境は良好")
    return "、".join(flags) if flags else "ストレス指標の判定に必要なデータが不足しています"


def score_regime(data: dict[str, pd.Series]) -> tuple[str, int, int, list[tuple[str, bool]]]:
    """リスクオン/オフを機械的にスコアリングする（加点式・最大7点）。"""
    reasons: list[tuple[str, bool]] = []
    score = 0
    total = 0

    spx = data.get("米国株 (S&P 500)")
    if spx is not None and not spx.empty:
        total += 1
        dev = deviation_from_sma(spx, 200)
        if dev is not None and dev > 0:
            score += 1
            reasons.append((f"S&P 500 は200日移動平均を {dev:+.1f}% 上回る（上昇基調）", True))
        elif dev is not None:
            reasons.append((f"S&P 500 は200日移動平均を {dev:+.1f}% 下回る（調整局面）", False))

    vix = data.get("VIX (株式恐怖指数)")
    if vix is not None and not vix.empty:
        total += 1
        if vix.iloc[-1] < 20:
            score += 1
            reasons.append((f"VIX は {vix.iloc[-1]:.1f} と低水準（投資家の警戒感は小さい）", True))
        else:
            reasons.append((f"VIX は {vix.iloc[-1]:.1f} と高め（警戒感が残る）", False))

    hy = data.get("ハイイールド債スプレッド (%)")
    if hy is not None and not hy.empty:
        total += 1
        if hy.iloc[-1] < 4.0:
            score += 1
            reasons.append((f"ハイイールド債スプレッドは {hy.iloc[-1]:.2f}% で信用環境は良好", True))
        else:
            reasons.append((f"ハイイールド債スプレッドは {hy.iloc[-1]:.2f}% で信用リスクが意識される", False))

    dxy = data.get("米ドル指数 (DXY)")
    if dxy is not None and not dxy.empty:
        total += 1
        m1 = period_return(dxy, 30)
        if m1 is not None and m1 < 0:
            score += 1
            reasons.append((f"ドル指数は1ヶ月で {m1:+.2f}% と下落（ドル安は新興国・リスク資産に追い風）", True))
        elif m1 is not None:
            reasons.append((f"ドル指数は1ヶ月で {m1:+.2f}% と上昇（ドル高はリスク資産の重荷）", False))

    stable = data.get("ステーブルコイン時価総額 ($B)")
    if stable is not None and not stable.empty:
        total += 1
        m1 = period_return(stable, 30)
        if m1 is not None and m1 > 0:
            score += 1
            reasons.append((f"ステーブルコイン残高は1ヶ月で {m1:+.2f}% 増加（暗号資産市場へ資金流入）", True))
        elif m1 is not None:
            reasons.append((f"ステーブルコイン残高は1ヶ月で {m1:+.2f}% 減少（暗号資産市場から資金流出）", False))

    m2 = data.get("米国M2 ($B)")
    if m2 is not None and not m2.empty:
        total += 1
        d90 = period_change_abs(m2, 91)
        if d90 is not None and d90 > 0:
            score += 1
            reasons.append((f"米国M2は3ヶ月で {d90:+,.0f} B$ 増加（マクロの流動性は拡大方向）", True))
        elif d90 is not None:
            reasons.append((f"米国M2は3ヶ月で {d90:+,.0f} B$ 減少（流動性は縮小方向）", False))

    mmf = data.get("米国MMF残高 ($B)")
    if mmf is not None and not mmf.empty:
        total += 1
        d90 = period_change_abs(mmf, 91)
        if d90 is not None and d90 < 0:
            score += 1
            reasons.append((f"米国MMF残高は3ヶ月で {d90:+,.0f} B$ 減少（待機資金が市場へ向かう兆候）", True))
        elif d90 is not None:
            reasons.append((f"米国MMF残高は3ヶ月で {d90:+,.0f} B$ 増加（待機資金が積み上がる局面）", False))

    if total == 0:
        return "判定不能", 0, 0, [("データが不足しているため判定できません。", False)]
    ratio = score / total
    if ratio >= 0.7:
        label = "リスクオン（資金がリスク資産へ向かう地合い）"
    elif ratio >= 0.4:
        label = "中立（強弱まちまち／様子見）"
    else:
        label = "リスクオフ（現金・安全資産へ資金が退避する地合い）"
    return label, score, total, reasons


def build_performance_table(data: dict[str, pd.Series]) -> list[dict]:
    """期間別リターン表の行データを作る。"""
    rows: list[dict] = []
    for _key, label in RISK_ASSETS:
        s = data.get(label)
        if s is None or s.empty:
            continue
        row = {"label": label, "last": _fmt(s.iloc[-1]), "values": {}, "date": _last_date(s)}
        for pk, _pl, days in PERIOD_LABELS:
            row["values"][pk] = period_return(s, days)
        row["values"]["all"] = _pct_change_n(s, len(s) - 1)
        rows.append(row)

    for label, unit, digits in [
        ("ステーブルコイン時価総額 ($B)", " B$", 1),
        ("米国M2 ($B)", " B$", 0),
        ("米国MMF残高 ($B)", " B$", 0),
    ]:
        s = data.get(label)
        if s is None or s.empty:
            continue
        row = {"label": label, "last": _fmt(s.iloc[-1], unit, digits),
               "values": {}, "date": _last_date(s)}
        for pk, _pl, days in PERIOD_LABELS:
            row["values"][pk] = period_return(s, days)
        row["values"]["all"] = _pct_change_n(s, len(s) - 1)
        rows.append(row)

    for label, unit, digits in [
        ("米ドル指数 (DXY)", "", 1),
        ("VIX (株式恐怖指数)", "", 2),
        ("ハイイールド債スプレッド (%)", " %", 2),
        ("米国債ボラティリティ (bp)", " bp", 1),
    ]:
        s = data.get(label)
        if s is None or s.empty:
            continue
        row = {"label": label, "last": _fmt(s.iloc[-1], unit, digits),
               "values": {}, "date": _last_date(s)}
        for pk, _pl, days in PERIOD_LABELS:
            row["values"][pk] = None   # 水準指標は変化率を出さず「—」表示
        row["values"]["all"] = None
        rows.append(row)
    return rows


def build_analysis(data: dict[str, pd.Series]) -> dict:
    """自動分析の結果をまとめて返す。"""
    regime_label, score, total, reasons = score_regime(data)

    vix = data.get("VIX (株式恐怖指数)")
    hy = data.get("ハイイールド債スプレッド (%)")
    stress_text = _level_comment(
        float(vix.iloc[-1]) if vix is not None and not vix.empty else None,
        float(hy.iloc[-1]) if hy is not None and not hy.empty else None,
    )

    # ハイライト: 期間リターンの上位／下位
    ranked = []
    for _key, label in RISK_ASSETS:
        s = data.get(label)
        if s is None or s.empty:
            continue
        r = _pct_change_n(s, len(s) - 1)
        if r is not None:
            ranked.append((label, r))
    ranked.sort(key=lambda x: x[1], reverse=True)
    best = ranked[0] if ranked else None
    worst = ranked[-1] if ranked else None

    # ボラティリティと最大DD（S&P 500 と BTC）
    risk_metrics = []
    for label in ("米国株 (S&P 500)", "ビットコイン (BTC)", "日本株 (日経平均)"):
        s = data.get(label)
        if s is None or s.empty:
            continue
        risk_metrics.append({
            "label": label,
            "vol": annualized_vol(s, 60),
            "mdd": max_drawdown(s),
            "sma_dev": deviation_from_sma(s, 200),
        })

    # 流動性の変化
    liquidity = []
    for label, unit, digits in [
        ("米国M2 ($B)", " B$", 0),
        ("米国MMF残高 ($B)", " B$", 0),
        ("ステーブルコイン時価総額 ($B)", " B$", 1),
    ]:
        s = data.get(label)
        if s is None or s.empty:
            continue
        liquidity.append({
            "label": label,
            "level": _fmt(s.iloc[-1], unit, digits),
            "d1m": period_change_abs(s, 30),
            "d3m": period_change_abs(s, 91),
            "unit": unit,
            "digits": digits,
            "date": _last_date(s),
        })

    return {
        "regime_label": regime_label,
        "score": score,
        "total": total,
        "reasons": reasons,
        "stress_text": stress_text,
        "best": best,
        "worst": worst,
        "risk_metrics": risk_metrics,
        "liquidity": liquidity,
        "table": build_performance_table(data),
    }


# =============================================================================
# F. HTMLテンプレート（v4改: レイアウト問題修正版）
# =============================================================================
HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Global Money Flow — 資金フロー・ライブボード</title>
<style>
  :root{
    --ink:#e9eefa; --ink2:#a8b7cd; --muted:#6f819a;
    --line:rgba(148,163,184,.14); --line2:rgba(148,163,184,.07);
    --up:#34d399; --down:#fb7185; --flat:#8fa3bd;
    --accent:#22d3ee; --accent2:#5eead4;
    --panelbg:rgba(16,25,41,.74);
  }
  *{box-sizing:border-box}
  html,body{height:100%}
  html{-webkit-text-size-adjust:100%}
  body{
    margin:0; color:var(--ink); overflow:hidden;
    font-family:'Hiragino Kaku Gothic ProN','Hiragino Sans','Yu Gothic UI','Yu Gothic','Meiryo','Noto Sans JP','Segoe UI',sans-serif;
    font-size:13px; line-height:1.55; -webkit-font-smoothing:antialiased;
    background:
      radial-gradient(1100px 560px at 10% -12%, rgba(34,211,238,.13), transparent 62%),
      radial-gradient(980px 520px at 96% -6%, rgba(94,234,212,.09), transparent 58%),
      radial-gradient(900px 700px at 50% 120%, rgba(59,130,246,.10), transparent 60%),
      #060a12;
  }
  .app{height:100vh; height:100dvh; display:grid; grid-template-rows:auto auto minmax(0,1fr) auto; gap:0; overflow:hidden}

  /* ---------- header ---------- */
  header{display:flex; align-items:center; justify-content:space-between; gap:14px; padding:10px 16px 8px}
  .brand{display:flex; align-items:center; gap:11px; min-width:0}
  .logo{width:32px;height:32px;border-radius:10px;flex:0 0 auto;
    background:conic-gradient(from 210deg,#22d3ee,#5eead4,#38bdf8,#22d3ee);
    box-shadow:0 0 0 1px rgba(255,255,255,.10), 0 8px 22px -10px rgba(34,211,238,.8);
    display:grid;place-items:center;color:#04121a;font-weight:800;font-size:15px}
  h1{margin:0;font-size:16px;font-weight:800;letter-spacing:.01em;white-space:nowrap}
  h1 small{display:block;font-size:10.5px;font-weight:500;color:var(--muted);letter-spacing:.02em;margin-top:1px}
  .topright{display:flex;align-items:center;gap:12px;min-width:0}
  .stamp{display:flex;align-items:center;gap:7px;flex-wrap:wrap;justify-content:flex-end}
  .stamp .lbl{font-size:9.5px;letter-spacing:.08em;text-transform:uppercase;color:var(--muted);
    border:1px solid var(--line);border-radius:999px;padding:1px 7px}
  .stamp b{font-size:11.5px;font-weight:700;font-variant-numeric:tabular-nums;color:var(--ink2)}
  .ghost{background:rgba(148,163,184,.08);border:1px solid var(--line);color:var(--ink2);
    border-radius:9px;width:28px;height:28px;cursor:pointer;font-size:13px;line-height:1;transition:.18s}
  .ghost:hover{background:rgba(34,211,238,.16);color:var(--accent);border-color:rgba(34,211,238,.4)}

  /* ---------- ticker ---------- */
  .ticker{overflow:hidden;border-top:1px solid var(--line2);border-bottom:1px solid var(--line2);
    background:rgba(8,14,24,.55);height:30px;display:flex;align-items:center;
    mask-image:linear-gradient(90deg,transparent,#000 3%,#000 97%,transparent)}
  .tape{display:flex;gap:26px;white-space:nowrap;animation:slide 46s linear infinite;padding-left:12px}
  .ticker:hover .tape{animation-play-state:paused}
  @keyframes slide{from{transform:translateX(0)}to{transform:translateX(-50%)}}
  .tape span{display:inline-flex;align-items:center;gap:7px;font-size:11.5px;color:var(--ink2)}
  .tape b{color:var(--ink);font-weight:700;font-variant-numeric:tabular-nums}
  .tape em{font-style:normal;font-variant-numeric:tabular-nums;font-weight:700;font-size:11px}
  .tape em.up{color:var(--up)} .tape em.down{color:var(--down)} .tape em.flat{color:var(--flat)}

  /* ---------- grid ---------- */
  .grid{display:grid;grid-template-columns:320px minmax(0,1fr) 348px;gap:11px;padding:11px 14px;min-height:0;overflow:hidden}
  .col{display:flex;flex-direction:column;gap:11px;min-height:0}
  .panel{background:var(--panelbg);border:1px solid var(--line);border-radius:15px;padding:12px 13px;
    display:flex;flex-direction:column;min-height:0;
    box-shadow:0 14px 34px -24px rgba(0,0,0,.95), inset 0 1px 0 rgba(255,255,255,.035);
    backdrop-filter:blur(9px)}
  .panel-h{display:flex;align-items:center;justify-content:space-between;gap:8px;
    font-size:10.5px;font-weight:800;letter-spacing:.10em;text-transform:uppercase;color:var(--muted);
    margin:0 0 9px;flex:0 0 auto}
  .panel-h .r{font-weight:600;letter-spacing:0;text-transform:none;font-size:10px;color:#5f728a}

  /* ---------- gauge ---------- */
  .gaugeBox{position:relative;flex:0 0 auto;display:flex;flex-direction:column;align-items:center;padding:2px 0 0}
  .gaugeBox svg{width:100%;max-width:250px;height:auto;display:block}
  .gaugeCenter{position:absolute;left:0;right:0;top:44%;text-align:center;pointer-events:none}
  .gaugeCenter .n{font-size:34px;font-weight:800;line-height:1;font-variant-numeric:tabular-nums;
    background:linear-gradient(180deg,#e9eefa,#7dd3fc);-webkit-background-clip:text;background-clip:text;color:transparent}
  .gaugeCenter .d{font-size:10.5px;color:var(--muted);margin-top:3px;letter-spacing:.06em}
  .regimeName{margin:7px 0 2px;text-align:center;font-size:13px;font-weight:800;line-height:1.4}
  .regimeName .tagline{display:block;font-size:10px;font-weight:600;color:var(--muted);letter-spacing:.03em;margin-top:2px}
  .reasons{list-style:none;margin:8px 0 0;padding:0;display:flex;flex-direction:column;gap:5px;
    overflow:auto;scrollbar-width:thin;min-height:0;flex:1 1 auto}
  .reasons::-webkit-scrollbar{width:5px}
  .reasons::-webkit-scrollbar-thumb{background:rgba(148,163,184,.25);border-radius:3px}
  .reasons li{position:relative;padding-left:15px;font-size:11px;line-height:1.5;color:var(--ink2)}
  .reasons li::before{content:"";position:absolute;left:3px;top:.52em;width:5px;height:5px;border-radius:50%;background:#64748b}
  .reasons li.on::before{background:var(--up);box-shadow:0 0 8px rgba(52,211,153,.75)}
  .reasons li.off::before{background:var(--down);box-shadow:0 0 8px rgba(251,113,133,.6)}

  /* ---------- tabs / chart ---------- */
  .chartPanel{flex:1 1 auto;min-height:0;padding:10px 12px 8px;display:flex;flex-direction:column}
  .tabsRow{display:flex;align-items:center;justify-content:space-between;gap:10px;flex:0 0 auto;margin-bottom:6px;flex-wrap:wrap}
  .tabs{display:flex;gap:4px;background:rgba(148,163,184,.07);padding:3px;border-radius:11px;border:1px solid var(--line2)}
  .tab{appearance:none;border:0;background:transparent;color:var(--ink2);font-family:inherit;
    font-size:11.5px;font-weight:700;padding:5px 11px;border-radius:8px;cursor:pointer;transition:.18s;white-space:nowrap}
  .tab:hover{color:#e9eefa;background:rgba(148,163,184,.10)}
  .tab.active{background:linear-gradient(180deg,rgba(34,211,238,.28),rgba(34,211,238,.14));
    color:#d7fbff;box-shadow:0 0 0 1px rgba(34,211,238,.45), 0 6px 16px -10px rgba(34,211,238,.9)}
  .tools{display:flex;align-items:center;gap:6px}
  .periods{display:flex;gap:2px;background:rgba(148,163,184,.07);padding:3px;border-radius:9px;border:1px solid var(--line2)}
  .pbtn{appearance:none;border:0;background:transparent;color:var(--muted);font-family:inherit;
    font-size:10.5px;font-weight:700;padding:3px 7px;border-radius:6px;cursor:pointer;transition:.16s}
  .pbtn:hover{color:var(--ink2)}
  .pbtn.active{background:rgba(94,234,212,.18);color:#d7fff7;box-shadow:0 0 0 1px rgba(94,234,212,.35)}
  .autobtn{font-size:10px;padding:3px 8px;border-radius:7px;border:1px solid var(--line);background:rgba(148,163,184,.07);
    color:var(--muted);cursor:pointer;font-family:inherit;font-weight:700;transition:.16s}
  .autobtn.on{color:#d7fff7;border-color:rgba(94,234,212,.42);background:rgba(94,234,212,.15)}
  .chartShell{position:relative;flex:1 1 auto;min-height:0}

  /* 修正: .plot はデフォルトで static、flowWorldPlot 以外で absolute */
  .plot{position:relative;width:100%;height:100%;opacity:1;transition:opacity .32s ease}
  .plot:not(.flowWorldPlot){position:absolute;inset:0}
  .js-plotly-plot .plotly .modebar{display:none!important}

  /* ---------- flowworld 専用スタイル ---------- */
  .flowWorldPlot{display:flex;flex-direction:column;gap:8px;height:100%}
  .fwMap{flex:3;min-height:0;border-radius:10px;border:1px solid var(--line2);background:rgba(8,14,24,.35)}
  .fwBottom{flex:2;min-height:0;display:grid;grid-template-columns:2fr 1fr;gap:8px}
  .fwTime,.fwWheel{border-radius:10px;border:1px solid var(--line2);background:rgba(8,14,24,.35);min-height:0}

  /* ---------- heat strip ---------- */
  .heatPanel{flex:0 0 auto;padding:9px 12px 10px}
  table.heat{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums;font-size:10.8px}
  table.heat th{font-size:9.5px;font-weight:700;letter-spacing:.06em;color:var(--muted);text-align:right;
    padding:3px 5px;border-bottom:1px solid var(--line2);white-space:nowrap}
  table.heat th:first-child{text-align:left}
  table.heat td{padding:3px 5px;text-align:right;color:var(--ink2);white-space:nowrap;border-bottom:1px solid rgba(148,163,184,.05)}
  table.heat td:first-child{text-align:left;color:var(--ink);font-weight:600;font-size:10.5px}
  table.heat td.h{font-weight:700;border-radius:5px}
  table.heat td.h.pos{color:#d1fae5} table.heat td.h.neg{color:#ffe4e6}
  table.heat td.na{color:#43536b}

  /* ---------- metrics list ---------- */
  .metrics{flex:1 1 auto;min-height:0}
  .mlist{display:flex;flex-direction:column;overflow:auto;min-height:0;scrollbar-width:thin;margin:0 -4px;padding:0 4px;flex:1 1 auto}
  .mlist::-webkit-scrollbar{width:5px}
  .mlist::-webkit-scrollbar-thumb{background:rgba(148,163,184,.25);border-radius:3px}
  .mrow{display:grid;grid-template-columns:1fr auto;gap:2px 8px;padding:5px 0;border-bottom:1px solid rgba(148,163,184,.06)}
  .mrow:last-child{border-bottom:0}
  .mrow .nm{font-size:11.3px;color:var(--ink2);font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .mrow .vl{font-size:12.5px;font-weight:800;font-variant-numeric:tabular-nums;text-align:right;line-height:1.2}
  .mrow .rt{display:flex;align-items:center;gap:6px;grid-column:1/-1}
  .spark{width:66px;height:16px;flex:0 0 auto;opacity:.9}
  .chip{font-size:10px;font-weight:800;padding:1px 5px;border-radius:5px;font-variant-numeric:tabular-nums;white-space:nowrap}
  .chip.up{color:#062e22;background:rgba(52,211,153,.85)}
  .chip.down{color:#3d0713;background:rgba(251,113,133,.85)}
  .chip.flat{color:#0b1220;background:rgba(148,163,184,.55)}
  .chip.ghost{color:var(--muted);background:rgba(148,163,184,.10);font-weight:700}

  /* ---------- status / footer ---------- */
  .statusPanel{flex:0 0 auto;padding:10px 12px}
  .sgrid{display:grid;grid-template-columns:1fr 1fr;gap:4px 10px;font-size:10.3px;color:var(--muted)}
  .sitem{display:flex;align-items:center;gap:5px;min-width:0}
  .dot{width:6px;height:6px;border-radius:50%;flex:0 0 auto}
  .dot.ok{background:var(--up);box-shadow:0 0 7px rgba(52,211,153,.8)}
  .dot.ng{background:var(--down);box-shadow:0 0 7px rgba(251,113,133,.8)}
  .sitem span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  footer{display:flex;align-items:center;justify-content:space-between;gap:14px;flex-wrap:wrap;
    padding:6px 16px 8px;border-top:1px solid var(--line2);font-size:10px;color:#5a6b82}
  footer b{color:var(--ink2);font-weight:700}
  footer code{background:rgba(148,163,184,.10);padding:1px 4px;border-radius:4px;font-size:9.5px}

  @media (max-height:840px){
    body{font-size:12px}
    header{padding:7px 14px 6px}
    h1{font-size:14.5px}
    .grid{padding:8px 12px;gap:9px}
    .panel{padding:9px 10px;border-radius:13px}
    .gaugeCenter .n{font-size:27px}
    .reasons li{font-size:10.2px}
    .mrow{padding:3.5px 0}
    .chartPanel{padding:8px 10px 6px}
  }
  @media (max-width:1180px), (max-height:660px){
    body{overflow:auto}
    .app{height:auto;min-height:100vh;overflow:visible}
    .grid{grid-template-columns:1fr;overflow:visible}
    .chartShell{height:52vh;min-height:330px;position:relative}
    .metrics,.reasons{overflow:visible}
    .mlist{overflow:visible}
    .flowWorldPlot{height:auto;min-height:500px}
  }
</style>
</head>
<body>
<div class="app">
  <header>
    <div class="brand">
      <div class="logo">◎</div>
      <div>
        <h1>Global Money Flow<small>世界の資金フロー定点観測 ／ リスク資産・ストレス・流動性</small></h1>
      </div>
    </div>
    <div class="topright">
      <div class="stamp">
        <span class="lbl">最終更新</span><b>__UPDATED_AT__</b>
        <span class="lbl">観測期間</span><b>__PERIOD__</b>
      </div>
      <button class="ghost" id="fsBtn" title="全画面表示（F）">⛶</button>
    </div>
  </header>

  <div class="ticker"><div class="tape">__TICKER__</div></div>

  <main class="grid">
    <section class="col left">
      <div class="panel" style="flex:0 1 auto">
        <div class="panel-h">地合い判定<span class="r">__SCORE__ / __TOTAL__ 項目</span></div>
        <div class="gaugeBox">
          <svg viewBox="0 0 240 132" aria-hidden="true">
            <defs>
              <linearGradient id="gg" x1="0" y1="1" x2="1" y2="0">
                <stop offset="0%" stop-color="#fb7185"/>
                <stop offset="52%" stop-color="#fbbf24"/>
                <stop offset="100%" stop-color="#34d399"/>
              </linearGradient>
            </defs>
            <path d="M22,118 A98,98 0 0,1 218,118" fill="none" stroke="rgba(148,163,184,.16)" stroke-width="13" stroke-linecap="round"/>
            <path id="gaugeArc" d="M22,118 A98,98 0 0,1 218,118" fill="none" stroke="url(#gg)" stroke-width="13"
                  stroke-linecap="round" stroke-dasharray="308" stroke-dashoffset="308" data-ratio="__RATIO__"
                  style="transition:stroke-dashoffset 1.1s cubic-bezier(.22,.9,.24,1)"/>
          </svg>
          <div class="gaugeCenter">
            <div class="n" id="gaugeNum" data-num="__SCOREF__" data-dec="0">0</div>
            <div class="d">RISK-ON SCORE</div>
          </div>
        </div>
        <div class="regimeName">__REGIME__</div>
        <ul class="reasons">__REASONS__</ul>
      </div>
      <div class="panel" style="flex:1 1 auto">
        <div class="panel-h">ストレス要約</div>
        <p style="margin:0 0 8px;font-size:11.2px;line-height:1.6;color:var(--ink2)">__STRESS__</p>
        <div class="panel-h" style="margin:6px 0 6px">リスク指標</div>
        <div class="mlist" style="gap:0">__RISKM__</div>
      </div>
    </section>

    <section class="col center">
      <div class="panel chartPanel">
        <div class="tabsRow">
          <div class="tabs">__TABS__</div>
          <div class="tools">
            <div class="periods">__PERIODBTN__</div>
            <button class="autobtn on" id="autoBtn" title="9秒ごとに表示を自動で切り替えます">AUTO</button>
          </div>
        </div>
        <div class="chartShell">__CHARTS__</div>
      </div>
      <div class="panel heatPanel">
        <div class="panel-h">期間別パフォーマンス<span class="r">単位 %／直近データ日から各期間前との比較</span></div>
        __HEAT__
      </div>
    </section>

    <section class="col right">
      <div class="panel metrics">
        <div class="panel-h">指標スナップショット<span class="r">1M比</span></div>
        <div class="mlist">__METRICS__</div>
      </div>
      <div class="panel statusPanel">
        <div class="panel-h" style="margin-bottom:7px">データ取得状況<span class="r">__STATUS_SUMMARY__</span></div>
        <div class="sgrid">__STATUS__</div>
      </div>
    </section>
  </main>

  <footer>
    <div>__FOOTER_LEFT__</div>
    <div>__FOOTER_RIGHT__</div>
  </footer>
</div>

<script>__PLOTLYJS__</script>
<script>
const DASH = __PAYLOAD__;
const FONT = "'Hiragino Kaku Gothic ProN','Hiragino Sans','Yu Gothic UI','Meiryo','Noto Sans JP',sans-serif";
const state = {tab:0, period:'ALL', auto:true, hover:false, rendered:{}};

/* ---------- ユーティリティ ---------- */
function hexToRgba(hex, alpha) {
  var r = parseInt(hex.slice(1, 3), 16);
  var g = parseInt(hex.slice(3, 5), 16);
  var b = parseInt(hex.slice(5, 7), 16);
  return 'rgba(' + r + ',' + g + ',' + b + ',' + alpha + ')';
}

function tracesFor(g) {
  return g.traces.map(function(t) {
    var tr = {
      x: t.dates, y: t.values, name: t.name, type: 'scatter',
      mode: t.markers ? 'lines+markers' : 'lines',
      marker: t.markers ? {size: 4, color: t.color} : undefined,
      line: {color: t.color, width: t.width || 2, dash: t.dash || 'solid', shape: 'linear'},
      hovertemplate: '%{y:,.2f}<extra>' + t.name + '</extra>',
      yaxis: t.axis || 'y'
    };
    if (t.fill) { tr.fill = 'tozeroy'; tr.fillcolor = t.fillcolor || 'rgba(34,211,238,.12)'; }
    return tr;
  });
}

function axisTitle(txt) { return {text: txt || '', font: {size: 9.5, color: '#6f819a'}, standoff: 6}; }

function layoutFor(g) {
  var l = {
    paper_bgcolor: 'rgba(0,0,0,0)', plot_bgcolor: 'rgba(0,0,0,0)',
    margin: {l: 2, r: g.y2 ? 2 : 10, t: 22, b: 2},
    font: {family: FONT, size: 10.2, color: '#7c8ba1'},
    hovermode: 'x unified', dragmode: 'pan', showlegend: true,
    legend: {orientation: 'h', x: 0, y: 1.14, xanchor: 'left', font: {size: 10, color: '#a8b7cd'}, bgcolor: 'rgba(0,0,0,0)'},
    xaxis: {type: 'date', gridcolor: 'rgba(148,163,184,.09)', zeroline: false,
      tickfont: {size: 9.5, color: '#6f819a'}, ticks: 'outside', tickcolor: 'rgba(148,163,184,.18)',
      showspikes: true, spikemode: 'across', spikethickness: 1, spikedash: 'dot', spikecolor: 'rgba(94,234,212,.45)'},
    yaxis: {gridcolor: 'rgba(148,163,184,.09)', zeroline: false, automargin: true,
      tickfont: {size: 9.5, color: '#6f819a'}, title: axisTitle(g.y1)},
    hoverlabel: {bgcolor: 'rgba(9,15,26,.96)', bordercolor: 'rgba(94,234,212,.35)',
      font: {family: FONT, size: 10.5, color: '#e9eefa'}},
    transition: {duration: 320, easing: 'cubic-in-out'}
  };
  if (g.y2) {
    l.yaxis2 = {overlaying: 'y', side: 'right', showgrid: false, zeroline: false, automargin: true,
      tickfont: {size: 9.5, color: '#6f819a'}, title: axisTitle(g.y2)};
  }
  if (g.hline) {
    l.shapes = [{type: 'line', xref: 'paper', x0: 0, x1: 1, y0: g.hline, y1: g.hline,
      line: {color: 'rgba(148,163,184,.32)', width: 1, dash: 'dot'}}];
  }
  return l;
}

/* ---------- 通常チャート描画 ---------- */
function renderChart(i) {
  var g = DASH.groups[i];
  var el = document.getElementById('chart' + i);
  if (!el || g.is_flowworld) return;
  Plotly.newPlot(el, tracesFor(g), layoutFor(g),
    {displayModeBar: false, responsive: true, scrollZoom: true, doubleClick: 'reset'});
  state.rendered[i] = true;
  applyPeriod(i);
}

function applyPeriod(i) {
  var g = DASH.groups[i], el = document.getElementById('chart' + i);
  if (!el || !state.rendered[i] || g.is_flowworld) return;
  if (state.period === 'ALL') { Plotly.relayout(el, {'xaxis.autorange': true}); return; }
  var days = {'1M': 30, '3M': 91, '6M': 182, '1Y': 365}[state.period] || 30;
  var d = g.traces[0].dates;
  if (!d || !d.length) return;
  var last = new Date(d[d.length - 1] + 'T00:00:00');
  var start = new Date(last.getTime() - days * 86400000);
  Plotly.relayout(el, {'xaxis.autorange': false, 'xaxis.range': [iso(start), iso(last)]});
}

function iso(dt) {
  var m = ('0' + (dt.getMonth() + 1)).slice(-2), day = ('0' + dt.getDate()).slice(-2);
  return dt.getFullYear() + '-' + m + '-' + day;
}

/* ---------- FlowWorld 描画（v4改: レイアウト修正版） ---------- */
function renderFlowWorld(i) {
  var flow = DASH.flow;
  if (!flow || !flow.nodes) return;

  // サンキー図
  var sankeyData = [{
    type: 'sankey',
    orientation: 'h',
    node: {
      pad: 10,
      thickness: 14,
      line: {color: 'rgba(148,163,184,.2)', width: 0.5},
      label: flow.nodes,
      color: flow.nodes.map(function(_, idx) {
        var colors = ['#22d3ee','#5eead4','#38bdf8','#818cf8','#f472b6','#a78bfa','#fbbf24','#fb923c','#34d399'];
        return colors[idx % colors.length];
      })
    },
    link: {
      source: flow.links.map(function(l) { return l.source; }),
      target: flow.links.map(function(l) { return l.target; }),
      value: flow.links.map(function(l) { return l.value; }),
      color: flow.links.map(function(l) {
        var nodeColors = ['#22d3ee','#5eead4','#38bdf8','#818cf8','#f472b6','#a78bfa','#fbbf24','#fb923c','#34d399'];
        var c = nodeColors[l.source % nodeColors.length];
        return hexToRgba(c, 0.25);
      })
    }
  }];

  var sankeyLayout = {
    paper_bgcolor: 'rgba(0,0,0,0)', plot_bgcolor: 'rgba(0,0,0,0)',
    margin: {l: 8, r: 8, t: 8, b: 8},
    font: {family: FONT, size: 9, color: '#7c8ba1'},
    hoverlabel: {bgcolor: 'rgba(9,15,26,.96)', bordercolor: 'rgba(94,234,212,.35)', font: {family: FONT, size: 10, color: '#e9eefa'}}
  };

  var sankeyEl = document.getElementById('fwMap' + i);
  if (sankeyEl) {
    Plotly.newPlot(sankeyEl, sankeyData, sankeyLayout, {displayModeBar: false, responsive: true});
  }

  // ヒートマップ
  var hm = DASH.flow_heatmap;
  if (hm && hm.length) {
    var regions = {};
    var classes = {};
    hm.forEach(function(row) {
      regions[row.region] = true;
      classes[row['class']] = true;
    });
    var regionList = Object.keys(regions);
    var classList = Object.keys(classes);

    var z = regionList.map(function(r) {
      return classList.map(function(c) {
        var cell = hm.find(function(x) { return x.region === r && x['class'] === c; });
        return cell ? cell.score : 0;
      });
    });

    var heatmapData = [{
      type: 'heatmap',
      z: z,
      x: classList,
      y: regionList,
      colorscale: [[0, '#0f172a'], [0.5, '#fbbf24'], [1, '#34d399']],
      showscale: false,
      hovertemplate: '%{y} × %{x}<br>スコア: %{z:.2f}<extra></extra>'
    }];

    var heatmapLayout = {
      paper_bgcolor: 'rgba(0,0,0,0)', plot_bgcolor: 'rgba(0,0,0,0)',
      margin: {l: 60, r: 8, t: 8, b: 32},
      font: {family: FONT, size: 9, color: '#7c8ba1'},
      xaxis: {tickfont: {size: 9}, showgrid: false},
      yaxis: {tickfont: {size: 9}, showgrid: false}
    };

    var timeEl = document.getElementById('fwTime' + i);
    if (timeEl) {
      Plotly.newPlot(timeEl, heatmapData, heatmapLayout, {displayModeBar: false, responsive: true});
    }
  }

  // ローテーションウィール（ドーナツ）
  var rot = DASH.flow_rotation;
  if (rot && rot.labels) {
    var pieData = [{
      type: 'pie',
      labels: rot.labels,
      values: rot.values,
      hole: 0.55,
      marker: {colors: rot.colors || ['#38bdf8', '#818cf8', '#fbbf24', '#f472b6', '#34d399']},
      textinfo: 'label+percent',
      textposition: 'outside',
      textfont: {family: FONT, size: 9, color: '#a8b7cd'},
      hovertemplate: '%{label}: %{percent}<extra></extra>'
    }];

    var pieLayout = {
      paper_bgcolor: 'rgba(0,0,0,0)', plot_bgcolor: 'rgba(0,0,0,0)',
      margin: {l: 4, r: 4, t: 4, b: 4},
      font: {family: FONT, size: 9, color: '#7c8ba1'},
      showlegend: false,
      annotations: [{
        text: '資産<br>配分',
        x: 0.5, y: 0.5, font: {size: 11, color: '#a8b7cd', family: FONT},
        showarrow: false
      }]
    };

    var wheelEl = document.getElementById('fwWheel' + i);
    if (wheelEl) {
      Plotly.newPlot(wheelEl, pieData, pieLayout, {displayModeBar: false, responsive: true});
    }
  }

  state.rendered[i] = true;
}

/* ---------- タブ切り替え（v4改: 表示制御修正） ---------- */
function showTab(i) {
  state.tab = i;
  var tabs = document.querySelectorAll('.tab');
  for (var k = 0; k < tabs.length; k++) tabs[k].classList.toggle('active', k === i);

  var plots = document.querySelectorAll('.plot');
  for (var k2 = 0; k2 < plots.length; k2++) {
    if (k2 === i) {
      plots[k2].style.display = 'block';
      plots[k2].style.opacity = '0';
      setTimeout((function(idx) {
        return function() {
          plots[idx].style.opacity = '1';
          // 表示後にリサイズを強制
          if (DASH.groups[idx] && DASH.groups[idx].is_flowworld) {
            renderFlowWorld(idx);
          } else {
            var el = document.getElementById('chart' + idx);
            if (el && window.Plotly) Plotly.Plots.resize(el);
          }
        };
      })(k2), 40);
    } else {
      plots[k2].style.display = 'none';
      plots[k2].style.opacity = '0';
    }
  }

  if (!state.rendered[i]) {
    if (DASH.groups[i] && DASH.groups[i].is_flowworld) {
      // flowworld は表示後に描画
      setTimeout(function() { renderFlowWorld(i); }, 60);
    } else {
      renderChart(i);
    }
  } else {
    var el = document.getElementById('chart' + i);
    if (el && window.Plotly) Plotly.Plots.resize(el);
  }
  applyPeriod(i);
}

/* ---------- イベントリスナー ---------- */
document.querySelectorAll('.tab').forEach(function(b) {
  b.addEventListener('click', function() {
    state.auto = false;
    autoBtn.classList.remove('on');
    autoBtn.textContent = 'MANUAL';
    showTab(parseInt(b.dataset.i, 10));
  });
});

var chartPanelEl = document.querySelector('.chartPanel');
chartPanelEl.addEventListener('mouseenter', function() { state.hover = true; });
chartPanelEl.addEventListener('mouseleave', function() { state.hover = false; });

document.querySelectorAll('.pbtn').forEach(function(b) {
  b.addEventListener('click', function() {
    state.period = b.dataset.p;
    document.querySelectorAll('.pbtn').forEach(function(x) { x.classList.toggle('active', x === b); });
    for (var i = 0; i < DASH.groups.length; i++) { if (state.rendered[i]) applyPeriod(i); }
  });
});

var autoBtn = document.getElementById('autoBtn');
autoBtn.addEventListener('click', function() {
  state.auto = !state.auto;
  autoBtn.classList.toggle('on', state.auto);
  autoBtn.textContent = state.auto ? 'AUTO' : 'MANUAL';
});

setInterval(function() {
  if (!state.auto || state.hover) return;
  var next = (state.tab + 1) % DASH.groups.length;
  showTab(next);
}, 9000);

document.getElementById('fsBtn').addEventListener('click', function() {
  if (!document.fullscreenElement) { document.documentElement.requestFullscreen && document.documentElement.requestFullscreen(); }
  else { document.exitFullscreen && document.exitFullscreen(); }
});

document.addEventListener('keydown', function(e) {
  if (e.key === 'ArrowRight') showTab((state.tab + 1) % DASH.groups.length);
  if (e.key === 'ArrowLeft') showTab((state.tab - 1 + DASH.groups.length) % DASH.groups.length);
  if (e.key === 'f' || e.key === 'F') { document.getElementById('fsBtn').click(); }
});

window.addEventListener('resize', function() {
  for (var i = 0; i < DASH.groups.length; i++) {
    if (!state.rendered[i]) continue;
    var el = document.getElementById('chart' + i);
    if (el && window.Plotly) Plotly.Plots.resize(el);
    // flowworld 子要素もリサイズ
    if (DASH.groups[i] && DASH.groups[i].is_flowworld) {
      var mapEl = document.getElementById('fwMap' + i);
      var timeEl = document.getElementById('fwTime' + i);
      var wheelEl = document.getElementById('fwWheel' + i);
      if (mapEl) Plotly.Plots.resize(mapEl);
      if (timeEl) Plotly.Plots.resize(timeEl);
      if (wheelEl) Plotly.Plots.resize(wheelEl);
    }
  }
});

/* ---------- count-up & gauge ---------- */
function countUp(el) {
  var target = parseFloat(el.dataset.num || '0'), dec = parseInt(el.dataset.dec || '0', 10);
  var suffix = el.dataset.suffix || '', pre = el.dataset.pre || '';
  if (!isFinite(target)) { return; }
  var t0 = null, dur = 820;
  function step(ts) {
    if (!t0) t0 = ts;
    var p = Math.min((ts - t0) / dur, 1);
    var e = 1 - Math.pow(1 - p, 3);
    var v = target * e;
    el.textContent = pre + v.toLocaleString('ja-JP', {minimumFractionDigits: dec, maximumFractionDigits: dec}) + suffix;
    if (p < 1) requestAnimationFrame(step);
  }
  requestAnimationFrame(step);
}
document.querySelectorAll('#gaugeNum').forEach(countUp);
var arc = document.getElementById('gaugeArc');
if (arc) { var ratio = Math.max(0, Math.min(1, parseFloat(arc.dataset.ratio || '0'))); }
setTimeout(function() { if (arc) arc.style.strokeDashoffset = String(308 * (1 - ratio)); }, 90);

/* ---------- first paint ---------- */
showTab(0);
</script>
</body>
</html>
"""


# =============================================================================
# G. CHART_GROUPS（5タブ構成: v4改）
# =============================================================================
CHART_GROUPS: list[dict] = [
    {
        "key": "flowworld",
        "label": "マネーフロー",
        "caption": "資金フロー・ワールド",
        "y1": None,
        "y2": None,
        "hline": None,
        "series": [],
        "normalize": False,
        "is_flowworld": True,
    },
    {
        "key": "risk",
        "label": "リスク資産",
        "caption": "起点＝100で正規化",
        "y1": "指数（起点=100）",
        "y2": None,
        "hline": 100.0,
        "series": [
            ("ビットコイン (BTC)", "#f7931a"),
            ("米国株 (S&P 500)", "#38bdf8"),
            ("日本株 (日経平均)", "#fb7185"),
            ("欧州株 (EURO STOXX 50)", "#a78bfa"),
            ("ゴールド (Gold)", "#fbbf24"),
        ],
        "normalize": True,
    },
    {
        "key": "stress",
        "label": "ストレス",
        "caption": "DXY／VIX／債券ボラ",
        "y1": "DXY（左軸）",
        "y2": "VIX・債券ボラ bp（右軸）",
        "hline": None,
        "series": [
            ("米ドル指 (DXY)", "#e9eefa", "y", 2.2, None, False),
            ("VIX (株式恐怖指数)", "#fb7185", "y2", 1.7, "dot", False),
            ("米国債ボラティリティ (bp)", "#a78bfa", "y2", 1.5, "solid", False),
        ],
        "normalize": False,
    },
    {
        "key": "credit",
        "label": "暗号資産・信用",
        "caption": "ステーブルコイン／HYスプレッド",
        "y1": "ステーブルコイン（十億ドル）",
        "y2": "HYスプレッド %（右軸）",
        "hline": None,
        "series": [
            ("ステーブルコイン時価総額 ($B)", "#34d399", "y", 2.2, None, True),
            ("ハイイールド債スプレッド (%)", "#fb923c", "y2", 1.8, "solid", False),
        ],
        "normalize": False,
    },
    {
        "key": "liquidity",
        "label": "流動性",
        "caption": "M2／MMF",
        "y1": "米国M2十億ドル）",
        "y2": "MMF残高（十億ドル・右軸）",
        "hline": None,
        "series": [
            ("米国M2 ($B)", "#22d3ee", "y", 2.2, None, False),
            ("米国MMF残高 ($B)", "#818cf8", "y2", 2.2, "solid", False),
        ],
        "normalize": False,
    },
]

# 指標スナップショット（右カラム）に並べる系列と表示形式
SNAPSHOT_SERIES: list[tuple[str, str, int]] = [
    ("ビットコイン (BTC)", "", 0),
    ("米国株 (S&P 500)", "", 0),
    ("日本株 (日経平均)", "", 0),
    ("欧州株 (EURO STOXX 50)", "", 0),
    ("ゴールド (Gold)", "", 0),
    ("米ドル指数 (DXY)", "", 1),
    ("VIX (株式恐怖指数)", "", 2),
    ("ハイイールド債スプレッド (%)", " %", 2),
    ("米国債ボラティリティ (bp)", " bp", 1),
    ("ステブルコイン時価総額 ($B)", " B$", 1),
    ("米国M2 ($B)", " B$", 0),
    ("米国MMF残高 ($B)", " B$", 0),
]


# =============================================================================
# H. マネーフロー計算（v4追加）
# =============================================================================

# 資産クラス → 地域の分類マッピング
ASSET_REGION_MAP: dict[str, str] = {
    "米国株 (S&P 500)": "北米",
    "ビットコイン (BTC)": "グローバル",
    "日本株 (日経平均)": "日本",
    "欧州株 (EURO STOXX 50)": "欧州",
    "ゴールド (Gold)": "商品",
    "英国 (FTSE 100)": "欧州",
    "ドイツ (DAX)": "欧州",
    "中国 (上海総合)": "アジア",
    "香港 (ハンセン)": "アジア",
    "インド (Nifty 50)": "アジア",
    "ブラジル (BOVESPA)": "ラテン",
    "オーストラリア (ASX)": "大洋州",
    "カナダ (TSX)": "北",
    "米長期債 (TLT)": "債券",
    "米中期債 (IEF)": "債券",
    "ハイールド債 (HYG)": "債券",
    "投資適格債 (LQD)": "債券",
    "不動産 (VNQ)": "不動産",
    "新興国株式 (EEM)": "新興国",
    "先進国株式 ex-US (EFA)": "先進国",
    "原油 WTI (CL=F)": "商品",
}

ASSET_CLASS_MAP: dict[str, str] = {
    "米国株 (S&P 500)": "株式",
    "ビットコイン (BTC)": "グローバル",
    "日本株 (日経平均)": "株式",
    "欧州株 (EURO STOXX 50)": "株式",
    "ゴールド (Gold)": "商品",
    "英国 (FTSE 100)": "株式",
    "ドイツ (DAX)": "株式",
    "中国 (上海総合)": "株式",
    "香港 (ハンセン)": "株式",
    "インド (Nifty 50)": "株式",
    "ブラジル (BOVESPA)": "株式",
    "オーストラリア (ASX)": "株式",
    "カナダ (TSX)": "株式",
    "長期債 (TLT)": "債券",
    "米中期債 (IEF)": "債券",
    "ハイールド債 (HYG)": "債券",
    "投資適格債 (LQD)": "債券",
    "不動産 (VNQ)": "不動産",
    "新興国株式 (EEM)": "株式",
    "先進国株式 ex-US (EFA)": "株式",
    "原油 WTI (CL=F)": "商品",
}


def calc_flow_score(s: pd.Series, days: int = 30) -> float:
    """1ヶ月リターンを[-1, 1]に正規化した資金フロースコア。"""
    if s is None or s.empty:
        return 0.0
    r = period_return(s, days)
    if r is None:
        return 0.0
    # 月次リターン ±15% を ±1.0 にマップ（tanhで飽和）
    return float(math.tanh(r / 15.0))


def build_sankey_flow(data: dict[str, pd.Series]) -> dict:
    """Sankey図用: 源泉→資産クラス→地域のフローデータを構築。"""
    # ソースノード定義
    source_nodes = ["M2拡大", "MMF流出", "ドル安", "ステーブル流入", "VIX低下"]
    asset_classes = ["株式", "債券", "商品", "暗号資産", "不動産", "現金待機"]
    regions = ["北米", "欧州", "日本", "アジア", "ラテン", "大洋州", "新興国", "先進国", "グローバル"]

    all_nodes = source_nodes + asset_classes + regions
    node_idx = {n: i for i, n in enumerate(all_nodes)}

    links: list[dict] = []

    # 各資産のフロースコアを計算
    asset_scores: dict[str, float] = {}
    for label in list(ASSET_CLASS_MAP.keys()):
        s = data.get(label)
        if s is not None and not s.empty:
            asset_scores[label] = calc_flow_score(s, 30)

    if not asset_scores:
        return {"nodes": all_nodes, "links": [], "scores": {}}

    # 資産クラスごとにスコア集計
    class_scores: dict[str, float] = {c: 0.0 for c in asset_classes}
    for label, score in asset_scores.items():
        cls = ASSET_CLASS_MAP.get(label, "株式")
        class_scores[cls] += score

    # 地域ごとにスコア集計
    region_scores: dict[str, float] = {r: 0.0 for r in regions}
    for label, score in asset_scores.items():
        region = ASSET_REGION_MAP.get(label, "グローバル")
        region_scores[region] += score

    # ソース→資産クラスのリンク
    # M2拡大 → 株式・債券・商品（M2増加なら流入）
    m2 = data.get("米国M2 ($B)")
    m2_flow = calc_flow_score(m2, 90) if m2 is not None else 0.0
    if m2_flow > 0:
        links.append({"source": node_idx["M2拡大"], "target": node_idx["株式"], "value": abs(m2_flow) * 0.5})
        links.append({"source": node_idx["M2拡大"], "target": node_idx["債券"], "value": abs(m2_flow) * 0.3})
        links.append({"source": node_idx["M2拡大"], "target": node_idx["商品"], "value": abs(m2_flow) * 0.2})

    # MMF流出 → 株式・債券（MMF減少なら市場へ流入）
    mmf = data.get("米国MMF残高 ($B)")
    mmf_flow = -calc_flow_score(mmf, 90) if mmf is not None else 0.0  # 減少=流入なので符号反転
    if mmf_flow > 0:
        links.append({"source": node_idx["MMF流出"], "target": node_idx["株式"], "value": abs(mmf_flow) * 0.6})
        links.append({"source": node_idx["MMF流出"], "target": node_idx["債券"], "value": abs(mmf_flow) * 0.4})

    # ドル安 → 株式・商品・債券
    dxy = data.get("米ドル指数 (DXY)")
    dxy_flow = -calc_flow_score(dxy, 30) if dxy is not None else 0.0  # ドル安=他通貨高
    if dxy_flow > 0:
        links.append({"source": node_idx["ドル安"], "target": node_idx["株式"], "value": abs(dxy_flow) * 0.4})
        links.append({"source": node_idx["ドル安"], "target": node_idx["商品"], "value": abs(dxy_flow) * 0.35})
        links.append({"source": node_idx["ドル安"], "target": node_idx["債券"], "value": abs(dxy_flow) * 0.25})

    # ステーブル流入 → 暗号資産
    stable = data.get("ステーブルコイン時価総額 ($B)")
    stable_flow = calc_flow_score(stable, 30) if stable is not None else 0.0
    if stable_flow > 0:
        links.append({"source": node_idx["ステーブル流入"], "target": node_idx["暗号資産"], "value": abs(stable_flow)})

    # VIX低下 → 株式・不動産
    vix = data.get("VIX (株式恐怖指数)")
    vix_flow = -calc_flow_score(vix, 30) if vix is not None else 0.0  # VIX低下=リスクオン
    if vix_flow > 0:
        links.append({"source": node_idx["VIX低下"], "target": node_idx["株式"], "value": abs(vix_flow) * 0.7})
        links.append({"source": node_idx["VIX低下"], "target": node_idx["不動産"], "value": abs(vix_flow) * 0.3})

    # 資産クラス→地域のリンク
    class_to_region: dict[str, list[tuple[str, float]]] = {
        "株式": [("北米", 0.25), ("欧州", 0.20), ("日本", 0.10), ("アジア", 0.20), ("ラテン", 0.10), ("大洋州", 0.05), ("新興国", 0.10)],
        "債券": [("北米", 0.50), ("欧州", 0.25), ("新興国", 0.15), ("先進国", 0.10)],
        "商品": [("グローバル", 1.0)],
        "暗号資産": [("グローバル", 1.0)],
        "不動産": [("北米", 0.60), ("欧州", 0.25), ("アジア", 0.15)],
        "現金待機": [("グローバル", 1.0)],
    }

    for cls, score in class_scores.items():
        if score <= 0 or cls not in class_to_region:
            continue
        for region, weight in class_to_region[cls]:
            links.append({
                "source": node_idx[cls],
                "target": node_idx[region],
                "value": abs(score) * weight,
            })

    # 個別資産スコアも返す（ヒートマップ用）
    return {
        "nodes": all_nodes,
        "links": links,
        "scores": asset_scores,
        "class_scores": class_scores,
        "region_scores": region_scores,
    }


def build_flow_heatmap(data: dict[str, pd.Series]) -> list[dict]:
    """世界マネー・ヒートマップ用データ（地域×資産クラス）。"""
    rows: list[dict] = []
    for label, score in build_sankey_flow(data)["scores"].items():
        region = ASSET_REGION_MAP.get(label, "グローバル")
        cls = ASSET_CLASS_MAP.get(label, "株式")
        rows.append({
            "asset": label.split(" (")[0],
            "region": region,
            "class": cls,
            "score": score,
            "return_1m": period_return(data.get(label), 30) or 0.0,
        })
    return rows


def build_rotation_wheel(data: dict[str, pd.Series]) -> dict:
    """資産ローテーション・ホイール用データ。"""
    flow = build_sankey_flow(data)
    classes = ["株式", "債券", "商品", "暗号資産", "不動産"]
    values = [max(0, flow["class_scores"].get(c, 0)) for c in classes]
    total = sum(values) or 1.0
    return {
        "labels": classes,
        "values": [v / total * 100 for v in values],
        "colors": ["#38bdf8", "#818cf8", "#fbbf24", "#f472b6", "#34d399"],
    }


# =============================================================================
# I. チャートペイロード構築
# =============================================================================
def _series_to_points(s: pd.Series, limit: int = 260) -> tuple[list[str], list[float]]:
    """Plotly 用に (日付文字列リスト, 数値リスト) へ変換する。長い系列は間引く。"""
    if s is None or s.empty:
        return [], []
    step = max(1, len(s) // limit)
    sub = s.iloc[::step]
    if sub.index[-1] != s.index[-1]:
        sub = pd.concat([sub, s.iloc[[-1]]])
    dates = [d.strftime("%Y-%m-%d") for d in sub.index]
    values = [round(float(v), 4) for v in sub.values]
    return dates, values


def build_chart_payload(data: dict[str, pd.Series]) -> list[dict]:
    """タブごとのチャート定義を作る。"""
    groups: list[dict] = []
    for spec in CHART_GROUPS:
        traces: list[dict] = []
        for item in spec["series"]:
            if len(item) == 2:
                label, color = item
                axis, width, dash, fill = "y", 2.2, None, False
            else:
                label, color, axis, width, dash, fill = item
            s = data.get(label)
            if s is None or s.empty:
                continue
            ys = normalize_100(s) if spec["normalize"] else s
            dates, values = _series_to_points(ys)
            if not dates:
                continue
            traces.append({
                "name": label.split(" (")[0] if spec["normalize"] else label,
                "dates": dates,
                "values": values,
                "color": color,
                "width": width,
                "dash": dash or "solid",
                "axis": axis,
                "fill": fill,
                "fillcolor": "rgba(52,211,153,.10)" if fill else None,
                "markers": label.startswith("米国MMF"),
            })
        if spec.get("is_flowworld"):
            groups.append({
                "key": spec["key"],
                "label": spec["label"],
                "caption": spec["caption"],
                "is_flowworld": True,
            })
        elif traces:
            groups.append({
                "key": spec["key"],
                "label": spec["label"],
                "caption": spec["caption"],
                "y1": spec["y1"],
                "y2": spec["y2"],
                "hline": spec["hline"],
                "traces": traces,
            })
    return groups


# =============================================================================
# J. レンダリング関数
# =============================================================================
def _spark_svg(s: pd.Series, up_color: str = "#34d399", down_color: str = "#fb7185") -> str:
    """右カラム用のミニスパークライン（SVG）を作る。"""
    if s is None or len(s) < 3:
        return ""
    tail = s.tail(48)
    lo, hi = float(tail.min()), float(tail.max())
    if hi == lo:
        hi = lo + 1.0
    w, h = 66.0, 16.0
    n = len(tail)
    pts = []
    for i, v in enumerate(tail.values):
        x = (i / (n - 1)) * w
        y = h - ((float(v) - lo) / (hi - lo)) * (h - 2.0) - 1.0
        pts.append(f"{x:.1f},{y:.1f}")
    color = up_color if tail.iloc[-1] >= tail.iloc[0] else down_color
    return (f"<svg class="spark" viewBox="0 0 66 16" preserveAspectRatio="none">"
            f"<polyline points="{" ".join(pts)}" fill="none" stroke="{color}" "
            f"stroke-width="1.4" stroke-linejoin="round" stroke-linecap="round"/></svg>")


def _num(v: Optional[float], unit: str = "", digits: int = 0) -> str:
    """符号付き数値の整形（未取得はダッシュ）。"""
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return "—"
    return f"{v:+,.{digits}f}{unit}"


def _chip(v: Optional[float], digits: int = 2) -> str:
    if v is None:
        return "<span class="chip ghost">—</span>"
    cls = "up" if v > 0.0001 else ("down" if v < -0.0001 else "flat")
    return f"<span class="chip {cls}">{v:+.{digits}f}%</span>"


def render_snapshot(data: dict[str, pd.Series]) -> str:
    """右カラム: 指標スナップショット（最新値＋1ヶ月比＋スパークライン）。"""
    out: list[str] = []
    for label, unit, digits in SNAPSHOT_SERIES:
        s = data.get(label)
        if s is None or s.empty:
            continue
        d1m = period_return(s, 30)
        name = label.replace(" ($B)", "").replace(" (%)", "").replace(" (bp)", "")
        out.append(
            f"<div class="mrow">"
            f"<div class="nm">{html_lib.escape(name)}</div>"
            f"<div class="vl">{_fmt(s.iloc[-1], unit, digits)}</div>"
            f"<div class="rt">{_spark_svg(s)}{_chip(d1m)}</div>"
            f"</div>"
        )
    return "\n".join(out)


def _heat_bg(v: Optional[float]) -> str:
    """騰落率に応じたセル背景色（ヒートマップ風）。"""
    if v is None:
        return ""
    cap = 25.0
    a = min(abs(v) / cap, 1.0)
    alpha = 0.10 + a * 0.55
    rgb = "52,211,153" if v > 0 else "251,113,133"
    return f' style="background:rgba({rgb},{alpha:.2f})"'


def render_heat(rows: list[dict]) -> str:
    """中央下: 期間別パフォーマンスのヒート表（変化率を持つ系列のみ）。"""
    rows = [
        r for r in rows
        if r["values"].get("all") is not None
        or any(v is not None for v in r["values"].values())
    ]
    if not rows:
        return '<p style="margin:0;font-size:11px;color:#6f819a">表示できるデータがありません。</p>'
    head = ["<tr><th>指標</th><th>直近値</th>"]
    for _k, lab, _d in PERIOD_LABELS:
        head.append(f"<th>{lab}</th>")
    head.append("<th>全期間</th></tr>")

    body: list[str] = []
    for r in rows:
        cells = [f'<td>{html_lib.escape(r["label"].split(" (")[0])}</td>',
                 f'<td>{r["last"]}</td>']
        for k, _lab, _d in PERIOD_LABELS:
            v = r["values"].get(k)
            if v is None:
                cells.append(''<td class="na">—</td>'')
            else:
                cls = "pos" if v > 0 else "neg"
                cells.append(f'<td class="h {cls}"{_heat_bg(v)}>{v:+.1f}</td>')
        allv = r["values"].get("all")
        if allv is None:
            cells.append(''<td class="na">—</td>'')
        else:
            cls = "pos" if allv > 0 else "neg"
            cells.append(f'<td class="h {cls}"{_heat_bg(allv)}>{allv:+.1f}</td>')
        body.append("<tr>" + "".join(cells) + "</tr>")
    return (''<table class="heat"><thead>'' + "".join(head) + "</thead><tbody>"
            + "\n".join(body) + "</tbody></table>")


def render_reasons(reasons: list[tuple[str, bool]]) -> str:
    out = []
    for text, positive in reasons:
        cls = "on" if positive else "off"
        out.append(f"        <li class="{cls}">{html_lib.escape(text)}</li>")
    return "\n".join(out)


def render_ticker(data: dict[str, pd.Series]) -> str:
    """ヘッダー下を流れる相場テープ（2周ぶん並べて継ぎ目を消す）。"""
    items: list[str] = []
    for label, _unit, digits in SNAPSHOT_SERIES:
        s = data.get(label)
        if s is None or s.empty:
            continue
        d1 = _pct_change_n(s, 1)
        cls = "up" if (d1 or 0) > 0.0001 else ("down" if (d1 or 0) < -0.0001 else "flat")
        txt = "—" if d1 is None else f"{d1:+.2f}%"
        name = label.split(" (")[0]
        items.append(
            f"<span>{html_lib.escape(name)} <b>{_fmt(s.iloc[-1], "", digits)}</b>"
            f"<em class="{cls}">{txt}</em></span>"
        )
    return "".join(items) + "".join(items)


def render_status_grid() -> tuple[str, str]:
    """データ取得状況のコンパクト表示。"""
    if not FETCH_STATUS:
        return "—", ''<div class="sitem"><span class="dot ng"></span><span>取得状況を記録できませんでした</span></div>''
    items = []
    for item in FETCH_STATUS:
        cls = "ok" if item["ok"] else "ng"
        label = html_lib.escape(item["label"].split(" (")[0])
        items.append(f"<div class="sitem" title="{html_lib.escape(item["source"])}／"
                     f"{html_lib.escape(item["detail"])}">"
                     f"<span class="dot {cls}"></span><span>{label}</span></div>")
    ng = sum(1 for x in FETCH_STATUS if not x["ok"])
    summary = f"全{len(FETCH_STATUS)}系列／失敗{ng}件" if ng else f"全{len(FETCH_STATUS)}系列OK"
    return summary, "\n".join(items)


def _render_risk_metrics(board: dict) -> str:
    """左カラム下段: ボラティリティ・最大DD・200日線乖離のミニ表示。"""
    rows: list[str] = []
    for m in board.get("_risk_metrics", []):
        rows.append(
            f"<div class="mrow"><div class="nm">{html_lib.escape(m["label"].split(" (")[0])}</div>"
            f"<div class="vl">{_num(m["sma_dev"], "%", 1)}</div>"
            f"<div class="rt" style="gap:9px;color:#6f819a;font-size:10px">"
            f"<span>ボラ {_num(m["vol"], "%", 0)}</span>"
            f"<span>最大DD {_num(m["mdd"], "%", 1)}</span>"
            f"<span>200日線 {_num(m["sma_dev"], "%", 1)}</span></div></div>"
        )
    return "\n".join(rows) if rows else ''<div class="mrow"><div class="nm">—</div></div>''


# =============================================================================
# K. ライブボード構築
# =============================================================================
def build_live_board(data: dict[str, pd.Series], analysis: dict) -> dict:
    """タブ・ゲージ・スナップショット等の表示用データをまとめる。"""
    groups = build_chart_payload(data)
    status_summary, status_items = render_status_grid()
    score, total = analysis["score"], analysis["total"]
    ratio = (score / total) if total else 0.0

    # v4改: マネーフローデータを追加
    flow_data = build_sankey_flow(data)
    flow_heatmap = build_flow_heatmap(data)
    flow_rotation = build_rotation_wheel(data)

    return {
        "groups": groups,
        "flow": flow_data,
        "flow_heatmap": flow_heatmap,
        "flow_rotation": flow_rotation,
        "snapshot": render_snapshot(data),
        "heat": render_heat(analysis["table"]),
        "reasons": render_reasons(analysis["reasons"]),
        "ticker": render_ticker(data),
        "status": status_items,
        "status_summary": status_summary,
        "score": score,
        "total": total,
        "ratio": round(ratio, 4),
        "regime": html_lib.escape(analysis["regime_label"]),
        "stress": html_lib.escape(analysis["stress_text"]),
    }


# =============================================================================
# L. HTML出力
# =============================================================================
def write_dashboard(board: dict, output_path: str = DEFAULT_OUTPUT,
                    include_plotlyjs: str = "cdn") -> str:
    """単一HTML（スクロール不要のライブボード）を書き出す。"""
    out_dir = os.path.dirname(output_path) or "."
    os.makedirs(out_dir, exist_ok=True)

    updated_at = datetime.now(JST).strftime("%Y-%m-%d %H:%M JST")
    period = f"{START_DATE} 〜 {datetime.now(JST).strftime('%Y-%m-%d')}""

    tabs = []
    plots = []
    for i, g in enumerate(board["groups"]):
        active = " active" if i == 0 else ""
        tabs.append(
            f'<button class="tab{active}" data-i="{i}" type="button">'
            f'{html_lib.escape(g["label"])}</button>'
        )
        if g.get("is_flowworld"):
            display_val = "block" if i == 0 else "none"
            plots.append(
                f'<div class="plot flowWorldPlot" id="chart{i}" style="display:{display_val}">'
                f'<div class="fwMap" id="fwMap{i}"></div>'
                f'<div class="fwBottom">'
                f'<div class="fwTime" id="fwTime{i}"></div>'
                f'<div class="fwWheel" id="fwWheel{i}"></div>'
                f'</div></div>'
            )
            plots.append(f"<div class="plot" id="chart{i}" style="display:{"block" if i==0 else "none"}"></div>")

    period_btns = "".join(
        f'<button class="pbtn{" active" if p == "ALL" else ""}" data-p="{p}" type="button">{lab}</button>'
        for p, lab in (("1M", "1M"), ("3M", "3M"), ("6M", "6M"), ("1Y", "1Y"), ("ALL", "全"))
    )

    payload = json.dumps({
        "groups": board["groups"],
        "flow": board.get("flow", {}),
        "flow_heatmap": board.get("flow_heatmap", []),
        "flow_rotation": board.get("flow_rotation", {}),
    }, ensure_ascii=False, separators=(",", ":")).replace("</", "<\/")

    if include_plotlyjs == "cdn":
        plotly_tag = (''<script src="https://cdn.plot.ly/plotly-%s.min.js" charset="utf-8"></script>''
                      % po.get_plotlyjs_version())
    else:
        plotly_tag = "<script>%s</script>" % po.get_plotlyjs()

    html_doc = (
        HTML_TEMPLATE
        .replace("__UPDATED_AT__", updated_at)
        .replace("__PERIOD__", period)
        .replace("__TICKER__", board["ticker"])
        .replace("__SCORE__", str(board["score"]))
        .replace("__TOTAL__", str(board["total"]))
        .replace("__SCOREF__", str(board["score"]))
        .replace("__RATIO__", str(board["ratio"]))
        .replace("__REGIME__", board["regime"])
        .replace("__REASONS__", board["reasons"])
        .replace("__STRESS__", board["stress"])
        .replace("__RISKM__", _render_risk_metrics(board))
        .replace("__TABS__", "".join(tabs))
        .replace("__PERIODBTN__", period_btns)
        .replace("__CHARTS__", "".join(plots))
        .replace("__HEAT__", board["heat"])
        .replace("__METRICS__", board["snapshot"])
        .replace("__STATUS_SUMMARY__", board["status_summary"])
        .replace("__STATUS__", board["status"])
        .replace("__FOOTER_LEFT__", "データ出所: Yahoo Finance（yfinance）／ FRED（米セントルイス連銀）／ DefiLlama — すべてAPIキー不要の公開データ")
        .replace("__FOOTER_RIGHT__", f"本ページは情報提供のみを目的とした定点観測であり、投資助言・勧誘ではありません。　最終更新 {updated_at}")
        .replace("__PAYLOAD__", payload)
        .replace("__PLOTLYJS__", "")
        .replace("<script></script>", plotly_tag)
    )

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html_doc)
    return output_path


def build_dashboard(output_path: str = DEFAULT_OUTPUT, demo: bool = False,
                    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
                    include_plotlyjs: str = "cdn") -> str:
    FETCH_STATUS.clear()
    configure_window(lookback_days)
    data = collect_data(demo=demo)
    analysis = build_analysis(data)
    board = build_live_board(data, analysis)
    board["_risk_metrics"] = analysis["risk_metrics"]
    path = write_dashboard(board, output_path, include_plotlyjs)
    size_kb = os.path.getsize(path) / 1024
    ng = sum(1 for x in FETCH_STATUS if not x["ok"])
    print(f"正常に生成されました: {path} ({size_kb:,.0f} KB) / 取得失敗 {ng} 件")
    print(f"地合い判定: {analysis['regime_label']}（スコア {analysis['score']}/{analysis['total']}）")
    return path


# =============================================================================
# M. CLI
# =============================================================================
def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Global Money Flow ライブボード生成")
    p.add_argument("--output", default=DEFAULT_OUTPUT, help=f"出力HTMLパス（既定: {DEFAULT_OUTPUT}）")
    p.add_argument("--lookback-days", type=int, default=DEFAULT_LOOKBACK_DAYS, help="取得期間（日数, 既定: 730）")
    p.add_argument("--demo", action="store_true", help="ダミーデータで動作確認（ネットワーク未使用）")
    p.add_argument("--inline-plotlyjs", action="store_true", help="Plotly.js をHTMLに同梱（オフライン閲覧用）")
    return p.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
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
