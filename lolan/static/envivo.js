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
  // The game reports CS rounded down to tens (for everybody) and the enemy jungler's camps only
  // when this team sees them, so CS is shown as a range, and as a lower bound for that jungler.
  var CS_HELP = 'El juego da el CS redondeado hacia abajo a la decena: el real está en ese rango.';
  var CS_PARTIAL_HELP = 'El juego solo cuenta los campamentos de la jungla enemiga que tu equipo ve: ' +
    'el real es mayor. Su oro se estima con su nivel.';
  function csText(p) {
    if (p.cs_partial) return '≥' + p.cs;
    return p.cs_max != null && p.cs_max > p.cs ? p.cs + '–' + p.cs_max : String(p.cs);
  }
  function playerRow(p, minutes, g) {
    var csMid = p.cs_max != null ? (p.cs + p.cs_max) / 2 : p.cs;
    var csMin = minutes >= 1 && !p.cs_partial ? (csMid / minutes).toFixed(1) : '';
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
      '<div class="cs" title="' + esc(p.cs_partial ? CS_PARTIAL_HELP : CS_HELP) + '">' + csText(p) +
      (p.cs_partial ? '<small class="rough">solo lo visto</small>' : '') +
      (csMin ? '<small>≈' + csMin + '/min</small>' : '') +
      '<small class="vis" title="Puntuación de visión">visión ' + p.ward_score + '</small></div>' +
      '<div class="gold" title="' + esc(p.gold_by === 'nivel' ? levelLegend(g) : legend) + '"><span class="gv">≈ ' +
      num(p.gold_est) + gdiff + '</span>' + (p.gold_by === 'nivel' ? '<small class="rough">por nivel</small>' : '') +
      goldSub + '</div>' +
      '<div class="val" title="' + esc(VALUE_HELP) + '"><span class="gv">' + num(p.value) + diff + '</span></div>' +
      '<div class="its">' + itemsHtml(p.items) + '</div></div>';
  }
  function levelLegend(g) {
    var err = g.gold_model && g.gold_model.error_level;
    return 'Estimado con su nivel, kills, asistencias y el minuto, porque el juego no cuenta los ' +
      'campamentos que hace en la niebla' + (err ? ' (error típico ±' + err + ' en esta fase).' : '.');
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
      '<div class="kv big" title="' + esc(CS_HELP) + '"><span>CS</span><b>' + me.cs + '–' + (me.cs + 9) +
      (me.cs_per_min != null ? ' <small>≈' + me.cs_per_min + '/min</small>' : '') + '</b></div>' +
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

  // ---- rivals (who is in the game): Riot's Spectator data, only what the API shows about each account
  var scoutBox = document.getElementById('live-scout');
  var scoutDetails = document.getElementById('scout-details');
  var scoutMsg = document.getElementById('scout-msg');
  var scoutGo = document.getElementById('scout-go');
  var scoutTarget = document.getElementById('scout-target');
  var scoutOpen = null;        // null until the viewer opens or closes it by hand
  var SCOUT_HINT = 'Buscando tu partida en Riot cada 30 segundos mientras esta página esté abierta.';

  function phIcon(label, title) {
    return '<span class="ph" title="' + esc(title) + '">' + esc(String(label || '?').slice(0, 2)) + '</span>';
  }
  function spellsHtml(p) {
    return '<span class="sc-spells">' + (p.spells || []).map(function (s) {
      return s.icon ? '<img src="' + esc(s.icon) + '" alt="" title="' + esc(s.name) + '">' : phIcon(s.name, s.name);
    }).join('') + '</span>';
  }
  function runesHtml(p) {
    var out = '';
    [[p.keystone, 'Runa clave'], [p.secondary, 'Árbol secundario']].forEach(function (pair) {
      var r = pair[0];
      if (!r) return;
      var title = pair[1] + ': ' + r.name;
      out += r.icon ? '<img src="' + esc(r.icon) + '" alt="" title="' + esc(title) + '">' : phIcon(r.name, title);
    });
    return '<span class="sc-runes">' + out + '</span>';
  }
  function rankBadge(r, tag) {
    if (!r) return '';
    return '<div class="rk tier-' + esc(String(r.tier).toLowerCase()) + '" title="' +
      esc(tag + ': ' + r.label + ' · ' + r.lp_text + ' · ' + r.record_text + ' esta temporada') +
      '"><small>' + esc(tag) + '</small> <b>' + esc(r.label) + '</b> ' + esc(r.lp_text) +
      ' · ' + esc(r.winrate_text) + ' <small>(' + r.games + ')</small></div>';
  }
  function rankHtml(p) {
    if (p.rank == null) return '<span class="muted">rango: sin datos</span>';
    var solo = rankBadge(p.rank.solo, 'Solo'), flex = rankBadge(p.rank.flex, 'Flex');
    if (!solo && !flex) return '<div class="rk none">Sin clasificar</div>';
    return solo + flex + (p.streak ? '<small class="streak" title="Racha de victorias">en racha</small>' : '');
  }
  function masteryHtml(p) {
    var m = p.mastery;
    if (!m) return '<span class="muted">maestría: sin datos</span>';
    var main = m.never ? 'nunca lo ha jugado'
      : '<b>' + esc(m.points_text) + '</b> pts · nivel ' + m.level + (m.last_text ? ' · ' + esc(m.last_text) : '');
    return '<div class="ms">' + main + '</div>' +
      (m.label ? '<span class="tag ' + esc(m.tone) + '">' + esc(m.label) + '</span>' : '');
  }
  function recentHtml(p) {
    var out = '';
    if (p.history_text) out += '<div class="hist">' + esc(p.history_text) + '</div>';
    var r = p.recent;
    if (r && r.known) {
      out += '<div class="rec" title="Partidas clasificatorias recientes que se pudieron revisar">Últimas ' + r.known +
        ' ranked: ' + esc(r.record_text) + (r.on_champion.length ? ' · con este campeón: ' + r.on_champion.map(function (x) {
          return (x.win ? 'V ' : 'D ') + esc(x.kda);
        }).join(', ') : '') + (r.partial ? ' <small>(parcial)</small>' : '') + '</div>';
    }
    return out;
  }
  function scoutRow(p) {
    var tags = (p.is_viewer ? ' <span class="tag me">tú</span>' : '') +
      (p.clan ? ' <span class="tag clan">de tu clan</span>' : '') + (p.bot ? ' <span class="tag">bot</span>' : '');
    var plain = p.hidden || p.bot;
    var body = plain
      ? '<div class="sc-cell sc-plain muted">' + (p.hidden ? 'Solo se muestra su campeón y sus hechizos.' : 'Jugador controlado por el juego.') + '</div>'
      : '<div class="sc-cell sc-rank">' + rankHtml(p) + '</div><div class="sc-cell sc-mast">' + masteryHtml(p) +
        '</div><div class="sc-cell sc-hist">' + recentHtml(p) + '</div>';
    return '<div class="sc-row' + (p.is_viewer ? ' me' : '') + (p.hidden ? ' is-hidden' : '') + '">' +
      '<div class="sc-who"><span class="ic-wrap">' + iconHtml(p.icon, p.champion_name, 'champ') + '</span><span class="nm"><b>' +
      esc(p.display_name) + '</b><small>' + esc(p.champion_name) + tags + '</small></span></div>' +
      '<div class="sc-cell sc-gear">' + spellsHtml(p) + (plain ? '' : runesHtml(p)) + '</div>' + body + '</div>';
  }
  function scoutTeam(title, cls, players) {
    return '<div class="sc-team ' + cls + '"><h3>' + esc(title) + '</h3>' + players.map(scoutRow).join('') + '</div>';
  }
  function bansHtml(bans) {
    function side(list, label) {
      if (!list.length) return '';
      return '<span class="bans-side"><span class="muted">' + esc(label) + '</span> ' + list.map(function (b) {
        return b.icon ? '<img src="' + esc(b.icon) + '" alt="' + esc(b.champion_name) + '" title="' + esc(b.champion_name) + '">'
          : phIcon(b.champion_name, b.champion_name);
      }).join('') + '</span>';
    }
    var html = side(bans.ally, 'Bans de tu equipo') + side(bans.enemy, 'Bans enemigos');
    return html ? '<div class="sc-bans">' + html + '</div>' : '';
  }
  // ---- duelos por línea (OP.GG) and the estimated win probability: data only, no advice
  var DUEL_TONES = { fav: 'Favorable', even: 'Parejo', unfav: 'Desfavorable', few: 'Pocos datos', none: 'Sin datos' };
  function duelIcon(side, cls) {
    var icon = side.icon
      ? '<img class="' + cls + '" src="' + esc(side.icon) + '" alt="" title="' + esc(side.champion_name) + '">'
      : '<span class="' + cls + ' ph" title="' + esc(side.champion_name) + '">' + esc(String(side.champion_name || '?').slice(0, 2)) + '</span>';
    return icon;
  }
  function duelRow(l) {
    var gamesText = l.games != null ? plural(l.games, 'partida', 'partidas') : '—';
    var wr = l.wr_text ? '<b class="duel-wr">' + esc(l.wr_text) + '</b>' : '<span class="duel-wr muted">—</span>';
    var doubt = l.low_confidence
      ? ' <span class="duel-doubt" title="La línea de estos jugadores es una suposición (la partida no dice las posiciones)">posición dudosa</span>' : '';
    var own = l.personal
      ? '<small class="duel-own" title="Tu historial guardado contra este campeón en esta línea">Tu historial: ' +
        esc(l.personal.text) + ' (' + l.personal.games + ')</small>' : '';
    return '<div class="duel tone-' + esc(l.tone) + (l.ally.is_viewer ? ' me' : '') + '">' +
      '<span class="duel-lane">' + esc(l.label) + doubt + '</span>' +
      '<span class="duel-pair">' + duelIcon(l.ally, 'champ ally') + '<i aria-hidden="true">vs</i>' + duelIcon(l.enemy, 'champ enemy') + '</span>' +
      '<span class="duel-names">' + esc(l.ally.champion_name) + ' contra ' + esc(l.enemy.champion_name) + '</span>' +
      '<span class="duel-nums">' + wr + ' <small>' + esc(gamesText) + '</small></span>' +
      '<span class="duel-chip tone-' + esc(l.tone) + '">' + esc(DUEL_TONES[l.tone] || '') + '</span>' + own + '</div>';
  }
  function duoHtml(duo, ownName) {
    if (!duo || !duo.ally || !duo.enemy) return '';
    function one(d, title) {
      var nums = d.games != null ? (d.wr_text ? d.wr_text + ' · ' : '') + plural(d.games, 'partida', 'partidas') : 'sin datos';
      return '<span class="duo-side"><b>' + esc(title) + '</b> ' + duelIcon({ icon: d.adc_icon, champion_name: d.adc_name }, 'champ mini') +
        '+' + duelIcon({ icon: d.support_icon, champion_name: d.support_name }, 'champ mini') + ' ' + esc(d.adc_name) + ' + ' +
        esc(d.support_name) + ': ' + esc(nums) + '</span>';
    }
    return '<div class="duel-duo"><span class="muted">Dúo de línea (tirador + soporte)</span> ' +
      one(duo.ally, ownName) + one(duo.enemy, 'Enemigo') + '</div>';
  }
  function probHtml(prob) {
    if (!prob) return '';
    var pct = Math.round(prob.value * 100);
    return '<div class="prob"><span class="prob-text">Probabilidad estimada: <b>' + esc(prob.text) + '</b> <small class="muted">de que gane tu equipo</small></span>' +
      '<span class="prob-bar" role="img" aria-label="' + esc('Probabilidad estimada de ganar tu equipo: ' + prob.text) + '"><i style="width:' + pct + '%"></i></span>' +
      '<small class="prob-label muted">' + esc(prob.label) + '</small></div>';
  }
  function duelosHtml(d, own) {
    if (!d || d.status === 'na') return '';
    if (d.status === 'pending') return '<div class="duelos"><p class="muted duel-msg">Calculando duelos con OP.GG…</p></div>';
    if (d.status === 'error' || d.status === 'nodata') {
      return '<div class="duelos"><p class="muted duel-msg">' + esc(d.message || 'Sin datos de OP.GG') + '</p></div>';
    }
    return '<div class="duelos">' + probHtml(d.prob) +
      '<h3>Duelos por línea <small class="muted">· OP.GG, ' + esc(d.scope) + (d.patch ? ', parche ' + esc(d.patch) : '') + '</small></h3>' +
      '<div class="duel-rows">' + d.lanes.map(duelRow).join('') + '</div>' + duoHtml(d.duo, own) +
      '<p class="duel-note muted">' + esc(d.note) + (d.status === 'partial' ? ' Faltan datos de algunos duelos.' : '') + '</p></div>';
  }
  function scoutReport(sc, status) {
    var r = sc.report, g = r.game, q = r.requests;
    var phase = status === 'playing' ? 'partida en curso' : status === 'finished' ? 'partida terminada'
      : g.length_s > 0 ? 'va por el minuto ' + Math.floor(g.length_s / 60) : 'en la pantalla de carga';
    var own = r.scouted.is_main ? 'Tu equipo' : 'Equipo de ' + r.scouted.riot_id.split('#')[0];
    var foot = 'Peticiones a Riot: ' + q.used + ' de ' + q.budget + ' como máximo' +
      (q.from_cache ? ' · ' + plural(q.from_cache, 'jugador salió', 'jugadores salieron') + ' de la caché' : '') + '.';
    var skipped = q.skipped.length ? '<p class="sc-skip">Faltó: ' + q.skipped.map(esc).join('; ') + '.</p>' : '';
    return '<p class="sc-head"><b>' + esc(r.scouted.riot_id) + '</b> · ' + esc(g.queue_label) + ' · ' + esc(phase) + '</p>' +
      duelosHtml(r.duelos, own) +
      '<div class="sc-teams">' + scoutTeam(own, 'ally', r.teams.ally) + scoutTeam('Equipo enemigo', 'enemy', r.teams.enemy) + '</div>' +
      bansHtml(r.bans) + '<p class="sc-foot muted">' + esc(foot) + '</p>' + skipped;
  }
  var SCOUT_CHIPS = { ready: '', busy: 'En espera', error: 'Sin datos', nogame: 'No está en partida' };
  function renderScout(data) {
    var sc = data.scout;
    if (!scoutBox || !sc) return;
    var hasReport = !!sc.report;
    var quiet = data.status === 'waiting' || data.status === 'loading';
    var msg = sc.message || (!hasReport && quiet ? SCOUT_HINT : '');
    // Stream mode: the box only shows up when there is something to read.
    scoutBox.hidden = stream && !hasReport && !msg;
    root.dataset.scout = hasReport ? '1' : '0';
    var chip = document.getElementById('scout-chip');
    if (chip) {
      var text = sc.busy ? 'Buscando…' : (SCOUT_CHIPS[sc.status] || '');
      chip.textContent = text;
      chip.hidden = !text;
    }
    var note = document.getElementById('scout-note');
    if (note) note.hidden = stream && !hasReport;
    if (scoutMsg) scoutMsg.textContent = msg;
    put('scout-body', hasReport ? scoutReport(sc, data.status) : (stream && msg ? '<p class="sc-empty muted">' + esc(msg) + '</p>' : ''));
    if (scoutGo) scoutGo.disabled = !!sc.busy;
    if (scoutDetails) {
      // Open on the loading screen; folded during the game (the viewer can open it, and it stays so).
      var want = scoutOpen != null ? scoutOpen : !(data.status === 'playing' || data.status === 'finished');
      if (scoutDetails.open !== want) scoutDetails.open = want;
    }
  }
  var scoutSummary = scoutDetails && scoutDetails.querySelector ? scoutDetails.querySelector('summary') : null;
  if (scoutSummary) {
    // The click comes before the browser flips `open`: remember what the viewer is asking for, so an
    // update that arrives in between does not undo it.
    scoutSummary.addEventListener('click', function () { scoutOpen = !scoutDetails.open; });
  }
  if (scoutGo) {
    scoutGo.addEventListener('click', function () {
      scoutGo.disabled = true;
      if (scoutMsg) scoutMsg.textContent = 'Buscando la partida…';
      fetch('/api/rivales/buscar', {
        method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Lolan': '1' },
        body: JSON.stringify({ jugador: scoutTarget ? scoutTarget.value : '' })
      }).then(function (r) { return r.json().then(function (d) { return { ok: r.ok, d: d }; }); })
        .then(function (res) {
          if (!res.ok) {
            if (scoutMsg) scoutMsg.textContent = res.d.error || 'No se pudo buscar la partida.';
            scoutGo.disabled = false;
            return;
          }
          renderScout({ status: root.dataset.status, scout: res.d });
        })
        .catch(function () {
          if (scoutMsg) scoutMsg.textContent = 'No se pudo hablar con el panel.';
          scoutGo.disabled = false;
        });
    });
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
    renderScout(data);
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
