"""Rolling-origin (walk-forward) folds. All indices are bar positions; `end` values are exclusive.

Each fold has a TRAIN window (used only to choose parameters) followed, after an optional embargo, by a TEST window
(never used for the choice). Test windows never overlap, and nothing at or beyond `dev_end` is ever touched, so the
protected final-test region stays untouched. Open positions are closed at every window end by the simulator, so train
and test results do not share risk.
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


@dataclass(frozen=True)
class Fold:
    index: int
    train_start: int
    train_end: int
    test_start: int
    test_end: int


def make_folds(dev_end: int, train_bars: int, test_bars: int, *, step: int | None = None, embargo: int = 0, anchored: bool = False, min_len: int = 20) -> list[Fold]:
    if min(train_bars, test_bars) < min_len or embargo < 0 or dev_end <= 0:
        raise ValueError(f"train and test windows must be >= {min_len}, embargo >= 0")
    step = step or test_bars
    if step < test_bars:
        raise ValueError("step must be >= test_bars so that test windows never overlap")
    folds, shift = [], 0
    while True:
        train_end = train_bars + shift
        test_start = train_end + embargo
        test_end = test_start + test_bars
        if test_end > dev_end:
            break
        folds.append(Fold(len(folds), 0 if anchored else shift, train_end, test_start, test_end))
        shift += step
    if not folds:
        raise ValueError(f"development data ({dev_end} bars) is too short for train={train_bars} + test={test_bars}")
    return folds


def development_end(bars: pd.DataFrame, frontier=None, protected_fraction: float = 0.2) -> int:
    """First bar index that a study may NOT use. With a consumed test window (frontier) everything up to it is development
    data and the fresh bars after it stay reserved; without one the last `protected_fraction` of the history is reserved."""
    if frontier is not None:
        return int((bars.index <= frontier).sum())
    return int(len(bars) * (1 - protected_fraction))
