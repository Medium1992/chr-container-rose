#!/bin/sh
# Work on partition 2 (the RouterOS writable partition) of a CHR raw image.
# Needs root for loop mounts: run with sudo on Linux, or inside a privileged
# container on Windows (build.py picks the right way).
#
#   img.sh inject  <image> <autorun> [<file for rw/disk>]
#   img.sh collect <image> <outdir>
#   img.sh scrub   <image> <stock image>
set -eu

# The Alpine container used on Windows starts without sfdisk.
if ! command -v sfdisk >/dev/null 2>&1 && command -v apk >/dev/null 2>&1; then
  apk add -q --no-progress util-linux >/dev/null 2>&1
fi

part2_offset() {
  start=$(sfdisk -d "$1" | awk -F'[=,]' '/img2 /||/img2:/{for(i=1;i<=NF;i++) if($i ~ /start/){gsub(/ /,"",$(i+1)); print $(i+1); exit}}')
  [ -n "$start" ] || { echo "cannot find partition 2 in $1" >&2; exit 1; }
  echo $((start * 512))
}

cmd=$1
image=$2
offset=$(part2_offset "$image")
p2=$(mktemp -d)
trap 'mountpoint -q "$p2" && umount "$p2"; rmdir "$p2" 2>/dev/null || true' EXIT

case "$cmd" in
  inject)
    mount -o loop,offset="$offset" "$image" "$p2"
    install -m 0600 "$3" "$p2/rw/autorun.scr"
    if [ $# -ge 4 ]; then
      mkdir -p "$p2/rw/disk"
      install -m 0666 "$4" "$p2/rw/disk/$(basename "$4")"
    fi
    sync
    umount "$p2"
    echo "injected $(basename "$3")${4:+ and $(basename "$4")} into $(basename "$image")"
    ;;
  collect)
    mount -o loop,ro,noload,offset="$offset" "$image" "$p2"
    mkdir -p "$3"
    (cd "$p2" && find rw -maxdepth 2 | sort) > "$3/tree.txt"
    for f in "$p2"/rw/disk/verify-*; do [ -e "$f" ] && cp "$f" "$3/"; done
    ls -la "$p2/rw/disk" > "$3/disk-listing.txt" 2>&1 || true
    if [ -e "$p2/rw/autorun.scr" ]; then echo present; else echo absent; fi > "$3/autorun-state.txt"
    if [ -e "$p2/rw/store/autorun.scr" ]; then cp "$p2/rw/store/autorun.scr" "$3/store-autorun.scr"; fi
    umount "$p2"
    # Hand the files back to the user who ran sudo.
    if [ -n "${SUDO_UID:-}" ]; then chown -R "$SUDO_UID:${SUDO_GID:-$SUDO_UID}" "$3"; fi
    echo "collected into $3"
    ;;
  scrub)
    # RouterOS keeps the executed autorun in rw/store; put back the stock
    # (empty) one so no build script remains in the finished image.
    stock=$(mktemp -d)
    mount -o loop,ro,offset="$(part2_offset "$3")" "$3" "$stock"
    mount -o loop,offset="$offset" "$image" "$p2"
    if [ -e "$p2/rw/store/autorun.scr" ]; then
      cp "$stock/rw/autorun.scr" "$p2/rw/store/autorun.scr"
      echo "rw/store/autorun.scr restored to the stock content"
    else
      echo "no rw/store/autorun.scr to restore"
    fi
    sync
    umount "$p2" "$stock"
    rmdir "$stock"
    ;;
  *)
    echo "unknown command $cmd" >&2
    exit 2
    ;;
esac
