# Phase 4.4 W02 reconciliation

This records the per-file disposition of the W02 `automation_references`
inventory (`ge16-runtime-dependency-inventory.json`).  The original inventory
has 38 files and 142 literal `01_RESEARCH` occurrences.  Batch 4.3 copied the
active subset into tracked packages; this reconciliation records the result of
the Task 4.4 canonical-data edits without treating an ignored legacy copy as
active code.

| W02 source file | occurrences | disposition | tracked destination / reason |
|---|---:|---|---|
| `05_AUTOMATION/archive_unused_2026-08-16.py` | 13 | no-tracked-copy | Classified outside the retained active set. |
| `05_AUTOMATION/assemble_agent_layers.py` | 1 | no-tracked-copy | Classified outside the retained active set. |
| `05_AUTOMATION/audit_db_coverage.py` | 2 | no-tracked-copy | Classified outside the retained active set. |
| `05_AUTOMATION/audit_dun_composition.py` | 1 | tracked-free | `automation/qa/audit_dun_composition.py`; canonical 1_DATA input. |
| `05_AUTOMATION/audit_precise.py` | 2 | no-tracked-copy | Classified outside the retained active set. |
| `05_AUTOMATION/audit_unused_v2.py` | 1 | no-tracked-copy | Classified outside the retained active set. |
| `05_AUTOMATION/build_boundary_swings.py` | 6 | tracked-free | `automation/state_analysis/build_boundary_swings.py`; canonical 1_DATA input. |
| `05_AUTOMATION/build_current_occupants.py` | 6 | no-tracked-copy | Classified outside the retained active set. |
| `05_AUTOMATION/build_dun_app_data.py` | 4 | no-tracked-copy | Classified outside the retained active set. |
| `05_AUTOMATION/build_figures_vdb.py` | 14 | tracked-deferred | `tools/figures/build_figures_vdb.py`; W02 figure/vector conflict. |
| `05_AUTOMATION/build_forecast_trend_chart.py` | 1 | no-tracked-copy | Classified outside the retained active set. |
| `05_AUTOMATION/build_knowledge_graph.py` | 6 | tracked-deferred | `tools/graph/build_knowledge_graph.py`; W02 graph conflict. |
| `05_AUTOMATION/build_narrative_scenarios.py` | 4 | tracked-deferred | `automation/state_analysis/build_narrative_scenarios.py`; deferred figure/graph constants. |
| `05_AUTOMATION/build_parties_vdb.py` | 9 | tracked-deferred | `tools/figures/build_parties_vdb.py`; W02 figure/vector conflict. |
| `05_AUTOMATION/build_personnel_vdb.py` | 10 | tracked-deferred | `tools/figures/build_personnel_vdb.py`; W02 figure/vector conflict. |
| `05_AUTOMATION/build_prn_scenarios.py` | 5 | no-tracked-copy | Classified outside the retained active set. |
| `05_AUTOMATION/build_remaining_vdbs.py` | 7 | tracked-deferred | `tools/figures/build_remaining_vdbs.py`; W02 figure/vector conflict. |
| `05_AUTOMATION/build_state_narrative_scenarios.py` | 3 | tracked-deferred | `automation/state_analysis/build_state_narrative_scenarios.py`; deferred figure constants. |
| `05_AUTOMATION/build_state_prn.py` | 3 | tracked-free | `automation/state_analysis/build_state_prn.py`; canonical 1_DATA input. |
| `05_AUTOMATION/build_state_prn_trend.py` | 1 | tracked-free | `automation/state_analysis/build_state_prn_trend.py`; canonical 1_DATA input. |
| `05_AUTOMATION/build_vec_map.py` | 1 | tracked-deferred | `tools/figures/build_vec_map.py`; W02 vector conflict. |
| `05_AUTOMATION/clean_exco_layers.py` | 1 | no-tracked-copy | Classified outside the retained active set. |
| `05_AUTOMATION/gen_app_data2.py` | 7 | no-tracked-copy | Classified outside the retained active set. |
| `05_AUTOMATION/judge_ge16_news.py` | 2 | no-tracked-copy | Classified outside the retained active set. |
| `05_AUTOMATION/judge_ge16_news_precise.py` | 2 | no-tracked-copy | Classified outside the retained active set. |
| `05_AUTOMATION/judge_ge16_news_self.py` | 2 | no-tracked-copy | Classified outside the retained active set. |
| `05_AUTOMATION/judge_news_baseline120d_parallel.py` | 2 | no-tracked-copy | Classified outside the retained active set. |
| `05_AUTOMATION/judge_news_ge16.py` | 2 | no-tracked-copy | Classified outside the retained active set. |
| `05_AUTOMATION/judge_news_qwen.py` | 1 | no-tracked-copy | Classified outside the retained active set. |
| `05_AUTOMATION/publish_stage_manifests.py` | 7 | no-tracked-copy | Retired legacy staging path; no tracked active copy. |
| `05_AUTOMATION/sarawak_projection.py` | 1 | no-tracked-copy | Classified outside the retained active set. |
| `05_AUTOMATION/stage3b_4_5.py` | 3 | no-tracked-copy | Classified outside the retained active set. |
| `05_AUTOMATION/state_deepdives.py` | 4 | tracked-free | `automation/reports/state_deepdives.py`; canonical 1_DATA input. |
| `05_AUTOMATION/track_ge16_candidates.py` | 1 | tracked-free | `automation/collection/track_ge16_candidates.py`; working tracking output. |
| `05_AUTOMATION/track_ge16_news.py` | 1 | tracked-free | `automation/collection/track_ge16_news.py`; working tracking output. |
| `05_AUTOMATION/track_ge16_polls.py` | 1 | tracked-free | `automation/collection/track_ge16_polls.py`; working tracking output. |
| `05_AUTOMATION/update_parties_from_cron.py` | 2 | tracked-deferred | `tools/figures/update_parties_from_cron.py`; W02 figure/vector conflict. |
| `05_AUTOMATION/update_personnel_from_cron.py` | 3 | tracked-deferred | `tools/figures/update_personnel_from_cron.py`; W02 figure/vector conflict. |

| disposition | files | occurrences |
|---|---:|---:|
| no-tracked-copy | 20 | 65 |
| tracked-free | 8 | 18 |
| tracked-deferred | 10 | 59 |
| **total** | **38** | **142** |

## Residual active-code whitelist

The residual active-code set is exactly the 14 files allowed by
`automation/tests/test_no_local_canonical_reads.py`; each retains only named
deferred constants for the unresolved W02 figure/graph destinations:

- `automation/state_analysis/build_narrative_scenarios.py`
- `automation/state_analysis/build_state_narrative_scenarios.py`
- `tools/figures/build_figures_vdb.py`
- `tools/figures/build_parties_vdb.py`
- `tools/figures/build_personnel_vdb.py`
- `tools/figures/build_remaining_vdbs.py`
- `tools/figures/build_vec_map.py`
- `tools/figures/search_figures.py`
- `tools/figures/search_parties.py`
- `tools/figures/search_personnel.py`
- `tools/figures/update_parties_from_cron.py`
- `tools/figures/update_personnel_from_cron.py`
- `tools/graph/build_knowledge_graph.py`
- `tools/graph/query_graph.py`

## Task 4.5 tracker-root deferral

Batch 4.3 redirected producer `02_FORECAST/engine/common/log_utils.py` to
`work/tracking/`, but `automation/outputs/stage_current_release.py` continued
to read `.tracking/LATEST.json`.  Task 4.5 closes that deferral: the consumer
prefers `work/tracking/LATEST.json`, falls back to `.tracking/LATEST.json` only
for compatibility, and emits a loud warning on that fallback.
