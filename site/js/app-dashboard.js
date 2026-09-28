/* ==================================================================
   Hitung Kerusi 222 — Dashboard Renderer (Vercel)
   Pure renderer: reads window.GE16_APP_DATA, renders DOM.
   No calculation — all values come from data.js summary.*
   ================================================================== */
(function () {
  'use strict';

  var D = window.GE16_APP_DATA || {};
  var S = D.summary || {};
  var C = window.hk222 || {};
  var STATE = C.state || '';
  var IS_DUN = !!C.isDun;
  var DUN = C.dunData || {};
  var ELECTION = DUN.election || {};
  var LANG = 'ms';
  var THEME = 'auto';

  // --- Restore lang/theme state ---
  try { var _l = localStorage.getItem('hk222_lang'); if (_l) LANG = _l; } catch(e) {}
  try { var _t = localStorage.getItem('hk222_theme'); if (_t) THEME = _t; } catch(e) {}

  var IS_FED = !!document.getElementById('federal') ||
               document.body.classList.contains('dashboard');

  /* ===== helpers ===== */
  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;')
      .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }
  function fmt(n) {
    return (n == null || isNaN(n)) ? '—' : Number(n).toLocaleString();
  }
  function pct(n, d) {
    d = d || 2;
    return (n == null || isNaN(n)) ? '—' : Number(n).toFixed(d) + '%';
  }
  function T(ms, en) { return LANG === 'ms' ? ms : en; }

  /* ===== Colour helpers ===== */
  function blocColor(bloc) {
    if (!bloc) return '#64748b';
    var bc = S.bloc_colors || {};
    if (bc[bloc]) return bc[bloc];
    // Hash fallback
    var hash = 0;
    for (var i = 0; i < bloc.length; i++) hash = ((hash << 5) - hash + bloc.charCodeAt(i)) | 0;
    var hue = Math.abs(hash) % 360;
    return 'hsl(' + hue + ', 65%, 45%)';
  }

  function partyColor(party) {
    if (!party) return '#64748b';
    var pc = S.party_colors || {};
    if (pc[party]) return pc[party];
    // Fallback to bloc color if party_bloc is known
    var pb = S.party_bloc || {};
    if (pb[party]) return blocColor(pb[party]);
    var hash = 0;
    for (var i = 0; i < party.length; i++) hash = ((hash << 5) - hash + party.charCodeAt(i)) | 0;
    var hue = Math.abs(hash) % 360;
    return 'hsl(' + hue + ', 65%, 45%)';
  }

  /* ===== Bloc composition from master ===== */
  function getBlocCounts() {
    var counts = {};
    var members = (D.master || []).filter(function (seat) { return !STATE || seat.state === STATE; });
    for (var i = 0; i < members.length; i++) {
      var c = members[i].coalition || 'IND';
      counts[c] = (counts[c] || 0) + 1;
    }
    return counts;
  }

  function getActiveScenario() {
    var scenarios = S.scenarios_display || [];
    return scenarios[currentScenario] || scenarios[1] || scenarios[0] || null;
  }

  function getScenarioCounts() {
    if (IS_DUN) return DUN.blocs || {};
    if (STATE) {
      var projected = {}; (D.projection || []).forEach(function (item) { projected[item.code] = item; });
      var stateCounts = {};
      (D.master || []).filter(function (seat) { return seat.state === STATE; }).forEach(function (seat) { var bloc = (projected[seat.code] || {}).proj_winner || seat.coalition || 'IND'; stateCounts[bloc] = (stateCounts[bloc] || 0) + 1; });
      return stateCounts;
    }
    var sc = getActiveScenario();
    if (!sc) return getBlocCounts();
    return {
      PH: sc.PH || 0, BN: sc.BN || 0, PN: sc.PN || 0, GPS: sc.GPS || 0,
      GRS: sc.GRS || 0, WARISAN: sc.WAR || 0, DAP: sc.DAP || 0,
      'BERSAMA+DAP': sc.BDP || 0, IND: sc.other || 0
    };
  }

  function getPartyByBloc(bloc) {
    var members = D.master || [];
    var parties = {};
    for (var i = 0; i < members.length; i++) {
      if (members[i].coalition === bloc) {
        var p = members[i].party || 'BEBAS';
        parties[p] = (parties[p] || 0) + 1;
      }
    }
    return parties;
  }

  /* ===== KPI Rendering ===== */
  function renderKPI() {
    var sc = getActiveScenario();
    var counts = getScenarioCounts();
    var total = IS_DUN ? (DUN.seats || []).length : (STATE ? (D.master || []).filter(function (seat) { return seat.state === STATE; }).length : 222);
    var leading = Object.keys(counts).sort(function (a, b) { return counts[b] - counts[a]; })[0] || '—';
    var govt = IS_DUN ? (counts[leading] || 0) : (STATE ? (S.govt_blocs || []).reduce(function (sum, bloc) { return sum + (counts[bloc] || 0); }, 0) : (sc ? sc.govt : (S.govt_p50 || 140)));
    var flips = STATE && !IS_DUN ? (S.flips_list || []).filter(function (flip) { return (D.master || []).some(function (seat) { return seat.code === flip.code && seat.state === STATE; }); }).length : (STATE ? 0 : (S.flips_list || []).length);
    var el = document.querySelector('.kpi-hero__num');
    if (el) el.textContent = fmt(govt) + ' / ' + total;

    var answer = document.getElementById('forecast-answer');
    if (answer) {
      var difference = govt - 112;
      answer.textContent = IS_DUN
        ? T(leading + ' memenangi ' + govt + ' daripada ' + total + ' kerusi DUN ' + (C.stateLabel ? C.stateLabel(STATE) : STATE) + ' dalam rekod pilihan raya terkini.', leading + ' won ' + govt + ' of ' + total + ' DUN seats in ' + (C.stateLabel ? C.stateLabel(STATE) : STATE) + ' in the latest election record.')
        : STATE
        ? T(govt + ' daripada ' + total + ' kerusi Parlimen di ' + (C.stateLabel ? C.stateLabel(STATE) : STATE) + ' diunjurkan untuk blok kerajaan.', govt + ' of ' + total + ' parliamentary seats in ' + (C.stateLabel ? C.stateLabel(STATE) : STATE) + ' are projected for government-aligned blocs.')
        : (difference >= 0 ? T('Kerajaan diunjurkan melepasi ambang majoriti dengan ' + difference + ' kerusi.', 'The government is projected to clear the majority threshold by ' + difference + ' seats.') : T('Tiada majoriti diunjurkan — masih kurang ' + Math.abs(difference) + ' kerusi daripada ambang 112.', 'No majority is projected — ' + Math.abs(difference) + ' seats short of the 112-seat threshold.'));
    }
    var range = document.getElementById('forecast-range');
    if (range) range.textContent = IS_DUN ? ((ELECTION.name || T('Keputusan pilihan raya negeri terkini', 'Latest state-election result')) + (ELECTION.date ? ' · ' + ELECTION.date : '') + '. ' + T('Unjuran belum diterbitkan untuk semua negeri.', 'A projection is not published for every state.')) : STATE ? T('Unjuran asas bagi kerusi Parlimen ' + (C.stateLabel ? C.stateLabel(STATE) : STATE) + '.', 'Base projection for parliamentary seats in ' + (C.stateLabel ? C.stateLabel(STATE) : STATE) + '.') : currentScenario === 1
      ? T('Julat P10–P90: ' + fmt(S.govt_p10) + '–' + fmt(S.govt_p90) + ' kerusi', 'P10–P90 range: ' + fmt(S.govt_p10) + '–' + fmt(S.govt_p90) + ' seats')
      : T('Julat ketidakpastian terperinci tersedia untuk unjuran asas.', 'Detailed uncertainty range is available for the base forecast.');

    var kpiCards = document.querySelectorAll('.kpi-card__num');
    if (kpiCards.length >= 3) {
      kpiCards[0].textContent = STATE ? total : (govt >= 112 ? 100 : 0) + '%';
      kpiCards[0].parentElement.querySelector('.kpi-card__label').textContent = IS_DUN ? T('Kerusi DUN', 'DUN seats') : (STATE ? T('Kerusi Parlimen', 'Parliamentary seats') : T('Melepasi 112?', 'Clears 112?'));
      kpiCards[1].textContent = IS_DUN ? leading : '+' + flips;
      kpiCards[1].parentElement.querySelector('.kpi-card__label').textContent = IS_DUN ? T('Blok terbesar', 'Largest bloc') : (STATE ? T('Bertukar', 'Flips') : T('Kerusi bertukar dalam unjuran asas', 'Base forecast seat changes'));
      kpiCards[2].textContent = IS_DUN ? Math.floor(total / 2 + 1) : (S.econ_term != null ? '+' + S.econ_term : '+4.59') + '%';
      kpiCards[2].parentElement.querySelector('.kpi-card__label').textContent = IS_DUN ? T('Ambang majoriti', 'Majority threshold') : T('Ekonomi', 'Economy');
    }

    var coalition = document.querySelector('.coalition-text');
    if (coalition) coalition.textContent = STATE ? Object.keys(counts).sort().map(function (bloc) { return bloc + ' ' + counts[bloc]; }).join(' · ') : (sc ? 'PH ' + (sc.PH || 0) + ' · BN ' + (sc.BN || 0) + ' · PN ' + (sc.PN || 0) + ' · GPS ' + (sc.GPS || 0) + ' · GRS ' + (sc.GRS || 0) + ' · WARISAN ' + (sc.WAR || 0) : '');
  }

  /* ===== Scenario chips ===== */
  function renderScenarioChips() {
    var el = document.getElementById('scenario-chips');
    if (!el) return;
    if (STATE) {
      el.innerHTML = '<p class="muted">' + (IS_DUN ? 'Komposisi ini menggunakan keputusan DUN terkini. Halaman Senario menunjukkan perbandingan yang tersedia untuk negeri ini.' : 'Gunakan halaman Senario untuk membandingkan kedudukan GE15 dan unjuran asas di ' + esc(C.stateLabel ? C.stateLabel(STATE) : STATE) + '.') + '</p>';
      return;
    }

    var chips = S.scenarios_display || [];
    var html = '';

    for (var i = 0; i < chips.length; i++) {
      var sc = chips[i];
      var govt = sc.govt || 0;
      // KERAJAAN if govt >= 112, HANG otherwise
      var tag = govt >= 112 ? T('KERAJAAN', 'GOVERNMENT') : T('TANPA MAJORITI', 'NO MAJORITY');
      var tagClass = govt >= 112 ? 'chip--kerja' : 'chip--hang';
      html += '<button type="button" class="chip ' + tagClass + (i === currentScenario ? ' active' : '') + '" data-si="' + i + '" ' +
              'onclick="window.hk222_selectScenario(' + i + ', this)">' +
              esc(sc.label || sc.full || '') + ' <span class="chip-tag">' + tag + ' (' + govt + ')</span>' +
              '</button>';
    }

    el.innerHTML = html;
  }

  /* ===== House composition bar (canvas) ===== */
  function renderHouseBar() {
    var canvas = document.getElementById('house-bar');
    if (!canvas) return;
    var ctx = canvas.getContext('2d');
    var w = canvas.width, h = canvas.height;
    var css = window.getComputedStyle(document.documentElement);
    var chartText = css.getPropertyValue('--text').trim() || '#15243a';
    var chartMuted = css.getPropertyValue('--muted').trim() || '#607089';
    var chartLine = css.getPropertyValue('--line-strong').trim() || '#aebdce';

    ctx.clearRect(0, 0, w, h);
    ctx.font = '-apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif';

    var counts = getScenarioCounts();
    var total = 0;
    var govtBlocs = S.govt_blocs || [];
    var oppBlocs = S.opp_blocs || [];

    // Build segments in order: govt first, then opp
    var segments = [];
    for (var i = 0; i < govtBlocs.length; i++) {
      var b = govtBlocs[i];
      var c = counts[b] || 0;
      if (c > 0) segments.push({ bloc: b, count: c, color: blocColor(b) });
      total += c;
    }
    for (var j = 0; j < oppBlocs.length; j++) {
      var b2 = oppBlocs[j];
      var c2 = counts[b2] || 0;
      if (c2 > 0) segments.push({ bloc: b2, count: c2, color: blocColor(b2) });
      total += c2;
    }

    // Include any blocs not in lists
    for (var key in counts) {
      if (counts.hasOwnProperty(key)) {
        var found = false;
        for (var k = 0; k < segments.length; k++) {
          if (segments[k].bloc === key) { found = true; break; }
        }
        if (!found) {
          segments.push({ bloc: key, count: counts[key], color: blocColor(key) });
          total += counts[key];
        }
      }
    }

    // Draw segments
    var barY = 30;
    var barH = 40;
    var labelY = barY + barH / 2;
    var x = 0;
    var segW = w / total;
    for (var s = 0; s < segments.length; s++) {
      ctx.fillStyle = segments[s].color;
      var segWidth = segW * segments[s].count;
      ctx.fillRect(x, barY, segWidth, barH);
      // Label inside if segment is wide enough
      ctx.fillStyle = '#fff';
      ctx.font = 'bold 11px -apple-system, BlinkMacSystemFont, sans-serif';
      ctx.textAlign = 'center';
      ctx.textBaseline = 'middle';
      if (segWidth > 30) {
        ctx.fillText(segments[s].bloc, x + segWidth / 2, labelY);
      }
      x += segWidth;
    }

    // Majority marker (112)
    var majorityX = (112 / total) * w;
    ctx.strokeStyle = '#bd3a34';
    ctx.lineWidth = 2;
    ctx.setLineDash([5, 3]);
    ctx.beginPath();
      ctx.moveTo(majorityX, barY - 8);
      ctx.lineTo(majorityX, barY + barH + 8);
    ctx.stroke();
    ctx.setLineDash([]);
    // Majority label — uses the current interface text colour for contrast.
    ctx.fillStyle = chartText;
    ctx.font = 'bold 11px -apple-system, BlinkMacSystemFont, sans-serif';
    ctx.textAlign = 'center';
    ctx.fillText(T('112 (majoriti)', '112 (majority)'), majorityX, barY - 12);

    // Axis ticks
    ctx.strokeStyle = chartLine;
    ctx.fillStyle = chartMuted;
    ctx.font = '10px -apple-system, BlinkMacSystemFont, sans-serif';
    ctx.textAlign = 'center';
    ctx.textBaseline = 'top';
    var tickStep = total <= 222 ? 50 : 10;
    for (var t = 0; t <= total; t += tickStep) {
      var tickX = (t / total) * w;
      ctx.strokeStyle = chartLine;
      ctx.beginPath();
        ctx.moveTo(tickX, barY + barH + 4);
        ctx.lineTo(tickX, barY + barH + 14);
      ctx.stroke();
      ctx.fillStyle = chartMuted;
      ctx.fillText(t, tickX, barY + barH + 16);
    }

    // Legend below — only for segments with count
    var legendY = barY + barH + 32;
    var lx = 20;
    ctx.textAlign = 'left';
    ctx.textBaseline = 'middle';
    for (var li = 0; li < segments.length; li++) {
      var seg = segments[li];
      if (lx + 80 > w) {
        lx = 20;
        legendY += 18;
      }
      ctx.fillStyle = seg.color;
      ctx.fillRect(lx, legendY - 6, 10, 12);
      ctx.fillStyle = chartMuted;
      ctx.font = '11px -apple-system, BlinkMacSystemFont, sans-serif';
      ctx.fillText(seg.bloc + ' ' + seg.count, lx + 16, legendY);
      lx += 60 + ctx.measureText(seg.bloc + ' ' + seg.count).width;
    }

    // Total label
    ctx.fillStyle = chartMuted;
    ctx.font = 'bold 11px -apple-system, BlinkMacSystemFont, sans-serif';
    ctx.textAlign = 'right';
    ctx.fillText(T('Jumlah: ' + total + ' kerusi', 'Total: ' + total + ' seats'), w - 10, legendY);
  }

  /* ===== 222-seat dot field ===== */
  function renderHouseBar() {
    var el = document.getElementById('house-bar');
    if (!el) return;
    var counts = getScenarioCounts();
    var order = (S.govt_blocs || []).concat(S.opp_blocs || []);
    Object.keys(counts).forEach(function (bloc) { if (order.indexOf(bloc) === -1) order.push(bloc); });
    var dots = [];
    order.forEach(function (bloc) {
      for (var i = 0; i < (counts[bloc] || 0); i++) dots.push({ bloc: bloc, color: blocColor(bloc) });
    });
    var rows = STATE ? [] : [10, 14, 18, 22, 26, 30, 34, 34, 34];
    if (STATE) { for (var remaining = dots.length; remaining > 0; remaining -= 10) rows.push(Math.min(10, remaining)); }
    var viewHeight = STATE ? 135 : 225;
    var html = '<svg viewBox="0 0 600 ' + viewHeight + '" role="img" aria-label="222 titik kerusi Parlimen; titik ke-112 ialah ambang majoriti">';
    var dotIndex = 0;
    for (var r = 0; r < rows.length; r++) {
      var count = rows[r];
      var gap = STATE ? 28 : 14;
      var start = 300 - ((count - 1) * gap) / 2;
      for (var col = 0; col < count && dotIndex < dots.length; col++, dotIndex++) {
        var dot = dots[dotIndex];
        html += '<circle class="house-dot' + (!STATE && dotIndex === 111 ? ' house-dot--threshold' : '') + '" cx="' + (start + col * gap) + '" cy="' + (25 + r * 18) + '" r="5.2" fill="' + dot.color + '"><title>' + dot.bloc + ' · kerusi ' + (dotIndex + 1) + '</title></circle>';
      }
    }
    html += STATE ? '<text class="house-threshold-label" x="300" y="124">' + esc(C.stateLabel ? C.stateLabel(STATE) : STATE) + ' · ' + dots.length + ' kerusi ' + (IS_DUN ? 'DUN' : 'Parlimen') + '</text></svg>' : '<path class="house-threshold-line" d="M265 194H335"/><text class="house-threshold-label" x="300" y="214">112 kerusi · ambang majoriti</text></svg>';
    el.innerHTML = html;
  }

  /* ===== Flip table ===== */
  function renderFlipTable() {
    var el = document.getElementById('flip-table');
    if (!el) return;

    if (IS_DUN) {
      var close = (DUN.seats || []).slice().sort(function (a, b) { return Number(a.margin || Infinity) - Number(b.margin || Infinity); }).slice(0, 4);
      el.innerHTML = '<thead><tr><th>Bil.</th><th>Kerusi DUN</th><th>Pemenang</th><th>Parti</th><th>Blok</th><th>Margin</th></tr></thead><tbody>' + close.map(function (seat, index) { return '<tr><td class="num">' + (index + 1) + '</td><td><strong>' + esc(seat.code) + '</strong> ' + esc(seat.name) + '</td><td>' + esc(seat.winner) + '</td><td>' + esc(seat.party) + '</td><td><span class="bloc-badge" style="background:' + blocColor(seat.bloc) + '">' + esc(seat.bloc) + '</span></td><td class="num">' + (seat.margin != null ? pct(seat.margin, 1) : '—') + '</td></tr>'; }).join('') + '</tbody>';
      return;
    }
    var flips = (S.flips_list || []).filter(function (flip) { return !STATE || (D.master || []).some(function (seat) { return seat.code === flip.code && seat.state === STATE; }); });
    var members = D.master || [];
    var masterMap = {};
    for (var i = 0; i < members.length; i++) {
      masterMap[members[i].code] = members[i];
    }

    var html = '<thead><tr><th>Bil.</th><th>Kerusi</th><th>Daerah</th><th>NEGERI</th>' +
               '<th>GE15</th><th>SEMULA</th><th>Margin GE15</th><th>Margin PRU16</th></tr></thead><tbody>';

    for (var j = 0; j < flips.length && j < 8; j++) {
      var f = flips[j];
      var m = masterMap[f.code] || {};
      html += '<tr class="flip-row ' + (f.flip ? 'flip-yes' : '') + '">' +
              '<td class="num">' + (j + 1) + '</td>' +
              '<td><strong>' + esc(f.code) + '</strong></td>' +
              '<td>' + esc(m.constituency || '') + '</td>' +
              '<td>' + esc(m.state || '') + '</td>' +
              '<td><span class="bloc-badge" style="background:' + blocColor(f.winner_ge15) + '">' + esc(f.winner_ge15) + '</span></td>' +
              '<td><span class="bloc-badge" style="background:' + blocColor(f.proj_winner) + '">' + esc(f.proj_winner) + '</span></td>' +
              '<td class="num">' + (f.margin_pct_ge15 != null ? pct(f.margin_pct_ge15, 1) : '—') + '</td>' +
              '<td class="num">' + (f.proj_margin != null ? pct(f.proj_margin, 1) : '—') + '</td>' +
              '</tr>';
    }
    html += '</tbody>';
    el.innerHTML = html;
  }

  /* ===== Tight seats ===== */
  function renderTightSeats() {
    var card = document.querySelector('.tight-card');
    if (!card) return;
    if (IS_DUN) {
      var dunTight = (DUN.seats || []).slice().sort(function (a, b) { return Number(a.margin || Infinity) - Number(b.margin || Infinity); }).slice(0, 3);
      card.innerHTML = '<h2>Kerusi DUN paling rapat</h2><p class="muted">Margin kemenangan terkecil dalam keputusan terkini.</p><div class="tight-list">' + dunTight.map(function (seat) { return '<div class="tight-item"><div class="tight-code">' + esc(seat.code) + '</div><div class="tight-name">' + esc(seat.name) + '</div><div class="tight-margin">' + pct(seat.margin, 1) + '</div></div>'; }).join('') + '</div>';
      return;
    }
    var tight = (S.tight_seats || []).filter(function (seat) { return !STATE || (D.master || []).some(function (member) { return member.code === seat.code && member.state === STATE; }); });
    var html = '<div class="tight-list">';

    for (var i = 0; i < tight.length && i < 3; i++) {
      var ts = tight[i];
      var members = D.master || [];
      var m = {};
      for (var j = 0; j < members.length; j++) {
        if (members[j].code === ts.code) { m = members[j]; break; }
      }
      html += '<div class="tight-item">' +
              '<div class="tight-code">' + esc(ts.code) + '</div>' +
              '<div class="tight-name">' + esc(m.constituency || '') + '</div>' +
              '<div class="tight-margin">' + (ts.margin != null ? pct(ts.margin, 1) : '—') + '</div>' +
              '</div>';
    }
    html += '</div>';
    card.innerHTML = html;
  }

  /* ===== History timeline ===== */
  function renderHistory() {
    var card = document.querySelector('.history-card');
    if (!card) return;
    if (IS_DUN) {
      var seats = DUN.seats || [];
      var changed = seats.filter(function (seat) { return seat.changed_hands === 'yes'; }).length;
      var comparable = seats.filter(function (seat) { return seat.previous_winner || (seat.historical_baseline || {}).winner; }).length;
      var turnout = seats.reduce(function (total, seat) { return total + Number(seat.turnout || 0); }, 0) / (seats.length || 1);
      card.innerHTML = '<h2>Rekod pilihan raya</h2><p class="muted">' + esc(ELECTION.name || 'Pilihan raya negeri terkini') + (ELECTION.date ? ' · ' + esc(ELECTION.date) : '') + '</p><div class="history-timeline"><div class="history-item"><div class="history-date">Kehadiran</div><div class="history-data"><span class="history-num">' + pct(turnout, 1) + '</span></div></div><div class="history-item"><div class="history-date">Kerusi berubah</div><div class="history-data"><span class="history-num">' + changed + '/' + comparable + '</span></div></div></div>' + (ELECTION.source_url ? '<p class="muted"><a href="' + esc(ELECTION.source_url) + '" target="_blank" rel="noopener">Sumber keputusan</a> · setakat ' + esc(ELECTION.source_as_of || '—') + '</p>' : '');
      return;
    }
    var history = D.history || [];
    if (!history.length) {
      card.innerHTML = '<p class="muted">' + T('Belum ada data sejarah', 'No history data yet') + '</p>';
      return;
    }

    var html = '<div class="history-timeline">';
    for (var i = 0; i < history.length; i++) {
      var h = history[i];
      html += '<div class="history-item">' +
              '<div class="history-date">' + esc(h.week) + '</div>' +
              '<div class="history-data">' +
              '<span class="history-num">' + fmt(h.govt_p50) + '/222</span>' +
              '<span class="history-flip">↕' + (h.flips || 0) + '</span>' +
              '</div>' +
              '</div>';
    }
    html += '</div>';
    card.innerHTML = html;
  }

  /* ===== Scenario selection ===== */
  var currentScenario = 1; // Base (the headline forecast)

  function selectScenario(si, element) {
    currentScenario = si;
    // Update chip active state
    var chips = document.querySelectorAll('.chip');
    chips.forEach(function(c) { c.classList.remove('active'); });
    if (element) element.classList.add('active');

    renderKPI();
    renderHouseBar();
  }

  /* ===== Bootstrap ===== */
  function renderAll() {
    if (STATE) {
      var label = C.stateLabel ? C.stateLabel(STATE) : STATE;
      var heading = document.querySelector('.overview-question h1');
      var intro = document.querySelector('.overview-question p');
      var compositionHeading = document.querySelector('#house-bar').closest('.section').querySelector('h2');
      if (heading) heading.textContent = IS_DUN ? 'Kerusi DUN ' + label : 'Kerusi Parlimen ' + label;
      if (intro) intro.textContent = IS_DUN ? (ELECTION.name || 'Keputusan pilihan raya negeri terkini') + (ELECTION.date ? ' · ' + ELECTION.date : '') + '.' : 'Unjuran asas bagi semua kerusi Parlimen di ' + label + '.';
      if (compositionHeading) compositionHeading.textContent = IS_DUN ? 'Komposisi kerusi DUN ' + label : 'Komposisi kerusi ' + label;
      if (IS_DUN) { var flipHeading = document.querySelector('#flip-table').closest('.section').querySelector('h2'); if (flipHeading) flipHeading.textContent = 'Kerusi DUN paling rapat'; }
    }
    if (S.bloc_colors || S.party_colors) {
      // Colours already injected by components.js, but re-apply on lang switch
    }
    renderKPI();
    renderScenarioChips();
    renderHouseBar();
    renderFlipTable();
    renderTightSeats();
    renderHistory();
  }

  // Expose to global scope for inline onclick handlers
  window.hk222_selectScenario = selectScenario;
  window.hk222_renderAll = renderAll;

  // Also make selectScenario available for chips rendered by components.js
  // (components.js calls hk222_selectScenario if it exists)

  // Auto-boot on DOM ready
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', renderAll);
  } else {
    renderAll();
  }

})();
