/* Shared application shell. Data and page renderers remain independent. */
(function () {
  'use strict';

  var D = window.GE16_APP_DATA || {};
  var S = D.summary || {};
  var LANG = 'ms';
  var THEME = 'auto';
  var DEPTH = 'summary';

  try { LANG = localStorage.getItem('hk222_lang') || LANG; } catch (e) {}
  try { THEME = localStorage.getItem('hk222_theme') || THEME; } catch (e) {}
  try { DEPTH = localStorage.getItem('hk222_depth') || DEPTH; } catch (e) {}
  if (LANG !== 'ms' && LANG !== 'en') LANG = 'ms';
  if (THEME !== 'light' && THEME !== 'dark' && THEME !== 'auto') THEME = 'auto';
  if (DEPTH !== 'analysis') DEPTH = 'summary';

  function T(ms, en) { return LANG === 'ms' ? ms : en; }
  function applyState() {
    var root = document.documentElement;
    root.lang = LANG;
    root.dataset.depth = DEPTH;
    if (THEME === 'auto') root.removeAttribute('data-theme');
    else root.dataset.theme = THEME;
  }
  applyState();

  var MENU = [
    { label: 'Ringkasan', en: 'Overview', href: '/app/', icon: 'grid' },
    { label: 'Peta kerusi', en: 'Seat map', href: '/app/maps.html', icon: 'map' },
    { label: 'Cari kerusi', en: 'Find a seat', href: '/app/seats.html', icon: 'search' },
    { label: 'Banding senario', en: 'Compare scenarios', href: '/app/scenarios.html', icon: 'compare' },
    { label: 'Berita & isu', en: 'News & issues', href: '/berita.html', icon: 'news' },
    { label: 'Laporan', en: 'Report', href: '/laporan.html', icon: 'report' }
  ];
  var STATES = [
    ['Johor', 'Johor'], ['Kedah', 'Kedah'], ['Kelantan', 'Kelantan'], ['Melaka', 'Malacca'], ['N. Sembilan', 'Negeri Sembilan'], ['Pahang', 'Pahang'], ['Perak', 'Perak'], ['Perlis', 'Perlis'], ['Pulau Pinang', 'Penang'], ['Sabah', 'Sabah'], ['Sarawak', 'Sarawak'], ['Selangor', 'Selangor'], ['Terengganu', 'Terengganu']
  ];
  var STATE_LABELS = { Malacca: 'Melaka', Penang: 'Pulau Pinang', 'Negeri Sembilan': 'N. Sembilan', 'Kuala Lumpur (FT)': 'Kuala Lumpur', 'Putrajaya (FT)': 'Putrajaya', 'Labuan (FT)': 'Labuan' };
  var STATE_REPORTS = { Johor: 'johor', Kedah: 'kedah', Kelantan: 'kelantan', Malacca: 'melaka', 'Negeri Sembilan': 'negeri-sembilan', Pahang: 'pahang', Perak: 'perak', Perlis: 'perlis', Penang: 'pulau-pinang', Sabah: 'sabah', Sarawak: 'sarawak', Selangor: 'selangor', Terengganu: 'terengganu' };
  function path() { return location.pathname || '/'; }
  function queryState() { var value = ''; try { value = new URLSearchParams(location.search).get('state') || ''; } catch (e) {} return STATES.some(function (state) { return state[1] === value; }) ? value : ''; }
  function queryChamber(state) { var value = ''; try { value = new URLSearchParams(location.search).get('chamber') || ''; } catch (e) {} return state && value === 'dun' && window.HK222_DUN_DATA && window.HK222_DUN_DATA[state] ? 'dun' : 'parliament'; }
  function stateLabel(state) { return STATE_LABELS[state] || state; }
  function scopedHref(href, state, chamber) {
    if (!state) return href;
    var url = new URL(href, location.origin);
    var activeChamber = chamber || queryChamber(state);
    url.searchParams.set('state', state);
    if (activeChamber === 'dun') url.searchParams.set('chamber', 'dun');
    else url.searchParams.delete('chamber');
    return url.pathname + (url.search ? url.search : '') + (url.hash || '');
  }
  function reportHref(state, chamber) { return scopedHref('/laporan.html', state, chamber); }
  function active(href, state) { return path() === href || (href === '/app/' && path() === '/app/index.html') || (!!state && href === '/laporan.html' && path() === '/laporan.html'); }
  function icon(name) {
    var paths = {
      home: '<path d="m3 10 9-7 9 7v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/><path d="M9 21v-6h6v6"/>',
      grid: '<rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/>',
      map: '<path d="m3 6 6-3 6 3 6-3v15l-6 3-6-3-6 3z"/><path d="M9 3v15M15 6v15"/>',
      search: '<circle cx="11" cy="11" r="6"/><path d="m20 20-4.2-4.2"/>',
      compare: '<path d="M4 18V9m8 9V5m8 13v-6"/><path d="M2 21h20"/>',
      news: '<path d="M4 5h16v14H4z"/><path d="M7 9h6M7 13h10M7 17h8"/>',
      report: '<path d="M6 3h9l4 4v14H6z"/><path d="M15 3v5h5M9 12h6M9 16h6"/>'
    };
    return '<svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">' + paths[name] + '</svg>';
  }
  function updateControls() {
    var lang = document.getElementById('lang-toggle');
    var theme = document.getElementById('theme-toggle');
    var summary = document.getElementById('depth-summary');
    var analysis = document.getElementById('depth-analysis');
    if (lang) lang.textContent = LANG === 'ms' ? 'EN' : 'MS';
    if (theme) theme.textContent = THEME === 'dark' ? 'Cerah' : 'Gelap';
    if (summary) summary.setAttribute('aria-pressed', DEPTH === 'summary');
    if (analysis) analysis.setAttribute('aria-pressed', DEPTH === 'analysis');
  }
  function rerender() { if (window.hk222_renderAll) window.hk222_renderAll(); }
  function cycleLang() { LANG = LANG === 'ms' ? 'en' : 'ms'; try { localStorage.setItem('hk222_lang', LANG); } catch (e) {} applyState(); renderTopbar(); rerender(); }
  function cycleTheme() { THEME = THEME === 'dark' ? 'light' : 'dark'; try { localStorage.setItem('hk222_theme', THEME); } catch (e) {} applyState(); updateControls(); }
  function setDepth(depth) { DEPTH = depth === 'analysis' ? 'analysis' : 'summary'; try { localStorage.setItem('hk222_depth', DEPTH); } catch (e) {} applyState(); updateControls(); }

  function renderTopbar(containerId) {
    var root = document.getElementById(containerId) || document.getElementById('topbar-root') || document.body;
    var selectedState = queryState();
    var isStates = !!selectedState;
    var chamber = queryChamber(selectedState);
    var isDashboard = path() === '/app/' || path() === '/app/index.html';
    var menu = MENU.map(function (m) { return '<a href="' + scopedHref(m.href, selectedState) + '" class="shell-nav__link' + (active(m.href, selectedState) ? ' active' : '') + '">' + icon(m.icon) + '<span>' + T(m.label, m.en) + '</span></a>'; }).join('');
    var stateOptions = '<option value=""' + (!selectedState ? ' selected' : '') + ' disabled>' + T('Pilih negeri', 'Choose a state') + '</option>' + STATES.map(function (s) { return '<option value="' + scopedHref(path(), s[1], chamber) + '"' + (selectedState === s[1] ? ' selected' : '') + '>' + s[0] + '</option>'; }).join('');
    var options = [{ label: 'Halaman utama', en: 'Home', href: '/' }].concat(MENU).map(function (m) { return '<option value="' + scopedHref(m.href, selectedState) + '"' + (active(m.href, selectedState) ? ' selected' : '') + '>' + T(m.label, m.en) + '</option>'; }).join('');
    document.body.classList.toggle('state-context', !!selectedState);
    document.body.classList.toggle('dun-context', chamber === 'dun');
    root.innerHTML = '<header id="topbar" class="app-shell">' +
      '<a class="brand" href="/" aria-label="Hitung Kerusi 222, halaman utama"><span>Hitung</span><strong>Kerusi 222</strong><em>PRU16</em></a>' +
      '<a class="universal-home' + (path() === '/' ? ' active' : '') + '" href="' + scopedHref('/', selectedState) + '">' + icon('home') + '<span>' + T('Halaman utama', 'Home') + '</span></a>' +
      '<section class="workspace-switcher" aria-label="' + T('Ruang kerja', 'Workspace') + '"><p class="nav-section-label">' + T('Ruang kerja', 'Workspace') + '</p><div class="scope-switch" aria-label="' + T('Pilih liputan', 'Choose coverage') + '"><a href="' + path() + '"' + (!isStates ? ' class="active"' : '') + '>' + T('Persekutuan', 'Federal') + '</a><a href="' + scopedHref(path(), selectedState || 'Sarawak', chamber) + '"' + (isStates ? ' class="active"' : '') + '>' + T('Negeri', 'States') + '</a></div>' +
      (isStates ? '<label class="state-context-select active"><span>' + T('Negeri aktif', 'Active state') + '</span><select aria-label="' + T('Pilih negeri', 'Choose state') + '" onchange="window.location.href=this.value">' + stateOptions + '</select></label><div class="chamber-switch" aria-label="' + T('Pilih dewan', 'Choose chamber') + '"><div><a href="' + scopedHref(path(), selectedState, 'parliament') + '"' + (chamber !== 'dun' ? ' class="active"' : '') + '>' + T('Parlimen', 'Parliament') + '</a><a href="' + scopedHref(path(), selectedState, 'dun') + '"' + (chamber === 'dun' ? ' class="active"' : '') + '>' + T('DUN', 'State assembly') + '</a></div></div>' : '') +
      '</section><p class="nav-section-label nav-section-label--explore">' + T('Teroka', 'Explore') + '</p><nav class="shell-nav" aria-label="Navigasi utama">' + menu + '</nav>' +
      '<div class="shell-footer"><p>' + T('Unjuran bebas berasaskan data', 'Independent data-led projection') + '</p><a href="/laporan.html">' + T('Kaedah & sumber', 'Methods & sources') + '</a></div>' +
      '<div class="mobile-shell"><a href="/" class="mobile-brand">Hitung Kerusi 222</a><select aria-label="Navigasi" onchange="window.location.href=this.value">' + options + '</select></div>' +
      '<div class="utility-bar">' + (isDashboard ? '<div class="depth-switch" aria-label="Tahap maklumat"><button id="depth-summary" onclick="window.hk222_setDepth(\'summary\')">' + T('Ringkasan', 'Summary') + '</button><button id="depth-analysis" onclick="window.hk222_setDepth(\'analysis\')">' + T('Analisis', 'Analysis') + '</button></div>' : '') + '<button id="lang-toggle" class="utility-button" onclick="window.hk222_cycleLang()"></button><button id="theme-toggle" class="utility-button" onclick="window.hk222_cycleTheme()"></button></div>' +
      '</header>';
    updateControls();
  }
  function injectColours() { var el = document.documentElement; Object.keys(S.bloc_colors || {}).forEach(function (key) { el.style.setProperty('--' + key.toLowerCase().replace(/\s/g, '-'), S.bloc_colors[key]); }); }
  var ACTIVE_STATE = queryState();
  var ACTIVE_CHAMBER = queryChamber(ACTIVE_STATE);
  window.hk222 = { T: T, MENU: MENU, renderTopbar: renderTopbar, injectColours: injectColours, isActive: active, state: ACTIVE_STATE, chamber: ACTIVE_CHAMBER, isDun: ACTIVE_CHAMBER === 'dun', dunData: (window.HK222_DUN_DATA || {})[ACTIVE_STATE] || null, stateLabel: stateLabel, scopedHref: scopedHref, reportHref: reportHref, hasStateReport: function (state) { return !!STATE_REPORTS[state]; } };
  window.hk222_cycleLang = cycleLang;
  window.hk222_cycleTheme = cycleTheme;
  window.hk222_setDepth = setDepth;
  injectColours();
  if (document.getElementById('topbar-root')) renderTopbar();
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', function () { injectColours(); if (document.getElementById('topbar-root')) renderTopbar(); });
}());
