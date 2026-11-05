import { useEffect, useRef, useState } from 'react';

const gb = n => n == null ? '—' : `${(n / 1073741824).toFixed(2)} GB`;
const fmt = (n, d = 1) => Number(n || 0).toFixed(d);
const date = s => s ? new Date(s).toLocaleString() : '—';

export default function Home() {
  const [data, setData] = useState(null);
  const [error, setError] = useState('');
  const [token, setToken] = useState('');
  const [starting, setStarting] = useState(false);
  const [rates, setRates] = useState({ rx: 0, tx: 0 });
  const previous = useRef(null);

  async function refresh() {
    try {
      const r = await fetch('/api/server', { cache: 'no-store' });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const d = await r.json();
      if (previous.current && d.network && previous.current.network) {
        const seconds = (Date.parse(d.time) - Date.parse(previous.current.time)) / 1000;
        if (seconds > 0) setRates({
          rx: Math.max(0, (d.network.rx - previous.current.network.rx) * 8 / seconds / 1e6),
          tx: Math.max(0, (d.network.tx - previous.current.network.tx) * 8 / seconds / 1e6)
        });
      }
      previous.current = d;
      setData(d); setError('');
    } catch (e) { setError(e.message); }
  }
  useEffect(() => { refresh(); const id = setInterval(refresh, 2500); return () => clearInterval(id); }, []);

  async function run() {
    if (!token.trim()) return setError('Enter the SPEEDTEST_TOKEN configured on the server.');
    setStarting(true); setError('');
    try {
      const r = await fetch('/api/server?action=run', { method: 'POST', headers: { 'x-speedtest-token': token } });
      const d = await r.json();
      if (!r.ok) throw new Error(d.error || 'Request failed');
      await refresh();
    } catch (e) { setError(e.message); }
    finally { setStarting(false); }
  }

  const cells = data ? [
    ['CPU usage', `${fmt(data.cpu)}%`, `${data.cpus} logical CPUs`],
    ['RAM used', gb(data.ram.used), `Host total: ${gb(data.ram.total)}`],
    ['Process RAM', gb(data.processRam), 'Current Node.js process'],
    ['Disk available', gb(data.disk?.free), `Filesystem total: ${gb(data.disk?.total)}`],
    ['Network download', `${fmt(rates.rx, 2)} Mbps`, 'Interface traffic (not speed test)'],
    ['Network upload', `${fmt(rates.tx, 2)} Mbps`, 'Interface traffic (not speed test)'],
    ['Server uptime', `${fmt(data.uptime / 3600, 1)} h`, data.hostname],
    ['Environment', `${data.platform}/${data.arch}`, data.node]
  ] : [];
  return <main>
    <header><div><span className="eyebrow">SERVERPULSE / NEXT.JS</span><h1>Server diagnostics</h1><p>Live host metrics and server-side bandwidth testing</p></div><span className="status">{error ? 'Connection issue' : data ? 'Live' : 'Connecting'}</span></header>
    {error && <div className="alert">{error}</div>}
    <section className="grid">{cells.map(([name, value, detail]) => <article key={name}><small>{name}</small><strong>{value}</strong><span>{detail}</span></article>)}</section>
    <section className="panel"><div className="panelhead"><div><h2>Server-side speed test</h2><p>Tests bandwidth from the hosting server, not your device.</p></div><span>{data?.job?.busy ? 'Running...' : 'Ready'}</span></div>
      <div className="results"><div><small>Download</small><b>{data?.job?.last ? fmt(data.job.last.download, 2) : '—'} <em>Mbps</em></b></div><div><small>Upload</small><b>{data?.job?.last ? fmt(data.job.last.upload, 2) : '—'} <em>Mbps</em></b></div><div><small>Ping</small><b>{data?.job?.last ? fmt(data.job.last.ping, 1) : '—'} <em>ms</em></b></div></div>
      <div className="controls"><input type="password" placeholder="SPEEDTEST_TOKEN" value={token} onChange={e => setToken(e.target.value)} aria-label="Speed test token"/><button onClick={run} disabled={starting || data?.job?.busy}>{data?.job?.busy ? 'Testing...' : starting ? 'Starting...' : 'Run speed test'}</button></div>
      <p className="note">Set SPEEDTEST_TOKEN in your hosting environment. Running a test consumes real network bandwidth. The token is kept in this page's memory only.</p>
      {data?.job?.error && <div className="alert">{data.job.error}</div>}
      {data?.job?.last && <p className="note">Last test: {date(data.job.last.time)} · {data.job.last.server} · {data.job.last.isp}</p>}
    </section>
    <section className="panel"><h2>Recent speed tests</h2><div className="tablewrap"><table><thead><tr><th>Time</th><th>Download</th><th>Upload</th><th>Ping</th><th>Server</th></tr></thead><tbody>{data?.history?.length ? data.history.map((r, i) => <tr key={i}><td>{date(r.time)}</td><td>{r.download} Mbps</td><td>{r.upload} Mbps</td><td>{r.ping} ms</td><td>{r.server}</td></tr>) : <tr><td colSpan="5">No completed tests yet</td></tr>}</tbody></table></div></section>
    <footer>Host CPU, memory, and disk metrics can differ from container resource quotas. Speed test history resets when the process restarts.</footer>
    <style jsx global>{`*{box-sizing:border-box}body{margin:0;background:#0a1020;color:#e8eefb;font-family:Inter,system-ui,-apple-system,sans-serif}main{max-width:1120px;margin:auto;padding:34px 22px}header,.panelhead{display:flex;justify-content:space-between;align-items:center;gap:18px}h1{font-size:36px;margin:10px 0}h2{font-size:20px;margin:0 0 7px}p{color:#9daac2;margin:6px 0}.eyebrow{font-size:11px;letter-spacing:2px;color:#76a7ff}.status{border:1px solid #255944;background:#14392b;color:#9cf0be;border-radius:50px;padding:8px 16px;font-size:12px}.grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:14px;margin:30px 0}.grid article,.panel{background:#111c32;border:1px solid #263451;border-radius:16px;padding:21px}.grid article{min-height:148px;display:flex;flex-direction:column;gap:15px}.grid small,.results small{font-size:13px;color:#9daac2}.grid strong{font-size:25px;overflow-wrap:anywhere}.grid article span{font-size:12px;color:#7d8eaa;overflow-wrap:anywhere}.panel{margin-bottom:20px}.panelhead>span{color:#9ec1ff}.results{display:grid;grid-template-columns:repeat(3,1fr);gap:16px;margin:24px 0}.results>div{border-radius:12px;background:#0b1427;padding:20px;display:flex;flex-direction:column;gap:10px}.results b{font-size:28px}.results em{font-size:13px;color:#9daac2;font-style:normal}.controls{display:flex;gap:10px}.controls input{background:#0a1426;border:1px solid #364865;border-radius:9px;padding:13px;color:#fff;flex:1;min-width:0}.controls button{border:0;background:#4e82f7;color:white;padding:12px 22px;border-radius:9px;font-weight:700;cursor:pointer}.controls button:disabled{opacity:.5;cursor:wait}.note{font-size:12px;margin-top:13px}.alert{background:#48252b;color:#ffbec5;padding:13px;border-radius:9px;margin:15px 0}.tablewrap{overflow:auto}table{width:100%;border-collapse:collapse;text-align:left;margin-top:18px}th,td{padding:14px 12px;border-bottom:1px solid #28354c;font-size:13px}th{color:#9daac2}footer{font-size:12px;color:#75849d;margin:30px 0}@media(max-width:850px){.grid{grid-template-columns:repeat(2,1fr)}}@media(max-width:520px){header{align-items:flex-start}.grid{gap:10px}.grid article{padding:15px;min-height:130px}.grid strong{font-size:20px}.results{gap:7px}.results>div{padding:11px}.results b{font-size:19px}.controls{flex-direction:column}h1{font-size:28px}}`}</style>
  </main>;
}
