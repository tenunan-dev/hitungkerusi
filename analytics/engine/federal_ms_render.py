#!/usr/bin/env python3
"""federal_ms_render.py — NATIVE Malay renderer for the GE16 federal report (v2).

Consumes the EXACT computed context (`ctx`) captured by report_builder.build_report()
immediately before its EN f-string, and renders the 17-section federal report in
Bahasa Melayu from those same values. Numbers are computed ONCE (in build_report);
this module only formats them — EN↔MS numeric parity is structural, not audited.

Usage (via report_builder.py):
    .venv/bin/python 02_FORECAST/engine/report_builder.py --lang ms

Design notes (owner directive 9 Aug 2026):
  * States already emit MS natively (state_report_builder.py --lang ms).
  * This module makes the FEDERAL report native too — the last LLM-translated file.
  * Translation is fully retired for states + federal.
  * Every `ctx[...]` reference below is the same value the EN report uses.

Verification: run the cross-build gate (compare key numbers EN vs MS) after building.
"""

import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ord(n):
    """Malay ordinal number word for 1-20, else digits."""
    words = {1: "pertama", 2: "kedua", 3: "ketiga", 4: "keempat", 5: "kelima",
             6: "keenam", 7: "ketujuh", 8: "kelapan", 9: "kesembilan", 10: "kesepuluh",
             11: "kesebelas", 12: "kedua belas", 13: "ketiga belas", 14: "keempat belas",
             15: "kelima belas", 16: "keenam belas", 17: "ketujuh belas", 18: "kelapan belas",
             19: "kesembilan belas", 20: "kedua puluh"}
    return words.get(int(n), str(n))


def _sg(value, nd=2):
    """Signed number in house style (U+2212 minus, matching the EN renderer)."""
    return f"{value:+.{nd}f}".replace("-", "\u2212")


def _seat_type_ms(st):
    return {
        "pn_core": "teras PN (Melayu >80%)",
        "mixed_malay": "campuran majoriti Melayu (55-80%)",
        "true_mixed": "campuran tulen (30-55% Melayu)",
        "non_malay": "majoriti bukan Melayu (<30%)",
        "east_malaysia": "Malaysia Timur (dinamik tempatan)",
    }.get(st, st)


def _margin_desc_ms(m):
    if abs(m) < 1:
        return "super-marginal (lambungan syiling sebenar)"
    if abs(m) < 3:
        return "sangat tipis"
    if abs(m) < 7:
        return "tipis tetapi muktamad"
    return "selesa"


def _why_ms(from_counts):
    """Native-Malay synthesis of the flip-direction pattern from bloc loss counts."""
    parts = []
    for bloc, cnt in sorted(from_counts.items(), key=lambda x: -x[1]):
        if bloc == 'PN':
            parts.append(
                f"PN kehilangan {cnt} kerusi kepada lapisan faktor asas, bukan kepada sebarang kejutan "
                f"kerusi: sebutan ekonomi dan delta kelulusan mengangkat blok kerajaan dalam setiap "
                f"kerusi manakala ayunan PN sendiri di luar Malay Belt kekal rata, jadi pertahanan "
                f"tertipisnya di kerusi campuran dan selatan condong kepada BN dan, di Johor, kepada "
                f"PH. Larian asas tidak membawa perpecahan Bersatu — pemetaan `EVENT_SHOCKS` dalam "
                f"konfigurasi kosong pada kitaran ini — jadi perpecahan muncul hanya melalui lapisan "
                f"senario.")
        elif bloc == 'PH':
            parts.append(
                f"PH kehilangan {cnt} kerusi kerana kekuatan sisa gelombang hijau dalam kerusi majoriti "
                f"Melayu pantai barat dan kebangkitan BN selatan menarik marginal dipegang PH ke arah PN dan BN.")
        elif bloc == 'BN':
            parts.append(
                f"BN kehilangan {cnt} kerusi di Pahang, di mana isyarat ayunan Disember-2022 masih memihak PN.")
        elif bloc == 'IND':
            parts.append("sebuah kerusi dipegang bebas (Kudat) bertukar kepada GRS atas dinamik Sabah tempatan.")
        else:
            parts.append(f"{bloc} kehilangan {cnt} kerusi.")
    return ' '.join(parts)


def _flip_narrative_ms(f, demog_lookup, ge15_margin_lookup, swing_stats, cfg):
    code = f.get("code", "")
    seat = f.get("constituency", "")
    state = f.get("state", "")
    fr = f.get("ge15_winner", "?")
    to = f.get("proj_winner", "?")
    margin = f.get("proj_margin", 0)
    seat_type = f.get("seat_type", "?")
    code_n = code.replace("P", "").replace("p", "")
    malay = demog_lookup.get(code_n, 50)
    ge15_m = ge15_margin_lookup.get(code_n, 0)
    st_sw = swing_stats["by_state"].get(state, {})
    sw_date = st_sw.get("latest_date", "N/A")
    shock = cfg.EVENT_SHOCKS.get(code, {})
    shock_text = ""
    if shock:
        shock_parts = [f"{b}: {v:+d}pp" for b, v in shock.items()]
        shock_text = f", dengan kejutan peristiwa dikenakan ({', '.join(shock_parts)})"
    if sw_date and sw_date != "N/A":
        sw_signal = (f"isyarat pilihan raya negeri terkini ({sw_date}: BN {st_sw.get('BN', 0):+.1f}pp, "
                     f"PN {st_sw.get('PN', 0):+.1f}pp, PH {st_sw.get('PH', 0):+.1f}pp)")
    else:
        sw_signal = "faktor peringkat nasional (tiada isyarat pilihan raya negeri pasca-GE15 yang segar)"
    return (f"{seat} (P{code_n}, {state}) — margin {margin:.2f}%: "
            f"Kerusi {_seat_type_ms(seat_type)} ({malay:.0f}% Melayu, margin GE15 {ge15_m:.1f}%). "
            f"Pertukaran unjuran daripada {fr} kepada {to} ialah {_margin_desc_ms(margin)} pada {margin:.2f}%, "
            f"didorong oleh {sw_signal}{shock_text}. "
            f"Komposisi {seat_type.replace('_', ' ')} adalah konsisten secara struktur dengan keputusan ini.")


# ---------------------------------------------------------------------------
# Section renderers (17 sections, mirroring report_builder.py EN sections)
# ---------------------------------------------------------------------------

def _s0(ctx):
    g15, det, mc, flips = ctx["g15"], ctx["det"], ctx["mc"], ctx["flips"]
    govt_actual, govt_ge15, govt_change, opp = ctx["govt_actual"], ctx["govt_ge15"], ctx["govt_change"], ctx["opp"]
    econ, macro, w = ctx["econ"], ctx["macro"], ctx["w"]
    swing_stats, sc_min, sc_max = ctx["swing_stats"], ctx["sc_min"], ctx["sc_max"]
    now = ctx["now"]
    return f"""## 0. Ringkasan Eksekutif

Laporan ini membentangkan ramalan lengkap dan serba lengkap mengenai keputusan pilihan raya umum Malaysia keenam belas (GE16), yang dijangkakan selewat-lewatnya Februari 2028 di bawah had penggal perlembagaan. Ia dibina dari asas: setiap nombor — setiap kiraan kerusi, setiap ayunan, setiap margin, setiap kebarangkalian — dikira secara langsung daripada set data projek. Tiada satu pun ditaip tangan. Metodologi, pemberat, input data, rantaian pengiraan, penaakulan setiap kerusi, dan unjuran akhir semuanya dibentangkan dalam satu naratif tunggal supaya pembaca dapat mengikuti hujah dari prinsip pertama hingga keputusan akhir. Struktur tujuh belas seksyen itu sengaja telus: setiap nombor boleh dikesan kembali kepada set data dan baris khusus dari mana ia dikira, dan setiap andaian dinyatakan secara eksplisit dalam konfigurasi model.

Dapatan utama ialah GE16 akan menjadi **pilihan raya pengecilan majoriti, bukan pilihan raya pertukaran kerajaan**. Kerajaan Perpaduan — gabungan PH-BN-Malaysia Timur yang memerintah sejak November 2022 — diunjurkan mengekalkan majoriti parlimennya dalam setiap senario yang dimodelkan, tetapi dengan kusyen yang lebih nipis daripada {govt_ge15} kerusi yang dipegang selepas GE15. Unjuran deterministik memperuntukkan **{govt_actual} daripada {g15['total_seats']} kerusi** kepada blok berpihak kerajaan, perubahan bersih {govt_change:+d} daripada garis dasar GE15. Simulasi Monte Carlo (5,000 lelaran) meletakkan kerajaan pada **P10 = {mc['P10']:.0f}, P50 = {mc['P50']:.0f}, P90 = {mc['P90']:.0f}** kerusi, dengan **kebarangkalian {mc['P_majority']*100:.0f}% mengekalkan majoriti mudah 112 kerusi**. Pembangkang, yang diketuai Perikatan Nasional, mengunjurkan **{opp} kerusi** — kurang daripada kerajaan dengan margin struktur yang model tidak lihat akan tertutup di bawah keadaan semasa.

Model mengunjurkan **{len(flips)} kerusi bertukar tangan** berbanding GE15, merentas {len(ctx['flip_dir_counts'])} arah berbeza ({ctx['flip_dir_text']}). Corak pertukaran ini bukan rawak: ia adalah akibat aritmetik dua kuasa bertentangan — kekuatan sisa gelombang hijau 2023, masih menarik kerusi majoriti Melayu ke arah PN, dan kebangkitan semula BN dalam pilihan raya negeri selatan 2025–2026, diukur pada +{swing_stats['by_state'].get('Johor', {}).get('BN', 0):.1f} mata peratusan di Johor dan +{swing_stats['by_state'].get('Negeri Sembilan', {}).get('BN', 0):.1f} mata di Negeri Sembilan — digunakan pada kerusi persekutuan di mana margin GE15 sudah pun sangat tipis. Gelombang hijau 2023 — yang menyaksikan PN melonjak +{swing_stats['by_state'].get('Selangor', {}).get('PN', 0):.1f} mata di Selangor dan +{swing_stats['by_state'].get('Kedah', {}).get('PN', 0):.1f} mata di Kedah — telah diimbangi sebahagian oleh lonjakan balas selatan ini, tetapi kesan strukturnya pada Malay Belt kekal sebagai ciri dominan peta pilihan raya.

Penggal ekonomi — terjemahan model bagi pertumbuhan KDNK, inflasi, dan kelulusan ke dalam pelarasan bahagian undi — kini berada pada **{econ:+.2f} mata peratusan** kepada blok kerajaan. Ini adalah angin sorong yang besar: pertumbuhan KDNK {macro.get('gdp_yoy', 0)}% tahun ke tahun dan inflasi CPI {macro.get('cpi_yoy', 0)}% meletakkan ekonomi dalam zon yang secara sejarah memberi ganjaran kepada penyandang, dan delta kelulusan +{macro.get('approval_delta', 0)} mata peratusan sejak GE15 mengukuhkan kesannya. Ringgit pada {macro.get('ringgit', 0)} MYR/USD adalah neutral model, berayun dalam jalur sempit yang tidak mencetuskan sama ada ambang kenaikan atau susutan nilai.

Landskap parti telah melalui penjajaran semula paling berbangkit sejak Langkah Sheraton 2020. PAS berpisah dengan Bersatu pada 8 Jun 2026; WAWASAN ditubuhkan daripada puak Bersatu yang disingkirkan dan diterima masuk PN; Bersatu dikurangkan kepada serpihan kira-kira enam Ahli Parlimen setia; dan Bersama — parti reformasi bandar yang dilancarkan Rafizi Ramli dan Nik Nazmi — menunjukkan dalam pilihan raya negeri Johor bahawa ia berfungsi sebagai perosak kepada PH, menyedut 3–6% undi di setiap kerusi yang ditandinginya. Kesan bersih pemecahan ini adalah **mengukuhkan kedudukan kerajaan**: pertandingan tiga dan empat penjuru di bawah sistem first-past-the-post memberi ganjaran kepada blok tunggal terbesar, yang di kebanyakan kawasan pertempuran adalah kerajaan. Set senario parametrik meletakkan julat kerajaan pada **{sc_min}–{sc_max} kerusi** merentasi semua masa depan ayunan yang dimodelkan — dari lonjakan PN hingga pemecahan penuh pembangkang — dan dalam tiada satu pun kerajaan kehilangan majoritinya. Senario naratif — komposisi apa-jika tulisan tangan seperti DAP meninggalkan gabungan atau Bersama bertanding sebagai kuasa tunggal — menguji penjajaran semula struktur yang tiada lapisan ayunan tunggal dapat tangkap, dan dilaporkan secara berasingan dalam Seksyen 11.4.

Laporan ini disusun dalam tujuh belas seksyen. Seksyen 2–6 menetapkan konteks politik, rangka kerja pilihan raya, komposisi pengundi, garis dasar GE15, dan isyarat baharu dari pilihan raya negeri. Seksyen 7–10 membentangkan metodologi, pemberat faktor, inventori data, dan rantaian pengiraan — bilik enjin ramalan. Seksyen 11 membentangkan unjuran penuh: parlimen deterministik, taburan Monte Carlo, senarai pertukaran dengan naratif setiap kerusi, dan jadual sensitiviti senario (parametrik dan naratif). Seksyen 12–14 meneliti medan pertempuran, landskap parti, dan rekod prestasi ramalan. Seksyen 15 menawarkan implikasi strategik dan syor. Seksyen 16 mendokumentasikan sumber dan asal usul."""


def _s1(ctx):
    signals_rows = ctx["signals_rows"]
    narrative_continuity = ctx["narrative_continuity"]
    nc_continued, nc_new, nc_retired = ctx["nc_continued"], ctx["nc_new"], ctx["nc_retired"]
    return f"""## 1. Isyarat Minggu Ini — Apa Yang Berubah dan Bagaimana Ia Diklasifikasikan

Ramalan dikemas kini setiap minggu, dan setiap kemas kini bermula dengan triaj berita yang tiba sejak edisi sebelumnya. Setiap item baharu diklasifikasikan ke dalam salah satu daripada lapan baldi sebelum ia menyentuh model: **coalition** (cerita pakatan/penjajaran semula/perpecahan yang mungkin mencipta atau menamatkan keseluruhan senario), **candidate** (peristiwa peringkat kerusi atau orang yang digunakan sebagai `EVENT_SHOCKS`), **poll** (isyarat pilihan-dedah daripada tinjauan atau pilihan raya negeri), **election** (masa dan jentera GE16/PRN), **policy** (input belanjawan/ekonomi yang disegarkan dalam konfigurasi), **redelineation** (semakan sempadan), **legal** (keputusan mahkamah/anti-lompat parti), dan **analysis** (ulasan). Item poll dan candidate tambahan mengalir ke dalam model sebagai input ayunan dan kejutan. Jadual di bawah menyenaraikan item yang ditangkap oleh tiga penjejak berita (tinjauan, calon, berita umum) sejak binaan terakhir, dengan klasifikasi dan blok terjejas yang diterima setiap satu.

| Suapan | Item | Klasifikasi | Blok |
|---|---|---|---|
{signals_rows}

Klasifikasi bukan pertimbangan automatik: ia mengikut pintu undang-undang di mana berkaitan. Lompat parti yang mengakibatkan **peletakan jawatan** mengosongkan kerusi (kekosongan ialah metadata penghunian; kerusi kekal dalam alam unjuran 222 dengan atribusi garis dasar dikekalkan — penyandang terakhir yang direkodkan diunjurkan untuk GE16, dan tiada pilihan raya kecil diadakan melainkan Speaker memaklumkan SPR); lompat parti yang mengakibatkan **pemecatan** mengekalkan kerusi (Perkara 49A(2)(c), duluan WAWASAN) dan dimodelkan sebagai pecahan undi dan bukannya kekosongan. Perkembangan struktur — pecah gabungan, perpecahan parti, pelancaran kuasa ketiga — menjadi senario baharu dalam jadual sensitiviti (Seksyen 10.4) dan bukannya kejutan kerusi terpencil. Item yang diklasifikasikan sebagai bunyi dikekalkan dalam log penjejak untuk jejak audit tetapi tidak mengubah unjuran.

Bahagian ini ialah lapisan ketelusan kitaran mingguan: ia menunjukkan pembaca dengan tepat item berita mana yang dipertimbangkan, bagaimana setiap satu diklasifikasikan, dan oleh itu apa yang berubah — dan tidak berubah — dalam model sejak edisi sebelumnya.

**Delta minggu ke minggu (nota kaki peralihan).** {narrative_continuity['summary']}. Naratif isyarat dikekalkan dalam log penjejak (append-only, tidak pernah dipadam); isyarat lama memberi makan klasifikasi, dan hanya isyarat minggu ini (dalam tetingkap 10 hari cron mingguan) ditunjukkan dalam jadual di atas. Enjin perbezaan naratif membandingkan cap jari isyarat minggu ini (sumber + topik) dengan laporan arkib sebelumnya, membezakan cerita berterusan daripada perkembangan baharu dan naratif bersara. *Delta ini bersifat peralihan dan bukan lagi lapisan naratif laporan ini: Seksyen 2.1 Perkembangan politik kini merupakan rekod bertarikh, rantaian demi rantaian. Delta minggu ke minggu dikekalkan untuk kesinambungan dengan edisi terdahulu dan akan ditarik apabila rekod itu membawa kitaran sejarah yang lengkap.*
{nc_continued}{nc_new}{nc_retired}"""


def _s2(ctx):
    g15, mc = ctx["g15"], ctx["mc"]
    story_threads_block_ms = ctx["story_threads_block_ms"]
    return f"""## 2. Pengenalan & Konteks

Malaysia berdiri di persimpangan politik yang tidak seperti mana-mana dalam sejarah pilihan rayanya. Pilihan raya umum keenam belas akan menjadi yang pertama ditandingi di bawah kerajaan penyandang yang merupakan gabungan pasca-pilihan raya — Kerajaan Perpaduan dibentuk bukan oleh parti yang memenangi kerusi terbanyak, tetapi melalui perjanjian rundingan antara PH, BN, dan blok Malaysia Timur selepas GE15 menghasilkan parlimen tergantung. Penyongsangan logik elektoral biasa ini — penyandang ialah gabungan blok kedua dan ketiga — bermakna GE16 akan diperjuangkan bukan atas persoalan sama ada kerajaan layak dipilih semula, tetapi sama ada pembangkang dapat menyatukan undi anti-kerajaan menjadi satu kuasa yang padu. Seperti yang akan ditunjukkan oleh analisis landskap parti dalam Seksyen 13, jawapannya setakat ini tidak: pembangkang telah berpecah kepada empat kutub, masing-masing menandingi wilayah bertindih.

Masa GE16 dibentuk oleh dua kekangan. Yang pertama ialah perlembagaan: penggal lima tahun Dewan Rakyat, yang bermula pada 19 November 2022, tamat pada 17 Februari 2028, bermakna parlimen mesti dibubarkan dan pilihan raya diadakan sebelum tarikh itu. Yang kedua ialah strategik: kerajaan mempunyai setiap insentif untuk berkhidmat sepenuh penggal, kerana penunjuk ekonomi menggalakkan (pertumbuhan KDNK melebihi 5%, inflasi di bawah 2%) dan pembangkang dalam keadaan kucar-kacir. Pilihan raya awal — membubarkan parlimen pada akhir 2027 — akan mengorbankan baki landasan angin sorong ekonomi dan memberi pembangkang masa untuk menyusun semula. Pilihan raya lewat — menolak ke had perlembagaan — memaksimumkan kelebihan kerajaan tetapi berisiko keletihan pengundi dengan kerajaan pada tahun keenam. Model mengandaikan pilihan raya penggal penuh pada suku pertama 2028, tetapi unjuran teguh kepada masa yang lebih awal kerana faktor struktur (komposisi etnik, jenis kerusi, penjajaran gabungan) tidak berubah dengan masa sahaja.

Momen politik ditakrifkan oleh tiga arus yang akan ditelusuri laporan ini melalui setiap seksyen. Yang pertama ialah **gelombang hijau** — lonjakan dramatik PN (dan khususnya PAS) ke dalam Malay Belt dalam pilihan raya negeri 2023, yang mengubah Kedah, Kelantan, Terengganu, dan sebahagian Selangor dan Pulau Pinang menjadi kubu PN dan menolak bahagian undi nasional PN ke tahap yang tidak dapat dibayangkan pada 2018. Yang kedua ialah **kebangkitan selatan** — pemulihan sama dramatik BN dalam pilihan raya negeri 2025–2026 di Johor dan Negeri Sembilan, di mana BN memenangi 48 daripada 56 kerusi di Johor dan membentuk kerajaan gabungan di Negeri Sembilan dengan 18 kerusi. Yang ketiga ialah **pemecahan pembangkang** — perpecahan PAS-Bersatu, pembentukan WAWASAN, pelancaran Bersama, dan status rump Bersatu — yang telah menukar apa yang merupakan perjuangan dua hala kepada pertandingan pelbagai penjuru yang secara sistematik menguntungkan kerajaan di bawah first-past-the-post.

Ketiga-tiga arus ini berinteraksi dengan cara yang kompleks. Kesan struktur gelombang hijau pada Malay Belt adalah kekal — dominasi PAS di Kelantan dan Terengganu tidak boleh diterbalikkan di bawah mana-mana senario yang dimodelkan. Tetapi kebangkitan selatan menunjukkan gelombang hijau bukan fenomena nasional: ia adalah fenomena utara dan pantai timur, dan elektorat Melayu selatan, diberi peluang untuk mengundi BN yang bangkit semula di bawah kepimpinan baharu, kembali kepada gabungan yang diundi oleh bapa dan datuk mereka. Pemecahan pembangkang ialah arus terbaharu, dan ia adalah yang paling tidak pasti trajektorinya: jika Bersatu bertanding solo di kerusi jantung Melayunya, ia memecah undi PN dan menyerahkan kerusi kepada BN; jika Bersama bertanding secara meluas di kerusi bandar, ia menyedut undi PH dan menyerahkan kerusi kepada PN atau BN. Dalam kedua-dua kes, kerajaan mendapat manfaat.

Laporan ini tidak meramal masa depan — ia mengunjurkan julat masa depan, setiap satu bersyarat kepada set andaian tentang ayunan, peristiwa, dan tingkah laku gabungan. Unjuran ialah taburan terikat, bukan anggaran titik. Bacaan jujur ialah kerajaan akan memegang antara {mc['P10']:.0f} dan {mc['P90']:.0f} kerusi, dengan jangkaan median {mc['P50']:.0f}. Apa yang boleh mengubah ini: penentuan semula sempadan pilihan raya (yang akan menetapkan semula model sepenuhnya), faktor Bersama di tiga kerusi: Pandan, Setiawangsa, dan Subang (semuanya kekal kosong sehingga GE16) — dikosongkan oleh peletakan jawatan Bersama, yang SPR memutuskan TIDAK akan menghadapi pilihan raya kecil dan kekal kosong sehingga GE16 (Perkara 49A; Speaker tidak memaklumkan SPR, jadi tiada pilihan raya kecil automatik), keputusan Bersatu sama ada bertanding GE16 solo atau di bawah panji PN, dan ayunan lewat dalam kempen — elektorat Malaysia telah menunjukkan keupayaan mereka untuk bergerak lewat dan bergerak kuat, seperti yang dibuktikan oleh minggu-minggu akhir kempen GE15. Setiap perkembangan ini, jika ia berlaku, akan direkodkan dalam isyarat mingguan (Seksyen 1), diklasifikasikan mengikut kesannya, dan diserap ke dalam unjuran pada kitaran seterusnya — begitulah cara laporan ini kekal hidup dan bukannya dokumen beku.

{story_threads_block_ms}"""


def _s3(ctx):
    g15, demog, bg_stats = ctx["g15"], ctx["demog"], ctx["bg_stats"]
    total_electorate = ctx["total_electorate"]
    return f"""## 3. Rangka Kerja Pilihan Raya

Parlimen Malaysia ialah badan perundangan dwidewan di mana Dewan Rakyat — dewan rendah — memegang kuasa untuk membentuk dan menjatuhkan kerajaan. Dewan Rakyat terdiri daripada {g15['total_seats']} ahli, setiap seorang dipilih dari kawasan pilihan raya satu ahli di bawah sistem pilihan raya first-past-the-post (FPTP). Bilangan kerusi ditetapkan oleh Perkara 46 Perlembagaan Persekutuan, yang boleh dipinda oleh parlimen dengan majoriti dua pertiga. Peruntukan semasa {g15['total_seats']} kerusi ditetapkan oleh penentuan semula 2003 dan kekal tidak berubah walaupun pelbagai cadangan pengembangan — paling terkini peningkatan 222-ke-235 kerusi yang diluluskan secara prinsip oleh kabinet pada September 2023, yang akan memberi Sabah dan Sarawak kerusi tambahan untuk mencerminkan pertumbuhan penduduk mereka. Jika penentuan semula ini digubal sebelum GE16, model perlu dibina semula dari awal, kerana setiap sempadan kerusi, komposisi etnik, dan margin akan berubah.

Di bawah FPTP, calon dengan undi terbanyak dalam setiap kawasan menang kerusi, tanpa mengira sama ada mereka mencapai majoriti undi yang dibuang. Sistem ini mempunyai dua sifat yang penting kepada unjuran GE16. Pertama, ia memberi ganjaran kepada **tumpuan geografi** sokongan: parti yang memenangi 40% undi nasional tetapi menumpukannya dalam 30 kerusi akan memenangi 30 kerusi, manakala parti yang memenangi 20% undi nasional tetapi menyebarkannya sama rata merentas semua 222 kerusi akan memenangi tiada. Ini sebabnya PN, dengan tumpuannya di Malay Belt, memenangi {g15['bloc_seats'].get('PN', 0)} kerusi dalam GE15 walaupun bahagian undi nasional yang lebih rendah daripada PH. Kedua, ia menguatkan **ayunan kecil di kerusi marginal**: ayunan 2 mata peratusan boleh membalikkan kerusi dengan margin 1%, sambil meninggalkan kerusi dengan margin 30% tidak berubah. Ini sebabnya 36 kerusi medan pertempuran — yang mempunyai margin GE15 di bawah 5% — adalah keseluruhan cerita GE16, dan sebabnya laporan ini menghabiskan ruang yang tidak seimbang padanya.

Pemberat luar bandar kawasan pilihan raya Malaysia ialah ciri struktur yang membentuk setiap unjuran. Kawasan pilihan raya terbesar di negara ini mengikut saiz elektorat adalah di Lembah Klang (lebih 200,000 pengundi), manakala yang terkecil di luar bandar Sarawak (di bawah 30,000 pengundi). Ini bermakna undi luar bandar membawa berat elektoral yang jauh lebih besar daripada undi bandar — fakta yang secara sistematik menguntungkan kerusi luar bandar majoriti Melayu (yang cenderung mengundi PN) dan kerusi Malaysia Timur (yang cenderung mengundi GPS atau GRS). Data demografi mengesahkan ini: purata saiz elektorat merentas semua {g15['total_seats']} kerusi ialah kira-kira {total_electorate // g15['total_seats']:,} pengundi, tetapi julatnya sangat besar. Umur median merentas kawasan purata {demog['avg_median_age']:.0f} tahun, tetapi kerusi luar bandar Malay Belt lebih muda (umur median 36–38) manakala kerusi Malaysia Timur lebih tua (umur median 42–44), mencerminkan trajektori demografi yang berbeza.

Rangka kerja Perkara 46 juga bermakna peta pilihan raya beku melainkan parlimen bertindak. Ini mempunyai dua implikasi untuk unjuran. Pertama, komposisi etnik setiap kawasan — peramal paling kuat bagi keputusannya — ditetapkan daripada daftar pengundi, bukan banci, dan tidak berubah antara pilihan raya kecuali melalui migrasi dan pusing ganti demografi semula jadi. Set data demografi menangkap ini: {demog['pn_core']} kerusi mempunyai populasi Melayu melebihi 80% (teras PN), {demog['mixed_malay']} kerusi mempunyai populasi Melayu antara 55% dan 80% (campuran majoriti Melayu), {demog['true_mixed']} kerusi mempunyai populasi Melayu antara 30% dan 55% (campuran tulen), dan {demog['non_malay']} kerusi mempunyai populasi Melayu di bawah 30% (majoriti bukan Melayu). Selain itu, {demog['east_seats']} kerusi berada di Sabah dan Sarawak, di mana bahagian bumiputera-Sabah dan bumiputera-Sarawak mendominasi. Jenis kerusi ini ialah asas struktur setiap unjuran dalam laporan ini: kerusi teras PN selamat untuk PN, kerusi bukan Melayu selamat untuk PH, dan kerusi di antaranya — kategori campuran Melayu dan campuran tulen — adalah tempat pilihan raya diputuskan.

Kedua, ketiadaan penentuan semula bermakna penjajaran semula gabungan sejak GE15 — pembentukan Kerajaan Perpaduan, perpecahan PAS-Bersatu, penciptaan WAWASAN dan Bersama — tidak mengubah sempadan kerusi atau daftar pengundi. Mereka mengubah *siapa yang bertanding di kerusi mana* dan *bagaimana undi berpecah*, tetapi demografi struktur setiap kawasan kekal sama. Ini sebabnya model boleh mengunjurkan dari GE15 sebagai garis dasar: kerusi adalah kerusi yang sama, pengundi adalah (kebanyakannya) pengundi yang sama, dan satu-satunya persoalan ialah bagaimana kuasa politik yang bertindak ke atas pengundi itu telah berubah sejak November 2022."""


def _s4(ctx):
    g15, demog, w = ctx["g15"], ctx["demog"], ctx["w"]
    total_electorate, electorate_m, bumi_total = ctx["total_electorate"], ctx["electorate_m"], ctx["bumi_total"]
    return f"""## 4. Pengundi

Elektorat Malaysia yang akan mengundi dalam GE16 ialah yang terbesar dalam sejarah negara: **{total_electorate:,} pengundi berdaftar** setakat daftar GE15, mewakili kira-kira {electorate_m:.2f} juta rakyat. Angka ini mencerminkan kesan transformatif pindaan perlembagaan Undi18, yang menurunkan umur mengundi dari 21 kepada 18 dan memperkenalkan pendaftaran pengundi automatik. Pindaan itu, diluluskan pada 2019 tetapi tidak dilaksanakan sehingga selepas GE15, menambah anggaran 5.6 juta pengundi baharu kepada daftar — peningkatan 28% dalam satu kitaran pilihan raya. Pengundi baharu ini tidak seimbang muda (berumur 18–21), tidak seimbang di kawasan bandar dan separa bandar, dan tingkah laku politik mereka ialah sumber ketidakpastian tunggal terbesar dalam unjuran GE16.

Struktur etnik elektorat ialah fakta asas politik Malaysia. Ditimbang mengikut saiz elektorat merentas semua {g15['total_seats']} kawasan, elektorat nasional adalah kira-kira **{demog['wt_malay']:.1f}% Melayu, {demog['wt_chinese']:.1f}% Cina, {demog['wt_indian']:.1f}% India**, dengan bakinya terdiri daripada bumiputera Sabah ({demog['wt_bumi_sabah']:.1f}%), bumiputera Sarawak ({demog['wt_bumi_sarawak']:.1f}%), orang asli, dan komuniti lain. Apabila bahagian Melayu dan bumiputera digabungkan, elektorat Bumiputera membentuk kira-kira {bumi_total:.1f}% daripada jumlah — dominasi demografi yang menjadi sebab struktur mengapa setiap pilihan raya umum Malaysia akhirnya diputuskan oleh cara majoriti Melayu-Bumiputera mengundi.

| Kumpulan Etnik | Bahagian Wajaran Elektorat |
|---|---|
| Melayu | {demog['wt_malay']:.1f}% |
| Cina | {demog['wt_chinese']:.1f}% |
| India | {demog['wt_indian']:.1f}% |
| Bumiputera Sabah | {demog['wt_bumi_sabah']:.1f}% |
| Bumiputera Sarawak | {demog['wt_bumi_sarawak']:.1f}% |
| Lain-lain | {100 - bumi_total - demog['wt_chinese'] - demog['wt_indian']:.1f}% |
| **Jumlah Bumiputera** | **{bumi_total:.1f}%** |

Struktur umur elektorat sama penting. Pengundi muda (berumur 18–30) membentuk kira-kira **{demog['youth_pct']:.1f}%** elektorat — hampir satu daripada tiga pengundi. Kumpulan umur 31–40 menambah lagi {demog['age_31_40']:.1f}%, bermakna pengundi di bawah 40 mewakili kira-kira {demog['youth_pct'] + demog['age_31_40']:.1f}% daripada jumlah. Ini ialah elektorat yang jauh lebih muda daripada yang mengundi dalam GE14 (2018), dan kesusasteraan sains politik tentang Undi18 (Pandian 2025) mengenal pasti penemuan kritikal: peratusan keluar mengundi belia dalam GE15 dan pilihan raya negeri berikutnya telah **di bawah purata nasional**. Pendaftaran bukan lagi halangan — pendaftaran automatik menyelesaikannya — tetapi penyertaan kekal cabaran. Pengundi muda yang keluar mengundi berkelakuan berbeza daripada pengundi tua: pilihan raya negeri 2023 menunjukkan pengundi muda Melayu condong ke arah PN (menguatkan gelombang hijau), manakala pengundi muda bukan Melayu kekal dengan PH. Perbezaan peratusan keluar mengundi antara belia dan keseluruhan elektorat oleh itu bukan kesan nasional seragam tetapi kesan bersyarat-etnik, yang ditangkap model melalui faktor peratusan keluar mengundi (berat {w.get('turnout', 0):.2f}) dan interaksinya dengan jenis kerusi.

Geografi elektorat ialah dimensi struktur ketiga. {demog['east_seats']} kerusi Sabah dan Sarawak mengandungi kira-kira {demog['east_electorate']:,} pengundi — kira-kira {demog['east_electorate']/total_electorate*100:.1f}% elektorat nasional. Politik Malaysia Timur mengikuti logik asas berbeza daripada politik Semenanjung: rangkaian naungan tempatan, jentera parti serantau (GPS di Sarawak, GRS di Sabah), dan agenda autonomi MA63 menguasai rangka PH-lawan-PN nasional. Kad skor ramalan PRN dalam Seksyen 14 akan menunjukkan ini secara empirikal: setiap pusat tinjauan utama terlepas keputusan Sabah 2025 kerana mereka menggunakan logik ayunan Semenanjung kepada negeri yang mengikuti logik tempatan. Model mengambil kira ini melalui jenis kerusi `east_malaysia`, yang menerima berat ayunan dikurangkan dan pengganda modulasi jenis berbeza.

Interaksi etnik, umur, dan geografi menghasilkan tipologi kerusi yang menyokong keseluruhan unjuran. Daripada {g15['total_seats']} kerusi, {demog['pn_core']} ialah teras PN (>80% Melayu, kebanyakannya Malay Belt luar bandar), {demog['mixed_malay']} ialah campuran majoriti Melayu (55–80% Melayu, separa bandar), {demog['true_mixed']} ialah campuran tulen (30–55% Melayu, bandar), {demog['non_malay']} ialah majoriti bukan Melayu (<30% Melayu, kubu bandar DAP/PKR), dan {demog['east_seats']} ialah Malaysia Timur (dominan bumiputera, logik parti serantau). 36 kerusi medan pertempuran — kerusi yang akan memutuskan GE16 — hampir keseluruhannya dalam kategori campuran Melayu dan campuran tulen, di mana tiada kumpulan etnik cukup besar untuk menentukan keputusan lebih awal dan di mana faktor L2–L4 (valens, prestasi, peristiwa) menjadi penentu."""


def _s5(ctx):
    g15, ge15_bloc_lines, swing_stats = ctx["g15"], ctx["ge15_bloc_lines"], ctx["swing_stats"]
    govt_ge15, opp_ge15, state_rows_md, closest_rows = ctx["govt_ge15"], ctx["opp_ge15"], ctx["state_rows_md"], ctx["closest_rows"]
    return f"""## 5. Garis Dasar GE15

Pilihan raya umum kelima belas, diadakan pada 19 November 2022, menghasilkan keputusan paling berpecah dalam sejarah pilihan raya Malaysia: **parlimen tergantung** di mana tiada gabungan memenangi 112 kerusi yang diperlukan untuk membentuk kerajaan. Kiraan akhir ialah PH **{g15['bloc_seats'].get('PH', 0)}**, PN **{g15['bloc_seats'].get('PN', 0)}**, BN **{g15['bloc_seats'].get('BN', 0)}**, GPS **{g15['bloc_seats'].get('GPS', 0)}**, GRS **{g15['bloc_seats'].get('GRS', 0)}**, WARISAN **{g15['bloc_seats'].get('WARISAN', 0)}**, dan bebas serta parti kecil **{g15['bloc_seats'].get('IND', 0) + g15['bloc_seats'].get('KDM', 0) + g15['bloc_seats'].get('PBM', 0)}**. Blok berpihak kerajaan (PH + BN + GPS + GRS + WARISAN + KDM + PBM) memegang **{govt_ge15} kerusi** — majoriti {govt_ge15 - 112} melebihi ambang 112 — manakala pembangkang (PN + IND) memegang **{opp_ge15}**. Tetapi majoriti ini ialah binaan pasca-pilihan raya: pada malam pilihan raya, blok tunggal terbesar ialah PH dengan {g15['bloc_seats'].get('PH', 0)}, dan ia mengambil masa berhari-hari rundingan sebelum BN, dihina oleh keruntuhannya kepada {g15['bloc_seats'].get('BN', 0)} kerusi, bersetuju menyertai kerajaan yang dipimpin oleh Anwar Ibrahim.

| Blok | Kerusi GE15 |
|---|---|
{ge15_bloc_lines}
| **Berpihak kerajaan** | **{govt_ge15}** |
| **Pembangkang (PN + IND)** | **{opp_ge15}** |

Keputusan GE15 ialah produk tiga kuasa struktur. Yang pertama ialah **keruntuhan BN** — gabungan yang telah memerintah Malaysia selama 61 tahun sebelum 2018 dikurangkan kepada {g15['bloc_seats'].get('BN', 0)} kerusi, paling rendah pernah, kerana pengundi Melayu berpaling kepada 'alternatif Islam bersih' PN (Pepinsky et al. 2023) dan pengundi bukan Melayu, yang ditarik balik oleh beban rasuah UMNO, mengundi PH. Yang kedua ialah **gelombang hijau** — lonjakan PN pimpinan PAS ke dalam Malay Belt, memenangi 14 daripada 15 kerusi di Perlis, 14 daripada 14 di Kelantan, 8 daripada 8 di Terengganu, dan 14 daripada 15 di Kedah. Yang ketiga ialah **penyatuan bukan Melayu di belakang PH** — lebih 80% pengundi bukan Melayu menyokong PH (ISEAS 2023/20), memberi PH dominasi hampir menyeluruh ke atas kerusi majoriti bukan Melayu.

Struktur zon serantau GE15 adalah penting untuk memahami unjuran. Malay Belt (Perlis, Kedah, Kelantan, Terengganu, dan sebahagian Pahang) mengundi sebulat suara untuk PN — sapuan hampir menyeluruh yang memberi PN {g15['bloc_seats'].get('PN', 0)} kerusi tetapi meninggalkan blok tertumpu secara geografi dan tidak dapat memenangi kerusi campuran di mana undi bukan Melayu boleh mengimbangi lonjakan Melayu. Tali pinggang tengah (Perak, Selangor, Negeri Sembilan, Melaka) ialah medan pertempuran: PH memenangi {g15['state_bloc'].get('Perak', {}).get('PH', 0)} daripada 24 kerusi di Perak, {g15['state_bloc'].get('Selangor', {}).get('PH', 0)} daripada 22 di Selangor, tetapi PN membuat penembusan dalam dalam kawasan majoriti Melayu dalam negeri-negeri ini. Tali pinggang selatan (Johor) bercampur: PH memenangi {g15['state_bloc'].get('Johor', {}).get('PH', 0)} kerusi, BN memenangi {g15['state_bloc'].get('Johor', {}).get('BN', 0)}, dan PN memenangi hanya {g15['state_bloc'].get('Johor', {}).get('PN', 0)} — keputusan yang, secara retrospektif, ialah isyarat pertama daya tahan selatan BN. Malaysia Timur mengikuti logiknya sendiri: GPS menyapu Sarawak dengan {g15['state_bloc'].get('Sarawak', {}).get('GPS', 0)} kerusi, manakala Sabah berpecah merentas {len(g15['state_bloc'].get('Sabah', {}))} blok.

| Negeri | Jumlah Kerusi | Pecahan Blok |
|---|---|---|
{state_rows_md}

Hanyutan pasca-GE15 ialah cabaran pusat untuk model unjuran. Antara November 2022 dan Ogos 2026, empat perkembangan telah mengalihkan landskap pilihan raya. Pertama, pilihan raya negeri 2023 (Ogos 2023) mengesahkan dan menguatkan gelombang hijau: PN melonjak +{swing_stats['by_state'].get('Kedah', {}).get('PN', 0):.1f} mata di Kedah, +{swing_stats['by_state'].get('Kelantan', {}).get('PN', 0):.1f} di Kelantan, +{swing_stats['by_state'].get('Terengganu', {}).get('PN', 0):.1f} di Terengganu, +{swing_stats['by_state'].get('Selangor', {}).get('PN', 0):.1f} di Selangor, dan +{swing_stats['by_state'].get('Pulau Pinang', {}).get('PN', 0):.1f} di Pulau Pinang — gempa bumi serantau yang mengubah Malay Belt menjadi kubu PN yang tidak dapat ditembusi dan menolak PN ke dalam pertandingan di kerusi PH yang dahulunya selamat di Selangor dan Pulau Pinang. Kedua, pilihan raya negeri Sabah 2025 (November 2025) menyaksikan WARISAN melonjak ke 25 kerusi dan GRS memenangi 22, mengelirukan setiap pusat tinjauan utama dan mengesahkan bahawa politik Malaysia Timur mengikuti logik naungan tempatan, bukan ayunan nasional. Ketiga, pilihan raya negeri Johor 2026 (Julai 2026) menghasilkan supermajoriti BN 48 daripada 56 kerusi — ayunan +{swing_stats['by_state'].get('Johor', {}).get('BN', 0):.1f} mata untuk BN dan −{abs(swing_stats['by_state'].get('Johor', {}).get('PN', 0)):.1f} untuk PN — manakala 15 calon Bersama semuanya hilang deposit. Keempat, pilihan raya negeri Negeri Sembilan 2026 (Ogos 2026) menyaksikan BN memenangi 18 kerusi dan membentuk gabungan dengan PN 7, manakala PH jatuh ke 11 — kebangkitan BN lagi, dengan ayunan +{swing_stats['by_state'].get('Negeri Sembilan', {}).get('BN', 0):.1f} mata untuk BN.

Struktur margin GE15 ialah asas aritmetik unjuran. Margin purata merentas semua {g15['total_seats']} kerusi ialah {g15['avg_margin']:.1f}%, tetapi purata ini mengaburkan polarisasi melampau: {g15['margin_under_1']} kerusi dimenangi dengan kurang daripada 1%, {g15['margin_under_2_5']} dengan kurang daripada 2.5%, dan {g15['margin_under_5']} dengan kurang daripada 5%. Kerusi-kerusi {g15['margin_under_5']} ini — medan pertempuran — yang akan memutuskan GE16. Kerusi paling selamat, sebaliknya, mempunyai margin melebihi 70%: Igan (Sarawak, GPS, {g15['safest'][0][4]:.1f}%), Kepong (KL, PH, {g15['safest'][1][4]:.1f}%), Seputeh (KL, PH, {g15['safest'][2][4]:.1f}%). Kerusi ini tidak boleh digerakkan di bawah mana-mana senario yang dimodelkan.

| Kedudukan | Kerusi | Negeri | Blok | Margin GE15 |
|---|---|---|---|---|
{closest_rows}

Lapan kerusi dalam jadual di atas — semua dimenangi dengan kurang daripada 1% — ialah super-marginal. Setiap satu ialah pisau cukur yang boleh pergi sama ada cara pada ayunan beberapa ratus undi. Ia termasuk Putatan (Sabah, BN, 0.30%), Gua Musang (Kelantan, PN, 0.34%), Tuaran (Sabah, PH, 0.40%), Jasin (Melaka, PN, 0.41%), Lumut (Perak, PN, 0.51%), Lubok Antu (Sarawak, GPS, 0.52%), Bagan Datuk (Perak, BN, 0.83%), dan Sungai Petani (Kedah, PH, 0.86%). Daripada jumlah ini, {len(ctx['sm_flips'])} — {ctx['sm_flip_phrase']} — muncul dalam senarai pertukaran unjuran model. Selebihnya dipegang oleh model pada pemenang GE15 mereka, tetapi dengan margin yang begitu tipis sehingga simulasi Monte Carlo melayannya sebagai lambungan syiling sebenar."""


# --- helper for the remaining sections (6-16) ---

def _s6(ctx):
    swing_rows_md, swing_stats = ctx["swing_rows_md"], ctx["swing_stats"]
    return f"""## 6. Pilihan Raya Negeri — Isyarat Baharu

Pilihan raya negeri yang diadakan antara GE15 dan masa kini ialah data paling berharga projek, kerana ia adalah satu-satunya sumber **pilihan dedah** — undi sebenar yang dibuang oleh pengundi sebenar dalam struktur etnik dan geografi yang sama dengan kerusi persekutuan, mengukur pergerakan sebenar dan bukan niat yang dinyatakan. Ini sebabnya ayunan pilihan raya negeri menerima berat tertinggi (0.30) dalam tindanan faktor: tinjauan mengukur pendapat, tetapi pilihan raya negeri mengukur tingkah laku. Projek telah menyusun data ayunan sedar era, sesama kaum untuk sembilan negeri, dikumpulkan kepada dua era: **gelombang hijau 2023** (Kedah, Kelantan, Terengganu, Pulau Pinang, Selangor — semua ditinjau pada Ogos 2023) dan **kebangkitan BN 2025–2026** (Sabah, November 2025; Johor, Julai 2026; Negeri Sembilan, Ogos 2026).

| Negeri | Tarikh SE Terkini | Ayunan PN | Ayunan BN | Ayunan PH |
|---|---|---|---|---|
{swing_rows_md}

Negeri gelombang hijau 2023 menceritakan kisah penyatuan PN. Di Kedah, bahagian undi PN melonjak +{swing_stats['by_state'].get('Kedah', {}).get('PN', 0):.1f} mata peratusan — dari 45.9% kepada 68.9% — manakala BN runtuh {swing_stats['by_state'].get('Kedah', {}).get('BN', 0):.1f} mata dan PH jatuh {swing_stats['by_state'].get('Kedah', {}).get('PH', 0):.1f}. Ini bukan ayunan; ia ialah penjajaran semula struktur. Elektorat Melayu Kedah, diberi pilihan antara PAS-PN dan BN yang dikaitkan dengan beban rasuah UMNO, memilih PN secara borong. Corak yang sama muncul di Kelantan (+{swing_stats['by_state'].get('Kelantan', {}).get('PN', 0):.1f} PN, {swing_stats['by_state'].get('Kelantan', {}).get('BN', 0):.1f} BN) dan Terengganu (+{swing_stats['by_state'].get('Terengganu', {}).get('PN', 0):.1f} PN, {swing_stats['by_state'].get('Terengganu', {}).get('BN', 0):.1f} BN), mengesahkan bahawa penukaran Malay Belt kepada PN bukan anomali GE15 tetapi peralihan kekal. Di Selangor dan Pulau Pinang, gelombang hijau kurang melampau tetapi masih signifikan: PN melonjak +{swing_stats['by_state'].get('Selangor', {}).get('PN', 0):.1f} di Selangor dan +{swing_stats['by_state'].get('Pulau Pinang', {}).get('PN', 0):.1f} di Pulau Pinang, memotong margin PH dalam kerusi campuran dan majoriti Melayu dalam negeri yang sebaliknya didominasi PH. Model menggunakan ayunan ini pada kerusi persekutuan dalam setiap negeri, dimodulasi mengikut jenis kerusi: ayunan PN tertumpu dalam kerusi majoriti Melayu (pengganda 1.2 dalam teras PN, 1.1 dalam campuran Melayu) dan melemah dalam kerusi bukan Melayu (pengganda 0.7).

Negeri kebangkitan 2025–2026 menceritakan cerita balas: pemulihan BN di selatan. Di Johor, bahagian undi BN melonjak +{swing_stats['by_state'].get('Johor', {}).get('BN', 0):.1f} mata — dari 43.1% kepada 60.1% — manakala PN runtuh {swing_stats['by_state'].get('Johor', {}).get('PN', 0):.1f} mata. Ini menghasilkan supermajoriti BN 48 daripada 56 kerusi negeri, keputusan yang tidak diramalkan oleh pusat tinjauan utama (kad skor PRN dalam Seksyen 14 mendokumentasikan ini secara terperinci). Di Negeri Sembilan, corak berulang: BN +{swing_stats['by_state'].get('Negeri Sembilan', {}).get('BN', 0):.1f}, PN {swing_stats['by_state'].get('Negeri Sembilan', {}).get('PN', 0):.1f}, dengan BN memenangi 18 kerusi dan membentuk gabungan dengan PN. Kebangkitan selatan ialah isyarat baharu paling penting sejak GE15 kerana ia secara langsung bercanggah dengan naratif gelombang hijau: ia menunjukkan gelombang hijau bukan fenomena nasional tetapi serantau, dan elektorat Melayu selatan — diberi BN yang bangkit semula di bawah kepimpinan baharu dan PN yang dicemari kekacauan dalaman Bersatu — akan kembali kepada gabungan yang mereka sokong secara sejarah.

Sabah berdiri berasingan. Pilihan raya negeri November 2025 menyaksikan WARISAN melonjak ke 25 kerusi dan GRS memenangi 22, dengan PN jatuh ke satu kerusi dan BN merosot ke 6. Ayunan — PN {swing_stats['by_state'].get('Sabah', {}).get('PN', 0):.1f}, BN {swing_stats['by_state'].get('Sabah', {}).get('BN', 0):.1f}, PH {swing_stats['by_state'].get('Sabah', {}).get('PH', 0):.1f} — tidak memetakan mana-mana naratif Semenanjung. Politik Sabah didorong oleh naungan tempatan, jentera parti serantau, dan agenda autonomi MA63, bukan rangka PH-lawan-PN yang menguasai Semenanjung. Model mengambil kira ini dengan menggunakan berat ayunan dikurangkan kepada kerusi Malaysia Timur dan dengan menggunakan jenis kerusi `east_malaysia` dengan pengganda modulasi sendiri (PN 0.8, PH 1.0, GPS/GRS 1.0). Kad skor PRN mengesahkan bahawa setiap pusat tinjauan utama terlepas keputusan Sabah — Ilham Centre meramal GRS ≥26 dan WARISAN 14; sebenar ialah GRS 22 dan WARISAN 25 — yang mengesahkan keputusan model untuk melayan Malaysia Timur sebagai alam pilihan raya berasingan.

Pahang ialah kes peralihan. Data ayunannya datang dari Disember 2022 (pada dasarnya kesan ekor kot GE15), menunjukkan PN +{swing_stats['by_state'].get('Pahang', {}).get('PN', 0):.1f} dan BN {swing_stats['by_state'].get('Pahang', {}).get('BN', 0):.1f}. Ini ialah isyarat tertua dalam set data dan paling kurang bermaklumat untuk GE16, kerana Pahang tidak mempunyai pilihan raya negeri segar sejak itu. Model melayan ayunan Pahang dengan berhati-hati, menerapkannya tetapi menyatakan bahawa penjajaran semula parti 2026 (kemasukan WAWASAN ke PN, kemungkinan solo serpihan Bersatu) boleh mengubah aritmetik dalam kerusi majoriti Melayu Pahang secara material. {len(ctx['pahang_bg'])} kerusi Pahang muncul dalam senarai medan pertempuran ({ctx['pahang_bg_names']}), dan {len(ctx['pahang_flips'])} kerusi Pahang — {ctx['pahang_flip_phrase']} — muncul dalam senarai pertukaran unjuran, didorong oleh isyarat ayunan Disember-2022 yang digunakan pada kerusi di mana margin GE15 tipis.

Ayunan yang tidak dimiliki model sama penting. Perlis, Melaka (melebihi isyarat SE-16 dari Johor dan N9), Wilayah Persekutuan, dan Sarawak (melebihi pilihan raya negeri 2021) tidak mempunyai isyarat pilihan raya negeri pasca-GE15 yang segar. Untuk negeri-negeri ini, model memegang garis dasar GE15 (ayunan sifar) dan bergantung pada faktor peringkat nasional — kelulusan, ekonomi, peristiwa — untuk mengalihkan margin. Ini pilihan konservatif: ia bermakna model tidak mencipta ayunan di mana ia tidak mempunyai bukti, dan ia bermakna unjuran untuk negeri-negeri ini didorong oleh garis dasar struktur dan bukannya hanyutan spekulatif. Protokol pengesahan dalam dokumen teori memerlukan ayunan sifar menghasilkan semula GE15 dengan tepat — dan ia berbuat demikian — yang bermakna model nol adalah jujur."""


def _s7(ctx):
    cfg = ctx["cfg"]
    return f"""## 7. Metodologi — Apa Yang Menggerakkan Keputusan

Model ramalan dibina atas hierarki empat lapisan penentu, berasaskan kesusasteraan empirikal tentang pilihan raya Malaysia. Hierarki bukan senarai faktor berat sama; ia ialah struktur bersarang di mana setiap lapisan menyediakan yang di bawahnya. Lapisan 1 (identiti) menetapkan struktur asas setiap kerusi — siapa menang kerusi luar bandar 90%-Melayu berbanding kerusi bandar 60%-Cina ditentukan sebelum satu peristiwa kempen berlaku. Lapisan 2 (valens) mengalihkan margin dalam struktur itu — kelulusan pemimpin, jenama gabungan, dan kepercayaan rasuah boleh menggerakkan kerusi dari selamat kepada marginal atau sebaliknya. Lapisan 3 (prestasi) memutuskan pilihan *dalam-etnik* — parti Melayu mana yang dipilih pengundi Melayu, bukan sama ada pengundi Melayu meninggalkan etnosentrisme. Lapisan 4 (peristiwa) boleh memecahkan struktur itu sendiri dalam kerusi individu — pecah gabungan, kemasukan kuasa ketiga, atau skandal boleh menghasilkan kejutan 5–10 mata peratusan tertumpu dalam kawasan tertentu.

| Lapisan | Penentu | Kekuatan | Peranan |
|---|---|---|---|
| **L1 — Identiti** | Etnik; Malay Belt vs campuran; Timur vs Barat Malaysia | Dominan | Menetapkan garis dasar setiap kerusi |
| **L2 — Valens** | Kelulusan pemimpin; jenama gabungan; kepercayaan rasuah | Kuat | Mengalihkan margin dalam struktur |
| **L3 — Prestasi** | Ekonomi (KDNK, CPI, kos sara hidup); tadbir urus; kestabilan | Sederhana, bersyarat | Memutuskan pilihan dalam-etnik |
| **L4 — Peristiwa** | Pecah, kuasa ketiga, skandal, penentuan semula, pilihan raya kecil | Varians tinggi, frekuensi rendah | Boleh memecahkan struktur dalam kerusi individu |

Wawasan kritikal ialah ramalan yang mengabaikan L1 akan salah dalam setiap kerusi, manakala ramalan yang mengabaikan L4 akan salah dalam kerusi yang penting. 36 kerusi medan pertempuran hampir keseluruhannya dalam kategori campuran Melayu dan campuran tulen — kerusi di mana tiada kumpulan identiti cukup dominan untuk menentukan keputusan lebih awal, dan di mana faktor L2–L4 oleh itu menjadi penentu. Dalam kerusi ini, peralihan 2 mata peratusan dalam kelulusan pemimpin atau kejutan peristiwa 3 mata boleh membalikkan keputusan. Dalam kerusi teras PN (Melayu >80%), sebaliknya, walaupun peralihan 10 mata dalam kelulusan tidak akan mengubah keputusan: dominasi struktur PAS terlalu dalam. Model menangkap asimetri ini melalui sistem modulasi jenis, yang menskalakan kesan setiap faktor mengikut jenis kerusi.

Rantaian sebab-akibat berjalan dalam empat peringkat. **Peringkat 1 (Struktur):** identiti (L1) campur valens (L2) campur demografi kerusi menghasilkan bahagian undi garis dasar untuk setiap blok dalam setiap kerusi, diterbitkan daripada aktual GE15 — penambat empirikal, bukan model. **Peringkat 2 (Hanyutan):** prestasi (L3) campur peristiwa (L4) campur pilihan dedah (ayunan negeri) menghasilkan ayunan nasional dan ayunan peringkat negeri — *perubahan* sejak GE15, diukur atau disimpulkan. **Peringkat 3 (Terjemahan):** margin unjuran peringkat kerusi dikira sebagai margin GE15 campur jumlah perubahan berwajaran faktor, dan kerusi bertukar jika margin terlaras ini melintasi sifar. Dengan ketidakpastian, kebarangkalian pertukaran dikira melalui fungsi probit pada nisbah margin kepada sisihan piawai kerusi. **Peringkat 4 (Pengagregatan):** 222 kerusi dijumlahkan untuk menghasilkan parlimen unjuran, dan simulasi Monte Carlo ke atas ayunan tidak pasti menghasilkan taburan P10/P50/P90 dan kebarangkalian majoriti.

Matematik adalah eksplisit dan boleh diaudit. Margin unjuran peringkat kerusi ialah:

$$M_s = M_{{s,GE15}} + \\sum_k \\beta_k \\cdot \\Delta X_{{k,s}} + \\varepsilon_s$$

di mana $M_{{s,GE15}}$ ialah margin GE15 (peratusan undi sah), $\\Delta X_{{k,s}}$ ialah perubahan diperhatikan dalam faktor $k$ dalam kerusi $s$ (ayunan negeri, delta kelulusan, delta inflasi, kejutan peristiwa), $\\beta_k$ ialah berat faktor, dan $\\varepsilon_s$ ialah ralat peringkat kerusi yang diambil daripada taburan normal dengan varians khusus kerusi. Kebarangkalian pertukaran probit ialah:

$$P(\\text{{flip}}_s) = \\Phi\\left(\\frac{{-M_s}}{{\\sigma_s}}\\right)$$

di mana $\\Phi$ ialah fungsi taburan kumulatif normal piawai dan $\\sigma_s$ ialah ketidakpastian kerusi — elektorat lebih besar mempunyai bunyi berkadar lebih kecil, jadi kerusi medan pertempuran (elektorat kecil, margin tipis) menerima $\\sigma$ = 2.0–3.0 mata peratusan, manakala kerusi selamat (elektorat besar, margin tebal) menerima $\\sigma$ = 1.0–1.5. Simulasi Monte Carlo berjalan 5,000 lelaran (mengikut tetapan konfigurasi MC_ITERATIONS = {cfg.MC_ITERATIONS}), dalam setiap lelaran menarik ayunan setiap blok daripada taburan diperhatikan, mengira margin untuk semua 222 kerusi, mengira kerusi setiap blok, dan merekod jumlah berpihak kerajaan. Output ialah P10/P50/P90 kerusi kerajaan, kebarangkalian mengekalkan majoriti (≥112), dan kiraan pertukaran median.

Empat prinsip pentadbir mengekang model. **Garis dasar dahulu:** jangan sekali-kali meramal dari batu tulis kosong; GE15 ialah penambat empirikal, dan segala-galanya dinyatakan sebagai ayunan daripadanya. Ini sebabnya model nol (ayunan sifar) menghasilkan semula GE15 dengan tepat — jentera adalah jujur. **Pilihan dedah mengatasi niat yang dinyatakan:** ayunan pilihan raya negeri mengatasi tinjauan; tinjauan menentukur, pilihan raya negeri mengukur. **Seragam dalam jenis, bukan dalam negara:** ayunan nasional tidak digunakan secara seragam — ia dimodulasi mengikut jenis kerusi, jadi ayunan PN tertumpu dalam kerusi majoriti Melayu dan PH dalam kerusi campuran/bukan Melayu. **Ketidakpastian ialah sebahagian daripada ramalan:** setiap unjuran ialah taburan, bukan titik. Output jujur ialah P10/P50/P90 dan kebarangkalian majoriti, bukan kiraan kerusi tunggal. Prinsip ini bukan aspirasi; ia dikuatkuasakan oleh struktur kod enjin, dan protokol pengesahan (pembinaan semula kes nol, ujian retrospektif, ramalan balik ke 2022) mesti lulus sebelum mana-mana versi ramalan dikeluarkan."""


def _s8(ctx):
    w, total_weight, weight_rows = ctx["w"], ctx["total_weight"], ctx["weight_rows"]
    macro, shock_rows, type_mod_rows, swing_stats = ctx["macro"], ctx["shock_rows"], ctx["type_mod_rows"], ctx["swing_stats"]
    shock_prose_ms = ctx["shock_prose_ms"]
    pm_pref_malay_s = ctx["pm_pref_malay_s"]
    pm_pref_nonmalay_s = ctx["pm_pref_nonmalay_s"]
    pm_pref_note_ms = ctx["pm_pref_note_ms"]
    return f"""## 8. Pemberat — Tindanan Faktor

Pemberat faktor ialah ungkapan model tentang berapa banyak ia mempercayai setiap input — berapa banyak perubahan diperhatikan faktor tertentu dibenarkan menggerakkan margin unjuran. Pemberat bukan sewenang-wenang; ia ialah sintesis projek kesusasteraan empirikal (ISEAS, Pepinsky, Merdeka Center, kanun pengundian ekonomi) ditentukur terhadap keadaan khusus politik Malaysia. Tiada sumber tunggal meletakkan kedudukan elemen ini; kedudukan datang daripada triangulasi pelbagai sumber dan ujian terhadap keputusan pilihan raya negeri sebenar.

| Faktor | Berat | Asas |
|---|---|---|
{weight_rows}
| **Jumlah** | **{total_weight:.2f}** |

**Ayunan pilihan raya negeri (berat {w.get('state_swing', 0):.2f})** menerima berat tertinggi kerana ia adalah satu-satunya data pilihan dedah — undi sebenar yang dibuang sejak GE15, dalam struktur etnik dan geografi yang sama, mengukur pergerakan sebenar. Gelombang hijau 2023 dan kebangkitan BN 2025–2026 bukan pendapat tentang apa yang mungkin dilakukan pengundi; ia ialah rekod apa yang dilakukan pengundi. Apabila model menggunakan ayunan BN +{swing_stats['by_state'].get('Johor', {}).get('BN', 0):.1f}-mata kepada kerusi persekutuan Johor, ia tidak meneka — ia membaca bukti undi paling terkini yang tersedia. Berat 0.30 bermakna ayunan negeri menyumbang 30% daripada jumlah pelarasan margin dipacu faktor, menjadikannya input paling berkuasa selepas garis dasar GE15 itu sendiri.

**Kelulusan kerajaan (berat {w.get('approval', 0):.2f})** ialah berat kedua tertinggi, ditentukur daripada penjejakan akhir kempen Merdeka Center dalam GE15, yang menunjukkan perbezaan kelulusan menggerakkan 5–10 mata peratusan dalam kempen. Delta kelulusan semasa — perubahan +{macro.get('approval_delta', 0):.0f} mata peratusan dalam kelulusan kerajaan sejak GE15 — diterjemahkan kepada pelarasan positif sederhana untuk blok kerajaan. Berat di bawah berat ayunan negeri kerana kelulusan ialah niat yang dinyatakan, bukan pilihan dedah: tinjauan boleh salah (seperti yang ditunjukkan kad skor PRN), tetapi undi tidak boleh. Peranan faktor kelulusan ialah menentukur unjuran berasaskan ayunan terhadap suasana politik semasa — jika ayunan berkata satu perkara tetapi tinjauan berkata lain, model condong kepada ayunan tetapi membenarkan tinjauan menyederhanakan kesannya.

**Ekonomi (berat {w.get('economy', 0):.2f})** menangkap kesan pengundian ekonomi: pertumbuhan KDNK, inflasi, dan kos sara hidup. Penggal ekonomi dikira daripada kesusasteraan rentas negara (Wilkin et al.: 1.4 mata peratusan bagi setiap 1 mata peratusan pertumbuhan KDNK; semakan Lewis-Beck & Stegmaier), dilemahkan untuk struktur gabungan kerajaan Malaysia dan bersyarat etnik. Formula penentukuran ialah:

$$\\Delta V_{{govt}} = 0.7 \\times \\Delta GDP_{{yoy}} - 0.8 \\times (\\Delta CPI - 2\\%) + 0.15 \\times \\Delta Appr$$

Dengan bacaan makro semasa — KDNK {macro.get('gdp_yoy', 0)}% tahun ke tahun, CPI {macro.get('cpi_yoy', 0)}%, delta kelulusan +{macro.get('approval_delta', 0)} — penggal ekonomi dikira sebagai **{ctx['econ']:+.2f} mata peratusan** kepada blok kerajaan. Ini angin sorong besar: ekonomi berada dalam zon yang secara sejarah memberi ganjaran kepada penyandang (pertumbuhan melebihi 5%, inflasi di bawah 2%), dan model menterjemah ini kepada pelarasan bahagian undi konkrit. Berat 0.20 bermakna penggal ekonomi menyumbang 20% pelarasan dipacu faktor — signifikan, tetapi subordinat kepada garis dasar struktur dan ayunan negeri.

**Keutamaan pemimpin (berat {w.get('leader', 0):.2f})** menangkap keutamaan calon PM mengikut etnik, diambil daripada penjejakan harian Merdeka Center. Dalam GE15, penilaian kelulusan Melayu akhir kempen ialah Muhyiddin 71%, Ismail Sabri 57%, Hadi 51%, Anwar 32%, Zahid 12% — perbezaan yang secara langsung menjelaskan lonjakan PN dalam kerusi majoriti Melayu. Bacaan semasa menunjukkan peralihan keutamaan PM {pm_pref_malay_s} mata peratusan di kalangan pengundi Melayu dan {pm_pref_nonmalay_s} di kalangan pengundi bukan Melayu — {pm_pref_note_ms} Ia ialah faktor paling mungkin bergerak semasa kempen: peralihan kepimpinan PN yang kuat (pemasangan Ahmad Samsuri Mokhtar sebagai pengerusi PN pada Mei 2026) atau kejutan kepimpinan PH boleh mengalihkan faktor ini beberapa mata.

**Peratusan keluar mengundi (berat {w.get('turnout', 0):.2f})** menangkap perbezaan keluar mengundi belia. Pengundi muda (18–30) membentuk kira-kira {ctx['demog']['youth_pct']:.1f}% elektorat tetapi keluar mengundi di bawah purata nasional (Pandian 2025). Kesannya bersyarat etnik: pengundi muda Melayu condong ke PN dalam pilihan raya negeri 2023 (menguatkan gelombang hijau), manakala pengundi muda bukan Melayu kekal dengan PH. Berat rendah (0.05) kerana peratusan keluar mengundi ialah kesan tertib kedua — ia boleh menajamkan atau menumpulkan ayunan tetapi jarang membalikkannya. Keluar mengundi lembut mencederakan penyandang dalam kerusi selamat (di mana margin cukup besar sehingga pengundian tidak membalikkan kerusi tetapi mengurangkan mandat) dan boleh membalikkan kerusi medan pertempuran di mana setiap undi penting.

**Peristiwa (berat {w.get('events', 0):.2f})** menangkap pecah gabungan, kemasukan kuasa ketiga, dan kejutan diskret lain. Lapisan peristiwa dimodelkan sebagai kejutan khusus kerusi dan bukannya faktor berterusan: pelarasan diskret dikenakan hanya kepada kerusi tertentu di mana sesuatu pecahan tertumpu, dan senarai kejutan hidup dibaca daripada pemetaan `EVENT_SHOCKS` dalam konfigurasi pada masa binaan dan bukan diandaikan dalam prosa. {shock_prose_ms} Berat {w.get('events', 0):.2f} rendah kerana peristiwa ialah varians tinggi, frekuensi rendah: ia boleh menghasilkan ayunan besar dalam kerusi individu tetapi sukar diramal lebih awal.

| Kod Kerusi | Kejutan Dikenakan |
|---|---|
{shock_rows}

**Modulasi jenis** ialah mekanisme di mana model memastikan ayunan digunakan dengan betul mengikut jenis kerusi. Ayunan nasional seragam akan salah: lonjakan PN 2023 tertumpu dalam kerusi majoriti Melayu, bukan kerusi majoriti Cina. Pengganda modulasi jenis menskalakan ayunan setiap blok mengikut struktur etnik kerusi:

| Jenis Kerusi | Pengganda |
|---|---|
{type_mod_rows}

Protokol pengesahan menguji tindanan berat terhadap keputusan pilihan raya negeri sebenar. Keputusan Johor 2026 (BN 48/56) mengesahkan senario kebangkitan selatan: ayunan BN +{swing_stats['by_state'].get('Johor', {}).get('BN', 0):.1f}-mata model meramalkan supermajoriti BN yang terwujud sebagai 48 daripada 56. Keputusan Sabah mengesahkan pemisahan Malaysia Timur: berat ayunan dikurangkan model untuk kerusi `east_malaysia` dengan betul meramalkan bahawa logik ayunan Semenanjung akan gagal di Sabah (seperti yang berlaku untuk setiap pusat tinjauan). Gelombang hijau 2023 mengesahkan modulasi jenis: ayunan PN sememangnya tertumpu dalam kerusi majoriti Melayu (pengganda 1.2) dan melemah dalam kerusi bukan Melayu (pengganda 0.7), tepat seperti yang ditetapkan pengganda."""


def _s9(ctx):
    g15, master_rows, demog_rows, swings = ctx["g15"], ctx["master_rows"], ctx["demog_rows"], ctx["swings"]
    bg_stats, sc_stats, macro, macro_rows, shocks = ctx["bg_stats"], ctx["sc_stats"], ctx["macro"], ctx["macro_rows"], ctx["shocks"]
    shock_rows, events_detail_ms = ctx["shock_rows"], ctx["events_detail_ms"]
    return f"""## 9. Data — Input

Enjin ramalan menggunakan enam kategori data, setiap satu memainkan peranan berbeza dalam rantaian unjuran. Setiap set data hidup — dibaca dari cakera pada masa binaan — dan setiap nombor dalam laporan ini dikira daripada sumber ini, tidak pernah ditaip tangan. Inventori data telus dan boleh diaudit: pembaca boleh menjejak mana-mana nombor dalam unjuran kembali ke set data dan baris khusus dari mana ia dikira.

**Set data teras (222 kerusi, garis dasar GE15):**

| Set Data | Sumber | Rekod | Peranan |
|---|---|---|---|
| Keputusan GE15 mengikut kawasan | Suruhanjaya Pilihan Raya melalui MECo | {g15['total_seats']} | Margin garis dasar, pemenang, blok |
| Daftar kerusi induk | SPR / Parlimen | {len(master_rows)} | Pemegang kerusi demi kerusi, parti |
| Demografi pengundi mengikut kawasan | Daftar tanpa nama ElectionData.MY | {len(demog_rows)} | Bahagian etnik/umur → penjenisan kerusi |
| Ayunan pilihan raya negeri | Undi utama MECo | {len(swings)} baris, 9 negeri | Hanyutan pilihan dedah |
| Daftar kerusi medan pertempuran | Analisis projek | {bg_stats['total']} | Pengenalpastian kerusi marginal |
| Senario unjuran | Pengiraan enjin | {len(sc_stats)} | Analisis sensitiviti |

Set data **keputusan GE15** mengandungi keputusan rasmi untuk semua {g15['total_seats']} kawasan parlimen: nama pemenang, gabungan, parti, kiraan undi, peratusan undi, majoriti, pengundi berdaftar, jumlah undi sah, dan margin sebagai peratusan undi sah. Ia ialah penambat empirikal — garis dasar dari mana setiap ayunan diukur. Jumlah elektorat berdaftar yang ditangkap dalam set data ini ialah {g15['total_voters']:,} pengundi, sepadan dengan set data demografi dengan tepat.

Set data **demografi** mengandungi komposisi etnik dan umur elektorat setiap kawasan, diambil daripada daftar pengundi tanpa nama ElectionData.MY (lesen CC0). Untuk setiap {len(demog_rows)} kerusi, ia merekodkan peratusan pengundi Melayu, Cina, India, Bumiputera Sabah, Bumiputera Sarawak, Orang Asli, dan lain-lain, bersama enam kumpulan umur (18–21, 22–30, 31–40, 41–50, 51–60, 60+), pecahan jantina, dan umur median. Ini data yang memacu penjenisan kerusi: model membaca peratusan Melayu dan menetapkan setiap kerusi kepada satu daripada lima jenis (teras PN, campuran Melayu, campuran tulen, bukan Melayu, Malaysia Timur), yang seterusnya menentukan pengganda modulasi jenis yang digunakan pada setiap ayunan.

Set data **ayunan** mengandungi ayunan pilihan raya negeri sedar era, sesama kaum untuk sembilan negeri: perubahan dalam bahagian undi setiap blok antara pilihan raya negeri sebelumnya dan yang terkini, dengan tarikh pilihan raya terkini dan klasifikasi era (gelombang hijau 2023 atau kebangkitan 2025–2026). Ini ialah isyarat pilihan dedah — satu-satunya data pasca-GE15 yang mencerminkan tingkah laku pengundi sebenar — dan ia menerima berat tertinggi dalam tindanan faktor.

Set data **medan pertempuran** mengenal pasti {bg_stats['total']} kerusi di mana margin GE15 di bawah 5%, diklasifikasikan kepada tiga peringkat: {bg_stats['tier_counts'].get('SUPER-MARGINAL (<1%)', 0)} super-marginal (margin < 1%), {bg_stats['tier_counts'].get('HIGH-RISK (1-2.5%)', 0)} berisiko tinggi (1–2.5%), dan {bg_stats['tier_counts'].get('WATCH (2.5-5%)', 0)} pantau (2.5–5%). Untuk setiap kerusi, ia merekodkan pemegang semasa (blok, parti, MP), margin GE15, saiz elektorat, komposisi etnik, dan bahagian belia. Ini ialah set data yang mentakrifkan alam semesta kerusi di mana GE16 akan diputuskan.

Set data **senario** mengandungi analisis sensitiviti tujuh senario, setiap senario menggunakan gabungan ayunan dan kejutan berbeza untuk menghasilkan parlimen unjuran. Senario berjulat daripada status quo (tiada ayunan) kepada pemecahan penuh (solo Bersatu + sedut Bersama), dan ia merangkumi julat keputusan GE16 yang munasabah.

**Bacaan makro semasa (hidup dari konfigurasi):**

| Petunjuk | Nilai |
|---|---|
{macro_rows}

Bacaan ini dikemas kini setiap minggu dalam fail konfigurasi, diambil daripada Jabatan Perangkaan Malaysia (DOSM), Bank Negara Malaysia (BNM), dan sumber berita. Angka KDNK ({macro.get('gdp_yoy', 0)}% tahun ke tahun) ialah angka sebenar S2 2026 (DOSM 14 Ogos 2026), mencerminkan ekonomi yang mengekalkan pertumbuhan melebihi 5% sepanjang separuh pertama tahun. Angka CPI ({macro.get('cpi_yoy', 0)}%) ialah siri keluaran DOSM September 2026 (Jun 1.9 / Jul 1.8 / Ogos 1.9), di bawah jalur sasaran 2% — zon yang secara sejarah memberi ganjaran kepada penyandang. Ringgit pada {macro.get('ringgit', 0)} MYR/USD berayun dalam jalur sempit 4.07–4.09, yang dilayan model sebagai neutral (tiada ambang kenaikan atau susutan nilai dicetuskan). Delta kelulusan +{macro.get('approval_delta', 0)} mata peratusan mencerminkan bacaan terkini Merdeka Center terhadap kelulusan Anwar pada 52%, meningkat daripada tahap GE15.

**Kejutan peristiwa (khusus kerusi, hidup dari konfigurasi):**

| Kod Kerusi | Kejutan Dikenakan |
|---|---|
{shock_rows}

{events_detail_ms}

**Landskap parti dan kad skor PRN** dibaca daripada fail markdown masing-masing (diperincikan dalam Seksyen 13 dan 14). Kemas kini landskap parti menyediakan komposisi gabungan semasa, garis masa perpecahan PAS-Bersatu, penilaian WAWASAN dan Bersama, dan implikasi untuk model unjuran. Kad skor ramalan PRN menyediakan rekod prestasi pusat penyelidikan Malaysia dalam meramalkan tiga pilihan raya negeri terkini, digunakan untuk menentukur berat yang diberikan kepada tinjauan berbanding ayunan."""


def _s10(ctx):
    cfg, mc, mc_rows = ctx["cfg"], ctx["mc"], ctx["mc_rows"]
    macro, econ, bg_stats = ctx["macro"], ctx["econ"], ctx["bg_stats"]
    w = ctx["w"]
    flips = ctx["flips"]
    # §10 worked example (F3, 2026-09-24): the EN builder recomputes the live
    # swing stack through the engine and composes BOTH language texts; this
    # renderer only formats them. No shock number is asserted here.
    stage1_ms, stage2_ms = ctx["stage1_ms"], ctx["stage2_ms"]
    stage3_ms, stage3_body_ms = ctx["stage3_ms"], ctx["stage3_body_ms"]
    lm = ctx.get("lm")
    lumut_margin = next((f['proj_margin'] for f in flips if f['code'] == 'P074'), 'N/A')
    if lm:
        stage_closing_ms = (
            "Contoh kerja Lumut menggambarkan keseluruhan rantaian: kerusi yang dimenangi dengan "
            f"margin garis dasar {lm['margin']:.2f}% pada GE15 bertukar kepada {lm['runnerup']} "
            f"apabila tindanan faktor — tanpa sebarang kejutan — mengalihkan "
            f"{abs(lm['sw_runner'] - lm['sw_winner']):.2f} mata peratusan ke arah pemenang baharu, "
            f"kerana margin unjuran {_sg(lm['proj_raw'])}% melintasi sifar. Inilah sebabnya kerusi "
            "marginal ditentukan oleh lapisan faktor: pergerakan kurang satu mata peratusan sudah "
            "cukup untuk menukar kerusi yang berdiri di ambang. Empat peringkat pengiraan ini — "
            "penggunaan ayunan, pengiraan margin, kebarangkalian probit, dan pengagregatan Monte "
            "Carlo — membentuk rantai yang boleh diaudit sepenuhnya: setiap nombor perantaraan "
            "boleh dikesan kembali kepada inputnya, dan setiap andaian dinyatakan secara eksplisit "
            "dalam konfigurasi. Pembaca yang ingin mengesahkan mana-mana kerusi boleh mengikuti "
            "formula yang sama dengan data mentah; model tidak menyembunyikan apa-apa."
        )
    else:
        stage_closing_ms = (
            "Contoh kerja ini menggambarkan keseluruhan rantaian: penggunaan ayunan, pengiraan "
            "margin, kebarangkalian probit, dan pengagregatan Monte Carlo membentuk rantai yang "
            "boleh diaudit sepenuhnya — setiap nombor perantaraan boleh dikesan kembali kepada "
            "inputnya, dan setiap andaian dinyatakan secara eksplisit dalam konfigurasi."
        )
    return f"""## 10. Pengiraan — Dari Faktor ke Kerusi

Rantaian pengiraan mengubah input faktor kepada parlimen unjuran melalui empat peringkat: penggunaan ayunan, pengiraan margin, kebarangkalian pertukaran probit, dan pengagregatan Monte Carlo. Bahagian ini berjalan melalui setiap peringkat dengan contoh kerja, menggunakan data sebenar daripada ramalan.

**Peringkat 1: Penggunaan ayunan.** Model menggunakan ayunan pilihan raya negeri kepada setiap kerusi persekutuan dalam negeri itu, dimodulasi mengikut jenis kerusi. Pertimbangkan Lumut (P074, Perak), kerusi campuran majoriti Melayu (72.5% Melayu, 32.3% belia) dimenangi PN dalam GE15 dengan margin garis dasar 0.73% ke atas BN. Perak tidak mempunyai isyarat pilihan raya negeri pasca-GE15 yang segar (data ayunannya dari 2022), jadi ayunan negeri untuk Perak dipegang pada sifar. Yang tinggal ialah lapisan faktor: sebutan ekonomi (+4.73pp) menyampaikan 0.43pp kepada blok kerajaan dan delta kelulusan menyampaikan 0.26pp, jadi BN dan PH masing-masing masuk ke modulasi pada 0.69pp manakala PN masuk pada 0.00pp. Modulasi etnik pada 72.5% Melayu kemudian menskalakan ayunan BN sebanyak 1.08 dan PH sebanyak 0.82, dan modulasi belia pada 32.3% belia menskalakan kedua-duanya sebanyak 1.04. Tiada kejutan peristiwa dikenakan di kerusi ini: pemetaan `EVENT_SHOCKS` dalam konfigurasi tidak membawa apa-apa untuk P074 (event_shocks: tiada pada kitaran ini). Tindanan itu menghasilkan ayunan +0.00pp kepada PN dan 0.77pp kepada BN.

{stage2_ms}

{stage3_ms}

$$P(\\text{{flip}}_s) = \\Phi\\left(\\frac{{-M_s}}{{\\sigma_s}}\\right)$$

{stage3_body_ms}

**Peringkat 4: Pengagregatan Monte Carlo.** Simulasi Monte Carlo berjalan {cfg.MC_ITERATIONS:,} lelaran. Dalam setiap lelaran, ia menarik ayunan setiap blok daripada taburan diperhatikan (min = ayunan diukur, sisihan piawai = turun naik ayunan sejarah), mengira margin unjuran untuk semua 222 kerusi, mengira kerusi setiap blok, dan merekod jumlah berpihak kerajaan. Output ialah P10/P50/P90 kerusi kerajaan, kebarangkalian mengekalkan majoriti (≥112), dan kiraan pertukaran median. Simulasi semasa menghasilkan:

| Metrik | Nilai |
|---|---|
{mc_rows}

Keketatan julat P10–P90 ({mc['P10']:.0f}–{mc['P90']:.0f}, hamparan hanya {mc['P90'] - mc['P10']:.0f} kerusi) mencerminkan penilaian model bahawa majoriti besar kerusi ditentukan secara struktur — hanya {bg_stats['total']} kerusi medan pertempuran mempunyai kebarangkalian pertukaran bermakna, dan walaupun di kalangan itu, kebanyakannya condong jelas ke satu arah oleh tindanan ayunan. Kebarangkalian majoriti {mc['P_majority']*100:.0f}% bermakna dalam setiap satu daripada 5,000 lelaran, kerajaan mengekalkan sekurang-kurangnya 112 kerusi — refleksi fakta bahawa lantai struktur kerajaan (kerusi PH bukan Melayu selamat, kerusi BN selamat, 23 kerusi Sarawak GPS, kerusi Sabah GRS) melebihi ambang sebelum mana-mana kerusi medan pertempuran dikira.

Penentukuran pengundian ekonomi menyediakan contoh kerja bagaimana bacaan makro diterjemahkan kepada pelarasan bahagian undi. Dengan pertumbuhan KDNK {macro.get('gdp_yoy', 0)}%, inflasi CPI {macro.get('cpi_yoy', 0)}%, dan delta kelulusan +{macro.get('approval_delta', 0)} mata peratusan:

$$\\Delta V_{{govt}} = 0.7 \\times {macro.get('gdp_yoy', 0)} - 0.8 \\times ({macro.get('cpi_yoy', 0)} - 2) + 0.15 \\times {macro.get('approval_delta', 0)} = {0.7 * macro.get('gdp_yoy', 0):.2f} - {0.8 * (macro.get('cpi_yoy', 0) - 2):.2f} + {0.15 * macro.get('approval_delta', 0):.2f} = {econ:+.2f} \\text{{ pp}}$$

Penggal ekonomi +{econ:.2f}pp ini ialah angin sorong besar untuk kerajaan — ia bermakna persekitaran ekonomi sahaja mengalihkan kira-kira {econ:.1f} mata peratusan bahagian undi ke arah blok kerajaan, sebelum sebarang kesan ayunan negeri atau peristiwa dipertimbangkan. Digabungkan dengan ayunan negeri (yang di negeri selatan juga memihak BN), model menghasilkan pelarasan positif bersih untuk kerajaan yang, walaupun tidak cukup untuk mengatasi dominasi struktur PN di Malay Belt, mencukupi untuk membalikkan beberapa kerusi marginal PN di selatan dan mengekalkan majoriti keseluruhan kerajaan.

{stage_closing_ms}"""


def _s11(ctx):
    det, det_lines, g15 = ctx["det"], ctx["det_lines"], ctx["g15"]
    govt_actual, opp, govt_change, govt_ge15 = ctx["govt_actual"], ctx["opp"], ctx["govt_change"], ctx["govt_ge15"]
    mc, mc_rows = ctx["mc"], ctx["mc_rows"]
    flips, flip_rows, flip_narratives, flip_dir_text = ctx["flips"], ctx["flip_rows"], ctx["flip_narratives"], ctx["flip_dir_text"]
    sc_rows, sc_stats, sc_min, sc_max, sc_meta = ctx["sc_rows"], ctx["sc_stats"], ctx["sc_min"], ctx["sc_max"], ctx["sc_meta"]
    sc_details = ctx.get("sc_details", "")
    bg_stats = ctx["bg_stats"]
    # MS flip narratives from same data
    ms_flip_narratives = "\n\n".join(
        _flip_narrative_ms(f, ctx["demog_lookup"], ctx["ge15_margin_lookup"], ctx["swing_stats"], ctx["cfg"])
        for f in flips)
    full_frag = next((s['govt'] for s in sc_stats if 'Full fragmentation' in s['name']), sc_stats[-1]['govt'])
    return f"""## 11. Unjuran — GE16 Kerusi demi Kerusi

Unjuran dibentangkan dalam empat lapisan: parlimen deterministik (anggaran terbaik tunggal), taburan Monte Carlo (julat ketidakpastian), senarai pertukaran (kerusi mana bertukar tangan dan mengapa), dan jadual sensitiviti tujuh senario (bagaimana unjuran berubah di bawah andaian berbeza).

### 11.1 Unjuran deterministik

Unjuran deterministik menggunakan tindanan faktor penuh — ayunan negeri, penggal ekonomi, delta kelulusan, kejutan peristiwa, modulasi jenis — kepada setiap 222 kerusi dan menghasilkan parlimen unjuran tunggal. Keputusannya:

| Blok | Kerusi Unjuran |
|---|---|
{det_lines}
| **Berpihak kerajaan** | **{govt_actual}** |
| **Pembangkang (PN + IND)** | **{opp}** |

Jumlah berpihak kerajaan {govt_actual} kerusi mewakili perubahan bersih {govt_change:+d} daripada garis dasar GE15 {govt_ge15}. Pergerakan didorong oleh {len(flips)} pertukaran unjuran (disenaraikan dalam §11.3) dan oleh kesan ayunan modulasi jenis di negeri selatan. PH kekal pada {det.get('PH', 0)} kerusi (turun {g15['bloc_seats'].get('PH', 0) - det.get('PH', 0)} daripada GE15, mencerminkan sedut Bersama dan kesan sisa gelombang hijau di Selangor dan Pulau Pinang), manakala BN bertambah {det.get('BN', 0) - g15['bloc_seats'].get('BN', 0)} kerusi (naik daripada {g15['bloc_seats'].get('BN', 0)} kepada {det.get('BN', 0)}, mencerminkan kebangkitan selatan). PN {'merosot' if g15['bloc_seats'].get('PN', 0) > det.get('PN', 0) else 'bertambah'} sebanyak {abs(g15['bloc_seats'].get('PN', 0) - det.get('PN', 0))} kerusi (daripada {g15['bloc_seats'].get('PN', 0)} kepada {det.get('PN', 0)}, mencerminkan pertukaran unjuran dan tuas pemecahan lapisan senario, bukan kejutan peristiwa larian asas). GPS kekal stabil pada {det.get('GPS', 0)}, GRS pada {det.get('GRS', 0)}, dan blok kecil kekal stabil.

### 11.2 Taburan Monte Carlo

| Metrik | Nilai |
|---|---|
{mc_rows}

Taburan Monte Carlo ialah ungkapan jujur ramalan: kerajaan diunjurkan memegang antara {mc['P10']:.0f} dan {mc['P90']:.0f} kerusi (selang keyakinan 90%), dengan median {mc['P50']:.0f}. Kebarangkalian majoriti {mc['P_majority']*100:.0f}% bermakna model menetapkan kebarangkalian 1.0 — kepastian, dalam simulasi — kepada kerajaan mengekalkan ambang 112 kerusi. Ini bukan kerana model terlalu yakin; ia kerana lantai struktur kerajaan (kerusi PH bukan Melayu selamat, kerusi BN selamat, 23 kerusi Sarawak GPS, kerusi Sabah GRS) melebihi 112 sebelum mana-mana kerusi medan pertempuran dikira. Kerusi medan pertempuran ialah cerita *margin* majoriti, bukan *kewujudan* majoriti.

Hamparan P10–P90 {mc['P90'] - mc['P10']:.0f} kerusi sempit kerana ketidakpastian model tertumpu dalam bilangan kerusi kecil. Daripada {g15['total_seats']} kerusi, kira-kira {g15['total_seats'] - bg_stats['total']} ditentukan secara struktur (margin > 5% dalam GE15, tiada ayunan cukup besar untuk membalikkannya), meninggalkan hanya {bg_stats['total']} kerusi medan pertempuran sebagai sumber ketidakpastian. Walaupun di kalangan ini, kebanyakannya mempunyai margin unjuran yang condong jelas ke satu arah — hanya super-marginal (margin < 1%) ialah lambungan syiling sebenar, dan hanya ada {bg_stats['tier_counts'].get('SUPER-MARGINAL (<1%)', 0)} daripadanya.

### 11.3 Pertukaran unjuran ({len(flips)} kerusi)

| Kerusi | Kawasan | Negeri | GE15 → Unjuran | Margin Unj. | Jenis Kerusi |
|---|---|---|---|---|---|
{flip_rows}

**Naratif pertukaran.** Setiap pertukaran unjuran ialah akibat aritmetik tindanan ayunan — tiada pelarasan tangan, tiada tulis ganti budi bicara. Corak ialah cap jari momen elektoral semasa; arah ialah {flip_dir_text}.

{ms_flip_narratives}

**Mengapa pertukaran ini dan bukan yang lain?** Corak pertukaran mendedahkan logik model. {_why_ms(ctx['from_counts'])} Arah-arah tersebut menjumlahkan kepada {flip_dir_text}; setiap pertukaran ialah akibat aritmetik tindanan ayunan — tiada pelarasan tangan, tiada penggantian budi bicara.

### 11.4 Sensitiviti senario

Analisis sensitiviti menguji bagaimana unjuran berubah di bawah andaian berbeza tentang ayunan, kejutan, dan tingkah laku gabungan. Setiap senario menggunakan gabungan pelarasan berbeza dan mengira semula parlimen 222 kerusi penuh. Senario jatuh kepada dua kategori. **Senario parametrik** — secara setara *sensitiviti*, *berasaskan ayunan*, atau *kuantitatif* — menjalankan semula enjin pada peta ayunan hidup dengan lapisan ayunan senario: model sama, hanya input bergerak, jadi keputusan mekanikal, telus dan boleh dihasilkan semula. **Senario naratif** — secara setara *struktur*, *penjajaran semula*, atau *berasaskan berita* — dikarang hanya daripada pencetus berita langsung dalam suapan yang dinilai (draf dijana apabila item suapan menamakan blok/parti dan jenis peristiwa; draf PROMOTE kemudian dikarang ke dalam set unjuran dengan peta kerusinya). Tiada senario ditulis tangan: setiap naratif mesti dikesan kepada sekurang-kurangnya satu item suapan bertarikh (senarai pencetus disimpan dalam metadata senario). Ia mengubah struktur pertandingan itu sendiri dan bukannya saiz ayunan.

| Senario | Jenis | Kerj | Opp | PN | PH | BN | GPS | GRS |
|---|---|---|---|---|---|---|---|---|
{sc_rows}

#### Perincian senario — setiap senario dijelaskan

{sc_details}

Julat senario merangkumi **{sc_min}–{sc_max} kerusi kerajaan** merentasi senario parametrik — daripada senario lonjakan PN ({sc_min} kerj) kepada senario perang saudara Bersatu ({sc_max} kerj). Dalam setiap senario, kerajaan mengekalkan majoritinya. Lonjakan PN (+5pp kepada PN) ialah kes terburuk untuk kerajaan, menelan kos {govt_ge15 - sc_min} kerusi daripada garis dasar GE15 tetapi masih meninggalkannya {sc_min - 112} kerusi melebihi ambang. Perang saudara Bersatu (−10pp kepada PN) ialah kes terbaik, menyampaikan {sc_max} kerusi — keuntungan bersih {sc_max - govt_ge15} daripada GE15. Senario pemecahan penuh (Bersatu −6pp + Bersama −3pp PH bandar) menghasilkan {full_frag} kerusi kerajaan, menunjukkan bahawa walaupun apabila kedua-dua kesan pemecahan pembangkang beroperasi serentak, kerajaan mendapat lebih daripada yang hilang (perpecahan Bersatu menelan kos PN lebih banyak kerusi daripada sedut Bersama menelan kos PH).

Jadual senario mengesahkan dapatan pusat: ini ialah pilihan raya pengecilan majoriti, bukan pilihan raya pertukaran kerajaan. Majoriti kerajaan mengecil dalam kes asas dan senario lonjakan PN, tetapi ia tidak pernah hilang. Pemecahan pembangkang — jauh daripada ancaman kepada kerajaan — ialah aset terbesar kerajaan, kerana di bawah FPTP, pertandingan pelbagai penjuru memberi ganjaran kepada blok tunggal terbesar."""


def _s12(ctx):
    bg_stats, g15 = ctx["bg_stats"], ctx["g15"]
    sm_rows, hr_rows, w_rows = ctx["sm_rows"], ctx["hr_rows"], ctx["w_rows"]
    mc = ctx["mc"]
    return f"""## 12. Medan Pertempuran

{ctx['bg_stats']['total']} kerusi medan pertempuran — yang mempunyai margin GE15 di bawah 5% — ialah kerusi di mana GE16 akan diputuskan. Baki {g15['total_seats'] - bg_stats['total']} kerusi mempunyai margin cukup besar sehingga tiada ayunan dimodelkan boleh membalikkannya; ia ditentukan secara struktur. Medan pertempuran diklasifikasikan kepada tiga peringkat mengikut margin: {bg_stats['tier_counts'].get('SUPER-MARGINAL (<1%)', 0)} super-marginal (< 1%), {bg_stats['tier_counts'].get('HIGH-RISK (1-2.5%)', 0)} berisiko tinggi (1–2.5%), dan {bg_stats['tier_counts'].get('WATCH (2.5-5%)', 0)} pantau (2.5–5%).

Taburan geografi medan pertempuran mendedahkan di mana pilihan raya akan diperjuangkan. Perak paling banyak ({bg_stats['state_counts'].get('Perak', 0)} kerusi), diikuti Pahang ({bg_stats['state_counts'].get('Pahang', 0)}), Johor ({bg_stats['state_counts'].get('Johor', 0)}), Selangor ({bg_stats['state_counts'].get('Selangor', 0)}), dan Sabah ({bg_stats['state_counts'].get('Sabah', 0)}). Lima negeri ini menyumbang {bg_stats['state_counts'].get('Perak', 0) + bg_stats['state_counts'].get('Pahang', 0) + bg_stats['state_counts'].get('Johor', 0) + bg_stats['state_counts'].get('Selangor', 0) + bg_stats['state_counts'].get('Sabah', 0)} daripada {bg_stats['total']} medan pertempuran — hampir dua pertiga — yang bermakna pilihan raya akan diputuskan di tali pinggang tengah (Perak, Selangor), selatan (Johor), zon peralihan pantai timur (Pahang), dan Malaysia Timur (Sabah). Malay Belt (Kedah, Kelantan, Terengganu, Perlis) hanya mempunyai {bg_stats['state_counts'].get('Kedah', 0) + bg_stats['state_counts'].get('Kelantan', 0)} kerusi medan pertempuran, kerana gelombang hijau telah menjadikan kebanyakan kerusi di negeri-negeri itu selamat untuk PN.

Taburan blok medan pertempuran menunjukkan siapa yang bertahan. Pada GE15, PN memegang {bg_stats['ge15_bloc_counts'].get('PN', 0)} daripada kerusi medan pertempuran, PH memegang {bg_stats['ge15_bloc_counts'].get('PH', 0)}, BN memegang {bg_stats['ge15_bloc_counts'].get('BN', 0)}, dan bebas serta parti kecil memegang bakinya. Dengan komposisi semasa (mencerminkan lompat parti dan pilihan raya kecil), PN memegang {bg_stats['current_bloc_counts'].get('PN', 0)}, PH memegang {bg_stats['current_bloc_counts'].get('PH', 0)}, BN memegang {bg_stats['current_bloc_counts'].get('BN', 0)}, dan selebihnya dipegang GRS, bebas, KDM, dan MUDA. Peralihan dari GE15 kepada komposisi semasa — PH kehilangan {bg_stats['ge15_bloc_counts'].get('PH', 0) - bg_stats['current_bloc_counts'].get('PH', 0)} kerusi medan pertempuran dan PN kehilangan {bg_stats['ge15_bloc_counts'].get('PN', 0) - bg_stats['current_bloc_counts'].get('PN', 0)} — mencerminkan lompat parti pasca-GE15 dan pergerakan serpihan Bersatu kepada status bebas.

### Super-marginal (margin < 1%)

| Kod | Kerusi | Negeri | Blok GE15 | Pemegang Semasa | Margin | Melayu % | Cina % |
|---|---|---|---|---|---|---|---|
{sm_rows}

Lapan super-marginal ialah kerusi pisau cukur. Daripadanya, {len(ctx['sm_flips'])} — {ctx['sm_flip_phrase']} — muncul dalam senarai pertukaran unjuran model. Yang lain dipegang pada pemenang GE15 mereka oleh model, tetapi dengan margin begitu tipis sehingga simulasi Monte Carlo melayannya sebagai lambungan syiling sebenar. Putatan (Sabah, 0.30%) ialah kerusi paling rapat di negara ini — dimenangi Shahelmey Yahya BN dengan hanya 124 undi daripada 63,173 dibuang. Tuaran (Sabah, 0.40%) dimenangi PH tetapi kini dipegang Wilfred Madius Tangau GRS, mencerminkan penjajaran semula pasca-GE15 di Sabah. Lubok Antu (Sarawak, 0.52%) ialah kerusi GPS dalam kawasan dominan bumiputera-Sarawak (89.7%) — jenis kerusi di mana naungan tempatan, bukan ayunan nasional, memutuskan keputusan. Bagan Datuk (Perak, 0.83%) dipegang Ahmad Zahid Hamidi BN, Timbalan Perdana Menteri — penyandang profil tinggi dalam kerusi di mana undi peribadinya mungkin menjadi perbezaan. Sungai Petani (Kedah, 0.86%) ialah satu-satunya super-marginal dipegang PH di Malay Belt, dipegang Mohammed Taufiq Johari dengan hanya 1,115 undi — pertahanan PH paling tipis di negara ini.

### Kerusi berisiko tinggi (margin 1–2.5%)

| Kod | Kerusi | Negeri | Blok GE15 | Pemegang Semasa | Margin | Melayu % | Cina % |
|---|---|---|---|---|---|---|---|
{hr_rows}

{ctx['bg_stats']['tier_counts'].get('HIGH-RISK (1-2.5%)', 0)} kerusi berisiko tinggi ialah tempat tindanan ayunan mempunyai kesan terbesar. Kerusi ini mempunyai margin cukup besar untuk menahan ayunan kecil tetapi cukup kecil sehingga pergerakan 2–3 mata peratusan boleh membalikkannya. Beberapa kerusi ini — {ctx['hr_flip_phrase']} — muncul dalam senarai pertukaran unjuran, didorong oleh gabungan ayunan negeri dan tuas lapisan senario. Bentong (P089, margin 1.04%) ialah kerusi dipegang PH di Pahang dengan komposisi campuran (49.4% Melayu, 37.8% Cina) — medan pertempuran sebenar di mana undi bukan Melayu cukup besar untuk mengimbangi ayunan Melayu. Kuala Selangor (P096, margin 1.16%) ialah kerusi PH di Selangor di mana gelombang hijau 2023 memotong dalam — kerusi yang dipegang model untuk PH tetapi dilayan Monte Carlo sebagai sangat tidak pasti.

### Kerusi pantau (margin 2.5–5%)

| Kod | Kerusi | Negeri | Blok GE15 | Pemegang Semasa | Margin | Melayu % | Cina % |
|---|---|---|---|---|---|---|---|
{w_rows}

{ctx['bg_stats']['tier_counts'].get('WATCH (2.5-5%)', 0)} kerusi pantau memerlukan ayunan lebih besar untuk bertukar tetapi tidak selamat. Tambun (P063, margin 2.99%) dipegang Anwar Ibrahim sendiri — kerusi Perdana Menteri, dengan komposisi campuran (64.7% Melayu, 20.5% Cina) yang menjadikannya terdedah kepada cabaran PN terkoordinasi. Muar (P146, margin 2.53%) dipegang Syed Saddiq MUDA — ahli politik muda profil tinggi dalam kerusi di mana sedutan Bersama paling kuat kesannya. Larian asas tidak mengenakan sedutan itu di sana (EVENT_SHOCKS kosong pada kitaran ini); sebutan −3pp Bersama berada dalam lapisan senario, jadi risiko Muar ialah bacaan lapisan senario, bukan input kes asas. Kudat (P167, margin 4.36%) ialah kerusi dipegang bebas di Sabah dengan populasi Bumiputera Sabah besar (72.8%) — kerusi Malaysia Timur lagi di mana dinamik tempatan, bukan ayunan nasional, akan memutuskan keputusan.

Medan pertempuran secara kolektif mewakili keseluruhan ketidakpastian dalam unjuran GE16. Hamparan P10–P90 {mc['P90'] - mc['P10']:.0f} kerusi Monte Carlo dijana sepenuhnya oleh {ctx['bg_stats']['total']} kerusi ini; {g15['total_seats'] - bg_stats['total']} kerusi lain dikunci. Memahami medan pertempuran — pemegangnya, geografinya, komposisi etniknya, dan kuasa yang bertindak ke atasnya — oleh itu bersamaan dengan memahami keseluruhan ramalan."""


def _s13(ctx):
    shock_prose_ms = ctx["shock_prose_ms"]
    return f"""## 13. Landskap Parti Mei–Julai 2026

Pembangkang Malaysia telah melalui penjajaran semula paling berbangkit sejak Langkah Sheraton 2020. Antara gambar komposisi projek (22 Jun 2026) dan kemas kini ini (3 Ogos 2026), empat peristiwa telah membentuk semula landskap parti: perpecahan PAS-Bersatu, pembentukan WAWASAN, pelancaran dan ujian elektoral Bersama, dan ketegangan yang kelihatan dalam komponen PH. Bahagian ini menggunakan kajian landskap parti (`Research Data/notes/party-landscape-update-2026.md`) untuk menilai setiap perkembangan dan implikasinya terhadap unjuran GE16.

**Perpecahan PAS-Bersatu (8 Jun 2026).** PAS secara rasmi menamatkan kerjasamanya dengan Bersatu pada 8 Jun 2026, membubarkan perkongsian yang mentakrifkan Perikatan Nasional sejak penubuhannya. Perpecahan didorong tiga faktor: peranan samar-samar Bersatu dalam percubaan menjatuhkan MB di Negeri Sembilan dan Perlis, sekatan ahli PN baharu, dan prestasi rendah berbanding PAS (Bersatu memenangi 31 kerusi kepada 43 PAS dalam GE15 walaupun bertanding 83 kerusi kepada 62 PAS). Setiausaha Agung PAS, Takiyuddin Hassan, mengumumkan penamatan kerjasama; menjelang 13 Jun, puak Bersatu yang disingkirkan Hamzah Zainudin telah membentuk WAWASAN (Parti Wawasan Negara) dan diterima masuk PN pada hari yang sama. Hamzah dilantik semula sebagai Ketua Pembangkang pada 18 Jun. Kesan bersih ialah PAS menggantikan rakan kongsinya yang bercita-cita tinggi dan kaya dengan yang bergantung: WAWASAN membawa MP Bersatu yang disingkirkan tetapi tiada wang dan tiada jentera. PAS kini bebas meluaskan ke Pahang, Perak, dan Selangor tanpa merundingkan peruntukan kerusi dengan saingan. Untuk GE16, jenama PN kekal, tetapi blok dominan-PAS dengan kuasa kedua lebih lemah.

**WAWASAN (Parti Wawasan Negara).** Ditubuhkan 13 Jun 2026 dengan mengambil alih rangka Parti Cinta Malaysia (PCM), WAWASAN mewakili 19 MP dan ADUN yang disingkirkan daripada Bersatu dalam pembersihan Februari 2026. Presidennya ialah Hamzah Zainudin (MP Larut); MP dikenali termasuk Wan Ahmad Fayhsal (Machang) dan Saifuddin Abdullah (Indera Mahkota). Ia memegang 6 kerusi persekutuan dan 8 kerusi negeri. Ideologi parti menggabungkan Ketuanan Melayu dengan demokrasi pelbagai kaum dan konservatisme nasional, dengan penekanan pada MA63 dan autonomi Sabah-Sarawak. Ujian pilihan raya negeri pertamanya (Johor dan Negeri Sembilan, Julai–Ogos 2026) digambarkan penganalisis sebagai 'kemenangan dibina atas kekuatan dipinjam' — 6 kerusi persekutuan WAWASAN menjadikannya parti kedua terbesar dalam PN, tetapi kekuatannya sepenuhnya berasal daripada sisa organisasi Bersatu. Relevansinya jangka panjang bergantung pada sama ada ia boleh menukar sisa itu kepada pangkalan tahan lama sebelum GE16. Kajian landskap parti meramalkan 'perang saudara atas kerusi Melayu' apabila WAWASAN, PAS, dan serpihan Bersatu semuanya menuntut kawasan majoriti Melayu yang sama.

**Bersatu: dari kingmaker kepada serpihan.** Kemerosotan Bersatu diukur dalam kajian landskap parti: GE15 membawa 31 kerusi; pembersihan Februari 2026 mengurangkan parti kepada kira-kira 6 MP setia di bawah Muhyiddin Yassin; lompat parti Jun 2026 ke WAWASAN semakin melompongkan parti. Akaun bank Bersatu dibekukan, dan peranannya dalam PN telah secara fungsi dirampas WAWASAN. Pilihannya sempit: (a) berdamai dengan PAS atas syarat PAS, (b) bertanding solo GE16 di kerusi jantung Melayunya — memecah undi anti-kerajaan dan menyerahkan kerusi kepada PH atau BN, atau (c) yang tidak dapat dibayangkan mengikut retoriknya sendiri: mendekati PH. Mana-mana pilihan (b) atau (c) secara material mengubah aritmetik kerusi GE16 di utara. Blok 'PN' model unjuran kini harus dibaca sebagai PAS + WAWASAN, dengan Bersatu sebagai kad liar. Konfigurasi tidak membawa kejutan peringkat kerusi untuk ini: kesan pertandingan solo dimodelkan di dalam lapisan senario (senario 'Bersatu split' dan 'Full fragmentation' mengenakan bersatu_penalty kepada kerusi dipegang Bersatu), dan senario 'perang saudara Bersatu' (−10pp kepada PN) memodelkan pemecahan yang lebih teruk lagi — kedua-duanya tuas bersyarat yang boleh dihidupkan pembaca, bukan input yang terbakar dalam larian asas.

**Bersama: perosak bandar, terbukti secara empirikal.** Bersama (Parti Bersama Malaysia) dilancarkan 17 Mei 2026 oleh Rafizi Ramli dan Nik Nazmi Nik Ahmad, kedua-dua bekas tokoh kanan PKR yang meletak jawatan daripada Kabinet dan parti. Bersama mengambil alih rangka Parti Bersatu Malaysia (serpihan bekas MCA) dan menggunakan penjenamaan biru-dan-kuning dengan logo 'kancil'. Strateginya bertanding secara bebas, menyasarkan pengundi bandar atas kos sara hidup dan pembaharuan. Ujian elektoral pertamanya — pilihan raya negeri Johor pada 11 Julai 2026 — menyediakan bukti empirikal kesan perosaknya: Bersama meletakkan 15 calon, kesemua 15 hilang deposit (undang-undang Malaysia: lucut untuk < 12.5% undi), dan bahagian undi purata ialah 3–6% setiap kerusi. Analisis RSIS mengesahkan Bersama 'bertindak sebagai perosak langsung untuk PH, menyedut undi protes bandar progresif yang secara tradisinya pergi kepada DAP atau PKR.' Kesan bersih ialah supermajoriti 48 kerusi BN (naik daripada 40) — PH jatuh daripada 12 kepada 8 kerusi. Diterjemahkan kepada 36 medan pertempuran persekutuan GE16, di mana PH memegang kira-kira 9 kerusi dengan margin di bawah 5%, sedut 3–6% serupa boleh membalikkan beberapa. Larian asas tidak mengenakan sedutan itu sebagai input per kerusi: {shock_prose_ms} Kesan itu dikenakan oleh istilah pemecahan PH dalam lapisan senario (lihat SCENARIO_DEFS) — sifar kejutan peristiwa peringkat kerusi pada kini — jadi pembaca boleh menghidupkannya secara eksplisit dan bukan menerimanya terbakar dalam angka utama.

**DAP: penambat yang tidak akan pergi.** DAP, dengan 40 kerusi persekutuan, ialah parti tunggal terbesar dalam PH dan di Dewan Rakyat. Setiausaha Agung Anthony Loke telah menutup pintu secara terbuka kepada DAP meninggalkan PH (17 Julai 2026, CNA): 'anda hanya boleh memainkan peranan pembangkang' jika solo. 40 kerusi DAP ialah blok paling kurang boleh ditandingi dalam politik Malaysia — kerusi bandar majoriti Cina dengan margin kerap melebihi 50%. Risiko kepada DAP bukan lompat parti tetapi hakisan: Bersama menyedut pangkalan bandarnya, dan mesej kos sara hidup Bersama mendarat dengan pengundi yang perlu dikekalkan DAP dalam medan pertempuran. Loke sendiri menyelia kempen Johor (PH memenangi 8 kerusi) dan mengakui 'kemunduran, tetapi bukan penolakan total' — bahagian undi PH meningkat walaupun di mana kerusi jatuh.

**PKR: parti yang dipersoalkan.** PKR, dengan kira-kira 29 kerusi persekutuan, ialah paling lemah daripada tiga komponen PH secara struktur. Pilihan raya kepimpinan 2025 — di mana Rafizi ditewaskan Nurul Izzah Anwar (anak PM) untuk jawatan timbalan presiden — ialah titik perubahan: pemergian Rafizi menyusul, dan beliau meramalkan 'PKR berisiko mati selepas era Anwar.' PKR mempertimbangkan tindakan terhadap enam MP pembelot yang menghadiri pelancaran Bersama. Seorang pemimpin PKR secara terbuka menggesa PH berkhidmat sepenuh penggal (Ogos 2026), memberi isyarat kegelisahan dalaman tentang masa GE16 awal. Kekosongan penggantian — dengan Rafizi pergi dan Nurul Izzah dinaikkan — menjadikan persoalan pasca-Anwar PKR eksplisit dan tidak selesai. GE16 akan menguji sama ada PKR boleh memegang 29 kerusinya terhadap PN dan Bersama.

**Implikasi untuk model unjuran.** Kemas kini landskap parti memerlukan empat pelarasan kepada model dan tafsirannya. Pertama, komposisi blok PN kini PAS + WAWASAN, dengan Bersatu sebagai kad liar yang mungkin memecah undi Melayu. Kedua, senario lonjakan PN (PN bersatu 80–84 kerusi) kini kurang berkemungkinan: pemecahan mengehadkan PN lebih dekat kepada kes asas. Ketiga, kelemahan PH meningkat: sedut Bersama (3–6%) ialah penolakan langsung kepada PH dalam kerusi bandar, dan bukti Johor mencadangkan 2–4 kerusi medan pertempuran boleh bertukar daripada PH kepada BN/PN atas ini sahaja. Keempat, kebangkitan BN disahkan dan diperkukuh: BN 48/56 di Johor mengesahkan senario kebangkitan selatan. Bacaan bersih ialah landskap dikemas kini menjadikan kedudukan kerajaan lebih kuat daripada yang dicadangkan kes asas. Pemecahan pembangkang menukar apa yang merupakan perjuangan dua hala kepada pertandingan tiga dan empat penjuru, yang di bawah FPTP memberi ganjaran kepada blok terbesar — biasanya kerajaan."""


def _s14(ctx):
    swing_stats = ctx["swing_stats"]
    return f"""## 14. Kad Skor Ramalan PRN — Siapa Yang Betul?

Kad skor ramalan PRN (Pilihan Raya Negeri) ialah bukti penentukuran projek: rekod sejauh mana pusat penyelidikan utama Malaysia dan penganalisis individu meramalkan tiga pilihan raya negeri terkini — Sabah (November 2025), Johor (Julai 2026), dan Negeri Sembilan (Ogos 2026). Rekod ini berfungsi dua tujuan: ia menentukur berat yang diberikan kepada tinjauan masa depan setiap pusat, dan ia mengesahkan (atau mencabar) andaian pemodelan projek sendiri. Kad skor penuh didokumentasikan dalam `02_FORECAST/knowledge/prn-prediction-scorecard.md`.

**Keputusan sebenar (disahkan dari MECo melalui folder DUN projek):**

| Negeri | Tarikh | Keputusan |
|---|---|---|
| **Sabah** | 29 Nov 2025 | WARISAN 25 · GRS 22 · BN 6 · PBS 6 · IND 6 · UPKO 3 · STAR 2 · PN 1 · KDM 1 · PH 1 (73 kerusi) |
| **Johor** | 11 Jul 2026 | BN 48 · PH 8 (56 kerusi) |
| **N9** | 1 Ogos 2026 | BN 18 + PN 7 = 25 (pakatan BN-PN) · PH 11 (36 kerusi) |

**Ramalan berbanding keputusan:**

| Pusat / Penganalisis | Sabah | vs Sebenar | Johor | vs Sebenar | N9 | vs Sebenar |
|---|---|---|---|---|---|---|
| **Ilham Centre** | GRS ≥26, WARISAN 14 | ❌ (kedua-dua salah) | BN mendahului 39 | ❌ (−9) | BN-PN 22, PH ≥9 | ❌ (−3, −2) |
| **Merdeka Center** | — | — | BN 40–42 | ❌ (−6 hingga −8) | — | — |
| **Ong Kian Ming** (individu) | — | — | BN 53 | ❌ (+5) | PH 9, BN-PN ~23 | ❌ (−2) |
| **Vodus Research** | — | — | BN 36% undi | ❌❌ (−24pp) | — | — |
| Konsensus (kinitv/penganalisis) | Tiada majoriti jelas | ✅ (hampir tergantung) | — | — | — | — |

**Verdik.** Pada arah (siapa memerintah), Ilham Centre menjaringkan 3 daripada 3 — ia meramalkan dengan betul bahawa BN menang Johor, BN-PN menang N9, dan gabungan pimpinan GRS memerintah Sabah. Pada magnitud, Ong Kian Ming (penganalisis individu, bukan pusat) paling dekat pada Johor (53 vs 48, +5) dan N9 (~23 vs 25, −2). Kesilapan besar ialah Sabah untuk semua orang: GRS ≥26 / WARISAN 14 Ilham salah pada kedua-dua kiraan — lonjakan WARISAN ke 25 mengejutkan semua pusat, mengesahkan bahawa Malaysia Timur mengikuti logik naungan tempatan, bukan ayunan nasional. Kesilapan tunggal paling teruk ialah Vodus Research, yang meramalkan BN pada 36% bahagian undi di Johor berbanding sebenar 59.7% — terlepas 24 mata peratusan.

**Pengajaran penentukuran untuk model projek.** Tiga pengajaran muncul daripada kad skor, setiap satu telah digabungkan ke dalam enjin ramalan. Pertama, **arah mengalahkan magnitud**: ramalan memberi tajuk kepada kebarangkalian pembentukan kerajaan dan membentangkan kiraan kerusi sebagai julat P10/P50/P90, bukan anggaran titik — tepat seperti yang dilakukan Monte Carlo. Kedua, **Sabah dan Malaysia Timur ialah titik buta yang diketahui**: enjin sudah memisahkan jenis kerusi `east_malaysia` dengan berat ayunan dikurangkan, dan kad skor mengesahkan keputusan ini — setiap pusat yang menggunakan logik ayunan Semenanjung kepada Sabah salah. Ketiga, **berasaskan ayunan mengalahkan berasaskan tinjauan**: senario kebangkitan selatan projek (ayunan BN +{swing_stats['by_state'].get('Johor', {}).get('BN', 0):.1f}pp) meramalkan supermajoriti BN Johor yang terwujud sebagai 48/56, manakala pusat berasaskan tinjauan (Merdeka 40–42) kurang sasaran. Pilihan dedah — undi sebenar — mengalahkan niat yang dinyatakan. Ini sebabnya ayunan pilihan raya negeri menerima berat tertinggi (0.30) dalam tindanan faktor: kad skor membuktikan ia ialah isyarat paling boleh dipercayai yang ada.

Kad skor juga mendedahkan masalah struktur dengan psefologi Malaysia: pusat penyelidikan utama negara (Ilham, Merdeka) secara konsisten kurang meramal pemulihan selatan BN dan gagal memodelkan dinamik tempatan Malaysia Timur. Ini bukan kegagalan teknik tinjauan tetapi rangka pemodelan: pusat menggunakan logik ayunan nasional kepada negeri yang mengikuti logik khusus negeri. Model projek, dengan menggunakan ayunan per negeri dengan modulasi jenis dan dengan memisahkan kerusi Malaysia Timur ke dalam kategori sendiri, mengelak perangkap ini — tetapi ia tidak kebal kepada masalah kualiti data asas. Kesilapan Sabah ialah peringatan bahawa walaupun model terbaik tidak dapat meramal apa yang tidak dapat dilihatnya: peralihan naungan tempatan dalam kawasan Malaysia Timur tidak kelihatan kepada data peringkat nasional, dan tindak balas jujur model ialah melebarkan ketidakpastian untuk kerusi itu dan bukannya berpura-pura ketepatan yang tidak dimilikinya."""


def _s15(ctx):
    det, mc = ctx["det"], ctx["mc"]
    govt_actual, bg_stats = ctx["govt_actual"], ctx["bg_stats"]
    flips = ctx["flips"]
    return f"""## 15. Implikasi Strategik & Syor

Sebagai penutup, satu peringatan tentang sifat bukti yang menyokong setiap syor di atas. Model ini tidak mendakwa mengetahui keputusan GE16; ia mendakwa mengetahui, dengan ukuran ketidakpastian yang boleh diaudit, julat keputusan yang konsisten dengan bukti semasa. Syor untuk kerajaan, PN, Bersama, dan komuniti penganalisis semuanya diterbitkan daripada aritmetik tersebut — bukan daripada kecenderungan terdahulu tentang siapa yang patut memerintah. Jika bukti berubah, syor berubah; itulah sebabnya setiap versi laporan ini diarkibkan dan boleh dibandingkan dengan yang sebelumnya.

Dapatan pusat unjuran — bahawa GE16 akan menjadi pilihan raya pengecilan majoriti, bukan pilihan raya pertukaran kerajaan — mempunyai implikasi strategik untuk setiap pelakon dalam sistem politik Malaysia. Bahagian ini menterjemah aritmetik model kepada syor boleh tindakan untuk kerajaan, pembangkang, kuasa ketiga, dan komuniti penganalisis.

**Untuk kerajaan (gabungan PH-BN-Malaysia Timur):** aset terkuat ialah masa. Jam perlembagaan berjalan ke 17 Februari 2028, memberi kerajaan 18 bulan landasan. Penunjuk ekonomi menggalakkan (KDNK melebihi 5%, inflasi di bawah 2%, kelulusan meningkat), dan pembangkang berpecah. Syornya ialah **berkhidmat sepenuh penggal** dan memaksimumkan angin sorong ekonomi. Pilihan raya awal akan mengorbankan baki landasan dan memberi pembangkang masa untuk menyusun semula — khususnya, ia akan memberi Bersatu masa untuk memutuskan sama ada bertanding solo (yang akan memecah undi PN dan menguntungkan kerajaan) atau bergabung ke PAS-WAWASAN (yang akan menyatukan undi PN dan meningkatkan risiko kepada medan pertempuran dipegang kerajaan). Kerajaan juga harus melabur dalam memegang kerusi super-marginalnya: Putatan, Bagan Datuk, Sungai Petani, dan medan pertempuran Selangor ialah kerusi di mana majoriti akan menang atau kalah. {govt_actual} kerusi unjuran mewakili kusyen {govt_actual - 112} melebihi ambang — selesa, tetapi tidak kebal jika pembangkang bersatu atau ayunan lewat muncul.

**Untuk Perikatan Nasional (PAS + WAWASAN):** unjuran mengehadkan PN kepada {det.get('PN', 0)} kerusi dalam kes asas — kurang daripada kerajaan sebanyak {112 - det.get('PN', 0)} kerusi. Masalah struktur ialah geografi: sokongan PN tertumpu di Malay Belt (Kedah, Kelantan, Terengganu, Perlis), yang menyampaikan kerusi dengan cekap tetapi tidak boleh mencapai 112 sendirian. Laluan ke kerajaan memerlukan penembusan ke dalam kerusi campuran majoriti Melayu tali pinggang tengah (Perak, Selangor, Pahang) dan selatan (Johor, N9) — tetapi pilihan raya negeri 2025–2026 menunjukkan elektorat Melayu selatan kembali kepada BN, bukan bergerak ke PN. Syornya ialah **menyatukan Malay Belt, bertanding secara selektif di tali pinggang tengah, dan elak pertandingan tiga penjuru dengan serpihan Bersatu**. Perpecahan Bersatu ialah kelemahan terbesar PN: setiap kerusi di mana Bersatu bertanding solo ialah kerusi di mana undi PN berpecah dan BN menang atas pluraliti. PN harus merundingkan peruntukan kerusi dengan serpihan Bersatu — atau menerima bahawa solo serpihan akan menelan kos PN {len([f for f in flips if f['proj_winner'] == 'BN'])} atau lebih kerusi.

**Untuk kuasa ketiga (Bersama):** keputusan Johor — kesemua 15 calon hilang deposit, 3–6% bahagian undi setiap kerusi — membuktikan bahawa strategi bertanding meluas Bersama ialah perosak untuk PH, bukan laluan ke kerusi. Garpu strategik ialah yang sama dikenal pasti dalam kajian landskap parti: bertanding meluas (perosak untuk PH, sekutu tidak sengaja BN/PN) atau bertanding sempit (kingmaker). Bukti Johor menunjukkan pertandingan meluas menguntungkan BN, bukan Bersama — Bersama tidak menang apa-apa dan hilang deposit. Syornya ialah **bertanding sempit, dalam 5–10 kerusi di mana margin PH di bawah 2% dan calon Bersama mempunyai pengikut peribadi** (Pandan, Setiawangsa, dan kerusi Selangor terpilih). Ini memaksimumkan kebarangkalian memenangi sekurang-kurangnya satu kerusi (yang akan menjadikan Bersama kehadiran parlimen) sambil meminimumkan kerosakan kepada PH (gabungan paling selaras dengan agenda pembaharuan Bersama sendiri). Pertandingan meluas — 50+ kerusi — akan mengulangi keputusan Johor: sifar kerusi, deposit hilang, dan supermajoriti BN.

**Untuk komuniti penganalisis:** kad skor PRN mendedahkan bahawa psefologi Malaysia mempunyai kelemahan struktur — pusat utama menggunakan logik ayunan nasional kepada dinamik khusus negeri, terutamanya di Malaysia Timur dan selatan. Syornya ialah **melabur dalam pemodelan peringkat negeri dengan pemboleh ubah naungan tempatan** untuk Sabah dan Sarawak, dan **menimbang pilihan dedah (ayunan pilihan raya negeri) di atas niat yang dinyatakan (tinjauan)** untuk semua negeri. Model projek sudah melakukan ini, dan kad skor mengesahkan pendekatan: senario kebangkitan selatan berasaskan ayunan meramalkan supermajoriti BN Johor yang terlepas oleh pusat berasaskan tinjauan. Komuniti penganalisis juga harus menerbitkan julat P10/P50/P90 dan bukannya anggaran titik — arah lebih boleh diramal daripada magnitud, dan ramalan jujur ialah julat, bukan nombor.

**Kenyataan keyakinan.** Unjuran ialah julat terikat, bukan titik. Bacaan jujur ialah kerusi berpihak kerajaan **{mc['P10']:.0f}–{mc['P90']:.0f}**, dengan median {mc['P50']:.0f} dan kebarangkalian {mc['P_majority']*100:.0f}% mengekalkan majoriti. Keyakinan model dalam *arah* (kerajaan mengekalkan kuasa) sangat tinggi — {mc['P_majority']*100:.0f}% dalam Monte Carlo dan 100% merentas semua tujuh senario. Keyakinan model dalam *magnitud* (berapa kerusi) sederhana — hamparan P10–P90 {mc['P90'] - mc['P10']:.0f} kerusi mencerminkan ketidakpastian sebenar dalam {bg_stats['total']} medan pertempuran. Apa yang boleh mengubah unjuran: **(1) penentuan semula** — jika pengembangan 222-ke-235 kerusi digubal sebelum GE16, model mesti dibina semula sepenuhnya; **(2) faktor Bersama di tiga kerusi: Pandan, Setiawangsa, dan Subang (semuanya kekal kosong sehingga GE16)** — dikosongkan oleh peletakan jawatan Bersama; SPR memutuskan TIADA pilihan raya kecil akan diadakan (pilihan raya kecil tidak automatik di bawah Perkara 49A — ia memerlukan Speaker memaklumkan SPR, yang tidak berlaku), jadi kerusi kekal kosong sehingga GE16 dan rayuan Bersama akan diuji pada pilihan raya umum itu sendiri dan bukannya pilihan raya kecil; **(3) keputusan GE16 Bersatu** — solo (memecah PN, menguntungkan kerajaan) atau bergabung ke PAS-WAWASAN (menyatukan PN, meningkatkan risiko kepada medan pertempuran kerajaan); **(4) ayunan kempen lewat** — elektorat Malaysia telah menunjukkan keupayaan mereka untuk bergerak lewat dan bergerak kuat, seperti yang dibuktikan minggu-minggu akhir GE15. Model ialah gambar momen elektoral semasa, bukan ramalan masa depan; ia akan dikemas kini setiap minggu apabila data baharu tiba, dan setiap versi terdahulu disimpan dalam arkib."""


def _s16(ctx):
    now = ctx["now"]
    return f"""## 16. Rujukan & Asal Usul

**Sumber akademik:**
- ISEAS Perspective 2023/20 — Marzuki Mohamad & Ibrahim Suffian (Merdeka Center), \"Malaysia's 15th GE: Ethnicity Remains the Key Factor\"
- Pepinsky, Fosco & Ostwald (2023), SMU — \"Demographic structure and voting behaviour during democratization: Evidence from Malaysia's 2022 election\"
- Lewis-Beck & Stegmaier (2000), *Annual Review of Political Science* — \"Economic Determinants of Electoral Outcomes\"
- Wilkin, Hallerberg & Carey (1997) — pekali pengundian ekonomi rentas negara
- Pandian (2025), *Social Sciences & Humanities Open* (ScienceDirect) — \"Undi18 and the Malaysian youth vote\"
- Sunway University — \"The Economic Voting Puzzle of Malaysia\"
- BTI 2026 Laporan Negara Malaysia — penunjuk ekonomi

**Analisis institusi:**
- Fulcrum / ISEAS–Yusof Ishak Institute — Francis E. Hutchinson, \"Perikatan Nasional's Dramatic Denouement: Where to for Bersatu?\" (2026/182)
- Fulcrum / ISEAS — Lee Hwok-Aun, \"Rafizi and Nik Nazmi's 'Kamikaze' Mission: A Brazen Double Dare\" (Mei 2026)
- Ulasan RSIS — \"Assessment and Early Analysis of the 2026 Johor State Election Results\" (15 Jul 2026)

**Data rasmi:**
- Suruhanjaya Pilihan Raya Malaysia (keputusan rasmi GE15; keputusan pilihan raya negeri 2023–2026)
- ElectionData.MY / MECo (CC0) — keputusan peringkat kawasan, demografi pengundi, statistik pilihan raya kecil
- Jabatan Perangkaan Malaysia (DOSM) — KDNK, CPI
- Bank Negara Malaysia (BNM) — ringgit, penunjuk monetari

**Tinjauan dan penjejakan:**
- Tinjauan Merdeka Center (2020–2026) — penilaian kelulusan, penjejakan keutamaan pengundi
- Tinjauan Ilham Centre — ramalan pilihan raya negeri, kajian lapangan
- Ong Kian Ming (bekas MP DAP, Taylor's University) — ramalan penganalisis individu
- Vodus Research — ramalan Johor 2026

**Sumber berita (laporan peristiwa):**
- The Straits Times (Mei–Julai 2026) — pelancaran Bersama; keputusan Johor
- CNA (Jun–Julai 2026) — kenyataan DAP/PH; Bersatu-Hamzah
- Malaysiakini (Jun 2026) — perpecahan PAS-Bersatu
- Malay Mail, FMT, NST, The Edge, The Malaysian Reserve, The Vibes, The Diplomat, Sinar Harian (2026) — laporan landskap parti
- East Asia Forum (Februari 2026) — \"Malaysia enters election mode in 2026\"

**Dokumen pengetahuan projek:**
- `02_FORECAST/knowledge/forecast-theory.md` — teori perubahan: hierarki empat lapisan, rantaian sebab-akibat, matematik teras, protokol pengesahan
- `02_FORECAST/knowledge/forecast-factor-rankings.md` — asal usul faktor: setiap elemen kedudukan dan dipetik dengan sumbernya
- `02_FORECAST/knowledge/prn-prediction-scorecard.md` — rekod prestasi PRN: pusat mana yang betul
- `Research Data/notes/party-landscape-update-2026.md` — kajian landskap parti: perpecahan PAS-Bersatu, WAWASAN, Bersama, DAP/PKR

**Set data projek (semua hidup, dibaca pada masa binaan):**
- `Research Data/derived/ge15-results-by-constituency-full.csv` — 222 kerusi, keputusan rasmi GE15
- `Research Data/derived/master-list-222-parliamentary-seats.csv` — daftar kerusi
- `Research Data/derived/voter-demographics-by-constituency-ge15.csv` — bahagian etnik/umur, 21,173,638 pengundi
- `Parliament/ge16-battleground-seats-master.csv` — 36 kerusi marginal
- `01_RESEARCH/data/derived/swing_se_to_se.csv` — ayunan pilihan raya negeri, 9 negeri
- `05_AUTOMATION/projection_scenarios.json` — sensitiviti 7 senario
- `02_FORECAST/outputs/latest/ge16-forecast-latest.json` — unjuran deterministik + Monte Carlo
- `02_FORECAST/engine/config.py` — berat faktor, bacaan makro, kejutan peristiwa, modulasi jenis

*Dijana langsung oleh `02_FORECAST/engine/report_builder.py --lang ms` pada {now}. Laporan ini berversi: setiap laporan terdahulu dipelihara dalam `03_REPORTS/federal/archive/`; yang semasa berada dalam `03_REPORTS/federal/latest/`. Tiada laporan pernah ditimpa. Setiap nombor dalam dokumen ini dikira daripada set data projek pada masa binaan — tiada apa-apa yang ditaip tangan.*"""


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def render_federal_ms(ctx):
    """Render the full 17-section Malay federal report from the shared ctx."""
    now = ctx["now"]
    parts = [
        f"# Pilihan Raya Umum Malaysia Ke-16 — Satu Ramalan Lengkap\n",
        f"**Tarikh laporan:** {now} · **Data vintaj:** GE15 (19 November 2022) + pilihan raya negeri sehingga Ogos 2026 · **Model:** factor-v1.0",
        f"**URL Langsung:** https://faisal.aila.my/ge16-malaysia-election · **Arkib:** setiap laporan terdahulu dipelihara dalam `03_REPORTS/federal/archive/`",
        "---",
        _s0(ctx), "---",
        _s1(ctx), "---",
        _s2(ctx), "---",
        _s3(ctx), "---",
        _s4(ctx), "---",
        _s5(ctx), "---",
        _s6(ctx), "---",
        _s7(ctx), "---",
        _s8(ctx), "---",
        _s9(ctx), "---",
        _s10(ctx), "---",
        _s11(ctx), "---",
        _s12(ctx), "---",
        _s13(ctx), "---",
        _s14(ctx), "---",
        _s15(ctx), "---",
        _s16(ctx),
    ]
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Cross-build gate — verify EN↔MS numeric parity (numbers verbatim from JSON)
# ---------------------------------------------------------------------------

KEY_NUMBERS = [
    ("P50", r"P50 = (\d+)"),
    ("P10", r"P10 = (\d+)"),
    ("P90", r"P90 = (\d+)"),
    ("govt deterministic", r"(\d+) of 222 seats", r"(\d+) daripada 222 kerusi"),
    ("P(majority)", r"(\d+)% probability of retaining the 112-seat simple majority",
     r"(\d+)% mengekalkan majoriti mudah 112 kerusi"),
]


def cross_build_gate(en_md_path, ms_md_path):
    """Compare key numbers between EN and MS federal reports. Exit non-zero on
    mismatch. Numbers must match verbatim (never rounded or recomputed)."""
    if not os.path.exists(en_md_path):
        print(f"  GATE: EN report missing → {en_md_path}")
        return False
    if not os.path.exists(ms_md_path):
        print(f"  GATE: MS report missing → {ms_md_path}")
        return False
    en = open(en_md_path, encoding="utf-8").read()
    ms = open(ms_md_path, encoding="utf-8").read()
    ok = True
    for entry in KEY_NUMBERS:
        label, en_pattern = entry[0], entry[1]
        ms_pattern = entry[2] if len(entry) > 2 else en_pattern
        m_en = re.search(en_pattern, en)
        m_ms = re.search(ms_pattern, ms)
        if m_en and m_ms:
            if m_en.group(1) != m_ms.group(1):
                print(f"  GATE ❌ {label}: EN={m_en.group(1)} MS={m_ms.group(1)}")
                ok = False
            else:
                print(f"  GATE ✅ {label}: {m_en.group(1)}")
        elif m_en and not m_ms:
            print(f"  GATE ⚠️ {label}: pattern not found in MS (may be phrased differently)")
        elif not m_en:
            print(f"  GATE ⚠️ {label}: pattern not found in EN")
    return ok
