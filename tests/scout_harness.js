// Runs lolan/static/envivo.js against a stub DOM and prints what it wrote into the rivals section.
// usage: node scout_harness.js <envivo.js> <payload.json> [stream]
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
['scout-chip', 'scout-note', 'scout-body', 'scout-msg', 'scout-go', 'scout-details', 'live-scout'].forEach(el);
el('live').dataset.stream = mode === 'stream' ? '1' : '0';
el('live-init').textContent = init;
el('scout-target').value = 'Yo#LAN';
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
process.stdout.write(JSON.stringify({
  body: els['scout-body'].innerHTML, msg: els['scout-msg'].textContent, boxHidden: els['live-scout'].hidden,
  chip: els['scout-chip'].textContent, chipHidden: els['scout-chip'].hidden, noteHidden: els['scout-note'].hidden,
  open: els['scout-details'].open, scoutFlag: els['live'].dataset.scout, goDisabled: els['scout-go'].disabled,
}));
