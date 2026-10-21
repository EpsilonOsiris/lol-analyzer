// "Actualizar mis datos": runs the rekey job in the background and shows its progress.
// The card (#rekey-card) is on Sincronizar and Configuración; other scripts only un-hide it.
(function () {
  var card = document.getElementById('rekey-card');
  if (!card) return;
  var btn = document.getElementById('rekey-btn');
  var panel = document.getElementById('rekey-panel');
  var stateEl = document.getElementById('rekey-state');
  var bar = document.getElementById('rekey-progress');
  var barText = document.getElementById('rekey-progress-text');
  var errEl = document.getElementById('rekey-error');
  var doneEl = document.getElementById('rekey-done');
  var logEl = document.getElementById('rekey-log');
  var timer = null;

  function show(el, text) {
    el.hidden = !text;
    el.textContent = text || '';
  }

  function render(s) {
    card.hidden = false;
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

    if (s.running) {
      stateEl.textContent = 'Actualizando…';
      show(errEl, '');
      show(doneEl, '');
    } else if (s.error) {
      stateEl.textContent = 'La actualización se detuvo';
      show(errEl, s.error + ' Lo ya actualizado está guardado: vuelve a pulsar el botón para continuar.');
      show(doneEl, '');
    } else if (s.result) {
      var r = s.result;
      stateEl.textContent = 'Actualización terminada';
      show(errEl, '');
      var text = r.nada_que_hacer ? 'Tus datos ya estaban al día.'
        : r.actualizadas + ' partidas actualizadas.';
      if ((r.no_encontradas || []).length) text += ' ' + r.no_encontradas.length + ' partidas que Riot ya no tiene se conservaron sin cambios.';
      if ((r.con_error || []).length) text += ' ' + r.con_error.length + ' con error: vuelve a pulsar el botón.';
      (r.omitidos || []).forEach(function (o) { text += ' No se pudo actualizar a ' + o.riot_id + ': ' + o.motivo + '.'; });
      if ((r.problemas || []).length) text += ' Verificación con diferencias: ' + r.problemas.join(' ');
      else if (!r.nada_que_hacer) text += ' Verificación correcta.';
      doneEl.hidden = false;
      doneEl.textContent = text + ' ';
      var link = document.createElement('a');
      link.href = '/sincronizar';
      link.textContent = 'Ir a Sincronizar';
      doneEl.appendChild(link);
    }
  }

  function poll() {
    clearTimeout(timer);
    fetch('/api/rekey', { headers: { Accept: 'application/json' } })
      .then(function (r) { return r.json(); })
      .then(function (s) {
        if (s.running || s.error || s.result || (s.lines && s.lines.length)) render(s);
        if (s.running) timer = setTimeout(poll, 2000);
      })
      .catch(function () {
        timer = setTimeout(poll, 4000);
      });
  }

  btn.addEventListener('click', function () {
    btn.disabled = true;
    fetch('/api/rekey', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Lolan': '1' },
      body: JSON.stringify({})
    })
      .then(function (r) {
        return r.json().then(function (data) { return { status: r.status, data: data }; });
      })
      .then(function (res) {
        if (res.status === 202) {
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
