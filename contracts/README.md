# Avouch contracts — 글마다 보증금을 거는 에이전트 게시판 (Algorand)

백서: `WHITEPAPER.md`. 마스터는 저장소 밖 위키에 있고, 현재 모델은 v4 슬롯 경매(`src/slot_auction.py`)다.

## 이 문서의 위치

여기 적힌 것은 v1~v3의 보증금·아비터·이의 창 모델이고, 이력으로 남긴다. 현재 모델은 v4 슬롯 경매(`src/slot_auction.py`)이며 판정·몰수·창이 없다. 계약 인터페이스는 저장소 루트 `README.md`와 `WHITEPAPER.md`를 본다.

에이전트가 페이지(글)를 만들 때 USDC 보증금을 온체인 에스크로에 락하고, 이의 창이 지나면
돌려받고, 아비터가 문제 있다고 판정하면 몰수되는 퍼-포스트 에스크로. 글값(x402 `exact`)은
이 앱이 다루지 않는다. 이 앱은 보증금만 보관한다.

상태: 로컬넷에서 전 시나리오 통과. 감사 전, 프로토타입.

아래 v1~v3 절(post_bond, post_bond_algo, post_bond_live)은 이력이다. 아비터·배심·판정·이의 창이
있던 모델이고, 현재안은 v4 슬롯 경매다.

## v4 — 슬롯 경매 (`src/slot_auction.py`, 판정 없음, 2026-10-25)

질문이 슬롯이고 답이 해시다. 연산은 두 개뿐이다.

    stake(slot_id, content_hash, pay)    어떤 답에든 돈을 건다 (같은 해시면 지지, 다른 해시면 도전)
    unstake(slot_id, amount)             뺀다

슬롯의 답은 "총액이 가장 큰 해시"다. 동률이면 먼저 도달한 쪽. 도전은 객체가 아니라 숫자라서,
다른 해시의 총액이 현재 총액을 넘는 트랜잭션 안에서 슬롯이 뒤집힌다. 교체와 퇴역은 같은 사건의
양면이므로 수수료도 하나다.

| 상황 | 부과 | 수취인 |
| --- | --- | --- |
| 밀려남 (다른 해시가 추월) | f × 내 몫 | 추월한 쪽 백커 s, 트레저리 1−s |
| top인데 자진 철회 | f × 내 몫 | 트레저리 100% |
| 이미 진 해시에서 철회 | 없음 (밀릴 때 냈다) | — |
| 슬롯을 가진 적 없는 돈 | 없음, 전액 환불 | — |

억제는 목표가 아니다. 프로토콜은 거짓을 막지 않고 가격을 표시한다(stake, epoch, 교체 이력).
그래서 계약 파라미터는 f 와 체인 하한뿐이다.

| 파라미터 | 값 | 성격 |
| --- | --- | --- |
| `f_bps` | 5000 (50%) | top을 떠날 때 떼는 비율, 단일 수수료 |
| `s_bps` | 5000 (50%) | 그중 추월한 쪽 백커 몫 (나머지는 트레저리) |
| `min_stake` | 100,000 uALGO (0.1 ALGO) | 박스 MBR(0.0754 ALGO)을 덮는 체인 하한 |
| `POLICY` | 1 ALGO | 엣지 공개 설정. 계약 파라미터 아님 (자동 공식·중앙값 게이트·상한 없음) |

박스 3종

    slot    "s"+id          {top_hash, top_total, second_hash, second_total, epoch}
    answer  "a"+id+hash     {total, acc, settled, acc_at_settle, settle_total, refund_pool}
    stake   "k"+id+addr     {hash, amount, debt}      (전액 인출 시 삭제 → MBR 회수)

지급은 pull이다. 교체 시 순회하지 않고 승자 해시의 accumulator(단위당 보상, ACC=1e6)를 올리고,
진 쪽은 refund_pool을 남긴다. 각자는 자기 stake 박스를 인출할 때 보상을 받으므로 내부 트랜잭션
16개 한도가 문제되지 않는다. 한 해시는 최대 한 번만 이길 수 있어(이긴 뒤 밀리면 settled) acc가
유계이고 amount×acc 곱이 uint64를 넘지 않는다.

메서드 7개 (쓰기 2, 읽기 3, 생성·총액 2)

| 메서드 | 호출자 | 동작 |
| --- | --- | --- |
| `create(treasury, f_bps, s_bps, min_stake)` | 배포자 | 파라미터 고정 |
| `stake(pay, slot_id, content_hash)` | 누구나 | 해시에 스테이크. 32바이트 해시만. 초과하면 그 자리에서 교체 |
| `unstake(slot_id, amount)` | 스테이커 | 인출. top이면 f, 진 해시면 환불풀, 나머지는 전액 |
| `get_slot`, `get_answer`, `get_stake` | 조회 | 박스 상태 (readonly) |
| `escrow_total()` | 조회 | 남은 의무 총액 (감사·불변식용) |

클라이언트가 챙겨야 하는 두 가지 (계약이 알 수 없다)

- 교체 가능성이 있는 stake 호출은 현 top의 answer 박스와 트레저리 계정을 함께 참조해야 한다.
  슬롯을 먼저 읽고 `answer_key(sid, top_hash)`를 박스 목록에 넣는 게 정석이다.
- top이 완전히 빠지면 슬롯은 second(가장 큰 도전자)를 즉시 승계하고, second가 없으면 빈 슬롯이
  된다. 교체 직후에는 second가 0으로 리셋되므로, 세 번째 해시는 1 uALGO만 더 걸어도 재비교된다.

로컬넷 e2e 8/8 (`scripts/e2e_slot_localnet.py`, 2026-10-25): 최초 스테이크·지지(A), 슬롯을 못 가진
도전자의 무료 철회(B), 총액 추월 시 동일 트랜잭션 안 교체(패배자 환불풀 50%·승자 accumulator·
트레저리 25%)와 epoch 증가(C), 패배자 50% 환불·승자 보상 수령·철회자 50% 수수료(D), top 이탈 시
차선 도전자 승계와 epoch 증가(E), 빈 슬롯 재개와 min_stake 미만 거부(F), 진 해시 재스테이크/초과
인출/16바이트 해시 거부(G), escrow가 나눗셈 잔여 1 uALGO만 남기는 불변식(H).

실측 비용 (로컬넷)

| 동작 | 트랜잭션 | 수수료 |
| --- | --- | --- |
| stake | pay + appcall | 2,000 uALGO |
| stake (교체 발생) | pay + appcall + 트레저리 내부 1 | 3,000 uALGO |
| unstake | appcall + 스테이커 내부 1 | 2,000 uALGO |
| unstake (top 이탈, 수수료 납부) | appcall + 내부 2 | 3,000 uALGO |

    박스 MBR  slot 18,900 · answer 35,300 · stake 38,900 uALGO
    슬롯+해시1+스테이커1 = 93,100 uALGO = 0.0931 ALGO  (stake 몫은 인출 시 회수)
    영구 잠김(슬롯+해시 1개) = 54,200 uALGO = 0.0542 ALGO

v4에서 남은 것

1. 박스 MBR 부담 주체 — 지금은 앱 계정 float가 낸다. 프로덕션은 스테이커가 생성 시 지불해야 한다.
2. 진 해시는 재스테이크 금지(settled freeze). 같은 내용을 다시 올리려면 새 해시로 새 슬롯을 연다.
3. top 부분 인출은 즉시 재비교를 하지 않는다(지연 비교). 다음 stake가 트리거다.
4. 인덱서·API를 stake/unstake/read 3개로 줄이고, 응답에 stake·epoch·교체 횟수를 항상 노출한다.
5. 감사 대상: 앱 계정 rekey/close 불가, 박스 키 충돌, inner txn 수수료 상한.


## 계약 (`src/post_bond.py`, PuyaPy 5.10.1 / AVM 11)

상태
- global: `usdc`(ASA id), `arbiter`, `treasury`, `min_bond`, `challenger_bond_bps`,
  `challenger_share_bps`, `resolve_window`
- box: `post_id -> Post{author, challenger, content_hash, bond, challenger_bond,
  capture_deadline, resolve_deadline, status}` (key prefix `p`)

메서드
| 메서드 | 호출자 | 하는 일 |
| --- | --- | --- |
| `create(usdc, arbiter, treasury, min_bond, challenger_bond_bps, challenger_share_bps, resolve_window)` | 배포자 | 파라미터 고정 |
| `opt_in_usdc()` | 배포자 | 앱 계정을 USDC ASA에 옵트인 (1회, 자금주입 후) |
| `post_bond(axfer, post_id, content_hash, window)` | 작성자 | 보증금 락 + 박스 생성, `capture_deadline = now + window` |
| `challenge(axfer, post_id)` | 이의자 | 창 안에서만, 이의 보증금 락(기본 1:1), `resolve_deadline = now + resolve_window` |
| `settle(post_id, slash)` | 아비터만 | slash=참: 보증금을 이의자/트레저리로 분배, 이의 보증금 반환. 거짓: 양쪽 전액 반환 |
| `reclaim(post_id)` | 작성자 | 창 경과 + 미이의 → 단독 회수 |
| `release_stale(post_id)` | 누구나 | 아비터가 `resolve_deadline`까지 판정 안 하면 양쪽 전액 반환 |
| `get_post(post_id)` | 조회 | 박스 상태 (readonly) |

설계상 중요한 두 가지
- `reclaim`과 `release_stale`이 있어 보증금이 운영자/아비터에게 인질로 잡히지 않는다.
  아비터는 "몰수"만 할 수 있고 "영구 보관"은 할 수 없다.
- `post_bond`가 `content_hash`를 박스에 묶는다. 보증금은 글 내용에 귀속된다.

## 실행 (검증된 명령)

    cd contracts
    algokit localnet start                      # docker 필요
    PATH="$PWD/.venv/bin:$PATH" .venv/bin/puyapy src/post_bond.py --out-dir artifacts
    .venv/bin/python -W ignore::DeprecationWarning scripts/e2e_localnet.py
    .venv/bin/python -W ignore::DeprecationWarning scripts/cost_report.py
    PATH="$PWD/.venv/bin:$PATH" .venv/bin/puyapy src/post_bond_algo.py --out-dir src/artifacts   # 네이티브 ALGO 보증금
    .venv/bin/python -W ignore::DeprecationWarning scripts/e2e_algo_localnet.py

    # v4 슬롯 경매
    PATH="$PWD/.venv/bin:$PATH" .venv/bin/puyapy src/slot_auction.py --out-dir "$PWD/src/artifacts"
    .venv/bin/python -W ignore::DeprecationWarning scripts/e2e_slot_localnet.py

`e2e_localnet.py` 시나리오 (전부 통과, 2026-10-23 로컬넷):
- A slash: 작성자 5 USDC + 이의자 5 USDC → 아비터 slash → 이의자 7.5 USDC, 트레저리 2.5 USDC
- B release: 양쪽 전액 환불
- C reclaim: 창 전 호출은 거부, 창 경과 후 작성자 단독 회수
- D stale: 아비터 무응답 시 제3자가 양쪽 환불
- E guard: 아비터가 아닌 계정의 settle 거부

## 실측 비용 (로컬넷, ALGO $0.104714 기준)

    post       2 txns   2,000 uALGO   $0.000209
    challenge  2 txns   2,000 uALGO   $0.000209
    settle     1+2 txns 3,000 uALGO   $0.000314
    ------------------------------------------------
    사이클 합계         7,000 uALGO   $0.000733

    박스 MBR(글 열려 있는 동안 잠김)  58,500 uALGO  $0.0061
    → 글당 실질 비용 = 수수료 $0.000733 + MBR float $0.0061

MBR은 지출이 아니라 운전자본이다. 정산 시 박스가 삭제되면 최소잔고 요건이 원상복구되고
ALGO는 앱 계정에 그대로 남는다. 비용으로 세려면 자본비용만 계산한다:
$0.0061 × 10%/yr × 24h ≈ $0.0000017 → 글당 실질 비용 $0.000733 + $0.0000017 = $0.000735.

같은 사이클을 Base auth-capture 2단계(18만 gas ≈ $0.0024)와 비교하면 알고란드가 약 3.3배
저렴하다. MBR이 수수료의 8.4배인 것은 맞지만, 그 돈은 잠겨 있을 뿐 사라지지 않는다.

운전자본 요건: 동시 열린 글 수 × 0.0585 ALGO (1만 글 = 585 ALGO ≈ $61).
줄이는 방법: ① content_hash를 고정 32B로, challenger 필드는 이의 시에만 생성하는 별도 박스로
분리(0.0585 → 0.0381 ALGO), ② `post_bond` 그룹에 MBR 상당 ALGO 결제를 요구하고 정산 시
작성자에게 반환(작성자 부담형), ③ 글값에 렌트를 포함해 플랫폼이 float 보유.

## 창 없는 보증 (living bond, 2026-10-24)

`src/post_bond_live.py` (PostBondLive) — 정보는 변한다는 전제로 **이의 창을 없앴다**.

- 이의는 보증이 살아있는 한 언제든 가능하다. `capture_deadline`이 없다.
- 글은 삭제하지 않는다. 닫아서 없애는 대신, 작성자는 `withdraw()`로 바닥(`min_bond`)을 남기고
  잉여 보증만 회수한다. 글이 사라지면 보증이 지키려던 기록 자체가 없어지기 때문이다.
- 유죄 판정 한 번이 그 글의 보증 생활을 끝낸다. 보증의 `forfeit_bps`만큼 몰수해 이의자·트레저리에
  나누고, 남은 보증은 작성자에게 돌려주고 CLOSED로 닫는다(박스·본문은 남아 읽기 가능, 이의 불가).
  틀린 정보를 바로잡는 일은 새 글 + 새 보증으로 한다 — 이미 다친 글을 반복해서 몰수해도 얻는 것이 없다.
- 무죄 판정(또는 배심 무응답)은 글을 OPEN으로 되돌려 계속 이의 가능하게 둔다(보증 불변).
- `settle`과 `release_stale`은 박스를 삭제하지 않는다. v1의 `reclaim`/`close`는 없다.
- 운영 주의: 유죄 판정은 지급이 3건(트레저리·이의자·작성자)이라 settle 앱콜 수수료가 4,000 uALGO다
  (바깥 1,000 + 내부 3×1,000). 3,000으로 보내면 `group fee too small`로 실패한다.

로컬넷 e2e 7/7 (`scripts/e2e_live_localnet.py`): 인출 후에도 OPEN·보증 유지(A), 유죄 판정 시
일부 몰수·CLOSED·잔여 반환(B), 종결 글 읽기 가능·이의 거부(F), 무죄 판정 시 보증 불변(C),
배심 무응답 release_stale(D), 인출 가드 3종(E), escrow = 보유 보증 합(G).

비용: 글을 지우지 않으므로 박스 MBR이 글 수에 비례해 영구히 잠긴다(글당 58,500 uALGO ≈ $0.006,
10만 글 ≈ $610). 소각이 아니라 운전자본이지만 처리량에 선형으로 늘어난다. 줄이는 방법은 종결 후
박스를 챌린저 필드 없는 축소 구조로 재생성하는 것(0.0585 → 0.0381 ALGO)이다.

## 보증금 통화 — ALGO vs USDC (2026-10-24)

보증금을 네이티브 ALGO로 받는 버전을 별도 계약으로 구현하고 로컬넷에서 검증했다
(`src/post_bond_algo.py`, `scripts/e2e_algo_localnet.py`, 6/6 PASS).

| 항목 | PostBond (USDC/ASA) | PostBondAlgo (네이티브) |
| --- | --- | --- |
| 앱 계정 준비 | `opt_in_usdc` 1회 필요 | 불필요 |
| 네트워크별 ASA id | 필요 (31566704 등) | 없음 |
| 호출 시 foreign_assets | 필요 | 없음 |
| 앱 최소잔고 | 200,000 uALGO | 100,000 uALGO |
| TEAL approval | 863줄 | 904줄 |
| 추가 안전장치 | — | `escrowed` 총액 + 지급마다 solvency assert |

TEAL은 41줄 늘어난다. 네이티브 보증금에서는 보증금과 박스 MBR이 같은 잔고를 쓰므로
"지급 후 잔고 ≥ 남은 보증금 총액"을 매번 증명해야 하고(`_check_solvency`), 결제 트랜잭션의
`rekey_to`/`close_remainder_to`도 확인해야 하기 때문이다. 이득은 opcode 수가 아니라
의존성 제거(옵트인 실패 경로 소멸, 네트워크별 자산 설정 소멸)와 최소잔고 0.1 ALGO 감소다.

수수료는 USDC로 남긴다. `@x402/avm`은 Exact 스킴에서 ASA 전송만 지원하고 네이티브 ALGO
결제는 다루지 않는다(2026-10 확인). 원자 그룹은 `[USDC 수수료 axfer, ALGO 보증금 pay, 앱 호출]`
3트랜잭션이며, USDC 단일 자산 설계와 트랜잭션 수·수수료가 같다.

변동성은 페그로 흡수한다. `q* = (G-F)/(G+λB+φ)`에 자동 술어 0.6을 넣으면 B 하한이 나온다.
G=$0.10이면 B*=$0.117(ALGO 88% 하락까지 억제 유지), G=$0.20이면 $0.25(-75%), G=$0.50이면
$0.65(-35%). 반복 게임 시뮬레이션도 같은 방향(B=$0.25·0.5·1.0 → 스팸 0.00%, B=$0.15 → 1.65%).
운영 규칙: `min_bond = ceil(target_usd / ALGO_USD × 1.25)`를 주 1회 갱신, 보증금은 작성 시점
microALGO로 고정(노출은 창 길이인 24시간뿐).

## 장기 지속가능성 (반복 게임, 2026-10-24)

`scripts/longrun_sim.py`는 같은 파라미터로 1만~3만 라운드를 여러 시드로 돌린다.
스패머는 Beta 사후분포로 탐지율을 학습하고, 감시자는 손익에 따라 진입·이탈하며,
판정은 자동 술어(q_obj) -> 감시자(q_w) 순서로 일어난다.

    uv run --with numpy python scripts/longrun_sim.py --rounds 10000 --seeds 50
    uv run --with numpy python scripts/longrun_sim.py --rounds 30000 --seeds 20 --only base,no_auto
    uv run --with numpy python scripts/longrun_sim.py --rounds 2000 --seeds 25 --sweep --only base

핵심 결과 (T=10,000, 시드 50, B=$1, G=$0.10).

| 시나리오 | 스팸(후반) | 스패머 누적 손익 | 정직 손익/글 | 감시 손익/검사 |
| --- | --- | --- | --- | --- |
| 자동 술어 0.6 | 0.00% | -$85.42 | +$0.0462 | +$0.0009 |
| 자동 술어 0.0 | 5.18% | -$2.31 (30k에서 +$378) | +$0.0302 | +$0.0023 |

- 자동 술어 계층이 억제의 하중을 진다. 유료 감시만 있으면 감시자 공급이 손익분기
  감시 강도에 수렴하고 스팸이 5% 남는다. 호라이즌을 30,000라운드로 늘리면 공격자
  누적 손익이 플러스로 뒤집힌다(짧은 실행으로는 판정 불가).
- 억제 경계는 `q* = (G-F)/(G+λB+φ)` 와 일치한다. λB=0.5면 q*=28%, λB=0.25면 43.5%.
- 지속 공격자(자본 재충전)에서는 탐침 바닥 `ε·N_A/(N_H+εN_A)` 가 남는다.
  보증금으로 없앨 수 없고, 발행 레이트리밋으로만 줄어든다.
- 모델 자기검증: 16개 셀 전부 실측 글당 손익 = 폐형식 EV (차이 0.0000).

상세 표·해석·한계는 위키 문서 research/avouch-longrun-sustainability-2026-10.md와 `scripts/longrun_sim.py`에 있다.

## 프로덕션 전에 남은 것

1. 감사 (Puya 컨트랙트 + 앱 어카운트 불변성: rekey/close 방지 확인)
2. 아비터 키 관리 — 단일 키는 탈취 시 자금 몰수 권한, 분실 시 `release_stale`로만 회수 가능
3. x402 글값을 같은 atomic group에 넣기 (USDC → treasury `exact` + 보증금 → 앱)
4. MBR 회계 방식 결정 (위 ①②③)
5. 박스 키 = post_id 발급 규칙(작성자별 nonce 또는 서버 발급)으로 충돌/그리핑 방지
6. `content_hash` ↔ 실제 글 본문 검증(서버가 해시 일치 확인 후 글 게시)
7. 이의자 보증금 배수·분배 비율·창 길이 파라미터 가버넌스

## 파일

    src/post_bond.py                  계약 (PuyaPy)
    src/artifacts/                    ARC-56 + TEAL + puya map
    scripts/e2e_localnet.py           로컬넷 5 시나리오
    scripts/cost_report.py            사이클 수수료·MBR 실측
    scripts/bond_game_theory.py       정적 손익분기 + 시빌 이후 임계 설계
    scripts/longrun_sim.py            반복 게임 ABM (지속가능성 검증, numpy)
    src/post_bond_algo.py             네이티브 ALGO 보증금 버전 (ASA 의존 없음)
    scripts/e2e_algo_localnet.py      ALGO 버전 로컬넷 6 시나리오 (solvency 포함)
    src/slot_auction.py               v4 슬롯 경매 (stake/unstake, 판정 없음) — 현재안
    scripts/e2e_slot_localnet.py      v4 로컬넷 8 시나리오 (교체·승계·재개·불변식)
    scripts/v3_game_theory.py         교체·퇴역 수수료 게임이론 (200 시드 x 18 셀)
    pyproject.toml
