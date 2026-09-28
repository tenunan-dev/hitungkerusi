/* ==================================================================
   Hitung Kerusi 222 — Maps Renderer (Vercel)
   Uses a native SVG for a simplified Malaysia map divided by state.
   Each state region shows seat count; click to drill into seats.
   ================================================================== */
(function () {
  'use strict';

  var D = window.GE16_APP_DATA || {};
  var S = D.summary || {};
  var C = window.hk222 || {};
  var CONTEXT_STATE = C.state || '';
  var IS_DUN = !!C.isDun;
  var DUN = C.dunData || {};
  var MAP_SEAT_LIMIT = 6;
  var mapLimit = MAP_SEAT_LIMIT;
  var activeState = '';

  // ---- State layout (simplified Malaysia map) ----
  // Deliberately simplified, but geographically arranged state polygons rather
  // than a treemap: Peninsular Malaysia is at left and Borneo at right.
  var STATE_REGIONS = [
    { name: "Perlis", path: "M156 64 L178 54 L199 68 L194 93 L166 96 L153 82 Z", cx: 176, cy: 76, seats: 3 },
    { name: "Kedah", path: "M166 96 L194 93 L218 112 L220 170 L202 220 L159 221 L143 165 L153 114 Z", cx: 180, cy: 158, seats: 15 },
    { name: "Penang", path: "M126 164 L140 164 L143 193 L129 203 L121 185 Z", cx: 132, cy: 183, seats: 13 },
    { name: "Perak", path: "M159 221 L202 220 L230 250 L226 323 L197 357 L151 336 L133 283 Z", cx: 180, cy: 286, seats: 24 },
    { name: "Kelantan", path: "M218 112 L254 108 L280 143 L273 208 L246 238 L202 220 L220 170 Z", cx: 242, cy: 168, seats: 14 },
    { name: "Terengganu", path: "M273 208 L291 219 L298 287 L267 322 L246 290 L246 238 Z", cx: 271, cy: 263, seats: 8 },
    { name: "Pahang", path: "M202 220 L246 238 L246 290 L267 322 L258 395 L229 432 L189 396 L197 357 L226 323 L230 250 Z", cx: 230, cy: 330, seats: 14 },
    { name: "Selangor", path: "M151 336 L197 357 L189 396 L174 424 L145 412 L131 371 Z", cx: 162, cy: 378, seats: 22 },
    { name: "Kuala Lumpur (FT)", path: "M181 374 L190 379 L188 390 L178 388 Z", cx: 184, cy: 382, seats: 11 },
    { name: "Putrajaya (FT)", path: "M180 399 L187 401 L185 409 L178 407 Z", cx: 182, cy: 404, seats: 1 },
    { name: "Negeri Sembilan", path: "M145 412 L174 424 L180 450 L159 467 L137 449 Z", cx: 158, cy: 440, seats: 8 },
    { name: "Malacca", path: "M137 449 L159 467 L155 483 L133 480 L126 462 Z", cx: 142, cy: 467, seats: 6 },
    { name: "Johor", path: "M159 467 L180 450 L229 432 L258 395 L270 438 L264 489 L240 529 L205 549 L171 532 L155 483 Z", cx: 211, cy: 485, seats: 26 },
    { name: "Sarawak", path: "M404 227 L440 189 L510 167 L572 177 L617 212 L605 259 L560 282 L515 314 L456 309 L420 281 Z", cx: 508, cy: 244, seats: 31 },
    { name: "Labuan (FT)", path: "M608 244 L616 241 L621 248 L616 255 L608 252 Z", cx: 614, cy: 248, seats: 1 },
    { name: "Sabah", path: "M617 212 L651 180 L701 166 L739 181 L758 215 L750 260 L717 283 L684 322 L647 308 L627 272 L605 259 Z", cx: 686, cy: 239, seats: 25 },
  ];

  // ---- Helper: get bloc colour ----
  function getBlocColor(bloc) {
    var bc = S.bloc_colors || {};
    return bc[bloc] || '#64748b';
  }

  // ---- Helper: get party colour ----
  function getPartyColor(party) {
    var pc = S.party_colors || {};
    return pc[party] || '#64748b';
  }

  function esc(value) {
    return String(value == null ? '' : value).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

  // ---- Helper: get seats for a state ----
  function getSeatsForState(stateName) {
    if (IS_DUN) return stateName === CONTEXT_STATE ? (DUN.seats || []).map(function (seat) { return { code: seat.code, constituency: seat.name, party: seat.party, bloc: seat.bloc, dun: seat }; }) : [];
    var members = D.master || [];
    return members.filter(function(m) { return m.state === stateName; });
  }

  // ---- State selection ----
  var LABEL_MAP = {
    "Perlis": "Perlis", "Kedah": "Kedah", "Penang": "Pulau Pinang", "Perak": "Perak",
    "Kuala Lumpur (FT)": "KL", "Putrajaya (FT)": "Putrajaya", "Selangor": "Selangor",
    "Kelantan": "Kelantan", "Terengganu": "Terengganu", "Pahang": "Pahang",
    "Negeri Sembilan": "N. Sembilan", "Malacca": "Melaka", "Johor": "Johor",
    "Sarawak": "Sarawak", "Sabah": "Sabah", "Labuan (FT)": "Labuan"
  };
  // ---- Helper: get projection for a seat ----
  function getProjection(code) {
    var proj = D.projection || [];
    for (var i = 0; i < proj.length; i++) {
      if (proj[i].code === code) return proj[i];
    }
    return null;
  }

  // ---- Render state grid ----
  function renderStateGrid() {
    var el = document.getElementById('state-grid');
    if (!el) return;

    var stateCounts = {};
    var members = D.master || [];
    for (var i = 0; i < members.length; i++) {
      var st = members[i].state;
      stateCounts[st] = (stateCounts[st] || 0) + 1;
    }

      var html = '';
    for (var j = 0; j < STATE_REGIONS.length; j++) {
      var r = STATE_REGIONS[j];
      var count = stateCounts[r.name] || r.seats;
      var label = LABEL_MAP[r.name] || r.name;
      html += '<div class="state-chip" data-name="' + r.name + '" onclick="window.hk222_selectState(\'' + r.name + '\')">' +
              label + ' <span class="state-count">' + count + '</span></div>';
    }
    el.innerHTML = html;
  }

  // ---- Render seats for selected state ----
  function renderSeats(stateName) {
    var listEl = document.getElementById('seat-list');
    var nameEl = document.getElementById('map-state-name');
    var countEl = document.getElementById('map-seat-count');

    if (!listEl || !nameEl || !countEl) return;

    var seats = getSeatsForState(stateName);
    var query = (document.getElementById('seat-search') || {}).value || '';
    query = query.toLowerCase();
    if (query) seats = seats.filter(function (seat) { return (seat.code + ' ' + seat.constituency).toLowerCase().indexOf(query) !== -1; });
    var displayLabel = LABEL_MAP[stateName] || stateName;
    nameEl.textContent = displayLabel;
    countEl.textContent = seats.length + ' kerusi';

    if (!seats.length) {
      listEl.innerHTML = '<p class="muted">Tiada data</p>';
      return;
    }

    seats.sort(function(a, b) { return a.code.localeCompare(b.code); });

    var visible = seats.slice(0, query ? seats.length : mapLimit);
    var html = '<div class="seat-grid">';
    for (var i = 0; i < visible.length; i++) {
      var s = visible[i];
      var proj = getProjection(s.code);
      var flipClass = proj && proj.flip ? 'seat-flip' : '';
      var flipIcon = proj && proj.flip ? ' ↻' : '';

      html += '<div class="seat-card ' + flipClass + '">' +
              '<div class="seat-card__code">' + s.code + '</div>' +
              '<div class="seat-card__name">' + s.constituency + '</div>' +
              '<div class="seat-card__party">' +
                '<span class="bloc-badge" style="background:' + getPartyColor(s.party) + '">' +
                s.party + '</span>' + flipIcon +
              '</div>' +
              (IS_DUN && s.dun ? '<div class="seat-card__margin">Margin: ' + (s.dun.margin != null ? Number(s.dun.margin).toFixed(1) + '%' : '—') + '</div>' : (proj && proj.margin_pct_ge15 != null ? '<div class="seat-card__margin">Margin: ' + Number(proj.margin_pct_ge15).toFixed(1) + '%</div>' : '')) +
              '</div>';
    }
    html += '</div>';
    if (!query && seats.length > visible.length) html += '<button class="load-more" id="map-seats-more" type="button">Lihat ' + Math.min(MAP_SEAT_LIMIT, seats.length - visible.length) + ' lagi</button>';
    listEl.innerHTML = html;
    var more = document.getElementById('map-seats-more');
    if (more) more.addEventListener('click', function () { mapLimit += MAP_SEAT_LIMIT; renderSeats(stateName); });

    var de = document.querySelector('.deepdive-empty');
    if (de) de.style.display = 'none';
  }

  // ---- Search filter ----
  function setupSearch() {
    var searchInput = document.getElementById('seat-search');
    if (!searchInput) return;

    searchInput.addEventListener('input', function() {
      if (activeState) renderSeats(activeState);
    });
  }

  // Native SVG fallback/renderer: keeps the map available without a third-party CDN.
  function createMapSvg(container) {
    if (IS_DUN) {
      renderDunGeoMap(container);
      return;
    }
    var shapes = '';
    for (var i = 0; i < STATE_REGIONS.length; i++) {
      var r = STATE_REGIONS[i];
      var seats = getSeatsForState(r.name);
      var blocVotes = {};
      for (var j = 0; j < seats.length; j++) {
        var bloc = seats[j].bloc || seats[j].coalition || 'IND';
        blocVotes[bloc] = (blocVotes[bloc] || 0) + 1;
      }
      var leading = '', largest = 0;
      for (var blocName in blocVotes) {
        if (blocVotes[blocName] > largest) { leading = blocName; largest = blocVotes[blocName]; }
      }
      var label = LABEL_MAP[r.name] || r.name;
      var textSize = r.name === 'Kuala Lumpur (FT)' ? 7 : (r.name === 'Putrajaya (FT)' || r.name === 'Labuan (FT)' ? 0 : 11);
      var labelY = r.cy - (textSize ? 5 : 0);
      shapes += '<g class="map-state-shape" data-state="' + r.name + '" tabindex="0" role="button" aria-label="' + label + ', ' + seats.length + ' kerusi">' +
        '<path d="' + r.path + '" fill="' + getBlocColor(leading) + '"/>' +
        (textSize ? '<text x="' + r.cx + '" y="' + labelY + '" font-size="' + textSize + '">' + label + '</text>' : '') +
        '<text class="map-seat-label" x="' + r.cx + '" y="' + (r.cy + (textSize ? 8 : 3)) + '">' + seats.length + '</text></g>';
    }
    container.innerHTML = '<svg class="map-svg" viewBox="0 0 800 640" role="img" aria-label="Rajah navigasi negeri Malaysia"><rect width="800" height="640" class="map-bg"/><text class="map-title" x="28" y="35">Peta navigasi negeri · pilih negeri</text><text class="map-region-label" x="205" y="590">SEMENANJUNG</text><text class="map-region-label" x="585" y="370">SABAH &amp; SARAWAK</text>' + shapes + '</svg>';
    var regions = container.querySelectorAll('.map-state-shape');
    regions.forEach(function (region) {
      function activate() { selectState(region.getAttribute('data-state')); }
      region.addEventListener('click', activate);
      region.addEventListener('keydown', function (event) { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); activate(); } });
    });
  }

  function renderDunGeoMap(container) {
    var geoUrl = DUN.geo_url;
    if (!geoUrl) {
      container.innerHTML = '<div class="map-empty"><strong>Peta DUN belum tersedia</strong><p>Gunakan senarai kerusi di sebelah untuk melihat keputusan.</p></div>';
      return;
    }
    container.innerHTML = '<div class="map-empty"><strong>Memuatkan peta DUN…</strong></div>';
    fetch(geoUrl).then(function (response) {
      if (!response.ok) throw new Error('Peta tidak ditemui');
      return response.json();
    }).then(function (geo) {
      var points = [];
      function collect(coords) {
        if (!Array.isArray(coords)) return;
        if (typeof coords[0] === 'number' && typeof coords[1] === 'number') { points.push(coords); return; }
        coords.forEach(collect);
      }
      (geo.features || []).forEach(function (feature) { collect((feature.geometry || {}).coordinates); });
      if (!points.length) throw new Error('Sempadan peta kosong');
      var minX = Math.min.apply(null, points.map(function (point) { return point[0]; }));
      var maxX = Math.max.apply(null, points.map(function (point) { return point[0]; }));
      var minY = Math.min.apply(null, points.map(function (point) { return point[1]; }));
      var maxY = Math.max.apply(null, points.map(function (point) { return point[1]; }));
      var width = 760, height = 570, pad = 22;
      var scale = Math.min((width - pad * 2) / (maxX - minX || 1), (height - pad * 2) / (maxY - minY || 1));
      var offsetX = (width - (maxX - minX) * scale) / 2;
      var offsetY = (height - (maxY - minY) * scale) / 2;
      function point(coord) { return (offsetX + (coord[0] - minX) * scale).toFixed(2) + ' ' + (height - offsetY - (coord[1] - minY) * scale).toFixed(2); }
      function ringPath(ring) { return ring.length ? 'M' + ring.map(point).join('L') + 'Z' : ''; }
      function pathFor(geometry) {
        if (!geometry) return '';
        if (geometry.type === 'Polygon') return (geometry.coordinates || []).map(ringPath).join('');
        if (geometry.type === 'MultiPolygon') return (geometry.coordinates || []).map(function (polygon) { return polygon.map(ringPath).join(''); }).join('');
        return '';
      }
      var seats = {}; (DUN.seats || []).forEach(function (seat) { seats[seat.code] = seat; });
      var paths = (geo.features || []).map(function (feature) {
        var props = feature.properties || {};
        var seat = seats[props.seat] || {};
        var label = props.seat + ' · ' + (props.seat_name || '') + (seat.party ? ' · ' + seat.party : '');
        return '<path class="dun-map-seat" tabindex="0" role="button" data-seat="' + props.seat + '" aria-label="' + label + '" d="' + pathFor(feature.geometry) + '" fill="' + getBlocColor(seat.bloc) + '"><title>' + label + '</title></path>';
      }).join('');
      var provenance = geo._provenance || {};
      var attribution = provenance.source ? '<p class="map-attribution">Sempadan: <a href="' + esc(provenance.source) + '" target="_blank" rel="noopener">sumber GeoJSON</a>' + (provenance.licence ? ' · ' + esc(provenance.licence) : '') + '</p>' : '';
      container.innerHTML = '<svg class="map-svg dun-map-svg" viewBox="0 0 ' + width + ' ' + height + '" role="img" aria-label="Peta sempadan kerusi DUN ' + (C.stateLabel ? C.stateLabel(CONTEXT_STATE) : CONTEXT_STATE) + '"><rect width="' + width + '" height="' + height + '" class="map-bg"/>' + paths + '</svg>' + attribution;
      container.querySelectorAll('.dun-map-seat').forEach(function (shape) {
        function selectSeat() {
          var input = document.getElementById('seat-search');
          if (input) { input.value = shape.getAttribute('data-seat'); input.dispatchEvent(new Event('input')); input.focus(); }
        }
        shape.addEventListener('click', selectSeat);
        shape.addEventListener('keydown', function (event) { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); selectSeat(); } });
      });
    }).catch(function () {
      container.innerHTML = '<div class="map-empty"><strong>Peta DUN tidak dapat dimuatkan</strong><p>Gunakan senarai kerusi di sebelah untuk melihat keputusan terkini.</p></div>';
    });
  }

  // ---- State selection ----
  function selectState(stateName) {
    if (CONTEXT_STATE && stateName !== CONTEXT_STATE) {
      window.location.href = '/app/maps.html?state=' + encodeURIComponent(stateName);
      return;
    }
    activeState = stateName;
    mapLimit = MAP_SEAT_LIMIT;
    var chips = document.querySelectorAll('.state-chip');
    chips.forEach(function(c) { c.classList.remove('active'); });

    // Find and highlight by exact name match (search by data-name attribute)
    for (var i = 0; i < chips.length; i++) {
      if (chips[i].getAttribute('data-name') === stateName) {
        chips[i].classList.add('active');
        break;
      }
    }

    // Update header with Malay label
    var nameEl = document.getElementById('map-state-name');
    if (nameEl) nameEl.textContent = LABEL_MAP[stateName] || stateName;

    renderSeats(stateName);
    document.querySelectorAll('.map-state-shape').forEach(function (shape) {
      shape.classList.toggle('active', shape.getAttribute('data-state') === stateName);
    });
  }

  // ---- Bootstrap ----
  function renderAll() {
    if (CONTEXT_STATE) {
      var label = LABEL_MAP[CONTEXT_STATE] || (C.stateLabel ? C.stateLabel(CONTEXT_STATE) : CONTEXT_STATE);
      var heading = document.querySelector('.overview-question h1');
      var intro = document.querySelector('.overview-question p');
      var metric = document.querySelector('.kpi-hero__num');
      var metricLabel = document.querySelector('.kpi-hero__label');
      var metricSub = document.querySelector('.kpi-hero__sub');
      var count = getSeatsForState(CONTEXT_STATE).length;
      if (heading) heading.textContent = IS_DUN ? 'Kerusi DUN ' + label : 'Peta kerusi ' + label;
      if (intro) intro.textContent = IS_DUN ? 'Semak setiap kerusi Dewan Undangan Negeri di ' + label + ' mengikut blok pemenang dan margin keputusan.' : 'Tumpukan semua kerusi Parlimen di ' + label + ', termasuk unjuran dan kawasan paling rapat.';
      if (metric) metric.textContent = count;
      if (metricLabel) metricLabel.textContent = (IS_DUN ? 'Kerusi DUN ' : 'Kerusi Parlimen ') + label;
      if (metricSub) metricSub.textContent = IS_DUN ? 'Keputusan pilihan raya negeri terkini' : 'Paparan ditapis kepada ' + label;
    }
    renderStateGrid();
    if (CONTEXT_STATE) { var grid = document.getElementById('state-grid'); if (grid) grid.hidden = true; }
    setupSearch();

    var stateNameEl = document.getElementById('map-state-name');
    if (stateNameEl) stateNameEl.textContent = 'Pilih Negeri';

    var container = document.getElementById('map-container');
    if (container && !container.hasChildNodes()) {
      createMapSvg(container);
    }

    // State links in the shared navigation open the same map workspace,
    // pre-focused on the selected state instead of relying on missing pages.
    var requested = CONTEXT_STATE;
    try { requested = new URLSearchParams(window.location.search).get('state') || ''; } catch (e) {}
    if (requested && STATE_REGIONS.some(function (region) { return region.name === requested; })) {
      selectState(requested);
    }
  }

  window.hk222_selectState = selectState;

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', renderAll);
  } else {
    renderAll();
  }
})();
