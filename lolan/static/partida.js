// Match page: opens the <details> an anchor points at, and steps through a fight on its minimap.
(function () {
  // A link or URL like #muerte-3 / #pelea-2 opens that block (they are <details> elements).
  function openFromHash() {
    var id = decodeURIComponent((location.hash || '').slice(1));
    var el = id ? document.getElementById(id) : null;
    if (el && el.tagName === 'DETAILS') {
      el.open = true;
      el.scrollIntoView({ block: 'start' });
    }
  }
  window.addEventListener('hashchange', openFromHash);
  openFromHash();

  document.querySelectorAll('[data-fight]').forEach(function (root) {
    var steps = root.querySelectorAll('svg.fight-map g.step');
    var items = root.querySelectorAll('ol.seq li');
    var controls = root.querySelector('.fight-controls');
    var caption = root.querySelector('.step-caption');
    if (!steps.length || !controls) return;

    var last = steps.length - 1;
    var current = 0;
    var timer = null;
    var prev = controls.querySelector('[data-act="prev"]');
    var next = controls.querySelector('[data-act="next"]');
    var play = controls.querySelector('[data-act="play"]');
    controls.hidden = false;

    // Each entry of the sequence becomes a button that jumps to that step.
    items.forEach(function (li, i) {
      var btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'seq-btn';
      var inner = li.firstElementChild;
      while (inner.firstChild) btn.appendChild(inner.firstChild);
      li.replaceChild(btn, inner);
      btn.addEventListener('click', function () { stop(); show(i); });
    });

    function show(k) {
      current = Math.max(0, Math.min(last, k));
      steps.forEach(function (g, i) {
        if (i === current) g.removeAttribute('hidden'); else g.setAttribute('hidden', '');
      });
      items.forEach(function (li, i) {
        if (i === current) li.setAttribute('aria-current', 'step'); else li.removeAttribute('aria-current');
      });
      var text = items[current] ? items[current].textContent.trim() : '';
      caption.textContent = (current === 0 ? '' : 'Muerte ' + current + ' de ' + last + ': ') + text;
      prev.disabled = current === 0;
      next.disabled = current === last;
    }

    function stop() {
      if (timer !== null) { clearInterval(timer); timer = null; }
      play.textContent = '▶ Reproducir';
      play.setAttribute('aria-pressed', 'false');
    }

    function start() {
      if (current >= last) show(0);
      play.textContent = '⏸ Pausar';
      play.setAttribute('aria-pressed', 'true');
      timer = setInterval(function () {
        if (current >= last) { stop(); return; }
        show(current + 1);
        if (current >= last) stop();
      }, 1600);
    }

    prev.addEventListener('click', function () { stop(); show(current - 1); });
    next.addEventListener('click', function () { stop(); show(current + 1); });
    play.addEventListener('click', function () { if (timer === null) start(); else stop(); });
    controls.addEventListener('keydown', function (e) {
      if (e.key === 'ArrowLeft') { stop(); show(current - 1); e.preventDefault(); }
      if (e.key === 'ArrowRight') { stop(); show(current + 1); e.preventDefault(); }
    });
    // Closing the fight stops the autoplay.
    var details = root.closest('details');
    if (details) details.addEventListener('toggle', function () { if (!details.open) stop(); });

    show(0);
  });
})();
