/* Scrollable full report with a persistent, anchor-based section navigator. */
(function () {
  'use strict';
  var REPORT = window.GE16_REPORT_MS || window.GE16_REPORT_EN || [];
  var C = window.hk222 || {};
  var STATE = C.state || '';
  var D = window.GE16_APP_DATA || {};
  var REPORT_SLUGS = { Johor: 'johor', Kedah: 'kedah', Kelantan: 'kelantan', Malacca: 'melaka', 'Negeri Sembilan': 'negeri-sembilan', Pahang: 'pahang', Perak: 'perak', Perlis: 'perlis', Penang: 'pulau-pinang', Sabah: 'sabah', Sarawak: 'sarawak', Selangor: 'selangor', Terengganu: 'terengganu' };
  function renderTable(t) {
    if (!t || !t.headers || !t.rows) return '';
    var html = '<div class="table-wrap"><table><thead><tr>';
    t.headers.forEach(function (header) { html += '<th>' + header + '</th>'; });
    html += '</tr></thead><tbody>';
    t.rows.forEach(function (row) { html += '<tr>' + row.map(function (cell) { return '<td>' + cell + '</td>'; }).join('') + '</tr>'; });
    return html + '</tbody></table></div>';
  }
  function renderContent(sec) {
    var html = '<h2 class="report-section__title">' + sec.title + '</h2>';
    if (sec.kind !== 'prose' || !Array.isArray(sec.body)) return html;
    sec.body.forEach(function (item) {
      if (typeof item === 'string') html += '<p>' + item + '</p>';
      else if (item && typeof item === 'object') {
        if (item.h) html += '<h3>' + item.h + '</h3>';
        if (item.li) html += '<ul>' + (Array.isArray(item.li) ? item.li : [item.li]).map(function (li) { return '<li>' + li + '</li>'; }).join('') + '</ul>';
        if (item.p) html += '<p>' + item.p + '</p>';
        if (item.table) html += renderTable(item.table);
        if (item.note) html += '<p class="report-note">' + item.note + '</p>';
        if (item.q) html += '<blockquote>' + item.q + '</blockquote>';
      }
    });
    return html;
  }
  function setActive(id) {
    document.querySelectorAll('.report-toc__item').forEach(function (item) { item.classList.toggle('active', item.getAttribute('href') === '#' + id); });
  }
  function renderAll() {
    var toc = document.getElementById('report-toc');
    var body = document.getElementById('report-body');
    if (!toc || !body) return;
    if (STATE && C.isDun && REPORT_SLUGS[STATE]) { renderStateReport(toc, body); return; }
    if (STATE) { renderParliamentStateReport(toc, body); return; }
    toc.innerHTML = '<nav class="report-toc__list" aria-label="Bahagian laporan">' + REPORT.map(function (sec, index) { return '<a class="report-toc__item' + (index === 0 ? ' active' : '') + '" href="#report-section-' + index + '">' + sec.title + '</a>'; }).join('') + '</nav>';
    body.innerHTML = REPORT.map(function (sec, index) { return '<section class="report-section" id="report-section-' + index + '">' + renderContent(sec) + '</section>'; }).join('');
    bindToc();
  }
  function renderParliamentStateReport(toc, body) {
    var label = C.stateLabel ? C.stateLabel(STATE) : STATE;
    var projected = {}; (D.projection || []).forEach(function (item) { projected[item.code] = item; });
    var seats = (D.master || []).filter(function (seat) { return seat.state === STATE; });
    var blocs = {};
    seats.forEach(function (seat) { var bloc = (projected[seat.code] || {}).proj_winner || seat.coalition || 'IND'; blocs[bloc] = (blocs[bloc] || 0) + 1; });
    var flips = seats.filter(function (seat) { return (projected[seat.code] || {}).flip; });
    var close = seats.map(function (seat) { var p = projected[seat.code] || {}; return { code: seat.code, name: seat.constituency, margin: Number(p.proj_margin == null ? Infinity : p.proj_margin) }; }).sort(function (a, b) { return a.margin - b.margin; }).slice(0, 5);
    var heading = document.querySelector('.report-page__header h1');
    var intro = document.querySelector('.report-page__header .muted');
    if (heading) heading.innerHTML = 'Laporan Parlimen <span>' + label + '</span>';
    if (intro) intro.textContent = 'Ringkasan unjuran PRU16 bagi kerusi Parlimen di ' + label + '.';
    var sections = [
      ['ringkasan', 'Ringkasan', '<p>' + seats.length + ' kerusi Parlimen di ' + label + ' dipaparkan dalam unjuran asas PRU16. Paparan DUN ialah set data berasingan dan boleh dipilih dalam navigasi kiri.</p>'],
      ['komposisi', 'Komposisi unjuran', '<div class="table-wrap"><table><thead><tr><th>Blok</th><th>Kerusi</th></tr></thead><tbody>' + Object.keys(blocs).sort().map(function (bloc) { return '<tr><td>' + bloc + '</td><td>' + blocs[bloc] + '</td></tr>'; }).join('') + '</tbody></table></div>'],
      ['perubahan', 'Kerusi bertukar', flips.length ? '<ul>' + flips.map(function (seat) { return '<li>' + seat.code + ' · ' + seat.constituency + '</li>'; }).join('') + '</ul>' : '<p>Tiada kerusi bertukar dalam unjuran asas ini.</p>'],
      ['rapat', 'Kerusi paling rapat', '<ul>' + close.map(function (seat) { return '<li>' + seat.code + ' · ' + seat.name + (isFinite(seat.margin) ? ' <strong>' + seat.margin.toFixed(1) + '%</strong>' : '') + '</li>'; }).join('') + '</ul>']
    ];
    toc.innerHTML = '<nav class="report-toc__list" aria-label="Bahagian laporan negeri">' + sections.map(function (section, index) { return '<a class="report-toc__item' + (index === 0 ? ' active' : '') + '" href="#report-section-' + section[0] + '">' + section[1] + '</a>'; }).join('') + '</nav>';
    body.innerHTML = sections.map(function (section) { return '<section class="report-section" id="report-section-' + section[0] + '"><h2 class="report-section__title">' + section[1] + '</h2>' + section[2] + '</section>'; }).join('');
    bindToc();
  }
  function bindToc() {
    document.querySelectorAll('.report-toc__item').forEach(function (item) { item.addEventListener('click', function () { setActive((this.getAttribute('href') || '').slice(1)); }); });
    if ('IntersectionObserver' in window) {
      var observer = new IntersectionObserver(function (entries) { entries.forEach(function (entry) { if (entry.isIntersecting) setActive(entry.target.id); }); }, { rootMargin: '-20% 0px -68% 0px' });
      document.querySelectorAll('.report-section').forEach(function (section) { observer.observe(section); });
    }
  }
  function renderStateReport(toc, body) {
    var label = C.stateLabel ? C.stateLabel(STATE) : STATE;
    var election = (C.dunData || {}).election || {};
    var heading = document.querySelector('.report-page__header h1');
    var intro = document.querySelector('.report-page__header .muted');
    if (heading) heading.innerHTML = 'Laporan DUN <span>' + label + '</span>';
    if (intro) intro.textContent = 'Analisis pilihan raya Dewan Undangan Negeri ' + label + (election.name ? ' — ' + election.name : '') + (election.date ? ', ' + election.date : '') + '.';
    body.innerHTML = '<p class="muted">Memuatkan laporan negeri…</p>';
    fetch('/state/' + REPORT_SLUGS[STATE] + '.html').then(function (response) { if (!response.ok) throw new Error('not found'); return response.text(); }).then(function (html) {
      var parsed = new DOMParser().parseFromString(html, 'text/html');
      var article = parsed.querySelector('.content');
      if (!article) throw new Error('invalid report');
      var title = article.querySelector('h1'); if (title) title.remove();
      var sections = Array.prototype.slice.call(article.querySelectorAll('h2'));
      sections.forEach(function (section, index) { section.id = 'report-section-' + index; });
      body.innerHTML = '<section class="report-section state-report-content">' + article.innerHTML + '</section>';
      toc.innerHTML = '<nav class="report-toc__list" aria-label="Bahagian laporan negeri">' + sections.map(function (section, index) { return '<a class="report-toc__item' + (index === 0 ? ' active' : '') + '" href="#report-section-' + index + '">' + section.textContent + '</a>'; }).join('') + '</nav>';
      bindToc();
    }).catch(function () {
      body.innerHTML = '<p class="muted">Laporan bagi ' + label + ' belum tersedia. Sila kembali ke paparan negeri untuk melihat data kerusi semasa.</p>';
      toc.innerHTML = '';
    });
  }
  window.hk222_renderAll = renderAll;
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', renderAll); else renderAll();
}());
