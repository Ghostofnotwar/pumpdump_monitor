"""Pure rolling-window detector; timestamps are Unix seconds, prices are positive."""

import math
from collections.abc import Sequence


def rolling_change(
    samples: Sequence[tuple[float, float]],
    now: float,
    window_sec: float,
    tolerance_sec: float,
    max_gap_sec: float,
) -> tuple[float, float] | None:
    """Return (percent change, actual elapsed seconds), or None during warm-up/gaps.

    Use the sample nearest to, but not after, now-window. Never substitute a
    30-second move for a five-minute move. Reject stale or discontinuous data.
    """
    samples = tuple(samples)
    if window_sec <= 0 or tolerance_sec < 0 or max_gap_sec <= 0 or len(samples) < 2:
        return None
    if not math.isfinite(now) or any(
        not math.isfinite(ts) or not math.isfinite(price) or price <= 0 or ts > now for ts, price in samples
    ):
        return None
    if any(b[0] <= a[0] for a, b in zip(samples, samples[1:], strict=False)):
        return None
    cutoff = now - window_sec
    older = [sample for sample in samples if cutoff - tolerance_sec <= sample[0] <= cutoff]
    if not older:
        return None
    old_ts, old_price = max(older, key=lambda sample: sample[0])
    segment = [sample for sample in samples if old_ts <= sample[0] <= now]
    if any(b[0] - a[0] > max_gap_sec for a, b in zip(segment, segment[1:], strict=False)):
        return None
    return (samples[-1][1] / old_price - 1) * 100, now - old_ts
