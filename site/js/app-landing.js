/* Universal landing briefing: federal by default, state-aware when ?state= is present. */
(function () {
  'use strict';
  var D = window.GE16_APP_DATA || {};
  var S = D.summary || {};
  var C = window.hk222 || {};
  var state = C.state || '';
  var DUN = C.dunData || null;
  function text(id, value) { var el = document.getElementById(id); if (el) el.textContent = value; }
  function href(id, value) { var el = document.getElementById(id); if (el) el.href = value; }
  function projectionMap() { var map = {}; (D.projection || []).forEach(function (item) { map[item.code] = item; }); return map; }
  function stateLabel(value) { return C.stateLabel ? C.stateLabel(value) : value; }
  function route(path) { return C.scopedHref ? C.scopedHref(path, state) : path + '?state=' + encodeURIComponent(state); }
  function stateLanding() {
    if (C.isDun && DUN) { dunLanding(); return; }
    var seats = (D.master || []).filter(function (seat) { return seat.state === state; });
    var projected = projectionMap();
    var govtBlocs = S.govt_blocs || [];
    var govt = seats.filter(function (seat) { var p = projected[seat.code] || {}; return govtBlocs.indexOf(p.proj_winner || seat.coalition) !== -1; }).length;
    var flips = seats.filter(function (seat) { return projected[seat.code] && projected[seat.code].flip; }).length;
    var tight = seats.map(function (seat) { var p = projected[seat.code] || {}; return { constituency: seat.constituency, margin: Number(p.proj_margin == null ? Infinity : p.proj_margin) }; }).sort(function (a, b) { return a.margin - b.margin; })[0] || {};
    var prn = (D.prn_upcoming || []).concat(D.prn_recent || []).filter(function (item) { return item.state === state || (item.state === 'Melaka' && state === 'Malacca') || (item.state === 'Pulau Pinang' && state === 'Penang'); })[0];
    var label = stateLabel(state);
    text('landing-context', label + ' · PRU16');
    document.getElementById('landing-title').innerHTML = 'Apa yang perlu diperhatikan di <span>' + label + '?</span>';
    text('landing-subtitle', 'Paparan khusus ' + label + ' — kerusi Parlimen, unjuran, berita dan laporan negeri dalam satu aliran.');
    text('landing-highlight-label', 'Unjuran kerusi pro-kerajaan');
    text('landing-govt', govt + ' / ' + seats.length);
    text('landing-outcome', govt + ' daripada ' + seats.length + ' kerusi Parlimen di ' + label + ' diunjurkan untuk blok kerajaan.');
    text('landing-primary-link', 'Buka ringkasan ' + label + ' →');
    href('landing-primary-link', route('/app/'));
    var watchlist = document.getElementById('landing-watchlist');
    if (watchlist) watchlist.innerHTML = '<li><span>Kerusi bertukar</span><strong>' + flips + '</strong></li><li><span>Kerusi paling rapat</span><strong>' + (tight.constituency || '—') + '</strong></li><li><span>Kerusi Parlimen</span><strong>' + seats.length + '</strong></li>';
    text('next-state-title', prn ? label : 'Laporan ' + label);
    text('landing-next-detail', prn ? (prn.days + ' hari · ' + prn.seats + ' kerusi DUN · ' + prn.gov) : 'Data kerusi Parlimen dan unjuran negeri ini.');
    var report = C.reportHref ? C.reportHref(state) : '/app/states.html?state=' + encodeURIComponent(state);
    text('landing-next-link', C.hasStateReport && C.hasStateReport(state) ? 'Baca laporan negeri →' : 'Terokai kerusi negeri →');
    href('landing-next-link', report);
    href('landing-map-link', route('/app/maps.html'));
    href('landing-scenarios-link', route('/app/scenarios.html'));
    href('landing-report-link', report);
  }
  function dunLanding() {
    var label = stateLabel(state);
    var seats = DUN.seats || [];
    var blocs = DUN.blocs || {};
    var election = DUN.election || {};
    var leading = Object.keys(blocs).sort(function (a, b) { return blocs[b] - blocs[a]; })[0] || '—';
    var tight = seats.slice().sort(function (a, b) { return Number(a.margin || Infinity) - Number(b.margin || Infinity); })[0] || {};
    text('landing-context', label + ' · DUN');
    document.getElementById('landing-title').innerHTML = 'Apa yang perlu diperhatikan di <span>DUN ' + label + '?</span>';
    text('landing-subtitle', 'Paparan khusus Dewan Undangan Negeri ' + label + ' — keputusan kerusi, komposisi, berita dan laporan dalam satu aliran.');
    text('landing-highlight-label', 'Blok terbesar dalam Dewan');
    text('landing-govt', (blocs[leading] || 0) + ' / ' + seats.length);
    text('landing-outcome', leading + ' memenangi ' + (blocs[leading] || 0) + ' daripada ' + seats.length + ' kerusi DUN dalam rekod pilihan raya terkini.');
    text('landing-primary-link', 'Buka ringkasan DUN ' + label + ' →');
    href('landing-primary-link', route('/app/'));
    var watchlist = document.getElementById('landing-watchlist');
    if (watchlist) watchlist.innerHTML = '<li><span>Kerusi DUN</span><strong>' + seats.length + '</strong></li><li><span>Blok terbesar</span><strong>' + leading + '</strong></li><li><span>Margin paling rapat</span><strong>' + (tight.code || '—') + '</strong></li>';
    text('next-state-title', 'Laporan DUN ' + label);
    text('landing-next-detail', (election.name || 'Keputusan pilihan raya negeri terkini') + (election.date ? ' · ' + election.date : '') + '.');
    text('landing-next-link', 'Baca laporan DUN →');
    href('landing-next-link', C.reportHref ? C.reportHref(state, 'dun') : '/laporan.html?state=' + encodeURIComponent(state) + '&chamber=dun');
    href('landing-map-link', route('/app/maps.html'));
    href('landing-scenarios-link', route('/app/scenarios.html'));
    href('landing-report-link', C.reportHref ? C.reportHref(state, 'dun') : '/laporan.html?state=' + encodeURIComponent(state) + '&chamber=dun');
  }
  function federalLanding() {
    var govt = Number(S.govt_p50 || 0);
    var difference = govt - 112;
    text('landing-govt', govt + ' / 222');
    text('landing-outcome', difference >= 0 ? 'Kerajaan diunjurkan melepasi ambang majoriti dengan ' + difference + ' kerusi.' : 'Tiada majoriti diunjurkan; masih kurang ' + Math.abs(difference) + ' kerusi daripada 112.');
    var tight = (S.tight_seats || [])[0] || {};
    var watchlist = document.getElementById('landing-watchlist');
    if (watchlist) watchlist.innerHTML = '<li><span>Kerusi bertukar</span><strong>' + ((S.flips_list || []).length) + '</strong></li><li><span>Kerusi paling rapat</span><strong>' + (tight.constituency || '—') + '</strong></li><li><span>Ambang majoriti</span><strong>112</strong></li>';
    var next = (D.prn_upcoming || [])[0];
    if (next) { text('next-state-title', next.state); text('landing-next-detail', next.days + ' hari · ' + next.seats + ' kerusi DUN · ' + next.gov); href('landing-next-link', '/app/states.html?state=' + encodeURIComponent(next.state === 'Melaka' ? 'Malacca' : next.state)); }
  }
  function boot() {
    if (S.updated) text('landing-updated', S.updated.slice(0, 10));
    if (state) stateLanding(); else federalLanding();
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot); else boot();
}());
