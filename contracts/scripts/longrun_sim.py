"""장기 지속가능성 검증: 반복 게임 ABM (에이전트 기반 모델).

`bond_game_theory.py`는 1회 정적 손익분기만 본다. 이 스크립트는 같은 파라미터로
수천 라운드를 여러 시드로 돌려서 "그 균형이 유지되는가"를 본다.

핵심 질문
  1) 억제가 유지되는가 — 스패머가 학습하고 자본이 소진되면 스팸이 사라지고 계속 사라지는가
  2) 정직 작성자가 남는가 — 라운드당 PnL > 0
  3) 감시가 자금조달되는가 — 감시자 1회 검사당 PnL > 0, 아니면 감시자가 떠나고 스팸이 돌아온다
  4) 자동 술어 검사(q_obj)가 없으면 어떻게 되는가 — 유료 감시에만 의존할 때의 진동
  5) 고정 비용 — 박스 MBR float과 수수료 수입의 관계

모델
  스패머: 자본 K0, Beta 사후분포로 탐지율 학습, 기대이익>0이면 발행.
          확률 eps_explore로 손실이어도 탐침 발행(정보 획득). 파산 시 영구 이탈.
  감시자: W명이 라운드당 M건 검사(비용 C). 스팸 적중 시 몰수액 λB 획득,
          오탐 시 중재료 φ 부담. 최근 손익이 +면 진입, -면 이탈(공급 탄력적).
  판정  : 자동 술어(항상 켜짐, q_obj) -> 감시자(확률 q_w) 순서.

  uv run --with numpy python scripts/longrun_sim.py --rounds 2000 --seeds 100
  uv run --with numpy python scripts/longrun_sim.py --grid --json /tmp/longrun.json
"""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass, field, asdict

import numpy as np

# --- 경제 파라미터 (백서 6~8절) ---------------------------------------------
F = 0.002          # 글 1건 수수료
LAM = 0.50         # 유죄 판정 시 보증금 몰수율
PHI = 0.005        # 중재 수수료 (패소 측)
R_SHARE = 1.0      # 몰수액 중 감시자 몫
C_INSPECT = 0.002  # 감시자 1회 검사 비용
S_UTIL = 0.05      # 정직 작성자 글 1건 효용
MBR_POST = 0.0061  # 글 1건당 박스 MBR (실측)
ALGO_USD = 0.104714

N_HONEST = 50      # 라운드당 1건 발행
N_ATTACK = 20
M_INSPECT = 5      # 감시자 1인 라운드당 검사 건수
K0 = 5.0
W_MAX = 40


@dataclass
class Params:
    rounds: int = 2000
    B: float = 1.0
    G: float = 0.10
    q_obj: float = 0.60        # 자동 술어 검사 탐지율
    q_w: float = 0.10          # 감시자 1회 검사당 탐지율
    eps_false: float = 0.02    # 정직 글 오탐률
    eps_explore: float = 0.05  # 스패머 탐침 확률
    lam: float = LAM           # 몰수율 (민감도 분석용)
    phi: float = PHI           # 중재 수수료
    w0: int = 10               # 초기 감시자 수
    refill: bool = False       # 스패머 자본 재충전 여부(지속 공격자)
    window: int = 1            # 정산 창(라운드)


@dataclass
class Result:
    seed: int
    spam_late: float
    spam_std: float
    spam_max: float
    attack_pnl: float
    attack_bankrupt: float
    honest_pnl_per_post: float
    watcher_pnl_per_inspection: float
    watchers_final: float
    watchers_min: float
    platform_rev: float
    box_float_peak: float
    spam_posts: int = 0
    caught_posts: int = 0
    spam_series: list = field(default_factory=list)
    w_series: list = field(default_factory=list)


def simulate(seed: int, pr: Params) -> Result:
    rng = np.random.default_rng(seed)
    T = pr.rounds

    bal = np.full(N_ATTACK, K0)
    a = np.ones(N_ATTACK) * 1.0     # Beta(1,1)
    b = np.ones(N_ATTACK) * 1.0
    honest_pnl = 0.0
    honest_posts = 0
    watch_pnl = 0.0
    inspections_total = 0
    platform_rev = 0.0
    spam_series = np.zeros(T)
    w_series = np.zeros(T)
    spam_posts = 0
    caught_posts = 0
    injected = 0.0
    float_peak = 0.0
    W = pr.w0
    w_hist: list[float] = []

    for t in range(T):
        p_hat = a / (a + b)
        ev = (1 - p_hat) * pr.G - p_hat * (pr.lam * pr.B + pr.phi) - F
        able = bal >= (pr.B + F)
        probe = rng.random(N_ATTACK) < pr.eps_explore
        active = able & ((ev > 0) | probe)
        n_spam = int(active.sum())
        n_honest = N_HONEST
        total = n_honest + n_spam

        # 감시 배분 (라운드 내 균등)
        inspections = W * M_INSPECT
        insp_per_post = inspections / max(total, 1)

        # --- 정직 작성자 ---
        false_alarm = 1 - math.exp(-pr.eps_false * insp_per_post)
        n_fa = int(rng.binomial(n_honest, min(false_alarm, 1.0)))
        honest_pnl += n_honest * (S_UTIL - F) - n_fa * (pr.lam * pr.B + pr.phi)
        honest_posts += n_honest

        # --- 스패머 정산 ---
        if n_spam:
            caught_obj = rng.random(n_spam) < pr.q_obj
            hits = rng.poisson(pr.q_w * insp_per_post, n_spam)
            caught = caught_obj | (hits > 0)
            # 잡히면: 보증금 중 λB만 손실, 이득 없음, 중재료·수수료 부담
            # 안 잡히면: 보증금 반환 + 스팸 이득, 수수료 부담
            delta = np.where(caught, -(pr.lam * pr.B + pr.phi), pr.G) - F
            bal[active] = np.maximum(bal[active] + delta, 0.0)
            a[active] += caught
            b[active] += ~caught
            gains = float(caught.sum()) * R_SHARE * pr.lam * pr.B
            spam_posts += n_spam
            caught_posts += int(caught.sum())
        else:
            gains = 0.0

        watch_pnl += gains - n_fa * pr.phi
        inspections_total += inspections
        spam_series[t] = n_spam / max(total, 1)
        w_series[t] = W

        # --- 감시자 진입·이탈 (최근 20라운드 손익) ---
        w_hist.append(gains - inspections * C_INSPECT - n_fa * pr.phi)
        if len(w_hist) > 20:
            w_hist.pop(0)
        recent = float(np.mean(w_hist))
        if recent > 0 and W < W_MAX:
            W += 1
        elif recent < 0 and W > 1:
            W -= 1

        # --- 비용 구조 ---
        platform_rev += total * F
        float_peak = max(float_peak, total * pr.window * (pr.B + MBR_POST))

        if pr.refill:
            gap = np.where(bal < pr.B + F, K0 - bal, 0.0)
            injected += float(gap.sum())
            bal = bal + gap

    late = spam_series[int(T * 0.8):]
    return Result(
        seed=seed,
        spam_late=float(late.mean()),
        spam_std=float(spam_series.std()),
        spam_max=float(spam_series.max()),
        attack_pnl=float(bal.sum() - N_ATTACK * K0 - injected),
        attack_bankrupt=float((bal <= 0.01).mean()),
        honest_pnl_per_post=honest_pnl / max(honest_posts, 1),
        watcher_pnl_per_inspection=watch_pnl / max(inspections_total, 1),
        watchers_final=float(w_series[-1]),
        watchers_min=float(w_series.min()),
        platform_rev=platform_rev,
        box_float_peak=float_peak,
        spam_posts=spam_posts,
        caught_posts=caught_posts,
        spam_series=[round(float(x), 4) for x in spam_series[:: max(1, T // 50)]],
        w_series=[int(x) for x in w_series[:: max(1, T // 50)]],
    )


def summarize(rs: list[Result]) -> dict:
    out = {}
    for k in ("spam_late", "spam_std", "spam_max", "attack_pnl", "attack_bankrupt",
              "honest_pnl_per_post", "watcher_pnl_per_inspection", "watchers_final",
              "watchers_min", "platform_rev", "box_float_peak"):
        v = np.array([getattr(r, k) for r in rs], dtype=float)
        out[k] = {"mean": float(v.mean()), "se": float(v.std(ddof=1) / math.sqrt(len(v))) if len(v) > 1 else 0.0}
    return out


def fmt(s: dict) -> str:
    return "\n".join([
        f"  스팸 비중(후반 20%)   {s['spam_late']['mean']:8.2%} ± {s['spam_late']['se']:.2%}",
        f"  스팸 진동(std/max)    {s['spam_std']['mean']:8.2%} / {s['spam_max']['mean']:.2%}",
        f"  스패머 누적 PnL       ${s['attack_pnl']['mean']:8.2f} ± {s['attack_pnl']['se']:.2f}",
        f"  스패머 파산 비율      {s['attack_bankrupt']['mean']:8.1%}",
        f"  정직 PnL/글           ${s['honest_pnl_per_post']['mean']:8.4f}",
        f"  감시 PnL/검사         ${s['watcher_pnl_per_inspection']['mean']:8.4f}",
        f"  감시자 수(최종/최소)  {s['watchers_final']['mean']:8.1f} / {s['watchers_min']['mean']:.1f}",
        f"  박스 float 최대       ${s['box_float_peak']['mean']:8.2f}",
    ])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int, default=2000)
    ap.add_argument("--seeds", type=int, default=100)
    ap.add_argument("--grid", action="store_true")
    ap.add_argument("--refill", action="store_true")
    ap.add_argument("--explore", type=float, default=0.05)
    ap.add_argument("--only", default="", help="base,no_auto,auto_only 중 일부(쉼표)")
    ap.add_argument("--sweep", action="store_true", help="λ x B 민감도")
    ap.add_argument("--json", default="")
    args = ap.parse_args()

    seeds = list(range(args.seeds))
    out: dict = {}

    scenarios = {
        "base            (자동검사 0.6)": Params(rounds=args.rounds, eps_explore=args.explore, refill=args.refill),
        "no_auto         (자동검사 0.0)": Params(rounds=args.rounds, q_obj=0.0, eps_explore=args.explore, refill=args.refill),
        "auto_only       (감시자 없음)": Params(rounds=args.rounds, w0=1, eps_explore=args.explore, refill=args.refill),
    }
    if args.only:
        keep = [x.strip() for x in args.only.split(",") if x.strip()]
        scenarios = {k: v for k, v in scenarios.items() if k.split()[0] in keep}

    cache: dict[str, list[Result]] = {}
    for name, pr in scenarios.items():
        rs = [simulate(s, pr) for s in seeds]
        cache[name] = rs
        s = summarize(rs)
        print("=" * 78)
        print(f"{name}  B=${pr.B} G=${pr.G} q_w={pr.q_w} T={pr.rounds} seeds={len(seeds)}")
        print("=" * 78)
        print(fmt(s))
        print(f"  스팸 시계열(50점): {rs[0].spam_series[:12]}")
        print(f"  감시자 시계열   : {rs[0].w_series[:12]}")
        print()
        out[name] = {"params": asdict(pr), "summary": s,
                     "spam_series": rs[0].spam_series, "w_series": rs[0].w_series}

    if args.grid:
        print("=" * 78)
        print("격자: 자동 술어 탐지율(q_obj) x 스팸 이득(G)")
        print("=" * 78)
        print(f"{'q_obj':>6} {'G':>6} {'스팸비중':>9} {'std':>7} {'스패머PnL':>10} {'파산율':>7} {'감시PnL/검사':>13}")
        grid = []
        sub = seeds[: min(20, len(seeds))]
        for q_obj in (0.0, 0.3, 0.6, 1.0):
            for G in (0.05, 0.10, 0.20, 0.50):
                pr = Params(rounds=max(600, args.rounds // 3), q_obj=q_obj, G=G, refill=args.refill)
                gs = summarize([simulate(s, pr) for s in sub])
                grid.append({"q_obj": q_obj, "G": G,
                             **{k: v["mean"] for k, v in gs.items()}})
                print(f"{q_obj:>6.1f} {G:>6.2f} {gs['spam_late']['mean']:>9.2%} "
                      f"{gs['spam_std']['mean']:>7.2%} {gs['attack_pnl']['mean']:>10.2f} "
                      f"{gs['attack_bankrupt']['mean']:>7.1%} "
                      f"{gs['watcher_pnl_per_inspection']['mean']:>13.4f}")
        out["grid"] = grid

    # --- 검증: 실측 탐지율·글당 손익을 폐형식 EV와 대조 ---
    print("=" * 78)
    print("검증: 실측 탐지율과 글당 손익 vs 폐형식 EV (모델 자기일관성)")
    print("=" * 78)
    print(f"{'q_obj':>6} {'G':>6} {'스팸글':>8} {'실측q':>8} {'글당PnL':>10} {'폐형식EV':>10} {'차이':>9} {'부호일치':>8}")
    agree = 0
    cells = 0
    for q_obj in (0.0, 0.3, 0.6, 1.0):
        for G in (0.05, 0.10, 0.20, 0.50):
            pr = Params(rounds=min(2000, max(600, args.rounds // 3)), q_obj=q_obj, G=G)
            rs = [simulate(s, pr) for s in seeds[:10]]
            sp = sum(r.spam_posts for r in rs)
            ca = sum(r.caught_posts for r in rs)
            pnl = sum(r.attack_pnl for r in rs)
            if sp < 100:
                continue
            q_real = ca / sp
            per = pnl / sp
            ev = (1 - q_real) * G - q_real * (pr.lam * pr.B + pr.phi) - F
            same = (per > 0) == (ev > 0)
            cells += 1
            agree += int(same)
            print(f"{q_obj:>6.1f} {G:>6.2f} {sp:>8} {q_real:>8.1%} {per:>10.4f} {ev:>10.4f} "
                  f"{abs(per-ev):>9.4f} {'OK' if same else 'MISMATCH':>8}")
    print(f"  부호 일치 {agree}/{cells}")
    print()

    # --- 정상성: 라운드를 10구간으로 나눠 스팸 비중 추이 ---
    for name, rs_all in cache.items():
        series = np.array([r.spam_series for r in rs_all[: min(20, len(rs_all))]], dtype=float)
        # 시계열은 50점으로 축약돼 있으므로 10구간 평균
        dec = np.array_split(series, 10, axis=1)
        means = [f"{d.mean():.2%}" for d in dec]
        print(f"정상성(10구간) {name:<32} " + " ".join(means))
    print()

    # --- 해석적 보정: 감시자 수 W에 따른 유효 탐지율과 스패머 EV ---
    print("=" * 78)
    print("해석적 대응: 감시 강도 W -> 유효 탐지율 q_eff -> 스패머 EV (G=$%.2f, B=$%.2f)" % (Params().G, Params().B))
    print("=" * 78)
    print(f"{'q_obj':>6} {'W':>4} {'q_eff':>8} {'스패머EV':>10}")
    for q_obj in (0.0, 0.6):
        for W in (1, 5, 10, 20, 40):
            insp = W * M_INSPECT / (N_HONEST + 3)
            q_eff = 1 - (1 - q_obj) * math.exp(-Params().q_w * insp)
            ev = (1 - q_eff) * Params().G - q_eff * (Params().lam * Params().B + Params().phi) - F
            print(f"{q_obj:>6.1f} {W:>4} {q_eff:>8.1%} {ev:>10.4f}")
    print()

    # --- 판정: 지속가능성 5개 기준 ---
    print("=" * 78)
    print("판정 (지속가능성 5기준)")
    print("=" * 78)
    print(f"{'시나리오':<26}{'억제':>6}{'정직':>6}{'감시자금':>9}{'진동':>6}{'공격자EV':>8}")
    for name in cache:
        rs = cache[name]
        s = summarize(rs)
        deter = s["spam_late"]["mean"] < 0.01
        honest = s["honest_pnl_per_post"]["mean"] > 0
        funded = s["watcher_pnl_per_inspection"]["mean"] > 0
        stable = s["spam_std"]["mean"] < 0.01
        atk = s["attack_pnl"]["mean"] < 0
        mark = lambda b: "PASS" if b else "FAIL"
        print(f"{name:<26}{mark(deter):>6}{mark(honest):>6}{mark(funded):>9}{mark(stable):>6}{mark(atk):>8}")
    print()

    if args.sweep:
        print("=" * 78)
        print(f"민감도: λ x B (q_obj=0.3, G=0.20, T={max(800, args.rounds//4)}) — 억제 경계 이동")
        print("=" * 78)
        print(f"{'λ':>5} {'B':>5} {'q*이론':>8} {'스팸비중':>9} {'글당PnL':>10} {'판정':>6}")
        sweep = []
        for lam in (0.25, 0.50, 1.00):
            for B in (0.5, 1.0, 2.0):
                pr = Params(rounds=max(800, args.rounds // 4), q_obj=0.3, G=0.20, B=B, lam=lam)
                rs = [simulate(s, pr) for s in seeds[:15]]
                sp = sum(r.spam_posts for r in rs)
                pnl = sum(r.attack_pnl for r in rs)
                qstar = (0.20 - F) / (0.20 + lam * B + PHI)
                per = pnl / sp if sp else float("nan")
                s_late = float(np.mean([r.spam_late for r in rs]))
                verdict = "억제" if s_late < 0.01 else "실패"
                sweep.append({"lam": lam, "B": B, "qstar": qstar, "spam_late": s_late,
                              "pnl_per_post": per, "verdict": verdict})
                print(f"{lam:>5.2f} {B:>5.1f} {qstar:>8.1%} {s_late:>9.2%} {per:>10.4f} {verdict:>6}")
        out["sweep"] = sweep
        print()

    posts = N_HONEST + N_ATTACK
    print("=" * 78)
    print("비용 구조 (라운드=1일, 만기 창=1라운드)")
    print("=" * 78)
    rev = posts * F
    flt = posts * (Params().B + MBR_POST)
    print(f"  글 {posts}건/일: 수수료 ${rev:.4f}/일, 박스 float ${flt:.2f} (MBR 비중 {MBR_POST/(Params().B+MBR_POST):.1%})")
    print(f"  float/수입 = {flt/rev:.0f}x, 창 7일이면 ${flt*7:.2f} — 소각이 아닌 예치(회수 가능)이나 운전자본 필요")
    print(f"  float을 ALGO로 보관할 때 가격 리스크: ALGO_USD={ALGO_USD}")

    if args.json:
        with open(args.json, "w") as fh:
            json.dump(out, fh, indent=1)
        print(f"\nwrote {args.json}")


if __name__ == "__main__":
    main()
