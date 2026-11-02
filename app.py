"""ServerPulse: standalone, server-side speedtest and host monitoring dashboard.

Only two source files are needed: app.py and requirements.txt.
The server automatically creates an optional SQLite history file in /tmp.

Build: python3 -m venv .venv && .venv/bin/python -m pip install -r requirements.txt
Run:   .venv/bin/python app.py

No credentials or login are required. Restrict public access using hosting firewall
or access controls if you do not want others triggering bandwidth tests.
"""

import json
import os
import platform
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import psutil
from flask import Flask, jsonify, render_template_string

APP_NAME = "ServerPulse"
BASE_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("HISTORY_DB", "/tmp/serverpulse-history.sqlite3"))
TEST_TIMEOUT = min(max(int(os.environ.get("SPEEDTEST_TIMEOUT", "180")), 30), 600)
HISTORY_LIMIT = 20
PORT = int(os.environ.get("PORT", "8000"))

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 8 * 1024

STATE_LOCK = threading.Lock()
JOB = {"running": False, "stage": "Ready to test", "phase": "idle", "error": None,
       "started_at": None, "result": None, "share_warning": None, "completed_at": None}
RATE_LOCK = threading.Lock()
LAST_COUNTERS = None


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def db_connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(DB_PATH), timeout=10)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    with db_connect() as con:
        con.execute("""CREATE TABLE IF NOT EXISTS tests (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          timestamp TEXT NOT NULL,
          download_mbps REAL NOT NULL,
          upload_mbps REAL NOT NULL,
          ping_ms REAL NOT NULL,
          server TEXT,
          location TEXT,
          isp TEXT,
          share_url TEXT
        )""")


def add_history(data):
    try:
        with db_connect() as con:
            con.execute("""INSERT INTO tests (timestamp,download_mbps,upload_mbps,ping_ms,
                server,location,isp,share_url) VALUES (?,?,?,?,?,?,?,?)""",
                (data["timestamp"], data["download_mbps"], data["upload_mbps"],
                 data["ping_ms"], data["server"], data["location"], data["isp"],
                 data.get("share_url")))
            con.execute("DELETE FROM tests WHERE id NOT IN (SELECT id FROM tests ORDER BY id DESC LIMIT 100)")
    except (OSError, sqlite3.Error) as exc:
        print(f"[ServerPulse] History could not be saved: {type(exc).__name__}: {exc}", flush=True)


def read_history():
    try:
        with db_connect() as con:
            return [dict(x) for x in con.execute(
                "SELECT timestamp, download_mbps, upload_mbps, ping_ms, server, location, isp, share_url "
                "FROM tests ORDER BY id DESC LIMIT ?", (HISTORY_LIMIT,)).fetchall()]
    except (OSError, sqlite3.Error):
        return []


def emit(event, **fields):
    print(json.dumps({"event": event, **fields}, allow_nan=False), flush=True)


def run_speedtest_worker():
    """Runs in a separate *server-side* Python process, never in the browser.

    Streaming small JSON events lets the web UI report phases. The parent can
    kill the child on timeout, even if an upstream library call blocks.
    """
    try:
        import speedtest

        emit("stage", phase="finding", stage="Locating the nearest Speedtest.net server")
        st = speedtest.Speedtest(secure=True, timeout=15)
        server = st.get_best_server()
        emit("stage", phase="download", stage="Measuring download bandwidth")
        download_bps = st.download()
        emit("stage", phase="upload", stage="Measuring upload bandwidth")
        upload_bps = st.upload(pre_allocate=False)
        result = st.results.dict()
        server = result.get("server") or server or {}
        client = result.get("client") or {}
        # The legacy speedtest-cli share endpoint can fail. This must never
        # discard a completed measurement.
        share_url, share_warning = None, None
        emit("stage", phase="share", stage="Requesting Speedtest.net result link")
        try:
            returned_url = st.results.share()
            if isinstance(returned_url, str) and returned_url.startswith("https://www.speedtest.net/result/"):
                share_url = returned_url
            else:
                share_warning = "Speedtest.net did not return a supported result URL."
        except Exception as exc:
            share_warning = "Speedtest.net sharing unavailable: " + str(exc)[:200]
        payload = {
            "timestamp": utc_now(),
            "download_mbps": round(float(download_bps) / 1_000_000, 2),
            "upload_mbps": round(float(upload_bps) / 1_000_000, 2),
            "ping_ms": round(float(result.get("ping", 0)), 2),
            "server": str(server.get("sponsor") or server.get("name") or "Unknown")[:120],
            "location": ", ".join(filter(None, [server.get("name"), server.get("country")]))[:160],
            "server_id": str(server.get("id") or ""),
            "isp": str(client.get("isp") or "Unknown")[:120],
            "ip": str(client.get("ip") or "")[:80],
            "share_url": share_url,
            "share_warning": share_warning,
            "data_received_mb": round(float(result.get("bytes_received") or 0) / 1_000_000, 1),
            "data_sent_mb": round(float(result.get("bytes_sent") or 0) / 1_000_000, 1),
        }
        emit("result", data=payload)
    except Exception as exc:
        emit("error", message=f"{type(exc).__name__}: {str(exc)[:500]}")
        sys.exit(1)


def update_job(**kwargs):
    with STATE_LOCK:
        JOB.update(kwargs)


def manage_speedtest():
    """Runs child with hard limit; only one benchmark can run at once."""
    result_received = False
    proc = None
    timer = None
    try:
        proc = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "--speedtest-worker"],
            cwd=str(BASE_DIR), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, encoding="utf-8", errors="replace",
        )
        timer = threading.Timer(TEST_TIMEOUT, proc.kill)
        timer.daemon = True
        timer.start()
        last_noise = ""
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except (TypeError, ValueError):
                last_noise = line[:180]
                continue
            if message.get("event") == "stage":
                update_job(stage=message.get("stage", "Testing"), phase=message.get("phase", "finding"))
            elif message.get("event") == "result":
                data = message.get("data") or {}
                add_history(data)
                update_job(result=data, share_warning=data.get("share_warning"),
                           stage="Test completed", phase="done", completed_at=utc_now())
                result_received = True
            elif message.get("event") == "error":
                update_job(error=message.get("message") or "Speedtest failed", stage="Test failed", phase="error")
        code = proc.wait()
        if not result_received:
            with STATE_LOCK:
                already_failed = bool(JOB.get("error"))
            if not already_failed:
                detail = ("The speedtest exceeded the configured time limit."
                          if code != 0 and timer and not timer.is_alive()
                          else f"Speedtest failed (exit code {code}). {last_noise}".strip())
                update_job(error=detail, stage="Test failed", phase="error")
    except Exception as exc:
        update_job(error=f"Could not run the speedtest: {type(exc).__name__}: {exc}",
                   stage="Test failed", phase="error")
    finally:
        if timer:
            timer.cancel()
        if proc and proc.poll() is None:
            proc.kill()
            proc.wait()
        update_job(running=False)


def cpu_model():
    try:
        with open("/proc/cpuinfo", "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.lower().startswith(("model name", "hardware")) and ":" in line:
                    return line.split(":", 1)[1].strip()[:180]
    except OSError:
        pass
    return platform.processor() or platform.machine() or "Unknown"


def cgroup_limits():
    """Read Linux container quotas where the hosting platform exposes them."""
    root = Path("/sys/fs/cgroup")

    def read(name):
        try:
            return (root / name).read_text(encoding="ascii").strip()
        except (OSError, ValueError):
            return None

    result = {"container_ram_limit_gb": None, "container_ram_used_gb": None,
              "container_ram_percent": None, "container_cpu_quota_vcpu": None}
    limit, used = read("memory.max"), read("memory.current")
    if limit is None:
        limit, used = read("memory/memory.limit_in_bytes"), read("memory/memory.usage_in_bytes")
    try:
        max_bytes, used_bytes = int(limit), int(used)
        # Some cgroup v1 hosts report a very large value meaning unlimited.
        if 0 < max_bytes < 1 << 60:
            result["container_ram_limit_gb"] = round(max_bytes / 1024 ** 3, 2)
            result["container_ram_used_gb"] = round(used_bytes / 1024 ** 3, 2)
            result["container_ram_percent"] = round(min(100, max(0, used_bytes / max_bytes * 100)), 1)
    except (TypeError, ValueError, ZeroDivisionError):
        pass
    quota = read("cpu.max")
    if quota:
        parts = quota.split()
        if len(parts) == 2 and parts[0] != "max":
            try:
                result["container_cpu_quota_vcpu"] = round(int(parts[0]) / int(parts[1]), 2)
            except (ValueError, ZeroDivisionError):
                pass
    else:
        try:
            q, period = int(read("cpu/cpu.cfs_quota_us")), int(read("cpu/cpu.cfs_period_us"))
            if q > 0 and period > 0:
                result["container_cpu_quota_vcpu"] = round(q / period, 2)
        except (TypeError, ValueError, ZeroDivisionError):
            pass
    return result


def system_details():
    mem, swap = psutil.virtual_memory(), psutil.swap_memory()
    disk = psutil.disk_usage("/")
    freq = psutil.cpu_freq()
    boot = psutil.boot_time()
    return {
        "hostname": socket.gethostname(),
        "os": platform.platform(),
        "architecture": platform.machine(),
        "python": platform.python_version(),
        "cpu_model": cpu_model(),
        "cpu_physical": psutil.cpu_count(logical=False),
        "cpu_logical": psutil.cpu_count(logical=True),
        "cpu_frequency_mhz": round(freq.current, 0) if freq else None,
        "memory_total_gb": round(mem.total / 1024 ** 3, 2),
        "swap_total_gb": round(swap.total / 1024 ** 3, 2),
        "disk_total_gb": round(disk.total / 1024 ** 3, 2),
        "disk_free_gb": round(disk.free / 1024 ** 3, 2),
        "boot_time": datetime.fromtimestamp(boot, timezone.utc).isoformat(timespec="seconds"),
        "app_uptime_seconds": max(0, int(time.time() - PROCESS_START)),
        **cgroup_limits(),
    }


def live_metrics():
    global LAST_COUNTERS
    mem, swap = psutil.virtual_memory(), psutil.swap_memory()
    disk = psutil.disk_usage("/")
    net = psutil.net_io_counters()
    io = psutil.disk_io_counters()
    now = time.monotonic()
    with RATE_LOCK:
        rates = {"network_down_mbps": 0, "network_up_mbps": 0,
                 "disk_read_mbps": 0, "disk_write_mbps": 0}
        if LAST_COUNTERS:
            elapsed = max(now - LAST_COUNTERS["time"], 0.001)
            if net and LAST_COUNTERS["net"]:
                rates["network_down_mbps"] = max(0, (net.bytes_recv - LAST_COUNTERS["net"].bytes_recv) * 8 / elapsed / 1e6)
                rates["network_up_mbps"] = max(0, (net.bytes_sent - LAST_COUNTERS["net"].bytes_sent) * 8 / elapsed / 1e6)
            if io and LAST_COUNTERS["disk"]:
                rates["disk_read_mbps"] = max(0, (io.read_bytes - LAST_COUNTERS["disk"].read_bytes) / elapsed / 1e6)
                rates["disk_write_mbps"] = max(0, (io.write_bytes - LAST_COUNTERS["disk"].write_bytes) / elapsed / 1e6)
        LAST_COUNTERS = {"time": now, "net": net, "disk": io}
    try:
        load = [round(x, 2) for x in os.getloadavg()]
    except (AttributeError, OSError):
        load = None
    return {
        "timestamp": utc_now(),
        "cpu_percent": psutil.cpu_percent(interval=None),
        "cpu_cores_percent": psutil.cpu_percent(interval=None, percpu=True)[:32],
        "ram_percent": mem.percent,
        "ram_used_gb": round(mem.used / 1024 ** 3, 2),
        "ram_available_gb": round(mem.available / 1024 ** 3, 2),
        "swap_percent": swap.percent,
        "disk_percent": disk.percent,
        "disk_used_gb": round(disk.used / 1024 ** 3, 2),
        "disk_free_gb": round(disk.free / 1024 ** 3, 2),
        "load_avg": load,
        "uptime_seconds": max(0, int(time.time() - psutil.boot_time())),
        "container_ram_percent": cgroup_limits()["container_ram_percent"],
        "network_total_sent_gb": round(net.bytes_sent / 1e9, 2) if net else 0,
        "network_total_received_gb": round(net.bytes_recv / 1e9, 2) if net else 0,
        **{key: round(value, 2) for key, value in rates.items()},
    }


DASHBOARD_HTML = r"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="theme-color" content="#0a101c"><title>ServerPulse · Server Diagnostics</title><style>
:root{color-scheme:dark;--bg:#080e19;--panel:#121c2d;--border:#27364c;--muted:#97a9c2;--text:#ecf3ff;--blue:#67a7ff;--mint:#5ee2b9;--orange:#ffbd72;--red:#ff8b9a}*{box-sizing:border-box}html{scroll-behavior:smooth}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.55 Inter,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background-image:radial-gradient(circle at 5% -10%,#1539676b,transparent 36%),radial-gradient(circle at 100% 0%,#073b4e4f,transparent 22%)}.shell{max-width:1240px;margin:0 auto;padding:0 24px 54px}header{display:flex;justify-content:space-between;align-items:center;gap:12px;padding:24px 0 25px;border-bottom:1px solid #243149}.brand{display:flex;align-items:center;gap:12px}.mark{display:grid;place-items:center;width:38px;height:38px;border-radius:11px;background:linear-gradient(135deg,#4f94ff,#54d4bb);color:#061325;font-size:23px;font-weight:800}.brand strong{font-size:19px;letter-spacing:-.5px}.brand small{display:block;color:var(--muted);font-size:11px;margin-top:-5px}.topright{display:flex;align-items:center;gap:18px}.online{font-size:12px;color:#b7e8cf}.online:before{content:"";display:inline-block;width:7px;height:7px;border-radius:50%;background:#64dca6;box-shadow:0 0 0 4px #4ec89022;margin-right:8px}.hero{padding:39px 0 27px;display:flex;align-items:end;justify-content:space-between;gap:18px}.eyebrow{color:#80b2f4;font-size:11px;font-weight:800;text-transform:uppercase;letter-spacing:2px}.hero h1{font-size:clamp(26px,4vw,39px);letter-spacing:-1.5px;line-height:1.18;margin:7px 0 9px}.hero p{color:var(--muted);margin:0;max-width:590px}.time{font-size:12px;color:#8497b1}section{margin-top:23px}.sectionhead{display:flex;align-items:center;justify-content:space-between;gap:10px;margin:0 0 14px}.sectionhead h2{margin:0;font-size:16px;letter-spacing:-.25px}.sectionhead span{color:var(--muted);font-size:12px}.grid4{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:14px}.card,.panel{background:linear-gradient(155deg,#142034,#101928);border:1px solid var(--border);border-radius:17px}.card{padding:20px 20px 16px;position:relative;overflow:hidden}.label{font-size:12px;color:#a3b4cc}.value{font-size:clamp(21px,2.6vw,31px);letter-spacing:-1px;font-weight:750;margin:10px 0 8px;font-variant-numeric:tabular-nums}.hint{color:#8b9cb5;font-size:11px}.track{height:5px;background:#30405a;border-radius:10px;overflow:hidden;margin:12px 0 3px}.fill{background:var(--blue);width:0;height:100%;transition:width .5s;border-radius:10px}.mint{background:var(--mint)}.orange{background:var(--orange)}.two{display:grid;grid-template-columns:minmax(0,1.35fr) minmax(330px,.95fr);gap:15px}.panel{padding:23px;min-width:0}.split{display:flex;align-items:center;justify-content:space-between;gap:12px}.panel h3{font-size:16px;margin:0;letter-spacing:-.25px}.sub{font-size:12px;color:var(--muted);margin:5px 0 0}.action{background:#67a7ff;color:#071322;border:0;border-radius:10px;font-weight:800;cursor:pointer;padding:13px 19px;font-size:13px;white-space:nowrap;box-shadow:0 5px 25px #2c76ed2f}.action:hover{background:#8abaff}.action:disabled{cursor:not-allowed;opacity:.5}.metrics3{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px;margin-top:26px}.metric{border:1px solid #2b3b53;border-radius:12px;padding:16px 12px;background:#0c1626}.metric .mvalue{font-size:clamp(23px,3vw,34px);font-weight:750;line-height:1.25;letter-spacing:-.9px;margin:9px 0 3px;font-variant-numeric:tabular-nums}.metric .mvalue small{font-size:13px;font-weight:600;letter-spacing:0}.metric .label{font-size:12px}.statusline{min-height:44px;padding:12px 14px;background:#0c1626;border:1px solid #26374e;color:#acc0d9;border-radius:10px;margin-top:18px;font-size:12px;display:flex;align-items:center;gap:10px}.dot{display:inline-block;width:8px;height:8px;border-radius:50%;background:#7890ad;flex-shrink:0}.dot.busy{background:#65a6ff;box-shadow:0 0 0 4px #65a6ff22;animation:pulse 1.6s infinite}.dot.done{background:#5de2af}.dot.error{background:#ff8199}@keyframes pulse{50%{opacity:.45}}.progress{display:flex;gap:5px;margin:13px 0 0}.progress>div{height:5px;border-radius:5px;background:#2c3950;flex:1;transition:background .25s}.progress>div.on{background:#67a7ff}.infoRows{margin:21px 0 0;display:grid;gap:12px}.infoRow{display:flex;justify-content:space-between;gap:15px;border-bottom:1px solid #27374c;padding-bottom:10px;color:var(--muted);font-size:12px}.infoRow:last-child{border-bottom:0}.infoRow b{font-weight:650;color:#dce9ff;text-align:right;overflow-wrap:anywhere}.link{color:#79b4ff;text-decoration:none;overflow-wrap:anywhere}.link:hover{text-decoration:underline}.warn{color:#ffcd8d;font-size:12px;margin:13px 0 0}.hidden{display:none!important}.specs{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:0 25px}.spec{padding:12px 0;border-bottom:1px solid #26364d;min-width:0}.spec span{color:var(--muted);font-size:11px;display:block}.spec strong{display:block;font-size:13px;font-weight:600;overflow-wrap:anywhere;margin-top:4px}.miniGrid{display:grid;grid-template-columns:repeat(2,1fr);gap:10px;margin-top:20px}.mini{border:1px solid #2a3b52;border-radius:11px;padding:13px;background:#0e1828}.mini b{display:block;font-size:19px;margin:7px 0 0;font-variant-numeric:tabular-nums}.mini small{font-size:11px;color:var(--muted)}.tablewrap{overflow-x:auto;border-radius:12px;border:1px solid #293a52}table{border-collapse:collapse;width:100%;text-align:left;min-width:650px}thead{background:#0b1728}th{color:#a1b3cd;font-size:11px;font-weight:600}td,th{padding:13px 16px;border-bottom:1px solid #26374c}td{color:#e1e9f8;font-size:12px}tr:last-child td{border:0}.empty{text-align:center;color:#91a3ba;padding:25px}.foot{margin:27px 0 0;color:#7f92ab;font-size:11px}.errorline{background:#482332;border:1px solid #804052;border-radius:10px;color:#ffc3cc;padding:13px;font-size:12px;margin-top:12px;overflow-wrap:anywhere}.note{font-size:11px;color:#8498b3;margin-top:15px}#netDown,#netUp{color:var(--mint)}@media(max-width:900px){.grid4{grid-template-columns:repeat(2,minmax(0,1fr))}.two{grid-template-columns:1fr}.hero{align-items:start}}@media(max-width:560px){.shell{padding:0 14px 30px}header{padding:15px 0}.hero{padding:28px 0 8px}.time{display:none}.grid4{gap:9px}.card{padding:15px}.panel{padding:17px}.metrics3{gap:7px}.metric{padding:12px 8px}.metric .label{font-size:11px}.metric .mvalue{font-size:21px}.specs{grid-template-columns:1fr}.topright{gap:9px}.online{font-size:11px}}
</style></head><body><div class="shell"><header><div class="brand"><div class="mark">◈</div><div><strong>ServerPulse</strong><small>Infrastructure diagnostics</small></div></div><div class="topright"><span class="online">Server-side monitoring</span></div></header><div class="hero"><div><div class="eyebrow">System overview</div><h1>Know your server.<br>Measure its performance.</h1><p>Real-time hosting specifications and a genuine server-initiated Speedtest.net measurement. No browser-side bandwidth tests.</p></div><span class="time" id="clock">--</span></div>
<section><div class="sectionhead"><h2>Live resource usage</h2><span>Updated every 2 seconds</span></div><div class="grid4"><div class="card"><div class="label">CPU utilization</div><div class="value" id="cpu">--%</div><div class="hint" id="cpuHint">-- logical cores</div><div class="track"><div class="fill" id="cpuBar"></div></div></div><div class="card"><div class="label">Memory utilization</div><div class="value" id="ram">--%</div><div class="hint" id="ramHint">-- GB used</div><div class="track"><div class="fill mint" id="ramBar"></div></div></div><div class="card"><div class="label">Storage utilization</div><div class="value" id="disk">--%</div><div class="hint" id="diskHint">-- GB available</div><div class="track"><div class="fill orange" id="diskBar"></div></div></div><div class="card"><div class="label">Network traffic now</div><div class="value" id="traffic">-- <span style="font-size:13px">Mbps</span></div><div class="hint">↓ <span id="netDown">--</span> / ↑ <span id="netUp">--</span> Mbps</div><div class="track"><div class="fill mint" id="netBar"></div></div></div></div></section>
<section class="two"><div class="panel"><div class="split"><div><h3>Internet speed test</h3><p class="sub">Runs on this hosting server using Python speedtest-cli</p></div><button class="action" id="runBtn" type="button">Run speed test</button></div><div class="metrics3"><div class="metric"><div class="label">↓ Download</div><div class="mvalue" style="color:var(--blue)"><span id="speedDown">--</span> <small>Mbps</small></div></div><div class="metric"><div class="label">↑ Upload</div><div class="mvalue" style="color:var(--mint)"><span id="speedUp">--</span> <small>Mbps</small></div></div><div class="metric"><div class="label">↔ Ping</div><div class="mvalue" style="color:var(--orange)"><span id="speedPing">--</span> <small>ms</small></div></div></div><div class="statusline"><span class="dot" id="statusDot"></span><span id="statusText">Ready to test server bandwidth.</span></div><div class="progress" id="steps"><div></div><div></div><div></div><div></div></div><div id="errorBox" class="errorline hidden"></div><div id="shareWarn" class="warn hidden"></div><p class="note">The test consumes real server bandwidth. Speedtest.net sharing may be unavailable because speedtest-cli uses a legacy API. Measurements remain visible either way.</p></div>
<div class="panel"><div class="sectionhead" style="margin:0"><h3>Speed test details</h3><span id="testedAt">Not tested yet</span></div><div class="infoRows"><div class="infoRow"><span>Test server</span><b id="testServer">—</b></div><div class="infoRow"><span>Server location</span><b id="testLocation">—</b></div><div class="infoRow"><span>Internet provider</span><b id="testISP">—</b></div><div class="infoRow"><span>Detected public IP</span><b id="testIP">—</b></div><div class="infoRow"><span>Data received / sent</span><b id="testData">—</b></div><div class="infoRow"><span>Speedtest.net result</span><b id="shareWrap">Unavailable until tested</b></div></div><div class="miniGrid"><div class="mini"><small>Network download traffic</small><b><span id="rx">--</span> Mbps</b></div><div class="mini"><small>Network upload traffic</small><b><span id="tx">--</span> Mbps</b></div><div class="mini"><small>Disk read rate</small><b><span id="readRate">--</span> MB/s</b></div><div class="mini"><small>Disk write rate</small><b><span id="writeRate">--</span> MB/s</b></div></div></div></section>
<section><div class="sectionhead"><h2>Hardware &amp; environment</h2><span>Reported by the server operating system</span></div><div class="panel"><div class="specs" id="specs"><div class="spec"><span>Status</span><strong>Loading hardware information...</strong></div></div><p class="note">Shared hosts and container instances can expose some host-machine metrics rather than your plan's allocated CPU/RAM limits.</p></div></section>
<section><div class="sectionhead"><h2>Recent speed tests</h2><span>Latest 20 results</span></div><div class="panel" style="padding:0;overflow:hidden"><div class="tablewrap" style="border:0;border-radius:16px"><table><thead><tr><th>Time</th><th>Download</th><th>Upload</th><th>Ping</th><th>Test server</th><th>Result</th></tr></thead><tbody id="history"><tr><td colspan="6" class="empty">No completed tests yet</td></tr></tbody></table></div></div></section><div class="foot">ServerPulse · Server-executed benchmarks · Python / Flask / psutil / speedtest-cli</div></div>
<script>
const $=id=>document.getElementById(id);const fmt=(v,d=1)=>Number(v??0).toFixed(d);const set=(id,v)=>{$(id).textContent=v};const pct=(id,n)=>$(id).style.width=Math.min(100,Math.max(0,n||0))+'%';const datefmt=s=>{if(!s)return '—';const d=new Date(s);return isNaN(d)?s:d.toLocaleString()};
async function api(path,opt){const r=await fetch(path,{cache:'no-store',credentials:'same-origin',...opt});if(r.status===401){location.href='/login';throw Error('Authentication expired')}const d=await r.json();if(!r.ok)throw Error(d.error||'Request failed');return d}
const specNames=[['hostname','Hostname'],['os','Operating system'],['architecture','Architecture'],['cpu_model','CPU model'],['cpu_physical','Physical CPU cores'],['cpu_logical','Logical CPU threads'],['cpu_frequency_mhz','CPU frequency (MHz)'],['container_cpu_quota_vcpu','Allocated CPU quota (vCPU)'],['container_ram_limit_gb','Container RAM limit (GB)'],['container_ram_used_gb','Container RAM used (GB)'],['memory_total_gb','Total system RAM (GB)'],['swap_total_gb','Swap size (GB)'],['disk_total_gb','Disk capacity (GB)'],['disk_free_gb','Free disk (GB)'],['python','Python version'],['boot_time','Host boot time']];
async function refreshSpecs(){try{const d=await api('/api/specs');$('specs').replaceChildren();for(const [key,name] of specNames){const el=document.createElement('div');el.className='spec';const title=document.createElement('span');title.textContent=name;const value=document.createElement('strong');value.textContent=key==='boot_time'?datefmt(d[key]):String(d[key]??'Unavailable');el.append(title,value);$('specs').append(el)}set('cpuHint',(d.cpu_logical||'?')+' logical cores')}catch(e){console.error(e)}}
async function refreshMetrics(){try{const m=await api('/api/metrics');set('clock',datefmt(m.timestamp));set('cpu',fmt(m.cpu_percent)+'%');pct('cpuBar',m.cpu_percent);const ramPct=m.container_ram_percent??m.ram_percent;set('ram',fmt(ramPct)+'%');pct('ramBar',ramPct);set('ramHint',m.container_ram_percent!==null?'Container memory usage':fmt(m.ram_used_gb,2)+' GB used · '+fmt(m.ram_available_gb,2)+' GB available');set('disk',fmt(m.disk_percent)+'%');pct('diskBar',m.disk_percent);set('diskHint',fmt(m.disk_free_gb,2)+' GB available');const total=(m.network_down_mbps||0)+(m.network_up_mbps||0);$('traffic').replaceChildren(document.createTextNode(fmt(total,2)+' '));const u=document.createElement('span');u.style.fontSize='13px';u.textContent='Mbps';$('traffic').append(u);set('netDown',fmt(m.network_down_mbps,2));set('netUp',fmt(m.network_up_mbps,2));pct('netBar',total);set('rx',fmt(m.network_down_mbps,2));set('tx',fmt(m.network_up_mbps,2));set('readRate',fmt(m.disk_read_mbps,2));set('writeRate',fmt(m.disk_write_mbps,2))}catch(e){console.error(e);set('clock','Connection lost')}}
let lastFinish=null;function showResult(r){if(!r)return;set('speedDown',fmt(r.download_mbps,2));set('speedUp',fmt(r.upload_mbps,2));set('speedPing',fmt(r.ping_ms,2));set('testServer',r.server||'Unknown');set('testLocation',r.location||'Unknown');set('testISP',r.isp||'Unknown');set('testIP',r.ip||'Unknown');set('testData',fmt(r.data_received_mb,1)+' / '+fmt(r.data_sent_mb,1)+' MB');set('testedAt',datefmt(r.timestamp));const wrap=$('shareWrap');wrap.replaceChildren();if(r.share_url&&r.share_url.startsWith('https://www.speedtest.net/result/')){const a=document.createElement('a');a.href=r.share_url;a.rel='noopener noreferrer';a.target='_blank';a.className='link';a.textContent='View Speedtest.net result ↗';wrap.append(a)}else{wrap.textContent='Not available'}if(r.share_warning){$('shareWarn').textContent=r.share_warning;$('shareWarn').classList.remove('hidden')}else{$('shareWarn').classList.add('hidden')}}
const phaseIndex={idle:0,finding:1,download:2,upload:3,share:4,done:4,error:0};let priorRunning=false;async function refreshJob(){try{const s=await api('/api/job');$('runBtn').disabled=s.running;set('runBtn',s.running?'Testing...':'Run speed test');set('statusText',s.stage||'Ready to test');$('statusDot').className='dot '+(s.running?'busy':s.phase==='done'?'done':s.phase==='error'?'error':'');Array.from($('steps').children).forEach((e,i)=>e.classList.toggle('on',i<(phaseIndex[s.phase]||0)));if(s.error){$('errorBox').textContent=s.error;$('errorBox').classList.remove('hidden')}else{$('errorBox').classList.add('hidden')}if(s.result)showResult(s.result);if(priorRunning&&!s.running)refreshHistory();priorRunning=s.running;if(s.completed_at&&lastFinish!==s.completed_at){lastFinish=s.completed_at;refreshHistory()}}catch(e){set('statusText','Unable to read test status')}}
async function refreshHistory(){try{const rows=await api('/api/history');$('history').replaceChildren();if(!rows.length){const tr=document.createElement('tr');const td=document.createElement('td');td.colSpan=6;td.className='empty';td.textContent='No completed tests yet';tr.append(td);$('history').append(tr);return}for(const r of rows){const tr=document.createElement('tr');const vals=[datefmt(r.timestamp),fmt(r.download_mbps,2)+' Mbps',fmt(r.upload_mbps,2)+' Mbps',fmt(r.ping_ms,2)+' ms',r.server||'—'];for(const v of vals){const td=document.createElement('td');td.textContent=v;tr.append(td)}const td=document.createElement('td');if(r.share_url){const a=document.createElement('a');a.href=r.share_url;a.target='_blank';a.rel='noopener noreferrer';a.className='link';a.textContent='View';td.append(a)}else{td.textContent='—'}tr.append(td);$('history').append(tr)}}catch(e){console.error(e)}}
$('runBtn').addEventListener('click',async()=>{if(!confirm('Run a bandwidth test from your hosting server? This will consume real download and upload data.'))return;$('runBtn').disabled=true;try{await api('/api/run',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});await refreshJob()}catch(e){alert(e.message);$('runBtn').disabled=false}});
refreshSpecs();refreshMetrics();refreshJob();refreshHistory();setInterval(refreshMetrics,2000);setInterval(refreshJob,1500);setInterval(refreshHistory,30000);
</script></body></html>"""


@app.after_request
def secure_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Cache-Control"] = "no-store"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; img-src 'self' data: https:; "
        "connect-src 'self'; object-src 'none'; frame-ancestors 'none'"
    )
    return response


@app.route("/healthz")
def healthz():
    return jsonify(status="ok", service="ServerPulse")


@app.route("/")
def dashboard():
    return render_template_string(DASHBOARD_HTML)


@app.route("/api/metrics")
def metrics_endpoint():
    return jsonify(live_metrics())


@app.route("/api/specs")
def specs_endpoint():
    return jsonify(system_details())


@app.route("/api/job")
def job_endpoint():
    with STATE_LOCK:
        return jsonify(dict(JOB))


@app.route("/api/history")
def history_endpoint():
    return jsonify(read_history())


@app.route("/api/run", methods=["POST"])
def run_endpoint():
    with STATE_LOCK:
        if JOB["running"]:
            return jsonify(error="A speed test is already in progress"), 409
        JOB.update(running=True, phase="finding", stage="Starting server-side Speedtest.net benchmark",
                   started_at=utc_now(), completed_at=None, error=None, result=None, share_warning=None)
    threading.Thread(target=manage_speedtest, daemon=True, name="speedtest-runner").start()
    return jsonify(ok=True, message="Server-side speed test started"), 202


PROCESS_START = time.time()
try:
    init_db()
except Exception as exc:
    print(f"[ServerPulse] Unable to initialize history: {exc}", flush=True)
psutil.cpu_percent(interval=None)


if __name__ == "__main__":
    if "--speedtest-worker" in sys.argv:
        run_speedtest_worker()
    else:
        from waitress import serve
        print("\n" + "=" * 63, flush=True)
        print(f"  {APP_NAME} — server-side speedtest & resource diagnostics", flush=True)
        print("  Dashboard available without login", flush=True)
        print(f"  Listening on 0.0.0.0:{PORT}", flush=True)
        print("=" * 63 + "\n", flush=True)
        serve(app, host="0.0.0.0", port=PORT, threads=6)
