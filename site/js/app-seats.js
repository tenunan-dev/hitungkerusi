/* ==================================================================
   Hitung Kerusi 222 — Seats Table Renderer (Vercel)
   Pure renderer: reads window.GE16_APP_DATA, populates a sortable table.
   ================================================================== */
(function () {
  'use strict';

  var D = window.GE16_APP_DATA || {};
  var S = D.summary || {};
  var C = window.hk222 || {};
  var STATE = C.state || '';
  var IS_DUN = !!C.isDun;
  var SEAT_LIMIT = IS_DUN ? 12 : 24;
  var shownLimit = SEAT_LIMIT;
  var matchingTotal = 0;
  function esc(value) { return String(value == null ? '' : value).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;'); }
  function members() {
    if (!IS_DUN) return D.master || [];
    return ((C.dunData || {}).seats || []).map(function (seat) { return { code: seat.code, constituency: seat.name, state: STATE, member: seat.winner, bloc: seat.bloc, coalition: seat.bloc, party: seat.party, dun: seat }; });
  }

  function getBlocColor(bloc) {
    return (S.bloc_colors || {})[bloc] || '#64748b';
  }

  function getPartyColor(party) {
    return (S.party_colors || {})[party] || '#64748b';
  }

  function getProjection(code) {
    var proj = D.projection || [];
    for (var i = 0; i < proj.length; i++) {
      if (proj[i].code === code) return proj[i];
    }
    return null;
  }

  function getStates() {
    var states = [];
    var seen = {};
    var list = members();
    for (var i = 0; i < list.length; i++) {
      var st = list[i].state;
      if (!seen[st]) {
        seen[st] = true;
        states.push(st);
      }
    }
    states.sort();
    return states;
  }

  function getBlocs() {
    var blocs = [];
    var seen = {};
    var list = members();
    for (var i = 0; i < list.length; i++) {
      var bl = list[i].bloc || list[i].coalition || 'IND';
      if (!seen[bl]) {
        seen[bl] = true;
        blocs.push(bl);
      }
    }
    blocs.sort();
    return blocs;
  }

  function renderStateFilter() {
    var sel = document.getElementById('seat-filter-state');
    if (!sel) return;

    var states = getStates();
    var html = '<option value="all">Semua Negeri</option>';
    for (var i = 0; i < states.length; i++) {
      html += '<option value="' + states[i] + '">' + states[i] + '</option>';
    }
    sel.innerHTML = html;
    if (STATE) {
      sel.value = STATE;
      sel.disabled = true;
      sel.setAttribute('aria-label', 'Negeri aktif: ' + (C.stateLabel ? C.stateLabel(STATE) : STATE));
    }

    sel.addEventListener('change', function() {
      shownLimit = SEAT_LIMIT;
      renderTable();
    });
  }

  function renderBlocFilter() {
    var sel = document.getElementById('seat-filter-bloc');
    if (!sel) return;

    var blocs = getBlocs();
    var html = '<option value="all">Semua Coaliti</option>';
    for (var i = 0; i < blocs.length; i++) {
      html += '<option value="' + blocs[i] + '">' + blocs[i] + '</option>';
    }
    sel.innerHTML = html;

    sel.addEventListener('change', function() {
      shownLimit = SEAT_LIMIT;
      renderTable();
    });
  }

  function renderTable() {
    var tbody = document.getElementById('seats-tbody');
    if (!tbody) return;

    if (IS_DUN) { renderDunTable(tbody); return; }

    var searchVal = document.getElementById('seat-search').value.toLowerCase();
    var stateFilter = STATE || document.getElementById('seat-filter-state').value;
    var blocFilter = document.getElementById('seat-filter-bloc').value;

    var list = members();
    var rows = [];
    for (var i = 0; i < list.length; i++) {
      var m = list[i];
      var proj = getProjection(m.code);
      var searchMatch = m.code.toLowerCase().indexOf(searchVal) > -1 ||
                        m.constituency.toLowerCase().indexOf(searchVal) > -1 ||
                        m.member.toLowerCase().indexOf(searchVal) > -1;

      if (!searchMatch) continue;
      if (stateFilter !== 'all' && m.state !== stateFilter) continue;
      if (blocFilter !== 'all' && (m.bloc || m.coalition) !== blocFilter) continue;

      var bloc = m.bloc || m.coalition || 'IND';
      var isFlip = proj && proj.flip;

      rows.push({
        idx: i + 1,
        code: m.code,
        constituency: m.constituency,
        state: m.state,
        member: m.member,
        bloc: bloc,
        party: m.party,
        projWinner: IS_DUN ? (m.bloc || '—') : (proj ? (proj.proj_winner || '—') : '—'),
        margin: IS_DUN && m.dun && m.dun.margin != null ? Number(m.dun.margin).toFixed(1) + '%' : (proj && proj.margin_pct_ge15 != null ? Number(proj.margin_pct_ge15).toFixed(1) + '%' : '—'),
        flip: isFlip
      });
    }

    // Sort by index (original order)
    rows.sort(function(a, b) { return a.idx - b.idx; });

    matchingTotal = rows.length;
    var visible = rows.slice(0, shownLimit);
    var html = '';
    for (var j = 0; j < visible.length; j++) {
      var r = visible[j];
      var blocColor = getBlocColor(r.bloc);
      var partyColor = getPartyColor(r.party);
      var flipClass = r.flip ? 'flip-yes' : '';

      html += '<tr class="' + flipClass + '">' +
              '<td class="num">' + r.idx + '</td>' +
              '<td><span class="num">' + r.code + '</span></td>' +
              '<td>' + r.constituency + '</td>' +
              '<td>' + r.state + '</td>' +
              '<td>' + r.member + '</td>' +
              '<td><span class="bloc-badge" style="background:' + blocColor + '">' + r.bloc + '</span></td>' +
              '<td><span class="bloc-badge" style="background:' + partyColor + '">' + r.party + '</span></td>' +
              '<td>' + r.projWinner + '</td>' +
              '<td>' + r.margin + '</td>' +
              '<td class="th-flip">' + (r.flip ? '<span style="color:' + '#f87171' + '; font-weight: 700;">✓</span>' : '') + '</td>' +
              '</tr>';
    }
    tbody.innerHTML = html;

    updateCount(visible.length);
    updateMore(rows.length);
  }

  function renderDunTable(tbody) {
    var searchVal = (document.getElementById('seat-search').value || '').toLowerCase();
    var blocFilter = document.getElementById('seat-filter-bloc').value;
    var seats = ((C.dunData || {}).seats || []).filter(function (seat) {
      var haystack = [seat.code, seat.name, seat.winner, seat.party, seat.runnerup, seat.runnerup_party].join(' ').toLowerCase();
      return haystack.indexOf(searchVal) !== -1 && (blocFilter === 'all' || seat.bloc === blocFilter);
    }).sort(function (a, b) { return a.code.localeCompare(b.code); });
    var heads = document.querySelectorAll('#seats-table th');
    ['#', 'Kerusi DUN', 'Pemenang', 'Parti', 'Naib calon', 'Blok', 'Margin', 'Sebelum', 'Bertukar?', 'Tarikh'].forEach(function (label, index) { if (heads[index]) heads[index].textContent = label; });
    matchingTotal = seats.length;
    var visible = seats.slice(0, shownLimit);
    tbody.innerHTML = visible.map(function (seat, index) {
      var historical = seat.historical_baseline || {};
      var estimate = String(historical.type || '').indexOf('estimate') === 0;
      var previous = historical.winner || seat.previous_winner || '';
      var previousBloc = historical.bloc || seat.previous_bloc || '';
      var change = seat.changed_hands === 'new' ? 'Kerusi baharu' : (seat.changed_hands === 'yes' ? 'Ya' : 'Tidak');
      return '<tr>' +
        '<td class="num">' + (index + 1) + '</td>' +
        '<td><span class="num">' + esc(seat.code) + '</span><br><strong>' + esc(seat.name) + '</strong></td>' +
        '<td>' + esc(seat.winner) + '</td>' +
        '<td><span class="bloc-badge" style="background:' + getPartyColor(seat.party) + '">' + esc(seat.party) + '</span></td>' +
        '<td>' + (seat.runnerup ? esc(seat.runnerup) + (seat.runnerup_party ? '<br><small>' + esc(seat.runnerup_party) + '</small>' : '') : '—') + '</td>' +
        '<td><span class="bloc-badge" style="background:' + getBlocColor(seat.bloc) + '">' + esc(seat.bloc) + '</span></td>' +
        '<td class="num">' + (seat.margin != null ? Number(seat.margin).toFixed(1) + '%' : '—') + '</td>' +
        '<td>' + (previous ? (estimate ? '<small>Anggaran 2018</small><br>' : '') + esc(previous) + (previousBloc ? '<br><small>' + esc(previousBloc) + '</small>' : '') : '—') + '</td>' +
        '<td>' + esc(change) + '</td>' +
        '<td class="num">' + esc(((C.dunData || {}).election || {}).date || '') + '</td>' +
        '</tr>';
    }).join('');
    updateCount(visible.length);
    updateMore(seats.length);
  }

  function updateCount(visibleRows) {
    var countEl = document.getElementById('seats-match-count');
    if (!countEl) return;
    countEl.textContent = visibleRows + (matchingTotal !== visibleRows ? ' daripada ' + matchingTotal : '') + ' kerusi';
  }

  function updateMore(total) {
    var button = document.getElementById('seats-more');
    if (!button) return;
    var remaining = total - Math.min(total, shownLimit);
    button.hidden = remaining <= 0;
    button.textContent = remaining > 0 ? 'Lihat ' + Math.min(SEAT_LIMIT, remaining) + ' lagi' : '';
  }

  function setupSearch() {
    var input = document.getElementById('seat-search');
    if (!input) return;
    input.addEventListener('input', function() {
      shownLimit = SEAT_LIMIT;
      renderTable();
    });
  }

  function renderAll() {
    if (STATE) {
      var label = C.stateLabel ? C.stateLabel(STATE) : STATE;
      var heading = document.querySelector('.display-1');
      if (heading) heading.innerHTML = 'Kerusi <span>' + label + '</span>';
      var intro = document.querySelector('.display-1 + .muted');
      if (intro) intro.textContent = IS_DUN ? 'Semua kerusi Dewan Undangan Negeri di ' + label + ' — cari dan semak keputusan setiap kerusi.' : 'Semua kerusi Parlimen di ' + label + ' — cari, semak dan bandingkan unjuran setiap kerusi.';
    }
    renderStateFilter();
    renderBlocFilter();
    renderTable();
    setupSearch();
    var more = document.getElementById('seats-more');
    if (more) more.addEventListener('click', function () { shownLimit += SEAT_LIMIT; renderTable(); });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', renderAll);
  } else {
    renderAll();
  }
})();
