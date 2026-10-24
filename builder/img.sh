#!/bin/sh
# Work on partition 2 (the RouterOS writable partition) of a CHR raw image.
# Needs root for loop mounts: run with sudo on Linux, or inside a privileged
# container on Windows (build.py picks the right way).
#
#   img.sh inject  <image> <autorun> [<file for rw/disk>]
#   img.sh collect <image> <outdir>
#   img.sh scrub   <image> <stock image>
#   img.sh uefi    <image> <uefi image>
set -eu

# The Alpine container used on Windows starts without these tools.
if ! command -v sgdisk >/dev/null 2>&1 && command -v apk >/dev/null 2>&1; then
  apk add -q --no-progress util-linux gptfdisk dosfstools >/dev/null 2>&1
fi

# part_field <image> <partition number> <start|size>, in sectors.
part_field() {
  value=$(sfdisk -d "$1" | awk -F'[=,]' -v p="img$2" -v f="$3" '
    index($0, p " ") || index($0, p ":") {
      for (i = 1; i <= NF; i++) if ($i ~ f) { gsub(/ /, "", $(i+1)); print $(i+1); exit }
    }')
  [ -n "$value" ] || { echo "cannot find partition $2 in $1" >&2; exit 1; }
  echo "$value"
}

part2_offset() {
  echo $(($(part_field "$1" 2 start) * 512))
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
  uefi)
    # UEFI firmware reads only FAT, and the CHR boot partition (partition 1,
    # holding EFI/BOOT/BOOTX64.EFI) is ext2.  The copy gets partition 1
    # reformatted as FAT16 with the same files, and the table rebuilt as GPT
    # with a hybrid MBR, partition 1 typed EF00.  Partition 2 is not touched.
    # The new boot sector replaces the BIOS loader of partition 1, so the
    # result boots under UEFI only.
    cp "$image" "$3"
    start=$(part_field "$3" 1 start)
    size=$(part_field "$3" 1 size)
    p1=$(mktemp -d)
    files=$(mktemp -d)
    loop=$(losetup -f --show -o $((start * 512)) --sizelimit $((size * 512)) "$3")
    mount -o ro "$loop" "$p1"
    cp -a "$p1/." "$files/"
    rm -rf "$files/lost+found"
    umount "$p1"
    mkfs.vfat -F 16 -n ROUTEROS "$loop" >/dev/null
    mount "$loop" "$p1"
    cp -r "$files/." "$p1/"
    sync
    umount "$p1"
    losetup -d "$loop"
    rm -rf "$p1" "$files"
    # The keystrokes of the "jaclaz" layout from tikoci/fat-chr: a fresh GPT
    # from the MBR with the backup structures at the end, the legacy-bootable
    # attribute on partition 1, EF00 "RouterOS Boot" and "RouterOS", then a
    # hybrid MBR carrying both partitions as type 83 with partition 1 active.
    printf '%s\n' 2 x e r f y x a 1 2 '' m t 1 EF00 c 1 'RouterOS Boot' c 2 RouterOS \
      x r h '1 2' n 83 y 83 n n w y | gdisk "$3" >/dev/null
    sgdisk -v "$3" | tail -n 1
    echo "UEFI copy written to $(basename "$3")"
    ;;
  *)
    echo "unknown command $cmd" >&2
    exit 2
    ;;
esac
