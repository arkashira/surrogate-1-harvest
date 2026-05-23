# DNS fallback helper — patches socket.getaddrinfo to use dig @8.8.8.8
# when system resolver fails (ISP DNS filtering certain AI endpoints).
# Import at top of any Python script: exec(open(...).read())
import socket as _sock
import subprocess as _sp

_orig_getaddrinfo = _sock.getaddrinfo

def _resilient_getaddrinfo(host, *args, **kwargs):
    try:
        return _orig_getaddrinfo(host, *args, **kwargs)
    except _sock.gaierror:
        # Fall back: resolve via public DNS (bypass ISP filtering)
        for resolver in ("1.1.1.1", "8.8.8.8", "9.9.9.9"):
            try:
                out = _sp.check_output(
                    ["dig", "+short", "+time=3", "+tries=1", f"@{resolver}", host],
                    text=True, timeout=5, stderr=_sp.DEVNULL
                ).strip().splitlines()
                ip = next((ln for ln in out if ln and ln[0].isdigit()), None)
                if ip:
                    return _orig_getaddrinfo(ip, *args, **kwargs)
            except Exception:
                continue
        raise

_sock.getaddrinfo = _resilient_getaddrinfo
