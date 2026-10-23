// Partida: follows the champion select of the League client through /api/partida/stream (server-sent events).
// Everything shown comes from the client on this PC; the panel only asks OP.GG about champion pairs (no player data).
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
    var sub = e.role_label
      ? esc(e.role_label) + ' <span class="sel-guess" title="Deducida con los roles de OP.GG">(' +
        (e.role_low ? 'posición dudosa' : 'supuesta') + ')</span>'
      : 'Rival ' + (i + 1);
    return '<div class="sel-row' + (champ ? '' : ' is-empty') + '">' + icon(champ, '') +
      '<div class="sel-who"><b>' + (champ ? esc(champ.name) : 'Sin revelar') + '</b><small>' + sub + '</small></div>' +
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

  // ---- lane cards: local samples at once, OP.GG as it arrives (the server fills those parts in)
  var openLanes = {};         // role -> the compact card was opened by the viewer (survives re-renders)

  function laneIcon(champs, id, cls) { return icon(champs[String(id)] || null, cls); }
  function toneBold(text, tone) { return '<b class="lane-tone ' + esc(tone || 'none') + '">' + esc(text) + '</b>'; }
  function laneRow(label, html) {
    return html ? '<dt>' + esc(label) + '</dt><dd>' + html + '</dd>' : '';
  }

  function sampleHtml(s) {
    if (!s) return '';
    if (!s.games) return '<span class="muted">' + esc(s.note || 'sin partidas de este cruce') + '</span>';
    return toneBold(s.wr_text, s.tone) + ' de líneas ganadas <span class="muted">(real ' + esc(s.raw_text) + ' · ' +
      s.games + (s.games === 1 ? ' partida · ' : ' partidas · ') + esc(s.label) +
      (s.diff14_text ? ' · ' + esc(s.diff14_text) : '') + ')</span>' +
      (s.thin ? ' <span class="badge">pocas partidas</span>' : '') +
      (s.note ? '<div class="muted lane-note">' + esc(s.note) + ': se usa ' + esc(s.label) + '</div>' : '');
  }
  function spikesHtml(sp) {
    if (!sp) return '';
    var side = function (x) { return '<div><b>' + esc(x.label) + '</b> ' + esc(x.text) + '</div>'; };
    return '<div class="lane-spikes">' + side(sp.ally) + side(sp.enemy) + '</div>' +
      (sp.duel ? '<div>' + esc(sp.duel) + '</div>' : '') +
      '<div class="muted lane-note">' + esc(sp.ally.source) + (sp.enemy.source !== sp.ally.source ? ' / ' + esc(sp.enemy.source) : '') + '</div>';
  }
  function opggHtml(c) {
    var o = c.opgg;
    if (!o) return '';
    if (o.status === 'pending') return '<span class="muted">consultando OP.GG…</span>';
    if (o.status === 'idle') return '<span class="muted">' + (c.ally.hover ? 'se consulta al fijar el campeón' : '—') + '</span>';
    if (o.status === 'error') return '<span class="muted">OP.GG no respondió</span>';
    if (o.status === 'none') return '<span class="muted">sin datos de este cruce en OP.GG</span>';
    var parts = [];
    if (o.wr_text) parts.push(toneBold(o.wr_text, o.tone) + ' de victorias del cruce <span class="muted">(' + o.games + ' partidas, todos los rangos)</span>');
    else if (o.games) parts.push('<span class="muted">pocos datos (' + o.games + ' partidas)</span>');
    if (o.lane) parts.push(esc(o.lane));
    if (o.solo) parts.push(esc(o.solo));
    var out = parts.join(' · ');
    if (o.tip) out += '<div class="lane-tip"><span class="badge">OP.GG</span> ' + esc(o.tip) + '</div>';
    return out;
  }
  function lengthHtml(l) {
    if (!l) return '';
    var parts = [];
    if (l.ours) parts.push(esc(l.label) + ': ' + esc(l.ours));
    if (l.opgg) parts.push('OP.GG: ' + esc(l.opgg));
    return parts.join('<br>');
  }
  function bestHtml(c, champs) {
    var b = c.best;
    if (!b) return '';
    var items = b.items.map(function (i) {
      return '<li class="lane-best-item">' + laneIcon(champs, i.champion_id, '') + '<span><b>' + esc(i.name) + '</b> ' +
        toneBold(i.wr_text, i.tone) + '<small class="muted"> ' + (i.games ? i.games + ' partidas' : 'sin partidas contra él') +
        (i.own_games ? ' · ' + i.own_games + ' tuyas' : '') + '</small></span></li>';
    }).join('');
    return '<ul class="lane-best">' + items + '</ul><div class="muted lane-note">' + esc(b.label) +
      (b.note ? ' (' + esc(b.note) + ')' : '') + '</div>';
  }

  function laneBody(c, champs) {
    if (c.state === 'waiting') return '<p class="muted">' + esc(c.message) + '</p>';
    var rows = '';
    if (c.state === 'pair') {
      rows += laneRow('Línea', sampleHtml(c.sample));
      rows += laneRow('Picos', spikesHtml(c.spikes));
      rows += laneRow(c.person ? c.person.name : '', c.person ? esc(c.person.text) + ' <span class="muted">(partidas propias)</span>' : '');
      rows += laneRow('OP.GG', opggHtml(c));
      rows += laneRow('Duración', lengthHtml(c.length));
    }
    rows += laneRow(c.state === 'picking' ? 'Mejores contra ' + (c.enemy.name || '') : 'Mejores contra', bestHtml(c, champs));
    return '<dl class="lane-rows">' + rows + '</dl>';
  }
  function laneHead(c, champs) {
    var enemy = laneIcon(champs, c.enemy ? c.enemy.champion_id : 0, 'foe');
    var ally = c.ally && c.ally.champion_id ? laneIcon(champs, c.ally.champion_id, c.ally.hover ? 'hover' : '') : '';
    var who = c.state === 'waiting' ? '' : ally + '<b>' + esc(c.ally.name || 'sin elegir') + '</b>' +
      (c.ally.hover ? ' <small class="muted">(eligiendo)</small>' : '') + ' <span class="lane-vs">vs</span> ' + enemy +
      '<b>' + esc(c.enemy.name) + '</b>' + (c.enemy.role_low ? ' <small class="sel-guess">(línea del rival dudosa)</small>' : '');
    return '<span class="lane-role">' + esc(c.role_label) + (c.mine ? ' · tú' : '') + '</span>' + who;
  }
  function laneCard(c, champs) {
    var head = laneHead(c, champs), body = laneBody(c, champs);
    if (c.mine) return '<article class="lane mine"><div class="lane-head">' + head + '</div>' + body + '</article>';
    return '<details class="lane" data-role="' + esc(c.role) + '"' + (openLanes[c.role] ? ' open' : '') + '><summary><span class="lane-head">' +
      head + '</span> <span class="muted lane-sum">' + esc(c.summary || '') + '</span></summary>' + body + '</details>';
  }
  function renderLanes(data) {
    var L = data.lanes, box = el('sel-lanes-box');
    if (!L) { box.hidden = true; put('sel-lanes', ''); return; }
    box.hidden = false;
    var msg = L.error || (!L.data ? 'Aún no hay muestra de líneas en este equipo (carpeta data/lineas): las cifras de abajo vendrán de OP.GG.' : '');
    el('sel-lanes-msg').textContent = msg;
    el('sel-lanes-msg').hidden = !msg;
    el('sel-lanes-sub').textContent = L.data ? (L.diamond ? '· muestra Diamante+' : '· aún sin muestra Diamante+') : '';
    var champs = L.champs || {};
    put('sel-lanes', (L.cards || []).map(function (c) { return laneCard(c, champs); }).join(''));
  }
  el('sel-lanes').addEventListener('toggle', function (ev) {
    var t = ev.target;
    if (t && t.dataset && t.dataset.role) openLanes[t.dataset.role] = !!t.open;
  }, true);

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
      renderLanes(data);
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
