const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const test = require('node:test');
const source = fs.readFileSync(require('node:path').join(__dirname, '../src/collection/bridge.js'), 'utf8');
function harness(runtime) {
  const listeners = new Set(), replies = [];
  const context = vm.createContext({chrome: {runtime}, location: {origin: 'https://app.tin.computer'}});
  context.window = context;
  context.addEventListener = (_, listener) => listeners.add(listener);
  context.removeEventListener = (_, listener) => listeners.delete(listener);
  context.postMessage = (value, origin) => replies.push({value: JSON.parse(JSON.stringify(value)), origin});
  vm.runInContext(source, context);
  function request(type = 'DISCOVER', overrides = {}) {
    context.request = {source: 'tin.dashboard.collection.v3', id: 'request-one', type, ...overrides};
    for (const listener of listeners) {
      context.listener = listener;
      vm.runInContext('listener({source:window,origin:location.origin,data:request})', context);
    }
  }
  return {context, listeners, replies, request};
}
for (const mode of ['missing', 'removed', 'invalidated']) test(`stale bridge retires without throwing or racing a replacement: ${mode}`, () => {
  const runtime = mode === 'missing' ? undefined : {id: 'extension', sendMessage() {throw Error('Extension context invalidated.');}};
  const h = harness(runtime);
  if (mode === 'removed') delete h.context.chrome.runtime;
  assert.doesNotThrow(() => h.request());
  assert.equal(h.listeners.size, 0);
  assert.deepEqual(h.replies, []);
  assert.doesNotThrow(() => h.request());
});
test('live bridge returns bounded status and consumes runtime errors', () => {
  let callback;
  const runtime = {id:'extension', sendMessage(_message, cb) {callback=cb;}};
  const h = harness(runtime);
  h.request();
  callback({ok:true, payload:{protocol:4, account:{key:'synthetic'}, bearer:'must-not-leak', cookies:'must-not-leak'}});
  assert.equal(h.replies[0].value.ok,true);
  assert.equal(JSON.stringify(h.replies).includes('must-not-leak'),false);
  runtime.lastError = {message:'internal error details'};
  h.request();callback();
  assert.equal(h.replies[1].value.error,'extension_unavailable');
  assert.equal(JSON.stringify(h.replies).includes('internal error'),false);
});
test('runtime disappearing during a pending reply does not race a live bridge', () => {
  let callback;
  const h=harness({id:'extension',sendMessage(_message,cb){callback=cb;}});
  h.request();delete h.context.chrome.runtime;
  assert.doesNotThrow(()=>callback({ok:true,payload:{}}));
  assert.equal(h.replies.length,0);assert.equal(h.listeners.size,0);
});
test('bridge rejects malformed requests before touching the runtime', () => {
  const h=harness({id:'extension',sendMessage(){assert.fail('unexpected forwarding');}});
  h.request('UNKNOWN');h.request('PAIR',{grant:123});h.request('DISCOVER',{source:'untrusted'});
  assert.equal(h.replies.length,0);
});
