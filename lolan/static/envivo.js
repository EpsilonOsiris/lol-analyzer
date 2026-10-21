// En vivo: draws the live game from /api/en-vivo/stream (server-sent events).
// Everything shown comes from the game client on this PC; nothing is sent anywhere.
(function () {
  var root = document.getElementById('live');
  if (!root) return;

  var stream = root.dataset.stream === '1';
  var VALUE_HELP = 'Valor en objetos: oro ya gastado en objetos (suma de sus precios). No es el oro actual.';
  var NOTE = 'Valor en objetos = oro que cada jugador ya gastó en sus objetos (suma del precio de cada uno). ' +
    'No es su oro actual: Riot solo muestra el oro actual del jugador que está jugando.';
  var NOTE_UNSPENT = '«Sin gastar» es solo una aproximación (estimado menos valor en objetos): no cuenta pociones ni ' +
    'centinelas gastados, objetos vendidos, tropas ni recompensas por objetivos.';
  var NOTE_NO_PRICES = ' No hay precios guardados y no hay conexión para bajarlos, por eso aparece «—».';
  var CHIPS = { waiting: 'Sin partida', loading: 'Cargando', playing: 'En partida', finished: 'Terminada' };
  var FEED_LIMIT = stream ? 14 : 30;

  var received = Date.now();
  var legend = '';
  var shown = {};   // last HTML written to each section, so unchanged sections are not redrawn

  function esc(v) {
    return String(v == null ? '' : v).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }
  function put(id, html) {
    if (shown[id] === html) return;
    shown[id] = html;
    var el = document.getElementById(id);
    if (el) el.innerHTML = html;
  }
  function num(n) { return n == null ? '—' : String(Math.round(n)); }
  function signed(n) { return n == null ? '' : (n > 0 ? '+' : n < 0 ? '−' : '±') + Math.abs(Math.round(n)); }
  function tone(n) { return n == null || n === 0 ? '' : n > 0 ? ' pos' : ' neg'; }
  function plural(n, one, many) { return n + ' ' + (n === 1 ? one : many); }

  function iconHtml(src, label, cls) {
    if (src) return '<img class="' + cls + '" src="' + esc(src) + '" alt="" loading="eager">';
    return '<span class="' + cls + ' ph" aria-hidden="true">' + esc((label || '?').slice(0, 2)) + '</span>';
  }

  // ---- header
  function teamHead(team, cls) {
    var t = team.totals;
    var dragons = t.dragons.length
      ? t.dragons.map(function (d) { return esc(d.label); }).join(', ') : 'ninguno';
    return '<div class="hd-team ' + cls + '">' +
      '<span class="hd-label">' + esc(team.label) + '</span>' +
      '<span class="hd-kills" title="Asesinatos">' + t.kills + '</span>' +
      '<span class="hd-obj">Torres <b>' + t.towers + '</b> · Inhibidores <b>' + t.inhibitors +
      '</b> · Heraldos <b>' + t.heralds + '</b> · Barones <b>' + t.barons + '</b></span>' +
      '<span class="hd-obj">Dragones: <b>' + dragons + '</b></span>' +
      '<span class="hd-obj" title="' + esc(legend) + '">Oro total ≈ <b>' + num(t.gold_est) + '</b></span>' +
      '<span class="hd-obj" title="' + esc(VALUE_HELP) + '">Valor en objetos <b>' + num(t.value) +
      '</b>' + (t.value_approx ? ' <abbr title="Faltan precios de algún objeto">≈</abbr>' : '') + '</span>' +
      '</div>';
  }
  function head(data) {
    var g = data.game, d = g.value_diff, gd = g.gold_diff;
    var meta = [g.mode_label, g.map_label].filter(Boolean).join(' · ');
    return '<div class="hd-clock"><span class="clock" aria-label="Tiempo de partida">' + esc(g.clock) +
      '</span><span class="hd-meta">' + esc(meta) + '</span></div>' +
      '<div class="hd-score">' + teamHead(g.teams.ally, 'ally') + '<div class="hd-sep" aria-hidden="true">vs</div>' +
      teamHead(g.teams.enemy, 'enemy') + '</div>' +
      '<div class="hd-diff" title="' + esc(legend) + '">Diferencia de oro total estimado: <b class="' +
      tone(gd).trim() + '">' + (gd == null ? '—' : '≈ ' + signed(gd)) + '</b> · en valor en objetos: <b class="' +
      tone(d).trim() + '">' + (d == null ? '—' : signed(d)) + '</b></div>';
  }

  // ---- teams
  function itemsHtml(items) {
    return items.map(function (it) {
      var title = (it.name || 'Objeto ' + it.id) + (it.count > 1 ? ' ×' + it.count : '');
      var inner = it.icon
        ? '<img src="' + esc(it.icon) + '" alt="">'
        : '<span class="ph">' + esc((it.name || String(it.id)).slice(0, 3)) + '</span>';
      return '<span class="it" title="' + esc(title) + '">' + inner +
        (it.count > 1 ? '<i>' + it.count + '</i>' : '') + '</span>';
    }).join('');
  }
  function playerRow(p, minutes, g) {
    var csMin = minutes >= 1 ? (p.cs / minutes).toFixed(1) : '';
    var who = p.champion_name + (p.is_me ? ' (tú)' : '');
    var sub = [p.role_label, p.name].filter(Boolean).join(' · ');
    var dead = p.dead
      ? '<span class="dead-tag">Muerto · <span class="respawn" data-left="' + p.respawn + '">' +
        Math.ceil(p.respawn) + '</span> s</span>' : '';
    var gdiff = p.gold_diff == null ? '' :
      '<small class="diff' + tone(p.gold_diff) + '" title="Frente a su rival de línea (' + esc(p.lane_opponent || '') +
      ')">' + signed(p.gold_diff) + '</small>';
    var goldSub = p.is_me
      ? '<small title="Tu oro real, el único que Riot muestra">actual ' + num(g.me && g.me.gold) + '</small>'
      : (p.unspent_est == null ? '' : '<small class="rough" title="Aproximado: estimado menos valor en objetos">sin gastar ≈ ' +
         num(p.unspent_est) + '</small>');
    var diff = p.value_diff == null ? '' :
      '<small class="diff' + tone(p.value_diff) + '" title="Frente a su rival de línea (' + esc(p.lane_opponent || '') +
      ')">' + signed(p.value_diff) + '</small>';
    return '<div class="prow' + (p.is_me ? ' me' : '') + (p.dead ? ' is-dead' : '') + '">' +
      '<div class="who"><span class="ic-wrap">' + iconHtml(p.icon, p.champion_name, 'champ') +
      '<i class="lvl" title="Nivel">' + p.level + '</i></span>' +
      '<span class="nm"><b>' + esc(who) + '</b><small>' + esc(sub) + '</small>' + dead + '</span></div>' +
      '<div class="kda" title="Asesinatos / muertes / asistencias">' + p.kills + '/' + p.deaths + '/' + p.assists + '</div>' +
      '<div class="cs" title="Súbditos y monstruos">' + p.cs + (csMin ? '<small>' + csMin + '/min</small>' : '') +
      '<small class="vis" title="Puntuación de visión">visión ' + p.ward_score + '</small></div>' +
      '<div class="gold" title="' + esc(legend) + '"><span class="gv">≈ ' + num(p.gold_est) + gdiff + '</span>' + goldSub + '</div>' +
      '<div class="val" title="' + esc(VALUE_HELP) + '"><span class="gv">' + num(p.value) + diff + '</span></div>' +
      '<div class="its">' + itemsHtml(p.items) + '</div></div>';
  }
  function teamHtml(team, minutes, g) {
    return '<h2>' + esc(team.label) + '</h2><div class="rows">' +
      '<div class="prow headrow"><div class="who">Jugador</div><div class="kda">KDA</div>' +
      '<div class="cs">CS</div><div class="gold" title="' + esc(legend) + '">Oro total ≈</div><div class="val" title="' + esc(VALUE_HELP) + '">Valor en objetos</div>' +
      '<div class="its">Objetos</div></div>' +
      team.players.map(function (p) { return playerRow(p, minutes, g); }).join('') + '</div>';
  }

  // ---- my panel
  function mine(g) {
    var me = g.me;
    if (!me) {
      return '<h2>Tú</h2><p class="muted">No se encontró tu cuenta entre los jugadores (¿estás viendo una repetición?).</p>';
    }
    var stats = me.stats.map(function (s) {
      return '<div class="kv"><span>' + esc(s.label) + '</span><b>' + esc(s.value) + '</b></div>';
    }).join('');
    return '<h2>Tú · ' + esc(me.champion_name) + ' <span class="muted">nivel ' + me.level + '</span></h2>' +
      '<div class="kvs">' +
      '<div class="kv big"><span>Oro actual</span><b>' + num(me.gold) + '</b></div>' +
      '<div class="kv big"><span>KDA</span><b>' + me.kills + '/' + me.deaths + '/' + me.assists + '</b></div>' +
      '<div class="kv big"><span>CS</span><b>' + me.cs + (me.cs_per_min != null ? ' <small>' + me.cs_per_min + '/min</small>' : '') + '</b></div>' +
      '<div class="kv" title="' + esc(legend) + '"><span>Oro total ≈</span><b>' + num(g.teams.ally.players.filter(function (p) { return p.is_me; }).map(function (p) { return p.gold_est; })[0]) + '</b></div>' +
      '<div class="kv" title="' + esc(VALUE_HELP) + '"><span>Valor en objetos</span><b>' + num(me.value) + '</b></div>' +
      '<div class="kv"><span>Visión</span><b>' + me.ward_score + '</b></div>' +
      (me.keystone ? '<div class="kv"><span>Runa clave</span><b>' + esc(me.keystone) + '</b></div>' : '') +
      stats + '</div>';
  }

  // ---- own history (neutral numbers, no advice)
  function history(data) {
    var ctx = data.context;
    var h = '<h2>Tú contra tu historial</h2>';
    if (!ctx) return h + '<p class="muted">Sin datos de tu cuenta en esta partida.</p>';
    var cs = ctx.cs;
    if (!ctx.has_games || !cs || !cs.has_base) {
      h += '<p class="muted">Todavía no hay suficientes partidas tuyas guardadas para comparar' +
        (stream ? '.' : '. Sincroniza tus partidas en la página Sincronizar.') + '</p>';
    } else {
      var now = 'Ahora: <b>' + cs.now + ' CS</b> a los ' + esc(cs.clock);
      if (cs.reference != null) {
        now += ' · tu promedio ' + esc(cs.scope_label) + ' a esta altura: <b>' + cs.reference + '</b>';
      }
      h += '<p class="ctx-line">' + now + ' <span class="muted">(' + plural(cs.games, 'partida', 'partidas') + ')</span></p>';
      h += '<div class="ctx-pts">' + cs.points.map(function (p) {
        return '<span class="pt' + (p.reached ? ' done' : '') + '"><b>' + p.minute + ':00</b> promedio ' + num(p.avg) +
          (p.live != null ? ' · esta partida <b>' + p.live + '</b>' : (p.reached ? ' · esta partida —' : '')) + '</span>';
      }).join('') + '</div>';
      if (cs.gold_diff_14 != null) {
        h += '<p class="note">Dif. de oro a los 14 min, tu promedio ' + esc(cs.scope_label) + ': ' + signed(cs.gold_diff_14) +
          ' (no se puede medir en vivo).</p>';
      }
    }
    var plan = ctx.plan;
    if (plan && plan.enough && plan.goals.length) {
      h += '<h3>Metas de tu plan de mejora</h3><ul class="goals">' + plan.goals.map(function (gl) {
        var cls = gl.live ? (gl.within ? ' within' : ' over') : ' off';
        return '<li class="' + cls.trim() + '"><span>' + esc(gl.label) + '</span><b>' + esc(gl.text) + '</b></li>';
      }).join('') + '</ul>';
    } else if (plan && !plan.enough) {
      h += '<p class="note">Tu plan de mejora aparece con ' + plan.min_games + ' partidas con línea de tiempo (tienes ' + plan.games + ').</p>';
    }
    return h;
  }

  // ---- feed
  function feed(g) {
    var items = g.feed.slice(0, FEED_LIMIT);
    return '<h2>Eventos</h2>' + (items.length ? '<ol class="feed">' + items.map(function (e) {
      return '<li class="' + (e.team || 'neutral') + (e.mine ? ' mine' : '') + '"><time>' + esc(e.clock) +
        '</time><span>' + esc(e.text) + '</span></li>';
    }).join('') + '</ol>' : '<p class="muted">Todavía no pasa nada.</p>');
  }

  function render(data) {
    received = Date.now();
    root.dataset.status = data.status;
    root.dataset.problem = data.problem || '';
    var tls = document.getElementById('live-tls');
    if (tls) tls.hidden = data.problem !== 'tls';
    var chip = document.getElementById('live-chip');
    if (chip) chip.textContent = CHIPS[data.status] || '';
    var game = data.game;
    var gameBox = document.getElementById('live-game');
    if (gameBox) gameBox.hidden = !game;
    var result = document.getElementById('live-result');
    if (result) result.textContent = game && game.result ? '· ' + (game.result === 'win' ? 'Victoria' : 'Derrota') : '';
    if (!game) return;
    var minutes = game.time / 60;
    legend = game.gold_model ? game.gold_model.legend : '';
    put('live-head', head(data));
    put('live-ally', teamHtml(game.teams.ally, minutes, game));
    put('live-enemy', teamHtml(game.teams.enemy, minutes, game));
    put('live-me', mine(game));
    put('live-ctx', history(data));
    put('live-feed', feed(game));
    put('live-note', esc(legend) + '<br>' + esc(NOTE) + '<span class="extra"> ' + esc(NOTE_UNSPENT) +
      (game.prices_available ? '' : esc(NOTE_NO_PRICES)) + '</span>');
    tickRespawn();
  }

  // Respawn counters run between updates so they do not jump once a second.
  function tickRespawn() {
    var spent = (Date.now() - received) / 1000;
    document.querySelectorAll('.respawn').forEach(function (el) {
      var left = parseFloat(el.dataset.left) - spent;
      el.textContent = Math.max(0, Math.ceil(left));
    });
  }
  setInterval(tickRespawn, 250);

  // ---- data source
  var conn = document.getElementById('live-conn');
  function connected(ok) { if (conn) conn.hidden = ok; }

  function parseInit() {
    try { return JSON.parse(document.getElementById('live-init').textContent); } catch (e) { return null; }
  }
  var first = parseInit();
  if (first) render(first);

  function poll() {
    fetch('/api/en-vivo', { cache: 'no-store' }).then(function (r) { return r.json(); })
      .then(function (d) { connected(true); render(d); })
      .catch(function () { connected(false); });
  }
  if (typeof EventSource === 'undefined') {
    poll();
    setInterval(poll, 2000);
  } else {
    var source = new EventSource('/api/en-vivo/stream');
    source.onmessage = function (ev) {
      try { render(JSON.parse(ev.data)); connected(true); } catch (e) { /* ignore a broken message */ }
    };
    source.onopen = function () { connected(true); };
    source.onerror = function () { connected(false); };    // EventSource reconnects by itself
  }
})();
