r"""Build a clean RouterOS CHR image with container and rose-storage installed.

The result is the stock CHR image of the given version with:
  * the official container and rose-storage packages installed;
  * device-mode "advanced" with container=yes and traffic-gen=yes;
  * an empty configuration, reset with no NIC attached, so the first NIC of
    the machine it is written to comes up as ether1;
  * no license, the default admin account, and nothing left of the build.

Nothing is ever typed into the guest: the CHR serial login redraws itself on
any input, so the build steers RouterOS with scripts injected into the image.

  1. rw/autorun.scr and rw/disk/build-stage2.rsc are written into the stock
     image.  On first boot autorun waits for the built-in DHCP client,
     fetches both packages from a local HTTP server, schedules stage 2 for
     the next startup and reboots cleanly, which installs the packages;
  2. stage 2 checks the packages, schedules a configuration reset for the
     next startup and requests device-mode; the power is then cut, which
     confirms device-mode;
  3. the image boots without any NIC and the scheduled reset wipes the
     configuration.  QEMU runs with -no-reboot, so every guest reboot shows
     up as the process exiting;
  4. once a boot stays up, it is powered off through ACPI without logging
     in, and the stock (empty) rw/store/autorun.scr is put back;
  5. a throwaway copy boots with a verification autorun that dumps the state
     into files, which are read back out of the image and checked.

The guest reports its progress as HTTP requests to the build host, so no
marker files end up in the image.

Linux (GitHub Actions): needs qemu-system-x86, unzip, sfdisk and sudo; KVM is
used when /dev/kvm is accessible.  Windows: needs QEMU (see vm.py), 7-Zip and
Docker Desktop, which provides the loop mounts; QEMU runs under TCG there.

Usage:  python build.py <version>          e.g. python build.py 7.24.4
Output: <CHR_BUILD_ROOT>/out/chr-<version>-container-rose.img and its .sha256
"""

import functools
import hashlib
import http.server
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS))
import vm  # noqa: E402

VERSION = sys.argv[1]
if not re.fullmatch(r"[0-9]+\.[0-9]+(\.[0-9]+)?([a-z]+[0-9]+)?", VERSION):
    sys.exit(f"not a RouterOS version: {VERSION!r}")

WINDOWS = os.name == "nt"
ROOT = Path(os.environ.get("CHR_BUILD_ROOT") or (r"D:\chr-build" if WINDOWS else TOOLS.parent / "work"))
WORK = ROOT / VERSION
OUT = ROOT / "out"
STOCK = WORK / f"chr-{VERSION}.img"
IMAGE = WORK / f"chr-{VERSION}-container-rose.img"
VERIFY_COPY = WORK / "verify-copy.img"
PKG_DIR = WORK / "pkg"
PORT = 18081
LOG = WORK / "build.log"
TRANSCRIPT = WORK / "serial.log"
PACKAGES = [f"container-{VERSION}.npk", f"rose-storage-{VERSION}.npk"]
SEVEN_ZIP = r"C:\Program Files\7-Zip\7z.exe"


def log(message):
    line = f"{time.strftime('%H:%M:%S')} {message}"
    print(line, flush=True)
    with open(LOG, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def image_tool(command, *paths):
    """Run img.sh as root: sudo on Linux, a privileged container on Windows."""
    if WINDOWS:
        def mapped(path):
            path = Path(path).resolve()
            for host, guest in ((ROOT.resolve(), "/b"), (TOOLS, "/t")):
                if path.is_relative_to(host):
                    return f"{guest}/{path.relative_to(host).as_posix()}"
            raise ValueError(f"{path} is outside the build root and the tools directory")

        cmd = ["docker", "run", "--rm", "--privileged", "-v", f"{ROOT}:/b", "-v", f"{TOOLS}:/t",
               "alpine:3.20", "sh", "/t/img.sh", command, *map(mapped, paths)]
    else:
        cmd = ["sudo", "sh", str(TOOLS / "img.sh"), command, *map(str, paths)]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"img.sh {command} failed:\n{result.stdout}\n{result.stderr}")
    return result.stdout.strip()


def prepare():
    """Download the stock image and the two packages if they are missing."""
    if STOCK.exists() and all((PKG_DIR / p).exists() for p in PACKAGES):
        return
    downloads = ROOT / "dl"
    downloads.mkdir(parents=True, exist_ok=True)
    PKG_DIR.mkdir(parents=True, exist_ok=True)
    base = f"https://download.mikrotik.com/routeros/{VERSION}"
    image_zip = downloads / f"chr-{VERSION}.img.zip"
    packages_zip = downloads / f"all_packages-x86-{VERSION}.zip"
    for target in (image_zip, packages_zip):
        if not target.exists():
            subprocess.run(["curl", "-sfL", "--retry", "3", "-o", str(target), f"{base}/{target.name}"],
                           check=True)
    if WINDOWS:
        subprocess.run([SEVEN_ZIP, "e", str(image_zip), f"-o{WORK}", "-y"], check=True, capture_output=True)
        subprocess.run([SEVEN_ZIP, "e", str(packages_zip), f"-o{PKG_DIR}", *PACKAGES, "-y"],
                       check=True, capture_output=True)
    else:
        subprocess.run(["unzip", "-o", "-j", "-q", str(image_zip), "-d", str(WORK)], check=True)
        subprocess.run(["unzip", "-o", "-j", "-q", str(packages_zip), *PACKAGES, "-d", str(PKG_DIR)],
                       check=True)
    log(f"downloaded the stock CHR {VERSION} and its packages")


class PackageServer:
    """Serves the .npk files and records every path the guest requests."""

    def __init__(self):
        self.requests = []
        outer = self

        class Handler(http.server.SimpleHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                outer.requests.append(self.path)
                if self.path.startswith("/marker/"):
                    self.send_response(200)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                super().do_GET()

        handler = functools.partial(Handler, directory=str(PKG_DIR))
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def wait_for(self, path, timeout):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if path in self.requests:
                return True
            time.sleep(1)
        return False

    def close(self):
        self.server.shutdown()


def install_and_request_device_mode():
    stage2_file = WORK / "build-stage2.rsc"
    stage2_file.write_text((TOOLS / "stage2.rsc").read_text(encoding="utf-8").replace("{PORT}", str(PORT)),
                           encoding="utf-8", newline="\n")
    script = (TOOLS / "stage1.scr.tmpl").read_text(encoding="utf-8")
    script = script.replace("{PACKAGES}", "{" + ";".join(f'"{p}"' for p in PACKAGES) + "}")
    script = script.replace("{PORT}", str(PORT))
    stage1_file = WORK / "stage1.scr"
    stage1_file.write_text(script, encoding="utf-8", newline="\n")
    log(image_tool("inject", IMAGE, stage1_file, stage2_file))

    server = PackageServer()
    proc = vm.start(IMAGE, nic=True, log=WORK / "qemu.log")
    try:
        if not server.wait_for("/marker/rebooting", 300):
            raise RuntimeError(f"stage 1 never finished; guest requested: {server.requests}")
        for pkg in PACKAGES:
            if f"/{pkg}" not in server.requests:
                raise RuntimeError(f"{pkg} was never fetched; guest requested: {server.requests}")
        log("packages fetched by the guest; clean reboot to install them")
        if not server.wait_for("/marker/device-mode-pending", 300):
            raise RuntimeError(f"stage 2 never reached device-mode; guest requested: {server.requests}")
        errors = [r for r in server.requests if r.startswith("/marker/error-")]
        if errors:
            raise RuntimeError(f"the build scripts reported errors: {errors}")
        installed = [r.rsplit("installed-", 1)[1] for r in server.requests if "/marker/installed-" in r]
        log("installed after reboot: " + ", ".join(installed))
        time.sleep(10)
        vm.monitor("quit")
        vm.wait_exit(proc, 20)
        log("device-mode requested; power cut to confirm it")
    finally:
        server.close()
        if proc.poll() is None:
            proc.kill()


def reset_without_nic():
    """Boot without a NIC until a boot stays up; returns the running QEMU."""
    reboots = 0
    for boot in range(1, 7):
        proc = vm.start(IMAGE, nic=False, log=WORK / "qemu.log", extra=["-no-reboot"])
        con = vm.Serial(transcript=TRANSCRIPT)
        started = time.time()
        login_seen = None
        while True:
            if proc.poll() is not None:
                reboots += 1
                log(f"boot {boot}: guest rebooted after {time.time() - started:.0f}s")
                break
            try:
                if login_seen is None and "Login:" in con.drain(2):
                    login_seen = time.time()
                    log(f"boot {boot}: login prompt after {login_seen - started:.0f}s")
            except ConnectionError:
                pass
            if login_seen and time.time() - login_seen > 75:
                log(f"boot {boot}: stayed up; this is the clean boot")
                con.close()
                if reboots == 0:
                    raise RuntimeError("the scheduled reset never rebooted the guest")
                return proc
            if time.time() - started > 300:
                raise TimeoutError(f"boot {boot}: neither rebooted nor reached a login prompt")
        con.close()
    raise RuntimeError("the guest kept rebooting")


def power_off(proc):
    vm.monitor("system_powerdown")
    if vm.wait_exit(proc, 120):
        log("powered off through ACPI")
        return
    log("ACPI power-off timed out; forcing QEMU to quit")
    vm.monitor("quit")
    vm.wait_exit(proc, 15)


def verify():
    shutil.copyfile(IMAGE, VERIFY_COPY)
    log(image_tool("inject", VERIFY_COPY, TOOLS / "verify.scr"))
    proc = vm.start(VERIFY_COPY, nic=True, log=WORK / "qemu.log")
    if not vm.wait_exit(proc, 300):
        vm.monitor("quit")
        vm.wait_exit(proc, 15)
        raise TimeoutError("verification boot did not shut itself down")
    for name, image in (("verify", VERIFY_COPY), ("final-state", IMAGE)):
        target = WORK / name
        shutil.rmtree(target, ignore_errors=True)
        log(image_tool("collect", image, target))
    VERIFY_COPY.unlink()


def check_results():
    """Fail the build unless the image is exactly what it claims to be."""
    verify_dir, final_dir = WORK / "verify", WORK / "final-state"

    def read(path):
        return path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""

    def rows(text):
        return [line for line in text.splitlines() if re.match(r"\s*\d+\s", line)]

    packages = read(verify_dir / "verify-packages.txt")
    mode = read(verify_dir / "verify-device-mode.txt")
    checks = {
        "routeros, container and rose-storage installed": all(
            re.search(rf"\b{name}\s+{re.escape(VERSION)}\b", packages)
            for name in ("routeros", "container", "rose-storage")
        ),
        "device-mode advanced": bool(re.search(r"\bmode:\s*advanced\b", mode)),
        "device-mode container=yes": bool(re.search(r"\bcontainer:\s*yes\b", mode)),
        "device-mode traffic-gen=yes": bool(re.search(r"\btraffic-gen:\s*yes\b", mode)),
        "first NIC comes up as ether1": bool(re.search(r"\bether1\b", read(verify_dir / "verify-interfaces.txt"))),
        "free license": bool(re.search(r"\blevel:\s*free\b", read(verify_dir / "verify-license.txt"))),
        "only the default admin user": [l.split()[-3] for l in rows(read(verify_dir / "verify-users.txt"))] == ["admin"],
        "no schedulers": not rows(read(verify_dir / "verify-scheduler.txt")),
        "no autorun left in rw": read(final_dir / "autorun-state.txt").strip() == "absent",
        "stock autorun in rw/store": read(final_dir / "store-autorun.scr").strip() == "",
        "rw/disk holds nothing but skins": [
            line.split()[-1] for line in read(final_dir / "disk-listing.txt").splitlines()[1:]
            if line.split() and line.split()[-1] not in (".", "..")
        ] == ["skins"],
    }
    summary = [f"- {'✅' if ok else '❌'} {name}" for name, ok in checks.items()]
    (WORK / "summary.md").write_text("\n".join(summary) + "\n", encoding="utf-8", newline="\n")
    for name, ok in checks.items():
        log(f"[{'ok' if ok else 'FAIL'}] {name}")
    failed = [name for name, ok in checks.items() if not ok]
    if failed:
        raise RuntimeError("verification failed: " + "; ".join(failed))
    log("verification passed")


def main():
    WORK.mkdir(parents=True, exist_ok=True)
    for path in (LOG, TRANSCRIPT):
        path.unlink(missing_ok=True)
    log(f"building CHR {VERSION} with QEMU acceleration '{vm.ACCEL_NAME}'")
    prepare()
    shutil.copyfile(STOCK, IMAGE)
    install_and_request_device_mode()
    power_off(reset_without_nic())
    log(image_tool("scrub", IMAGE, STOCK))
    digest = hashlib.sha256(IMAGE.read_bytes()).hexdigest()
    log(f"image ready: {IMAGE.name} ({IMAGE.stat().st_size} bytes) sha256={digest}")
    verify()
    check_results()
    OUT.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(IMAGE, OUT / IMAGE.name)
    (OUT / f"{IMAGE.name}.sha256").write_text(f"{digest}  {IMAGE.name}\n", encoding="ascii", newline="\n")
    log(f"done: {OUT / IMAGE.name}")


if __name__ == "__main__":
    main()
