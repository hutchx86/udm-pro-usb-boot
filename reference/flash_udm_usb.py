#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 hutchx86
"""
flash_udm_usb.py — earlier Linux-only implementation of the UDM Pro USB-stick builder.

Superseded by udm_usb_builder.py (Windows + Linux, builds the GPT itself and verifies the
result by read-back). Kept as a reference for the original Linux approach.
"""
import argparse
import os
import struct
import subprocess
import sys
import tempfile
import time

PAD_TO = 4096

# (partition number, start sector, end sector, name); end sector 0 = rest of disk
PARTITIONS = [
    (1, 2048, 133119, "boot"),
    (2, 133120, 198655, "recovery"),
    (3, 198656, 4392959, "root"),
    (4, 4392960, 6490111, "log"),
    (5, 6490112, 10684415, "persistent"),
    (6, 10684416, 0, "overlay"),
]

SHIM_DIR = "udm-boot-shim"
SHIM_SCRIPT_NAME = "udm-boot-links.sh"
SHIM_UNIT_NAME = "udm-boot-links.service"

SHIM_SCRIPT = """#!/bin/sh
# Creates /dev/boot and /dev/boot1..6 compatibility links for a UDM Pro whose
# internal storage is not a "Generic" USB card reader. The stock kernel only
# names the disk "boot" when the SCSI vendor starts with "Generic"; the
# firmware updater hard-codes /dev/boot1 and /dev/boot3.
# Lives on /persistent so it survives firmware updates.

# Never touch a real kernel-provided block node (stock hardware).
if [ -b /dev/boot ] && [ ! -L /dev/boot ]; then
\texit 0
fi

n=0
while [ ! -e /dev/disk/by-partlabel/boot ] && [ "$n" -lt 60 ]; do
\tsleep 1
\tn=$((n + 1))
done
[ -e /dev/disk/by-partlabel/boot ] || { echo "no 'boot' partition found" >&2; exit 1; }

bootpart=$(readlink -f /dev/disk/by-partlabel/boot)
disk=$(lsblk -no PKNAME "$bootpart" | head -n 1)
[ -n "$disk" ] || { echo "cannot determine parent disk of $bootpart" >&2; exit 1; }

ln -sfn "$disk" /dev/boot

i=1
for label in boot recovery root log persistent overlay; do
\tp=$(readlink -f "/dev/disk/by-partlabel/$label" 2>/dev/null)
\tif [ -b "$p" ] && ! { [ -e "/dev/boot$i" ] && [ ! -L "/dev/boot$i" ]; }; then
\t\tln -sfn "$(basename "$p")" "/dev/boot$i"
\tfi
\ti=$((i + 1))
done
"""

SHIM_UNIT = f"""[Unit]
Description=Create /dev/boot* compatibility links for the firmware updater
After=local-fs.target systemd-udevd.service
ConditionPathExists=/persistent/{SHIM_DIR}/{SHIM_SCRIPT_NAME}

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/bin/sh /persistent/{SHIM_DIR}/{SHIM_SCRIPT_NAME}

[Install]
WantedBy=multi-user.target
"""


def run(cmd, **kw):
    print("+", " ".join(cmd))
    subprocess.run(cmd, check=True, **kw)


def part_path(device, num):
    # /dev/sdX -> /dev/sdX1 ; /dev/nvme0n1 -> /dev/nvme0n1p1
    return device + (f"p{num}" if device[-1].isdigit() else str(num))


def rescan_partitions(device):
    """Make the kernel re-read the new table and wait for the partition nodes.
    partprobe is optional (not installed everywhere), so fall back to blockdev."""
    for cmd in (["partprobe", device], ["blockdev", "--rereadpt", device]):
        try:
            if subprocess.run(cmd, capture_output=True).returncode == 0:
                break
        except FileNotFoundError:
            continue
    for tool in (["udevadm", "settle"],):
        try:
            subprocess.run(tool, capture_output=True, timeout=20)
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
    last = part_path(device, PARTITIONS[-1][0])
    for _ in range(30):
        if os.path.exists(last):
            return
        time.sleep(0.5)
    sys.exit(f"partition node {last} did not appear after writing the partition table")


def ensure_not_mounted(device):
    if not os.path.exists(device):
        sys.exit(f"{device} does not exist")
    candidates = [device] + [part_path(device, n) for n, *_ in PARTITIONS]
    for p in candidates:
        if not os.path.exists(p):
            continue
        r = subprocess.run(["findmnt", p], capture_output=True, text=True)
        if r.returncode == 0:
            sys.exit(f"{p} is currently mounted — unmount it before running this script")


def find_fit_blobs(data):
    """All FDT/FIT magic occurrences with a plausible (in-bounds) totalsize."""
    blobs = []
    start = 0
    magic = b"\xd0\x0d\xfe\xed"
    while True:
        idx = data.find(magic, start)
        if idx == -1:
            break
        if idx + 8 <= len(data):
            totalsize = struct.unpack_from(">I", data, idx + 4)[0]
            if 0 < totalsize <= len(data) - idx:
                blobs.append((idx, totalsize))
        start = idx + 1
    return blobs


def find_squashfs(data):
    """(offset, bytes_used) of the squashfs image with the largest bytes_used
    (the real rootfs, as opposed to any small stray squashfs elsewhere)."""
    best = None
    start = 0
    while True:
        idx = data.find(b"hsqs", start)
        if idx == -1:
            break
        if idx + 48 <= len(data):
            bytes_used = struct.unpack_from("<Q", data, idx + 40)[0]
            if 0 < bytes_used <= len(data) - idx:
                if best is None or bytes_used > best[1]:
                    best = (idx, bytes_used)
        start = idx + 1
    return best


def find_kernel_fit_before(blobs, squashfs_offset):
    """The FIT blob whose end lands closest to (at or before) squashfs_offset —
    that's the kernel+initramfs FIT paired with this rootfs."""
    candidates = [(off, size) for off, size in blobs if off + size <= squashfs_offset]
    if not candidates:
        return None
    candidates.sort(key=lambda t: squashfs_offset - (t[0] + t[1]))
    return candidates[0]


def extract_padded(data, offset, length, pad_to=PAD_TO):
    end = offset + length
    padded_len = ((length + pad_to - 1) // pad_to) * pad_to
    chunk = bytearray(data[offset:end])
    chunk.extend(b"\x00" * (padded_len - length))
    return bytes(chunk)


def write_file_into_partition(part, filename_to_content):
    with tempfile.TemporaryDirectory() as md:
        run(["mount", part, md])
        try:
            for name, content in filename_to_content.items():
                path = os.path.join(md, name)
                print(f"  writing {path} ({len(content)} bytes)")
                with open(path, "wb") as f:
                    f.write(content)
        finally:
            run(["sync"])
            run(["umount", md])


def install_shim(device):
    """Put the /dev/boot* shim script on `persistent` and its enabled systemd
    unit in the `overlay` upper layer (data/etc/systemd/system)."""
    # script -> persistent partition (survives firmware updates)
    persistent = part_path(device, 5)
    with tempfile.TemporaryDirectory() as md:
        run(["mount", persistent, md])
        try:
            d = os.path.join(md, SHIM_DIR)
            os.makedirs(d, exist_ok=True)
            path = os.path.join(d, SHIM_SCRIPT_NAME)
            print(f"  writing {path}")
            with open(path, "w") as f:
                f.write(SHIM_SCRIPT)
            os.chmod(path, 0o755)
        finally:
            run(["sync"])
            run(["umount", md])

    # unit + enable symlink -> overlay upper dir. The initramfs mounts the
    # overlay with upperdir=<overlay>/data and workdir=<overlay>/.workdir and
    # creates both itself if missing, so pre-seeding them is safe.
    overlay = part_path(device, 6)
    with tempfile.TemporaryDirectory() as md:
        run(["mount", overlay, md])
        try:
            os.makedirs(os.path.join(md, ".workdir"), exist_ok=True)
            sysd = os.path.join(md, "data", "etc", "systemd", "system")
            wants = os.path.join(sysd, "multi-user.target.wants")
            os.makedirs(wants, exist_ok=True)
            unit = os.path.join(sysd, SHIM_UNIT_NAME)
            print(f"  writing {unit}")
            with open(unit, "w") as f:
                f.write(SHIM_UNIT)
            link = os.path.join(wants, SHIM_UNIT_NAME)
            if os.path.lexists(link):
                os.remove(link)
            os.symlink(f"/etc/systemd/system/{SHIM_UNIT_NAME}", link)
            print(f"  enabled via {link}")
        finally:
            run(["sync"])
            run(["umount", md])


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("device", help="target USB device, e.g. /dev/sdX (whole disk)")
    ap.add_argument("firmware", help="Ubiquiti firmware .bin package")
    ap.add_argument(
        "--recovery-image",
        help="optional raw recovery-partition dump to write to partition 2",
    )
    ap.add_argument(
        "--no-shim",
        action="store_true",
        help="do not install the /dev/boot* firmware-update shim (only useful if the "
        "device's SCSI vendor string already starts with 'Generic')",
    )
    ap.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    args = ap.parse_args()

    if os.geteuid() != 0:
        sys.exit("must run as root")

    device = args.device
    if not os.path.exists(device):
        sys.exit(f"{device} does not exist")
    dev_type = subprocess.run(
        ["lsblk", "-ndo", "TYPE", device], capture_output=True, text=True
    ).stdout.split()
    if not dev_type or dev_type[0] not in ("disk", "loop"):
        sys.exit(
            f"{device} is a {dev_type[0] if dev_type else 'unknown device'}; "
            "pass the whole disk (e.g. /dev/sdX or /dev/mmcblk0), not a partition"
        )

    ensure_not_mounted(device)

    print(f"Reading firmware image {args.firmware} ...")
    with open(args.firmware, "rb") as f:
        data = f.read()

    squashfs = find_squashfs(data)
    if squashfs is None:
        sys.exit("could not find a squashfs (rootfs) image inside the firmware file")
    sq_off, sq_bytes_used = squashfs

    fit_blobs = find_fit_blobs(data)
    kernel = find_kernel_fit_before(fit_blobs, sq_off)
    if kernel is None:
        sys.exit("could not find a kernel FIT image preceding the squashfs rootfs")
    kern_off, kern_size = kernel

    print(f"Found kernel FIT   at offset {kern_off:>12} size {kern_size:>12} bytes")
    print(f"Found squashfs     at offset {sq_off:>12} bytes_used {sq_bytes_used:>12} bytes")

    kernel_blob = data[kern_off : kern_off + kern_size]  # FIT images are used as-is, no padding needed
    rootfs_blob = extract_padded(data, sq_off, sq_bytes_used)
    pad_added = len(rootfs_blob) - sq_bytes_used
    print(f"Padded rootfs to {len(rootfs_blob)} bytes (+{pad_added} bytes padding to a {PAD_TO}-byte boundary)")

    print(f"\nPlanned partition layout on {device}:")
    for num, start, end, name in PARTITIONS:
        print(f"  {num}: {name:<10} sectors {start}-{end if end else '(end of disk)'}")
    print(
        "  firmware-update shim: "
        + ("NOT installed (--no-shim)" if args.no_shim else "will be installed (persistent + overlay)")
    )
    if args.recovery_image:
        print(f"  recovery partition will be restored raw from {args.recovery_image}")
    else:
        print(
            "  ! no --recovery-image given: partition 2 (recovery) will be left as an\n"
            "    empty ext4 filesystem. The firmware .bin does not contain a recovery\n"
            "    image — only the normal boot path (uImage/rootfs) will be bootable."
        )

    if not args.yes:
        resp = input(f"\nThis will ERASE ALL DATA on {device}. Type YES to continue: ")
        if resp != "YES":
            sys.exit("aborted")

    # 1. Partition table
    run(["sgdisk", "--zap-all", device])
    cmd = ["sgdisk"]
    for num, start, end, name in PARTITIONS:
        cmd += [
            f"--new={num}:{start}:{end if end else 0}",
            f"--typecode={num}:8300",
            f"--change-name={num}:{name}",
        ]
    cmd.append(device)
    run(cmd)
    rescan_partitions(device)

    # 2. boot: ext4 + uImage / uImage.bkp
    boot = part_path(device, 1)
    run(["mkfs.ext4", "-F", "-L", "boot", boot])
    write_file_into_partition(boot, {"uImage": kernel_blob, "uImage.bkp": kernel_blob})

    # 3. recovery: raw dd from a real capture, or empty ext4
    recovery = part_path(device, 2)
    if args.recovery_image:
        run(["dd", f"if={args.recovery_image}", f"of={recovery}", "bs=4M", "status=progress", "conv=fsync"])
    else:
        run(["mkfs.ext4", "-F", "-L", "recovery", recovery])

    # 4. root: ext4 + rootfs / rootfs.bkp
    root = part_path(device, 3)
    run(["mkfs.ext4", "-F", "-L", "root", root])
    write_file_into_partition(root, {"rootfs": rootfs_blob, "rootfs.bkp": rootfs_blob})

    # 5. log / persistent / overlay: fresh ext4
    for num, label in ((4, "log"), (5, "persistent"), (6, "overlay")):
        run(["mkfs.ext4", "-F", "-L", label, part_path(device, num)])

    # 6. firmware-update shim (recreates /dev/boot* so web-UI updates work)
    if args.no_shim:
        print("Skipping firmware-update shim (--no-shim).")
    else:
        print("Installing firmware-update shim ...")
        install_shim(device)

    print(f"\nDone. {device} is ready.")


if __name__ == "__main__":
    main()
