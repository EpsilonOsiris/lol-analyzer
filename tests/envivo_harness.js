// Runs lolan/static/envivo.js against a stub DOM and prints the HTML it wrote into each live section.
// usage: node envivo_harness.js <envivo.js> <payload.json> [stream]
const fs = require('fs');
const vm = require('vm');

const [, , scriptPath, payloadPath, mode] = process.argv;
const init = fs.readFileSync(payloadPath, 'utf8');
const els = {};
function el(id) {
  if (!els[id]) {
    els[id] = {
      id, innerHTML: '', textContent: '', hidden: false, disabled: false, open: true, value: '', dataset: {},
      addEventListener() {}, setAttribute() {}, querySelectorAll() { return []; },
    };
  }
  return els[id];
}
['live-head', 'live-ally', 'live-enemy', 'live-me', 'live-ctx', 'live-feed', 'live-note', 'live-game'].forEach(el);
el('live').dataset.stream = mode === 'stream' ? '1' : '0';
el('live-init').textContent = init;
const document = {
  getElementById(id) {
    if (mode === 'stream' && ['scout-go', 'scout-msg', 'scout-target'].includes(id)) return null;
    return el(id);
  },
  querySelectorAll() { return []; },
};
const sandbox = {
  document, console, setInterval() { return 0; }, setTimeout() { return 0; },
  fetch() { return new Promise(() => {}); }, Date, Math, JSON, String, parseFloat, parseInt,
};
sandbox.window = sandbox;
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(scriptPath, 'utf8'), sandbox);
const out = {};
['live-head', 'live-ally', 'live-enemy', 'live-me', 'live-ctx', 'live-feed', 'live-note'].forEach(function (id) {
  out[id] = els[id].innerHTML;
});
process.stdout.write(JSON.stringify(out));
