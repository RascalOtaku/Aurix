// AURIX rail buttons: Command Center and God's Eye View. Both open in a new tab so the chat is never disturbed.
// God's Eye is a separate container (docker-compose service `gods-eye`) published on the same host; the server
// tells us its URL because the port is configurable (GODSEYE_PORT).
function open(url) { window.open(url, '_blank', 'noopener'); }

const cmd = document.getElementById('rail-command');
if (cmd) cmd.addEventListener('click', () => open('/command'));

const globe = document.getElementById('rail-godseye');
if (globe) {
  globe.addEventListener('click', async () => {
    try {
      const r = await fetch('/api/command/links', { credentials: 'same-origin' });
      if (!r.ok) throw new Error('HTTP ' + r.status);
      open((await r.json()).godseye);
    } catch (e) {
      open(location.protocol + '//' + location.hostname + ':4173/');          // best guess if the lookup failed
    }
  });
}
