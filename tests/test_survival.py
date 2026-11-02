"""The waiting-time estimate, on numbers small enough to work out by hand."""
import numpy as np
import pytest

from p2p.survival import day_reached, kaplan_meier


def test_hand_worked_example_with_one_unfinished_item():
    # Five items. On day 2 one finishes and one was last seen, so 4 are at risk that day.
    times, left = kaplan_meier([1, 2, 2, 3, 4], [True, True, False, True, True])
    assert times.tolist() == [1, 2, 3, 4]
    assert left == pytest.approx([0.8, 0.6, 0.3, 0.0])


def test_a_day_when_nothing_finished_is_not_listed():
    times, left = kaplan_meier([1, 2, 3], [True, False, True])
    assert times.tolist() == [1, 3]
    assert left == pytest.approx([2 / 3, 0.0])


def test_with_nothing_unfinished_it_is_the_plain_share():
    days = [5, 1, 3, 3, 9, 7]
    times, left = kaplan_meier(days, [True] * 6)
    plain = [np.mean(np.array(days) > day) for day in times]
    assert left == pytest.approx(plain)


def test_an_item_last_seen_on_a_day_still_counts_for_that_day():
    # Finishing comes first on a tie: 1 of 2 finishes on day 2, not 1 of 1.
    _, left = kaplan_meier([2, 2], [True, False])
    assert left == pytest.approx([0.5])


def test_order_of_items_does_not_matter():
    days, finished = [4, 1, 2, 3, 2], [True, True, False, True, True]
    order = [3, 0, 4, 1, 2]
    first = kaplan_meier(days, finished)
    second = kaplan_meier(np.array(days)[order], np.array(finished)[order])
    assert first[0].tolist() == second[0].tolist()
    assert first[1] == pytest.approx(second[1])


def test_unfinished_items_push_the_median_past_the_finished_only_one():
    # The plain median of finished items is 3. Two more items still waiting at 10 move it.
    days = [1, 3, 5, 10, 10]
    finished = [True, True, True, False, False]
    assert np.median([1, 3, 5]) == 3
    assert day_reached(days, finished, 0.5) == 5


def test_day_reached_is_none_when_the_log_never_gets_there():
    assert day_reached([1, 2, 3, 3], [True, False, False, False], 0.5) is None


def test_day_reached_exactly_half():
    assert day_reached([1, 2, 3, 4], [True] * 4, 0.5) == 2


def test_negative_gaps_are_allowed():
    # An invoice older than its PO is a real negative gap.
    assert day_reached([-3, 1, 2], [True, True, True], 0.3) == -3


def test_matches_a_plain_loop_on_random_data():
    generator = np.random.default_rng(7)
    days = generator.integers(0, 40, 400)
    finished = generator.random(400) < 0.7
    share, left, expected = 1.0, {}, []
    for day in sorted(set(days.tolist())):
        at_risk = int((days >= day).sum())
        done = int(((days == day) & finished).sum())
        if done:
            share *= 1 - done / at_risk
            left[day] = share
    times, survival = kaplan_meier(days, finished)
    assert times.tolist() == list(left)
    assert survival == pytest.approx(list(left.values()))


@pytest.mark.parametrize("days,finished,message", [
    ([1, 2], [True], "same length"),
    ([1, float("nan")], [True, True], "blanks"),
    ([], [], "no items"),
])
def test_bad_input_is_refused(days, finished, message):
    with pytest.raises(ValueError, match=message):
        kaplan_meier(days, finished)


@pytest.mark.parametrize("share", [0, -0.1, 1.5])
def test_a_share_outside_the_range_is_refused(share):
    with pytest.raises(ValueError):
        day_reached([1, 2], [True, True], share)
