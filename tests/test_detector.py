import math

import pytest

from detector import rolling_change


def test_uses_five_minute_baseline_not_thirty_seconds():
    history = [(0, 100), (270, 109), (298, 108), (300, 110)]
    change, elapsed = rolling_change(history, 300, 300, 10, 300)
    assert change == pytest.approx(10)
    assert elapsed == 300


def test_negative_move_is_real_dump():
    change, elapsed = rolling_change([(0, 100), (300, 90)], 300, 300, 10, 300)
    assert change == pytest.approx(-10)
    assert elapsed == 300


def test_nearest_older_baseline_within_tolerance():
    history = [(0, 90), (292, 100), (294, 101), (296, 102), (298, 103), (600, 110)]
    change, elapsed = rolling_change(history, 600, 300, 10, 310)
    assert change == pytest.approx(100 * (110 / 103 - 1))
    assert elapsed == 302


def test_warm_up_never_borrows_short_window():
    assert rolling_change([(270, 100), (300, 120)], 300, 300, 10, 10) is None


def test_selects_nearest_baseline_inside_tolerance():
    change, elapsed = rolling_change(
        [(0, 99), (292, 100), (294, 101), (296, 102), (298, 103), (300, 110)], 300, 300, 10, 300
    )
    assert change == pytest.approx(100 * (110 / 99 - 1))
    assert elapsed == 300


@pytest.mark.parametrize(
    "history",
    [
        [],
        [(300, 100)],
        [(0, 100), (100, 101), (300, 120)],
        [(0, 100), (300, 0)],
        [(0, 100), (300, math.nan)],
        [(0, 100), (300, math.inf)],
        [(0, 100), (300, -1)],
        [(1, 100), (0, 100), (300, 120)],
        [(0, 100), (0, 101), (300, 120)],
        [(0, 100), (301, 120)],
    ],
)
def test_rejects_missing_invalid_or_discontinuous_samples(history):
    assert rolling_change(history, 300, 300, 10, 10) is None


def test_stale_baseline_rejected():
    assert rolling_change([(0, 100), (10, 110)], 330, 300, 10, 10) is None


def test_no_lookahead():
    assert rolling_change([(0, 100), (300, 110), (301, 150)], 300, 300, 10, 10) is None
