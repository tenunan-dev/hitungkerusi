#!/usr/bin/env python3
# Provenance: original path 05_AUTOMATION/build_boundary_swings.py; original SHA-256 e6700f0e5b58021adeb3d4cd3138b16f5458cbf997196d4d1b3376b943faa012; classification active (high; boundary-state); versioned 2026-09-11.
"""Boundary-grouped swing modulation engine.
For each parliament: state swing × DUN composition factor."""
try:
    import pandas as pd
except ImportError:  # Keep provenance/import inspection independent of optional runtime deps.
    pd = None
import json, os
import warnings
from pathlib import Path
from collections import Counter, defaultdict

def resolve_repository_root(anchor=None):
    """Find the ANALYTICS repository root independently of cwd."""
    anchor_path = Path(anchor or __file__).resolve()
    return anchor_path.parents[2]


ROOT = resolve_repository_root()
DATA_ROOT = ROOT.parent / "1_DATA"
REQUIRED_DATA_RELATIVES = (
    "research/derived/dun_to_parliament_mapping.json",
    "research/derived/swing_se_to_se.csv",
    "research/derived/master-list-222-parliamentary-seats.csv",
    "research/states",
)


def canonical_path(*relative_parts):
    path = DATA_ROOT.joinpath("research", *relative_parts)
    if not path.exists():
        raise FileNotFoundError(f"Required canonical DATA input is missing: {path}")
    return path

def main():
    if pd is None:
        raise RuntimeError("build_boundary_swings requires pandas; install runtime dependencies before building.")
    # 1. Mapping
    with open(canonical_path("derived", "dun_to_parliament_mapping.json")) as f:
        mapping = json.load(f)

    # 2. State swings
    swing_df = pd.read_csv(canonical_path("derived", "swing_se_to_se.csv"))

    # 3. DUN results
    all_dun = []
    states_root = canonical_path("states")
    for state_dir in sorted(os.listdir(states_root)):
        if not state_dir.startswith('DUN '):
            continue
        state_name = state_dir.replace('DUN ', '')
        csv_path = states_root / state_dir / "dun-election-results-latest.csv"
        if not os.path.exists(csv_path):
            warnings.warn(f"Optional canonical DATA input is missing: {csv_path}", RuntimeWarning)
            continue
        df = pd.read_csv(csv_path)
        df['state'] = state_name
        df['dun_code'] = df['seat'].str.extract(r'(N\.\d+)')
        all_dun.append(df)

    dun_df = pd.concat(all_dun, ignore_index=True)
    print(f"DUN results: {len(dun_df)} rows, states: {sorted(dun_df['state'].unique())}")
    # Debug: check extraction
    sample = dun_df['seat'].head(3).tolist()
    print(f"Sample seats: {sample}")
    dun_df['dun_code'] = dun_df['seat'].str.extract(r'(N\.\d+)')
    matched = dun_df['dun_code'].notna().sum()
    print(f"DUN codes extracted: {matched}/{len(dun_df)}")

    # DUN -> bloc map
    dun_bloc = {}
    for _, row in dun_df.iterrows():
        code = row['dun_code']
        if pd.notna(code):
            dun_bloc[code] = row['winner_bloc']
    print(f"DUN bloc map: {len(dun_bloc)} entries")

    # State name normalization for swing matching
    SWING_STATES = {'Pulau Pinang': 'Penang', 'Penang': 'Penang', 'Malacca': 'Malacca', 'Melaka': 'Malacca'}

    # Parliament -> state from master list
    master_df = pd.read_csv(canonical_path("derived", "master-list-222-parliamentary-seats.csv"))
    parl_to_state = {}
    for _, row in master_df.iterrows():
        code = str(row['code']).strip().replace('P', '').replace('p', '')
        p_code = f"P{int(code):03d}"
        st = row['state']
        st = SWING_STATES.get(st, st)
        parl_to_state[p_code] = st

    print(f"Parliament-to-state: {len(parl_to_state)}")

    # 4. Per-parliament DUN VOTE-WEIGHTED bloc shares
    # Instead of counting seats won, aggregate actual VOTES per bloc.
    # For each DUN: winner gets their actual votes; remaining votes distributed
    # proportionally among other blocs based on state-level bloc shares.

    # First, compute state-level bloc vote shares (using winner votes as proxy)
    state_bloc_votes = defaultdict(float)
    state_total_votes = 0
    for _, row in dun_df.iterrows():
        b = row['winner_bloc']
        state_bloc_votes[b] += row['votes']
        state_total_votes += row['votes_valid']

    state_bloc_share_vw = {b: v/state_total_votes for b, v in state_bloc_votes.items()}
    print(f"State-level vote-weighted shares computed for {len(state_bloc_share_vw)} states")

    # Now compute per-parliament estimated bloc vote shares
    parliament_bloc_votes = defaultdict(lambda: defaultdict(float))
    parliament_total = defaultdict(float)

    for m in mapping:
        p = m['parliament']
        dun = m['dun']
        row = dun_df[dun_df['dun_code'] == dun]
        if len(row) == 0:
            continue
        r = row.iloc[0]
        winner = r['winner_bloc']
        winner_votes = r['votes']
        valid = r['votes_valid']

        # Winner gets their actual votes
        parliament_bloc_votes[p][winner] += winner_votes
        parliament_total[p] += valid

        # Distribute remaining votes to other blocs proportionally
        remaining = valid - winner_votes
        # Get state for this parliament
        state = parl_to_state.get(p, '')
        # Get state-level shares for this state from dun_df
        state_rows = dun_df[dun_df['state'] == state]
        state_other_blocs = defaultdict(float)
        state_other_total = 0
        for _, sr in state_rows.iterrows():
            b = sr['winner_bloc']
            if b != winner:
                state_other_blocs[b] += sr['votes']
                state_other_total += sr['votes']

        if state_other_total > 0 and remaining > 0:
            for b, v in state_other_blocs.items():
                parliament_bloc_votes[p][b] += remaining * (v / state_other_total)

    print(f"Parliaments with vote-weighted DUN data: {len(parliament_total)}")

    # 5. Build boundary swings
    rows = []
    for p_code in sorted(parliament_total.keys()):
        state = parl_to_state.get(p_code)
        if not state:
            continue

        state_swings = swing_df[swing_df['state'] == state]
        if len(state_swings) == 0:
            continue

        total_votes_p = parliament_total[p_code]
        if total_votes_p == 0:
            continue

        latest_date = state_swings['latest_date'].iloc[0]
        bloc_votes_p = parliament_bloc_votes[p_code]

        for _, srow in state_swings.iterrows():
            bloc = srow['bloc']
            state_swing = srow['swing_pp']

            # Vote-weighted DUN share for this bloc in this parliament
            dun_votes = bloc_votes_p.get(bloc, 0)
            dun_share = dun_votes / total_votes_p if total_votes_p > 0 else 0

            # Vote-weighted state-level share for this bloc
            state_rows = dun_df[dun_df['state'] == state]
            state_bloc_votes_for_state = state_rows[state_rows['winner_bloc'] == bloc]['votes'].sum()
            state_total_votes_for_state = state_rows['votes_valid'].sum()
            state_bloc_share = state_bloc_votes_for_state / state_total_votes_for_state if state_total_votes_for_state > 0 else 0

            alpha = 0.30
            if state_bloc_share > 0 and dun_share > 0:
                # Cap the ratio to prevent extreme modulation from small-sample blocs
                # (Sabah: 10 blocs/73 DUNs → tiny state_bloc_share → insane ratios)
                ratio = dun_share / state_bloc_share
                ratio = max(0.2, min(5.0, ratio))  # cap at 0.2x–5.0x
                raw = ratio - 1.0
                if state_swing > 0:
                    factor = 1.0 + alpha * raw   # amplify gains in strongholds
                else:
                    factor = 1.0 - alpha * raw   # dampen losses in strongholds
            elif dun_share > 0:
                factor = 1.0 + (alpha if state_swing > 0 else -alpha)
            else:
                factor = 1.0 - (alpha if state_swing > 0 else -alpha)

            # Hard cap on modulation factor to prevent any seat from going wild
            factor = max(0.60, min(1.60, factor))

            modulated_swing = state_swing * factor

            rows.append({
                'parliament': p_code,
                'state': state,
                'bloc': bloc,
                'state_swing_pp': round(state_swing, 2),
                'parl_total_votes': int(total_votes_p),
                'bloc_est_votes': int(dun_votes),
                'bloc_vote_share': round(dun_share, 4),
                'state_bloc_vote_share': round(state_bloc_share, 4),
                'modulation_factor': round(factor, 3),
                'boundary_swing_pp': round(modulated_swing, 2),
                'latest_date': latest_date
            })

    boundary_df = pd.DataFrame(rows)
    print(f"\nGenerated {len(boundary_df)} boundary swing records for {boundary_df['parliament'].nunique()} parliaments")

    # Show key examples
    for p in ['P154', 'P160', 'P032', 'P139']:
        subset = boundary_df[boundary_df['parliament'] == p]
        if len(subset) > 0:
            pname = next((m['parliament_name'] for m in mapping if m['parliament'] == p), p)
            print(f"\n{p} {pname}:")
            for _, r in subset.iterrows():
                arrow = 'UP' if r['boundary_swing_pp'] > r['state_swing_pp'] else ('DN' if r['boundary_swing_pp'] < r['state_swing_pp'] else '--')
                print(f"  {r['bloc']:10s}: {r['state_swing_pp']:+6.1f}pp {arrow} {r['boundary_swing_pp']:+6.1f}pp (vote shr: {r['bloc_vote_share']:.1%}, factor={r['modulation_factor']:.2f})")

    # Save
    out_path = ROOT / "work" / "forecast" / "latest" / "swing_boundary_grouped.csv"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    boundary_df.to_csv(out_path, index=False)
    print(f"\nSaved: {out_path} ({len(boundary_df)} rows)")


if __name__ == "__main__":
    main()
