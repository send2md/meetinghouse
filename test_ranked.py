"""The rate-the-choices count."""

from ranked import Count


def test_majority_first_round():
    result = Count([["a"], ["a", "b"], ["b"]], ["a", "b", "c"])
    assert result["winners"] == ["a"] and len(result["rounds"]) == 1


def test_lowest_is_dropped_and_ballots_move_on():
    ballots = [["a"], ["a"], ["b"], ["b"], ["c", "b"]]
    result = Count(ballots, ["a", "b", "c"])
    assert result["rounds"][0]["dropped"] == "c"
    assert result["winners"] == ["b"]


def test_exhausted_ballots_stop_counting():
    ballots = [["a"], ["a"], ["b"], ["c"], ["c"]]
    result = Count(ballots, ["a", "b", "c"])
    # b is dropped; its ballot has no next choice, so 4 ballots still count.
    assert result["rounds"][1]["counting"] == 4
    assert result["winners"] == ["a"]  # tie 2-2: a came forward first


def test_three_seats_in_order_of_support():
    ballots = [["a"], ["a"], ["a"], ["b"], ["b"], ["c"], ["d", "c"]]
    result = Count(ballots, ["a", "b", "c", "d"], seats=3)
    assert result["winners"] == ["a", "b", "c"]


def test_no_ballots_still_chooses_in_the_order_they_came_forward():
    assert Count([], ["a", "b", "c"])["winners"] == ["a"]
