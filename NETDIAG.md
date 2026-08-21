# netdiag.py — internet slowness diagnostic

Standard library only. No `pip install`, no root, no telemetry.
Python 3.8+ on macOS, Linux or Windows.

```bash
python3 netdiag.py            # full run, ~90 seconds
python3 netdiag.py --quick    # ~30 seconds
python3 netdiag.py --json     # machine-readable
```

Run it **on the machine that feels slow**, over the connection that feels
slow, with other heavy network use closed. Run it twice — once on Wi-Fi and
once on a cable if you can — the difference between those two runs is
usually the whole answer.

## Why not just use a speed test

A speed test answers one question: how many bits per second can this link
move in a burst. That is frequently *not* why a connection feels slow. This
script measures the other causes too.

| Test | What it catches |
|---|---|
| Connection identity | Which ISP/ASN you're on and which edge server you reach |
| DNS | A slow resolver adds delay to every new domain before a single byte moves — reads as "slow internet" at full bandwidth |
| Idle latency, jitter, loss | High RTT makes every page feel sluggish; jitter wrecks calls; even 2% loss collapses TCP throughput |
| First hop (your router) | **Separates a home Wi-Fi problem from an ISP problem** |
| Throughput | Sustained down/up over 4 and 3 parallel streams, first second discarded for TCP slow-start |
| Bufferbloat | Latency *while the link is busy* |

## The two tests that matter most

**First hop.** The script finds your default gateway and times it. A wired
link answers in under 2 ms, healthy Wi-Fi under 10 ms, with no loss. If that
hop is slow or lossy, the bottleneck is inside your home and no plan upgrade
will fix it — move closer to the router, change Wi-Fi channel, or use a cable.
If the first hop is clean, the problem is upstream and worth an ISP call.

**Bufferbloat.** This is the usual answer to "the speed test says 400 Mbps
but everything still feels laggy." When a link saturates, an oversized queue
in the modem or router makes every interactive packet wait behind a backlog.
Latency can jump from 20 ms to 500 ms while a backup or update runs. Graded
here A–F by how far latency rises under load. Anything C or worse is fixed
by enabling **SQM / Smart Queue / QoS** on the router and capping it at
about 90% of your measured speed — a router setting, not a faster plan.

## Reading the output

The `FINDINGS` section ranks what it found: `[!]` high, `[~]` medium,
`[i]` informational. Empty findings with the connection still feeling slow
points at a specific site, a VPN, or the device itself rather than the link.

Two caveats worth knowing:

- If `HTTP(S)_PROXY` is set, or you're on a VPN, every number describes the
  path through it. The script says so when it detects one. A distant proxy
  is itself a very common cause of the slowness being investigated.
- Throughput uses Cloudflare's public endpoint. Behind a restrictive
  firewall or captive portal those transfers fail; the script reports the
  error rather than silently showing `n/a`, and marks the bufferbloat
  result invalid rather than reporting a meaningless grade.
