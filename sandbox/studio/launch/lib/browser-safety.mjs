// Rendered film HTML may read only its own assets and these shared runtime files.
import {readFileSync, realpathSync} from 'node:fs';
import {dirname, extname, relative, resolve, sep} from 'node:path';
import net from 'node:net';
export const FILM_ORIGIN = 'https://film.invalid';
const MIME = {'.html':'text/html', '.js':'text/javascript', '.mjs':'text/javascript', '.css':'text/css', '.json':'application/json', '.svg':'image/svg+xml', '.png':'image/png', '.jpg':'image/jpeg', '.jpeg':'image/jpeg', '.webp':'image/webp', '.gif':'image/gif', '.avif':'image/avif', '.woff':'font/woff', '.woff2':'font/woff2', '.ttf':'font/ttf', '.otf':'font/otf', '.mp3':'audio/mpeg', '.wav':'audio/wav', '.ogg':'audio/ogg', '.mp4':'video/mp4', '.webm':'video/webm'};
const IMAGES = new Set(['.svg','.png','.jpg','.jpeg','.webp','.gif','.avif']);
const inside = (root, path) => path === root || path.startsWith(root + sep);
const sensitive = path => path.split(/[\\/]/).some(part => part.startsWith('.') || /^(?:private|secrets?|credentials?|logs?|node_modules)(?:[._-]|$)/i.test(part));
const CSP = "default-src 'none'; script-src 'self' 'unsafe-inline' 'unsafe-eval'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; font-src 'self' data:; media-src 'self' blob:; connect-src 'self'; frame-src 'none'; worker-src 'none'; object-src 'none'; form-action 'none'; base-uri 'none'";

export function createFilmPolicy(source, {root = dirname(resolve(source))} = {}) {
  root = realpathSync(root);
  const page = realpathSync(source), name = relative(root, page);
  if (!inside(root, page) || sensitive(name) || extname(page).toLowerCase() !== '.html') throw new Error('Film page must be an HTML file inside the film workspace');
  const filmRoot = root;
  const asset = (path, {imageOnly = false} = {}) => {
    const requested = resolve(path), actual = realpathSync(requested), extension = extname(actual).toLowerCase();
    if (!inside(root, actual) || sensitive(relative(root, requested)) || sensitive(relative(root, actual)) ||
        !(inside(filmRoot, requested) && inside(filmRoot, actual)) ||
        !Object.hasOwn(MIME, extension) || imageOnly && !IMAGES.has(extension)) {
      throw new Error('Asset is outside this film or is not a public film asset');
    }
    return actual;
  };
  asset(page);
  return {root, page, asset, url(query = '') {
    const url = new URL(name.split(sep).map(encodeURIComponent).join('/'), FILM_ORIGIN + '/');
    url.search = typeof query === 'string' ? query : new URLSearchParams(query).toString();
    return url;
  }};
}

export async function launchFilmBrowser(chromium) {
  // WebRTC bypasses request routes and CSP; send TCP to a deny-only proxy and disable direct UDP.
  const sockets = new Set();
  const proxy = net.createServer(socket => {
    sockets.add(socket);
    socket.on('error', () => socket.destroy());
    socket.once('data', () => socket.end('HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n'));
    socket.on('close', () => sockets.delete(socket));
  });
  await new Promise((done, fail) => { proxy.once('error', fail); proxy.listen(0, '127.0.0.1', done); });
  const close = () => { for (const socket of sockets) socket.destroy(); proxy.close(); };
  try {
    const browser = await chromium.launch({timeout: 30000,
      args: ['--force-webrtc-ip-handling-policy=disable_non_proxied_udp'],
      proxy: {server: `http://127.0.0.1:${proxy.address().port}`, bypass: '<-loopback>'}});
    browser.once('disconnected', close);
    return browser;
  } catch (error) { close(); throw error; }
}

export async function installFilmRoutes(context, policy, onIssue = () => {}) {
  await context.addInitScript(() => {
    for (const name of ['RTCPeerConnection', 'webkitRTCPeerConnection'])
      Object.defineProperty(globalThis, name, {value: undefined, writable: false, configurable: false});
  });
  await context.route('**/*', async route => {
    const request = route.request(), url = new URL(request.url());
    if (url.origin !== FILM_ORIGIN || request.method() !== 'GET') {
      onIssue({code:'external_request', url:url.href.slice(0,500)});
      return route.abort('blockedbyclient');
    }
    try {
      const path = policy.asset(resolve(policy.root, '.' + decodeURIComponent(url.pathname)));
      if (request.isNavigationRequest() && (request.frame().parentFrame() || path !== policy.page)) throw new Error('Only the selected film may be opened');
      return await route.fulfill({status:200, contentType:MIME[extname(path).toLowerCase()], headers:{'Content-Security-Policy':CSP, 'X-Content-Type-Options':'nosniff'}, body:readFileSync(path)});
    } catch {
      onIssue({code:'local_asset_error', path:url.pathname, message:'Film asset blocked or unavailable'});
      return route.fulfill({status:403, contentType:'text/plain', body:'Film asset blocked or unavailable'});
    }
  });
  await context.routeWebSocket('**/*', socket => {
    onIssue({code:'external_websocket', url:socket.url().slice(0,500)});
    socket.close();
  });
}
