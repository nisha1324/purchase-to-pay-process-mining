"""Waiting times when some items haven't finished yet.

The log stops on one day, so an item still waiting then tells us only that its wait is longer than
what we saw. Dropping those items makes recent months look fast, because only their quickest items
have finished. The Kaplan-Meier estimate keeps them: each one counts as "still waiting" up to the
day it was last seen, then leaves the group.
"""
import numpy as np


def kaplan_meier(days, finished) -> tuple[np.ndarray, np.ndarray]:
    """Return each day something finished, and the share still waiting after that day.

    days: how long each item was watched. finished: True if it finished that day, False if it was
    still waiting. On a day with both, items finish first, which is the usual convention.
    """
    days = np.asarray(days, dtype=float)
    finished = np.asarray(finished, dtype=bool)
    if days.shape != finished.shape:
        raise ValueError("days and finished must be the same length")
    if np.isnan(days).any():
        raise ValueError("days has blanks")
    if len(days) == 0:
        raise ValueError("no items")
    values, inverse = np.unique(days, return_inverse=True)
    done = np.bincount(inverse, weights=finished, minlength=len(values))
    left = np.bincount(inverse, minlength=len(values))
    at_risk = left[::-1].cumsum()[::-1]
    survival = np.cumprod(1 - done / at_risk)
    keep = done > 0
    return values[keep], survival[keep]


def day_reached(days, finished, share: float) -> float | None:
    """The first day by which this share of items had finished, or None if the log never gets there."""
    if not 0 < share <= 1:
        raise ValueError("share must be above 0 and at most 1")
    times, survival = kaplan_meier(days, finished)
    reached = np.nonzero(1 - survival >= share - 1e-12)[0]
    return float(times[reached[0]]) if len(reached) else None
