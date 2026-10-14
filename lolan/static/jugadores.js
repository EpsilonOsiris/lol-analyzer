// Players page: add a Riot ID, and remove a player with an inline (non-modal) confirmation.
(function () {
  var form = document.getElementById('add-form');
  var addBtn = document.getElementById('add-btn');
  var errEl = document.getElementById('add-error');
  var okEl = document.getElementById('add-ok');
  var list = document.getElementById('lista');

  function show(el, text) {
    el.hidden = !text;
    el.textContent = text || '';
  }

  function call(method, url, body) {
    var opts = { method: method, headers: { 'X-Lolan': '1', Accept: 'application/json' } };
    if (body !== undefined) {
      opts.headers['Content-Type'] = 'application/json';
      opts.body = JSON.stringify(body);
    }
    return fetch(url, opts).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (data) {
        return { ok: r.ok, status: r.status, data: data };
      });
    });
  }

  // Re-read the list from the server so counts and links stay right.
  function refreshList() {
    return fetch(location.href, { headers: { Accept: 'text/html' } })
      .then(function (r) { return r.text(); })
      .then(function (html) {
        var doc = new DOMParser().parseFromString(html, 'text/html');
        var fresh = doc.getElementById('lista');
        if (fresh) list.innerHTML = fresh.innerHTML;
      });
  }

  form.addEventListener('submit', function (ev) {
    ev.preventDefault();
    var riotId = form.elements.riot_id.value.trim();
    show(errEl, '');
    show(okEl, '');
    addBtn.disabled = true;
    call('POST', '/api/jugadores', { riot_id: riotId })
      .then(function (res) {
        if (res.ok) {
          form.reset();
          show(okEl, res.data.mensaje + (res.data.partidas
            ? ' Ya hay ' + res.data.partidas + ' partidas suyas guardadas (compartidas con otros jugadores).'
            : ''));
          return refreshList();
        }
        show(errEl, res.data.error || 'No se pudo agregar al jugador.');
      })
      .catch(function () { show(errEl, 'No se pudo contactar con el servidor local.'); })
      .then(function () { addBtn.disabled = false; });
  });

  // Row actions: "Quitar" turns into an inline "¿Quitar a X? Sí / Cancelar" in the same cell.
  list.addEventListener('click', function (ev) {
    var btn = ev.target.closest('button[data-action]');
    if (!btn) return;
    var row = btn.closest('tr');
    var cell = btn.closest('.row-actions');
    var action = btn.dataset.action;

    if (action === 'ask') {
      cell.innerHTML = '';
      var q = document.createElement('span');
      q.className = 'confirm-q';
      q.textContent = '¿Quitar a ' + row.dataset.riotId + '? ';
      var yes = document.createElement('button');
      yes.type = 'button';
      yes.className = 'btn small danger';
      yes.dataset.action = 'yes';
      yes.textContent = 'Sí, quitar';
      var no = document.createElement('button');
      no.type = 'button';
      no.className = 'btn small';
      no.dataset.action = 'no';
      no.textContent = 'Cancelar';
      cell.appendChild(q);
      cell.appendChild(yes);
      cell.appendChild(no);
      no.focus();
    } else if (action === 'no') {
      cell.innerHTML = '<button type="button" class="btn small" data-action="ask">Quitar</button>';
      cell.querySelector('button').focus();
    } else if (action === 'yes') {
      btn.disabled = true;
      show(errEl, '');
      show(okEl, '');
      call('DELETE', '/api/jugadores/' + encodeURIComponent(row.dataset.puuid))
        .then(function (res) {
          if (res.ok) {
            show(okEl, res.data.mensaje);
            return refreshList();
          }
          show(errEl, res.data.error || 'No se pudo quitar al jugador.');
          btn.disabled = false;
        })
        .catch(function () {
          show(errEl, 'No se pudo contactar con el servidor local.');
          btn.disabled = false;
        });
    }
  });
})();
