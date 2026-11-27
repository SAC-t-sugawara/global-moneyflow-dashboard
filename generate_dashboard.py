#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Global Money Flow & Market Dashboard
===================================

APIキー不要の公開データから、単一HTMLのダッシュボードを生成する。

データソース:
    Yahoo Finance / yfinance
    FRED 公開CSV・テキスト
    DefiLlama
ｆ
タブ:
    flowworld / risk / stress / liquidity / assetclass

注意:
    サンキー図はマクロ指標と価格モメンタムを組み合わせた概念モデル。
    実測の資金移動額、投資家の保有比率、推奨資産配分ではない。

使い方:
    python generate_dashboard.py
    python generate_dashboard.py --demo --output index.html
    python generate_dashboard.py --demo --inline-plotlyjs \
        --output ライブボード見本_v4.html
"""

from __future__ import annotations

import argparse
import html as html_lib
import io
import json
import math
import os
import random
import re
import sys
import tempfile
import time
import traceback

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Optional

import pandas as pd
import requests

from plotly.offline import get_plotlyjs, get_plotlyjs_version


# =============================================================================
# A. 設定
# =============================================================================

JST = timezone(timedelta(hours=9))

DEFAULT_LOOKBACK_DAYS = 730
DEFAULT_OUTPUT = os.path.join("public", "index.html")
CACHE_DIR = os.path.join("data", "cache")

LOOKBACK_DAYS = DEFAULT_LOOKBACK_DAYS
START_DATE = (
    datetime.now(JST) - timedelta(days=DEFAULT_LOOKBACK_DAYS)
).strftime("%Y-%m-%d")

FRED_CSV_URL = (
    "https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"
)
FRED_TXT_URL = (
    "https://fred.stlouisfed.org/data/{series_id}.txt"
)
DEFILLAMA_STABLECOIN_URL = (
    "https://stablecoins.llama.fi/stablecoincharts/all"
)

HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    ),
    "Accept": "text/csv,text/plain,text/html,application/json,*/*",
    "Accept-Language": "en-US,en;q=0.9",
}

FETCH_STATUS: list[dict] = []

BTC = "ビットコイン (BTC)"
SPX = "米国株 (S&P 500)"
N225 = "日本株 (日経平均)"
SX5E = "欧州株 (EURO STOXX 50)"
GOLD = "ゴールド (Gold)"

DXY = "米ドル指数 (DXY)"
VIX = "VIX (株式恐怖指数)"
M2 = "米国M2 ($B)"
MMF = "米国MMF残高 ($B)"
HY = "ハイイールド債スプレッド (%)"
BOND_VOL = "米国債ボラティリティ (bp)"
STABLE = "ステーブルコイン時価総額 ($B)"

TLT = "米長期債 (TLT)"
IEF = "米中期債 (IEF)"
HYG = "ハイイールド債 (HYG)"
LQD = "投資適格債 (LQD)"
VNQ = "不動産 (VNQ)"
EEM = "新興国株式 (EEM)"
EFA = "先進国株式 ex-US (EFA)"
WTI = "原油 WTI (CL=F)"

RISK_ASSETS: list[tuple[str, str]] = [
    ("BTC", BTC),
    ("SPX", SPX),
    ("N225", N225),
    ("SX5E", SX5E),
    ("GOLD", GOLD),
]

RISK_TICKERS = {
    "BTC": "BTC-USD",
    "SPX": "^GSPC",
    "N225": "^N225",
    "SX5E": "^STOXX50E",
    "GOLD": "GC=F",
}

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

ASSET_CLASS_TICKERS: dict[str, str] = {
    TLT: "TLT",
    IEF: "IEF",
    HYG: "HYG",
    LQD: "LQD",
    VNQ: "VNQ",
    EEM: "EEM",
    EFA: "EFA",
    WTI: "CL=F",
}

WORLD_COLORS = [
    "#2dd4bf",
    "#a78bfa",
    "#f97316",
    "#f472b6",
    "#84cc16",
    "#eab308",
    "#22d3ee",
    "#c084fc",
]

ETF_COLORS = [
    "#818cf8",
    "#a5b4fc",
    "#fb923c",
    "#2dd4bf",
    "#f472b6",
    "#84cc16",
    "#38bdf8",
    "#fbbf24",
]

PERIOD_LABELS = [
    ("1w", "1週間", 7),
    ("1m", "1ヶ月", 30),
    ("3m", "3ヶ月", 91),
    ("6m", "6ヶ月", 182),
    ("1y", "1年", 365),
    ("2y", "2年", 730),
]


def configure_window(lookback_days: int) -> None:
    global START_DATE, LOOKBACK_DAYS

    if lookback_days < 30:
        raise ValueError("--lookback-days は30以上を指定してください")

    LOOKBACK_DAYS = int(lookback_days)
    START_DATE = (
        datetime.now(JST) - timedelta(days=LOOKBACK_DAYS)
    ).strftime("%Y-%m-%d")


def _start_ts() -> pd.Timestamp:
    return pd.Timestamp(START_DATE)


def _record(
    label: str,
    source: str,
    ok: bool,
    detail: str = "",
) -> None:
    FETCH_STATUS.append({
        "label": label,
        "source": source,
        "ok": bool(ok),
        "detail": detail or ("正常に取得" if ok else "取得失敗"),
    })


def _escape(value) -> str:
    return html_lib.escape(str(value), quote=True)


# =============================================================================
# B. データ取得
# =============================================================================

def _clean(s: Optional[pd.Series]) -> pd.Series:
    """数値・日付インデックスを正規化し、欠損・無限値を除去する。"""
    if s is None or s.empty:
        return pd.Series(dtype=float)

    index = pd.DatetimeIndex(
        pd.to_datetime(s.index, errors="coerce")
    )

    if index.tz is not None:
        index = index.tz_localize(None)

    values = pd.to_numeric(s, errors="coerce").to_numpy(dtype=float)
    result = pd.Series(values, index=index.normalize(), dtype=float)

    result = result[~result.index.isna()].dropna()
    result = result[result.map(math.isfinite)]
    result = result[~result.index.duplicated(keep="last")]

    return result.sort_index()


def fetch_yf_series(
    label: str,
    ticker: str,
    retries: int = 3,
    wait: float = 5.0,
) -> pd.Series:
    try:
        import yfinance as yf
    except ImportError:
        _record(label, "Yahoo Finance", False, "yfinance 未インストール")
        print("[WARN] yfinance が未インストールです")
        return pd.Series(dtype=float)

    last_error = "データが空です"

    for attempt in range(1, retries + 1):
        try:
            history = yf.Ticker(ticker).history(
                start=START_DATE,
                auto_adjust=True,
            )

            if (
                history is None
                or history.empty
                or "Close" not in history.columns
            ):
                raise ValueError("終値データが空です")

            s = _clean(history["Close"])
            s = s[s.index >= _start_ts()]

            if s.empty:
                raise ValueError("取得期間内に有効なデータがありません")

            print(f"[ OK ] Yahoo {ticker}: {len(s)} 件")
            _record(label, "Yahoo Finance", True)
            return s

        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            print(
                f"[WARN] Yahoo {ticker}: {last_error} "
                f"({attempt}/{retries})"
            )

        if attempt < retries:
            time.sleep(wait * attempt)

    _record(
        label,
        "Yahoo Finance",
        False,
        f"{retries}回試行して失敗: {last_error[:100]}",
    )
    return pd.Series(dtype=float)


def _parse_fred_csv(text: str) -> pd.Series:
    text = text.lstrip("\ufeff \r\n\t")
    first_line = text.splitlines()[0] if text else ""

    if not first_line.lower().startswith(("observation_date", "date,")):
        raise ValueError("FRED CSV形式ではありません")

    frame = pd.read_csv(io.StringIO(text))

    if frame.shape[1] < 2:
        raise ValueError("FRED CSVの列数が不足しています")

    dates = pd.to_datetime(frame.iloc[:, 0], errors="coerce")
    values = pd.to_numeric(frame.iloc[:, 1], errors="coerce")

    return _clean(
        pd.Series(values.to_numpy(), index=dates)
    )


def _parse_fred_table(text: str) -> pd.Series:
    """FREDテキストまたはHTML表から日付・値の行だけを抽出する。"""
    chunks = re.split(r"(?i)</?tr[^>]*>|\n", text)
    pattern = re.compile(
        r"^\s*(\d{4}-\d{2}-\d{2})\s+"
        r"(-?\d+(?:\.\d+)?)\s*$"
    )

    records: dict[pd.Timestamp, float] = {}

    for chunk in chunks:
        line = html_lib.unescape(
            re.sub(r"<[^>]+>", " ", chunk)
        )
        match = pattern.match(line)

        if match:
            records[pd.Timestamp(match.group(1))] = float(
                match.group(2)
            )

    if not records:
        raise ValueError("FREDテキストからデータを抽出できません")

    return _clean(pd.Series(records, dtype=float))


def _cache_path(series_id: str) -> str:
    return os.path.join(CACHE_DIR, f"{series_id}.csv")


def _read_cache_all(series_id: str) -> pd.Series:
    path = _cache_path(series_id)

    if not os.path.exists(path):
        return pd.Series(dtype=float)

    try:
        frame = pd.read_csv(path, index_col=0)

        if frame.empty or frame.shape[1] == 0:
            return pd.Series(dtype=float)

        s = frame.iloc[:, 0]
        # インデックスを強制的にDatetimeIndexに変換してからcleanを通す
        s.index = pd.DatetimeIndex(s.index)
        return _clean(s)

    except Exception as exc:
        print(f"[WARN] cache read {series_id}: {exc}")
        return pd.Series(dtype=float)


def _save_cache(series_id: str, s: pd.Series) -> None:
    """既存キャッシュと結合し、過去の保存済みデータを維持する。"""
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)

        old = _read_cache_all(series_id)
        merged = _clean(pd.concat([old, s]))

        frame = pd.DataFrame({series_id: merged})
        frame.index.name = "observation_date"
        frame.to_csv(_cache_path(series_id))

    except Exception as exc:
        print(f"[WARN] cache write {series_id}: {exc}")


def _load_cache(series_id: str) -> pd.Series:
    s = _read_cache_all(series_id)
    return s[s.index >= _start_ts()]


def fetch_fred(
    series_id: str,
    label: Optional[str] = None,
    attempts_csv: int = 3,
    attempts_txt: int = 2,
) -> pd.Series:
    label = label or series_id
    last_error = "unknown"

    endpoints = [
        ("FRED CSV", FRED_CSV_URL, _parse_fred_csv, attempts_csv),
        ("FRED テキスト", FRED_TXT_URL, _parse_fred_table, attempts_txt),
    ]

    headers = {
        **HTTP_HEADERS,
        "Referer": "https://fred.stlouisfed.org/",
    }

    for source, url_template, parser, attempts in endpoints:
        for attempt in range(1, attempts + 1):
            try:
                response = requests.get(
                    url_template.format(series_id=series_id),
                    headers=headers,
                    timeout=30,
                )
                response.raise_for_status()

                full = parser(response.text)
                s = full[full.index >= _start_ts()]

                if s.empty:
                    raise ValueError("取得期間内にデータがありません")

                _save_cache(series_id, full)

                detail = (
                    "正常に取得"
                    if source == "FRED CSV"
                    else "CSV取得失敗後、テキスト形式で取得"
                )
                _record(label, source, True, detail)

                print(f"[ OK ] {source} {series_id}: {len(s)} 件")
                return s

            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                print(
                    f"[WARN] {source} {series_id}: {last_error} "
                    f"({attempt}/{attempts})"
                )

                if attempt < attempts:
                    time.sleep(4.0 * attempt)

    cached = _load_cache(series_id)

    if not cached.empty:
        print(f"[ OK ] FRED(cache) {series_id}: {len(cached)} 件（保存済みデータ）")
        _record(label, "保存済みキャッシュ", True,
                f"最新取得に失敗したため前回データを表示（{last_error[:40]}）")
        return cached

    # 【重要】取得もキャッシュもなくてもエラーで止めず、空のSeriesを返して全体を継続する
    print(f"[WARN] FRED {series_id} の取得に完全に失敗しました。この指標はスキップします。")
    _record(label, "FRED", False, f"取得失敗（スキップ継続）: {last_error[:60]}")
    return pd.Series(dtype=float)

    _record(
        label,
        "FRED",
        False,
        f"取得失敗: {last_error[:100]}",
    )
    return pd.Series(dtype=float)


def fetch_stablecoin_mcap() -> pd.Series:
    """DefiLlamaのUSD換算時価総額を十億ドル単位で取得する。"""
    try:
        response = requests.get(
            DEFILLAMA_STABLECOIN_URL,
            headers=HTTP_HEADERS,
            timeout=30,
        )
        response.raise_for_status()

        rows = response.json()

        if not isinstance(rows, list):
            raise ValueError("DefiLlamaの応答形式が不正です")

        records: dict[pd.Timestamp, float] = {}

        for row in rows:
            if not isinstance(row, dict):
                continue

            try:
                date = pd.to_datetime(
                    int(row["date"]),
                    unit="s",
                    utc=True,
                ).tz_convert(None).normalize()
            except (KeyError, TypeError, ValueError, OverflowError):
                continue

            usd_values = row.get("totalCirculatingUSD")
            amount = None

            if isinstance(usd_values, dict):
                valid = []

                for value in usd_values.values():
                    try:
                        numeric = float(value)
                    except (TypeError, ValueError):
                        continue

                    if math.isfinite(numeric) and numeric >= 0:
                        valid.append(numeric)

                if valid:
                    amount = sum(valid)

            if amount is None:
                circulating = row.get("totalCirculating") or {}
                raw = circulating.get("peggedUSD")

                if raw is not None:
                    try:
                        amount = float(raw)
                    except (TypeError, ValueError):
                        amount = None

            if (
                amount is None
                or not math.isfinite(amount)
                or amount <= 0
            ):
                continue

            records[date] = amount / 1e9

        s = _clean(pd.Series(records, dtype=float))
        s = s[s.index >= _start_ts()]

        if s.empty:
            raise ValueError("取得期間内に有効なデータがありません")

        _record(STABLE, "DefiLlama", True)
        print(f"[ OK ] DefiLlama: {len(s)} 件")
        return s

    except Exception as exc:
        _record(
            STABLE,
            "DefiLlama",
            False,
            f"{type(exc).__name__}: {str(exc)[:100]}",
        )
        print(f"[WARN] DefiLlama: {exc}")
        return pd.Series(dtype=float)


def _bond_vol(y10: pd.Series) -> pd.Series:
    """
    米10年債利回りの日次変化の20観測標準偏差、単位bp。

    ICE BofA MOVE指数そのものではない。
    """
    if y10 is None or y10.empty:
        return pd.Series(dtype=float)

    changes_bp = y10.diff() * 100.0

    return _clean(
        changes_bp.rolling(
            window=20,
            min_periods=10,
        ).std()
    )


# =============================================================================
# C. データ収集・デモ
# =============================================================================

def collect_data(demo: bool = False) -> dict[str, pd.Series]:
    if demo:
        print("[INFO] デモモード: ネットワークを使わず生成します")
        data = _demo_data()

        for label, s in data.items():
            _record(
                label,
                "デモデータ",
                not s.empty,
                "実際の市場データではありません"
                if not s.empty
                else "指定期間内にデモ観測日がありません",
            )

        return data

    data: dict[str, pd.Series] = {}

    for key, label in RISK_ASSETS:
        data[label] = fetch_yf_series(label, RISK_TICKERS[key])

    data[DXY] = fetch_yf_series(DXY, "DX-Y.NYB")
    data[VIX] = fetch_yf_series(VIX, "^VIX")

    for label, ticker in WORLD_INDEX_TICKERS.items():
        data[label] = fetch_yf_series(label, ticker)

    for label, ticker in ASSET_CLASS_TICKERS.items():
        data[label] = fetch_yf_series(label, ticker)

    data[M2] = fetch_fred("WM2NS", M2)

    # FRED MMMFFAQ027S: 百万ドル → 十億ドル
    data[MMF] = fetch_fred("MMMFFAQ027S", MMF) / 1000.0

    data[HY] = fetch_fred("BAMLH0A0HYM2", HY)

    y10 = fetch_fred("DGS10", "米10年債利回り")
    data[BOND_VOL] = _bond_vol(y10)

    data[STABLE] = fetch_stablecoin_mcap()

    return data


def _demo_data() -> dict[str, pd.Series]:
    """直接のnumpy依存なしで、再現可能なダミー系列を生成する。"""
    rng = random.Random(42)

    end = pd.Timestamp(datetime.now(JST).date())
    start = _start_ts()

    business_days = pd.bdate_range(start=start, end=end)
    calendar_days = pd.date_range(start=start, end=end, freq="D")
    weekly = pd.date_range(start=start, end=end, freq="W-WED")
    quarterly = pd.date_range(start=start, end=end, freq="QE-DEC")

    def walk(
        index: pd.DatetimeIndex,
        initial: float,
        drift: float,
        volatility: float,
    ) -> pd.Series:
        price = float(initial)
        values = []

        for _ in index:
            price *= math.exp(rng.gauss(drift, volatility))
            values.append(price)

        return pd.Series(values, index=index, dtype=float)

    def reverted(
        index: pd.DatetimeIndex,
        level: float,
        volatility: float,
        lower: float,
        upper: float,
    ) -> pd.Series:
        value = level
        values = []

        for _ in index:
            value += (
                (level - value) * 0.04
                + rng.gauss(0.0, volatility)
            )
            value = min(max(value, lower), upper)
            values.append(value)

        return pd.Series(values, index=index, dtype=float)

    data = {
        BTC: walk(calendar_days, 60000, 0.0007, 0.026),
        SPX: walk(business_days, 5800, 0.0004, 0.009),
        N225: walk(business_days, 39000, 0.0004, 0.012),
        SX5E: walk(business_days, 4900, 0.0003, 0.010),
        GOLD: walk(business_days, 2600, 0.0005, 0.008),
        DXY: walk(business_days, 104, 0.0, 0.003),
        VIX: reverted(business_days, 18, 0.7, 11, 45),
        M2: walk(weekly, 21000, 0.0010, 0.001),
        MMF: walk(quarterly, 6800, 0.008, 0.020),
        HY: reverted(business_days, 3.4, 0.06, 2.2, 6.5),
        BOND_VOL: reverted(business_days, 8, 0.3, 3, 18),
        STABLE: walk(calendar_days, 160, 0.0006, 0.004),
    }

    world_specs = [
        ("英国 (FTSE 100)", 7800, 0.0003, 0.008),
        ("ドイツ (DAX)", 18500, 0.0004, 0.009),
        ("中国 (上海総合)", 3100, 0.0002, 0.011),
        ("香港 (ハンセン)", 17500, 0.0001, 0.014),
        ("インド (Nifty 50)", 23500, 0.0006, 0.009),
        ("ブラジル (BOVESPA)", 125000, 0.0003, 0.013),
        ("オーストラリア (ASX)", 8200, 0.0003, 0.008),
        ("カナダ (TSX)", 24500, 0.0003, 0.008),
    ]

    for label, initial, drift, volatility in world_specs:
        data[label] = walk(
            business_days,
            initial,
            drift,
            volatility,
        )

    asset_specs = [
        (TLT, 95, -0.0002, 0.007),
        (IEF, 102, -0.0001, 0.005),
        (HYG, 78, 0.0002, 0.006),
        (LQD, 112, 0.0001, 0.005),
        (VNQ, 85, 0.0002, 0.010),
        (EEM, 42, 0.0003, 0.012),
        (EFA, 78, 0.0003, 0.009),
        (WTI, 72, 0.0002, 0.018),
    ]

    for label, initial, drift, volatility in asset_specs:
        data[label] = walk(
            business_days,
            initial,
            drift,
            volatility,
        )

    return data


# =============================================================================
# D. 指標計算
# =============================================================================

def normalize_100(s: pd.Series) -> pd.Series:
    if s is None or s.empty:
        return pd.Series(dtype=float)

    first = float(s.iloc[0])

    if not math.isfinite(first) or first == 0:
        return pd.Series(dtype=float)

    return s / first * 100.0


def _pct_change_n(
    s: Optional[pd.Series],
    periods: int,
) -> Optional[float]:
    if s is None or s.empty or periods < 1 or len(s) <= periods:
        return None

    base = float(s.iloc[-1 - periods])
    latest = float(s.iloc[-1])

    if not math.isfinite(base) or base == 0:
        return None

    result = (latest / base - 1.0) * 100.0
    return result if math.isfinite(result) else None


def _prior_point(
    s: Optional[pd.Series],
    days: int,
) -> Optional[float]:
    if s is None or len(s) < 2:
        return None

    target = s.index[-1] - pd.Timedelta(days=days)
    prior = s[s.index <= target]

    if prior.empty:
        return None

    tolerance = max(days * 0.35, 10)

    if (target - prior.index[-1]).days > tolerance:
        return None

    return float(prior.iloc[-1])


def period_return(
    s: Optional[pd.Series],
    days: int,
) -> Optional[float]:
    base = _prior_point(s, days)

    if base is None or base == 0 or s is None or s.empty:
        return None

    result = (float(s.iloc[-1]) / base - 1.0) * 100.0
    return result if math.isfinite(result) else None


def period_change_abs(
    s: Optional[pd.Series],
    days: int,
) -> Optional[float]:
    base = _prior_point(s, days)

    if base is None or s is None or s.empty:
        return None

    result = float(s.iloc[-1]) - base
    return result if math.isfinite(result) else None


def annualized_vol(
    s: pd.Series,
    window: int = 60,
    annual_periods: int = 252,
) -> Optional[float]:
    if s is None or len(s) < window + 1:
        return None

    returns = s.pct_change(fill_method=None).dropna().tail(window)

    if len(returns) < 2:
        return None

    value = float(
        returns.std() * math.sqrt(annual_periods) * 100.0
    )
    return value if math.isfinite(value) else None


def max_drawdown(s: pd.Series) -> Optional[float]:
    if s is None or len(s) < 5:
        return None

    peaks = s.cummax()
    drawdowns = (s / peaks - 1.0) * 100.0

    value = float(drawdowns.min())
    return value if math.isfinite(value) else None


def deviation_from_sma(
    s: pd.Series,
    window: int = 200,
) -> Optional[float]:
    if s is None or len(s) < window:
        return None

    average = float(s.tail(window).mean())

    if average == 0 or not math.isfinite(average):
        return None

    return (float(s.iloc[-1]) / average - 1.0) * 100.0


def _last_date(s: Optional[pd.Series]) -> str:
    if s is None or s.empty:
        return "—"

    return s.index[-1].strftime("%Y-%m-%d")


def _fmt(
    value: Optional[float],
    unit: str = "",
    digits: Optional[int] = None,
) -> str:
    if value is None or not math.isfinite(float(value)):
        return "—"

    value = float(value)

    if digits is None:
        absolute = abs(value)
        digits = 0 if absolute >= 1000 else 1 if absolute >= 10 else 2

    return f"{value:,.{digits}f}{unit}"


def _num(
    value: Optional[float],
    unit: str = "",
    digits: int = 1,
) -> str:
    if value is None or not math.isfinite(float(value)):
        return "—"

    return f"{float(value):+,.{digits}f}{unit}"


# =============================================================================
# E. 自動分析
# =============================================================================

def score_regime(
    data: dict[str, pd.Series],
) -> tuple[str, int, int, list[tuple[str, bool]]]:
    """
    比較可能な項目だけを分母に含める。
    データ不足の項目を自動的に減点しない。
    """
    reasons: list[tuple[str, bool]] = []

    def add(text: str, positive: bool) -> None:
        reasons.append((text, bool(positive)))

    spx = data.get(SPX)
    deviation = deviation_from_sma(spx, 200)

    if deviation is not None:
        add(
            f"S&P 500 の200観測移動平均乖離は {deviation:+.1f}%",
            deviation > 0,
        )

    vix = data.get(VIX)

    if vix is not None and not vix.empty:
        value = float(vix.iloc[-1])
        add(
            f"VIX は {value:.1f}。20未満を低警戒として評価",
            value < 20,
        )

    hy = data.get(HY)

    if hy is not None and not hy.empty:
        value = float(hy.iloc[-1])
        add(
            f"HYスプレッドは {value:.2f}%。4%未満を良好として評価",
            value < 4,
        )

    change = period_return(data.get(DXY), 30)

    if change is not None:
        add(
            f"ドル指数の1ヶ月変化は {change:+.2f}%",
            change < 0,
        )

    change = period_return(data.get(STABLE), 30)

    if change is not None:
        add(
            f"ステーブルコイン時価総額の1ヶ月変化は {change:+.2f}%",
            change > 0,
        )

    change = period_change_abs(data.get(M2), 91)

    if change is not None:
        add(
            f"米国M2の3ヶ月変化は {change:+,.0f} B$",
            change > 0,
        )

    change = period_change_abs(data.get(MMF), 91)

    if change is not None:
        add(
            f"米国MMF残高の3ヶ月変化は {change:+,.0f} B$",
            change < 0,
        )

    total = len(reasons)
    score = sum(1 for _, positive in reasons if positive)

    if total == 0:
        return (
            "判定不能",
            0,
            0,
            [("判定に必要なデータが不足しています。", False)],
        )

    ratio = score / total

    if ratio >= 0.7:
        label = "リスクオン"
    elif ratio >= 0.4:
        label = "中立"
    else:
        label = "リスクオフ"

    return label, score, total, reasons


def _level_comment(
    vix: Optional[float],
    hy: Optional[float],
) -> str:
    comments = []

    if vix is not None:
        if vix >= 30:
            comments.append("VIXは30以上で強い警戒")
        elif vix >= 20:
            comments.append("VIXは20以上でやや警戒")
        else:
            comments.append("VIXは20未満で比較的落ち着いた水準")

    if hy is not None:
        if hy >= 6:
            comments.append("HYスプレッドは6%以上で信用不安が強い")
        elif hy >= 4:
            comments.append("HYスプレッドは4%以上で信用リスクに注意")
        else:
            comments.append("HYスプレッドは4%未満")

    return (
        "。".join(comments) + "。"
        if comments
        else "ストレス指標のデータが不足しています。"
    )


def build_performance_table(
    data: dict[str, pd.Series],
) -> list[dict]:
    specs = [
        (BTC, "", 0),
        (SPX, "", 0),
        (N225, "", 0),
        (SX5E, "", 0),
        (GOLD, "", 0),
        (STABLE, " B$", 1),
        (M2, " B$", 0),
        (MMF, " B$", 0),
    ]

    rows = []

    for label, unit, digits in specs:
        s = data.get(label)

        if s is None or s.empty:
            continue

        values = {
            key: period_return(s, days)
            for key, _, days in PERIOD_LABELS
        }
        values["all"] = _pct_change_n(s, len(s) - 1)

        rows.append({
            "label": label,
            "last": _fmt(s.iloc[-1], unit, digits),
            "date": _last_date(s),
            "values": values,
        })

    return rows


def build_analysis(data: dict[str, pd.Series]) -> dict:
    label, score, total, reasons = score_regime(data)

    def latest(name: str) -> Optional[float]:
        s = data.get(name)
        return (
            float(s.iloc[-1])
            if s is not None and not s.empty
            else None
        )

    risk_metrics = []

    for name in (SPX, BTC, N225):
        s = data.get(name)

        if s is None or s.empty:
            continue

        risk_metrics.append({
            "label": name,
            "vol": annualized_vol(
                s,
                annual_periods=365 if name == BTC else 252,
            ),
            "mdd": max_drawdown(s),
            "sma_dev": deviation_from_sma(s),
        })

    return {
        "regime_label": label,
        "score": score,
        "total": total,
        "reasons": reasons,
        "stress_text": _level_comment(latest(VIX), latest(HY)),
        "risk_metrics": risk_metrics,
        "table": build_performance_table(data),
    }


# =============================================================================
# F. 5タブの定義
# =============================================================================

# リスク資産タブは BTC + 米日欧株 + 世界指数8本 = 12系列。
# ゴールドはフロー、スナップショット、期間別表に表示する。
RISK_CHART_SERIES = [
    (BTC, "#f7931a"),
    (SPX, "#38bdf8"),
    (N225, "#fb7185"),
    (SX5E, "#a78bfa"),
] + list(zip(WORLD_INDEX_TICKERS.keys(), WORLD_COLORS))

CHART_GROUPS: list[dict] = [
    {
        "key": "flowworld",
        "label": "マネーフロー",
        "caption": (
            "1ヶ月モメンタム＋マクロ指標の概念モデル"
            "／実測資金フローではありません"
        ),
        "series": [],
        "normalize": False,
        "is_flowworld": True,
        "y1": "",
        "y2": None,
        "hline": None,
    },
    {
        "key": "risk",
        "label": "リスク資産",
        "caption": (
            "12系列／取得開始時点=100"
            "／各指数の現地通貨建てリターン"
        ),
        "series": RISK_CHART_SERIES,
        "normalize": True,
        "y1": "指数（取得開始時点=100）",
        "y2": None,
        "hline": 100.0,
    },
    {
        "key": "stress",
        "label": "ストレス",
        "caption": (
            "DXY・VIX・債券ボラ代理・HY"
            "／右軸は異なる単位の参考比較"
        ),
        "series": [
            (DXY, "#e9eefa", "y", 2.2, "solid", False),
            (VIX, "#fb7185", "y2", 1.8, "dot", False),
            (BOND_VOL, "#a78bfa", "y2", 1.8, "solid", False),
            (HY, "#fb923c", "y2", 1.8, "dash", False),
        ],
        "normalize": False,
        "y1": "DXY",
        "y2": "VIX / 債券ボラ bp / HY %",
        "hline": None,
    },
    {
        "key": "liquidity",
        "label": "流動性",
        "caption": (
            "M2・MMF・ステーブルコイン"
            "／取得開始時点=100／実額は右の一覧"
        ),
        "series": [
            (M2, "#22d3ee"),
            (MMF, "#818cf8"),
            (STABLE, "#34d399"),
        ],
        "normalize": True,
        "y1": "指数（取得開始時点=100）",
        "y2": None,
        "hline": 100.0,
    },
    {
        "key": "assetclass",
        "label": "資産クラス",
        "caption": "ETF・原油8系列／取得開始時点=100",
        "series": list(zip(ASSET_CLASS_TICKERS.keys(), ETF_COLORS)),
        "normalize": True,
        "y1": "指数（取得開始時点=100）",
        "y2": None,
        "hline": 100.0,
    },
]

SNAPSHOT_SERIES: list[tuple[str, str, int]] = [
    (BTC, "", 0),
    (SPX, "", 0),
    (N225, "", 0),
    (SX5E, "", 0),
    (GOLD, "", 0),
    (DXY, "", 1),
    (VIX, "", 2),
    (HY, " %", 2),
    (BOND_VOL, " bp", 1),
    (STABLE, " B$", 1),
    (M2, " B$", 0),
    (MMF, " B$", 0),
] + [
    (label, "", 0)
    for label in WORLD_INDEX_TICKERS
] + [
    (label, "", 2)
    for label in ASSET_CLASS_TICKERS
]


# =============================================================================
# G. フロー概念モデル
# =============================================================================

FLOW_CLASSES = ["株式", "債券", "商品", "暗号資産", "不動産"]

FLOW_REGIONS = [
    "北米",
    "欧州",
    "日本",
    "アジア",
    "ラテン",
    "大洋州",
    "新興国",
    "先進国",
    "グローバル",
]

FLOW_CLASS_COLORS = [
    "#38bdf8",
    "#818cf8",
    "#fbbf24",
    "#f472b6",
    "#34d399",
]

ASSET_CLASS_MAP = {
    BTC: "暗号資産",
    SPX: "株式",
    N225: "株式",
    SX5E: "株式",
    GOLD: "商品",
    **{label: "株式" for label in WORLD_INDEX_TICKERS},
    TLT: "債券",
    IEF: "債券",
    HYG: "債券",
    LQD: "債券",
    VNQ: "不動産",
    EEM: "株式",
    EFA: "株式",
    WTI: "商品",
}

ASSET_REGION_MAP = {
    BTC: "グローバル",
    SPX: "北米",
    N225: "日本",
    SX5E: "欧州",
    GOLD: "グローバル",
    "英国 (FTSE 100)": "欧州",
    "ドイツ (DAX)": "欧州",
    "中国 (上海総合)": "アジア",
    "香港 (ハンセン)": "アジア",
    "インド (Nifty 50)": "アジア",
    "ブラジル (BOVESPA)": "ラテン",
    "オーストラリア (ASX)": "大洋州",
    "カナダ (TSX)": "北米",
    TLT: "北米",
    IEF: "北米",
    HYG: "北米",
    LQD: "北米",
    VNQ: "北米",
    EEM: "新興国",
    EFA: "先進国",
    WTI: "グローバル",
}


def calc_flow_score(
    s: Optional[pd.Series],
    days: int = 30,
) -> Optional[float]:
    """価格リターンをtanhで[-1, 1]に変換した参考スコア。"""
    change = period_return(s, days)

    if change is None:
        return None

    return math.tanh(change / 15.0)


def build_sankey_flow(data: dict[str, pd.Series]) -> dict:
    """
    マクロ指標 → 資産クラス → 地域 の概念モデル。

    ・リンク幅は無次元のモデル強度。
    ・資産クラス中間ノードの流入と流出を一致させる。
    ・負のモメンタムはヒートマップに表示する。
    ・欠損は0として扱わない。
    """
    source_specs = [
        (
            "M2拡大",
            M2,
            91,
            1.0,
            {"株式": 0.5, "債券": 0.3, "商品": 0.2},
        ),
        (
            "MMF減少",
            MMF,
            91,
            -1.0,
            {"株式": 0.6, "債券": 0.4},
        ),
        (
            "ドル安",
            DXY,
            30,
            -1.0,
            {"株式": 0.4, "商品": 0.35, "債券": 0.25},
        ),
        (
            "ステーブル増加",
            STABLE,
            30,
            1.0,
            {"暗号資産": 1.0},
        ),
        (
            "VIX低下",
            VIX,
            30,
            -1.0,
            {"株式": 0.7, "不動産": 0.3},
        ),
    ]

    source_names = [item[0] for item in source_specs]
    nodes = source_names + FLOW_CLASSES + FLOW_REGIONS
    node_index = {name: index for index, name in enumerate(nodes)}

    source_colors = [
        "#22d3ee",
        "#5eead4",
        "#a78bfa",
        "#34d399",
        "#fb923c",
    ]
    region_colors = [
        "#38bdf8",
        "#a78bfa",
        "#fb7185",
        "#f97316",
        "#eab308",
        "#2dd4bf",
        "#84cc16",
        "#c084fc",
        "#94a3b8",
    ]
    node_colors = (
        source_colors + FLOW_CLASS_COLORS + region_colors
    )

    asset_scores: dict[str, float] = {}
    class_values: dict[str, list[float]] = {
        name: [] for name in FLOW_CLASSES
    }
    region_values: dict[str, list[float]] = {
        name: [] for name in FLOW_REGIONS
    }

    positive_by_class_region: dict[str, dict[str, float]] = {
        name: defaultdict(float) for name in FLOW_CLASSES
    }

    for label, asset_class in ASSET_CLASS_MAP.items():
        score = calc_flow_score(data.get(label), 30)

        if score is None:
            continue

        region = ASSET_REGION_MAP[label]

        asset_scores[label] = score
        class_values[asset_class].append(score)
        region_values[region].append(score)

        if score > 0:
            positive_by_class_region[asset_class][region] += score

    class_scores = {
        name: sum(values) / len(values) if values else None
        for name, values in class_values.items()
    }
    region_scores = {
        name: sum(values) / len(values) if values else None
        for name, values in region_values.items()
    }

    links = []
    class_incoming = {name: 0.0 for name in FLOW_CLASSES}

    for source_name, label, days, sign, weights in source_specs:
        score = calc_flow_score(data.get(label), days)

        if score is None:
            continue

        strength = max(0.0, sign * score)

        if strength <= 0:
            continue

        eligible = {}

        for asset_class, prior_weight in weights.items():
            momentum = class_scores.get(asset_class)

            if momentum is not None and momentum > 0:
                eligible[asset_class] = prior_weight * momentum

        denominator = sum(eligible.values())

        if denominator <= 0:
            continue

        for asset_class, weight in eligible.items():
            value = strength * weight / denominator

            links.append({
                "source": node_index[source_name],
                "target": node_index[asset_class],
                "value": value,
            })
            class_incoming[asset_class] += value

    for asset_class, incoming in class_incoming.items():
        regions = positive_by_class_region[asset_class]
        denominator = sum(regions.values())

        if incoming <= 0 or denominator <= 0:
            continue

        for region, weight in regions.items():
            links.append({
                "source": node_index[asset_class],
                "target": node_index[region],
                "value": incoming * weight / denominator,
            })

    return {
        "nodes": nodes,
        "node_colors": node_colors,
        "links": links,
        "scores": asset_scores,
        "class_scores": class_scores,
        "region_scores": region_scores,
    }


def build_flow_heatmap(
    data: dict[str, pd.Series],
    flow: Optional[dict] = None,
) -> list[dict]:
    """同じ地域×資産クラスの複数系列を平均する。"""
    if flow is None:
        flow = build_sankey_flow(data)

    buckets = defaultdict(list)

    for label, score in flow["scores"].items():
        region = ASSET_REGION_MAP[label]
        asset_class = ASSET_CLASS_MAP[label]

        buckets[(region, asset_class)].append({
            "score": score,
            "return": period_return(data.get(label), 30),
        })

    rows = []

    for region in FLOW_REGIONS:
        for asset_class in FLOW_CLASSES:
            values = buckets[(region, asset_class)]

            scores = [item["score"] for item in values]
            returns = [
                item["return"]
                for item in values
                if item["return"] is not None
            ]

            rows.append({
                "region": region,
                "class": asset_class,
                "score": (
                    sum(scores) / len(scores) if scores else None
                ),
                "return_1m": (
                    sum(returns) / len(returns) if returns else None
                ),
                "count": len(values),
            })

    return rows


def build_rotation_wheel(
    data: dict[str, pd.Series],
    flow: Optional[dict] = None,
) -> dict:
    """正の平均モメンタムの強度比。実際の資産配分ではない。"""
    if flow is None:
        flow = build_sankey_flow(data)

    values = [
        max(0.0, flow["class_scores"].get(name) or 0.0)
        for name in FLOW_CLASSES
    ]

    return {
        "labels": FLOW_CLASSES,
        "values": values,
        "colors": FLOW_CLASS_COLORS,
    }


# =============================================================================
# H. チャートペイロード
# =============================================================================

def _series_to_points(
    s: pd.Series,
    limit: int = 600,
) -> tuple[list[str], list[float]]:
    if s is None or s.empty:
        return [], []

    # 最終点を含め、最大limit点程度に間引く。
    if len(s) > limit:
        step = max(1, math.ceil((len(s) - 1) / (limit - 1)))
        sampled = s.iloc[::step]

        if sampled.index[-1] != s.index[-1]:
            sampled = pd.concat([sampled, s.iloc[[-1]]])
    else:
        sampled = s

    dates = [
        date.strftime("%Y-%m-%d")
        for date in sampled.index
    ]
    values = [
        round(float(value), 6)
        for value in sampled
    ]

    return dates, values


def build_chart_payload(
    data: dict[str, pd.Series],
) -> list[dict]:
    """
    データがなくても5タブを維持する。
    空タブはブラウザ側で「データなし」を表示する。
    """
    groups = []

    for specification in CHART_GROUPS:
        traces = []

        for item in specification["series"]:
            if len(item) == 2:
                label, color = item
                axis, width, dash, fill = (
                    "y",
                    2.0,
                    "solid",
                    False,
                )
            else:
                label, color, axis, width, dash, fill = item

            s = data.get(label)

            if s is None or s.empty:
                continue

            values_series = (
                normalize_100(s)
                if specification["normalize"]
                else s
            )
            dates, values = _series_to_points(values_series)

            if not dates:
                continue

            unit = ""

            if not specification["normalize"]:
                if label == BOND_VOL:
                    unit = " bp"
                elif label == HY:
                    unit = " %"
                elif label in (M2, MMF, STABLE):
                    unit = " B$"

            traces.append({
                "name": label,
                "dates": dates,
                "values": values,
                "color": color,
                "axis": axis,
                "width": width,
                "dash": dash,
                "fill": fill,
                "markers": label == MMF,
                "unit": unit,
            })

        groups.append({
            "key": specification["key"],
            "label": specification["label"],
            "caption": specification["caption"],
            "is_flowworld": specification.get("is_flowworld", False),
            "normalize": specification["normalize"],
            "y1": specification["y1"],
            "y2": specification["y2"],
            "hline": specification["hline"],
            "traces": traces,
        })

    return groups


# =============================================================================
# I. HTML断片
# =============================================================================

def _spark_svg(s: pd.Series) -> str:
    if s is None or len(s) < 3:
        return ""

    tail = s.tail(48)
    minimum = float(tail.min())
    maximum = float(tail.max())

    if maximum == minimum:
        maximum = minimum + 1.0

    width, height = 66.0, 16.0
    count = len(tail)
    points = []

    for index, value in enumerate(tail):
        x = index / (count - 1) * width
        y = (
            height
            - (float(value) - minimum)
            / (maximum - minimum)
            * (height - 2)
            - 1
        )
        points.append(f"{x:.1f},{y:.1f}")

    color = (
        "#34d399"
        if tail.iloc[-1] >= tail.iloc[0]
        else "#fb7185"
    )
    points_text = " ".join(points)

    return (
        '<svg class="spark" viewBox="0 0 66 16" '
        'preserveAspectRatio="none" aria-hidden="true">'
        f'<polyline points="{points_text}" fill="none" '
        f'stroke="{color}" stroke-width="1.4" '
        'stroke-linejoin="round" stroke-linecap="round"/>'
        '</svg>'
    )


def _chip(value: Optional[float]) -> str:
    if value is None:
        return '<span class="chip missing">—</span>'

    css_class = (
        "up" if value > 0.0001
        else "down" if value < -0.0001
        else "flat"
    )

    return (
        f'<span class="chip {css_class}">'
        f'{value:+.2f}%</span>'
    )


def render_snapshot(data: dict[str, pd.Series]) -> str:
    rows = []

    for label, unit, digits in SNAPSHOT_SERIES:
        s = data.get(label)

        if s is None or s.empty:
            continue

        name = (
            label
            .replace(" ($B)", "")
            .replace(" (%)", "")
            .replace(" (bp)", "")
        )

        rows.append(
            f'<div class="mrow" title="最終観測日 {_last_date(s)}">'
            f'<div class="nm">{_escape(name)}</div>'
            f'<div class="vl">{_fmt(s.iloc[-1], unit, digits)}</div>'
            f'<div class="rt">{_spark_svg(s)}'
            f'{_chip(period_return(s, 30))}'
            f'<span class="mdate">{_last_date(s)}</span>'
            '</div></div>'
        )

    return "\n".join(rows) or '<p class="empty">データがありません。</p>'


def _heat_bg(value: float) -> str:
    alpha = 0.10 + min(abs(value) / 25.0, 1.0) * 0.55
    rgb = "52,211,153" if value >= 0 else "251,113,133"

    return f' style="background:rgba({rgb},{alpha:.2f})"'


def render_heat(rows: list[dict]) -> str:
    if not rows:
        return '<p class="empty">表示できるデータがありません。</p>'

    headers = [
        "<tr><th>指標</th><th>直近値</th>"
    ]
    headers.extend(
        f"<th>{_escape(label)}</th>"
        for _, label, _ in PERIOD_LABELS
    )
    headers.append("<th>全期間</th></tr>")

    body = []

    for row in rows:
        name = row["label"].split(" (")[0]

        cells = [
            f'<td title="最終観測日 {_escape(row["date"])}">'
            f'{_escape(name)}</td>',
            f'<td>{_escape(row["last"])}</td>',
        ]

        keys = [key for key, _, _ in PERIOD_LABELS] + ["all"]

        for key in keys:
            value = row["values"].get(key)

            if value is None:
                cells.append('<td class="na">—</td>')
            else:
                css_class = "pos" if value >= 0 else "neg"

                cells.append(
                    f'<td class="h {css_class}"{_heat_bg(value)}>'
                    f'{value:+.1f}</td>'
                )

        body.append("<tr>" + "".join(cells) + "</tr>")

    return (
        '<div class="tableScroll">'
        '<table class="heat"><thead>'
        + "".join(headers)
        + "</thead><tbody>"
        + "\n".join(body)
        + "</tbody></table></div>"
    )


def render_reasons(reasons: list[tuple[str, bool]]) -> str:
    return "\n".join(
        f'<li class="{"on" if positive else "off"}">'
        f'{_escape(text)}</li>'
        for text, positive in reasons
    )


def render_ticker(data: dict[str, pd.Series]) -> str:
    items = []

    for label, unit, digits in SNAPSHOT_SERIES:
        s = data.get(label)

        if s is None or s.empty:
            continue

        change = _pct_change_n(s, 1)
        css_class = (
            "up" if change is not None and change > 0.0001
            else "down" if change is not None and change < -0.0001
            else "flat"
        )
        change_text = (
            "—" if change is None else f"{change:+.2f}%"
        )

        items.append(
            '<span>'
            f'{_escape(label.split(" (")[0])} '
            f'<b>{_fmt(s.iloc[-1], unit, digits)}</b>'
            f'<em class="{css_class}">{change_text}</em>'
            '</span>'
        )

    if not items:
        return '<span>市場データを取得できませんでした</span>'

    tape = "".join(items)
    return tape + tape


def render_status_grid() -> tuple[str, str]:
    if not FETCH_STATUS:
        return (
            "記録なし",
            '<div class="sitem"><span class="dot ng"></span>'
            '<span>取得記録がありません</span></div>',
        )

    items = []

    for status in FETCH_STATUS:
        css_class = "ok" if status["ok"] else "ng"
        title = _escape(
            f'{status["source"]}／{status["detail"]}'
        )
        label = _escape(status["label"].split(" (")[0])

        items.append(
            f'<div class="sitem" title="{title}">'
            f'<span class="dot {css_class}"></span>'
            f'<span>{label}</span></div>'
        )

    failures = sum(not item["ok"] for item in FETCH_STATUS)
    cached = sum(
        item["source"] == "保存済みキャッシュ"
        for item in FETCH_STATUS
    )

    summary = (
        f"全{len(FETCH_STATUS)}系列／失敗{failures}／キャッシュ{cached}"
    )

    return summary, "\n".join(items)


def render_risk_metrics(metrics: list[dict]) -> str:
    rows = []

    for metric in metrics:
        rows.append(
            '<div class="riskRow">'
            f'<strong>{_escape(metric["label"].split(" (")[0])}</strong>'
            '<div>'
            f'<span>年率ボラ {_fmt(metric["vol"], "%", 1)}</span>'
            f'<span>最大DD {_num(metric["mdd"], "%", 1)}</span>'
            f'<span>200観測線 {_num(metric["sma_dev"], "%", 1)}</span>'
            '</div></div>'
        )

    return "\n".join(rows) or '<p class="empty">データがありません。</p>'


# =============================================================================
# J. HTMLテンプレート
# =============================================================================

HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="dark">
<title>Global Money Flow — 資金フロー・ライブボード</title>
<style>
:root{
  --ink:#e9eefa;
  --ink2:#a8b7cd;
  --muted:#6f819a;
  --line:rgba(148,163,184,.16);
  --line2:rgba(148,163,184,.08);
  --up:#34d399;
  --down:#fb7185;
  --accent:#22d3ee;
  --panelbg:rgba(16,25,41,.84);
}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{
  margin:0;
  color:var(--ink);
  font-family:'Hiragino Kaku Gothic ProN','Hiragino Sans',
    'Yu Gothic UI','Yu Gothic','Meiryo','Noto Sans JP',
    'Segoe UI',sans-serif;
  font-size:13px;
  line-height:1.55;
  -webkit-font-smoothing:antialiased;
  background:
    radial-gradient(1100px 560px at 10% -12%,
      rgba(34,211,238,.13),transparent 62%),
    radial-gradient(980px 520px at 96% -6%,
      rgba(94,234,212,.09),transparent 58%),
    #060a12;
  overflow-x:hidden;
}
button{font-family:inherit}
button:focus-visible{
  outline:2px solid var(--accent);
  outline-offset:3px;
}
button:disabled{opacity:.4;cursor:not-allowed}
[hidden]{display:none!important}

.app{
  min-height:100vh;
  min-height:100dvh;
  display:flex;
  flex-direction:column;
}
header{
  display:flex;
  align-items:center;
  justify-content:space-between;
  gap:16px;
  padding:12px 16px;
}
.brand{display:flex;align-items:center;gap:11px;min-width:0}
.logo{
  width:34px;height:34px;border-radius:10px;
  background:conic-gradient(from 210deg,#22d3ee,#5eead4,#38bdf8,#22d3ee);
  display:grid;place-items:center;
  color:#04121a;font-weight:800;font-size:18px;
  flex:0 0 auto;
}
h1{margin:0;font-size:17px;line-height:1.4}
h1 small{
  display:block;font-size:10px;font-weight:500;
  color:var(--muted);margin-top:3px;
}
.topright{display:flex;align-items:center;gap:10px}
.stamp{font-size:11px;color:var(--ink2);text-align:right}
.stamp small{display:block;color:var(--muted)}
.ghost{
  background:rgba(148,163,184,.08);
  border:1px solid var(--line);
  color:var(--ink2);
  border-radius:9px;
  width:30px;height:30px;
  cursor:pointer;
}
.demoBadge{
  display:inline-block;margin-left:7px;
  padding:1px 6px;border-radius:5px;
  font-size:10px;background:#7c2d12;color:#fed7aa;
}

.ticker{
  overflow:hidden;
  border-top:1px solid var(--line2);
  border-bottom:1px solid var(--line2);
  background:rgba(8,14,24,.6);
  height:31px;
  flex:0 0 auto;
  display:flex;align-items:center;
}
.tape{
  display:flex;gap:26px;white-space:nowrap;
  width:max-content;padding-left:12px;
  animation:slide 70s linear infinite;
}
.ticker:hover .tape{animation-play-state:paused}
@keyframes slide{
  from{transform:translateX(0)}
  to{transform:translateX(-50%)}
}
.tape span{
  display:inline-flex;align-items:center;gap:7px;
  font-size:11px;color:var(--ink2);
}
.tape b{color:var(--ink);font-variant-numeric:tabular-nums}
.tape em{font-style:normal;font-size:10px;font-weight:700}
.up{color:var(--up)}
.down{color:var(--down)}
.flat{color:#94a3b8}

.grid{
  display:grid;
  grid-template-columns:270px minmax(0,1fr) 300px;
  gap:12px;padding:12px 14px;
  flex:1 0 auto;
  align-items:stretch;
  min-width:0;
}
.col{
  display:flex;flex-direction:column;gap:12px;
  min-width:0;min-height:0;
}
.panel{
  background:var(--panelbg);
  border:1px solid var(--line);
  border-radius:14px;
  padding:12px;
  display:flex;flex-direction:column;
  min-width:0;min-height:0;
  box-shadow:0 14px 34px -24px rgba(0,0,0,.95);
}
.panel-h{
  display:flex;align-items:center;justify-content:space-between;
  gap:8px;margin:0 0 9px;
  font-size:10px;font-weight:800;
  letter-spacing:.07em;color:var(--muted);
}
.panel-h .r{
  font-size:9px;font-weight:500;
  letter-spacing:0;text-align:right;
}
.empty{margin:5px 0;color:var(--muted);font-size:11px}

.gaugeBox{position:relative}
.gaugeBox svg{display:block;width:100%;max-width:250px;margin:auto}
.gaugeCenter{
  position:absolute;left:0;right:0;top:43%;
  text-align:center;
}
.gaugeCenter .n{
  font-size:33px;font-weight:800;line-height:1;
  color:#7dd3fc;font-variant-numeric:tabular-nums;
}
.gaugeCenter .d{font-size:9px;color:var(--muted);margin-top:4px}
.regimeName{
  text-align:center;font-size:16px;font-weight:800;margin:7px 0;
}
.regimeNote{font-size:10px;color:var(--muted);text-align:center}
.reasons{
  list-style:none;margin:10px 0 0;padding:0;
  display:flex;flex-direction:column;gap:7px;
}
.reasons li{
  position:relative;padding-left:14px;
  font-size:10.5px;color:var(--ink2);
}
.reasons li::before{
  content:"";position:absolute;left:2px;top:.55em;
  width:5px;height:5px;border-radius:50%;
}
.reasons li.on::before{background:var(--up)}
.reasons li.off::before{background:var(--down)}
.stressPanel{flex:1}
.stressText{margin:0 0 14px;font-size:11px;color:var(--ink2)}
.riskRow{padding:8px 0;border-bottom:1px solid var(--line2)}
.riskRow strong{font-size:11px}
.riskRow div{
  display:flex;flex-wrap:wrap;gap:4px 10px;
  margin-top:4px;font-size:10px;color:var(--muted);
}

.chartPanel{flex:1;padding:10px}
.tabsRow{
  display:flex;align-items:center;justify-content:space-between;
  flex-wrap:wrap;gap:8px;margin-bottom:6px;
  flex:0 0 auto;
}
.tabs{
  display:flex;gap:3px;
  max-width:100%;overflow-x:auto;
  background:rgba(148,163,184,.07);
  padding:3px;border-radius:10px;
}
.tab{
  border:0;background:transparent;color:var(--ink2);
  font-size:11px;font-weight:700;
  padding:6px 9px;border-radius:7px;
  cursor:pointer;white-space:nowrap;
}
.tab:hover{background:rgba(148,163,184,.10)}
.tab.active{
  background:rgba(34,211,238,.20);
  color:#d7fbff;
  box-shadow:inset 0 0 0 1px rgba(34,211,238,.4);
}
.tools{display:flex;align-items:center;gap:6px}
.periods{
  display:flex;gap:2px;padding:3px;
  background:rgba(148,163,184,.07);border-radius:8px;
}
.pbtn{
  border:0;background:transparent;color:var(--muted);
  font-size:10px;font-weight:700;
  padding:4px 6px;border-radius:5px;cursor:pointer;
}
.pbtn.active{background:rgba(94,234,212,.18);color:#d7fff7}
.autobtn{
  border:1px solid var(--line);
  background:rgba(148,163,184,.07);
  color:var(--muted);
  padding:5px 7px;border-radius:7px;
  font-size:10px;cursor:pointer;
}
.autobtn.on{
  color:#d7fff7;border-color:rgba(94,234,212,.4);
  background:rgba(94,234,212,.14);
}
.chartCaption{
  color:var(--muted);font-size:10px;
  min-height:18px;margin:0 2px 7px;
  flex:0 0 auto;
}

/*
 * 描画領域には確定した高さを与える。
 * 小さい画面ではページをスクロールさせ、チャートをつぶさない。
 */
.chartShell{
  position:relative;
  width:100%;
  height:clamp(480px,calc(100dvh - 430px),900px);
  min-height:480px;
  flex:1 0 auto;
  min-width:0;
}

/*
 * 全タブの外側コンテナは同じ配置方式に統一。
 * 非表示制御はhidden属性のみ。
 */
.plot{
  position:absolute;
  inset:0;
  width:100%;
  height:100%;
  min-width:0;
  min-height:0;
}

/*
 * マネーフローの内部はgrid。
 * showTab()からdisplay:blockを設定しないこと。
 */
.flowWorldPlot{
  display:grid;
  grid-template-rows:minmax(0,3fr) minmax(0,2fr);
  gap:9px;
}
.fwBottom{
  display:grid;
  grid-template-columns:minmax(0,1.65fr) minmax(0,1fr);
  gap:9px;
  min-width:0;min-height:0;
}
.fwMap,.fwTime,.fwWheel{
  width:100%;height:100%;
  min-width:0;min-height:0;
  overflow:hidden;
  border:1px solid var(--line2);
  border-radius:10px;
  background:rgba(8,14,24,.4);
}
.chartError{
  position:absolute;inset:12px;
  z-index:20;display:grid;place-items:center;
  padding:20px;text-align:center;
  color:#fecdd3;background:rgba(15,23,42,.95);
  border:1px solid rgba(251,113,133,.35);
  border-radius:10px;font-size:12px;
}
.js-plotly-plot .plotly .modebar{display:none!important}

.heatPanel{flex:0 0 auto}
.tableScroll{overflow-x:auto}
table.heat{
  width:100%;border-collapse:collapse;
  font-size:10px;font-variant-numeric:tabular-nums;
}
table.heat th{
  padding:4px;text-align:right;color:var(--muted);
  border-bottom:1px solid var(--line);
  font-size:9px;white-space:nowrap;
}
table.heat td{
  padding:4px;text-align:right;white-space:nowrap;
  border-bottom:1px solid var(--line2);
}
table.heat th:first-child,table.heat td:first-child{text-align:left}
table.heat td:first-child{font-weight:600}
table.heat .pos{color:#d1fae5}
table.heat .neg{color:#ffe4e6}
table.heat .na{color:#43536b}

.metrics{flex:1}
.mlist{
  overflow:auto;max-height:640px;
  scrollbar-width:thin;
}
.mrow{
  display:grid;grid-template-columns:minmax(0,1fr) auto;
  gap:3px 8px;padding:7px 0;
  border-bottom:1px solid var(--line2);
}
.mrow .nm{
  font-size:10.5px;color:var(--ink2);
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap;
}
.mrow .vl{
  font-size:12px;font-weight:800;
  font-variant-numeric:tabular-nums;text-align:right;
}
.mrow .rt{
  grid-column:1/-1;display:flex;align-items:center;gap:7px;
}
.spark{width:66px;height:16px;flex:0 0 auto}
.chip{
  font-size:9px;font-weight:800;
  padding:1px 5px;border-radius:4px;
}
.chip.up{color:#062e22;background:rgba(52,211,153,.85)}
.chip.down{color:#3d0713;background:rgba(251,113,133,.85)}
.chip.flat{color:#0b1220;background:rgba(148,163,184,.6)}
.chip.missing{color:var(--muted);background:rgba(148,163,184,.10)}
.mdate{margin-left:auto;font-size:9px;color:#536780}

.sgrid{
  display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);
  gap:5px 8px;font-size:9.5px;color:var(--muted);
  max-height:190px;overflow:auto;
}
.sitem{display:flex;align-items:center;gap:5px;min-width:0}
.sitem span:last-child{
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap;
}
.dot{width:6px;height:6px;border-radius:50%;flex:0 0 auto}
.dot.ok{background:var(--up)}
.dot.ng{background:var(--down)}

footer{
  display:flex;justify-content:space-between;
  flex-wrap:wrap;gap:5px 18px;
  padding:9px 16px;
  border-top:1px solid var(--line2);
  color:#61748e;font-size:9.5px;
}
footer b{color:var(--ink2)}
.note{color:var(--muted);font-size:9.5px;margin:8px 0 0}

@media(min-width:1600px){
  .grid{grid-template-columns:310px minmax(0,1fr) 340px}
}
@media(max-width:1180px){
  .grid{grid-template-columns:minmax(0,1fr)}
  .center{order:1}
  .left{order:2}
  .right{order:3}
  .chartShell{height:660px;min-height:660px;flex:none}
  .mlist{max-height:480px}
  .sgrid{max-height:none}
}
@media(max-width:600px){
  header{align-items:flex-start;flex-wrap:wrap;padding:10px}
  .topright{width:100%;justify-content:space-between}
  .stamp{text-align:left}
  .grid{padding:8px;gap:8px}
  .col{gap:8px}
  .panel{padding:10px}
  .tab{font-size:10px;padding:6px 7px}
  .chartShell{height:920px;min-height:920px}
  .flowWorldPlot{
    grid-template-rows:minmax(0,.9fr) minmax(0,1.1fr);
  }
  .fwBottom{
    grid-template-columns:minmax(0,1fr);
    grid-template-rows:minmax(0,1.1fr) minmax(0,.9fr);
  }
}
@media(prefers-reduced-motion:reduce){
  .tape{animation:none}
}
</style>
</head>
<body>
<div class="app">
  <header>
    <div class="brand">
      <div class="logo">◎</div>
      <div>
        <h1>Global Money Flow __DEMO_BADGE__
          <small>世界の資金フロー定点観測 ／ リスク・ストレス・流動性</small>
        </h1>
      </div>
    </div>
    <div class="topright">
      <div class="stamp">
        <b>生成日時 __UPDATED_AT__</b>
        <small>観測期間 __PERIOD__</small>
      </div>
      <button class="ghost" id="fsBtn" type="button"
        title="全画面表示（F）" aria-label="全画面表示">⛶</button>
    </div>
  </header>

  <div class="ticker"><div class="tape">__TICKER__</div></div>

  <main class="grid">
    <section class="col left">
      <div class="panel">
        <div class="panel-h">
          地合い判定
          <span class="r">__SCORE__ / __TOTAL__ 項目</span>
        </div>
        <div class="gaugeBox">
          <svg viewBox="0 0 240 132" aria-hidden="true">
            <defs>
              <linearGradient id="gg" x1="0" y1="1" x2="1" y2="0">
                <stop offset="0%" stop-color="#fb7185"/>
                <stop offset="52%" stop-color="#fbbf24"/>
                <stop offset="100%" stop-color="#34d399"/>
              </linearGradient>
            </defs>
            <path d="M22,118 A98,98 0 0,1 218,118"
              fill="none" stroke="rgba(148,163,184,.16)"
              stroke-width="13" stroke-linecap="round"/>
            <path id="gaugeArc" d="M22,118 A98,98 0 0,1 218,118"
              fill="none" stroke="url(#gg)" stroke-width="13"
              stroke-linecap="round" stroke-dasharray="308"
              stroke-dashoffset="308" data-ratio="__RATIO__"
              style="transition:stroke-dashoffset 1s ease"/>
          </svg>
          <div class="gaugeCenter">
            <div class="n" id="gaugeNum" data-num="__SCORE_PERCENT__">0</div>
            <div class="d">RISK-ON SCORE / 100</div>
          </div>
        </div>
        <div class="regimeName">__REGIME__</div>
        <div class="regimeNote">比較可能な項目だけを集計した参考判定</div>
        <ul class="reasons">__REASONS__</ul>
      </div>

      <div class="panel stressPanel">
        <div class="panel-h">ストレス要約</div>
        <p class="stressText">__STRESS__</p>
        <div class="panel-h">リスク指標</div>
        __RISK_METRICS__
        <p class="note">
          債券ボラは米10年債利回りの日次変化の20観測標準偏差です。
          MOVE指数ではありません。
        </p>
      </div>
    </section>

    <section class="col center">
      <div class="panel chartPanel">
        <div class="tabsRow">
          <div class="tabs" role="tablist" aria-label="チャート種別">
            __TABS__
          </div>
          <div class="tools">
            <div class="periods" aria-label="チャート表示期間">
              __PERIOD_BUTTONS__
            </div>
            <button class="autobtn on" id="autoBtn" type="button"
              title="9秒ごとにタブを切り替えます" aria-pressed="true">
              AUTO
            </button>
          </div>
        </div>
        <div class="chartCaption" id="chartCaption"></div>
        <div class="chartShell" id="chartShell">
          __CHARTS__
          <div class="chartError" id="chartError" role="alert" hidden></div>
        </div>
        <p class="note">
          ← →：タブ切り替え ／ F：全画面 ／
          マネーフローは1ヶ月固定・マクロ源泉の一部は3ヶ月比較。
          その他の期間ボタンは表示範囲のみ変更します。
        </p>
      </div>

      <div class="panel heatPanel">
        <div class="panel-h">
          期間別パフォーマンス
          <span class="r">単位 %／各系列の直近観測日を基準</span>
        </div>
        __HEAT__
        <p class="note">
          M2・MMF・ステーブルコインは残高・時価総額の増減率です。
          金額の増減と実際の投資資金移動は同義ではありません。
        </p>
      </div>
    </section>

    <section class="col right">
      <div class="panel metrics">
        <div class="panel-h">
          指標スナップショット
          <span class="r">1ヶ月比／日付は最終観測日</span>
        </div>
        <div class="mlist">__SNAPSHOT__</div>
      </div>

      <div class="panel">
        <div class="panel-h">
          データ取得状況
          <span class="r">__STATUS_SUMMARY__</span>
        </div>
        <div class="sgrid">__STATUS__</div>
      </div>
    </section>
  </main>

  <footer>
    <div>Yahoo Finance ／ FRED ／ DefiLlama __DEMO_FOOTER__</div>
    <div>
      <b>フロー図は概念モデル・ドーナツは正のモメンタム強度比。</b>
      実測資金フロー・実際の資産配分・投資助言ではありません。
    </div>
  </footer>
</div>

__PLOTLY_TAG__

<script>
"use strict";

const DASH = __PAYLOAD__;
const FONT = "'Hiragino Kaku Gothic ProN','Hiragino Sans',"
  + "'Yu Gothic UI','Meiryo','Noto Sans JP',sans-serif";

const state = {
  tab: 0,
  period: "ALL",
  auto: true,
  hover: false,
  rendered: {},
  jobs: {},
  switchToken: 0
};

const CONFIG = {
  displayModeBar: false,
  responsive: false,
  scrollZoom: true,
  doubleClick: "reset"
};

const autoBtn = document.getElementById("autoBtn");
const chartShell = document.getElementById("chartShell");
const chartError = document.getElementById("chartError");

function nextPaint() {
  return new Promise(function(resolve) {
    requestAnimationFrame(function() {
      requestAnimationFrame(resolve);
    });
  });
}

function hexToRgba(hex, alpha) {
  const clean = String(hex).replace("#", "");

  if (!/^[0-9a-fA-F]{6}$/.test(clean)) {
    return "rgba(56,189,248," + alpha + ")";
  }

  const r = parseInt(clean.slice(0, 2), 16);
  const g = parseInt(clean.slice(2, 4), 16);
  const b = parseInt(clean.slice(4, 6), 16);

  return "rgba(" + r + "," + g + "," + b + "," + alpha + ")";
}

function dimensions(el) {
  return {
    width: Math.max(1, el.clientWidth),
    height: Math.max(1, el.clientHeight)
  };
}

function baseLayout(el, title) {
  const size = dimensions(el);

  return {
    autosize: true,
    width: size.width,
    height: size.height,
    paper_bgcolor: "rgba(0,0,0,0)",
    plot_bgcolor: "rgba(0,0,0,0)",
    font: {
      family: FONT,
      size: 10,
      color: "#a8b7cd"
    },
    margin: {l: 12, r: 12, t: title ? 34 : 18, b: 20},
    title: title ? {
      text: title,
      x: 0.03,
      xanchor: "left",
      font: {family: FONT, size: 11, color: "#a8b7cd"}
    } : undefined,
    hoverlabel: {
      bgcolor: "#09101c",
      bordercolor: "rgba(94,234,212,.4)",
      font: {family: FONT, size: 11, color: "#e9eefa"}
    }
  };
}

function emptyLayout(el, title, message) {
  const layout = baseLayout(el, title);

  layout.xaxis = {visible: false};
  layout.yaxis = {visible: false};
  layout.annotations = [{
    text: message,
    x: 0.5,
    y: 0.5,
    xref: "paper",
    yref: "paper",
    showarrow: false,
    font: {family: FONT, size: 12, color: "#6f819a"}
  }];

  return layout;
}

function tracesFor(group) {
  return (group.traces || []).map(function(trace) {
    const output = {
      type: "scatter",
      mode: trace.markers ? "lines+markers" : "lines",
      name: trace.name,
      x: trace.dates,
      y: trace.values,
      yaxis: trace.axis || "y",
      line: {
        color: trace.color,
        width: trace.width || 2,
        dash: trace.dash || "solid"
      },
      hovertemplate:
        "%{y:,.2f}" + (trace.unit || "") + "<extra>%{fullData.name}</extra>"
    };

    if (trace.markers) {
      output.marker = {size: 4, color: trace.color};
    }

    if (trace.fill) {
      output.fill = "tozeroy";
      output.fillcolor = "rgba(52,211,153,.10)";
    }

    return output;
  });
}

function layoutFor(group, el) {
  if (!(group.traces || []).length) {
    return emptyLayout(
      el,
      "",
      "このタブのデータを取得できませんでした"
    );
  }

  const layout = baseLayout(el, "");

  layout.margin = {
    l: 48,
    r: group.y2 ? 54 : 20,
    t: 76,
    b: 42
  };
  layout.hovermode = "x unified";
  layout.dragmode = "pan";
  layout.showlegend = true;
  layout.legend = {
    orientation: "h",
    x: 0,
    y: 1.02,
    xanchor: "left",
    yanchor: "bottom",
    font: {size: 9, color: "#a8b7cd"},
    bgcolor: "rgba(0,0,0,0)"
  };

  layout.xaxis = {
    type: "date",
    gridcolor: "rgba(148,163,184,.09)",
    zeroline: false,
    automargin: true,
    tickfont: {size: 9, color: "#6f819a"},
    showspikes: true,
    spikemode: "across",
    spikethickness: 1,
    spikedash: "dot",
    spikecolor: "rgba(94,234,212,.45)"
  };

  layout.yaxis = {
    gridcolor: "rgba(148,163,184,.09)",
    zeroline: false,
    automargin: true,
    tickfont: {size: 9, color: "#6f819a"},
    title: {
      text: group.y1 || "",
      font: {size: 10, color: "#6f819a"}
    }
  };

  if (group.y2) {
    layout.yaxis2 = {
      overlaying: "y",
      side: "right",
      showgrid: false,
      zeroline: false,
      automargin: true,
      tickfont: {size: 9, color: "#6f819a"},
      title: {
        text: group.y2,
        font: {size: 10, color: "#6f819a"}
      }
    };
  }

  if (group.hline !== null && group.hline !== undefined) {
    layout.shapes = [{
      type: "line",
      xref: "paper",
      x0: 0,
      x1: 1,
      y0: group.hline,
      y1: group.hline,
      line: {
        color: "rgba(148,163,184,.35)",
        width: 1,
        dash: "dot"
      }
    }];
  }

  return layout;
}

async function renderChart(i) {
  const group = DASH.groups[i];
  const el = document.getElementById("chart" + i);

  await Plotly.newPlot(
    el,
    tracesFor(group),
    layoutFor(group, el),
    CONFIG
  );
}

async function renderFlowWorld(i) {
  const flow = DASH.flow || {};
  const tasks = [];

  const mapEl = document.getElementById("fwMap" + i);
  const timeEl = document.getElementById("fwTime" + i);
  const wheelEl = document.getElementById("fwWheel" + i);

  // ---------- サンキー図 ----------
  if (mapEl) {
    const links = (flow.links || []).filter(function(link) {
      return Number.isFinite(link.value) && link.value > 0;
    });

    if (!links.length) {
      tasks.push(
        Plotly.newPlot(
          mapEl,
          [],
          emptyLayout(
            mapEl,
            "マクロ源泉 → 資産クラス → 地域（概念モデル）",
            "正の源泉条件と正の資産モメンタムの組み合わせがありません"
          ),
          CONFIG
        )
      );
    } else {
      const colors = flow.node_colors || [];
      const layout = baseLayout(
        mapEl,
        "マクロ源泉 → 資産クラス → 地域（概念モデル）"
      );
      layout.margin = {l: 18, r: 18, t: 40, b: 18};
      layout.font.size = 10;

      const data = [{
        type: "sankey",
        orientation: "h",
        arrangement: "snap",
        node: {
          pad: 12,
          thickness: 15,
          label: flow.nodes || [],
          color: colors,
          line: {
            color: "rgba(148,163,184,.25)",
            width: 0.5
          },
          hovertemplate:
            "%{label}<br>モデル強度: %{value:.3f}<extra></extra>"
        },
        link: {
          source: links.map(function(link) { return link.source; }),
          target: links.map(function(link) { return link.target; }),
          value: links.map(function(link) { return link.value; }),
          color: links.map(function(link) {
            return hexToRgba(colors[link.source] || "#38bdf8", 0.3);
          }),
          hovertemplate:
            "%{source.label} → %{target.label}"
            + "<br>モデル強度: %{value:.3f}<extra></extra>"
        }
      }];

      tasks.push(Plotly.newPlot(mapEl, data, layout, CONFIG));
    }
  }

  // ---------- 地域×資産クラスのヒートマップ ----------
  if (timeEl) {
    const rows = DASH.flow_heatmap || [];
    const regions = DASH.flow_regions || [];
    const classes = DASH.flow_classes || [];
    const lookup = new Map();

    rows.forEach(function(row) {
      lookup.set(row.region + "\u0000" + row.class, row);
    });

    const z = [];
    const customdata = [];
    let hasData = false;

    regions.forEach(function(region) {
      const zrow = [];
      const customrow = [];

      classes.forEach(function(assetClass) {
        const cell = lookup.get(region + "\u0000" + assetClass);
        const score = cell ? cell.score : null;

        zrow.push(score);

        if (score !== null && score !== undefined) {
          hasData = true;
        }

        customrow.push([
          cell && cell.return_1m !== null
            ? cell.return_1m.toFixed(2) + "%"
            : "—",
          cell ? cell.count : 0
        ]);
      });

      z.push(zrow);
      customdata.push(customrow);
    });

    if (!hasData) {
      tasks.push(
        Plotly.newPlot(
          timeEl,
          [],
          emptyLayout(
            timeEl,
            "地域×資産クラス／1ヶ月モメンタム",
            "比較可能なデータがありません"
          ),
          CONFIG
        )
      );
    } else {
      const layout = baseLayout(
        timeEl,
        "地域×資産クラス／1ヶ月モメンタム"
      );
      layout.margin = {l: 66, r: 12, t: 38, b: 42};
      layout.xaxis = {
        showgrid: false,
        automargin: true,
        tickfont: {size: 9}
      };
      layout.yaxis = {
        showgrid: false,
        autorange: "reversed",
        automargin: true,
        tickfont: {size: 9}
      };

      const data = [{
        type: "heatmap",
        x: classes,
        y: regions,
        z: z,
        customdata: customdata,
        zmin: -1,
        zmax: 1,
        zmid: 0,
        colorscale: [
          [0, "#be123c"],
          [0.5, "#182438"],
          [1, "#10b981"]
        ],
        showscale: false,
        hoverongaps: false,
        xgap: 2,
        ygap: 2,
        hovertemplate:
          "%{y} × %{x}"
          + "<br>平均スコア: %{z:.3f}"
          + "<br>平均1ヶ月リターン: %{customdata[0]}"
          + "<br>対象系列数: %{customdata[1]}"
          + "<extra></extra>"
      }];

      tasks.push(Plotly.newPlot(timeEl, data, layout, CONFIG));
    }
  }

  // ---------- 正のモメンタム強度比 ----------
  if (wheelEl) {
    const rotation = DASH.flow_rotation || {};
    const values = rotation.values || [];
    const total = values.reduce(function(sum, value) {
      return sum + value;
    }, 0);

    if (total <= 0) {
      tasks.push(
        Plotly.newPlot(
          wheelEl,
          [],
          emptyLayout(
            wheelEl,
            "正のモメンタム強度比",
            "正の平均モメンタムがありません"
          ),
          CONFIG
        )
      );
    } else {
      const layout = baseLayout(wheelEl, "正のモメンタム強度比");

      layout.margin = {l: 8, r: 8, t: 38, b: 52};
      layout.showlegend = true;
      layout.legend = {
        orientation: "h",
        x: 0.5,
        xanchor: "center",
        y: -0.03,
        yanchor: "top",
        font: {size: 9}
      };
      layout.annotations = [{
        text: "正の<br>強度比",
        x: 0.5,
        y: 0.5,
        xref: "paper",
        yref: "paper",
        showarrow: false,
        font: {family: FONT, size: 11, color: "#a8b7cd"}
      }];

      const data = [{
        type: "pie",
        labels: rotation.labels || [],
        values: values,
        hole: 0.58,
        sort: false,
        direction: "clockwise",
        marker: {
          colors: rotation.colors || [],
          line: {color: "#0b1220", width: 1}
        },
        textinfo: "percent",
        textposition: "inside",
        textfont: {family: FONT, size: 10, color: "#e9eefa"},
        hovertemplate:
          "%{label}<br>正の強度比: %{percent}"
          + "<br>平均正スコア: %{value:.3f}<extra></extra>"
      }];

      tasks.push(Plotly.newPlot(wheelEl, data, layout, CONFIG));
    }
  }

  await Promise.all(tasks);
}

/*
 * 同じタブへの重複newPlotを防ぐ。
 * 成功後にだけrenderedを設定する。
 */
function ensureRendered(i) {
  if (state.rendered[i]) {
    return Promise.resolve();
  }

  if (state.jobs[i]) {
    return state.jobs[i];
  }

  state.jobs[i] = (async function() {
    try {
      if (DASH.groups[i].is_flowworld) {
        await renderFlowWorld(i);
      } else {
        await renderChart(i);
      }

      state.rendered[i] = true;
    } finally {
      delete state.jobs[i];
    }
  })();

  return state.jobs[i];
}

async function applyPeriod(i) {
  const group = DASH.groups[i];
  const el = document.getElementById("chart" + i);

  if (
    !state.rendered[i]
    || group.is_flowworld
    || !(group.traces || []).length
    || i !== state.tab
    || el.hidden
  ) {
    return;
  }

  if (state.period === "ALL") {
    await Plotly.relayout(el, {"xaxis.autorange": true});
    return;
  }

  const days = {
    "1M": 30,
    "3M": 91,
    "6M": 182,
    "1Y": 365
  }[state.period];

  if (!days) {
    return;
  }

  // 最初の系列ではなく、全系列の最終日から表示範囲を決める。
  const lastDates = group.traces
    .filter(function(trace) { return trace.dates.length > 0; })
    .map(function(trace) {
      return trace.dates[trace.dates.length - 1];
    })
    .sort();

  if (!lastDates.length) {
    return;
  }

  const lastText = lastDates[lastDates.length - 1];
  const last = new Date(lastText + "T00:00:00Z");
  const start = new Date(last.getTime() - days * 86400000);

  await Plotly.relayout(el, {
    "xaxis.autorange": false,
    "xaxis.range": [
      start.toISOString().slice(0, 10),
      lastText
    ]
  });
}

function activePlotElements() {
  if (!state.rendered[state.tab]) {
    return [];
  }

  const group = DASH.groups[state.tab];

  if (group.is_flowworld) {
    return ["fwMap", "fwTime", "fwWheel"]
      .map(function(prefix) {
        return document.getElementById(prefix + state.tab);
      })
      .filter(Boolean);
  }

  const el = document.getElementById("chart" + state.tab);
  return el ? [el] : [];
}

/*
 * 表示中、描画済み、サイズが正の要素だけをリサイズする。
 * flowWorldPlotの外側divはPlotlyグラフではないため対象にしない。
 */
async function resizeActive() {
  if (!window.Plotly) {
    return;
  }

  const tasks = activePlotElements().map(async function(el) {
    if (
      !el._fullLayout
      || el.getClientRects().length === 0
      || el.clientWidth <= 0
      || el.clientHeight <= 0
    ) {
      return;
    }

    const size = dimensions(el);

    if (
      Math.abs(el._fullLayout.width - size.width) > 1
      || Math.abs(el._fullLayout.height - size.height) > 1
    ) {
      await Plotly.relayout(el, {
        width: size.width,
        height: size.height
      });
    }

    // newPlot完了・タブ表示後にPlotly内部のサイズも更新する。
    if (
      el.getClientRects().length > 0
      && el.clientWidth > 0
      && el.clientHeight > 0
    ) {
      await Plotly.Plots.resize(el);
    }
  });

  await Promise.all(tasks);
}

function displayError(message) {
  chartError.textContent = message;
  chartError.hidden = false;
}

async function showTab(index) {
  const count = DASH.groups.length;

  if (!count) {
    return;
  }

  const i = ((index % count) + count) % count;
  const token = ++state.switchToken;
  state.tab = i;

  document.querySelectorAll(".tab").forEach(function(button) {
    const active = Number(button.dataset.i) === i;

    button.classList.toggle("active", active);
    button.setAttribute("aria-selected", active ? "true" : "false");
    button.tabIndex = active ? 0 : -1;
  });

  // DOM順序ではなくIDで1対1対応させる。
  DASH.groups.forEach(function(group, k) {
    const el = document.getElementById("chart" + k);

    if (el) {
      el.hidden = k !== i;
    }
  });

  const group = DASH.groups[i];

  document.getElementById("chartCaption").textContent = group.caption || "";

  document.querySelectorAll(".pbtn").forEach(function(button) {
    button.disabled = Boolean(group.is_flowworld);
    button.title = group.is_flowworld
      ? "マネーフローは1ヶ月固定です"
      : "表示する日付範囲を変更します";
  });

  chartError.hidden = true;

  if (!window.Plotly) {
    displayError(
      "Plotly.jsを読み込めませんでした。"
      + "CDN版ではネットワーク接続を確認してください。"
      + "オフライン閲覧には --inline-plotlyjs を指定してください。"
    );
    return;
  }

  try {
    // hidden解除後、レイアウト確定を待ってからnewPlotする。
    await nextPaint();

    if (token !== state.switchToken) {
      return;
    }

    const outer = document.getElementById("chart" + i);

    if (!outer || outer.clientWidth <= 0 || outer.clientHeight <= 0) {
      throw new Error("描画領域の幅または高さが0です");
    }

    await ensureRendered(i);

    if (token !== state.switchToken) {
      return;
    }

    await applyPeriod(i);
    await nextPaint();

    if (token !== state.switchToken) {
      return;
    }

    await resizeActive();

  } catch (error) {
    console.error("Dashboard rendering failed:", error);

    if (token === state.switchToken) {
      displayError(
        "チャート描画に失敗しました: "
        + (error && error.message ? error.message : String(error))
      );
    }
  }
}

function setAuto(enabled) {
  state.auto = Boolean(enabled);
  autoBtn.classList.toggle("on", state.auto);
  autoBtn.textContent = state.auto ? "AUTO" : "MANUAL";
  autoBtn.setAttribute("aria-pressed", state.auto ? "true" : "false");
}

document.querySelectorAll(".tab").forEach(function(button) {
  button.addEventListener("click", function() {
    setAuto(false);
    void showTab(Number(button.dataset.i));
  });
});

document.querySelectorAll(".pbtn").forEach(function(button) {
  button.addEventListener("click", async function() {
    state.period = button.dataset.p;

    document.querySelectorAll(".pbtn").forEach(function(other) {
      other.classList.toggle("active", other === button);
    });

    try {
      await applyPeriod(state.tab);
    } catch (error) {
      console.error("Period update failed:", error);
    }
  });
});

autoBtn.addEventListener("click", function() {
  setAuto(!state.auto);
});

const chartPanel = document.querySelector(".chartPanel");

chartPanel.addEventListener("mouseenter", function() {
  state.hover = true;
});
chartPanel.addEventListener("mouseleave", function() {
  state.hover = false;
});

setInterval(function() {
  if (
    !state.auto
    || state.hover
    || document.hidden
    || !window.Plotly
  ) {
    return;
  }

  void showTab(state.tab + 1);
}, 9000);

async function toggleFullscreen() {
  try {
    if (!document.fullscreenElement) {
      if (document.documentElement.requestFullscreen) {
        await document.documentElement.requestFullscreen();
      }
    } else if (document.exitFullscreen) {
      await document.exitFullscreen();
    }
  } catch (error) {
    console.warn("Fullscreen unavailable:", error);
  }
}

document.getElementById("fsBtn").addEventListener(
  "click",
  function() {
    void toggleFullscreen();
  }
);

document.addEventListener("keydown", function(event) {
  const target = event.target;

  if (
    target
    && (
      target.isContentEditable
      || /^(INPUT|TEXTAREA|SELECT)$/.test(target.tagName)
    )
  ) {
    return;
  }

  if (event.altKey || event.ctrlKey || event.metaKey) {
    return;
  }

  if (event.key === "ArrowRight" || event.key === "ArrowLeft") {
    event.preventDefault();
    setAuto(false);

    void showTab(
      state.tab + (event.key === "ArrowRight" ? 1 : -1)
    );
  } else if (event.key === "f" || event.key === "F") {
    event.preventDefault();
    void toggleFullscreen();
  }
});

let resizeTimer = null;

function scheduleResize() {
  clearTimeout(resizeTimer);

  resizeTimer = setTimeout(async function() {
    try {
      await nextPaint();
      await resizeActive();
    } catch (error) {
      console.warn("Chart resize failed:", error);
    }
  }, 100);
}

window.addEventListener("resize", scheduleResize);
document.addEventListener("fullscreenchange", scheduleResize);

if ("ResizeObserver" in window) {
  // 親の描画領域だけを監視し、Plotly内部DOMの更新ループを避ける。
  const observer = new ResizeObserver(scheduleResize);
  observer.observe(chartShell);
}

// ---------- ゲージ ----------
const gaugeNum = document.getElementById("gaugeNum");
const gaugeArc = document.getElementById("gaugeArc");
const reducedMotion = window.matchMedia
  && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

if (gaugeNum) {
  const target = Number(gaugeNum.dataset.num || 0);

  if (reducedMotion) {
    gaugeNum.textContent = String(Math.round(target));
  } else {
    let started = null;

    function step(timestamp) {
      if (started === null) {
        started = timestamp;
      }

      const progress = Math.min((timestamp - started) / 820, 1);
      const eased = 1 - Math.pow(1 - progress, 3);

      gaugeNum.textContent = String(Math.round(target * eased));

      if (progress < 1) {
        requestAnimationFrame(step);
      }
    }

    requestAnimationFrame(step);
  }
}

if (gaugeArc) {
  const ratio = Math.max(
    0,
    Math.min(1, Number(gaugeArc.dataset.ratio || 0))
  );

  setTimeout(function() {
    gaugeArc.style.strokeDashoffset = String(308 * (1 - ratio));
  }, 80);
}

// スクリプトはbody末尾にあるため、ここではDOM構築済み。
void showTab(0);
</script>
</body>
</html>
"""


# =============================================================================
# K. ボード構築
# =============================================================================

def build_live_board(
    data: dict[str, pd.Series],
    analysis: dict,
    demo: bool = False,
) -> dict:
    flow = build_sankey_flow(data)
    status_summary, status_items = render_status_grid()

    total = analysis["total"]
    score = analysis["score"]
    ratio = score / total if total else 0.0

    return {
        "groups": build_chart_payload(data),
        "flow": flow,
        "flow_heatmap": build_flow_heatmap(data, flow),
        "flow_rotation": build_rotation_wheel(data, flow),
        "flow_regions": FLOW_REGIONS,
        "flow_classes": FLOW_CLASSES,
        "snapshot": render_snapshot(data),
        "heat": render_heat(analysis["table"]),
        "reasons": render_reasons(analysis["reasons"]),
        "ticker": render_ticker(data),
        "status": status_items,
        "status_summary": status_summary,
        "risk_metrics": render_risk_metrics(analysis["risk_metrics"]),
        "score": score,
        "total": total,
        "ratio": ratio,
        "score_percent": round(ratio * 100),
        "regime": _escape(analysis["regime_label"]),
        "stress": _escape(analysis["stress_text"]),
        "demo": demo,
    }


# =============================================================================
# L. HTML出力
# =============================================================================

def _safe_json(payload: dict) -> str:
    """
    JSONをscript内に安全に埋め込む。
    NaN/InfinityはJSONとして出力しない。
    """
    text = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    )

    return (
        text
        .replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def write_dashboard(
    board: dict,
    output_path: str = DEFAULT_OUTPUT,
    include_plotlyjs: str | bool = "cdn",
) -> str:
    output_dir = os.path.dirname(output_path) or "."
    os.makedirs(output_dir, exist_ok=True)

    now = datetime.now(JST)
    updated_at = now.strftime("%Y-%m-%d %H:%M JST")
    period = f"{START_DATE} 〜 {now.strftime('%Y-%m-%d')}"

    tabs = []
    charts = []

    for index, group in enumerate(board["groups"]):
        active = index == 0
        active_class = " active" if active else ""
        selected = "true" if active else "false"
        tab_index = "0" if active else "-1"
        hidden = "" if active else " hidden"

        tabs.append(
            f'<button class="tab{active_class}" '
            f'id="tab{index}" data-i="{index}" type="button" '
            f'role="tab" aria-controls="chart{index}" '
            f'aria-selected="{selected}" tabindex="{tab_index}">'
            f'{_escape(group["label"])}</button>'
        )

        if group["is_flowworld"]:
            charts.append(
                f'<div class="plot flowWorldPlot" '
                f'id="chart{index}" role="tabpanel" '
                f'aria-labelledby="tab{index}"{hidden}>'
                f'<div class="fwMap" id="fwMap{index}"></div>'
                '<div class="fwBottom">'
                f'<div class="fwTime" id="fwTime{index}"></div>'
                f'<div class="fwWheel" id="fwWheel{index}"></div>'
                '</div></div>'
            )
        else:
            charts.append(
                f'<div class="plot" id="chart{index}" '
                f'role="tabpanel" aria-labelledby="tab{index}"'
                f'{hidden}></div>'
            )

    period_buttons = []

    for key, label in [
        ("1M", "1M"),
        ("3M", "3M"),
        ("6M", "6M"),
        ("1Y", "1Y"),
        ("ALL", "全"),
    ]:
        active_class = " active" if key == "ALL" else ""

        period_buttons.append(
            f'<button class="pbtn{active_class}" '
            f'data-p="{key}" type="button">{label}</button>'
        )

    payload = _safe_json({
        "groups": board["groups"],
        "flow": board["flow"],
        "flow_heatmap": board["flow_heatmap"],
        "flow_rotation": board["flow_rotation"],
        "flow_regions": board["flow_regions"],
        "flow_classes": board["flow_classes"],
    })

    if include_plotlyjs == "cdn":
        version = get_plotlyjs_version()

        plotly_tag = (
            f'<script src="https://cdn.plot.ly/'
            f'plotly-{version}.min.js" charset="utf-8"></script>'
        )
    elif include_plotlyjs is True or include_plotlyjs == "inline":
        plotly_tag = "<script>" + get_plotlyjs() + "</script>"
    else:
        raise ValueError(
            "include_plotlyjs は 'cdn'、'inline'、True のいずれかです"
        )

    replacements = {
        "__UPDATED_AT__": _escape(updated_at),
        "__PERIOD__": _escape(period),
        "__TICKER__": board["ticker"],
        "__SCORE__": str(board["score"]),
        "__TOTAL__": str(board["total"]),
        "__RATIO__": str(round(board["ratio"], 6)),
        "__SCORE_PERCENT__": str(board["score_percent"]),
        "__REGIME__": board["regime"],
        "__REASONS__": board["reasons"],
        "__STRESS__": board["stress"],
        "__RISK_METRICS__": board["risk_metrics"],
        "__TABS__": "".join(tabs),
        "__PERIOD_BUTTONS__": "".join(period_buttons),
        "__CHARTS__": "".join(charts),
        "__HEAT__": board["heat"],
        "__SNAPSHOT__": board["snapshot"],
        "__STATUS_SUMMARY__": _escape(board["status_summary"]),
        "__STATUS__": board["status"],
        "__DEMO_BADGE__": (
            '<span class="demoBadge">DEMO</span>'
            if board["demo"]
            else ""
        ),
        "__DEMO_FOOTER__": (
            "／ デモ数値：実際の市場データではありません"
            if board["demo"]
            else ""
        ),
    }

    document = HTML_TEMPLATE

    for placeholder, value in replacements.items():
        document = document.replace(placeholder, value)

    # 大きなJS・JSONは最後に差し込む。
    document = document.replace("__PAYLOAD__", payload)
    document = document.replace("__PLOTLY_TAG__", plotly_tag)

    # 書き込み途中のファイルで既存HTMLを壊さないよう原子的に置換する。
    temporary_path = None

    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=output_dir,
            prefix=".dashboard-",
            suffix=".html",
            delete=False,
        ) as handle:
            temporary_path = handle.name
            handle.write(document)

        os.replace(temporary_path, output_path)
        temporary_path = None

    finally:
        if temporary_path and os.path.exists(temporary_path):
            os.remove(temporary_path)

    return output_path


def build_dashboard(
    output_path: str = DEFAULT_OUTPUT,
    demo: bool = False,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    include_plotlyjs: str | bool = "cdn",
) -> str:
    FETCH_STATUS.clear()
    configure_window(lookback_days)

    data = collect_data(demo=demo)
    analysis = build_analysis(data)
    board = build_live_board(data, analysis, demo=demo)

    path = write_dashboard(
        board,
        output_path=output_path,
        include_plotlyjs=include_plotlyjs,
    )

    size_kb = os.path.getsize(path) / 1024
    failures = sum(not item["ok"] for item in FETCH_STATUS)

    print(
        f"[DONE] {path} ({size_kb:,.0f} KB) "
        f"/ 取得失敗 {failures} 件"
    )
    print(
        f"[INFO] 地合い判定: {analysis['regime_label']} "
        f"({analysis['score']}/{analysis['total']})"
    )

    return path


# =============================================================================
# M. CLI
# =============================================================================

def parse_args(
    argv: Optional[list[str]] = None,
) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Global Money Flow ダッシュボード生成"
    )

    parser.add_argument(
        "--output",
        default=DEFAULT_OUTPUT,
        help=f"出力HTMLパス（既定: {DEFAULT_OUTPUT}）",
    )
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=DEFAULT_LOOKBACK_DAYS,
        help="取得期間の暦日数（30以上、既定: 730）",
    )
    parser.add_argument(
        "--demo",
        action="store_true",
        help="ダミーデータで生成。データ取得にネットワークを使わない",
    )
    parser.add_argument(
        "--inline-plotlyjs",
        action="store_true",
        help="Plotly.jsをHTMLに同梱し、オフライン閲覧可能にする",
    )

    args = parser.parse_args(argv)

    if args.lookback_days < 30:
        parser.error("--lookback-days は30以上を指定してください")

    return args


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)

    try:
        build_dashboard(
            output_path=args.output,
            demo=args.demo,
            lookback_days=args.lookback_days,
            include_plotlyjs=(
                "inline" if args.inline_plotlyjs else "cdn"
            ),
        )
    except Exception:
        traceback.print_exc()
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
