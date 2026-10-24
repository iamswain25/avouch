"""공동 이의(crowdfunded challenge) + 부분 패널티의 게임이론 체크.

세 조건을 만족하는 패널티 설계를 찾는다.

  1) 억제   : F + q * p * B >= G            (스패머가 글을 안 쓰는 조건)
  2) 정직   : eps * p * B <= S              (정직 작성자가 남는 조건)
  3) 이의   : q * (r * p * B) / n >= C + (1-q) * A / n   (기여자 1인 EV > 0)

핵심: 억제와 이의 보상은 같은 변수를 본다. P = p * B (기대 손실액 = 이의자 보상 풀).
패널티율 p는 보증금 B에 대한 비율일 뿐이고, 실제로 튜닝해야 하는 것은 P다.

  python3 scripts/bond_game_theory.py
"""

F = 0.002      # 글값(수수료)
Q = 0.5        # 검거율: 정크 글이 이의 제기되고 인용될 확률
EPS = 0.02     # 오판율(정직 글을 위반으로 판정)
S = 0.05       # 정직 작성자가 글 1건에서 얻는 효용
N = 5          # 공동 이의 참여 인원
C = 0.002      # 참여자 1인 검토비(LLM 열람 + 가스)
A = 0.005      # 패소 측이 무는 중재 수수료
R = 1.0        # 몰수 패널티 중 이의자 몫 비율


def p_deter(G: float, B: float, q: float = Q) -> float:
    """억제에 필요한 최소 패널티율. q*p*B >= G-F"""
    return max(0.0, (G - F) / (q * B))


def p_challenge_min(B: float, q: float = Q, n: int = N, c: float = C, a: float = A) -> float:
    """기여자 1인 기대이익이 0이 되는 패널티율."""
    return (c + (1 - q) * a / n) * n / (q * R * B)


def p_honest_max(B: float, eps: float = EPS, s: float = S) -> float:
    """정직 작성자 기대손실이 효용과 같아지는 패널티율."""
    return s / (eps * B)


def main() -> None:
    print("P = p*B (기대 손실액 = 이의자 보상 풀)")
    for g in (0.02, 0.10, 0.50):
        print(f"  억제 하한 (G=${g:.2f}, q={Q}): P >= ${(g-F)/Q:.3f}")
    print(f"  정직 허용 상한: P <= S/eps = ${S/EPS:.2f}")
    print(f"  이의 성립 하한: P >= ${(N*C + (1-Q)*A)/Q:.3f}")
    print()

    print("B별: G=$0.10 정크를 억제하는 패널티율과 그때의 이의 보상")
    print(f"{'B':>6} {'p':>7} {'P':>7} {'1인 보상':>9} {'검토비 대비':>10} {'정직비용':>9}")
    for B in (0.5, 1, 2, 5):
        p = p_deter(0.10, B)
        pot = p * B
        per = pot / N
        print(f"{B:>6.2f} {p:>6.0%} {pot:>7.3f} {per:>9.3f} {per/C:>9.1f}x {EPS*p*B:>9.4f}")
    print()

    print("10% 고정일 때 각 보증금이 억제하는 정크 가치 상한 (q=0.5)")
    for B in (0.5, 1, 2, 5):
        print(f"  B=${B:<4} p*B=${0.10*B:.2f} -> G < ${Q*0.10*B + F:.3f}")
    print()

    print("q=1(완전 탐지)이어도 10%가 불가능한 구간 (B < (G-F)/p)")
    for g in (0.05, 0.10, 0.50):
        print(f"  G=${g:.2f}: 필요 B >= ${(g-F)/0.10:.2f}")
    print()

    print("검토비 C 민감도: P=$0.20, q=0.5 일 때 감당 가능한 이의 인원")
    for c in (0.0005, 0.002, 0.01):
        print(f"  C=${c}: n <= {Q*0.20/c:.0f}")
    print()

    print("권장 파라미터 (P=$0.20 목표, r=100%, 패소자는 중재료만)")
    for B in (0.5, 1, 2, 5):
        p = 0.20 / B
        print(f"  B=${B:<4} p={p:>5.0%}  (정직비용 ${EPS*p*B:.4f}, 1인 보상 ${0.20/N:.3f})")


# ---------------------------------------------------------------------------
# 시빌 이후 버전: 개수 기준은 무의미, 총액 기준만 남는다.
#
# 글 1건당 이의는 1회뿐이므로 "몇 명이 걸었나"는 집행에 영향을 주지 않는다.
# 에이전트는 자식을 무한히 찍을 수 있고(Ed25519 키 생성 비용 0), 알고란드 계정
# 활성화 0.1 ALGO도 예치라 회수된다. 따라서 임계값은 총액 Q와 손실률 λ로만 건다.
#
# 이의자의 손익분기 신념 q* = (λ*Q + C + φ) / (r*P + λ*Q)
#   q* > 평균 승소율이면 "확신 있는 쪽만 제기"하는 필터가 된다.
# ---------------------------------------------------------------------------

def belief_threshold(P: float, Q: float, lam: float, r: float = 1.0,
                     cphi: float = 0.007) -> float:
    lam_q = lam * Q
    return (lam_q + cphi) / (r * P + lam_q)


def sybil_section() -> None:
    P, r, cphi, qbar = 0.20, 1.0, 0.007, 0.5
    neutral = (qbar * r * P - cphi) / (1 - qbar)   # EV=0 이 되는 λQ
    print()
    print(f"시빌 이후 설계: P=${P}, r={r}, C+φ=${cphi}, 평균승소={qbar:.0%}")
    print(f"  EV 중립 λQ = ${neutral:.3f} (이보다 크면 확신 있는 쪽만 제기)")
    print()
    print("  자본 강도 ρ=λQ/EV중립    q*(제출 임계 신념)")
    for rho in (1.0, 1.5, 2.0, 3.0, 5.0):
        lam_q = rho * neutral
        qstar = (lam_q + cphi) / (r * P + lam_q)
        print(f"  {rho:>18.1f}    {qstar:>8.0%}")
    print()
    print("  λ(패소 몰수율)별로 q*=68%를 만족하는 필요 스테이크 Q")
    lam_q_target = 2.0 * neutral
    for lam in (0.10, 0.25, 0.50, 1.00):
        Q = lam_q_target / lam
        print(f"    λ={lam:>4.0%}  Q=${Q:.2f}  (Q/P={Q/P:.1f}x)")


if __name__ == "__main__":
    sybil_section()
