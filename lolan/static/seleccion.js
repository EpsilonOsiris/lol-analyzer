// Partida: follows the champion select of the League client through /api/partida/stream (server-sent events).
// Everything shown comes from the client on this PC; nothing is sent anywhere.
(function () {
  var root = document.getElementById('sel');
  if (!root) return;

  var CHIPS = { esperando: 'Esperando', seleccion: 'Selección de campeones', juego: 'En partida', fin: 'Terminada' };
  var received = Date.now();
  var timer = null;           // {left, infinite} of the last message
  var prevView = null;
  var shown = {};
  var source = null;
  var params = new URLSearchParams(location.search);

  function esc(v) {
    return String(v == null ? '' : v).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }
  function el(id) { return document.getElementById(id); }
  function put(id, html) {
    if (shown[id] === html) return;
    shown[id] = html;
    var node = el(id);
    if (node) node.innerHTML = html;
  }
  function icon(champ, cls) {
    if (champ && champ.icon) {
      return '<img class="sel-champ ' + cls + '" src="' + esc(champ.icon) + '" alt="' + esc(champ.name) +
        '" title="' + esc(champ.name) + '">';
    }
    var label = champ ? champ.name : '';
    return '<span class="sel-champ ' + cls + '" title="' + esc(label) + '">' + esc(label ? label.slice(0, 2) : '?') + '</span>';
  }

  function allyRow(a) {
    var champ = a.champion || a.hover;
    var state = a.champion ? '<span class="sel-state locked">elegido</span>'
      : a.hover ? '<span class="sel-state">eligiendo…</span>' : '<span class="sel-state">sin elegir</span>';
    var role = a.role_label
      ? '<small>' + esc(a.role_label) +
        (a.role_guess ? ' <span class="sel-guess" title="Supuesta por su historial">(supuesta)</span>' : '') + '</small>'
      : '';
    var who = '<b' + (a.title ? ' title="' + esc(a.title) + '"' : '') + '>' + esc(a.name) + (a.is_me ? ' (tú)' : '') + '</b>';
    return '<div class="sel-row' + (a.is_me ? ' me' : '') + (champ ? '' : ' is-empty') + '">' +
      icon(champ, a.champion ? '' : 'hover') + '<div class="sel-who">' + who + role +
      (champ ? '<small>' + esc(champ.name) + '</small>' : '') + '</div>' + state + '</div>';
  }
  function enemyRow(e, i) {
    var champ = e.champion;
    return '<div class="sel-row' + (champ ? '' : ' is-empty') + '">' + icon(champ, '') +
      '<div class="sel-who"><b>' + (champ ? esc(champ.name) : 'Sin revelar') + '</b><small>Rival ' + (i + 1) + '</small></div>' +
      '<span class="sel-state' + (champ ? ' locked' : '') + '">' + (champ ? 'elegido' : '—') + '</span></div>';
  }
  function banSide(label, list) {
    if (!list.length) return '';
    return '<div class="sel-ban-side"><span class="lbl">' + esc(label) + '</span>' +
      list.map(function (c) { return icon(c, ''); }).join('') + '</div>';
  }

  function renderBoard(data) {
    var b = data.board;
    el('sel-queue').textContent = b.queue_name || 'Selección de campeones';
    el('sel-custom').hidden = !b.is_custom;
    el('sel-timer-phase').textContent = b.timer.text || '';
    timer = { left: b.timer.seconds_left, infinite: b.timer.infinite };
    received = Date.now();
    tick();
    put('sel-allies', b.allies.map(allyRow).join(''));
    put('sel-enemies', b.enemies.length ? b.enemies.map(enemyRow).join('') : '<p class="muted">Sin información del rival.</p>');
    var bans = banSide('Tu equipo', b.bans.ally) + banSide('Rival', b.bans.enemy);
    el('sel-bans-box').hidden = !bans;
    put('sel-bans', bans);
    var bench = b.bench;
    el('sel-bench-box').hidden = !bench.enabled;
    put('sel-bench', bench.champions.length ? bench.champions.map(function (c) { return icon(c, ''); }).join('')
      : '<span class="muted">Banca vacía.</span>');
    el('sel-rerolls').textContent = bench.can_reroll || bench.rerolls ? 'Rerolls disponibles: ' + bench.rerolls : '';
  }

  function renderDraft(data) {
    var d = data.draft || {};
    var msg = el('sel-draft-msg');
    msg.textContent = d.message || '';
    msg.hidden = !d.message;
    el('sel-draft-box').hidden = !(d.html || d.message);
    put('sel-draft', d.html || '');       // server-rendered (escaped) with the same partial as Composiciones
    var box = el('sel-role-box');
    box.hidden = d.status === 'no-aplica' || !d.main_role;
    if (d.main_role && !box.hidden) {
      var select = el('sel-rol');
      if (select.value !== d.main_role && document.activeElement !== select) select.value = d.main_role;
      el('sel-role-src').textContent = d.role_source_text ? '(' + d.role_source_text + ')' : '';
    }
  }

  function tick() {
    var node = el('sel-timer');
    if (!timer || timer.left == null) { node.textContent = ''; return; }
    if (timer.infinite) { node.textContent = 'sin límite'; return; }
    var left = Math.max(0, timer.left - Math.floor((Date.now() - received) / 1000));
    node.textContent = left + ' s';
  }
  setInterval(tick, 500);

  function render(data) {
    var view = data.view;
    root.dataset.view = view;
    el('sel-chip').textContent = CHIPS[view] || view;
    el('sel-phase-text').textContent = data.phase_text || '';
    var err = el('sel-error');
    err.textContent = data.error || '';
    err.hidden = !data.error;
    if (view === 'seleccion' && data.board) {
      renderBoard(data);
      renderDraft(data);
    }
    if (view === 'juego' && prevView !== null && prevView !== 'juego') {
      el('sel-live-text').textContent = 'La partida empezó: te llevamos a En vivo…';
      location.assign(root.dataset.liveUrl);        // the game started while this page was open
    } else if (view === 'juego') {
      el('sel-live-text').textContent = 'Hay una partida en curso.';
    }
    prevView = view;
  }

  function connected(ok) { el('sel-conn').hidden = ok; }

  function streamUrl() {
    var qs = new URLSearchParams();
    ['rol', 'cola'].forEach(function (k) { if (params.get(k)) qs.set(k, params.get(k)); });
    var s = qs.toString();
    return root.dataset.streamUrl + (s ? '?' + s : '');
  }
  function connect() {
    if (source) source.close();
    source = new EventSource(streamUrl());
    source.onmessage = function (ev) {
      try { render(JSON.parse(ev.data)); connected(true); } catch (e) { /* ignore a broken message */ }
    };
    source.onopen = function () { connected(true); };
    source.onerror = function () { connected(false); };    // EventSource reconnects by itself
  }

  // My line is a per-viewer choice kept in the address (?rol=), never stored.
  el('sel-rol').addEventListener('change', function () {
    params.set('rol', this.value);
    history.replaceState(null, '', location.pathname + '?' + params.toString());
    connect();
  });

  if (typeof EventSource === 'undefined') {
    el('sel-conn').textContent = 'Este navegador no admite actualización en vivo.';
    el('sel-conn').hidden = false;
  } else {
    connect();
  }
})();
