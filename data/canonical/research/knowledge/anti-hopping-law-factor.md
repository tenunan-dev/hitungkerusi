# Anti-Hopping Law (Article 49A) — A Forecast Factor

**Added:** 3 August 2026 · **Status:** active factor with a live legal test in the current cycle

## The law

The Constitution (Amendment) Act (No. 3) 2022 inserted **Article 49A** into the Federal
Constitution; in force since **5 October 2022** (passed unanimously 28 July 2022, two
years after the 2020 Sheraton Move collapsed the PH government).

### What triggers a seat vacancy
| Trigger | Consequence |
|---|---|
| MP **resigns** from the party they were elected under | seat vacated → by-election |
| MP **ceases to be a member** (voluntarily) of their elected party | seat vacated → by-election |
| MP elected as **independent joins a party** | seat vacated → by-election |

### What does NOT trigger a vacancy (the loopholes)
| Case | Legal basis | Precedent |
|---|---|---|
| MP **expelled** from their party | Art 49A(2)(c) — expulsion ≠ ceasing membership | **WAWASAN (13 Jun 2026)**: Bersatu's ejected faction (Hamzah et al., 6 MPs) kept seats and joined PN — no by-elections |
| **En-bloc** departure of a whole party from a coalition | not covered by 49A | 2020 Sheraton Move |
| Party **dissolution or merger** | amendment / practice | protected, seats kept |

## Why it matters for the GE16 forecast

A defection's *forecast impact* depends on its **legal classification FIRST**, before any
vote-swing arithmetic:

```
resigned?            → VACANCY → by-election event; seat leaves the 222 baseline
expelled?            → NO vacancy → personal-vote swap model (EVENT_SHOCK on the seat)
en-bloc / dissolution→ NO vacancy → valence/national story only
independent → party  → VACANCY → by-election event
legal challenge?     → seat flagged UNCERTAIN (do not flip in base case)
```

This is why the model keeps a `VACANCIES` register separate from `EVENT_SHOCKS`:
a vacancy is not a swing, it is a **structural removal** — the seat is contested fresh
in a by-election with new candidate dynamics.

## LIVE: the expulsion-loophole test (Aug 2026)

Bersatu has attempted to force vacancies for rebel MPs by expelling them — the very
mechanism Art 49A(2)(c) says *keeps* seats. The courts may rule on this in the current
cycle. Outcomes:

| Ruling | Model consequence |
|---|---|
| Courts uphold 49A(2)(c) (expulsion keeps seat) | status quo; WAWASAN 6 seats remain valid |
| Courts close the loophole | **model reset**: WAWASAN seats + any future expelled MP become vacancy risks |
| Ruling mid-campaign | treated as a major EVENT; vacancies handled via the by-election mechanism |

## Reference

- Federal Constitution, Article 49A (inserted by Act A1663, 2022)
- Skrine (Aug 2022), "Malaysia's Anti-Party Hopping Law: A Compromised Law?"
- Fulcrum / ISEAS, "Loopholes in Malaysia's Anti-Defection Law"
- ConstitutionNet (Oct 2022), law in force 5 Oct 2022
- News: Bersatu rebel-MP expulsion gambit (Aug 2026) — live legal test
