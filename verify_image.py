#!/usr/bin/env python
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 hutchx86
"""Read-only check of an image or USB disk built by udm_usb_builder.py.
usage: verify_image.py FILE|--disk N|/dev/sdX [scratch_dir]   (a real disk needs Administrator / root)
Parses the GPT, cuts out each ext4 partition, runs e2fsck -fn, and compares dumped files to the confirmed-firmware hashes."""
from __future__ import print_function
import hashlib
import os
import struct
import subprocess
import sys

import udm_usb_builder as u

S, MIB = u.SECTOR, u.MIB
if len(sys.argv) < 2:
    sys.exit(__doc__)
if sys.argv[1] == "--disk":
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    sel, rest = sys.argv[2], sys.argv[3:]
    if not u.IS_WINDOWS and not sel.startswith("/"):
        sel = "/dev/" + sel
    match = [d for d in u.list_disks() if str(d["id"]) == str(sel)]
    if not match or match[0]["bus"] != "USB":
        sys.exit("%s does not exist or is not a USB disk" % sel)
    print(u.describe(match[0]))
    img, total_bytes = match[0]["dev"], match[0]["size"]
else:
    img, rest = sys.argv[1], sys.argv[2:]
    total_bytes = os.path.getsize(img)
try:
    f = open(img, "rb", 0)
except (IOError, OSError) as e:
    sys.exit("cannot open %s (%s). Raw disk reads need an Administrator prompt." % (img, e))
here = os.path.dirname(os.path.abspath(__file__))
w = os.path.abspath(rest[0] if rest else os.path.join(here, "verify_work"))
if not os.path.isdir(w):
    os.makedirs(w)
t = u.find_tools(os.path.join(here, "tools"))
if not t:
    sys.exit("e2fsprogs not found (run the builder once with --install-tools)")
env = u.tool_env(t["bindir"])
n = total_bytes // S
failed = []


def check(label, good):
    if not good:
        failed.append(label)
    print("%-52s %s" % (label, "ok" if good else "FAIL"))


def read_at(off, size):
    """Sector-aligned read (raw disks need it); returns exactly `size` bytes."""
    start = off - off % S
    want = (off - start + size + S - 1) // S * S
    f.seek(start)
    data = b""
    while len(data) < want:
        b = f.read(want - len(data))
        if not b:
            break
        data += b
    return data[off - start:off - start + size]


def header_ok(h):
    return u.crc32(h[:16] + b"\0\0\0\0" + h[20:92]) == struct.unpack("<I", h[16:20])[0]


mbr, h, ent = read_at(0, S), read_at(S, S), read_at(2 * S, 128 * 128)
bh, bent = read_at((n - 1) * S, S), read_at((n - 33) * S, 128 * 128)
check("protective MBR signature", mbr[510:512] == b"\x55\xaa" and mbr[450:451] == b"\xee")
check("primary header signature and CRC", h[:8] == b"EFI PART" and header_ok(h))
check("primary entries CRC", u.crc32(ent) == struct.unpack("<I", h[88:92])[0])
check("backup header signature and CRC", bh[:8] == b"EFI PART" and header_ok(bh))
check("backup entries == primary entries", bent == ent)
check("disk GUID", h[56:72] == u.DISK_GUID.bytes_le)
parts = []
for i in range(6):
    e = ent[i * 128:(i + 1) * 128]
    first, last = struct.unpack("<QQ", e[32:48])
    parts.append((i + 1, e[56:128].decode("utf-16-le").rstrip(u"\0"), first, last))
    check("partition %d %-10s %9d-%-9d" % parts[i], parts[i] == u.PARTS[i])


def tool(args):
    p = subprocess.Popen(args, cwd=w, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    return p.communicate()[0].decode("latin1"), p.returncode


def cut(num):
    """Leaves all-zero blocks as holes."""
    _, _, first, last = parts[num - 1]
    size = (last - first + 1) * S
    p = os.path.join(w, "x%d.img" % num)
    u.make_sparse(p, size)
    with open(p, "r+b") as o:
        for pos in range(0, size, MIB):
            b = read_at(first * S + pos, min(MIB, size - pos))
            if b != u.ZERO[:len(b)]:
                o.seek(pos)
                o.write(b)


for num in (1, 3, 4, 5, 6):
    cut(num)
    check("e2fsck -fn partition %d (%s)" % (num, parts[num - 1][1]), tool([t["e2fsck"], "-fn", "x%d.img" % num])[1] == 0)


def dump(num, path, out):
    tool([t["debugfs"], "-R", "dump /%s %s" % (path, out), "x%d.img" % num])
    p = os.path.join(w, out)
    return u.hash_file(p) if os.path.exists(p) else None


boot_uimage = dump(1, "uImage", "d1_uImage")
check("boot/uImage present", boot_uimage is not None)
ver = next((v for v, h in u.KNOWN_FW.items() if h["uImage"] == boot_uimage), None) if boot_uimage else None
if ver:
    print("  firmware: %s (confirmed)" % ver)
    c = u.KNOWN_FW[ver]
    for label, num, path, want in (("boot/uImage", 1, "uImage", c["uImage"]),
                                   ("boot/uImage.bkp", 1, "uImage.bkp", c["uImage"]),
                                   ("root/rootfs", 3, "rootfs", c["rootfs"]),
                                   ("root/rootfs.bkp", 3, "rootfs.bkp", c["rootfs"])):
        check("%-15s == confirmed hash" % label, dump(num, path, "d%d_%s" % (num, path)) == want)
else:
    print("  firmware: not in the confirmed list (%s); payload hashes not checked"
          % ", ".join(sorted(u.KNOWN_FW)))
check("persistent shim script == working unit",
      dump(5, "udm-boot-shim/udm-boot-links.sh", "shim.sh") == u.SHIM_SCRIPT_SHA256)
check("overlay shim unit == working unit",
      dump(6, "data/etc/systemd/system/udm-boot-links.service", "shim.service") == u.SHIM_UNIT_SHA256)
check("overlay enable symlink present", "Type: symlink" in tool(
    [t["debugfs"], "-R", "stat /data/etc/systemd/system/multi-user.target.wants/udm-boot-links.service", "x6.img"])[0])
rec = parts[1][2] * S
if os.path.exists(os.path.join(w, "d1_uImage")):
    with open(os.path.join(w, "d1_uImage"), "rb") as fh:
        ui = fh.read()
    check("recovery == firmware kernel FIT",
          hashlib.sha256(read_at(rec, len(ui))).hexdigest() == hashlib.sha256(ui).hexdigest())
else:
    check("recovery is a FIT image", read_at(rec, 4) == b"\xd0\x0d\xfe\xed")
check("sectors 34..2047 (before partition 1) are zero", read_at(34 * S, (2048 - 34) * S) == b"\0" * ((2048 - 34) * S))

print("\nVERIFY %s" % ("FAILED: " + "; ".join(failed) if failed else "PASSED"))
sys.exit(1 if failed else 0)
