#!/usr/bin/env python3
"""
netdiag.py - Home internet slowness diagnostic.

Standard library only. No pip install, no root. Python 3.8+.
Works on macOS, Linux and Windows.

    python3 netdiag.py            # full run, ~90 seconds
    python3 netdiag.py --quick    # ~30 seconds
    python3 netdiag.py --json     # machine-readable output

It measures the things that actually make a connection *feel* slow,
which is often not raw bandwidth:

  1. Who your ISP is and which edge server you land on
  2. DNS resolver speed (your ISP's vs Cloudflare's vs Google's)
  3. Idle latency, jitter and connection failure rate
  4. Latency to your own router  -> separates Wi-Fi problems from ISP problems
  5. Download and upload throughput
  6. Bufferbloat: latency *while the link is busy* -> the #1 cause of
     "fast speed test but everything still feels laggy"
"""

import argparse
import concurrent.futures as cf
import json
import os
import platform
import random
import re
import socket
import ssl
import statistics
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

CF = "speed.cloudflare.com"
UA = {"User-Agent": "netdiag/1.0"}
TIMEOUT = 10

# Domains used for DNS timing. Mixed TLDs and authorities so one slow
# nameserver does not dominate the median.
DNS_PROBES = [
    "wikipedia.org", "github.com", "nytimes.com", "reddit.com",
    "cloudflare.com", "bbc.co.uk", "stackoverflow.com", "apple.com",
]

LATENCY_TARGETS = [
    ("cloudflare", "1.1.1.1", 443),
    ("google", "8.8.8.8", 443),
    ("cf-speed", CF, 443),
]


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

def pct(values, p):
    """Percentile using nearest-rank; safe on tiny samples."""
    if not values:
        return None
    s = sorted(values)
    k = max(0, min(len(s) - 1, int(round((p / 100.0) * len(s) + 0.5)) - 1))
    return s[k]


def fmt_ms(v):
    return "n/a" if v is None else f"{v:.1f} ms"


def fmt_mbps(v):
    return "n/a" if v is None else f"{v:.1f} Mbps"


def http_get(url, timeout=TIMEOUT):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


# --------------------------------------------------------------------------
# 1. connection identity
# --------------------------------------------------------------------------

def probe_environment():
    """A proxy or VPN in the path skews every number below - and is often
    the actual cause of the slowness being investigated."""
    keys = [k for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")
            if os.environ.get(k) or os.environ.get(k.lower())]
    return {"proxy_env": keys, "has_ping": bool(_which("ping"))}


def _which(name):
    exts = [""] if platform.system() != "Windows" else ["", ".exe"]
    for d in os.environ.get("PATH", "").split(os.pathsep):
        for e in exts:
            if d and os.path.isfile(os.path.join(d, name + e)):
                return os.path.join(d, name + e)
    return None


def probe_identity():
    """Cloudflare's /meta tells us the ISP, ASN and which edge PoP we hit."""
    out = {}
    try:
        raw = http_get(f"https://{CF}/meta")
        meta = json.loads(raw.decode("utf-8", "replace"))
        out = {
            "ip": meta.get("clientIp"),
            "isp": meta.get("asOrganization"),
            "asn": meta.get("asn"),
            "city": meta.get("city"),
            "country": meta.get("country"),
            "edge": meta.get("colo"),
        }
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
    return out


# --------------------------------------------------------------------------
# 2. DNS
# --------------------------------------------------------------------------

def system_resolvers():
    """Best-effort discovery of the resolvers the OS is configured to use."""
    found = []
    try:
        if platform.system() == "Windows":
            txt = subprocess.run(["ipconfig", "/all"], capture_output=True,
                                 text=True, timeout=15).stdout
            block = re.findall(r"DNS Servers[^:]*:\s*(.*(?:\n\s{6,}.*)*)", txt)
            for b in block:
                found += re.findall(r"\d+\.\d+\.\d+\.\d+", b)
        else:
            with open("/etc/resolv.conf") as fh:
                for line in fh:
                    m = re.match(r"\s*nameserver\s+(\S+)", line)
                    if m:
                        found.append(m.group(1))
    except Exception:
        pass
    # de-dup, keep order
    seen, uniq = set(), []
    for f in found:
        if f not in seen:
            seen.add(f)
            uniq.append(f)
    return uniq


def build_dns_query(name):
    """Minimal DNS query packet for an A record, recursion desired."""
    tid = random.randint(0, 0xFFFF)
    header = struct.pack(">HHHHHH", tid, 0x0100, 1, 0, 0, 0)
    q = b"".join(bytes([len(p)]) + p.encode("ascii")
                 for p in name.split(".")) + b"\x00"
    return tid, header + q + struct.pack(">HH", 1, 1)


def dns_query_once(resolver, name, timeout=3.0):
    """Time a single UDP DNS query. Returns ms, or None on failure."""
    tid, pkt = build_dns_query(name)
    fam = socket.AF_INET6 if ":" in resolver else socket.AF_INET
    s = socket.socket(fam, socket.SOCK_DGRAM)
    s.settimeout(timeout)
    try:
        t0 = time.perf_counter()
        s.sendto(pkt, (resolver, 53))
        while True:
            data, _ = s.recvfrom(2048)
            if len(data) >= 2 and struct.unpack(">H", data[:2])[0] == tid:
                return (time.perf_counter() - t0) * 1000
    except Exception:
        return None
    finally:
        s.close()


def probe_dns(quick):
    names = DNS_PROBES[:4] if quick else DNS_PROBES
    candidates = []
    for r in system_resolvers()[:2]:
        candidates.append((f"your resolver ({r})", r, True))
    candidates += [
        ("Cloudflare 1.1.1.1", "1.1.1.1", False),
        ("Google 8.8.8.8", "8.8.8.8", False),
    ]

    results = []
    for label, addr, is_system in candidates:
        samples = [t for n in names
                   if (t := dns_query_once(addr, n)) is not None]
        results.append({
            "label": label,
            "resolver": addr,
            "is_system": is_system,
            "median_ms": statistics.median(samples) if samples else None,
            "worst_ms": max(samples) if samples else None,
            "answered": len(samples),
            "asked": len(names),
        })

    # getaddrinfo goes through the OS stack, caches and any VPN/proxy shim.
    t0 = time.perf_counter()
    try:
        socket.getaddrinfo("wikipedia.org", 443, proto=socket.IPPROTO_TCP)
        os_ms = (time.perf_counter() - t0) * 1000
    except Exception:
        os_ms = None

    return {"resolvers": results, "os_stack_ms": os_ms}


# --------------------------------------------------------------------------
# 3. latency / jitter / loss
# --------------------------------------------------------------------------

def tcp_connect_ms(host, port, timeout=3.0):
    """One TCP handshake, timed. Reliable where ICMP is filtered."""
    try:
        info = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)[0]
    except Exception:
        return None
    fam, typ, proto, _, sa = info
    s = socket.socket(fam, typ, proto)
    s.settimeout(timeout)
    try:
        t0 = time.perf_counter()
        s.connect(sa)
        return (time.perf_counter() - t0) * 1000
    except Exception:
        return None
    finally:
        s.close()


def probe_latency(quick):
    n = 8 if quick else 20
    out = []
    for label, host, port in LATENCY_TARGETS:
        samples, fails = [], 0
        for _ in range(n):
            t = tcp_connect_ms(host, port)
            if t is None:
                fails += 1
            else:
                samples.append(t)
            time.sleep(0.05)
        jitter = None
        if len(samples) > 1:
            # mean absolute consecutive difference == RFC-style jitter
            jitter = statistics.mean(
                abs(b - a) for a, b in zip(samples, samples[1:]))
        out.append({
            "target": label,
            "host": host,
            "attempts": n,
            "failed": fails,
            "loss_pct": 100.0 * fails / n,
            "min_ms": min(samples) if samples else None,
            "median_ms": statistics.median(samples) if samples else None,
            "p95_ms": pct(samples, 95),
            "jitter_ms": jitter,
        })
    return out


# --------------------------------------------------------------------------
# 4. the first hop (your router)
# --------------------------------------------------------------------------

def default_gateway():
    sysname = platform.system()
    try:
        if sysname == "Darwin":
            txt = subprocess.run(["route", "-n", "get", "default"],
                                 capture_output=True, text=True,
                                 timeout=10).stdout
            m = re.search(r"gateway:\s*(\S+)", txt)
            return m.group(1) if m else None
        if sysname == "Linux":
            # /proc first: works on minimal systems with no iproute2.
            try:
                with open("/proc/net/route") as fh:
                    next(fh)
                    for line in fh:
                        f = line.split()
                        if len(f) > 2 and f[1] == "00000000":
                            raw = struct.pack("<L", int(f[2], 16))
                            return socket.inet_ntoa(raw)
            except Exception:
                pass
            txt = subprocess.run(["ip", "route", "show", "default"],
                                 capture_output=True, text=True,
                                 timeout=10).stdout
            m = re.search(r"default via (\S+)", txt)
            return m.group(1) if m else None
        if sysname == "Windows":
            txt = subprocess.run(["ipconfig"], capture_output=True,
                                 text=True, timeout=15).stdout
            m = re.search(r"Default Gateway[^:]*:\s*(\d+\.\d+\.\d+\.\d+)", txt)
            return m.group(1) if m else None
    except Exception:
        pass
    return None


def ping_host(host, count):
    """Shell out to the system ping; parse the individual RTTs."""
    sysname = platform.system()
    if sysname == "Windows":
        cmd = ["ping", "-n", str(count), host]
    else:
        cmd = ["ping", "-c", str(count), "-i", "0.3", host]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=count * 2 + 15)
    except Exception:
        return None
    rtts = [float(x) for x in re.findall(r"time[=<]\s*([\d.]+)\s*ms",
                                         p.stdout)]
    if not rtts:
        return None
    jitter = (statistics.mean(abs(b - a) for a, b in zip(rtts, rtts[1:]))
              if len(rtts) > 1 else None)
    return {
        "host": host,
        "sent": count,
        "received": len(rtts),
        "loss_pct": 100.0 * (count - len(rtts)) / count,
        "min_ms": min(rtts),
        "median_ms": statistics.median(rtts),
        "max_ms": max(rtts),
        "jitter_ms": jitter,
    }


def probe_first_hop(quick):
    gw = default_gateway()
    if not gw:
        return {"gateway": None, "note": "could not determine default gateway"}
    if not _which("ping"):
        return {"gateway": gw, "ping": None, "note": "no ping binary found"}
    r = ping_host(gw, 10 if quick else 25)
    return {"gateway": gw, "ping": r}


# --------------------------------------------------------------------------
# 5. throughput
# --------------------------------------------------------------------------

class Meter:
    """Thread-safe byte counter shared across parallel transfer workers."""

    def __init__(self):
        self.n = 0
        self.lock = threading.Lock()

    def add(self, k):
        with self.lock:
            self.n += k

    def get(self):
        with self.lock:
            return self.n


def _download_worker(meter, stop, size, errors):
    url = f"https://{CF}/__down?bytes={size}"
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            while not stop.is_set():
                chunk = r.read(65536)
                if not chunk:
                    break
                meter.add(len(chunk))
    except Exception as e:
        errors.append(f"{type(e).__name__}: {e}")


def _upload_worker(meter, stop, size, errors):
    url = f"https://{CF}/__up"
    payload = os.urandom(1 << 16)

    class Body:
        """File-like body that counts bytes as urllib pulls them."""

        def __init__(self, total):
            self.left = total

        def read(self, n=-1):
            if stop.is_set() or self.left <= 0:
                return b""
            k = min(len(payload), self.left if n in (-1, None) else min(n, self.left))
            self.left -= k
            meter.add(k)
            return payload[:k]

    try:
        req = urllib.request.Request(url, data=Body(size), headers={
            **UA,
            "Content-Type": "application/octet-stream",
            "Content-Length": str(size),
        }, method="POST")
        urllib.request.urlopen(req, timeout=TIMEOUT + 20).read()
    except Exception as e:
        errors.append(f"{type(e).__name__}: {e}")


def run_transfer(kind, streams, seconds, size):
    """Run N parallel streams for `seconds`. Reports Mbps *and* why not."""
    meter, stop, errors = Meter(), threading.Event(), []
    worker = _download_worker if kind == "down" else _upload_worker
    threads = [threading.Thread(target=worker,
                                args=(meter, stop, size, errors),
                                daemon=True) for _ in range(streams)]

    # Ignore the first second: TCP slow-start has not ramped up yet.
    for t in threads:
        t.start()
    time.sleep(1.0)
    start_bytes, t0 = meter.get(), time.perf_counter()
    time.sleep(seconds)
    elapsed = time.perf_counter() - t0
    moved = meter.get() - start_bytes
    stop.set()
    for t in threads:
        t.join(timeout=3)

    uniq = sorted(set(errors))[:2]
    mbps = (moved * 8) / elapsed / 1e6 if elapsed > 0 and moved > 0 else None
    return {"mbps": mbps, "bytes": moved, "errors": uniq}


def probe_throughput(quick):
    secs = 5 if quick else 8
    down = run_transfer("down", 4, secs, 200 << 20)
    time.sleep(1)
    up = run_transfer("up", 3, secs, 60 << 20)
    return {
        "download_mbps": down["mbps"], "upload_mbps": up["mbps"],
        "download_errors": down["errors"], "upload_errors": up["errors"],
        "seconds_each": secs,
    }


# --------------------------------------------------------------------------
# 6. bufferbloat - latency under load
# --------------------------------------------------------------------------

def probe_bufferbloat(idle_median, quick):
    """
    Saturate the link, and probe latency at the same time.

    A connection can hit its full advertised speed and still feel awful:
    if the modem/router queues packets deep, every interactive packet waits
    behind a backlog. That shows up here and nowhere else.
    """
    if idle_median is None:
        return {"error": "no idle latency baseline"}

    secs = 6 if quick else 10
    meter, stop, errors = Meter(), threading.Event(), []
    threads = [threading.Thread(target=_download_worker,
                                args=(meter, stop, 200 << 20, errors),
                                daemon=True)
               for _ in range(4)]
    threads += [threading.Thread(target=_upload_worker,
                                 args=(meter, stop, 60 << 20, errors),
                                 daemon=True)
                for _ in range(2)]
    for t in threads:
        t.start()

    time.sleep(1.5)  # let the queues actually fill
    loaded = []
    load_start = meter.get()
    t_load = time.perf_counter()
    deadline = time.time() + secs
    while time.time() < deadline:
        t = tcp_connect_ms("1.1.1.1", 443)
        if t is not None:
            loaded.append(t)
        time.sleep(0.1)

    load_elapsed = time.perf_counter() - t_load
    moved = meter.get() - load_start
    stop.set()
    for t in threads:
        t.join(timeout=3)

    if not loaded:
        return {"error": "no successful probes under load"}

    # A latency-under-load number is only meaningful if the link was really
    # under load. Without this guard a blocked transfer yields a bogus "A".
    load_mbps = (moved * 8) / load_elapsed / 1e6 if load_elapsed > 0 else 0.0
    if load_mbps < 1.0:
        return {
            "error": "link was never saturated, so this test is invalid",
            "load_mbps": load_mbps,
            "transfer_errors": sorted(set(errors))[:2],
        }

    med = statistics.median(loaded)
    return {
        "idle_median_ms": idle_median,
        "loaded_median_ms": med,
        "loaded_p95_ms": pct(loaded, 95),
        "increase_ms": med - idle_median,
        "load_mbps": load_mbps,
        "samples": len(loaded),
    }


# --------------------------------------------------------------------------
# interpretation
# --------------------------------------------------------------------------

def grade_bufferbloat(delta):
    if delta is None:
        return "?", "could not measure"
    if delta < 30:
        return "A", "well managed"
    if delta < 60:
        return "B", "mild"
    if delta < 100:
        return "C", "noticeable"
    if delta < 200:
        return "D", "bad"
    return "F", "severe"


def interpret(rep):
    """Turn raw numbers into ranked, actionable findings."""
    findings = []

    def add(sev, title, detail):
        findings.append({"severity": sev, "title": title, "detail": detail})

    # --- latency & loss to the internet
    lat = [t for t in rep["latency"] if t["median_ms"] is not None]
    best = min(lat, key=lambda t: t["median_ms"]) if lat else None
    idle = best["median_ms"] if best else None

    if idle is not None:
        if idle > 150:
            add("high", "Very high baseline latency",
                f"Best median RTT is {fmt_ms(idle)}. Typical wired broadband "
                f"is 10-40 ms. This alone makes every page load feel slow "
                f"regardless of bandwidth. Consistent with satellite, a "
                f"congested ISP link, or a VPN routing you the long way.")
        elif idle > 80:
            add("medium", "Elevated baseline latency",
                f"Best median RTT is {fmt_ms(idle)}. Higher than expected for "
                f"fixed broadband; browsing will feel sluggish before "
                f"bandwidth is ever the limit.")

    worst_loss = max((t["loss_pct"] for t in rep["latency"]), default=0)
    if worst_loss >= 10:
        add("high", "Packet loss / failed connections",
            f"{worst_loss:.0f}% of connection attempts failed. Loss above a "
            f"few percent causes TCP to back off hard — this destroys "
            f"throughput far more than it looks like it should.")
    elif worst_loss > 2:
        add("medium", "Intermittent connection failures",
            f"{worst_loss:.0f}% of attempts failed. Worth re-running to see "
            f"if it is persistent.")

    worst_jit = max((t["jitter_ms"] for t in rep["latency"]
                     if t["jitter_ms"] is not None), default=None)
    if worst_jit is not None and worst_jit > 30:
        add("medium", "High jitter",
            f"Latency varies by {fmt_ms(worst_jit)} between samples. Unstable "
            f"timing like this is the signature of Wi-Fi interference or a "
            f"marginal signal, and it wrecks calls and video.")

    # --- first hop: is it your own network or the ISP?
    fh = rep.get("first_hop", {})
    p = fh.get("ping") if isinstance(fh, dict) else None
    if p:
        if p["loss_pct"] > 1:
            add("high", "Packet loss to your own router",
                f"{p['loss_pct']:.0f}% loss reaching {p['host']}. The problem "
                f"is inside your home, before your ISP is involved. Almost "
                f"always Wi-Fi: distance, walls, or channel congestion.")
        elif p["median_ms"] > 25:
            add("high", "Slow hop to your own router",
                f"{fmt_ms(p['median_ms'])} just to reach {p['host']}. A wired "
                f"link is under 2 ms and healthy Wi-Fi under 10 ms. Your "
                f"local link is the bottleneck, not your ISP — no plan "
                f"upgrade will fix this.")
        elif p["jitter_ms"] and p["jitter_ms"] > 15:
            add("medium", "Unstable link to your router",
                f"Jitter of {fmt_ms(p['jitter_ms'])} on the first hop points "
                f"at Wi-Fi rather than the ISP.")
        elif idle is not None and p["median_ms"] < 5:
            add("info", "Local network looks healthy",
                f"First hop to {p['host']} is {fmt_ms(p['median_ms'])} with "
                f"no loss, so anything slow is upstream of your router.")

    # --- DNS
    dns = rep.get("dns", {})
    sys_r = [r for r in dns.get("resolvers", [])
             if r["is_system"] and r["median_ms"] is not None]
    pub_r = [r for r in dns.get("resolvers", [])
             if not r["is_system"] and r["median_ms"] is not None]
    if sys_r:
        s = min(sys_r, key=lambda r: r["median_ms"])
        if s["median_ms"] > 120:
            add("medium", "Slow DNS resolver",
                f"{s['label']} answers in {fmt_ms(s['median_ms'])}. Every new "
                f"domain on a page pays this before a single byte arrives, "
                f"which reads as 'slow internet' even at full speed.")
        if pub_r:
            b = min(pub_r, key=lambda r: r["median_ms"])
            if s["median_ms"] > b["median_ms"] * 2.5 and s["median_ms"] > 40:
                add("medium", "A public resolver is much faster than yours",
                    f"{s['label']} takes {fmt_ms(s['median_ms'])} vs "
                    f"{fmt_ms(b['median_ms'])} for {b['label']}. Switching "
                    f"resolvers is a free, two-minute change.")
    for r in dns.get("resolvers", []):
        if r["answered"] == 0:
            add("info", f"No DNS response from {r['label']}",
                "It may be firewalled or unreachable from this network.")

    # --- throughput
    tp = rep.get("throughput", {})
    d, u = tp.get("download_mbps"), tp.get("upload_mbps")
    terr = (tp.get("download_errors") or []) + (tp.get("upload_errors") or [])
    if d is None and u is None:
        add("high", "Throughput test could not run",
            "No data moved at all, so this is a reachability problem rather "
            "than a slow link. "
            + (f"First error: {terr[0]}" if terr else "")
            + " A proxy, VPN, firewall or captive portal is the usual cause.")
    if d is not None and d < 10:
        add("high", "Very low download throughput",
            f"{fmt_mbps(d)} sustained. Compare against what you pay for.")
    if u is not None and u < 2:
        add("medium", "Very low upload throughput",
            f"{fmt_mbps(u)} up. Low upload breaks video calls and makes "
            f"browsing feel slow, because requests and ACKs queue behind "
            f"whatever else is uploading.")
    if d and u and u > 0 and d / u > 25:
        add("info", "Heavily asymmetric link",
            f"{fmt_mbps(d)} down vs {fmt_mbps(u)} up. Typical of cable/DSL; "
            f"the thin upload is often what you actually feel.")

    # --- bufferbloat
    bb = rep.get("bufferbloat", {})
    if bb.get("error"):
        add("info", "Bufferbloat test inconclusive", bb["error"])
    delta = bb.get("increase_ms")
    grade, word = grade_bufferbloat(delta)
    if delta is not None and delta >= 60:
        sev = "high" if delta >= 100 else "medium"
        add(sev, f"Bufferbloat: grade {grade} ({word})",
            f"Latency rises from {fmt_ms(bb['idle_median_ms'])} idle to "
            f"{fmt_ms(bb['loaded_median_ms'])} when the link is busy "
            f"(+{delta:.0f} ms). This is why things feel slow while a "
            f"download, backup or update is running, even though the speed "
            f"test looks fine. Fix: enable SQM / Smart Queue / QoS on your "
            f"router and cap it at ~90% of measured speed.")
    elif delta is not None:
        add("info", f"Bufferbloat: grade {grade} ({word})",
            f"Latency under load rises only {delta:.0f} ms. Queue management "
            f"is behaving.")

    # --- environment
    if rep.get("environment", {}).get("proxy_env"):
        add("medium", "Traffic is going through a proxy",
            f"{', '.join(rep['environment']['proxy_env'])} is set. Everything "
            f"above measures the proxy path. A slow or distant proxy/VPN is "
            f"itself one of the most common causes of 'the internet is slow' "
            f"when the underlying line is fine.")

    order = {"high": 0, "medium": 1, "info": 2}
    findings.sort(key=lambda f: order[f["severity"]])
    return findings


# --------------------------------------------------------------------------
# output
# --------------------------------------------------------------------------

def rule(title):
    print(f"\n\033[1m{title}\033[0m\n" + "-" * 62)


def render(rep):
    env = rep.get("environment", {})
    if env.get("proxy_env"):
        rule("ENVIRONMENT")
        print(f"  A proxy is configured for this shell "
              f"({', '.join(env['proxy_env'])}).")
        print(f"  All results below describe the path through that proxy, "
              f"not your\n  raw connection. Re-run without it to compare.")

    ident = rep["identity"]
    rule("CONNECTION")
    if ident.get("error"):
        print(f"  could not reach the measurement endpoint: {ident['error']}")
    else:
        print(f"  ISP        : {ident.get('isp')} (AS{ident.get('asn')})")
        print(f"  Public IP  : {ident.get('ip')}")
        print(f"  Location   : {ident.get('city')}, {ident.get('country')}")
        print(f"  Edge server: {ident.get('edge')}")

    rule("LATENCY  (TCP handshake, lower is better)")
    print(f"  {'target':<12}{'median':>10}{'min':>10}{'p95':>10}"
          f"{'jitter':>10}{'loss':>8}")
    for t in rep["latency"]:
        print(f"  {t['target']:<12}{fmt_ms(t['median_ms']):>10}"
              f"{fmt_ms(t['min_ms']):>10}{fmt_ms(t['p95_ms']):>10}"
              f"{fmt_ms(t['jitter_ms']):>10}{t['loss_pct']:>7.0f}%")

    rule("FIRST HOP  (your router - separates home vs ISP)")
    fh = rep["first_hop"]
    if not fh.get("gateway"):
        print(f"  {fh.get('note', 'unavailable')}")
    elif not fh.get("ping"):
        print(f"  gateway {fh['gateway']} did not answer ping "
              f"(some routers block it; not necessarily a fault)")
    else:
        p = fh["ping"]
        print(f"  gateway    : {p['host']}")
        print(f"  median     : {fmt_ms(p['median_ms'])}   "
              f"max {fmt_ms(p['max_ms'])}   jitter {fmt_ms(p['jitter_ms'])}")
        print(f"  loss       : {p['loss_pct']:.0f}%  "
              f"({p['received']}/{p['sent']} replies)")

    rule("DNS  (time to first byte starts here)")
    for r in rep["dns"]["resolvers"]:
        got = f"{r['answered']}/{r['asked']}"
        print(f"  {r['label']:<28}{fmt_ms(r['median_ms']):>10}"
              f"   worst {fmt_ms(r['worst_ms']):>9}   answered {got}")
    print(f"  {'OS resolver stack (cached)':<28}"
          f"{fmt_ms(rep['dns']['os_stack_ms']):>10}")

    rule("THROUGHPUT")
    tp = rep["throughput"]
    print(f"  download   : {fmt_mbps(tp['download_mbps'])}  "
          f"(4 streams, {tp['seconds_each']}s)")
    for e in tp.get("download_errors", []):
        print(f"               ! {e}")
    print(f"  upload     : {fmt_mbps(tp['upload_mbps'])}  "
          f"(3 streams, {tp['seconds_each']}s)")
    for e in tp.get("upload_errors", []):
        print(f"               ! {e}")

    rule("BUFFERBLOAT  (latency while the link is saturated)")
    bb = rep["bufferbloat"]
    if bb.get("error"):
        print(f"  {bb['error']}")
        if bb.get("load_mbps") is not None:
            print(f"  only {bb['load_mbps']:.2f} Mbps was moving during the "
                  f"test")
        for e in bb.get("transfer_errors", []):
            print(f"  ! {e}")
    else:
        g, w = grade_bufferbloat(bb["increase_ms"])
        print(f"  idle       : {fmt_ms(bb['idle_median_ms'])}")
        print(f"  under load : {fmt_ms(bb['loaded_median_ms'])}  "
              f"(p95 {fmt_ms(bb['loaded_p95_ms'])})")
        print(f"  increase   : +{bb['increase_ms']:.0f} ms   "
              f"-> grade {g} ({w})")

    rule("FINDINGS")
    tag = {"high": "\033[31m[!]\033[0m", "medium": "\033[33m[~]\033[0m",
           "info": "\033[32m[i]\033[0m"}
    if not rep["findings"]:
        print("  Nothing anomalous. If it still feels slow, the bottleneck is "
              "likely\n  a specific site, a VPN, or the device itself rather "
              "than the link.")
    for f in rep["findings"]:
        print(f"\n  {tag[f['severity']]} {f['title']}")
        for line in _wrap(f["detail"], 58):
            print(f"      {line}")
    print()


def _wrap(text, width):
    words, lines, cur = text.split(), [], ""
    for w in words:
        if len(cur) + len(w) + 1 > width:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur:
        lines.append(cur)
    return lines


# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--quick", action="store_true",
                    help="shorter sampling, ~30s instead of ~90s")
    ap.add_argument("--json", action="store_true",
                    help="emit raw JSON instead of the report")
    args = ap.parse_args()

    if not args.json:
        print("netdiag - measuring your connection"
              f"{' (quick mode)' if args.quick else ''}")
        print("Close other heavy network use for accurate numbers.\n")

    steps = [
        ("checking environment", lambda: probe_environment()),
        ("identifying connection", lambda: probe_identity()),
        ("testing DNS", lambda: probe_dns(args.quick)),
        ("measuring idle latency", lambda: probe_latency(args.quick)),
        ("checking first hop", lambda: probe_first_hop(args.quick)),
    ]
    rep = {"generated": time.strftime("%Y-%m-%d %H:%M:%S"),
           "platform": f"{platform.system()} {platform.release()}",
           "quick": args.quick}
    keys = ["environment", "identity", "dns", "latency", "first_hop"]

    for (label, fn), key in zip(steps, keys):
        if not args.json:
            print(f"  ... {label}", flush=True)
        rep[key] = fn()

    if not args.json:
        print("  ... measuring throughput (this saturates your link)",
              flush=True)
    rep["throughput"] = probe_throughput(args.quick)

    lat_ok = [t["median_ms"] for t in rep["latency"]
              if t["median_ms"] is not None]
    if not args.json:
        print("  ... measuring latency under load", flush=True)
    rep["bufferbloat"] = probe_bufferbloat(min(lat_ok) if lat_ok else None,
                                           args.quick)

    rep["findings"] = interpret(rep)

    if args.json:
        print(json.dumps(rep, indent=2))
    else:
        render(rep)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\ninterrupted")
        sys.exit(130)
