'use strict';

const net = require('net');
const http = require('http');

const TARGETS = [
  { label: '5ease-lan-mc', host: '10.50.249.97', port: 25565, proto: 'tcp' },
  { label: '5ease-lan-api', host: '10.50.249.97', port: 18900, proto: 'http', path: '/api/v1/health' },
  { label: '5ease-radmin-mc', host: '26.14.9.42', port: 25565, proto: 'tcp' },
  { label: '5ease-radmin-api', host: '26.14.9.42', port: 18900, proto: 'http', path: '/api/v1/health' },
];

function checkTcp(t, timeoutMs = 2500) {
  return new Promise((resolve) => {
    const t0 = Date.now();
    const sock = net.connect({ host: t.host, port: t.port });
    let settled = false;
    const finish = (res) => {
      if (settled) return;
      settled = true;
      sock.destroy();
      resolve({ ...t, ms: Date.now() - t0, ...res });
    };
    sock.setTimeout(timeoutMs);
    sock.on('connect', () => finish({ ok: true }));
    sock.on('timeout', () => finish({ ok: false, error: 'ETIMEDOUT' }));
    sock.on('error', (err) => finish({ ok: false, error: err.code || err.message }));
  });
}

function checkHttp(t, timeoutMs = 2500) {
  return new Promise((resolve) => {
    const t0 = Date.now();
    const req = http.get(
      { host: t.host, port: t.port, path: t.path || '/', timeout: timeoutMs },
      (res) => {
        let body = '';
        res.setEncoding('utf8');
        res.on('data', (chunk) => {
          if (body.length < 512) body += chunk;
        });
        res.on('end', () => {
          resolve({
            ...t,
            ok: res.statusCode >= 200 && res.statusCode < 500,
            statusCode: res.statusCode,
            bodyPreview: body.slice(0, 200),
            ms: Date.now() - t0,
          });
        });
      }
    );
    req.on('timeout', () => {
      req.destroy();
      resolve({ ...t, ok: false, error: 'ETIMEDOUT', ms: Date.now() - t0 });
    });
    req.on('error', (err) => {
      resolve({ ...t, ok: false, error: err.code || err.message, ms: Date.now() - t0 });
    });
  });
}

async function main() {
  const results = await Promise.all(
    TARGETS.map((t) => (t.proto === 'http' ? checkHttp(t) : checkTcp(t)))
  );
  console.log(JSON.stringify({ checked_at: new Date().toISOString(), results }, null, 2));
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
