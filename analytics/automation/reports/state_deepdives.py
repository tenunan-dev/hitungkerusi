#!/usr/bin/env python3
# Provenance: original path 05_AUTOMATION/state_deepdives.py; original SHA-256 804741802cfb6f5c737a3ef8bb2d6c9506e9d2db429dc50fd33100e5d246046f; classification active (reports; OPS 8c1852b); versioned 2026-09-11.
"""Build state battleground deep-dive reports from maintained research inputs."""

from pathlib import Path
import sys


def resolve_repository_root(anchor=None):
    """Find the ANALYTICS repository root independently of cwd."""
    return Path(anchor or __file__).resolve().parents[2]


REQUIRED_DATA_RELATIVES = (
    "research/federal/ge16-battleground-seats-master.csv",
    "research/derived/master-list-222-parliamentary-seats.csv",
    "research/derived/voter-demographics-by-constituency-ge15.csv",
    "research/raw/meco-byelections-stats-1957-2026.csv",
)


def canonical_path(*relative_parts):
    path = resolve_repository_root().parent / "1_DATA" / "research"
    path = path.joinpath(*relative_parts)
    if not path.exists():
        raise FileNotFoundError(f"Required canonical DATA input is missing: {path}")
    return path


def main():
    import pandas as pd

    root = resolve_repository_root()
    batt = pd.read_csv(canonical_path("federal", "ge16-battleground-seats-master.csv"))
    master = pd.read_csv(canonical_path("derived", "master-list-222-parliamentary-seats.csv"))
    demog = pd.read_csv(canonical_path("derived", "voter-demographics-by-constituency-ge15.csv"))
    bye = pd.read_csv(canonical_path("raw", "meco-byelections-stats-1957-2026.csv"))

    master['code_n'] = master['code'].astype(str).str.replace('.', '', regex=False)
    demog['code_n'] = demog['code'].astype(str).str.replace('.', '', regex=False)
    batt['code_n'] = batt['code'].astype(str).str.replace('.', '', regex=False)
    bye['seat_code'] = bye['seat'].astype(str).str.extract(r'(P\.?\d{3})')
    bye['code_n'] = bye['seat_code'].astype(str).str.replace('.', '', regex=False)
    recent_bye = bye[pd.to_datetime(bye['date']) >= '2023-01-01'].copy()
    recent_bye = recent_bye.sort_values('date', ascending=False)
    age_map = demog.set_index('code_n')['age60plus_pct'].to_dict()
    batt['age60plus_pct'] = batt['code_n'].map(age_map)

    state_map = {
        'Johor': 'DUN Johor', 'Kedah': 'DUN Kedah', 'Kelantan': 'DUN Kelantan',
        'Malacca': 'DUN Melaka', 'Negeri Sembilan': 'DUN Negeri Sembilan',
        'Pahang': 'DUN Pahang', 'Perak': 'DUN Perak', 'Perlis': 'DUN Perlis',
        'Penang': 'DUN Pulau Pinang', 'Sabah': 'DUN Sabah', 'Sarawak': 'DUN Sarawak',
        'Selangor': 'DUN Selangor', 'Terengganu': 'DUN Terengganu',
    }
    for state, folder in state_map.items():
        st_batt = batt[batt['state_std'] == state].sort_values('margin_pct_valid')
        st_master = master[master['state'] == state]
        st_demog = demog[demog['state'] == state]
        lines = [f'# GE16 Battleground Deep-Dive — {state}', '',
                 f'**State federal seats:** {len(st_master)} | **Battleground seats (<5%):** {len(st_batt)}', '']
        if len(st_batt) == 0:
            lines.extend(['## No battleground seats', f'{state} has no federal seat with a GE15 margin under 5% — all its seats are considered safe for the incumbent blocs.', '', '## State context'])
            bloc = st_master['coalition'].value_counts().to_dict()
            lines.append(f'- Coalition holding: {", ".join(f"{k} {v}" for k, v in bloc.items())}')
            lines.append(f'- Total voters: {st_demog["total_voters"].sum():,.0f}')
            lines.append(f'- Avg Malay %: {st_demog["malay_pct"].mean():.1f}% | Chinese: {st_demog["chinese_pct"].mean():.1f}%')
            output_path = root / "work" / "reports" / "latest" / "states" / folder / "ge16-battleground-deepdive.md"
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text('\n'.join(lines), encoding="utf-8")
            print(f'{folder}: no battlegrounds (deep-dive notes written)')
            continue
        lines.append('## Battleground overview')
        cur = st_batt['current_bloc'].value_counts().to_dict()
        lines.append(f'- Held by (Jun 2026): {", ".join(f"{k} {v}" for k, v in cur.items())}')
        tiers = st_batt['tier'].value_counts().to_dict()
        for tier in ['SUPER-MARGINAL (<1%)', 'HIGH-RISK (1-2.5%)', 'WATCH (2.5-5%)']:
            if tier in tiers:
                lines.append(f'- {tier}: {tiers[tier]}')
        lines.extend(['', '## Seat-by-seat analysis', ''])
        for _, row in st_batt.iterrows():
            by_election = recent_bye[recent_bye['code_n'] == row['code_n']]
            by_election_text = ''
            if len(by_election):
                record = by_election.iloc[0]
                by_election_text = f" By-election {record['date']}: winner majority {record['majority']:,.0f} ({record['majority_perc']:.1f}%), turnout {record['voter_turnout']:.1f}%."
            lines.append(f'### {row["code"]} — {row["constituency"]} ({row["tier"]})')
            lines.append(f'- **GE15 winner:** {row["ge15_bloc"]} (majority {row["majority"]:,.0f} votes, {row["margin_pct_valid"]:.2f}% of valid)')
            lines.append(f'- **Held by (Jun 2026):** {row["current_bloc"]} — {row.get("current_party", "n/a")}, {row.get("current_mp", "n/a")}')
            lines.append(f'- **Electorate:** {row["total_voters"]:,.0f} registered voters (GE15)')
            lines.append(f'- **Demographics:** Malay {row["malay_pct"]}% | Chinese {row["chinese_pct"]}% | Indian {row["indian_pct"]}% | Bumi-Sabah {row.get("bumi_sabah_pct", 0)}% | Bumi-Sarawak {row.get("bumi_sarawak_pct", 0)}%')
            lines.append(f'- **Age:** youth 18-30 = {row["youth_pct"]:.1f}% | 60+ = {row["age60plus_pct"]}% | median {row["median_age"]}')
            if by_election_text:
                lines.append(f'- **By-election signal:**{by_election_text}')
            lines.append('')
        lines.extend(['---', '*Source: GE15 (EC via Wikipedia), ElectionData.MY GE15 voter roll (CC0), MECo by-elections, Parliament of Malaysia (Jun 2026).*'])
        output_path = root / "work" / "reports" / "latest" / "states" / folder / "ge16-battleground-deepdive.md"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text('\n'.join(lines), encoding='utf-8')
        print(f'{folder}: {len(st_batt)} battlegrounds deep-dived')


if __name__ == '__main__':
    if '--help' in sys.argv or '-h' in sys.argv:
        print(__doc__)
    else:
        main()
