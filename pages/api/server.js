import os from 'node:os';
import fs from 'node:fs';

// In-memory history is intentionally bounded and resets on restart.
const state = globalThis.__serverpulse ??= { busy: false, last: null, history: [], started: 0, error: null };
let previous = globalThis.__serverpulseCounters ??= { time: Date.now(), cpu: os.cpus().map(c => ({ ...c.times })) };

function cpuPercent() {
  const current = os.cpus().map(c => c.times);
  let idle = 0, total = 0;
  current.forEach((t, i) => {
    const p = previous.cpu[i] || t;
    for (const key of Object.keys(t)) total += t[key] - (p[key] || 0);
    idle += t.idle - (p.idle || 0);
  });
  previous = globalThis.__serverpulseCounters = { time: Date.now(), cpu: current.map(times => ({ ...times })) };
  return total > 0 ? Math.max(0, Math.min(100, 100 * (1 - idle / total))) : 0;
}

function disk() {
  try {
    const d = fs.statfsSync(process.cwd(), { bigint: true });
    const total = Number(d.blocks * d.bsize), free = Number(d.bavail * d.bsize);
    return { total, free, used: total - free };
  } catch { return null; }
}

function network() {
  try {
    const interfaces = fs.readdirSync('/sys/class/net').filter(n => n !== 'lo');
    let rx = 0, tx = 0;
    for (const name of interfaces) {
      rx += Number(fs.readFileSync(`/sys/class/net/${name}/statistics/rx_bytes`, 'utf8'));
      tx += Number(fs.readFileSync(`/sys/class/net/${name}/statistics/tx_bytes`, 'utf8'));
    }
    return { rx, tx };
  } catch { return null; }
}

export default async function handler(req, res) {
  res.setHeader('Cache-Control', 'no-store');
  const action = req.query.action || 'metrics';
  if (action === 'metrics' && req.method === 'GET') {
    const total = os.totalmem(), free = os.freemem();
    return res.status(200).json({
      time: new Date().toISOString(), hostname: os.hostname(), platform: os.platform(), arch: os.arch(),
      node: process.version, cpus: os.cpus().length, model: os.cpus()[0]?.model || 'Unknown',
      cpu: cpuPercent(), ram: { total, used: total - free, free }, disk: disk(), network: network(),
      uptime: os.uptime(), load: os.loadavg(), processRam: process.memoryUsage().rss,
      job: { busy: state.busy, last: state.last, error: state.error }, history: state.history
    });
  }
  if (action === 'run' && req.method === 'POST') {
    const token = process.env.SPEEDTEST_TOKEN;
    if (!token || req.headers['x-speedtest-token'] !== token) return res.status(401).json({ error: 'Set SPEEDTEST_TOKEN in hosting environment and provide it to run tests.' });
    if (state.busy) return res.status(409).json({ error: 'Speed test already running.' });
    state.busy = true; state.error = null; state.started = Date.now();
    res.status(202).json({ ok: true, message: 'Speed test started' });
    // Requires a persistent Node.js process; serverless instances may terminate this work.
    (async () => {
      try {
        const { default: speedTest } = await import('speedtest-net');
        const result = await speedTest({ acceptLicense: true, acceptGdpr: true });
        const item = {
          time: new Date().toISOString(),
          download: +(result.download.bandwidth * 8 / 1e6).toFixed(2),
          upload: +(result.upload.bandwidth * 8 / 1e6).toFixed(2),
          ping: result.ping.latency,
          server: result.server?.name || 'Unknown', location: result.server?.location || '',
          isp: result.isp || '', url: result.result?.url || ''
        };
        state.last = item; state.history.unshift(item); state.history.length = Math.min(state.history.length, 20);
      } catch (e) { state.error = e.message || 'Speed test failed'; }
      finally { state.busy = false; }
    })();
    return;
  }
  return res.status(404).json({ error: 'Unknown API action' });
}
