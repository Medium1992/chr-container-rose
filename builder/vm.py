"""Run a RouterOS CHR guest in QEMU: serial console output and the monitor.

The CHR serial login cannot be driven by typing (any input makes it redraw the
login screen), so the build only reads the console to notice login prompts and
steers the guest through injected scripts instead.
"""

import os
import re
import socket
import subprocess
import time

SERIAL_PORT = 5555
MONITOR_PORT = 5556
ANSI = re.compile(rb"\x1b\[[0-9;?]*[A-Za-z]|\x1b[()][A-Z0-9]|\x1b[cZ78=>]|\r")

if os.name == "nt":
    DEFAULT_QEMU = r"D:\chr-build\qemu\qemu-system-x86_64.exe"
else:
    DEFAULT_QEMU = "qemu-system-x86_64"
QEMU = os.environ.get("QEMU", DEFAULT_QEMU)

ACCEL_MODES = {
    # GitHub-hosted Linux runners and Proxmox-like hosts.
    "kvm": ["-accel", "kvm", "-cpu", "host"],
    # Portable fallback.  On Windows it is the only reliable choice: under
    # WHPX the RouterOS processes crash and leave autosupout files behind.
    "tcg": ["-accel", "tcg", "-cpu", "qemu64"],
    "whpx": ["-accel", "whpx", "-accel", "tcg"],
}


def default_accel():
    if os.name != "nt" and os.access("/dev/kvm", os.R_OK | os.W_OK):
        return "kvm"
    return "tcg"


ACCEL_NAME = os.environ.get("CHR_ACCEL") or default_accel()
ACCEL = ACCEL_MODES[ACCEL_NAME]


def start(image, nic=True, log=None, extra=()):
    args = [
        QEMU,
        *ACCEL,
        "-m", "512",
        "-drive", f"file={image},format=raw,if=ide",
        "-display", "none",
        "-serial", f"tcp:127.0.0.1:{SERIAL_PORT},server=on,wait=off",
        "-monitor", f"tcp:127.0.0.1:{MONITOR_PORT},server=on,wait=off",
    ]
    if nic:
        # 10.0.2.2 inside the guest reaches the build host (package server).
        args += ["-nic", "user,model=virtio-net-pci"]
    else:
        args += ["-nic", "none"]
    args += list(extra)
    out = open(log, "ab") if log else subprocess.DEVNULL
    proc = subprocess.Popen(args, stdout=out, stderr=out)
    time.sleep(2)
    if proc.poll() is not None:
        raise RuntimeError(f"QEMU exited immediately with code {proc.returncode}")
    return proc


class Serial:
    """Read-only view of the guest serial console."""

    def __init__(self, transcript=None):
        deadline = time.time() + 30
        while True:
            try:
                self.sock = socket.create_connection(("127.0.0.1", SERIAL_PORT), timeout=5)
                break
            except OSError:
                if time.time() > deadline:
                    raise
                time.sleep(0.5)
        self.sock.settimeout(0.5)
        self.buffer = b""
        self.pos = 0
        self.transcript = open(transcript, "ab") if transcript else None

    def _pump(self):
        try:
            chunk = self.sock.recv(65536)
        except socket.timeout:
            return False
        if not chunk:
            raise ConnectionError("serial console closed")
        if self.transcript:
            self.transcript.write(chunk)
            self.transcript.flush()
        self.buffer += chunk
        return True

    def text(self):
        return ANSI.sub(b"", self.buffer).decode("utf-8", "replace")

    def drain(self, seconds=1.0):
        """Collect everything printed during `seconds` and mark it consumed."""
        deadline = time.time() + seconds
        while time.time() < deadline:
            self._pump()
        text = self.text()
        out = text[self.pos:]
        self.pos = len(text)
        return out

    def close(self):
        self.sock.close()
        if self.transcript:
            self.transcript.close()


def monitor(command):
    with socket.create_connection(("127.0.0.1", MONITOR_PORT), timeout=10) as sock:
        sock.settimeout(2)
        time.sleep(0.5)
        try:
            sock.recv(65536)
        except socket.timeout:
            pass
        sock.sendall((command + "\n").encode())
        time.sleep(1)
        try:
            return sock.recv(65536).decode("utf-8", "replace")
        except (socket.timeout, ConnectionError):
            return ""


def wait_exit(proc, timeout):
    try:
        proc.wait(timeout=timeout)
        return True
    except subprocess.TimeoutExpired:
        return False
