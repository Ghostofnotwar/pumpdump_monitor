import asyncio
import os
import time
import uuid
from pathlib import Path

import matplotlib.pyplot as plt
import pytest
from PIL import Image

from chart_builder import build_snapshot, klines_to_df
from snapshots import cleanup_snapshots, snapshot_path


def test_klines_sorted_validated_and_no_fabricated_oi(klines):
    df = klines_to_df(list(reversed(klines)) + [["bad"], [1700000000000, "nan", 2, 1, 2, 3, 4]])
    assert len(df) == 80
    assert df.index.is_monotonic_increasing
    assert not any("OI" in column or "Liq" in column for column in df.columns)
    assert df["SMA9"].iloc[0] == df["Volume"].iloc[0]


def test_zero_range_candle_delta_is_zero():
    rows = [[1700000000000 + i * 60000, 100, 100, 100, 100, 10, 1000] for i in range(12)]
    assert klines_to_df(rows)["DeltaProxy"].eq(0).all()


@pytest.mark.parametrize("theme", ["dark", "light"])
async def test_parallel_real_png_render_and_cleanup(klines, snapshot_dir, theme):
    before = len(plt.get_fignums())
    settings = {"theme": theme, "show_sma": 1, "show_delta": 1, "show_heatmap": 1}
    paths = await asyncio.gather(
        *(
            asyncio.to_thread(
                build_snapshot,
                "BTCUSDT",
                klines,
                [],
                settings,
                {"direction": "PUMP", "change_percent": 6.2},
                snapshot_dir=snapshot_dir,
            )
            for _ in range(2)
        )
    )
    assert len(set(paths)) == 2
    for path in path_objects(paths):
        assert path.stat().st_size > 10000
        with Image.open(path) as image:
            assert image.size == (1440, 1200)
            image.verify()
    assert len(plt.get_fignums()) == before
    assert not list(snapshot_dir.glob("*.tmp"))


def path_objects(paths):
    return [Path(path) for path in paths]


def test_insufficient_data_returns_empty(klines, snapshot_dir):
    assert build_snapshot("BTCUSDT", klines[:9], [], {}, snapshot_dir=snapshot_dir) == ""
    assert not list(snapshot_dir.iterdir())


def test_failed_render_closes_figure_and_temp_file(klines, snapshot_dir, monkeypatch):
    import matplotlib.figure

    def fail(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(matplotlib.figure.Figure, "savefig", fail)
    before = len(plt.get_fignums())
    with pytest.raises(OSError):
        build_snapshot("BTCUSDT", klines, [], {}, snapshot_dir=snapshot_dir)
    assert len(plt.get_fignums()) == before
    assert not list(snapshot_dir.iterdir())


def test_snapshot_path_rejects_traversal_and_symlink(snapshot_dir):
    name = f"BTCUSDT_{uuid.uuid4().hex}.png"
    image = snapshot_dir / name
    image.write_bytes(b"test")
    assert snapshot_path(snapshot_dir, f"/old/path/{name}") == image
    assert snapshot_path(snapshot_dir, "../../etc/passwd") is None
    assert snapshot_path(snapshot_dir, "not-managed.png") is None
    link = snapshot_dir / f"ETHUSDT_{uuid.uuid4().hex}.png"
    link.symlink_to(image)
    assert snapshot_path(snapshot_dir, link.name) is None


def test_cleanup_only_managed_files_age_and_size(snapshot_dir):
    old = snapshot_dir / f"BTCUSDT_{uuid.uuid4().hex}.png"
    recent = snapshot_dir / f"ETHUSDT_{uuid.uuid4().hex}.png"
    unrelated = snapshot_dir / "keep.txt"
    for path in (old, recent, unrelated):
        path.write_bytes(b"x" * 1024)
    os.utime(old, (time.time() - 9 * 86400, time.time() - 9 * 86400))
    assert cleanup_snapshots(snapshot_dir, 7, 10) == 1
    assert not old.exists() and recent.exists() and unrelated.exists()
    assert cleanup_snapshots(snapshot_dir, 7, 0) == 1
    assert not recent.exists() and unrelated.exists()
