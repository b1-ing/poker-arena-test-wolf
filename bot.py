"""Team wolf: tight, position-aware, value-oriented heads-up bot.

Preflop: a Chen-score chart. The button (small blind) opens wide-ish, the big
blind defends tighter, and we only continue against 3-bets/4-bets with premium
hands. Postflop: classify the hand into levels (monster / strong / medium /
draw / air), bet big for value, give up medium hands to large bets, and bluff
rarely (an occasional flop continuation bet).
"""

from __future__ import annotations

import random

from player_sdk.bot_interface import PokerBot
from player_sdk.models import Action, GameState

RANKS = "23456789TJQKA"


def _r(card: str) -> int:
    return RANKS.index(card[0]) + 2


def chen_score(hole) -> float:
    """Chen formula: a classic quick preflop hand strength (AA = 20)."""
    r1, r2 = sorted((_r(hole[0]), _r(hole[1])), reverse=True)

    def pts(r: int) -> float:
        table = {14: 10.0, 13: 8.0, 12: 7.0, 11: 6.0}
        return table.get(r, r / 2.0)

    score = pts(r1)
    if r1 == r2:
        return float(max(5.0, score * 2))
    if hole[0][1] == hole[1][1]:
        score += 2
    gap = r1 - r2 - 1
    score -= {0: 0, 1: 1, 2: 2, 3: 4}.get(gap, 5)
    if gap <= 1 and r1 < 12:
        score += 1
    return float(score)


def _straight_high(rank_set) -> int:
    """Highest card of a straight in the rank set, or 0."""
    s = set(rank_set)
    if 14 in s:
        s.add(1)
    for high in range(14, 4, -1):
        if all((high - k) in s for k in range(5)):
            return high
    return 0


def classify(hole, board) -> tuple[int, str]:
    """Return (level, note). Levels: 5 monster, 4 strong, 3 medium, 2 draw, 1 air."""
    hranks = [_r(c) for c in hole]
    branks = [_r(c) for c in board]
    allr = hranks + branks
    counts = {v: allr.count(v) for v in set(allr)}
    bcounts = {v: branks.count(v) for v in set(branks)}
    suits_all = [c[1] for c in list(hole) + list(board)]
    river = len(board) >= 5

    # Flush using at least one hole card.
    for s in set(suits_all):
        if suits_all.count(s) >= 5 and any(c[1] == s for c in hole):
            return 5, "flush"
    # Straight that uses a hole card.
    sh = _straight_high(allr)
    if sh and sh != _straight_high(branks):
        return 5, "straight"

    mine = [v for v in hranks if counts[v] >= 2]
    # Quads / trips / full house involving our cards.
    if any(counts[v] >= 3 for v in mine):
        return 5, "trips+"
    pair_vals = sorted((v for v in counts if counts[v] == 2), reverse=True)
    mypairs = [v for v in pair_vals if v in hranks]
    if len(mypairs) >= 2:
        return 5, "two pair"
    if len(pair_vals) >= 2 and mypairs:
        # one of our pair + board pair
        return 4, "pair+boardpair"
    top = max(branks)
    if mypairs:
        p = mypairs[0]
        pocket = hranks[0] == hranks[1]
        if pocket and p > top:
            return 4, "overpair"
        if p == top and not pocket:
            kicker = max(v for v in hranks if v != p)
            return (4 if kicker >= 10 else 3), "top pair"
        if pocket:
            return 3, "underpair"
        return 3, "weak pair"

    # Draws (only meaningful before the river).
    if not river:
        for s in set(suits_all):
            if suits_all.count(s) == 4 and any(c[1] == s for c in hole):
                return 2, "flush draw"
        uniq = set(allr)
        if 14 in uniq:
            uniq.add(1)
        for low in range(2, 11):  # four in a row, open at both ends
            run = set(range(low, low + 4))
            if run <= uniq and (set(hranks) & run) and (low - 1) >= 1 and (low + 4) <= 14:
                return 2, "oesd"
    return 1, "air"


class WolfBot(PokerBot):
    def __init__(self, *args, **kwargs) -> None:
        try:
            super().__init__(*args, **kwargs)
        except TypeError:
            super().__init__()
        self.rng = random.Random(1234)
        self.bets = 0    # postflop bets we faced
        self.checks = 0  # postflop checks by opponent (seen when we hold the button)

    # ---------------------------------------------------------------- helpers
    @staticmethod
    def _bounds(gs: GameState):
        lo = gs.min_raise_to or 0
        hi = gs.max_raise_to or lo
        return lo, hi

    def _raise(self, gs: GameState, target: float) -> Action:
        lo, hi = self._bounds(gs)
        if hi <= 0 or not (gs.can_raise or gs.can_bet):
            return self._passive(gs)
        stack_total = (gs.my_stack or 0) + (gs.my_bet or 0)
        t = int(target)
        if stack_total and t >= 0.55 * stack_total:
            t = hi  # committed anyway: jam
        t = max(min(t, hi), min(lo, hi))
        return Action.raise_to(t)

    @staticmethod
    def _passive(gs: GameState) -> Action:
        return Action.call() if gs.call_amount == 0 else Action.fold()

    def _frac_bet(self, gs: GameState, frac: float) -> Action:
        pot = gs.pot_size
        opp_total = (gs.my_bet or 0) + gs.call_amount
        return self._raise(gs, opp_total + frac * (pot + gs.call_amount))

    def _was_preflop_aggressor(self, gs: GameState) -> bool:
        pid = gs.my_player_id
        last = None
        for rec in gs.action_history:
            if rec.street not in (None, "preflop"):
                continue
            if rec.action in ("raise", "raise_to", "bet"):
                last = rec.player_id
        return pid is not None and last == pid

    # ---------------------------------------------------------------- act
    def act(self, game_state: GameState) -> Action:
        try:
            if game_state.community_cards:
                return self._postflop(game_state)
            return self._preflop(game_state)
        except Exception:
            return self._passive(game_state)

    # ---------------------------------------------------------------- preflop
    def _preflop(self, gs: GameState) -> Action:
        bb = gs.big_blind or 20
        score = chen_score(gs.hole_cards)
        my_bet = gs.my_bet or 0
        opp_total = my_bet + gs.call_amount
        to_call = gs.call_amount
        button = gs.is_dealer

        if opp_total <= bb:
            # Unopened pot (SB to act) or a limp (BB to act).
            if button:
                if score >= 7:
                    return self._raise(gs, 3 * bb)
                if score >= 3.5:
                    return self._raise(gs, 2.5 * bb)
                if score >= 2 and to_call <= bb / 2:
                    return Action.call()  # cheap limp with marginal hands
                return Action.fold()
            # Big blind, checked to.
            if score >= 7:
                return self._raise(gs, 3.5 * bb)
            return Action.call()

        ratio = opp_total / bb
        if ratio <= 4:  # a normal open
            if score >= 10:
                return self._raise(gs, opp_total * 3.2)
            if score >= (5.5 if not button else 6.5):
                return Action.call()
            return Action.fold()
        if ratio <= 14:  # a 3-bet (or bigger open)
            if score >= 14:
                return self._raise(gs, opp_total * 2.3)
            if score >= 9.5:
                return Action.call()
            return Action.fold()
        # 4-bet or shove: premiums only
        if score >= 12:
            return Action.call()
        return Action.fold()

    # ---------------------------------------------------------------- postflop
    def _postflop(self, gs: GameState) -> Action:
        level, _note = classify(gs.hole_cards, gs.community_cards)
        pot = gs.pot_size
        to_call = gs.call_amount
        street = gs.street or {3: "flop", 4: "turn", 5: "river"}.get(
            len(gs.community_cards), "flop"
        )
        can_raise = gs.can_raise or gs.can_bet

        if to_call > 0:
            self.bets += 1
        elif gs.is_dealer:
            self.checks += 1
        n = self.bets + self.checks
        agg = self.bets / n if n else 0.5
        # Against a frequent bettor, many bets are bluffs: call wider.
        slack = 0.0
        if n >= 25:
            slack = 0.12 if agg > 0.55 else (-0.04 if agg < 0.3 else 0.0)

        if to_call == 0:  # we may bet
            if level == 5:
                return self._frac_bet(gs, 0.9 if street == "river" else 0.75)
            if level == 4:
                return self._frac_bet(gs, 0.65)
            if level == 3:
                if street == "river" and self.rng.random() < 0.4:
                    return self._frac_bet(gs, 0.35)  # thin value
                return Action.call()
            if (
                level == 1
                and street == "flop"
                and self._was_preflop_aggressor(gs)
                and self.rng.random() < 0.35
            ):
                return self._frac_bet(gs, 0.33)  # occasional c-bet bluff
            if (
                level <= 2
                and street == "turn"
                and gs.is_dealer
                and n >= 25
                and agg < 0.3
                and self.rng.random() < 0.3
            ):
                return self._frac_bet(gs, 0.5)  # passive opponent: stab when checked to
            return Action.call()

        # Facing a bet.
        price = to_call / (pot + to_call) if pot + to_call > 0 else 1.0
        if level == 5:
            if can_raise and street != "river" and self.rng.random() < 0.5:
                return self._frac_bet(gs, 1.0)
            if can_raise and street == "river":
                return self._frac_bet(gs, 1.0)
            return Action.call()
        if level == 4:
            limit = (0.42 if street == "river" else 0.5) + slack
            return Action.call() if price <= limit else Action.fold()
        if level == 3:
            return Action.call() if price <= 0.26 + slack else Action.fold()
        if level == 2:
            equity = 0.33 if street == "flop" else 0.17
            return Action.call() if price <= equity + slack / 2 else Action.fold()
        return Action.fold()
