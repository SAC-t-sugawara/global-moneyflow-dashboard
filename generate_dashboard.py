#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Global Money Flow & Market Dashboard (v2)
=========================================
APIキー不要の無料データソースから世界のマネーフロー指標を取得し、
「数値カード＋自動分析＋期間別パフォーマンス＋4段チャート」を備えた
インタラクティブな単一HTML（public/index.html）を生成する。

  * Yahoo Finance (yfinance) : リスク資産 / DXY / VIX
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
import html as html_lib
import io
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

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
    d["米国MMF残高 ($B)"] = walk(quarterly, 6800.0, 0.01, 0.02)
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


def _level_comment(vix: Optional[float], hy: Optional[float]) -> tuple[str, str]:
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


def score_regime(data: dict[str, pd.Series]) -> tuple[str, int, int, list[str]]:
    """リスクオン/オフを機械的にスコアリングする（加点式・最大7点）。"""
    reasons: list[str] = []
    score = 0
    total = 0

    spx = data.get("米国株 (S&P 500)")
    if spx is not None and not spx.empty:
        total += 1
        dev = deviation_from_sma(spx, 200)
        if dev is not None and dev > 0:
            score += 1
            reasons.append(f"S&P 500 は200日移動平均を {dev:+.1f}% 上回る（上昇基調）")
        elif dev is not None:
            reasons.append(f"S&P 500 は200日移動平均を {dev:+.1f}% 下回る（調整局面）")

    vix = data.get("VIX (株式恐怖指数)")
    if vix is not None and not vix.empty:
        total += 1
        if vix.iloc[-1] < 20:
            score += 1
            reasons.append(f"VIX は {vix.iloc[-1]:.1f} と低水準（投資家の警戒感は小さい）")
        else:
            reasons.append(f"VIX は {vix.iloc[-1]:.1f} と高め（警戒感が残る）")

    hy = data.get("ハイイールド債スプレッド (%)")
    if hy is not None and not hy.empty:
        total += 1
        if hy.iloc[-1] < 4.0:
            score += 1
            reasons.append(f"ハイイールド債スプレッドは {hy.iloc[-1]:.2f}% で信用環境は良好")
        else:
            reasons.append(f"ハイイールド債スプレッドは {hy.iloc[-1]:.2f}% で信用リスクが意識される")

    dxy = data.get("米ドル指数 (DXY)")
    if dxy is not None and not dxy.empty:
        total += 1
        m1 = period_return(dxy, 30)
        if m1 is not None and m1 < 0:
            score += 1
            reasons.append(f"ドル指数は1ヶ月で {m1:+.2f}% と下落（ドル安は新興国・リスク資産に追い風）")
        elif m1 is not None:
            reasons.append(f"ドル指数は1ヶ月で {m1:+.2f}% と上昇（ドル高はリスク資産の重荷）")

    stable = data.get("ステーブルコイン時価総額 ($B)")
    if stable is not None and not stable.empty:
        total += 1
        m1 = period_return(stable, 30)
        if m1 is not None and m1 > 0:
            score += 1
            reasons.append(f"ステーブルコイン残高は1ヶ月で {m1:+.2f}% 増加（暗号資産市場へ資金流入）")
        elif m1 is not None:
            reasons.append(f"ステーブルコイン残高は1ヶ月で {m1:+.2f}% 減少（暗号資産市場から資金流出）")

    m2 = data.get("米国M2 ($B)")
    if m2 is not None and not m2.empty:
        total += 1
        d90 = period_change_abs(m2, 91)
        if d90 is not None and d90 > 0:
            score += 1
            reasons.append(f"米国M2は3ヶ月で {d90:+,.0f} B$ 増加（マクロの流動性は拡大方向）")
        elif d90 is not None:
            reasons.append(f"米国M2は3ヶ月で {d90:+,.0f} B$ 減少（流動性は縮小方向）")

    mmf = data.get("米国MMF残高 ($B)")
    if mmf is not None and not mmf.empty:
        total += 1
        d90 = period_change_abs(mmf, 91)
        if d90 is not None and d90 < 0:
            score += 1
            reasons.append(f"米国MMF残高は3ヶ月で {d90:+,.0f} B$ 減少（待機資金が市場へ向かう兆候）")
        elif d90 is not None:
            reasons.append(f"米国MMF残高は3ヶ月で {d90:+,.0f} B$ 増加（待機資金が積み上がる局面）")

    if total == 0:
        return "判定不能", 0, 0, ["データが不足しているため判定できません。"]
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
# F. グラフ生成
# =============================================================================
def build_figure(data: dict[str, pd.Series]) -> tuple[go.Figure, list[dict]]:
    """4段構成のインタラクティブグラフと、サマリーカードを生成する。"""
    fig = make_subplots(
        rows=4, cols=1, shared_xaxes=True, vertical_spacing=0.085,
        subplot_titles=(
            "① リスク資産の相対推移（起点＝100）",
            "② 現金逃避・市場ストレス（DXY／VIX／米国債ボラティリティ）",
            "③ 暗号資産の資金量（ステーブルコイン時価総額）と信用スプレッド",
            "④ マクロ流動性・待機現金（米国M2／米国MMF残高）",
        ),
        specs=[[{"secondary_y": False}],
               [{"secondary_y": True}],
               [{"secondary_y": True}],
               [{"secondary_y": True}]],
    )

    cards: list[dict] = []

    def add_card(label: str, raw: pd.Series, unit: str = "", digits: Optional[int] = None) -> None:
        if raw is None or raw.empty:
            return
        cards.append({
            "label": label,
            "value": _fmt(raw.iloc[-1], unit, digits),
            "d1": _pct_change_n(raw, 1),
            "d1m": period_return(raw, 30),
            "date": _last_date(raw),
        })

    # --- ① リスク資産 -------------------------------------------------------
    for (key, label), color in zip(RISK_ASSETS, RISK_COLORS):
        s = data.get(label)
        if s is None or s.empty:
            continue
        norm = normalize_100(s)
        if norm.empty:
            continue
        fig.add_trace(
            go.Scatter(x=norm.index, y=norm.values, name=label, mode="lines",
                       line=dict(width=2.2, color=color),
                       hovertemplate="%{y:.1f}<extra>" + label + "</extra>"),
            row=1, col=1,
        )
        add_card(label, s)
    fig.add_hline(y=100, line=dict(color="#9ca3af", width=1, dash="dash"),
                  row=1, col=1)

    # --- ② ストレス指標 -----------------------------------------------------
    dxy = data.get("米ドル指数 (DXY)")
    vix = data.get("VIX (株式恐怖指数)")
    bondvol = data.get("米国債ボラティリティ (bp)")

    if dxy is not None and not dxy.empty:
        fig.add_trace(go.Scatter(x=dxy.index, y=dxy.values, name="米ドル指数 (DXY)",
                                 line=dict(color="#111827", width=2.2)),
                      row=2, col=1, secondary_y=False)
        add_card("米ドル指数 (DXY)", dxy, "", 1)
    if vix is not None and not vix.empty:
        fig.add_trace(go.Scatter(x=vix.index, y=vix.values, name="VIX（株式恐怖指数）",
                                 line=dict(color="#ef4444", width=1.6, dash="dot")),
                      row=2, col=1, secondary_y=True)
        add_card("VIX（株式恐怖指数）", vix, "", 2)
    if bondvol is not None and not bondvol.empty:
        fig.add_trace(go.Scatter(x=bondvol.index, y=bondvol.values,
                                 name="米国債ボラティリティ（20日・bp）",
                                 line=dict(color="#8b5cf6", width=1.6)),
                      row=2, col=1, secondary_y=True)

    fig.update_yaxes(title_text="DXY", row=2, col=1, secondary_y=False,
                     title_font=dict(size=11), automargin=True)
    fig.update_yaxes(title_text="VIX ／ 債券ボラ（bp）", row=2, col=1, secondary_y=True,
                     title_font=dict(size=11), automargin=True)

    # --- ③ ステーブルコインと信用スプレッド ---------------------------------
    stable = data.get("ステーブルコイン時価総額 ($B)")
    hy = data.get("ハイイールド債スプレッド (%)")

    if stable is not None and not stable.empty:
        fig.add_trace(go.Scatter(x=stable.index, y=stable.values,
                                 name="ステーブルコイン時価総額（$B）",
                                 fill="tozeroy", fillcolor="rgba(16,185,129,0.14)",
                                 line=dict(color="#10b981", width=2.2)),
                      row=3, col=1, secondary_y=False)
        add_card("ステーブルコイン時価総額", stable, " B$", 1)
    if hy is not None and not hy.empty:
        fig.add_trace(go.Scatter(x=hy.index, y=hy.values,
                                 name="ハイイールド債スプレッド（%）",
                                 line=dict(color="#f97316", width=1.9)),
                      row=3, col=1, secondary_y=True)
        add_card("ハイイールド債スプレッド", hy, " %", 2)

    fig.update_yaxes(title_text="ステーブルコイン（$B）", row=3, col=1, secondary_y=False,
                     title_font=dict(size=11), automargin=True)
    fig.update_yaxes(title_text="HYスプレッド（%）", row=3, col=1, secondary_y=True,
                     title_font=dict(size=11), automargin=True)

    # --- ④ M2 と MMF --------------------------------------------------------
    m2 = data.get("米国M2 ($B)")
    mmf = data.get("米国MMF残高 ($B)")

    if m2 is not None and not m2.empty:
        fig.add_trace(go.Scatter(x=m2.index, y=m2.values, name="米国M2（$B）",
                                 line=dict(color="#0284c7", width=2.2)),
                      row=4, col=1, secondary_y=False)
        add_card("米国M2マネーサプライ", m2, " B$", 0)
    if mmf is not None and not mmf.empty:
        fig.add_trace(go.Scatter(x=mmf.index, y=mmf.values, name="米国MMF残高（$B）",
                                 mode="lines+markers", marker=dict(size=6),
                                 line=dict(color="#6366f1", width=2.2)),
                      row=4, col=1, secondary_y=True)
        add_card("米国MMF残高", mmf, " B$", 0)

    fig.update_yaxes(title_text="米国M2（$B）", row=4, col=1, secondary_y=False,
                     title_font=dict(size=11), automargin=True)
    fig.update_yaxes(title_text="MMF（$B）", row=4, col=1, secondary_y=True,
                     title_font=dict(size=11), automargin=True)

    if not fig.data:
        fig.add_annotation(
            text="データを取得できませんでした（ネットワークまたは各APIの状態をご確認ください）",
            xref="paper", yref="paper", x=0.5, y=0.5, showarrow=False,
            font=dict(size=16, color="#dc2626"),)

    fig.update_layout(
        height=1580,
        template="plotly_white",
        hovermode="x unified",
        hoverlabel=dict(font_size=12, font_family=FONT_STACK),
        legend=dict(orientation="h", yanchor="bottom", y=1.045, xanchor="left", x=0,
                    font=dict(size=11.5), bgcolor="rgba(255,255,255,0.9)"),
        margin=dict(l=28, r=28, t=118, b=46),
        font=dict(family=FONT_STACK, size=12.5, color="#111827"),
        paper_bgcolor="#ffffff",
        plot_bgcolor="#ffffff",
        dragmode="pan",
    )

    # 期間切り替えボタン（共有X軸の最下段）
    fig.update_xaxes(
        rangeselector=dict(
            buttons=[
                dict(count=1, label="1ヶ月", step="month", stepmode="backward"),
                dict(count=3, label="3ヶ月", step="month", stepmode="backward"),
                dict(count=6, label="6ヶ月", step="month", stepmode="backward"),
                dict(count=1, label="1年", step="year", stepmode="backward"),
                dict(step="all", label="全期間"),
            ],
            bgcolor="#f1f3f5", activecolor="#d1d5db",
            font=dict(color="#111827", size=11.5), y=1.28,
        ),
        row=4, col=1,
    )
    fig.update_xaxes(showgrid=True, gridcolor="rgba(17,24,39,0.07)",
                     showline=True, linecolor="rgba(17,24,39,0.15)", ticks="outside",
                     tickfont=dict(size=11.5))
    fig.update_yaxes(showgrid=True, gridcolor="rgba(17,24,39,0.07)",
                     tickfont=dict(size=11.5), zeroline=False)

    # サブプロットタイトルを左寄せ・大きめにして重なりを避ける
    for ann in fig.layout.annotations:
        ann.update(x=0.0, xanchor="left", font=dict(size=14, color="#111827"))
    return fig, cards


# =============================================================================
# G. HTML出力
# =============================================================================
HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Global Money Flow &amp; Market Dashboard</title>
<style>
  :root {
    --bg: #f4f6f8;
    --panel: #ffffff;
    --ink: #0f172a;
    --ink-2: #334155;
    --muted: #64748b;
    --line: #e2e8f0;
    --up: #047857;
    --up-bg: #ecfdf5;
    --down: #b91c1c;
    --down-bg: #fef2f2;
    --flat: #475569;
    --accent: #0f766e;
    --radius: 12px;
  }
  * { box-sizing: border-box; }
  html { -webkit-text-size-adjust: 100%; }
  body {
    margin: 0;
    padding: 26px 22px 44px;
    background: var(--bg);
    color: var(--ink);
    font-family: 'Hiragino Kaku Gothic ProN', 'Hiragino Sans', 'Yu Gothic UI',
                 'Yu Gothic', 'Meiryo', 'Noto Sans JP', 'Segoe UI', sans-serif;
    font-size: 14px;
    line-height: 1.7;
    font-feature-settings: "palt" 0;
    -webkit-font-smoothing: antialiased;
  }
  .wrap { max-width: 1400px; margin: 0 auto; }

  /* ---------- ヘッダー ---------- */
  header.top {
    display: flex; flex-wrap: wrap; align-items: flex-end;
    justify-content: space-between; gap: 10px 20px;
    padding-bottom: 14px; margin-bottom: 18px;
    border-bottom: 1px solid var(--line);
  }
  h1 {
    font-size: 23px; line-height: 1.35; margin: 0; letter-spacing: .01em;
    font-weight: 700;
  }
  h1 .sub { display: block; font-size: 12.5px; font-weight: 500; color: var(--muted); margin-top: 4px; }
  .stamp { font-size: 12.5px; color: var(--muted); line-height: 1.9; }
  .stamp b { color: var(--ink); font-weight: 700; font-variant-numeric: tabular-nums; }

  /* ---------- セクション見出し ---------- */
  h2.sec {
    display: flex; align-items: center; gap: 9px;
    font-size: 15px; margin: 0 0 11px; font-weight: 700; letter-spacing: .01em;
  }
  h2.sec::before {
    content: ""; width: 4px; height: 16px; border-radius: 2px;
    background: var(--accent); flex: 0 0 auto;
  }
  h2.sec .hint { font-size: 11.5px; font-weight: 500; color: var(--muted); }
  section.block { margin: 0 0 20px; }

  /* ---------- カード ---------- */
  .cards {
    display: grid; gap: 11px;
    grid-template-columns: repeat(auto-fill, minmax(196px, 1fr));
  }
  .card {
    background: var(--panel); border: 1px solid var(--line); border-radius: var(--radius);
    padding: 12px 14px 11px;
    box-shadow: 0 1px 2px rgba(15,23,42,.04), 0 6px 16px -14px rgba(15,23,42,.35);
  }
  .card .label {
    font-size: 11.5px; color: var(--muted); line-height: 1.45;
    min-height: 2.9em; font-weight: 600;
  }
  .card .value {
    font-size: 20px; font-weight: 700; margin: 1px 0 7px;
    font-variant-numeric: tabular-nums; letter-spacing: -.01em;
  }
  .card .deltas { display: flex; flex-direction: column; gap: 3px; font-size: 11.5px; color: var(--muted); }
  .card .deltas span.row { display: flex; justify-content: space-between; gap: 6px; }
  .card .deltas .k { flex: 0 0 auto; }
  .card .when { margin-top: 6px; font-size: 10.5px; color: #94a3b8; font-variant-numeric: tabular-nums; }

  .up { color: var(--up); font-weight: 700; font-variant-numeric: tabular-nums; }
  .down { color: var(--down); font-weight: 700; font-variant-numeric: tabular-nums; }
  .flat { color: var(--flat); font-weight: 600; font-variant-numeric: tabular-nums; }
  .pill { font-weight: 700; }

  /* ---------- 分析パネル ---------- */
  .analysis { display: grid; gap: 13px; grid-template-columns: 1fr 1fr; }
  @media (max-width: 980px) { .analysis { grid-template-columns: 1fr; } }
  .panel {
    background: var(--panel); border: 1px solid var(--line); border-radius: var(--radius);
    padding: 15px 16px; box-shadow: 0 1px 2px rgba(15,23,42,.04);
  }
  .panel h3 {
    font-size: 13px; margin: 0 0 10px; color: var(--ink);
    font-weight: 700; letter-spacing: .01em;
  }
  .regime {
    display: inline-flex; align-items: baseline; gap: 8px;
    padding: 7px 12px; border-radius: 8px; margin-bottom: 11px;
    background: #f1f5f9; border: 1px solid var(--line);
  }
  .regime .txt { font-size: 14px; font-weight: 700; }
  .regime .scr { font-size: 11.5px; color: var(--muted); font-variant-numeric: tabular-nums; }
  .meter { height: 6px; background: #e8edf2; border-radius: 3px; overflow: hidden; margin: 0 0 12px; }
  .meter > i { display: block; height: 100%; background: linear-gradient(90deg,#0ea5a4,#0f766e); }
  ul.reasons { list-style: none; margin: 0; padding: 0; display: grid; gap: 7px; }
  ul.reasons li {
    font-size: 12.8px; line-height: 1.65; color: var(--ink-2);
    padding-left: 17px; position: relative;
  }
  ul.reasons li::before {
    content: ""; position: absolute; left: 4px; top: .62em;
    width: 6px; height: 6px; border-radius: 50%; background: #94a3b8;
  }
  .note { font-size: 12.5px; color: var(--ink-2); line-height: 1.75; margin: 0 0 8px; }
  .kv { display: grid; grid-template-columns: auto 1fr; gap: 5px 12px; font-size: 12.5px; }
  .kv dt { color: var(--muted); font-weight: 600; }
  .kv dd { margin: 0; font-variant-numeric: tabular-nums; }

  /* ---------- 表 ---------- */
  .tablewrap { overflow-x: auto; -webkit-overflow-scrolling: touch; }
  table.perf {
    width: 100%; border-collapse: collapse; font-size: 12.8px;
    font-variant-numeric: tabular-nums; min-width: 720px;
  }
  table.perf th, table.perf td {
    padding: 8px 10px; text-align: right; border-bottom: 1px solid var(--line);
    white-space: nowrap;
  }
  table.perf th {
    font-size: 11.5px; color: var(--muted); font-weight: 700;
    background: #f8fafc; position: sticky; top: 0; text-align: right;
  }
  table.perf th:first-child, table.perf td:first-child {
    text-align: left; position: sticky; left: 0; background: var(--panel); z-index: 1;
  }
  table.perf th:first-child { background: #f8fafc; }
  table.perf tbody tr:hover td { background: #f9fbfc; }
  table.perf td.last { font-weight: 700; }
  table.perf .sep td { border-top: 2px solid var(--line); }

  /* ---------- チャート ---------- */
  .chart {
    background: var(--panel); border: 1px solid var(--line); border-radius: var(--radius);
    padding: 6px 6px 2px; overflow: hidden;
  }
  .js-plotly-plot .plotly .modebar { opacity: .55; }

  /* ---------- 取得状況 ---------- */
  details.status {
    background: var(--panel); border: 1px solid var(--line); border-radius: var(--radius);
    padding: 11px 15px;
  }
  details.status > summary {
    cursor: pointer; font-size: 12.5px; color: var(--ink-2); font-weight: 600;
    list-style: none; display: flex; align-items: center; gap: 8px;
  }
  details.status > summary::-webkit-details-marker { display: none; }
  details.status > summary::before { content: "▶"; font-size: 9px; color: var(--muted); }
  details.status[open] > summary::before { content: "▼"; }
  .statuslist {
    list-style: none; margin: 11px 0 0; padding: 0;
    display: grid; gap: 5px; grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));
  }
  .statuslist li { font-size: 11.8px; display: flex; gap: 7px; align-items: baseline; flex-wrap: wrap; }
  .tag { font-weight: 700; font-size: 10.5px; padding: 1px 6px; border-radius: 4px; flex: 0 0 auto; }
  .tag.ok { background: var(--up-bg); color: var(--up); }
  .tag.ng { background: var(--down-bg); color: var(--down); }
  .statuslist .src { color: var(--muted); }

  footer {
    margin-top: 18px; color: var(--muted); font-size: 11.5px; line-height: 1.85;
    border-top: 1px solid var(--line); padding-top: 13px;
  }
  footer code { background: #eef2f6; padding: 1px 5px; border-radius: 4px; font-size: 11px; }

  @media (max-width: 700px) {
    body { padding: 16px 12px 30px; font-size: 13.5px; }
    h1 { font-size: 18px; }
    .cards { grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); gap: 9px; }
    .card .value { font-size: 17px; }
    .card .label { min-height: 0; }
  }
  @media print {
    body { background: #fff; padding: 0; }
    .chart, .panel, .card, details.status { box-shadow: none; }
    details.status { break-inside: avoid; }
    details.status > summary { list-style: none; }
    @page { size: A4 landscape; margin: 12mm; }
  }
</style>
</head>
<body>
<div class="wrap">
  <header class="top">
    <h1>Global Money Flow &amp; Market Dashboard
      <span class="sub">世界の資金フロー定点観測 — リスク資産・ストレス指標・流動性を4層で可視化</span>
    </h1>
    <div class="stamp">
      最終更新: <b>__UPDATED_AT__</b><br>
      観測期間: <b>__PERIOD__</b>（リスク資産は起点＝100で正規化）
    </div>
  </header>

  <section class="block">
    <h2 class="sec">最新値 <span class="hint">前回比は直前データ、1ヶ月比は30日前との比較</span></h2>
    <div class="cards">__CARDS__</div>
  </section>

  <section class="block">
    <h2 class="sec">自動分析 <span class="hint">数値から機械的に判定（投資助言ではありません）</span></h2>
    <div class="analysis">__ANALYSIS__</div>
  </section>

  <section class="block">
    <h2 class="sec">期間別パフォーマンス <span class="hint">直近データ日から各期間前との騰落率</span></h2>
    <div class="panel">
      <div class="tablewrap">__TABLE__</div>
    </div>
  </section>

  <section class="block">
    <h2 class="sec">チャート <span class="hint">ドラッグで移動／ホイールで拡大／右上のボタンで期間切替</span></h2>
    <div class="chart">__PLOT__</div>
  </section>

  <section class="block">
    <details class="status"__OPEN__>
      <summary>データ取得状況（今回の実行結果）__STATUS_SUMMARY__</summary>
      <ul class="statuslist">__STATUS__</ul>
    </details>
  </section>

  <footer>
    <b>データ出所</b>: Yahoo Finance（yfinance）／ FRED（米セントルイス連銀）／ DefiLlama。すべてAPIキー不要の公開データを利用しています。<br>
    <b>指標の定義</b>: リスク資産は観測期間の初日を100として正規化。米国債ボラティリティは、Yahoo Finance の <code>^MOVE</code> が ICE BofA MOVE 指数ではないため、FRED の米10年債利回り（DGS10）の日次変化から20営業日の標準偏差（bp）を算出した代替指標です。M2は週次・10億ドル、MMFは四半期・百万ドルを1,000で割って10億ドル表示に換算しています。<br>
    <b>ご注意</b>: 本ページは情報提供のみを目的とした定点観測用ダッシュボードであり、投資助言・勧誘ではありません。数値はデータ提供元の定義改定や遅延により変動・欠損することがあります。
  </footer>
</div>
</body>
</html>
"""

CARD_TEMPLATE = """      <div class="card">
        <div class="label">__LABEL__</div>
        <div class="value">__VALUE__</div>
        <div class="deltas">
          <span class="row"><span class="k">前回比</span>__D1__</span>
          <span class="row"><span class="k">1ヶ月比</span>__D1M__</span>
        </div>
        <div class="when">基準日 __DATE__</div>
      </div>"""

STATUS_TEMPLATE = """        <li><span class="tag __CLASS__">__TAG__</span><span>__LABEL__</span><span class="src">（__SOURCE__）__DETAIL__</span></li>"""


def _delta_cell(v: Optional[float]) -> str:
    if v is None:
        return '<span class="flat">—</span>'
    cls = "up" if v > 0.0001 else ("down" if v < -0.0001 else "flat")
    return f'<span class="{cls}">{v:+,.2f}%</span>'


def _num(v: Optional[float], unit: str = "", digits: int = 0) -> str:
    if v is None:
        return "—"
    return f"{v:+,.{digits}f}{unit}"


def render_cards(cards: list[dict]) -> str:
    if not cards:
        return '      <div class="card"><div class="label">データなし</div><div class="value">—</div></div>'
    out = []
    for c in cards:
        out.append(
            CARD_TEMPLATE.replace("__LABEL__", c["label"])
            .replace("__VALUE__", c["value"])
            .replace("__D1__", _delta_cell(c["d1"]))
            .replace("__D1M__", _delta_cell(c["d1m"]))
            .replace("__DATE__", c["date"])
        )
    return "\n".join(out)


def render_analysis(analysis: dict) -> str:
    # --- パネル1: 地合い判定 ---
    score, total = analysis["score"], analysis["total"]
    pct = (score / total * 100) if total else 0
    reasons_html = "\n".join(f"        <li>{r}</li>" for r in analysis["reasons"])
    panel1 = f"""      <div class="panel">
        <h3>地合い判定（リスクオン／オフのスコアリング）</h3>
        <div class="regime"><span class="txt">{analysis['regime_label']}</span><span class="scr">{score} / {total} 項目がリスクオン</span></div>
        <div class="meter"><i style="width:{pct:.0f}%"></i></div>
        <ul class="reasons">
{reasons_html}
        </ul>
      </div>"""

    # --- パネル2: ストレス・リスク・流動性 ---
    stress = analysis["stress_text"]
    best, worst = analysis["best"], analysis["worst"]
    lead = ""
    if best and worst:
        lead = (f"<p class=\"note\">観測期間の騰落率は <b>{best[0]}</b> が {best[1]:+.2f}% で最も強く、"
                f"<b>{worst[0]}</b> が {worst[1]:+.2f}% で最も弱い結果です。</p>")

    risk_rows = []
    for m in analysis["risk_metrics"]:
        risk_rows.append(
            f"<dt>{m['label']}</dt><dd>{_num(m['vol'], '%', 1)}<span style=\"color:#94a3b8\">（年率ボラ）</span> ／ "
            f"{_num(m['mdd'], '%', 1)}<span style=\"color:#94a3b8\">（最大DD）</span> ／ "
            f"{_num(m['sma_dev'], '%', 1)}<span style=\"color:#94a3b8\">（200日線乖離）</span></dd>"
        )
    liq_rows = []
    for l in analysis["liquidity"]:
        liq_rows.append(
            f"<dt>{l['label']}</dt><dd>{l['level']} ／ 1ヶ月 {_num(l['d1m'], l['unit'], l['digits'])}"
            f" ／ 3ヶ月 {_num(l['d3m'], l['unit'], l['digits'])}</dd>"
        )

    panel2 = f"""      <div class="panel">
        <h3>市場ストレスと流動性の変化</h3>
        <p class="note"><b>ストレス水準</b>: {stress}。</p>
        {lead}
        <dl class="kv">
{chr(10).join('          ' + r for r in risk_rows)}
        </dl>
        <h3 style="margin-top:14px">流動性・待機資金の変化</h3>
        <dl class="kv">
{chr(10).join('          ' + r for r in liq_rows)}
        </dl>
      </div>"""
    return panel1 + "\n" + panel2


def render_table(rows: list[dict]) -> str:
    if not rows:
        return '<p class="note">表示できるデータがありません。</p>'
    head = ("<tr><th>指標</th><th>直近値</th><th>基準日</th>"
            + "".join(f"<th>{lab}</th>" for _k, lab, _d in PERIOD_LABELS)
            + "<th>全期間</th></tr>")
    body = []
    prev_group_analysis = False
    for r in rows:
        cells = [f'<td>{r["label"]}</td>', f'<td class="last">{r["last"]}</td>', f'<td>{r["date"]}</td>']
        for k, _lab, _d in PERIOD_LABELS:
            cells.append(f"<td>{_delta_cell(r['values'].get(k))}</td>")
        cells.append(f"<td>{_delta_cell(r['values'].get('all'))}</td>")
        body.append("<tr>" + "".join(cells) + "</tr>")
    return f"<table class=\"perf\">\n<thead>{head}</thead>\n<tbody>{chr(10).join(body)}</tbody>\n</table>"


def render_status() -> tuple[str, str, str]:
    if not FETCH_STATUS:
        return ("", "",
                '        <li><span class="tag ng">情報なし</span><span>取得状況を記録できませんでした</span></li>')
    rows = []
    for item in FETCH_STATUS:
        ok = item["ok"]
        rows.append(
            STATUS_TEMPLATE.replace("__CLASS__", "ok" if ok else "ng")
            .replace("__TAG__", "取得OK" if ok else "失敗")
            .replace("__LABEL__", item["label"])
            .replace("__SOURCE__", item["source"])
            .replace("__DETAIL__", f" — {item['detail']}" if item["detail"] else "")
        )
    ng = sum(1 for x in FETCH_STATUS if not x["ok"])
    summary = (f" — 全 {len(FETCH_STATUS)} 系列／失敗 {ng} 件" if ng else
               f" — 全 {len(FETCH_STATUS)} 系列すべて取得OK")
    return (" open" if ng else "", summary, "\n".join(rows))


def write_dashboard(fig: go.Figure, cards: list[dict], analysis: dict,
                    output_path: str = DEFAULT_OUTPUT,
                    include_plotlyjs: str = "cdn") -> str:
    """単一HTMLを書き出す。"""
    out_dir = os.path.dirname(output_path) or "."
    os.makedirs(out_dir, exist_ok=True)

    plot_html = fig.to_html(
        full_html=False, include_plotlyjs=include_plotlyjs,
        config={"displayModeBar": True, "responsive": True, "displaylogo": False,
                "scrollZoom": True, "modeBarButtonsToRemove": ["lasso2d", "select2d"]},
    )

    updated_at = datetime.now(JST).strftime("%Y-%m-%d %H:%M JST")
    period = f"{START_DATE} 〜 {datetime.now(JST).strftime('%Y-%m-%d')}"
    open_attr, status_summary, status_rows = render_status()

    html_doc = (
        HTML_TEMPLATE
        .replace("__UPDATED_AT__", updated_at)
        .replace("__PERIOD__", period)
        .replace("__CARDS__", render_cards(cards))
        .replace("__ANALYSIS__", render_analysis(analysis))
        .replace("__TABLE__", render_table(analysis["table"]))
        .replace("__STATUS_SUMMARY__", status_summary)
        .replace("__STATUS__", status_rows)
        .replace("__OPEN__", open_attr)
        .replace("__PLOT__", plot_html)
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
    fig, cards = build_figure(data)
    analysis = build_analysis(data)
    path = write_dashboard(fig, cards, analysis, output_path, include_plotlyjs)
    size_kb = os.path.getsize(path) / 1024
    ng = sum(1 for x in FETCH_STATUS if not x["ok"])
    print(f"正常に生成されました: {path} ({size_kb:,.0f} KB) / 取得失敗 {ng} 件")
    print(f"地合い判定: {analysis['regime_label']}（スコア {analysis['score']}/{analysis['total']}）")
    return path


# =============================================================================
# H. CLI
# =============================================================================
def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Global Money Flow & Market Dashboard 生成")
    p.add_argument("--output", default=DEFAULT_OUTPUT, help=f"出力HTMLパス（既定: {DEFAULT_OUTPUT}）")
    p.add_argument("--lookback-days", type=int, default=DEFAULT_LOOKBACK_DAYS, help="取得期間（日数, 既定: 730）")
    p.add_argument("--demo", action="store_true", help="ダミーデータで動作確認（ネットワーク未使用）")
    p.add_argument("--inline-plotlyjs", action="store_true", help="Plotly.js をHTMLに同梱（オフライン閲覧用・約4MB増）")
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
