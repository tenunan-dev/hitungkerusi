/* State explorer: election-clock context plus the same seat-level evidence used federally. */
(function () {
  'use strict';
  var D = window.GE16_APP_DATA || {};
  var S = D.summary || {};
  var stateTints = { Sarawak: '#8b6ca8', Sabah: '#c17d55', 'Malacca': '#bf7a68', 'Negeri Sembilan': '#b38459', Penang: '#4f8a9d', Selangor: '#5e8fbd', Johor: '#8b8fae', Kedah: '#5a947f', Kelantan: '#5a947f', Terengganu: '#4f8a9d', Pahang: '#b38459', Perak: '#788f68', Perlis: '#788f68', 'Kuala Lumpur (FT)': '#7186a1', 'Putrajaya (FT)': '#7186a1', 'Labuan (FT)': '#c17d55' };
  var aliases = { Melaka: 'Malacca', 'Pulau Pinang': 'Penang', 'N. Sembilan': 'Negeri Sembilan' };
  function esc(value) { return String(value == null ? '' : value).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'); }
  function color(bloc) { return (S.bloc_colors || {})[bloc] || '#64748b'; }
  function label(state) { return ({ Malacca: 'Melaka', Penang: 'Pulau Pinang', 'Negeri Sembilan': 'N. Sembilan' })[state] || state; }
  function reportSlug(state) { return ({ 'Negeri Sembilan': 'negeri-sembilan', 'Malacca': 'melaka', 'Penang': 'pulau-pinang' })[state] || state.toLowerCase().replace(/\s+/g, '-'); }
  function sourceState(value) { return aliases[value] || value; }
  function allStates() { var seen = {}; (D.master || []).forEach(function (m) { seen[m.state] = true; }); return Object.keys(seen).sort(); }
  function prn(state) { return (D.prn_upcoming || []).concat(D.prn_recent || []).filter(function (item) { return sourceState(item.state) === state; })[0]; }
  function projections() { var map = {}; (D.projection || []).forEach(function (item) { map[item.code] = item; }); return map; }
  var selected = '';

  function render() {
    var seats = (D.master || []).filter(function (m) { return m.state === selected; });
    var projected = projections();
    var prnInfo = prn(selected);
    var tint = stateTints[selected] || '#7186a1';
    document.documentElement.style.setProperty('--state-accent', tint);
    document.getElementById('state-title').textContent = label(selected);
    document.getElementById('state-summary').textContent = prnInfo
      ? 'Kedudukan pilihan raya negeri dan ' + seats.length + ' kerusi Parlimen dalam ' + label(selected) + '.'
      : seats.length + ' kerusi Parlimen dalam ' + label(selected) + ', menggunakan data unjuran yang sama seperti paparan persekutuan.';
    document.getElementById('state-map-link').href = '/app/maps.html?state=' + encodeURIComponent(selected);
    var report = document.getElementById('state-report-link');
    if (report) {
      var hasReport = ['Johor', 'Kedah', 'Kelantan', 'Malacca', 'Negeri Sembilan', 'Pahang', 'Perak', 'Perlis', 'Penang', 'Sabah', 'Sarawak', 'Selangor', 'Terengganu'].indexOf(selected) !== -1;
      report.hidden = !hasReport;
      if (hasReport) {
        var suffix = document.documentElement.lang === 'en' ? '-en' : '';
        report.href = '/state/' + reportSlug(selected) + suffix + '.html';
        report.textContent = document.documentElement.lang === 'en' ? 'Read state report' : 'Baca laporan negeri';
      }
    }
    document.getElementById('state-seats-heading').textContent = seats.length + ' kerusi Parlimen di ' + label(selected);

    var flips = seats.filter(function (m) { return projected[m.code] && projected[m.code].flip; }).length;
    var stats = [
      { label: 'Kerusi Parlimen', value: seats.length, note: 'dalam negeri ini' },
      { label: 'Kerusi bertukar', value: flips, note: 'dalam unjuran asas' }
    ];
    if (prnInfo) {
      stats.unshift({ label: 'Pilihan raya terdekat', value: prnInfo.days + ' hari', note: label(selected) });
      stats.push({ label: 'Dewan negeri', value: prnInfo.seats, note: prnInfo.gov });
    }
    var statsEl = document.getElementById('state-stats');
    statsEl.className = 'state-stat-grid' + (stats.length === 2 ? ' state-stat-grid--two' : '');
    statsEl.innerHTML = stats.map(function (stat) { return '<article class="state-stat"><span>' + esc(stat.label) + '</span><strong>' + esc(stat.value) + '</strong><small>' + esc(stat.note) + '</small></article>'; }).join('');

    var blocs = {};
    seats.forEach(function (m) { var bloc = m.coalition || 'IND'; blocs[bloc] = (blocs[bloc] || 0) + 1; });
    var parts = Object.keys(blocs).sort().map(function (bloc) { return '<li><span style="background:' + color(bloc) + '"></span>' + esc(bloc) + '<strong>' + blocs[bloc] + '</strong></li>'; }).join('');
    document.getElementById('state-composition').innerHTML = '<p class="state-panel__lead">Komposisi kerusi Parlimen semasa mengikut gabungan.</p><ul class="state-composition">' + parts + '</ul>';

    var close = seats.map(function (m) { var p = projected[m.code]; return p ? { member: m, projection: p, margin: Number(p.proj_margin == null ? 999 : p.proj_margin) } : null; }).filter(Boolean).sort(function (a, b) { return a.margin - b.margin; }).slice(0, 4);
    document.getElementById('state-tight').innerHTML = close.length ? '<ol class="state-tight-list">' + close.map(function (item) { return '<li><span><b>' + esc(item.member.code) + '</b> ' + esc(item.member.constituency) + '</span><strong>' + item.margin.toFixed(1) + '%</strong></li>'; }).join('') + '</ol>' : '<p class="muted">Tiada margin unjuran tersedia.</p>';

    var visibleSeats = seats.slice(0, 10);
    document.getElementById('state-seats').innerHTML = visibleSeats.map(function (m) {
      var p = projected[m.code] || {}; var winner = p.proj_winner || '—'; var ge15 = m.coalition || '—';
      return '<tr><td class="num">' + esc(m.code) + '</td><td>' + esc(m.constituency) + '</td><td><span class="bloc-badge" style="background:' + color(ge15) + '">' + esc(ge15) + '</span></td><td><span class="bloc-badge" style="background:' + color(winner) + '">' + esc(winner) + '</span></td><td class="num">' + (p.proj_margin != null ? Number(p.proj_margin).toFixed(1) + '%' : '—') + '</td></tr>';
    }).join('');
    var note = document.getElementById('state-seats-note');
    if (note) note.innerHTML = seats.length > visibleSeats.length ? 'Memaparkan 10 kerusi terawal. <a href="/app/seats.html?state=' + encodeURIComponent(selected) + '">Lihat semua ' + seats.length + ' kerusi</a>.' : '';
  }

  function boot() {
    var select = document.getElementById('state-dashboard-select');
    var requested = '';
    try { requested = new URLSearchParams(window.location.search).get('state') || ''; } catch (e) {}
    selected = sourceState(requested) || 'Sarawak';
    if (allStates().indexOf(selected) === -1) selected = allStates()[0];
    select.innerHTML = allStates().map(function (state) { return '<option value="' + esc(state) + '"' + (state === selected ? ' selected' : '') + '>' + esc(label(state)) + '</option>'; }).join('');
    select.addEventListener('change', function () { window.location.href = '?state=' + encodeURIComponent(this.value); });
    render();
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot); else boot();
}());
