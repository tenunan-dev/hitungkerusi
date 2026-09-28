# GE16 Forecast — Factor Rankings with Sources

**Compiled:** 3 August 2026 | **Version:** 1.0 | **Purpose:** the authoritative ranked list of elements that can affect the Malaysian election result, each with its source(s) and what they say. Reference for all forecast updates (cron c176829fbdb3) and the `malaysia-ge16-forecast` skill.

**Provenance policy:** every element cites its source. Sources are split into:
- 🎓 **Academic** — peer-reviewed / institutional research
- 📰 **News** — event reporting (facts about what happened)
- 📊 **Official data** — EC, DOSM, BNM
- 🔬 **Our analysis** — derived from the project's datasets

---

## Tier 1 — Structural (decides who wins each seat before a single vote is cast)

### 1. Ethnic composition of the seat
| Source | What it says |
|---|---|
| 🎓 ISEAS Perspective 2023/20 — Marzuki Mohamad & Ibrahim Suffian (Merdeka Center), "Malaysia's 15th GE: Ethnicity Remains the Key Factor" | "More than 80% of non-Malay voters supported PH; ~57% of Malay voters chose PN; 95% Chinese, ~75% Indian → PH" |
| 🎓 Pepinsky, Fosco & Ostwald (2023), SMU — "Demographic structure and voting behaviour during democratization: Evidence from Malaysia's 2022 election" | Multinomial logit on all 222 seats: ethnicity predicts victory; PH ~certain in non-Malay seats, near-zero above ~80% Malay; urbanization adds little once ethnicity is controlled |
| 🔬 `voter-demographics-by-constituency-ge15.csv` (ElectionData.MY anonymised roll, 21,173,638 voters) | Exact per-seat ethnic shares of the actual electorate |

### 2. Region / seat geography (Malay Belt vs mixed vs East Malaysia)
| Source | What it says |
|---|---|
| 🎓 Pepinsky et al. 2023 (SMU) | Malay Belt (Kedah, Perlis, Kelantan, Terengganu + parts of Pahang) swung wholesale to PN; Malay Belt vs rest more predictive than East/West Coast |
| 🎓 ISEAS 2023/20 | Malay voters shifted to PN in the campaign's final weeks; regional Malay concentration amplified it |
| 🔬 `ge15-results-by-state.csv`; DUN folders | Observed: PN 14/15 Perlis, 14/14 Kelantan, 8/8 Terengganu, 14/15 Kedah |

### 3. GE15 baseline margin
| Source | What it says |
|---|---|
| 📊 Election Commission (official) via Wikipedia — "Results of the 2022 general election by parliamentary constituency" | The 222 verified per-seat margins; basis of `ge15-results-by-constituency-full.csv` |

### 4. Coalition structure & seat allocation
| Source | What it says |
|---|---|
| 📰 The Star (2026) — "Zahid assures no overlapping seats between BN and PH in GE16"; "Umno eyes contesting more than 30 seats" | Seat-negotiation news is itself the signal: who contests where is being decided now |
| 📰 NST (2026) — "Saifuddin: Seat requests should not be raised publicly" | PH internal seat-allocation friction |
| 🎓 Fulcrum/ISEAS — Hutchinson, "PN's Dramatic Denouement" (2026/182) | PAS controls PN seat allocation; WAWASAN gets what PAS gives |
| 🔬 `master-list-222-parliamentary-seats.csv`; party-landscape study | Current holder + party per seat |

---

## Tier 2 — Valence (shifts margins within the structure)

### 5. Leader approval ratings (by ethnicity)
| Source | What it says |
|---|---|
| 📊 Merdeka Center daily tracking (final GE15 campaign poll, 18 Nov 2022), quoted in 🎓 ISEAS 2023/20 | End-of-campaign Malay approval: Muhyiddin 71%, Ismail Sabri 57%, Hadi 51%, Anwar 32%, Zahid 12%; non-Malay: Anwar 65%. PM candidate = top vote factor for 29% of voters |

### 6. Coalition brand / corruption trust
| Source | What it says |
|---|---|
| 🎓 ISEAS 2023/20 | "UMNO leaders charged with corruption… serious trust deficit among Malay voters" → Malay voters moved BN→PN |
| 🎓 Pepinsky et al. 2023 | PN's "clean Islamic alternative" reputation "crucial in positioning it as a credible champion of Malay Muslim rights" |

### 7. Incumbent MP strength / candidate quality
| Source | What it says |
|---|---|
| 📊 Merdeka Center pre-GE15 poll (Jul 2022), cited in 🎓 Pepinsky et al. | "25% party, 25% candidate, 29% PM candidate, 17% issue" as top vote factors |
| 🔬 `meco-candidates-ge15-federal.csv`; candidate tracker (cron, Wed) | Who's defending, who's parachuted in |

---

## Tier 3 — Performance (conditional: decides within-ethnic choice)

### 8. Cost of living / inflation (CPI)
| Source | What it says |
|---|---|
| 📊 Merdeka Center (Oct–Nov 2022), cited in 🎓 ISEAS 2023/20 | Inflation (31%), political instability (13%), corruption (12%) = top three voter concerns |
| 📊 BTI 2026 Malaysia Country Report | CPI 3.7% (Jan 2023) → 1.5% (Jan 2024) → ~2.8% (2024); food inflation 2.6% |
| 🎓 Sunway University — "The Economic Voting Puzzle of Malaysia" | Malaysians do economically vote — but the effect is conditional, often masked by ethnic voting |

### 9. GDP growth
| Source | What it says |
|---|---|
| 🎓 Wilkin, Hallerberg & Carey (1997), in Lewis-Beck & Stegmaier (2000), *Annual Review of Political Science* | "For every percentage point of GDP growth in the election year, the major incumbent party stands to gain 1.4% of the vote" (cross-national) |
| 🎓 Lewis-Beck & Stegmaier (2000) — "Economic Determinants of Electoral Outcomes" | Canonical review: sociotropic > pocketbook; accountability clearest when blame is clear |
| 📊 BTI 2026 | Malaysia grew 5.2% (first 3 quarters 2024) vs 1.4% same period 2023 |

### 10. Government approval (overall)
| Source | What it says |
|---|---|
| 📊 Merdeka Center surveys 2021–2022, cited in 🎓 ISEAS 2023/20 | BN-led govt approval fell 50% (Sep 2021) → 31% (Oct 2022) — tracked the vote collapse |
| 🔬 Poll tracker (cron, Mon) | Live weekly Merdeka/Ilham headlines ("Anwar approval 55% May 2026" etc.) |

### 11. Ringgit / fuel & food prices
| Source | What it says |
|---|---|
| 📊 BTI 2026 | MYR 4.49/USD (Jan 2025); sector inflation detail (medical 12%, food 2.6%, ICT −3.9%) |

### 12. Governance & stability
| Source | What it says |
|---|---|
| 📊 Merdeka (2022), cited in 🎓 ISEAS 2023/20 | Political instability = voters' #2 issue (13%) |
| 🎓 Pepinsky et al. 2023 | "Three successive governments headed by three PMs in less than five years" |
| 📰 News 2026 (PRN Melaka dissolution; Bersatu toppling attempts per Fulcrum) | Instability events directly precede swings |

---

## Tier 4 — Events & Mechanics (low frequency, high variance)

### 13. Coalition ruptures (PAS–Bersatu split, WAWASAN)
| Source | What it says |
|---|---|
| 📰 Malaysiakini (8 Jun 2026) — "PAS hentikan kerjasama politik dengan Bersatu"; Malay Mail (10 Jun) | PAS ended cooperation with Bersatu 8 Jun 2026 |
| 🎓 Fulcrum/ISEAS — Hutchinson (2026/182) | Feb 2026 purge: 19 Bersatu MPs ejected → WAWASAN (13 Jun); Bersatu left with ~6 MPs, funds frozen |

### 14. Third-force entries (Bersama)
| Source | What it says |
|---|---|
| 🎓 RSIS Commentary (15 Jul 2026) — "Assessment and Early Analysis of the 2026 Johor State Election" | Bersama "acted as a direct spoiler for PH, siphoning progressive, urban protest votes" → helped BN's 48-seat supermajority |
| 📰 Straits Times (12 Jul 2026) | "All 15 Bersama candidates lose their deposit" — 3–6% each |
| 🔬 Johor DUN data (MECo) | PH 12→8 seat-level proof |

### 15. By-elections
| Source | What it says |
|---|---|
| 📊 ElectionData.MY / MECo — `meco-byelections-stats-1957-2026.csv` | 276 by-elections; latest Kinabatangan & Lamag (24 Jan 2026) |

### 16. Redelineation
| Source | What it says |
|---|---|
| 📰 FMT (Nov 2025) — "More Parliament seats for Sabah, S'wak before GE16, says Anwar"; East Asia Forum (8 Feb 2026); Wikipedia "Next Malaysian general election" | 222→235 approved in principle Sep 2023; EC review possible after Mar 2026; Sarawak +12 minimum |

### 17. Electoral timing
| Source | What it says |
|---|---|
| 📰 East Asia Forum (8 Feb 2026) — "Malaysia enters election mode in 2026" | Aligning Melaka (Dec 2026) with GE16 could save RM200M — timing is a strategic variable |
| 📰 Harian Metro (May 2026) — Ab Rauf on Melaka PRN timing; 🔬 Melaka PRN report | Separate polls favor local machinery (BN 2021 playbook) |

### 18. Youth turnout differentials (Undi18)
| Source | What it says |
|---|---|
| 🎓 Pandian (2025), *Social Sciences & Humanities Open* (ScienceDirect) — "Undi18 and the Malaysian youth vote" | Youth turnout below national average in GE15 and state polls; participation, not registration, is the issue |
| 🔬 Voter-roll data | 18–30 = 30.1% of electorate; youth share per battleground |

### 19. Scandals / MACC probes
| Source | What it says |
|---|---|
| 📰 Straits Times (May 2026) — Rafizi expected to be charged (Arm Holdings RM1.1bn probe); FMT | Concentrated trust damage; also drove Bersama's founding narrative |

---

## Weight stack (engine calibration — MY synthesis, not from any single source)

| Input | Weight | Basis |
|---|---|---|
| State-election swings (revealed preference) | 0.30 | Only *actual votes* since GE15; our strongest signal |
| Government approval | 0.25 | Merdeka tracking; approval tracked the GE15 collapse |
| Economy (CPI/GDP/ringgit) | 0.20 | Economic-voting literature, dampened for Malaysia |
| Events (ruptures, third forces) | 0.10 | Seat-specific shocks |
| Leader preference by ethnicity | 0.10 | Merdeka PM-preference data |
| Turnout differentials | 0.05 | Undi18 softness |

## Caveats on provenance
1. The ranking and weights are **our synthesis** — no single source ranks these elements. Order comes from triangulating ISEAS + Pepinsky + Merdeka + economic-voting literature.
2. Economic-voting coefficients (0.7·GDP − 0.8·(CPI−2) + 0.15·ΔAppr) are **cross-national estimates** (Wilkin; Lewis-Beck) dampened for Malaysia — not yet Malaysia-calibrated (Sunway paper flags Malaysia's conditional economic voting).
3. News sources are cited for **facts about events**, verified against our own election data where possible.
4. Source dates span 2000–2026; pre-2010 economics literature is foundational, Malaysian-specific calibration remains open work.
