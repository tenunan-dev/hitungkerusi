# Sabah Old→New Seat Delineation Map (2019 redelineation)

Maps the 14 new Sabah DUN seats created by the 2019 delimitation (first contested PRN 26 Sep 2020) back to the 60 pre-2019 seats, so 2018 results can be carried over as **estimates only**.

- **Primary source:** TindakMalaysia `SABAH_2019_DUN_REDELINEATION_IMPACT.csv` (derived from the SPR Sabah redelineation, gazetted/effective 2019) — https://raw.githubusercontent.com/TindakMalaysia/HISTORICAL-ELECTION-RESULTS/main/2020-SABAH-STATE-ELECTIONS/SABAH_2019_DUN_REDELINEATION_IMPACT.csv
- **2018 baselines:** TindakMalaysia `MALAYSIA_2018_DUN_RESULTS.csv` (winner, party, 2018 electorate)
- **Per-seat detail:** en.wikipedia.org `<Name> (state constituency)` articles; polling-district counts from Federal Government Gazette 31 Oct 2022 as cited there
- **Spatial sanity check:** centroid distances computed from `01_RESEARCH/geo/dun/Sabah.geojson` (73 seats); all claimed donor seats sit 0.02–0.76° from the new seat's centroid — consistent with adjacency/carve-out for Sabah's seat scale (typical unrelated pair is several degrees apart).

**Never treat est_* columns as real 2018 results for the new seats** — they are carry-over estimates with the stated confidence.

| New seat | Old seat(s) (2018 codes) | Method | Est. 2018 winner | Est. party/bloc | Confidence | Voters (2018, old) |
|---|---|---|---|---|---|---|
| N.02 Bengkoka | N.01 Banggi, N.03 Pitas | merged-from-two | Mohammad Mohamarin (old Banggi) | WARISAN/WARISAN | low | 28,323 (Banggi 11,054 + Pitas 17,269) |
| N.06 Bandau | N.04 Matunggong, N.05 Tandek | merged-from-two | Julita Mojungki (old Matunggong) | PBS/BN | low | 47,007 (Matunggong 22,906 + Tandek 24,101) |
| N.08 Pintasan | N.06 Tempasuk, N.08 Usukan | merged-from-two | Japlin Akim (old Usukan) | UMNO/BN | low | 39,937 (Tempasuk 19,129 + Usukan 20,808) |
| N.13 Pantai Dalit | N.09 Tamparuli, N.10 Sulaman | merged-from-two | Hajiji Haji Noor (old Sulaman) | UMNO/BN | low | 41,447 (Tamparuli 18,836 + Sulaman 22,611) |
| N.17 Darau | N.12 Karambunai | split-from-one | Azhar Matussin (old Karambunai) | WARISAN/WARISAN | medium | 34,451 (Karambunai 2018 electorate) |
| N.24 Tanjung Keramat | N.17 Tanjong Aru, N.18 Petagas, N.21 Kawang | carved-from-multiple | Wong Hong Jun (old Tanjong Aru) | WARISAN/WARISAN | low | 64,814 (Tanjong Aru 23,829 + Petagas 17,483 + Kawang 23,102) |
| N.27 Limbahau | N.22 Pantai Manis | split-from-one | Aidi Moktar (old Pantai Manis) | WARISAN/WARISAN | medium | 20,484 (Pantai Manis 2018 electorate) |
| N.44 Tulid | N.37 Sook | split-from-one | Ellron Bin Angin (old Sook) | PBRS/BN | medium | 19,121 (Sook 2018 electorate) |
| N.47 Telupid | N.40 Labuk | split-from-one | Abdul Rahman Bin Datuk Hj Kongkawang (old Labuk) | PBS/BN | medium | 16,806 (Labuk 2018 electorate) |
| N.51 Sungai Manila | N.42 Sungai Sibuga | split-from-one | Musa Haji Aman (old Sungai Sibuga) | UMNO/BN | medium | 35,454 (Sungai Sibuga 2018 electorate) |
| N.58 Lamag | N.47 Kuamut, N.48 Sukau | carved-from-multiple | Masiung Banah (old Kuamut) | UPKO/BN | low | 28,730 (Kuamut 17,875 + Sukau 10,855) |
| N.61 Segama | N.49 Tungku, N.50 Lahad Datu | merged-from-two | Dumi Bin Pg Masdal (old Lahad Datu) | WARISAN/WARISAN | low | 46,431 (Tungku 17,773 + Lahad Datu 28,658) |
| N.62 Silam | N.50 Lahad Datu | whole-seat-rename | Dumi Bin Pg Masdal (old Lahad Datu) | WARISAN/WARISAN | high | 28,658 (Lahad Datu 2018 electorate) |
| N.70 Kukusan | N.58 Merotai, N.59 Tanjong Batu, N.60 Sebatik | carved-from-multiple | Hamisa Binti Samat (old Tanjong Batu) | UMNO/BN | low | 52,199 (Merotai 20,634 + Tanjong Batu 23,951 + Sebatik 7,614) |

## Per-seat notes

### N.02 Bengkoka

- **Transfer:** merged-from-two; old seats: N.01 Banggi;N.03 Pitas
- **Polling districts:** SPR gazette (31 Oct 2022): 10 polling districts; no polling-district-level transfer list published in sources consulted
- **Est. 2018 baseline:** Mohammad Mohamarin (old Banggi) (WARISAN, WARISAN) — voter-weighted merge of N.01+N.03 — confidence **low**
- **Notes:** TindakMalaysia/SPR: 'Constituency formed from Banggi and Pitas'. Voter-weighted dominant donor is Pitas (17,269 electors) whose 2018 winner was Bolkiah Ismail (UMNO/BN), but Banggi's Mohammad Mohamarin (WARISAN) was re-elected here in PRN 2020 - estimates carry wide error. Wiki: 're-created from Banggi and Pitas'. Geo centroid check: Pitas 0.13deg, Banggi 0.44deg from Bengkoka centroid - both adjacent/consistent.
- **Source (as of 2026-08-29):** https://raw.githubusercontent.com/TindakMalaysia/HISTORICAL-ELECTION-RESULTS/main/2020-SABAH-STATE-ELECTIONS/SABAH_2019_DUN_REDELINEATION_IMPACT.csv

### N.06 Bandau

- **Transfer:** merged-from-two; old seats: N.04 Matunggong;N.05 Tandek
- **Polling districts:** SPR gazette (31 Oct 2022): 13 polling districts; no polling-district-level transfer list published in sources consulted
- **Est. 2018 baseline:** Julita Mojungki (old Matunggong) (PBS, BN) — voter-weighted merge of N.04+N.05 — confidence **low**
- **Notes:** TindakMalaysia/SPR: 'formed from Matunggong and Tandek'. BOTH donor seats were won by PBS (BN) in 2018, so the bloc estimate (BN) is stronger than the generic low confidence implies; individual winner estimate stays low. Geo check: Tandek 0.13deg, Matunggong 0.25deg - consistent.
- **Source (as of 2026-08-29):** https://raw.githubusercontent.com/TindakMalaysia/HISTORICAL-ELECTION-RESULTS/main/2020-SABAH-STATE-ELECTIONS/SABAH_2019_DUN_REDELINEATION_IMPACT.csv

### N.08 Pintasan

- **Transfer:** merged-from-two; old seats: N.06 Tempasuk;N.08 Usukan
- **Polling districts:** SPR gazette (31 Oct 2022): 10 polling districts; no polling-district-level transfer list published in sources consulted
- **Est. 2018 baseline:** Japlin Akim (old Usukan) (UMNO, BN) — voter-weighted merge of N.06+N.08 — confidence **low**
- **Notes:** TindakMalaysia/SPR: 'formed from Tempasuk and Usukan'. Both donors won by UMNO (BN) in 2018, strengthening the bloc-level estimate. Geo check: Tempasuk 0.16deg, Usukan 0.26deg - consistent.
- **Source (as of 2026-08-29):** https://raw.githubusercontent.com/TindakMalaysia/HISTORICAL-ELECTION-RESULTS/main/2020-SABAH-STATE-ELECTIONS/SABAH_2019_DUN_REDELINEATION_IMPACT.csv

### N.13 Pantai Dalit

- **Transfer:** merged-from-two; old seats: N.09 Tamparuli;N.10 Sulaman
- **Polling districts:** SPR gazette (31 Oct 2022): 7 polling districts; no polling-district-level transfer list published in sources consulted
- **Est. 2018 baseline:** Hajiji Haji Noor (old Sulaman) (UMNO, BN) — voter-weighted merge of N.09+N.10 — confidence **low**
- **Notes:** TindakMalaysia/SPR: 'formed from Tamparuli and Sulaman'; en-wiki Pantai Dalit article additionally lists Kiulu as a donor - treat Kiulu donation as unverified (TindakMalaysia/SPR is primary). Donor winners split: Sulaman UMNO (Hajiji Noor), Tamparuli PBS (Jahid Jahim) - both BN. Geo check: Tamparuli 0.11deg, Sulaman 0.11deg, Kiulu 0.19deg - all adjacent.
- **Source (as of 2026-08-29):** https://raw.githubusercontent.com/TindakMalaysia/HISTORICAL-ELECTION-RESULTS/main/2020-SABAH-STATE-ELECTIONS/SABAH_2019_DUN_REDELINEATION_IMPACT.csv

### N.17 Darau

- **Transfer:** split-from-one; old seats: N.12 Karambunai
- **Polling districts:** SPR gazette (31 Oct 2022): 8 polling districts; Darau carved out of the old Karambunai seat (Karambunai itself 'boundaries were changed')
- **Est. 2018 baseline:** Azhar Matussin (old Karambunai) (WARISAN, WARISAN) — majority of territory from N.12 Karambunai — confidence **medium**
- **Notes:** TindakMalaysia/SPR: 'Constituency formed from Karambunai' - single donor seat. Geo check: Karambunai centroid 0.07deg from Darau - closest pair in the whole map, fully consistent with a carve-out.
- **Source (as of 2026-08-29):** https://raw.githubusercontent.com/TindakMalaysia/HISTORICAL-ELECTION-RESULTS/main/2020-SABAH-STATE-ELECTIONS/SABAH_2019_DUN_REDELINEATION_IMPACT.csv

### N.24 Tanjung Keramat

- **Transfer:** carved-from-multiple; old seats: N.17 Tanjong Aru;N.18 Petagas;N.21 Kawang
- **Polling districts:** SPR gazette (31 Oct 2022): 4 polling districts; no polling-district-level transfer list published in sources consulted
- **Est. 2018 baseline:** Wong Hong Jun (old Tanjong Aru) (WARISAN, WARISAN) — majority of territory from N.17 Tanjong Aru — confidence **low**
- **Notes:** TindakMalaysia/SPR: 'largely formed from formerly named Tanjong Aru and remaining areas from Petagas and Kawang'; en-wiki additionally lists Moyog as a donor (unverified against SPR summary). Tanjong Aru was itself renamed from Tanjong Aru spelling 'Tanjong Aru' to 'Tanjung Aru' in 2019. Geo check: Petagas 0.02deg, Kawang 0.10deg, Tanjung Aru 0.14deg, Moyog 0.14deg - consistent.
- **Source (as of 2026-08-29):** https://raw.githubusercontent.com/TindakMalaysia/HISTORICAL-ELECTION-RESULTS/main/2020-SABAH-STATE-ELECTIONS/SABAH_2019_DUN_REDELINEATION_IMPACT.csv

### N.27 Limbahau

- **Transfer:** split-from-one; old seats: N.22 Pantai Manis
- **Polling districts:** SPR gazette (31 Oct 2022): 9 polling districts; Limbahau carved out of Pantai Manis (Pantai Manis itself 'boundaries were changed')
- **Est. 2018 baseline:** Aidi Moktar (old Pantai Manis) (WARISAN, WARISAN) — majority of territory from N.22 Pantai Manis — confidence **medium**
- **Notes:** TindakMalaysia/SPR: 'Constituency formed from Pantai Manis' - single donor. En-wiki Limbahau article lists donors as Tambunan, Bingkor, Kawang and Pantai Manis - treat extra donors as unverified (SPR summary is primary; Kawang/Bingkor centroids are 0.12/0.28deg away, Tambunan 0.31deg - all geographically plausible but not confirmed). Geo check: Pantai Manis 0.16deg - consistent.
- **Source (as of 2026-08-29):** https://raw.githubusercontent.com/TindakMalaysia/HISTORICAL-ELECTION-RESULTS/main/2020-SABAH-STATE-ELECTIONS/SABAH_2019_DUN_REDELINEATION_IMPACT.csv

### N.44 Tulid

- **Transfer:** split-from-one; old seats: N.37 Sook
- **Polling districts:** SPR gazette (31 Oct 2022): 9 polling districts; Tulid carved out of Sook (Sook itself 'boundaries were changed')
- **Est. 2018 baseline:** Ellron Bin Angin (old Sook) (PBRS, BN) — majority of territory from N.37 Sook — confidence **medium**
- **Notes:** TindakMalaysia/SPR: 'Constituency formed from Sook'. The en-wiki line 'created from Sook, Kuamut and Bingkor' describes the ORIGINAL Tulid seat (1985-2004 history), not the 2019 recreation. Geo check: Sook 0.39deg - consistent (Pensiangan interior seats are large).
- **Source (as of 2026-08-29):** https://raw.githubusercontent.com/TindakMalaysia/HISTORICAL-ELECTION-RESULTS/main/2020-SABAH-STATE-ELECTIONS/SABAH_2019_DUN_REDELINEATION_IMPACT.csv

### N.47 Telupid

- **Transfer:** split-from-one; old seats: N.40 Labuk
- **Polling districts:** SPR gazette (31 Oct 2022): 8 polling districts; Telupid carved out of Labuk (Labuk itself 'boundaries were changed')
- **Est. 2018 baseline:** Abdul Rahman Bin Datuk Hj Kongkawang (old Labuk) (PBS, BN) — majority of territory from N.40 Labuk — confidence **medium**
- **Notes:** TindakMalaysia/SPR and en-wiki agree: 'created from Labuk'. Geo check: Labuk 0.41deg - consistent (large rural seats).
- **Source (as of 2026-08-29):** https://raw.githubusercontent.com/TindakMalaysia/HISTORICAL-ELECTION-RESULTS/main/2020-SABAH-STATE-ELECTIONS/SABAH_2019_DUN_REDELINEATION_IMPACT.csv

### N.51 Sungai Manila

- **Transfer:** split-from-one; old seats: N.42 Sungai Sibuga
- **Polling districts:** SPR gazette (31 Oct 2022): 5 polling districts; Sungai Manila carved out of Sungai Sibuga (Sungai Sibuga itself 'boundaries were changed')
- **Est. 2018 baseline:** Musa Haji Aman (old Sungai Sibuga) (UMNO, BN) — majority of territory from N.42 Sungai Sibuga — confidence **medium**
- **Notes:** TindakMalaysia/SPR: 'Constituency formed from Sungai Sibuga'. En-wiki additionally lists Gum-Gum as a donor (unverified against SPR summary; Gum-Gum centroid 0.15deg away - plausible but not confirmed). Geo check: Sungai Sibuga 0.09deg - consistent.
- **Source (as of 2026-08-29):** https://raw.githubusercontent.com/TindakMalaysia/HISTORICAL-ELECTION-RESULTS/main/2020-SABAH-STATE-ELECTIONS/SABAH_2019_DUN_REDELINEATION_IMPACT.csv

### N.58 Lamag

- **Transfer:** carved-from-multiple; old seats: N.47 Kuamut;N.48 Sukau
- **Polling districts:** SPR gazette (31 Oct 2022): 6 polling districts; no polling-district-level transfer list published in sources consulted
- **Est. 2018 baseline:** Masiung Banah (old Kuamut) (UPKO, BN) — voter-weighted merge of N.47+N.48 — confidence **low**
- **Notes:** TindakMalaysia/SPR: 'largely from Kuamut and remaining from Sukau'. Both donors were BN-won in 2018 (Kuamut: Masiung Banah UPKO; Sukau: Saddi Abdul Rahman UMNO), so bloc estimate BN. Note the OLD 'Lamag' (abolished) is unrelated naming history. Geo check: Kuamut 0.66deg, Sukau 0.64deg - consistent for very large interior Kinabatangan seats.
- **Source (as of 2026-08-29):** https://raw.githubusercontent.com/TindakMalaysia/HISTORICAL-ELECTION-RESULTS/main/2020-SABAH-STATE-ELECTIONS/SABAH_2019_DUN_REDELINEATION_IMPACT.csv

### N.61 Segama

- **Transfer:** merged-from-two; old seats: N.49 Tungku;N.50 Lahad Datu
- **Polling districts:** SPR gazette (31 Oct 2022): 9 polling districts; no polling-district-level transfer list published in sources consulted
- **Est. 2018 baseline:** Dumi Bin Pg Masdal (old Lahad Datu) (WARISAN, WARISAN) — voter-weighted merge of N.49+N.50 — confidence **low**
- **Notes:** TindakMalaysia/SPR: 'formed from formally named constituency of Lahad Datu and Tungku'. Both donors were WARISAN-won in 2018, strengthening the bloc estimate. En-wiki Segama article adds Lahad Datu + Tungku + Sukau as donors - Sukau donation unverified against SPR summary. Geo check: Sukau 0.43deg, Tungku 0.49deg - consistent.
- **Source (as of 2026-08-29):** https://raw.githubusercontent.com/TindakMalaysia/HISTORICAL-ELECTION-RESULTS/main/2020-SABAH-STATE-ELECTIONS/SABAH_2019_DUN_REDELINEATION_IMPACT.csv

### N.62 Silam

- **Transfer:** whole-seat-rename; old seats: N.50 Lahad Datu
- **Polling districts:** SPR gazette (31 Oct 2022): 7 polling districts; seat continuity with old N.50 Lahad Datu (boundaries changed, name changed)
- **Est. 2018 baseline:** Dumi Bin Pg Masdal (old Lahad Datu) (WARISAN, WARISAN) — rename of old seat — confidence **high**
- **Notes:** TindakMalaysia/SPR: old N.50 LAHAD DATU -> new N.62 SILAM with DUN NAME CHANGE = YES; boundaries changed but the seat is the direct successor of Lahad Datu (en-wiki Silam: 'Previously it was named Lahad Datu until a name change'). Carry 2018 Lahad Datu result at high confidence, noting minor boundary adjustments.
- **Source (as of 2026-08-29):** https://raw.githubusercontent.com/TindakMalaysia/HISTORICAL-ELECTION-RESULTS/main/2020-SABAH-STATE-ELECTIONS/SABAH_2019_DUN_REDELINEATION_IMPACT.csv

### N.70 Kukusan

- **Transfer:** carved-from-multiple; old seats: N.58 Merotai;N.59 Tanjong Batu;N.60 Sebatik
- **Polling districts:** SPR gazette (31 Oct 2022): 5 polling districts; no polling-district-level transfer list published in sources consulted
- **Est. 2018 baseline:** Hamisa Binti Samat (old Tanjong Batu) (UMNO, BN) — voter-weighted merge of N.58+N.59+N.60 — confidence **low**
- **Notes:** TindakMalaysia/SPR: 'formed from Merotai, formerly named Tanjong Batu and Sebatik'. Largest donor by voters is Tanjong Batu (UMNO/BN, 23,951); combined WARISAN donors (Merotai) 20,634 - genuinely mixed territory, lowest-confidence estimate in the map (in PRN 2020 WARISAN's Siti Aminah Aching won here). Geo check: Tanjong Batu 0.02deg, Merotai 0.26deg, Sebatik 0.41deg - consistent.
- **Source (as of 2026-08-29):** https://raw.githubusercontent.com/TindakMalaysia/HISTORICAL-ELECTION-RESULTS/main/2020-SABAH-STATE-ELECTIONS/SABAH_2019_DUN_REDELINEATION_IMPACT.csv
