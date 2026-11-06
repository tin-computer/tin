// The film's origin when the kit runs as a package (npx): an empty server the take's pages are
// routed through, since film.js answers for the stage, its kit and the site's recording itself.
// Tin's own dashboard films use the repository's fixture (web/app-fixture.js) instead.
import http from "node:http";

export async function serveApp() {
  const server = http.createServer((request, response) => response.writeHead(404).end());
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  return {server, base: `http://127.0.0.1:${server.address().port}`};
}
