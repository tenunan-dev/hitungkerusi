/* ==================================================================
   Hitung Kerusi 222 — News Renderer (Vercel)
   Pure renderer: reads window.GE16_APP_DATA.general_news.
   ================================================================== */
(function () {
  'use strict';

  var D = window.GE16_APP_DATA || {};
  var S = D.summary || {};
  var C = window.hk222 || {};
  var STATE = C.state || '';
  var IS_DUN = !!C.isDun;
  var NEWS_LIMIT = 8;
  var newsLimit = NEWS_LIMIT;
  var STATE_TERMS = { Johor: ['johor'], Kedah: ['kedah'], Kelantan: ['kelantan'], Malacca: ['melaka', 'malacca'], 'Negeri Sembilan': ['negeri sembilan', 'n. sembilan', 'n9'], Pahang: ['pahang'], Perak: ['perak'], Perlis: ['perlis'], Penang: ['penang', 'pulau pinang'], Sabah: ['sabah'], Sarawak: ['sarawak', 's\u2019wak', 's\'wak'], Selangor: ['selangor'], Terengganu: ['terengganu'], 'Kuala Lumpur (FT)': ['kuala lumpur', 'kl '], 'Putrajaya (FT)': ['putrajaya'], 'Labuan (FT)': ['labuan'] };
  var DATA_STATE = { Malacca: 'Melaka', Penang: 'Pulau Pinang' };
  var news = (D.general_news || []).filter(function (item) {
    if (!STATE) return true;
    var haystack = ((item.title || '') + ' ' + (item.query || '')).toLowerCase();
    var states = item.states || [];
    var chambers = item.chambers || [];
    var taggedState = DATA_STATE[STATE] || STATE;
    var stateMatch = states.length ? states.indexOf(taggedState) !== -1 : (STATE_TERMS[STATE] || []).some(function (term) { return haystack.indexOf(term) !== -1; });
    var chamberMatch = !chambers.length || chambers.indexOf(IS_DUN ? 'dun' : 'parliament') !== -1;
    return stateMatch && chamberMatch;
  });

  function getBlocColor(bloc) {
    return (S.bloc_colors || {})[bloc] || '#64748b';
  }

  // Category → Malay label
  var CAT_LABEL = {
    'coalition': 'Realignment',
    'candidate': 'Calon',
    'election': 'Pilihan Raya',
    'policy': 'Dasar',
    'legal': 'Undang-undang',
    'analysis': 'Analisis'
  };

  function renderCatFilters() {
    var el = document.getElementById('news-cats');
    if (!el) return;

    var cats = ['all'];
    var seen = {};
    for (var i = 0; i < news.length; i++) {
      var c = news[i].category;
      if (!seen[c]) { seen[c] = true; cats.push(c); }
    }

    var html = '';
    for (var j = 0; j < cats.length; j++) {
      var c = cats[j];
      var label = c === 'all' ? 'Semua' : (CAT_LABEL[c] || c);
      html += '<button class="cat-chip' + (c === 'all' ? ' active' : '') + '" data-cat="' + c + '">' + label + '</button>';
    }
    el.innerHTML = html;

    // Click handlers
    var chips = el.querySelectorAll('.cat-chip');
    for (var k = 0; k < chips.length; k++) {
      chips[k].addEventListener('click', function() {
        var active = el.querySelectorAll('.cat-chip.active');
        active.forEach(function(a) { a.classList.remove('active'); });
        this.classList.add('active');
        newsLimit = NEWS_LIMIT;
        renderList(this.getAttribute('data-cat'));
      });
    }
  }

  function renderList(catFilter) {
    var el = document.getElementById('news-list');
    if (!el) return;

    var items = [];
    for (var i = 0; i < news.length; i++) {
      var n = news[i];
      if (catFilter && catFilter !== 'all' && n.category !== catFilter) continue;
      items.push(n);
    }

    // Sort by date desc
    items.sort(function(a, b) { return b.date.localeCompare(a.date); });

    var html = '';
    var visible = items.slice(0, newsLimit);
    for (var j = 0; j < visible.length; j++) {
      var n = visible[j];
      var catLabel = CAT_LABEL[n.category] || n.category;
      var blocTags = '';
      if (n.blocs && n.blocs.length) {
        blocTags = n.blocs.map(function(b) {
          return '<span class="bloc-tag" style="border-color:' + getBlocColor(b) + ';color:' + getBlocColor(b) + '">' + b + '</span>';
        }).join('');
      }

      html += '<a class="news-item" href="' + (n.source_url || n.link) + '" target="_blank" rel="noopener">' +
              '<div class="news-item__meta">' +
                '<span class="news-item__date">' + n.date + '</span>' +
                '<span class="news-item__cat">' + catLabel + '</span>' +
                '<span class="news-item__src">' + n.source + '</span>' +
              '</div>' +
              '<div class="news-item__title">' + n.title + '</div>' +
              (blocTags ? '<div class="news-item__blocs">' + blocTags + '</div>' : '') +
              '</a>';
    }

    if (!items.length) {
      html = STATE ? '<p class="muted">Belum ada berita khusus ' + (C.stateLabel ? C.stateLabel(STATE) : STATE) + ' dalam penjejak semasa. <a href="/berita.html">Lihat berita persekutuan</a>.</p>' : '<p class="muted">Tiada berita dalam kategori ini.</p>';
    }

    el.innerHTML = html;
    var more = document.getElementById('news-more');
    if (more) more.remove();
    if (items.length > visible.length) {
      var button = document.createElement('button');
      button.type = 'button'; button.id = 'news-more'; button.className = 'load-more';
      button.textContent = 'Lihat ' + Math.min(NEWS_LIMIT, items.length - visible.length) + ' lagi';
      button.addEventListener('click', function () { newsLimit += NEWS_LIMIT; renderList(catFilter); });
      el.parentNode.appendChild(button);
    }
  }

  function renderAll() {
    if (STATE) {
      var label = C.stateLabel ? C.stateLabel(STATE) : STATE;
      var heading = document.querySelector('.display-1');
      var intro = document.querySelector('.display-1 + .muted');
      if (heading) heading.innerHTML = (IS_DUN ? 'Berita DUN &amp; <span>' : 'Berita &amp; <span>') + label + '</span>';
      if (intro) intro.textContent = IS_DUN ? 'Pemantauan media khusus ' + label + ' yang berkaitan dengan politik dan pilihan raya negeri.' : 'Pemantauan media khusus ' + label + ' — hanya perkembangan yang dikaitkan dengan negeri ini.';
    }
    renderCatFilters();
    renderList('all');
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', renderAll);
  } else {
    renderAll();
  }
})();
