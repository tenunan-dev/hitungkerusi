/* ==================================================================
   Hitung Kerusi 222 — Scenarios Renderer (Vercel)
   Pure renderer: reads window.GE16_APP_DATA, renders 8 scenario cards
   + a bloc-by-bloc comparison table.
   ================================================================== */
(function () {
  'use strict';

  var D = window.GE16_APP_DATA || {};
  var S = D.summary || {};
  var C = window.hk222 || {};
  var STATE = C.state || '';
  var IS_DUN = !!C.isDun;
  var DUN = C.dunData || {};
  var DUN_SCENARIO_PARTIAL = false;
  var SCENARIO_LIMIT = 4;
  var scenarioLimit = SCENARIO_LIMIT;

  function getBlocColor(bloc) {
    return (S.bloc_colors || {})[bloc] || '#64748b';
  }

  // Which blocs to show in the comparison table (all present across scenarios)
  var TABLE_BLOCS = ['PH', 'BN', 'PN', 'GPS', 'GRS', 'WAR', 'other'];
  function stateScenarios() {
    if (IS_DUN) {
      DUN_SCENARIO_PARTIAL = false;
      var currentDun = { label: 'Keputusan', full: 'Keputusan pilihan raya terkini', derivation: 'quantitative' };
      Object.keys(DUN.blocs || {}).forEach(function (bloc) { currentDun[bloc] = DUN.blocs[bloc]; });
      var rows = [currentDun];
      (DUN.scenarios || []).forEach(function (scenario) {
        var blocks = scenario.blocks || {};
        if (!scenario.blocks) {
          Object.keys(scenario).forEach(function (key) {
            if (['scenario', 'label', 'full', 'category', 'derivation', 'flips', 'seats_total', 'note'].indexOf(key) === -1 && typeof scenario[key] === 'number') blocks[key] = scenario[key];
          });
        }
        var suppliedTotal = Object.keys(blocks).reduce(function (total, bloc) { return total + Number(blocks[bloc] || 0); }, 0);
        if (suppliedTotal !== (DUN.seats || []).length) { DUN_SCENARIO_PARTIAL = true; return; }
        var label = scenario.label || scenario.scenario || 'Senario';
        var row = { label: label, full: scenario.full || label, derivation: scenario.derivation || (scenario.category === 'narrative' ? 'news-narrative' : 'quantitative') };
        Object.keys(blocks).forEach(function (bloc) { row[bloc] = blocks[bloc]; });
        rows.push(row);
      });
      rows.forEach(function (scenario) { scenario.govt = Object.keys(scenario).reduce(function (total, bloc) { return ['label', 'full', 'derivation', 'govt'].indexOf(bloc) === -1 ? Math.max(total, scenario[bloc] || 0) : total; }, 0); });
      TABLE_BLOCS = rows.reduce(function (all, row) { Object.keys(row).forEach(function (key) { if (['label', 'full', 'derivation', 'govt'].indexOf(key) === -1 && all.indexOf(key) === -1) all.push(key); }); return all; }, []).sort();
      return rows;
    }
    var seats = (D.master || []).filter(function (seat) { return seat.state === STATE; });
    var projected = {}; (D.projection || []).forEach(function (item) { projected[item.code] = item; });
    var current = { label: 'GE15', full: 'Kedudukan GE15', derivation: 'quantitative' };
    var base = { label: 'Base', full: 'Unjuran asas', derivation: 'quantitative' };
    seats.forEach(function (seat) {
      var now = seat.coalition || 'IND'; var future = (projected[seat.code] || {}).proj_winner || now;
      current[now] = (current[now] || 0) + 1; base[future] = (base[future] || 0) + 1;
    });
    var govt = S.govt_blocs || [];
    [current, base].forEach(function (scenario) { scenario.govt = Object.keys(scenario).reduce(function (total, bloc) { return govt.indexOf(bloc) !== -1 ? total + scenario[bloc] : total; }, 0); });
    TABLE_BLOCS = Object.keys(current).concat(Object.keys(base)).filter(function (value, index, list) { return ['label', 'full', 'derivation', 'govt'].indexOf(value) === -1 && list.indexOf(value) === index; }).sort();
    return [current, base];
  }
  function getScenarios() { return STATE ? stateScenarios() : (S.scenarios_display || []); }

  // Derivation badge text
  function derivationBadge(d) {
    if (d === 'news-narrative') return '<span class="badge-deriv news">BERITA</span>';
    if (d === 'quantitative') return '<span class="badge-deriv quant">KUANTITATIF</span>';
    return '<span class="badge-deriv">' + (d || '?').toUpperCase() + '</span>';
  }

  // Build a horizontal stacked bar for one scenario (PH..other)
  function buildMiniBar(sc) {
    var parts = '';
    var total = IS_DUN ? (DUN.seats || []).length : (STATE ? (D.master || []).filter(function (seat) { return seat.state === STATE; }).length : 222);
    for (var i = 0; i < TABLE_BLOCS.length; i++) {
      var b = TABLE_BLOCS[i];
      var count = sc[b] || 0;
      if (count <= 0) continue;
      parts += '<div class="mini-bar__seg" style="width:' + (count / total * 100) + '%;background:' +
               getBlocColor(b === 'other' ? 'IND' : b) + '" title="' + b + ' ' + count + '"></div>';
    }
    return '<div class="mini-bar">' + parts + '</div>';
  }

  // Render scenario cards
  function renderCards() {
    var el = document.getElementById('scenario-cards');
    if (!el) return;

    var scenarios = getScenarios();
    var descMap = {};
    var descriptions = S.scenario_descriptions || [];
    for (var i = 0; i < descriptions.length; i++) {
      descMap[descriptions[i].label] = descriptions[i];
    }

    var html = '';
    var visible = scenarios.slice(0, scenarioLimit);
    for (var j = 0; j < visible.length; j++) {
      var sc = visible[j];
      var govt = sc.govt || 0;
      var pMajority = STATE ? null : (govt >= 112 ? 100 : 0);
      var desc = descMap[sc.label] || {};

      html += '<div class="scenario-card">' +
              '<div class="scenario-card__head">' +
                '<div class="scenario-card__label">' + (sc.full || sc.label) + '</div>' +
                derivationBadge(desc.derivation || sc.derivation) +
              '</div>' +
              '<div class="scenario-card__govt">' + govt + '<span>/' + (STATE ? (D.master || []).filter(function (seat) { return seat.state === STATE; }).length : 222) + '</span></div>' +
              '<div class="scenario-card__pmaj">' +
                (IS_DUN ? '<span class="ok">Blok terbesar: ' + govt + ' kerusi</span>' : (STATE ? '<span class="ok">Kerusi blok kerajaan</span>' : pMajority === 100
                  ? '<span class="ok">✓ Kerajaan</span>'
                  : '<span class="bad">✗ Tiada majoriti</span>')) +
              '</div>' +
              buildMiniBar(sc) +
              '<div class="scenario-card__blocs">' +
                TABLE_BLOCS.filter(function (bloc) { return sc[bloc]; }).map(function (bloc) { return bloc + ' ' + sc[bloc]; }).join(' · ') +
              '</div>' +
              '<div class="scenario-card__desc analysis-only">' + (desc.ms || desc.en || '') + '</div>' +
              '</div>';
    }
    el.innerHTML = html;
    var more = document.getElementById('scenarios-more');
    if (more) more.remove();
    if (scenarios.length > visible.length) {
      var button = document.createElement('button');
      button.type = 'button'; button.id = 'scenarios-more'; button.className = 'load-more';
      button.textContent = 'Lihat ' + Math.min(SCENARIO_LIMIT, scenarios.length - visible.length) + ' lagi';
      button.addEventListener('click', function () { scenarioLimit += SCENARIO_LIMIT; renderCards(); });
      el.parentNode.appendChild(button);
    }
  }

  // Render comparison table
  function renderTable() {
    var thead = document.getElementById('scenario-thead');
    var tbody = document.getElementById('scenario-tbody');
    if (!thead || !tbody) return;

    var scenarios = getScenarios();

    // Head
    var head = '<tr><th>Senario</th><th>Govt</th>';
    for (var i = 0; i < TABLE_BLOCS.length; i++) {
      head += '<th>' + TABLE_BLOCS[i] + '</th>';
    }
    head += '<th>Status</th></tr>';
    thead.innerHTML = head;

    // Body
    var rows = '';
    for (var j = 0; j < scenarios.length; j++) {
      var sc = scenarios[j];
      var govt = sc.govt || 0;
      var isCurrent = sc.label === 'Base';

      rows += '<tr class="' + (isCurrent ? 'row-current' : '') + '">' +
              '<td>' + (sc.full || sc.label) + (isCurrent ? ' <span class="tag-current">SEMASA</span>' : '') + '</td>' +
              '<td class="num"><strong>' + govt + '</strong></td>';
      for (var k = 0; k < TABLE_BLOCS.length; k++) {
        var b = TABLE_BLOCS[k];
        var v = sc[b] || 0;
        rows += '<td class="num"><span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:' +
                getBlocColor(b === 'other' ? 'IND' : b) + ';margin-right:6px;"></span>' + v + '</td>';
      }
      rows += '<td>' + (IS_DUN ? '<span style="color:var(--pos)">' + (govt >= Math.floor((DUN.seats || []).length / 2) + 1 ? 'Majoriti' : 'Tiada blok majoriti') + '</span>' : (govt >= 112 ? '<span style="color:var(--pos)">✓</span>' : '<span style="color:var(--badge)">✗</span>')) + '</td>' +
              '</tr>';
    }
    tbody.innerHTML = rows;
  }

  function renderAll() {
    if (STATE) {
      var label = C.stateLabel ? C.stateLabel(STATE) : STATE;
      var heading = document.querySelector('.display-1');
      var intro = document.querySelector('.display-1 + .muted');
      var tableHeading = document.querySelector('#scenario-table').closest('.section').querySelector('h2');
      if (heading) heading.innerHTML = (IS_DUN ? 'Senario DUN <span>' : 'Senario <span>') + label + '</span>';
      if (intro) intro.textContent = IS_DUN ? ('Keputusan DUN terkini bagi ' + label + '.' + (DUN_SCENARIO_PARTIAL ? ' Senario belum dipaparkan kerana jumlah blok yang dibekalkan tidak meliputi semua kerusi.' : (getScenarios().length > 1 ? ' Senario lengkap tersedia untuk perbandingan.' : ''))) : 'Bandingkan kedudukan GE15 dengan unjuran asas bagi kerusi Parlimen di ' + label + '.';
      if (tableHeading) tableHeading.textContent = IS_DUN ? 'Komposisi DUN ' + label : 'Bandingkan blok di ' + label;
    }
    renderCards();
    renderTable();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', renderAll);
  } else {
    renderAll();
  }
})();
