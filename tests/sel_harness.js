// Runs lolan/static/seleccion.js against a stub DOM, delivers one payload as the event stream would, and prints what it wrote.
// usage: node sel_harness.js <seleccion.js> <payload.json> [comma separated roles the viewer opened before]
const fs = require('fs');
const vm = require('vm');

const [, , scriptPath, payloadPath, opened] = process.argv;
const payload = fs.readFileSync(payloadPath, 'utf8');
const els = {};
const listeners = {};
function el(id) {
  if (!els[id]) {
    els[id] = {
      id, innerHTML: '', textContent: '', hidden: false, disabled: false, open: false, value: '', dataset: {},
      addEventListener(type, fn) { (listeners[id] = listeners[id] || []).push([type, fn]); }, setAttribute() {},
    };
  }
  return els[id];
}
[
  'sel', 'sel-chip', 'sel-phase-text', 'sel-error', 'sel-queue', 'sel-custom', 'sel-timer-phase', 'sel-timer',
  'sel-allies', 'sel-enemies', 'sel-bans-box', 'sel-bans', 'sel-bench-box', 'sel-bench', 'sel-rerolls', 'sel-draft-msg',
  'sel-draft-box', 'sel-draft', 'sel-role-box', 'sel-rol', 'sel-role-src', 'sel-conn', 'sel-live-text',
  'sel-lanes-box', 'sel-lanes-msg', 'sel-lanes-sub', 'sel-lanes',
].forEach(el);
el('sel').dataset.streamUrl = '/api/partida/stream';
el('sel').dataset.liveUrl = '/en-vivo';
let source = null;
class EventSource {
  constructor(url) { this.url = url; source = this; }
  close() {}
}
const document = { getElementById: el, activeElement: null };
const location = { pathname: '/partida', search: '', assign() {} };
const sandbox = {
  document, console, EventSource, location, history: { replaceState() {} }, URLSearchParams,
  setInterval() { return 0; }, setTimeout() { return 0; }, Date, Math, JSON, String, parseFloat, parseInt, Object, Array,
};
sandbox.window = sandbox;
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(scriptPath, 'utf8'), sandbox);
(opened || '').split(',').filter(Boolean).forEach((role) => {
  (listeners['sel-lanes'] || []).filter((l) => l[0] === 'toggle').forEach((l) => l[1]({ target: { dataset: { role }, open: true } }));
});
source.onmessage({ data: payload });
process.stdout.write(JSON.stringify({
  lanes: els['sel-lanes'].innerHTML, boxHidden: els['sel-lanes-box'].hidden, msg: els['sel-lanes-msg'].textContent,
  msgHidden: els['sel-lanes-msg'].hidden, sub: els['sel-lanes-sub'].textContent, enemies: els['sel-enemies'].innerHTML,
}));
