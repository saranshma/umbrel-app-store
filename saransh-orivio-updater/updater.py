#!/usr/bin/env python3
"""Keep Orivio TV on the Apple TV at the latest GitHub release.

Every CHECK_INTERVAL seconds: look up the newest release, and if its tag differs from the
last one installed, download the "Sideload" IPA, strip the Top Shelf extension and the
shared app-group entitlement (Apple refuses to register that group on a foreign team),
serve the result on a local port and ask atvloadly (MCP install_app) to install it.
"""
import json, os, shutil, struct, sys, threading, time, urllib.request, zipfile
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

REPO = os.environ.get("RELEASE_REPO", "prehakanson-art/OrivioTVAppleTV")
ASSET_SUFFIX = os.environ.get("ASSET_SUFFIX", "Sideload.ipa")  # not Sideloadly.ipa
ATV = os.environ.get("ATVLOADLY_URL", "http://127.0.0.1:5533")
PORT = int(os.environ.get("SERVE_PORT", "5534"))
INTERVAL = int(os.environ.get("CHECK_INTERVAL", "21600"))
DATA = "/data"
STATE = f"{DATA}/state.json"
WWW = f"{DATA}/www"


def log(*a):
    print(time.strftime("%F %T"), *a, flush=True)


def http_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "orivio-updater", "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def latest_release():
    for rel in http_json(f"https://api.github.com/repos/{REPO}/releases?per_page=10"):
        if rel.get("draft") or rel.get("prerelease"):
            continue
        for a in rel["assets"]:
            if a["name"].endswith(ASSET_SUFFIX) and not a["name"].endswith("Sideloadly.ipa"):
                return rel["tag_name"], a["browser_download_url"]
    raise RuntimeError("no suitable release asset found")


def strip_signature(path):
    """Remove LC_CODE_SIGNATURE from a thin 64-bit Mach-O (like `codesign --remove-signature`),
    dropping the embedded entitlements, including the shared app-group one."""
    with open(path, "rb") as f:
        b = bytearray(f.read())
    magic, _, _, _, ncmds, sizeofcmds, _, _ = struct.unpack_from("<8I", b, 0)
    if magic != 0xFEEDFACF:
        raise RuntimeError("expected a thin 64-bit little-endian Mach-O")
    off, sig, linkedit = 32, None, None
    for _ in range(ncmds):
        cmd, size = struct.unpack_from("<2I", b, off)
        if cmd == 0x1D:  # LC_CODE_SIGNATURE
            sig = (off, size, *struct.unpack_from("<2I", b, off + 8))
        elif cmd == 0x19 and bytes(b[off + 8:off + 24]).rstrip(b"\0") == b"__LINKEDIT":
            linkedit = off
        off += size
    if sig is None:
        return
    s_off, s_size, dataoff, datasize = sig
    if dataoff + datasize != len(b) or linkedit is None:
        raise RuntimeError("code signature is not the last thing in the file")
    end = 32 + sizeofcmds
    b[s_off:end - s_size] = b[s_off + s_size:end]
    b[end - s_size:end] = bytes(s_size)
    struct.pack_into("<2I", b, 16, ncmds - 1, sizeofcmds - s_size)
    fileoff = struct.unpack_from("<Q", b, linkedit + 40)[0]
    new_filesize = dataoff - fileoff
    struct.pack_into("<Q", b, linkedit + 32, (new_filesize + 0x3FFF) & ~0x3FFF)  # vmsize
    struct.pack_into("<Q", b, linkedit + 48, new_filesize)  # filesize
    with open(path, "wb") as f:
        f.write(b[:dataoff])


def repackage(url, out):
    work = f"{DATA}/work"
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work)
    src = f"{work}/src.ipa"
    urllib.request.urlretrieve(url, src)
    with zipfile.ZipFile(src) as z:
        z.extractall(work)
    os.remove(src)
    apps = [d for d in os.listdir(f"{work}/Payload") if d.endswith(".app")]
    app = f"{work}/Payload/{apps[0]}"
    shutil.rmtree(f"{app}/PlugIns", ignore_errors=True)
    exe = f"{app}/{apps[0][:-4]}"
    strip_signature(exe)
    os.makedirs(WWW, exist_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for root, _, files in os.walk(f"{work}/Payload"):
            z.write(root, os.path.relpath(root, work))
            for fn in files:
                p = os.path.join(root, fn)
                z.write(p, os.path.relpath(p, work))
    shutil.rmtree(work, ignore_errors=True)


class MCP:
    def __init__(self):
        self.sid = None
        self.n = 0

    def _post(self, body):
        h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        if self.sid:
            h["Mcp-Session-Id"] = self.sid
        req = urllib.request.Request(f"{ATV}/mcp", json.dumps(body).encode(), h)
        with urllib.request.urlopen(req, timeout=120) as r:
            self.sid = r.headers.get("Mcp-Session-Id") or self.sid
            text = r.read().decode()
        for line in text.splitlines():
            if line.startswith("data:"):
                return json.loads(line[5:])

    def start(self):
        self._post({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "orivio-updater", "version": "1"}}})
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def call(self, name, args=None):
        self.n += 1
        resp = self._post({"jsonrpc": "2.0", "id": 100 + self.n, "method": "tools/call",
                           "params": {"name": name, "arguments": args or {}}})
        if "error" in resp:
            raise RuntimeError(resp["error"])
        res = resp["result"]
        text = "".join(c.get("text", "") for c in res.get("content", []))
        if res.get("isError"):
            raise RuntimeError(text)
        return text


def install(ipa_name):
    m = MCP()
    m.start()
    log("install_app:", m.call("install_app", {"ipa_url": f"http://127.0.0.1:{PORT}/{ipa_name}"})[:500])
    time.sleep(5)
    deadline = time.time() + 1800
    while time.time() < deadline:
        st = m.call("get_install_status")
        if "in_progress" not in st:
            log("install status:", st[:500])
            return "completed" in st and "fail" not in st.lower() and "error" not in st.lower()
        time.sleep(10)
    raise RuntimeError("install timed out")


def check_once():
    state = json.load(open(STATE)) if os.path.exists(STATE) else {}
    tag, url = latest_release()
    if state.get("installed_tag") == tag:
        log(f"{tag} already installed")
        return
    log(f"new release {tag}, repackaging {url}")
    name = f"Orivio-{tag}.ipa"
    repackage(url, f"{WWW}/{name}")
    if install(name):
        json.dump({"installed_tag": tag, "at": time.time()}, open(STATE, "w"))
        log(f"{tag} installed")
    else:
        log(f"{tag} install FAILED, will retry next cycle")
    for f in os.listdir(WWW):
        os.remove(f"{WWW}/{f}")


def main():
    os.makedirs(WWW, exist_ok=True)
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), partial(SimpleHTTPRequestHandler, directory=WWW))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log(f"serving {WWW} on :{PORT}; checking {REPO} every {INTERVAL}s")
    while True:
        try:
            check_once()
        except Exception as e:
            log("check failed:", repr(e))
        time.sleep(INTERVAL)


if __name__ == "__main__":
    sys.exit(main())
