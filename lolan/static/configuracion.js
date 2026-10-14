// Configuration page: save the settings and test the Riot key. Keys are never put back in the page.
(function () {
  var form = document.getElementById('config-form');
  var saveBtn = document.getElementById('save-btn');
  var testBtn = document.getElementById('test-btn');
  var saveErr = document.getElementById('save-error');
  var saveOk = document.getElementById('save-ok');
  var saveNext = document.getElementById('save-next');
  var testOk = document.getElementById('test-ok');
  var testErr = document.getElementById('test-error');

  function show(el, text) {
    el.hidden = !text;
    el.textContent = text || '';
  }

  function call(url, body) {
    return fetch(url, {
      method: 'POST',
      headers: { 'X-Lolan': '1', 'Content-Type': 'application/json', Accept: 'application/json' },
      body: JSON.stringify(body)
    }).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (data) {
        return { ok: r.ok, status: r.status, data: data };
      });
    });
  }

  function fields() {
    var f = form.elements;
    return {
      riot_id: f.riot_id.value.trim(),
      plataforma: f.plataforma.value,
      riot_api_key: f.riot_api_key.value.trim(),
      gemini_api_key: f.gemini_api_key.value.trim(),
      key_permanente: f.key_permanente.checked
    };
  }

  function setSaved(id, fin) {
    var el = document.getElementById(id);
    if (el && fin) el.textContent = '· guardada, termina en ' + fin;
  }

  testBtn.addEventListener('click', function () {
    var f = fields();
    show(testOk, '');
    show(testErr, '');
    testBtn.disabled = true;
    testBtn.textContent = 'Probando…';
    call('/api/configuracion/probar', { riot_api_key: f.riot_api_key, plataforma: f.plataforma })
      .then(function (res) {
        if (res.data.ok) show(testOk, res.data.mensaje);
        else show(testErr, res.data.mensaje || res.data.error || 'No se pudo probar la key.');
      })
      .catch(function () { show(testErr, 'No se pudo contactar con el panel local.'); })
      .then(function () {
        testBtn.disabled = false;
        testBtn.textContent = 'Probar key de Riot';
      });
  });

  form.addEventListener('submit', function (ev) {
    ev.preventDefault();
    var f = fields();
    show(saveErr, '');
    show(saveOk, '');
    saveNext.hidden = true;
    saveBtn.disabled = true;
    call('/api/configuracion', f)
      .then(function (res) {
        if (!res.ok) {
          show(saveErr, res.data.error || 'No se pudo guardar.');
          return;
        }
        form.elements.riot_api_key.value = '';
        form.elements.gemini_api_key.value = '';
        setSaved('riot-saved', res.data.riot_key_fin);
        setSaved('gemini-saved', res.data.gemini_key_fin);
        if (f.riot_api_key) {
          var banner = document.getElementById('key-banner');
          if (banner) banner.hidden = true;
        }
        if (res.data.listo) {
          show(saveOk, res.data.mensaje);
          saveNext.hidden = false;
          var welcome = document.getElementById('welcome');
          if (welcome) welcome.hidden = true;
        } else {
          show(saveOk, res.data.mensaje + ' Todavía falta tu key de Riot para poder empezar.');
        }
      })
      .catch(function () { show(saveErr, 'No se pudo contactar con el panel local.'); })
      .then(function () { saveBtn.disabled = false; });
  });
})();
