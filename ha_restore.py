#!/usr/bin/env python3
"""ha-auto-restore: bring a broken Home Assistant OS back from another computer.

Python 3.9+, standard library only. Settings live in config.ini (see config.example.ini).

Commands:
  diagnose      what answers: network, HA Core, Observer, app SSH, system SSH (22222), backups
  where         where backups are found and which one would be used
  restart       case 1: restart HA Core over the SSH app, rebuild it if that is not enough
  restore       case 1: restore HA (config + database) from the newest backup on the HA box
                (--full: also apps and folders)
  reinstall     case 2: wipe the HAOS data partition over system SSH (port 22222), HAOS comes
                back as a fresh install, then runs "upload"
  upload [tar]  fresh HAOS (also a new disk, case 3): upload the newest backup found (or the
                given .tar) and restore it through the onboarding API, no login needed
                (--ha-only: without apps and folders)
  sync          copy this tool to the local emergency folder (when it lives somewhere that
                disappears together with HA, e.g. the HA Samba share)
  auto          diagnose, then the steps in order, asking before every destructive one

Every destructive step asks you to type YES. The backup key is never printed.
"""
import configparser
import getpass
import http.client
import json
import os
import shutil
import socket
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
IS_WINDOWS = os.name == "nt"


# ---------- configuration ----------

def _expand(value):
    return os.path.expandvars(os.path.expanduser(value.strip())) if value else value


def load_config():
    path = Path(os.environ.get("HA_RESTORE_CONFIG", HERE / "config.ini"))
    if not path.exists():
        sys.exit(f"Missing configuration: {path}\nCopy config.example.ini to config.ini and edit it.")
    cp = configparser.ConfigParser(interpolation=None)
    cp.read(path, encoding="utf-8")
    c = {
        "host": cp.get("homeassistant", "host"),
        "port": cp.getint("homeassistant", "port", fallback=8123),
        "ssh_host": cp.get("ssh", "host", fallback=None) or None,
        "ssh_user": cp.get("ssh", "user", fallback="") or None,
        "ssh_port": cp.getint("ssh", "port", fallback=22),
        "sys_port": cp.getint("ssh", "system_port", fallback=22222),
        "key_storage": cp.get("backup_key", "storage", fallback="dpapi" if IS_WINDOWS else "prompt").strip(),
        "key_file": _expand(cp.get("backup_key", "file", fallback="~/.ha-auto-restore/backup_key"
                                   + (".dpapi" if IS_WINDOWS else ""))),
        "emergency_dir": _expand(cp.get("paths", "emergency_dir", fallback="~/ha-auto-restore")),
        "sources": [],
    }
    c["ssh_host"] = c["ssh_host"] or c["host"]
    if cp.has_section("backup_sources"):
        for name, folder in cp.items("backup_sources"):
            c["sources"].append((name, Path(_expand(folder))))
    # environment overrides, handy for tests against a VM
    if os.environ.get("HA_HOST"):
        c["host"] = os.environ["HA_HOST"]
    if os.environ.get("HA_PORT"):
        c["port"] = int(os.environ["HA_PORT"])
        c["port_fixed"] = True
    return c


CFG = load_config()
HOST = CFG["host"]
HA_PORT = CFG["port"]
HA_URL = f"http://{HOST}:{HA_PORT}"


# ---------- helpers ----------

def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def port_open(port, timeout=3):
    try:
        with socket.create_connection((HOST, port), timeout=timeout):
            return True
    except OSError:
        return False


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


_opener = urllib.request.build_opener(_NoRedirect)


def _set_port(port):
    global HA_PORT, HA_URL
    HA_PORT, HA_URL = port, f"http://{HOST}:{port}"


def _follow_port(location):
    """A fresh HAOS (18+) serves onboarding on port 80 and 8123 redirects there (307, no port).
    Only the port is taken from the redirect; the address stays HOST."""
    new = urllib.parse.urlsplit(location).port or 80
    if new != HA_PORT and not CFG.get("port_fixed"):
        log(f"HA answers on port {new}, not {HA_PORT}: switching")
        _set_port(new)
        return True
    return False


def http_json(method, path, body=None, timeout=30, _again=0):
    """Return (status, json or text). Status 0 when HA does not answer."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(HA_URL + path, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with _opener.open(req, timeout=timeout) as r:
            raw, status = r.read(), r.status
    except urllib.error.HTTPError as e:
        raw, status = e.read(), e.code
        if status in (301, 302, 307, 308) and not _again and _follow_port(e.headers.get("Location", "")):
            return http_json(method, path, body, timeout, _again=1)
    except (OSError, http.client.HTTPException):
        return 0, None
    try:
        return status, json.loads(raw)
    except ValueError:
        return status, raw.decode(errors="replace")


def core_alive():
    if http_json("GET", "/manifest.json", timeout=10)[0] == 200:
        return True
    # a fresh HA sits on 80, a restored one goes back to the port in its configuration (8123)
    if not CFG.get("port_fixed"):
        for p in (CFG["port"], 8123, 80):
            if p != HA_PORT and port_open(p, 2):
                old = HA_PORT
                _set_port(p)
                if http_json("GET", "/manifest.json", timeout=10)[0] == 200:
                    log(f"HA now answers on port {p}")
                    return True
                _set_port(old)
    return False


def onboarding_state():
    """None if onboarding is over or HA does not answer, else {step: done}."""
    status, data = http_json("GET", "/api/onboarding", timeout=10)
    if status != 200 or not isinstance(data, list):
        return None
    return {s["step"]: s["done"] for s in data}


def wait_for(cond, minutes, what, every=15):
    log(f"Waiting: {what} (up to {minutes} min)")
    end = time.time() + minutes * 60
    while time.time() < end:
        if cond():
            log("ok")
            return True
        time.sleep(every)
    log(f"Timed out: {what}")
    return False


def ssh(script, system=False, timeout=600):
    """Run a shell script on the HA box, passed on stdin. Returns (code, output)."""
    cmd = ["ssh", "-o", "ConnectTimeout=10", "-o", "BatchMode=yes"]
    if system:  # HAOS host, port 22222, root; its host key changes after a reinstall
        cmd += ["-p", str(CFG["sys_port"]), "-l", "root", "-o", "StrictHostKeyChecking=accept-new",
                CFG["ssh_host"], "sh -s"]
    else:       # SSH app: login shell, so that $SUPERVISOR_TOKEN is set
        cmd += ["-p", str(CFG["ssh_port"])] + (["-l", CFG["ssh_user"]] if CFG["ssh_user"] else [])
        cmd += [CFG["ssh_host"], "bash -l -s"]
    try:
        p = subprocess.run(cmd, input=script, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        return 124, "timeout"
    return p.returncode, (p.stdout + p.stderr).strip()


def confirm(question):
    return input(f"\n{question}\nType YES to go on: ").strip() == "YES"


_key_cache = None


def backup_key():
    """The backup encryption key, from the configured storage. Never printed."""
    global _key_cache
    if _key_cache:
        return _key_cache
    storage, f = CFG["key_storage"], Path(CFG["key_file"])
    if storage == "dpapi":
        if not IS_WINDOWS:
            sys.exit("storage = dpapi works only on Windows")
        if not f.exists():
            sys.exit(f"No saved backup key: run save_backup_key.ps1 first ({f})")
        ps = ("$s = Get-Content -LiteralPath $env:HA_KEYFILE | ConvertTo-SecureString; "
              "[Runtime.InteropServices.Marshal]::PtrToStringBSTR("
              "[Runtime.InteropServices.Marshal]::SecureStringToBSTR($s))")
        p = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                           capture_output=True, text=True, env={**os.environ, "HA_KEYFILE": str(f)})
        key = p.stdout.rstrip("\r\n")
        if p.returncode != 0 or not key:
            sys.exit("Cannot decrypt the saved key (it must be saved by this Windows user on this PC)")
    elif storage == "file":
        if not f.exists():
            sys.exit(f"Key file not found: {f}")
        if not IS_WINDOWS and f.stat().st_mode & 0o077:
            sys.exit(f"{f} is readable by others: chmod 600 it first")
        key = f.read_text(encoding="utf-8").strip()
    else:
        key = getpass.getpass("Backup encryption key: ").strip()
    if not key:
        sys.exit("Empty backup key")
    _key_cache = key
    return key


def find_backups(show=False):
    """[(mtime, source, path)] of reachable .tar files, newest first."""
    found = []
    for i, (name, folder) in enumerate(CFG["sources"]):
        err = None
        try:
            # os.listdir, not glob: glob hides "access denied" (e.g. NAS share without rights)
            tars = [folder / n for n in os.listdir(folder) if n.lower().endswith(".tar")]
            tars = [p for p in tars if p.is_file()]
        except OSError as e:
            tars, err = [], e
        if show:
            if err:
                print(f"  NO  {name}: not reachable ({folder}: {err})")
            elif not tars:
                print(f"  --  {name}: no backups")
            else:
                n = max(tars, key=lambda p: p.stat().st_mtime)
                print(f"  ok  {name}: {len(tars)} backup(s), newest {n.name} "
                      f"({time.strftime('%Y-%m-%d %H:%M', time.localtime(n.stat().st_mtime))})")
        found += [(p.stat().st_mtime, -i, name, p) for p in tars]
    found.sort(reverse=True)
    return [(m, name, p) for m, _, name, p in found]


def backup_info(path):
    """Read backup.json from the .tar. It sits at the end: mode "r:" seeks from header to header
    without reading the data ("r|" would pull the whole file from a cloud-synced folder)."""
    with tarfile.open(path, "r:") as t:
        for m in t:
            if m.name.lstrip("./") == "backup.json":
                return json.load(t.extractfile(m))
    return None


def newest_backup():
    """(path, description) of the newest usable backup, or None."""
    for _, source, path in find_backups():
        try:
            info = backup_info(path)
        except (OSError, tarfile.TarError, ValueError) as e:
            log(f"Skipping {source} {path.name}: unreadable ({e})")
            continue
        if not info or not info.get("homeassistant"):
            log(f"Skipping {source} {path.name}: no Home Assistant inside")
            continue
        try:  # the date in backup.json is UTC
            when = datetime.fromisoformat(info["date"]).astimezone().strftime("%Y-%m-%d %H:%M")
        except (KeyError, ValueError):
            when = "unknown date"
        return path, f"{source}, '{info.get('name')}' of {when}, {path.stat().st_size / 1e9:.1f} GB"
    return None


# ---------- commands ----------

def diagnose():
    ping = ["ping", "-n" if IS_WINDOWS else "-c", "1", HOST]
    rows = [
        ("network (ping)", subprocess.run(ping, capture_output=True).returncode == 0),
        (f"HA Core ({HA_PORT})", core_alive()),
        ("Observer (4357)", port_open(4357)),
        (f"SSH app ({CFG['ssh_port']})", port_open(CFG["ssh_port"])),
        (f"system SSH ({CFG['sys_port']})", port_open(CFG["sys_port"])),
    ]
    for name, ok in rows:
        print(f"  {'ok ' if ok else 'NO '} {name}")
    onb = onboarding_state()
    if onb is not None and not onb.get("user", True):
        print("  !!  HA is a fresh install waiting for onboarding: use 'upload'")
    print("  backups:")
    find_backups(show=True)
    if CFG["key_storage"] in ("dpapi", "file"):
        print(f"  backup key saved: {'yes' if Path(CFG['key_file']).exists() else 'NO'}")
    else:
        print("  backup key: asked when needed")
    return {name.split(" (")[0]: ok for name, ok in rows}


def restart():
    if not port_open(CFG["ssh_port"]):
        log("The SSH app does not answer: case 1 is not possible, use 'reinstall'")
        return False
    log("Restarting HA Core")
    ssh("ha core restart", timeout=900)
    if wait_for(core_alive, 10, "HA Core answers"):
        return True
    log("Rebuilding HA Core (ha core rebuild)")
    ssh("ha core rebuild", timeout=1200)
    return wait_for(core_alive, 15, "HA Core answers")


def restore(full=False):
    if not port_open(CFG["ssh_port"]):
        log("The SSH app does not answer: use 'reinstall'")
        return False
    key = backup_key()
    _, out = ssh('curl -s -H "Authorization: Bearer $SUPERVISOR_TOKEN" http://supervisor/backups')
    try:
        backups = json.loads(out)["data"]["backups"]
    except (ValueError, KeyError):
        log(f"The Supervisor does not answer ({out[:200]}): use 'reinstall'")
        return False
    # automatic backups are often "partial" (selected folders): containing HA is what matters
    with_ha = [b for b in backups if (b.get("content") or {}).get("homeassistant")]
    if not with_ha:
        log("No backup with Home Assistant on the HA box")
        return False
    b = max(with_ha, key=lambda x: x["date"])
    what = "FULL (HA, apps, folders)" if full else "HA only (configuration and database)"
    if not confirm(f"Restore {what} from '{b['name']}' of {b['date'][:16]}. "
                   "Anything changed since then is lost."):
        return False
    body = {"password": key, "background": True, "homeassistant": True}
    if full:
        body["addons"] = b["content"].get("addons", [])
        body["folders"] = b["content"].get("folders", [])
    url = f"http://supervisor/backups/{b['slug']}/restore/partial"
    script = (f'curl -s -X POST -H "Authorization: Bearer $SUPERVISOR_TOKEN" '
              f'-H "Content-Type: application/json" -d @- {url} <<\'JSON\'\n{json.dumps(body)}\nJSON\n')
    _, out = ssh(script, timeout=300)
    log(f"Supervisor: {out[:300]}")
    try:
        if json.loads(out).get("result") != "ok":
            return False
    except ValueError:
        return False
    time.sleep(60)
    return wait_for(core_alive, 45, "restored HA answers", every=30)


def reinstall():
    found = newest_backup()
    if not found:
        sys.exit("No reachable backup: nothing is wiped without one.")
    tar, desc = found
    backup_key()  # make sure the key is there before wiping anything
    if not port_open(CFG["sys_port"]):
        sys.exit(f"System SSH ({CFG['sys_port']}) is closed: cannot wipe from here. "
                 "Reinstall HAOS by hand (or boot a spare disk) and then run 'upload'.")
    if not confirm(f"WIPE ALL Home Assistant DATA on {HOST} (HAOS itself stays). "
                   f"Then upload the backup: {desc}."):
        return False
    code, out = ssh("busctl call io.hass.os /io/hass/os/System io.hass.os.System "
                    "ScheduleWipeDevice && (sleep 2; reboot) >/dev/null 2>&1 &", system=True, timeout=60)
    log(f"Wipe scheduled: {out or code}")
    if code != 0 or "true" not in out:
        log("The wipe was not scheduled, stopping")
        return False
    wait_for(lambda: not port_open(HA_PORT, 2), 5, "the HA box reboots", every=5)
    return upload(tar)


def upload(file=None, ha_only=False):
    if file:
        tar = Path(file)
        if not tar.exists():
            sys.exit(f"Not found: {tar}")
    else:
        found = newest_backup()
        if not found:
            sys.exit("No reachable backup")
        tar, desc = found
        log(f"Backup to upload: {desc}")
    key = backup_key()

    def onboarding():
        onb = onboarding_state()
        return onb is not None and onb.get("user") is False

    if not wait_for(onboarding, 60, "fresh HA waiting for onboarding (the first boot downloads HA)",
                    every=30):
        return False

    def info():
        s, d = http_json("GET", "/api/onboarding/backup/info")
        return d if s == 200 and isinstance(d, dict) else None

    if http_json("GET", "/api/onboarding/backup/info")[0] == 404:
        log("This HA has no onboarding backup API (needs Home Assistant 2025.1 or newer): "
            "restore it by hand from the welcome screen.")
        return False
    if not wait_for(lambda: info() is not None, 10, "backup manager ready"):
        return False

    slug = (backup_info(tar) or {}).get("slug")
    if slug and any(x["backup_id"] == slug for x in info()["backups"]):
        backup_id = slug
        log(f"Backup already uploaded ({slug}): not uploading again")
    else:
        log(f"Uploading {tar.name} ({tar.stat().st_size / 1e9:.1f} GB)")
        backup_id = _upload(tar)
        log(f"Uploaded: {backup_id}")
    data = next((x for x in info()["backups"] if x["backup_id"] == backup_id), None)
    if not data:
        log("The uploaded backup is not listed")
        return False
    body = {
        "backup_id": backup_id,
        "agent_id": "hassio.local",
        "password": key,
        "restore_database": True,
        "restore_addons": [] if ha_only else [a["slug"] for a in data.get("addons", [])],
        "restore_folders": [] if ha_only else data.get("folders", []),
    }
    log(f"Restoring: {len(body['restore_addons'])} app(s), folders {body['restore_folders']}")
    s, d = http_json("POST", "/api/onboarding/backup/restore", body, timeout=300)
    if s not in (200, 0):  # 0: HA already went down for the restore
        time.sleep(5)
        event = (info() or {}).get("last_action_event")
        log(f"Restore refused: {s} {d or ''} - state in HA: {event}")
        if (event or {}).get("state") == "failed":
            log("If the Supervisor log says 'no supervisor internet connection': "
                "the HA box needs internet during the restore.")
            return False
        if s == 400:  # e.g. incorrect_password
            return False
        log("Still waiting: it may have started anyway")
    time.sleep(60)

    def done():
        if not core_alive():
            return False
        onb = onboarding_state()  # after onboarding /api/onboarding is gone (404)
        return onb is None or onb.get("user") is True

    ok = wait_for(done, 90, "restored HA is up", every=30)
    if ok:
        log("Done. Check your integrations, Zigbee/Z-Wave sticks, VPNs and dashboards.")
    return ok


def _upload(tar):
    boundary = uuid.uuid4().hex
    head = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
            f"filename=\"{tar.name}\"\r\nContent-Type: application/x-tar\r\n\r\n").encode()
    tail = f"\r\n--{boundary}--\r\n".encode()
    conn = http.client.HTTPConnection(HOST, HA_PORT, timeout=600)
    conn.putrequest("POST", "/api/onboarding/backup/upload?agent_id=hassio.local")
    conn.putheader("Content-Type", f"multipart/form-data; boundary={boundary}")
    conn.putheader("Content-Length", str(len(head) + tar.stat().st_size + len(tail)))
    conn.endheaders()
    conn.send(head)
    with open(tar, "rb") as f:
        while chunk := f.read(1 << 20):
            conn.send(chunk)
    conn.send(tail)
    r = conn.getresponse()
    text = r.read().decode(errors="replace")
    if r.status not in (200, 201):
        sys.exit(f"Upload failed: {r.status} {text[:300]}")
    return json.loads(text)["backup_id"]


def sync():
    dest = Path(CFG["emergency_dir"])
    if HERE.resolve() == dest.resolve():
        log("Already running from the emergency folder")
    else:
        dest.mkdir(parents=True, exist_ok=True)
        shutil.copytree(HERE, dest, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("__pycache__", ".git"))
        log(f"Tool copied to {dest}")
    return where()


def where():
    print("Backup sources:")
    find_backups(show=True)
    found = newest_backup()
    print(f"Would use: {found[1] if found else 'NO BACKUP'}")
    return bool(found)


def auto():
    state = diagnose()
    onb = onboarding_state()
    if onb is not None and not onb.get("user", True):
        return upload()
    if state["HA Core"]:
        log("HA answers: nothing to restore")
        return True
    if not state["network"]:
        log("The HA box does not answer on the network: power-cycle it. "
            "If it does not come back it is case 3 (hardware).")
        return False
    if state["SSH app"]:
        if restart() or restore():
            return True
    return reinstall()


def main():
    args = sys.argv[1:]
    commands = {
        "diagnose": lambda: diagnose() and True,
        "where": where,
        "restart": restart,
        "restore": lambda: restore(full="--full" in args),
        "reinstall": reinstall,
        "upload": lambda: upload(next((a for a in args[1:] if not a.startswith("--")), None),
                                 ha_only="--ha-only" in args),
        "sync": sync,
        "auto": auto,
    }
    if not args or args[0] not in commands:
        sys.exit(__doc__)
    sys.exit(0 if commands[args[0]]() else 1)


if __name__ == "__main__":
    main()
