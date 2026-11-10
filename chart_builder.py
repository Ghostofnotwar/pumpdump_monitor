"""Candlestick snapshots from actual Bybit OHLCV; derived indicators are labeled.

Matplotlib is not thread-safe: callers use asyncio.to_thread and this module locks
the entire render. No real OI, liquidation or order-flow feed is fabricated.
"""

from __future__ import annotations

import math
import os
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path

import matplotlib
import matplotlib.colors
import matplotlib.dates

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import mplfinance as mpf
import numpy as np
import pandas as pd

_RENDER_LOCK = threading.Lock()


def human_format(num, pos=None):
    for threshold, suffix in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "k")):
        if abs(num) >= threshold:
            return f"{num / threshold:.1f}{suffix}"
    return f"{num:.4f}" if 0 < abs(num) < 1 else f"{num:g}"


def klines_to_df(klines: list) -> pd.DataFrame:
    columns = ["Date", "Open", "High", "Low", "Close", "Volume", "Turnover"]
    records = []
    for row in klines[-1000:]:
        try:
            if not isinstance(row, (list, tuple)) or len(row) < 6:
                continue
            stamp, *values = row[:7]
            values = [float(value) for value in values]
            if len(values) == 5:
                values.append(values[3] * values[4])
            ts, o, h, low, c, volume, turnover = [float(stamp), *values]
            if not all(math.isfinite(v) for v in (ts, o, h, low, c, volume, turnover)):
                continue
            if ts <= 0 or min(o, h, low, c) <= 0 or volume < 0 or turnover < 0:
                continue
            if low > min(o, c) or h < max(o, c) or low > h:
                continue
            records.append((ts, o, h, low, c, volume, turnover))
        except (TypeError, ValueError, OverflowError):
            continue
    df = pd.DataFrame(records, columns=columns)
    if df.empty:
        return pd.DataFrame()
    df["Date"] = pd.to_datetime(df["Date"], unit="ms", utc=True).dt.tz_localize(None)
    df = df.drop_duplicates("Date", keep="last").set_index("Date").sort_index()
    df["SMA9"] = df["Volume"].rolling(9, min_periods=1).mean()
    spread = df["High"] - df["Low"]
    clv = ((df["Close"] - df["Low"]) - (df["High"] - df["Close"])) / spread
    df["DeltaProxy"] = clv.replace([np.inf, -np.inf], 0).fillna(0) * df["Volume"]
    df["CVD_C"] = df["DeltaProxy"].cumsum()
    df["CVD_O"] = df["CVD_C"].shift(1).fillna(0)
    df["CVD_H"] = df[["CVD_O", "CVD_C"]].max(axis=1)
    df["CVD_L"] = df[["CVD_O", "CVD_C"]].min(axis=1)
    return df


def _align_indicator(df: pd.DataFrame, points: list, value_index: int = 1) -> pd.Series | None:
    """Interpolate a sampled time series onto candle closes without extrapolating."""
    if not points:
        return None
    clean = []
    for point in points:
        try:
            stamp, value = int(point[0]), float(point[value_index])
            if math.isfinite(value):
                clean.append((stamp, value))
        except (TypeError, ValueError, IndexError, OverflowError):
            continue
    if not clean:
        return None
    clean.sort()
    source_x = np.asarray([item[0] for item in clean], dtype=float)
    source_y = np.asarray([item[1] for item in clean], dtype=float)
    target_x = df.index.to_numpy(dtype="datetime64[ms]").astype(float)
    values = np.interp(target_x, source_x, source_y)
    values[(target_x < source_x[0]) | (target_x > source_x[-1])] = np.nan
    return pd.Series(values, index=df.index) if np.isfinite(values).any() else None


def _aggregate_bars(df: pd.DataFrame, points: list, value_index: int) -> pd.Series | None:
    """Sum liquidation notionals into chart candles; do not interpolate event amounts."""
    if not points:
        return None
    timestamps = df.index.to_numpy(dtype="datetime64[ms]").astype(float)
    values = np.full(len(df), np.nan)
    populated = np.zeros(len(df), dtype=bool)
    for point in points:
        try:
            stamp, amount = int(point[0]), float(point[value_index])
            if not math.isfinite(amount) or amount < 0 or stamp < timestamps[0] or stamp > timestamps[-1]:
                continue
            index = min(len(df) - 1, max(0, int(np.searchsorted(timestamps, stamp, side="right") - 1)))
            if not populated[index]:
                values[index] = 0.0
            values[index] += amount
            populated[index] = True
        except (TypeError, ValueError, IndexError, OverflowError):
            continue
    return pd.Series(values, index=df.index) if populated.any() else None


def build_snapshot(
    symbol: str,
    klines: list,
    trades: list,
    settings: dict,
    pumpdump_info: dict | None = None,
    *,
    snapshot_dir: Path,
    indicators: dict | None = None,
) -> str:
    """Synchronous CPU work. Returns a unique PNG path, or '' if insufficient data."""
    df = klines_to_df(klines)
    if len(df) < 10:
        return ""
    if not isinstance(symbol, str) or not symbol.replace("_", "").replace("-", "").isalnum():
        raise ValueError("Invalid symbol")
    dark = settings.get("theme", "dark") == "dark"
    bg, text = ("#0b0e11", "#c7ced8") if dark else ("#ffffff", "#333333")
    up, down = "#02c076", "#f84960"
    root = Path(snapshot_dir)
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{symbol.upper()}_{uuid.uuid4().hex}.png"
    temporary = path.with_suffix(".tmp")
    with _RENDER_LOCK:
        fig = None
        try:
            style = mpf.make_mpf_style(
                base_mpf_style="charles",
                marketcolors=mpf.make_marketcolors(up=up, down=down, inherit=True, volume="in", edge="inherit"),
                facecolor=bg,
                edgecolor="#596579",
                gridcolor="#252c36" if dark else "#e8ecf1",
                gridstyle=":",
                rc={"font.size": 9, "axes.labelcolor": text, "xtick.color": text, "ytick.color": text},
            )
            overlays, ratios, panel_labels = [], [4, 1], {}
            if settings.get("show_sma", 1):
                overlays.append(mpf.make_addplot(df["SMA9"], panel=1, color="#00a8cc", width=1))
            indicators = indicators or {}
            if settings.get("show_delta", 0):
                panel = len(ratios)
                cvd = _align_indicator(df, indicators.get("cvd", []))
                if cvd is not None:
                    overlays.append(mpf.make_addplot(cvd, panel=panel, color="#31c9a5", width=1.3))
                    panel_labels[panel] = f"CVD · {indicators.get('source', 'public trades')} (base qty)"
                else:
                    candles = df[["CVD_O", "CVD_H", "CVD_L", "CVD_C"]].rename(
                        columns={"CVD_O": "Open", "CVD_H": "High", "CVD_L": "Low", "CVD_C": "Close"}
                    )
                    candles["Volume"] = 0.0
                    overlays.append(mpf.make_addplot(candles, panel=panel, type="candle"))
                    panel_labels[panel] = "CVD proxy from OHLCV · estimated, not trade delta"
                ratios.append(1)
            if settings.get("show_oi", 0):
                oi = _align_indicator(df, indicators.get("oi", []))
                if oi is not None:
                    panel = len(ratios)
                    overlays.append(mpf.make_addplot(oi, panel=panel, color="#b38cff", width=1.3))
                    panel_labels[panel] = "Open interest · Binance contracts"
                    ratios.append(1)
            if settings.get("show_liq", 0):
                liquidation_rows = indicators.get("liquidations", [])
                long_liq = _aggregate_bars(df, liquidation_rows, 1)
                short_liq = _aggregate_bars(df, liquidation_rows, 2)
                if long_liq is not None and short_liq is not None:
                    panel = len(ratios)
                    overlays.extend(
                        [
                            mpf.make_addplot(long_liq, panel=panel, type="bar", color="#f84960", alpha=0.8),
                            mpf.make_addplot(short_liq, panel=panel, type="bar", color="#02c076", alpha=0.8),
                        ]
                    )
                    panel_labels[panel] = "Liquidations · USD notional (long red / short green)"
                    ratios.append(1)
            fig, axes = mpf.plot(
                df,
                type="candle",
                style=style,
                volume=True,
                addplot=overlays or None,
                figsize=(12, 8 if len(ratios) == 2 else 10 + 1.5 * max(0, len(ratios) - 3)),
                returnfig=True,
                panel_ratios=ratios,
                datetime_format="%m-%d %H:%M",
                tight_layout=False,
                warn_too_much_data=1001,
            )
            fig.subplots_adjust(left=0.05, right=0.86, top=0.86, bottom=0.12, hspace=0.25)
            main, volume_axis = axes[0], axes[2]
            for axis in axes[::2]:
                axis.yaxis.tick_right()
                axis.yaxis.set_label_position("right")
                axis.tick_params(axis="y", labelright=True, labelsize=8)
            volume_axis.yaxis.set_major_formatter(mticker.FuncFormatter(human_format))
            for panel, label in panel_labels.items():
                axis = axes[panel * 2]
                axis.yaxis.set_major_formatter(mticker.FuncFormatter(human_format))
                axis.text(0.01, 0.9, label, transform=axis.transAxes, color=text, fontsize=7, va="top")
            volume_axis.text(
                0.01,
                0.9,
                "Volume (base asset)" + (" + SMA 9" if settings.get("show_sma", 1) else ""),
                transform=volume_axis.transAxes,
                color=text,
                fontsize=8,
                va="top",
            )
            if settings.get("show_heatmap", 0):
                low, high = float(df["Low"].min()), float(df["High"].max())
                if high > low:
                    bins = np.linspace(low, high, 31)
                    histogram = np.zeros(30)
                    for candle in df.itertuples():
                        overlap = np.maximum(0, np.minimum(bins[1:], candle.High) - np.maximum(bins[:-1], candle.Low))
                        if overlap.sum() > 0:
                            histogram += overlap / overlap.sum() * candle.Volume
                    maximum = histogram.max()
                    if maximum > 0:
                        for index, amount in enumerate(histogram):
                            main.axhspan(
                                bins[index],
                                bins[index + 1],
                                xmin=0,
                                xmax=float(amount / maximum),
                                color="#f0b90b",
                                alpha=0.10,
                                zorder=0,
                                linewidth=0,
                            )
                main.text(
                    0.01,
                    0.02,
                    "Volume profile: uniform-in-candle estimate",
                    transform=main.transAxes,
                    color=text,
                    fontsize=7,
                )
            price = float(df["Close"].iloc[-1])
            direction = (pumpdump_info or {}).get("direction", "")
            color = down if direction == "DUMP" else up
            main.axhline(price, color=color, linestyle="--", linewidth=1, alpha=0.7)
            main.annotate(
                f" {price:.8g} ",
                xy=(1, price),
                xycoords=("axes fraction", "data"),
                xytext=(6, 0),
                textcoords="offset points",
                va="center",
                fontsize=10,
                bbox={"boxstyle": "round,pad=0.25", "fc": color, "ec": "none"},
                color="#07130e",
            )
            change = (pumpdump_info or {}).get("change_percent")
            title = symbol + (f"  {change:+.2f}%" if change is not None else "")
            main.text(0, 1.13, title, transform=main.transAxes, fontsize=18, fontweight="bold", color=text)
            window = (pumpdump_info or {}).get("window_sec", 300)
            detail = f"Move window: {window / 60:g} min" if change is not None else "Market snapshot"
            if direction == "TEST":
                detail = "TEST IMAGE — not a market signal"
            timeframe = str(settings.get("timeframe", "5"))
            label_tf = "1D" if timeframe == "D" else f"{timeframe}m"
            main.text(0, 1.04, f"{detail} | Chart TF: {label_tf}", transform=main.transAxes, fontsize=10, color=text)
            fig.text(
                0.05, 0.035, f"Bybit public OHLCV | {datetime.now(UTC):%Y-%m-%d %H:%M UTC}", color=text, fontsize=8
            )
            fig.text(
                0.05,
                0.015,
                "Optional Binance public derivatives feeds; unavailable panels omitted. Informational only, not financial advice.",
                color=text,
                fontsize=7,
            )
            fig.savefig(temporary, format="png", dpi=120, facecolor=bg)
            os.replace(temporary, path)
            return str(path)
        finally:
            if fig is not None:
                plt.close(fig)
            temporary.unlink(missing_ok=True)


def build_liquidation_heatmap(symbol: str, payload: dict, *, snapshot_dir: Path) -> str:
    """Render Coinglass sparse model intensities; colorbar deliberately has no currency unit."""
    candles, prices, points = payload.get("candles"), payload.get("y_axis"), payload.get("points")
    if not candles or not prices or not points:
        return ""
    matrix = np.zeros((len(prices), len(candles)), dtype=float)
    for candle_index, price_index, intensity in points:
        try:
            if 0 <= int(candle_index) < len(candles) and 0 <= int(price_index) < len(prices):
                value = float(intensity)
                if math.isfinite(value) and value >= 0:
                    matrix[int(price_index), int(candle_index)] += value
        except (TypeError, ValueError, OverflowError):
            continue
    if matrix.max() <= 0:
        return ""
    if not isinstance(symbol, str) or not symbol.replace("_", "").replace("-", "").isalnum():
        raise ValueError("Invalid symbol")
    root = Path(snapshot_dir)
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{symbol.upper()}_HEATMAP_{uuid.uuid4().hex}.png"
    temporary = path.with_suffix(".tmp")
    with _RENDER_LOCK:
        fig = None
        try:
            bg, text = "#0b0e11", "#c7ced8"
            fig, axis = plt.subplots(figsize=(12, 7), facecolor=bg)
            axis.set_facecolor(bg)
            dates = [datetime.fromtimestamp(stamp / 1000, UTC).replace(tzinfo=None) for stamp, _ in candles]
            x = matplotlib.dates.date2num(dates)
            y = np.asarray(prices, dtype=float)
            axis.imshow(
                matrix,
                origin="lower",
                aspect="auto",
                interpolation="nearest",
                extent=(x[0], x[-1] if x[-1] > x[0] else x[0] + 1 / 1440, y[0], y[-1]),
                cmap="inferno",
                norm=matplotlib.colors.PowerNorm(gamma=0.55, vmin=0, vmax=float(matrix.max())),
            )
            axis.plot(dates, [close for _, close in candles], color="#d5d9df", linewidth=0.9, alpha=0.9, label="Price")
            axis.set_title(
                f"{symbol.upper()} · Liquidation leverage heatmap", color=text, loc="left", fontsize=15, pad=14
            )
            axis.set_xlabel("Time (UTC)", color=text)
            axis.set_ylabel("Price", color=text)
            axis.xaxis.set_major_formatter(matplotlib.dates.DateFormatter("%m-%d %H:%M", tz=UTC))
            axis.tick_params(colors=text, labelsize=8)
            for spine in axis.spines.values():
                spine.set_color("#596579")
            colorbar = fig.colorbar(axis.images[0], ax=axis, pad=0.015)
            colorbar.set_label("Model intensity (not USD)", color=text)
            colorbar.ax.tick_params(colors=text, labelsize=8)
            axis.legend(loc="upper left", framealpha=0.7, facecolor="#111720", labelcolor=text)
            fig.text(
                0.02,
                0.015,
                f"Coinglass API v4 · model output · {datetime.now(UTC):%Y-%m-%d %H:%M UTC}",
                color=text,
                fontsize=8,
            )
            fig.tight_layout(rect=(0, 0.035, 1, 1))
            fig.savefig(temporary, format="png", dpi=120, facecolor=bg)
            os.replace(temporary, path)
            return str(path)
        finally:
            if fig is not None:
                plt.close(fig)
            temporary.unlink(missing_ok=True)
