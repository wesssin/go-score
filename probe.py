#!/usr/bin/env python3
"""probe.py - times each step of connecting to the Go Score data servers (DNS, TCP, TLS, first byte, full answer).
Run on GitHub Actions by the "Probe data servers" workflow; results go to diag/."""
import os
import socket
import ssl
import time
from datetime import datetime, timezone

TARGETS = [
    ("PacIOOS ERDDAP (WW3/SWAN/HIMB)", "pae-paha.pacioos.hawaii.edu", "/erddap/griddap/ww3_hawaii.dds"),
    ("PacIOOS WW3 one point (real request)", "pae-paha.pacioos.hawaii.edu",
     "/erddap/griddap/ww3_hawaii.csv?Thgt[(last)][0][(21.6070)][(202.4800)]"),
    ("PacIOOS ERDDAP home page", "pae-paha.pacioos.hawaii.edu", "/erddap/index.html"),
    ("Hawaii Mesonet (HCDP)", "api.hcdp.ikewai.org", "/"),
    ("NWS api", "api.weather.gov", "/"),
    ("Open-Meteo marine", "marine-api.open-meteo.com", "/v1/marine?latitude=21.6&longitude=-157.5&hourly=wave_height&forecast_days=1"),
    ("NDBC", "www.ndbc.noaa.gov", "/data/realtime2/51202.txt"),
]


def probe(host, path, family, timeout=25):
    out = {}
    t0 = time.time()
    try:
        infos = socket.getaddrinfo(host, 443, family, socket.SOCK_STREAM)
    except Exception as e:  # noqa
        return {"error": "DNS: %s" % e}
    out["dns_s"] = time.time() - t0
    addr = infos[0][4]
    out["addr"] = addr[0]
    s = socket.socket(infos[0][0], socket.SOCK_STREAM)
    s.settimeout(timeout)
    t1 = time.time()
    try:
        s.connect(addr)
    except Exception as e:  # noqa
        out["error"] = "TCP connect: %s after %.1fs" % (e, time.time() - t1)
        return out
    out["tcp_s"] = time.time() - t1
    t2 = time.time()
    try:
        ss = ssl.create_default_context().wrap_socket(s, server_hostname=host)
    except Exception as e:  # noqa
        out["error"] = "TLS handshake: %s after %.1fs" % (e, time.time() - t2)
        return out
    out["tls_s"] = time.time() - t2
    t3 = time.time()
    try:
        ss.sendall(("GET %s HTTP/1.1\r\nHost: %s\r\nUser-Agent: windward-go-score-probe/1.0\r\nConnection: close\r\n\r\n" % (path, host)).encode())
        first = ss.recv(1)
        out["first_byte_s"] = time.time() - t3
        buf = first
        while True:
            b = ss.recv(65536)
            if not b:
                break
            buf += b
        out["total_s"] = time.time() - t3
        out["bytes"] = len(buf)
        head, _, body = buf.partition(b"\r\n\r\n")
        out["status"] = head.split(b"\r\n")[0].decode("latin1")
        out["body"] = body[:220].decode("utf-8", "replace").replace("\n", " | ")
    except Exception as e:  # noqa
        out["error"] = "request: %s after %.1fs" % (e, time.time() - t3)
    finally:
        try:
            ss.close()
        except Exception:  # noqa
            pass
    return out


def main():
    os.makedirs("diag", exist_ok=True)
    now = datetime.now(timezone.utc)
    lines = ["Probe from GitHub runner at %s" % now.strftime("%Y-%m-%d %H:%M UTC")]
    for name, host, path in TARGETS:
        try:
            fams = sorted({i[0] for i in socket.getaddrinfo(host, 443, 0, socket.SOCK_STREAM)}, key=int)
        except Exception as e:  # noqa
            lines.append("%s: DNS failed: %s" % (name, e))
            continue
        for fam in fams:
            for attempt in range(3):
                r = probe(host, path, fam)
                fam_txt = "IPv6" if fam == socket.AF_INET6 else "IPv4"
                lines.append("%-40s %s try %d: %s" % (name, fam_txt, attempt + 1, ", ".join(
                    "%s=%s" % (k, ("%.2f" % v) if isinstance(v, float) else v) for k, v in r.items())))
                time.sleep(1)
    txt = "\n".join(lines)
    print(txt)
    with open(os.path.join("diag", now.strftime("probe_%Y%m%d_%H%M.txt")), "w") as f:
        f.write(txt + "\n")


if __name__ == "__main__":
    main()
