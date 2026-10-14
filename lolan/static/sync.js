(function () {
  var form = document.getElementById('sync-form');
  var btn = document.getElementById('sync-btn');
  var panel = document.getElementById('sync-panel');
  var stateEl = document.getElementById('sync-state');
  var bar = document.getElementById('sync-progress');
  var barText = document.getElementById('sync-progress-text');
  var errEl = document.getElementById('sync-error');
  var doneEl = document.getElementById('sync-done');
  var logEl = document.getElementById('sync-log');
  var playerEl = document.getElementById('sync-player');
  var timer = null;

  function show(el, text) {
    el.hidden = !text;
    el.textContent = text || '';
  }

  function render(s) {
    panel.hidden = false;
    btn.disabled = s.running;
    logEl.textContent = (s.lines || []).slice(-30).join('\n');
    logEl.scrollTop = logEl.scrollHeight;

    if (s.progress && s.progress.total) {
      var pct = Math.round(100 * s.progress.done / s.progress.total);
      bar.hidden = false;
      bar.value = pct;
      show(barText, s.progress.done + ' de ' + s.progress.total + ' partidas (' + pct + '%)');
    } else {
      bar.hidden = true;
      show(barText, '');
    }

    var cur = s.current;
    if (cur && cur.count > 1) {
      show(playerEl, 'Jugador ' + cur.index + ' de ' + cur.count + ': ' + cur.riot_id);
    } else {
      show(playerEl, '');
    }

    if (s.running) {
      stateEl.textContent = 'Sincronizando…';
      show(errEl, '');
      show(doneEl, '');
    } else if (s.error) {
      stateEl.textContent = 'La sincronización se detuvo';
      show(errEl, s.error);
      show(doneEl, '');
    } else if (s.result) {
      var r = s.result;
      stateEl.textContent = 'Sincronización terminada';
      show(errEl, '');
      var text = r.downloaded + ' partidas descargadas'
        + (r.missing_timelines ? ', ' + r.missing_timelines + ' sin timeline' : '')
        + (r.failed ? ', ' + r.failed + ' con error' : '') + '.';
      (r.jugadores || []).filter(function (j) { return j.error; }).forEach(function (j) {
        text += ' No se pudo sincronizar a ' + j.riot_id + ': ' + j.error;
      });
      doneEl.hidden = false;
      doneEl.textContent = text + ' ';
      var link = document.createElement('a');
      link.href = resultsHref();
      link.textContent = selectedTarget() === 'clan' ? 'Ver la comparación del clan' : 'Ver el resumen';
      doneEl.appendChild(link);
    }
  }

  function selectedCola() {
    return form.elements.cola.value;
  }

  function selectedTarget() {
    return form.elements.jugador.value;
  }

  // Where to look at what was just downloaded: the clan table, or that player's summary.
  function resultsHref() {
    var t = selectedTarget();
    var base = t === 'clan' ? '/clan?cola=' : '/?cola=';
    var extra = (t !== 'clan' && t !== 'yo') ? '&jugador=' + encodeURIComponent(t) : '';
    return base + selectedCola() + extra;
  }

  function poll() {
    clearTimeout(timer);
    fetch('/api/sync', { headers: { Accept: 'application/json' } })
      .then(function (r) { return r.json(); })
      .then(function (s) {
        // On page load only show the panel if something is running or just finished.
        if (s.running || s.error || s.result || (s.lines && s.lines.length)) render(s);
        if (s.running) timer = setTimeout(poll, 2000);
      })
      .catch(function () {
        stateEl.textContent = 'No se pudo consultar el estado; reintentando…';
        timer = setTimeout(poll, 4000);
      });
  }

  form.addEventListener('submit', function (ev) {
    ev.preventDefault();
    var max = form.elements.max.value.trim();
    var body = {
      cola: selectedCola(),
      jugador: selectedTarget(),
      max: max === '' ? null : Number(max),
      timeline: form.elements.timeline.checked
    };
    btn.disabled = true;
    fetch('/api/sync', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Lolan': '1' },
      body: JSON.stringify(body)
    })
      .then(function (r) {
        return r.json().then(function (data) { return { status: r.status, data: data }; });
      })
      .then(function (res) {
        if (res.status === 202 || res.status === 409) {
          poll();
        } else {
          btn.disabled = false;
          panel.hidden = false;
          stateEl.textContent = 'No se pudo iniciar';
          show(errEl, res.data.error || 'Error desconocido.');
        }
      })
      .catch(function () {
        btn.disabled = false;
        panel.hidden = false;
        stateEl.textContent = 'No se pudo iniciar';
        show(errEl, 'No se pudo contactar con el servidor local.');
      });
  });

  poll();
})();
