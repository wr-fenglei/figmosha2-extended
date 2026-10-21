// Fake Figma desktop: runs the REAL plugin/code.js against the mock `figma`
// in a vm context, and plays the part of plugin/ui.html (WebSocket client to
// the bridge, relaying messages to and from code.js).
//
//   node --experimental-websocket tests/fake_figma/run_plugin.js \
//        --port 8787 [--name "Mock File"] [--setup setup.js]
//
// Prints "READY <name>" once the bridge has the hello.
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const { createMockFigma } = require('./mock_figma');

const args = process.argv.slice(2);
const opt = (k, d) => { const i = args.indexOf(k); return i >= 0 ? args[i + 1] : d; };
const port = Number(opt('--port', '8787'));
const name = opt('--name', 'Mock File');
const editorType = opt('--editor', 'figma');
const pluginDir = path.resolve(__dirname, '..', '..', 'plugin');

if (typeof WebSocket === 'undefined') {
  console.error('no global WebSocket: run node with --experimental-websocket (Node 20) or use Node 22+');
  process.exit(2);
}

const figma = createMockFigma({ fileName: name, editorType });
const context = vm.createContext({
  figma, __html__: '<html></html>', console, setTimeout, clearTimeout, setInterval, clearInterval,
  Promise, Uint8Array, Buffer, TextEncoder, TextDecoder,
});
vm.runInContext(fs.readFileSync(path.join(pluginDir, 'code.js'), 'utf8'), context, { filename: 'plugin/code.js' });

// ── ui.html stand-in ───────────────────────────────────────────────────
let identity = null;
let ws = null;
const toPlugin = (m) => { if (typeof figma.ui.onmessage === 'function') figma.ui.onmessage(m); };
figma.ui._listen((m) => {
  if (m.type === 'identity') { identity = m; sendHello(); return; }
  if (!ws || ws.readyState !== 1) return;
  if (typeof m.valueJson === 'string') {
    const { valueJson, ...rest } = m;
    ws.send(JSON.stringify(rest).slice(0, -1) + ',"value":' + valueJson + '}');
  } else {
    ws.send(JSON.stringify(m));
  }
});
function sendHello() {
  if (!ws || ws.readyState !== 1 || !identity) return;
  ws.send(JSON.stringify({ type: 'hello', version: '2.1', plugin: identity.pluginVersion,
    fileKey: identity.fileKey, name: identity.name, docSig: identity.docSig }));
  console.log('READY ' + identity.name);
}
function connect() {
  ws = new WebSocket(`ws://127.0.0.1:${port}/plugin`);
  ws.onopen = () => { sendHello(); toPlugin({ type: 'need-identity' }); };
  ws.onmessage = (e) => {
    const msg = JSON.parse(e.data);
    if (msg.type === 'exec') toPlugin({ type: 'exec', id: msg.id, code: msg.code, timeout: msg.timeout });
    else if (msg.type === 'ping') ws.send(JSON.stringify({ type: 'pong' }));
    else if (msg.type === 'abort') toPlugin({ type: 'abort', id: msg.id });
  };
  ws.onclose = () => setTimeout(connect, 300);
  ws.onerror = () => {};
}
connect();
