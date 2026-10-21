// Click-to-sort for tables with class "sortable" (cells carry data-v for numeric values).
(function () {
  document.querySelectorAll('table.sortable').forEach(function (table) {
    var tbody = table.tBodies[0];
    var heads = Array.prototype.slice.call(table.tHead.rows[0].cells);

    heads.forEach(function (th, col) {
      var btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'sort-btn';
      while (th.firstChild) btn.appendChild(th.firstChild);
      th.appendChild(btn);

      btn.addEventListener('click', function () {
        var numeric = th.dataset.type === 'num';
        var dir = th.getAttribute('aria-sort') === 'ascending' ? -1 : 1;
        if (numeric && th.getAttribute('aria-sort') === null) dir = -1; // numbers: biggest first
        heads.forEach(function (h) { h.removeAttribute('aria-sort'); });
        th.setAttribute('aria-sort', dir === 1 ? 'ascending' : 'descending');

        function key(row) {
          var cell = row.cells[col];
          var v = cell.dataset.v !== undefined ? cell.dataset.v : cell.textContent.trim();
          if (!numeric) return v.toLowerCase();
          var n = parseFloat(v);
          return isNaN(n) ? null : n;
        }
        var rows = Array.prototype.slice.call(tbody.rows);
        rows.sort(function (a, b) {
          var ka = key(a), kb = key(b);
          if (ka === null && kb === null) return 0;
          if (ka === null) return 1;
          if (kb === null) return -1;
          return (ka < kb ? -1 : ka > kb ? 1 : 0) * dir;
        });
        rows.forEach(function (r) { tbody.appendChild(r); });
      });
    });
  });
})();

// Selects marked data-autosubmit send their form as soon as the value changes (the player picker).
(function () {
  document.querySelectorAll('select[data-autosubmit]').forEach(function (sel) {
    sel.addEventListener('change', function () { sel.form.submit(); });
  });
})();

// Menu dot "En vivo": pages that stay open learn when a game starts or ends (not needed on the live page).
(function () {
  var link = document.getElementById('nav-live');
  if (!link || link.getAttribute('aria-current') || typeof fetch === 'undefined') return;
  function paint(playing) {
    var dot = link.querySelector('.live-dot');
    if (playing && !dot) {
      dot = document.createElement('span');
      dot.className = 'live-dot';
      dot.setAttribute('role', 'img');
      dot.setAttribute('aria-label', 'Hay una partida en curso');
      dot.title = 'Hay una partida en curso';
      link.appendChild(document.createTextNode(' '));
      link.appendChild(dot);
    } else if (!playing && dot) {
      dot.remove();
    }
  }
  setInterval(function () {
    if (document.hidden) return;
    fetch('/api/en-vivo/estado', { cache: 'no-store' }).then(function (r) { return r.json(); })
      .then(function (d) { paint(d.status === 'playing'); }).catch(function () {});
  }, 10000);
})();
