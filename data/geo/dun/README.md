# DUN (State Assembly) Boundary GeoJSON

## Source
- **URL:** https://github.com/atifmustaffa/malaysia-geojson — file `malaysia.dun.geojson` (raw: https://raw.githubusercontent.com/atifmustaffa/malaysia-geojson/master/malaysia.dun.geojson)
- **Licence:** MIT (Copyright (c) 2026 Atif Mustaffa) — see repo `LICENSE`.
- **Download date:** 2026-08-29
- Original national file: 613 features (600 DUN seats across the 13 state assemblies + 13 federal-territory placeholder features with no DUN code, excluded here). No simplification was needed — per-state files are all well under the 2 MB target (0.02–0.46 MB); original coordinates retained (unmodified geometry from source).

## Files
One FeatureCollection per state: `<State>.geojson` for
Johor, Kedah, Kelantan, Melaka, Negeri Sembilan, Pahang, Perak, Perlis, Pulau Pinang, Sabah, Sarawak, Selangor, Terengganu.

Every feature's properties: `state` (English state name), `seat` (`N.<nn> <SeatName>`, 2-digit padded seat code — taken verbatim from the source's `dun` field), `seat_name`.

## Per-state coverage (vs `01_RESEARCH/states/DUN <State>/dun-election-results-latest.csv`)

| State | GeoJSON seats | CSV seats | Matched | Unmatched |
|---|---|---|---|---|
| Johor | 56 | 56 | 56 | 0 |
| Kedah | 36 | 36 | 36 | 0 |
| Kelantan | 45 | 45 | 45 | 0 |
| Melaka | 28 | 28 | 28 | 0 |
| Negeri Sembilan | 36 | 36 | 36 | 0 |
| Pahang | 42 | 42 | 42 | 0 |
| Perak | 59 | 59 | 59 | 0 |
| Perlis | 15 | 15 | 15 | 0 |
| Pulau Pinang | 40 | 40 | 40 | 0 |
| Sabah | 73 | 73 | 72 | 1 |
| Sarawak | 82 | 82 | 81 | 1 |
| Selangor | 56 | 56 | 56 | 0 |
| Terengganu | 32 | 32 | 32 | 0 |
| **Total** | **600** | **600** | **598** | **2** |

The 2 unmatched seats are spelling variants only (same seat code, same polygon):
- Sabah `N.22`: geojson `N.22 Tanjong Aru` vs CSV `N.22 Tanjung Aru`
- Sarawak `N.81`: geojson `N.81 Ba\`Kelalan` vs CSV `N.81 Ba'kelalan`

Join on the `N.<nn>` seat code (first token of `seat`) if exact-name joins are needed.


## Post-download reconciliation (29 Aug 2026, Hermes)
- Seat-name spellings normalised to match `dun-election-results-latest.csv`: Sabah N.22 'Tanjong Aru'→'Tanjung Aru'; Sarawak N.81 'Ba`Kelalan'→"Ba'kelalan".
- Verified: 600/600 seat codes match exactly between geo features and results CSVs.


## malaysia-states.geojson (30 Aug 2026)
- 16 features (13 states + KL/Putrajaya/Labuan FT), same MIT source repo as DUN files.
- `properties.state` uses APP/MASTER-DATA keys: `Malacca`, `Penang`, `Kuala Lumpur (FT)`, `Putrajaya (FT)`, `Labuan (FT)`; original name/state_name/state_code kept.
- _provenance at top level; 359 KB original coords (source also offers a .min variant).
