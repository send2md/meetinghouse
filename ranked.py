"""The rate-the-choices Vote (instant-runoff counting), used to choose Stewards
and, later, how to split a group.

Each ballot rates some or all of the choices 1st, 2nd, 3rd and so on. If more
than half the ballots still counting rate the same choice 1st, it wins.
Otherwise the choice with the fewest 1st ratings is dropped, and each ballot
that rated it 1st counts for its next choice still in the running. A ballot
stops counting once all its choices are dropped.

To fill several seats at once (the first Steward Vote fills three), choices are
dropped the same way until only that many are left; they are then put in order
by how many ballots rate them first in that last round.

Ties are broken the same way every time, so anyone can check the count: drop
the choice with fewer 1st ratings in the first round; if still tied, drop the
one that came forward last (later in `choices`).
"""

from __future__ import annotations


def Count(ballots, choices, seats=1):
    """Count `ballots` (each a list of choices, most preferred first) among
    `choices` (in the order they came forward). Returns
    {"winners": [...], "rounds": [{"counts": {choice: n}, "dropped": choice or None, "counting": n}]}.
    """
    choices = list(choices)
    running = list(choices)
    ballots = [[c for c in ballot if c in choices] for ballot in ballots]
    first_round = None
    rounds = []
    while True:
        counts = {c: 0 for c in running}
        counting = 0
        for ballot in ballots:
            top = next((c for c in ballot if c in counts), None)
            if top is not None:
                counts[top] += 1
                counting += 1
        if first_round is None:
            first_round = dict(counts)
        leader = max(running, key=lambda c: (counts[c], -choices.index(c))) if running else None
        majority = seats == 1 and leader is not None and counting > 0 and counts[leader] * 2 > counting
        if majority or len(running) <= seats:
            rounds.append({"counts": counts, "dropped": None, "counting": counting})
            if majority:
                return {"winners": [leader], "rounds": rounds}
            ordered = sorted(running, key=lambda c: (-counts[c], -first_round.get(c, 0), choices.index(c)))
            return {"winners": ordered[:seats], "rounds": rounds}
        dropped = min(running, key=lambda c: (counts[c], first_round.get(c, 0), -choices.index(c)))
        rounds.append({"counts": counts, "dropped": dropped, "counting": counting})
        running.remove(dropped)
