"""Game theory for the v3 model: capital-weighted replacement, no verdicts.

Remaining decisions under test
  1. phi_ret  — fee an owner pays to retire (archive) a post
  2. block    — a retire is refused while a replacement pool exceeds this share of the bond
  3. decay    — how fast an archived slot's takeover floor falls back to min_bond

Units: every amount below is a NUMERAIRE, not a price. The economics is scale-free — only the
ratios G/B, p, phi and the correction delay Delta matter — so the same run reads as G=1 ALGO/day
with B=10 ALGO. The contract stores microALGO; the peg to USD exists only because the value of a
lie is usually USD-denominated.

The questions the numbers must answer
  - Can a liar retire to escape an incoming correction, and what does that cost?
  - Is 10% enough, or does a retire-under-pool need a higher fee?
  - Does the floor rule block legitimate reuse, i.e. does decay matter?

Model, one slot, one day per round
  - A false claim earns the owner G per day while it is visible.
  - Corrector agents notice with probability q per day and add money to a replacement pool;
    a pool strictly greater than the bond displaces the claim (owner keeps bond*(1-phi_rep)).
  - The owner may retire instead: keeps bond*(1-phi_ret), the post becomes archived
    (readable, not challengeable) and carries a takeover floor.
  - An archived slot is taken over by paying floor (one payment, no pool needed).
  - A displaced liar can counter-pool to take the slot back, which is how arms races form.

Flows tracked: the liar's net (external lying gain minus fees paid), the treasury, and the bond held
in the slot. Fines and fees are the only flows that change owners; the model does not attempt a full
balance sheet, so read the numbers as relative comparisons, not audited totals.

Run: uv run --with numpy python scripts/v3_game_theory.py
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, asdict

import numpy as np

G = 0.10           # liar gain per day while a false claim is visible ($)
PHI_REP = 0.10     # fee on displacement -> treasury
P_PENALTY = 0.50   # penalty on displacement -> the party that displaced you (correction reward)
MIN_BOND = 1.0
Q_DETECT = 0.05    # per-day chance a corrector spots a false claim
N_CORR = 5
CORR_CAP = 2.0
CORR_GIVE = 0.40
LIAR_CAP = 4.0
LIAR_COUNTER_P = 0.25
LIAR_FEAR = 0.30      # liar retires once the pool reaches this share of the bond
DECAY_DAYS = 180      # 0.9x per 180 days when decay is on


@dataclass
class Params:
    T: int = 365
    phi_ret: float = 0.10
    phi_ret_pool: float = 0.10     # fee when retiring under an open pool
    p_pen: float = P_PENALTY       # penalty on the displaced party
    pen_to_treasury: bool = False  # True: the penalty leaves the game instead of paying the displacer
    pen_split: float = 1.0         # share of the penalty paid to the displacer (rest to treasury)
    block: float = 0.50
    decay: bool = False
    b0: float = 1.0


@dataclass
class Outcome:
    false_visible_days: int
    escapes: int
    retaliations: int
    liar_net: float
    treasury: float
    archived: bool
    slot_bond: float
    corr_equity: float


def run(seed: int, pr: Params) -> Outcome:
    rng = np.random.default_rng(seed)
    treasury = 0.0

    # wallets: the single liar, and a pool of correctors with shared capital
    liar = LIAR_CAP
    corr = CORR_CAP * N_CORR

    owner = "liar"                    # who holds the claim
    bond = pr.b0
    pool = 0.0                        # replacement pool against the current claim
    archived = False
    archived_liar = False   # an archived post is still readable, so a false archive still misleads
    floor = 0.0
    archived_at = 0
    false_days = 0
    escapes = 0
    retaliations = 0
    liar_net = 0.0

    for day in range(pr.T):
        # --- archived slot: someone may take it over by paying the floor
        if archived:
            if archived_liar:
                false_days += 1   # readable by agents even though the owner walked away
            price = floor
            if pr.decay:
                price = max(MIN_BOND, floor * (0.9 ** ((day - archived_at) / DECAY_DAYS)))
            if day - archived_at >= 1 and corr >= price > 0 and rng.random() < 0.05:
                corr -= price
                bond = price
                owner = "corr"
                archived = False
                archived_liar = False
                pool = 0.0
                floor = 0.0
            continue

        visible_false = owner == "liar"
        if visible_false:
            false_days += 1
            liar += G
            liar_net += G

        # --- correctors notice and pool money
        if rng.random() < Q_DETECT * min(N_CORR, 3) and corr > 0:
            give = min(CORR_GIVE, corr, max(0.0, bond - pool + 0.1))
            corr -= give
            pool += give

        # --- owner decides whether to retire before being displaced
        if pool > 0 and bond > 0:
            ratio = pool / bond
            if ratio > pr.block:
                can_retire = False
            else:
                can_retire = True
            if can_retire and owner == "liar" and ratio >= LIAR_FEAR and rng.random() < 0.6:
                fee = bond * pr.phi_ret_pool
                liar += bond - fee
                liar_net += bond - fee
                # fee split: half pro-rata to the pool, half to the treasury
                to_pool = fee / 2
                corr += to_pool
                treasury += fee - to_pool
                escapes += 1
                archived = True
                archived_liar = owner == "liar"
                floor = max(MIN_BOND, bond)
                archived_at = day
                bond = 0.0
                pool = 0.0
                continue

        # --- displacement when the pool outweighs the bond
        if pool > bond:
            fee = bond * PHI_REP
            pen = bond * pr.p_pen
            if owner == "liar":
                liar += bond - fee - pen
                liar_net += bond - fee - pen
                if pr.pen_to_treasury:
                    treasury += pen
                else:
                    corr += pen * pr.pen_split   # correctors collect their share
                    treasury += pen * (1 - pr.pen_split)
            else:
                corr += bond - fee - pen
                if pr.pen_to_treasury:
                    treasury += pen
                else:
                    liar += pen * pr.pen_split   # the displacing liar collects its share
                    liar_net += pen * pr.pen_split
                    treasury += pen * (1 - pr.pen_split)
            treasury += fee
            # the pool becomes the new bond
            bond = pool
            owner = "corr"
            pool = 0.0
            continue

        # --- the liar tries to take the slot back
        if owner == "corr" and liar >= bond * (1 + PHI_REP) and rng.random() < LIAR_COUNTER_P:
            need = bond * 1.01
            liar -= need
            fee = bond * PHI_REP
            pen = bond * pr.p_pen
            corr += bond - fee - pen
            treasury += fee
            if pr.pen_to_treasury:
                treasury += pen
            else:
                liar += pen * pr.pen_split
                liar_net += pen * pr.pen_split
                treasury += pen * (1 - pr.pen_split)
            bond = need
            owner = "liar"
            pool = 0.0
            retaliations += 1

    return Outcome(
        false_visible_days=false_days,
        escapes=escapes,
        retaliations=retaliations,
        liar_net=liar_net,
        treasury=treasury,
        archived=archived,
        slot_bond=bond,
        corr_equity=corr + (bond if owner == "corr" else 0.0),
    )


def summarize(rs: list[Outcome]) -> dict:
    out = {}
    for k in ("false_visible_days", "escapes", "retaliations", "liar_net", "treasury", "slot_bond", "corr_equity"):
        v = np.array([getattr(r, k) for r in rs], dtype=float)
        out[k] = {"mean": float(v.mean()), "se": float(v.std(ddof=1) / math.sqrt(len(v))) if len(v) > 1 else 0.0}
    out["archived_share"] = float(np.mean([1.0 if r.archived else 0.0 for r in rs]))
    return out


def main() -> None:
    seeds = range(200)
    print("기준(단위=누메레르): B=1, G=0.10/일, 탐지 5%/일, 교정자 5명(각 2), 교체 수수료 10%, T=365일, 시드 200")
    print("  ALGO 표기로는 B=10 ALGO, G=1 ALGO/일, 교체 수수료 1 ALGO — 비율이 같으면 결과도 같다")
    print()
    print(f"{'phi_ret':>8}{'phi_ret_pool':>13}{'block':>7}{'decay':>7}{'거짓노출일':>10}{'도피':>6}{'반격':>6}{'거짓말EV':>10}{'트레저리':>10}{'슬롯보증':>9}")
    rows = []
    for phi_ret_pool in (0.10, 0.25, 0.50):
        for block in (0.25, 0.50, 0.75):
            for decay in (False, True):
                pr = Params(phi_ret=0.10, phi_ret_pool=phi_ret_pool, block=block, decay=decay)
                s = summarize([run(sd, pr) for sd in seeds])
                rows.append({"params": asdict(pr), "summary": s})
                print(f"{pr.phi_ret:>8.0%}{phi_ret_pool:>13.0%}{block:>7.0%}{'on' if decay else 'off':>7}"
                      f"{s['false_visible_days']['mean']:>10.1f}{s['escapes']['mean']:>6.2f}"
                      f"{s['retaliations']['mean']:>6.2f}{s['liar_net']['mean']:>10.2f}"
                      f"{s['treasury']['mean']:>10.2f}{s['slot_bond']['mean']:>9.2f}")
    with open("/tmp/v3_game_theory.json", "w") as fh:
        json.dump(rows, fh, indent=1)
    print()
    print("wrote /tmp/v3_game_theory.json")


if __name__ == "__main__":
    main()
