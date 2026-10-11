#!/usr/bin/env python
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 hutchx86
"""Build a bootable UDM Pro USB stick (replaces the internal storage) from a Ubiquiti firmware .bin.

    python udm_usb_builder.py                       guided mode (Administrator on Windows, root on Linux)
    python udm_usb_builder.py --list | --disk N | --image test.img | --selftest

Steps: download the firmware and cut out the kernel FIT and squashfs rootfs; build the partition
images with e2fsprogs (a private Cygwin copy is offered on Windows if missing); write them at the stock
sector offsets, then the GPT, and read everything back; install the /dev/boot* shim.
Safety: only USB, non-system, 512-byte-sector disks of 14.6-128 GiB, and you type the disk's serial.
Runs on Windows and Linux. Writing a real disk needs Administrator on Windows or root on Linux:
--disk N (Windows) or --disk /dev/sdX (Linux). --image touches no hardware and works on either.
The layout is the stock 14.56 GiB one; the rest of a larger stick is left unallocated.
Python 3 recommended; 2.7 also works.
"""
from __future__ import print_function

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import time
import uuid
import zlib

PY3 = sys.version_info[0] >= 3
if os.name == "nt":
    PLATFORM = "windows"
elif sys.platform.startswith("linux"):
    PLATFORM = "linux"
else:
    PLATFORM = "posix"
IS_WINDOWS = PLATFORM == "windows"
IS_LINUX = PLATFORM == "linux"

if PY3:
    from urllib.request import urlopen, Request
    _raw_input = input
else:
    from urllib2 import urlopen, Request
    _raw_input = raw_input

SECTOR = 512
MIB = 1024 * 1024
ZERO = b"\0" * MIB
DEFAULT_FW = ("https://fw-download.ubnt.com/data/unifi-dream/"
              "a943-UDMPRO-6.0.11-c94c1e76-a360-4c80-a84e-dd28f5b960e7.bin")

# Tool locations to search in addition to PATH: mke2fs/debugfs/e2fsck live in an sbin dir on most
# systems, which is often not on a normal user's PATH.
if IS_WINDOWS:
    SYSTEM_DIRS = ()
else:
    SYSTEM_DIRS = ("/usr/local/sbin", "/usr/local/bin", "/usr/sbin", "/usr/bin", "/sbin", "/bin")

# (number, partlabel, first sector, last sector), from `sgdisk -p` of the working stick.
PARTS = [
    (1, "boot", 2048, 133119),
    (2, "recovery", 133120, 198655),
    (3, "root", 198656, 4392959),
    (4, "log", 4392960, 6490111),
    (5, "persistent", 6490112, 10684415),
    (6, "overlay", 10684416, 30533631),
]
MIN_DISK_SECTORS = PARTS[-1][3] + 1 + 33    # layout plus the backup GPT
STOCK_DISK_SECTORS = 30535680               # the stock internal card (image mode)
MAX_DISK_BYTES = 128 * 1024 * MIB
ZERO_HEAD = 32 * MIB                        # zero chunks are always written this far into a partition

DISK_GUID = uuid.UUID("188E7BC9-922B-49CB-AF8F-9AE29776F090")
LINUX_FS_GUID = uuid.UUID("0FC63DAF-8483-4772-8E79-3D69D8477DE4")
PART_GUIDS = {
    1: uuid.UUID("33EC3C06-C1BE-4C28-9B7A-9B2547B597AF"),
    2: uuid.UUID("7F47F4FB-19CC-4456-BFBE-ADA3B479EAFB"),
    3: uuid.UUID("82B5EC1F-5E7A-4A22-A8C9-044746D708B1"),
    4: uuid.UUID("10F839A6-C768-4F65-836B-07B8214130C5"),
    5: uuid.UUID("8493D741-9AC5-4CDF-B458-DF89B5CC395F"),
    6: uuid.UUID("87689ECF-789C-402D-9D04-1AC7105B7857"),
}

# Confirmed-working firmware: version -> sha256 of the payloads, where it came from and how it was
# confirmed. This is a record of what has been tested, NOT a lock: any other UBNT .bin builds too,
# just without hash verification (see check_payloads). Add an entry whenever a build is confirmed on
# a real UDM Pro; `--list-firmware` prints this table.
KNOWN_FW = {
    "UDMPRO.al324.v6.0.11.dd9ab55.260930.0750": {
        "uImage": "244326e87858b88fd0267cbb2b9c64577011723fab8240f05efe5bcc8318036b",
        "rootfs": "f570133c01955ce7c008b18d66cb15047956dfab90e0d03992ffb17c4deccb6f",
        "url": DEFAULT_FW,
        "notes": "verified against a live UDM Pro (sysid ea15) and the working stick",
    },
}

# The /dev/boot* shim, byte-identical to the working unit (hashed below).
SHIM_SCRIPT_SHA256 = "6def854b6ec7767b1730de65dbf13d24327830a6f571b8b5b03790529ac5f8c5"
SHIM_UNIT_SHA256 = "a139ae46dab2a7c2f3367a7ecfe225f76776cb09f9542e79712dc1209446436c"
SHIM_SCRIPT = """#!/bin/sh
# Creates /dev/boot and /dev/boot1..6 compatibility links for a UDM Pro whose
# internal storage is not a "Generic" USB card reader. The stock kernel only
# names the disk "boot" when the SCSI vendor starts with "Generic"; the
# firmware updater hard-codes /dev/boot1 and /dev/boot3.
# Lives on /persistent so it survives firmware updates.

# Never touch a real kernel-provided block node (stock hardware).
if [ -b /dev/boot ] && [ ! -L /dev/boot ]; then
@T@exit 0
fi

n=0
while [ ! -e /dev/disk/by-partlabel/boot ] && [ "$n" -lt 60 ]; do
@T@sleep 1
@T@n=$((n + 1))
done
[ -e /dev/disk/by-partlabel/boot ] || { echo "no 'boot' partition found" >&2; exit 1; }

bootpart=$(readlink -f /dev/disk/by-partlabel/boot)
disk=$(lsblk -no PKNAME "$bootpart" | head -n 1)
[ -n "$disk" ] || { echo "cannot determine parent disk of $bootpart" >&2; exit 1; }

ln -sfn "$disk" /dev/boot

i=1
for label in boot recovery root log persistent overlay; do
@T@p=$(readlink -f "/dev/disk/by-partlabel/$label" 2>/dev/null)
@T@if [ -b "$p" ] && ! { [ -e "/dev/boot$i" ] && [ ! -L "/dev/boot$i" ]; }; then
@T@@T@ln -sfn "$(basename "$p")" "/dev/boot$i"
@T@fi
@T@i=$((i + 1))
done
""".replace("@T@", "\t")

SHIM_UNIT = """[Unit]
Description=Create /dev/boot* compatibility links for the firmware updater
After=local-fs.target systemd-udevd.service
ConditionPathExists=/persistent/udm-boot-shim/udm-boot-links.sh

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/bin/sh /persistent/udm-boot-shim/udm-boot-links.sh

[Install]
WantedBy=multi-user.target
"""


def text_input(prompt):
    """Print the prompt, flush it, then read; exit cleanly on Ctrl+C or closed stdin."""
    try:
        sys.stdout.write(prompt)
        sys.stdout.flush()
        s = _raw_input()
    except (EOFError, KeyboardInterrupt):
        print("")
        sys.exit("Cancelled; nothing was written.")
    return s.replace("\ufeff", "") if PY3 else s.replace("\xef\xbb\xbf", "")


def die(msg):
    print("\nERROR: %s" % msg, file=sys.stderr)
    sys.exit(1)


def hash_file(path, algo=hashlib.sha256):
    h = algo()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(MIB), b""):
            h.update(b)
    return h.hexdigest()


def is_admin():
    if IS_WINDOWS:
        try:
            import ctypes
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            return False
    try:
        return os.geteuid() == 0
    except AttributeError:
        return True


def valid_fw(s):
    return s.lower().startswith("https://") or os.path.isfile(s)


# =========================================================================== GPT
def crc32(b):
    return zlib.crc32(b) & 0xFFFFFFFF


def gpt_header(cur, backup, last_usable, entries_lba, entries_crc):
    raw = struct.pack("<8sIIIIQQQQ16sQIII", b"EFI PART", 0x00010000, 92, 0, 0, cur, backup, 34,
                      last_usable, DISK_GUID.bytes_le, entries_lba, 128, 128, entries_crc)
    return (raw[:16] + struct.pack("<I", crc32(raw)) + raw[20:]).ljust(SECTOR, b"\0")


def build_gpt(disk_sectors):
    """Returns (primary, backup): LBA 0..33 and LBA last-32..last as bytes."""
    last = disk_sectors - 1
    entries = b""
    for num, name, first, end in PARTS:
        entries += LINUX_FS_GUID.bytes_le + PART_GUIDS[num].bytes_le + struct.pack("<QQQ", first, end, 0)
        entries += name.encode("utf-16-le").ljust(72, b"\0")
    entries = entries.ljust(128 * 128, b"\0")
    ecrc = crc32(entries)
    mbr = bytearray(SECTOR)
    mbr[446:462] = struct.pack("<B3sB3sII", 0, b"\x00\x02\x00", 0xEE, b"\xff\xff\xff", 1,
                               min(last, 0xFFFFFFFF))
    mbr[510:512] = b"\x55\xaa"
    primary = bytes(mbr) + gpt_header(1, last, last - 33, 2, ecrc) + entries
    backup = entries + gpt_header(last, 1, last - 33, last - 32, ecrc)
    return primary, backup


# =========================================================================== firmware
def download(url, dest):
    """Fetch to dest.part and rename only on success, so a cut-off download is never reused."""
    print("Downloading %s" % url)
    part = dest + ".part"
    curl = which("curl")
    if curl:     # Windows' curl.exe speaks modern TLS; Python 2.7's OpenSSL does not
        rc = subprocess.call([curl, "-fL", "--retry", "3", "--connect-timeout", "30", "-o", part, url])
        if rc:
            die("download failed (curl exit %d). Check the connection/proxy, or pass a local .bin "
                "with --firmware." % rc)
    else:
        try:
            _download_py(url, part)
        except Exception as e:
            die("download failed: %s%s" % (e, "" if PY3 else " (Python 2: install Python 3 or curl)"))
    if os.path.exists(dest):
        os.remove(dest)
    os.rename(part, dest)
    print("  done, %d bytes" % os.path.getsize(dest))


def _download_py(url, dest):
    r = urlopen(Request(url, headers={"User-Agent": "udm-usb-builder/1.0"}), timeout=60)
    total = int(r.headers.get("Content-Length") or 0)
    got, last = 0, 0
    with open(dest, "wb") as f:
        for b in iter(lambda: r.read(MIB), b""):
            f.write(b)
            got += len(b)
            if time.time() - last > 2:
                last = time.time()
                print("  %d MiB%s" % (got // MIB, " / %d MiB" % (total // MIB) if total else ""))
    if total and got != total:
        die("download incomplete (%d of %d bytes)" % (got, total))


def parse_firmware(path, outdir):
    """Cut 'kernel' and 'rootfs' out of a UBNT package: tag + 32-byte name + 5 BE u32 (4th = size) + data + 8."""
    with open(path, "rb") as f:
        head = f.read(0x10c)
        if head[:4] != b"UBNT":
            die("%s is not a UBNT firmware package" % path)
        version = head[4:0x40].split(b"\0")[0].decode("ascii", "replace")
        total = os.path.getsize(path)
        want = {"kernel": os.path.join(outdir, "uImage"), "rootfs": os.path.join(outdir, "rootfs")}
        seen, pos = [], 0x10c
        while pos < total - 8 and want:
            f.seek(pos)
            if f.read(4) not in (b"FILE", b"PART"):
                break
            name = f.read(32).split(b"\0")[0].decode("ascii", "replace")
            size = struct.unpack(">5I", f.read(20))[3]
            start = pos + 56
            seen.append((name, size))
            if name in want:
                f.seek(start)
                left = size
                with open(want.pop(name), "wb") as o:
                    while left:
                        b = f.read(min(left, MIB))
                        if not b:
                            die("firmware truncated inside section %s" % name)
                        o.write(b)
                        left -= len(b)
            pos = start + size + 8
    if want:
        die("firmware has no section(s): %s (found %s)" % (", ".join(want), seen))
    return version, seen


def fit_totalsize(path):
    with open(path, "rb") as f:
        h = f.read(8)
    if h[:4] != b"\xd0\x0d\xfe\xed":
        die("%s is not a FIT image (missing d00dfeed magic)" % path)
    return struct.unpack(">I", h[4:8])[0]


def check_payloads(version, uimage, rootfs):
    """Structural checks always; hash-check only for a confirmed version."""
    if fit_totalsize(uimage) != os.path.getsize(uimage):
        die("uImage size %d != its FIT totalsize" % os.path.getsize(uimage))
    with open(rootfs, "rb") as f:
        sb = f.read(96)
    if sb[:4] != b"hsqs":
        die("rootfs is not squashfs")
    size = os.path.getsize(rootfs)
    if size < struct.unpack("<Q", sb[40:48])[0]:
        die("rootfs is shorter than the squashfs bytes_used: truncated")
    if size % 4096:
        print("  rootfs not 4096-aligned; padding (as UBNT's own files are)")
        with open(rootfs, "ab") as f:
            f.write(b"\0" * (4096 - size % 4096))
    known = KNOWN_FW.get(version)
    if known:
        for name, path in (("uImage", uimage), ("rootfs", rootfs)):
            if hash_file(path) != known[name]:
                die("%s hash differs from the confirmed %s; delete work/firmware.bin and re-run" % (name, version))
        print("  payloads match the confirmed %s" % version)
    else:
        print("  %s is not in the confirmed-firmware list; building anyway (unverified)." % version)
        print("  confirmed: %s" % ", ".join(sorted(KNOWN_FW)))


# =========================================================================== tools (e2fsprogs)
def which(name, extra_dirs=()):
    exts = [""] + (os.environ.get("PATHEXT", ".EXE").split(";") if IS_WINDOWS else [])
    for d in list(extra_dirs) + list(SYSTEM_DIRS) + os.environ.get("PATH", "").split(os.pathsep):
        for e in exts:
            p = os.path.join(d, name + e)
            if d and os.path.isfile(p):
                return p
    return None


def bin_of(name):
    return which(name) or name


def tool_env(bindir):
    env = dict(os.environ, CYGWIN="nodosfilewarning")
    if bindir:
        env["PATH"] = bindir + os.pathsep + env.get("PATH", "")
    return env


def find_tools(tools_dir):
    root = os.path.join(tools_dir, "cygwin")     # e2fsprogs is in usr\sbin, its DLLs in bin
    dirs = [d for d in (os.path.join(root, *p) for p in (("bin",), ("usr", "sbin"), ("sbin",), ("usr", "bin")))
            if os.path.isdir(d)]
    t = {}
    for n in ("mke2fs", "debugfs", "e2fsck"):
        t[n] = which(n, dirs)
        if not t[n]:
            return None
    t["bindir"] = os.pathsep.join(dirs) if t["mke2fs"].startswith(root) else os.path.dirname(t["mke2fs"])
    return t


def install_cygwin(tools_dir, assume_yes):
    if not IS_WINDOWS:
        die("e2fsprogs (mke2fs, debugfs, e2fsck) not found. Install the package first, e.g.\n"
            "  Debian/Ubuntu: sudo apt-get install e2fsprogs    Fedora: sudo dnf install e2fsprogs")
    root = os.path.join(tools_dir, "cygwin")
    print("\nThe e2fsprogs tools (mke2fs, debugfs, e2fsck) were not found. I can install a private Cygwin")
    print("copy in:\n  %s\nsetup-x86_64.exe is downloaded from cygwin.com and checked against its sha512.sum;"
          % root)
    print("e2fsprogs comes from mirrors.kernel.org (about 100 MB). Nothing is installed system-wide.")
    if not assume_yes and text_input("Download and install now? [y/N] ").strip().lower() not in ("y", "yes"):
        die("tools are required; install e2fsprogs yourself or re-run with --install-tools")
    if not os.path.isdir(tools_dir):
        os.makedirs(tools_dir)
    setup, sums = os.path.join(tools_dir, "setup-x86_64.exe"), os.path.join(tools_dir, "sha512.sum")
    download("https://www.cygwin.com/setup-x86_64.exe", setup)
    download("https://www.cygwin.com/sha512.sum", sums)
    with open(sums) as f:
        want = ([l.split()[0].lower() for l in f if "setup-x86_64.exe" in l] or [None])[-1]
    if want != hash_file(setup, hashlib.sha512):
        os.remove(setup)
        die("setup-x86_64.exe failed its sha512 check (or the hash list could not be read); not running it")
    print("  setup-x86_64.exe sha512 OK")
    cmd = [setup, "-q", "-n", "-N", "-d", "-O", "-R", root, "-l", os.path.join(tools_dir, "pkgs"),
           "-s", "https://mirrors.kernel.org/sourceware/cygwin/", "-P", "e2fsprogs"]
    rc = subprocess.call(cmd)
    t = find_tools(tools_dir)
    if rc or not t:
        die("Cygwin setup failed (exit %s). Delete %s and retry, or install e2fsprogs yourself." % (rc, tools_dir))
    return t


def run_tool(tools, cmd, cwd, check=True):
    p = subprocess.Popen(cmd, cwd=cwd, env=tool_env(tools["bindir"]), stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT)
    out = p.communicate()[0].decode("utf-8", "replace")
    if check and p.returncode:
        die("%s failed (%d):\n%s" % (" ".join(cmd), p.returncode, out))
    return p.returncode, out


# =========================================================================== partition images
ERR_RE = re.compile(r"(?i)(ext2fs_|no such|not found|cannot|could not|error|failed|invalid|already exists|no space)")


def make_sparse(path, size):
    with open(path, "wb"):
        pass
    if IS_WINDOWS:
        subprocess.call(["fsutil", "sparse", "setflag", path], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    with open(path, "r+b") as f:
        f.truncate(size)


def build_images(tools, work, uimage, rootfs, recovery, with_shim):
    _, out = run_tool(tools, [tools["mke2fs"], "-V"], work, check=False)
    m = re.search(r"mke2fs (\d+)\.(\d+)", out)
    ver = (int(m.group(1)), int(m.group(2))) if m else (0, 0)
    print("  mke2fs version %d.%d" % ver)
    # per-partition mke2fs options, matching tune2fs on the working unit
    mk = {1: ["-b", "1024", "-I", "128", "-N", "16384", "-O",
              "64bit,metadata_csum" + (",^orphan_file" if ver >= (1, 47) else "")],
          3: ["-b", "4096", "-L", "root"], 4: ["-b", "4096"],
          5: ["-b", "4096", "-L", "persistent"], 6: ["-b", "4096"]}
    lazy = "lazy_itable_init=1,lazy_journal_init=1,root_owner=0:0"
    images = {}
    for num, name, first, last in PARTS:
        size = (last - first + 1) * SECTOR
        img = "p%d_%s.img" % (num, name)
        images[num] = os.path.join(work, img)
        make_sparse(images[num], size)
        if num == 2:     # raw recovery partition, no filesystem
            if recovery:
                with open(recovery, "rb") as src, open(images[num], "r+b") as dst:
                    shutil.copyfileobj(src, dst, MIB)
            continue
        print("  mke2fs %s (%d MiB)" % (name, size // MIB))
        run_tool(tools, [tools["mke2fs"], "-F", "-q", "-t", "ext4", "-E", lazy] + mk[num] + [img], work)

    def debugfs(num, args):
        return run_tool(tools, [tools["debugfs"]] + args + [os.path.basename(images[num])], work)[1]

    def populate(num, cmds, checks):
        cf = "cmds%d.txt" % num
        with open(os.path.join(work, cf), "w") as f:
            f.write("\n".join(cmds) + "\n")
        out = debugfs(num, ["-w", "-f", cf])
        bad = [l for l in out.splitlines() if not l.startswith("debugfs:") and ERR_RE.search(l)]
        if bad:
            die("debugfs reported problems on %s:\n%s" % (images[num], "\n".join(bad)))
        for path, size in checks:
            m = re.search(r"Size: (\d+)", debugfs(num, ["-R", "stat /" + path]))
            if not m or int(m.group(1)) != size:
                die("%s in %s is missing or has the wrong size (expected %d)" % (path, images[num], size))

    def fileops(src, dst, mode):
        d, _, n = dst.rpartition("/")     # debugfs 'write' only creates files in the current directory
        return (["cd /" + d] if d else []) + [
            "write %s %s" % (src, n), "set_inode_field %s mode 0%o" % (n, 0o100000 | mode),
            "set_inode_field %s uid 0" % n, "set_inode_field %s gid 0" % n] + (["cd /"] if d else [])

    def mkdirs(*dirs):
        return [c for d in dirs for c in ("mkdir " + d, "set_inode_field %s uid 0" % d, "set_inode_field %s gid 0" % d)]

    ksize, rsize = os.path.getsize(uimage), os.path.getsize(rootfs)
    print("  populating boot")
    populate(1, fileops("uImage", "uImage", 0o644) + fileops("uImage", "uImage.bkp", 0o644),
             [("uImage", ksize), ("uImage.bkp", ksize)])
    print("  populating root (rootfs is large, this takes a minute)")
    populate(3, fileops("rootfs", "rootfs", 0o644) + fileops("rootfs", "rootfs.bkp", 0o644),
             [("rootfs", rsize), ("rootfs.bkp", rsize)])
    if with_shim:
        print("  installing shim")
        for fname, text in (("udm-boot-links.sh", SHIM_SCRIPT), ("udm-boot-links.service", SHIM_UNIT)):
            with open(os.path.join(work, fname), "wb") as f:
                f.write(text.encode("ascii"))
        populate(5, mkdirs("udm-boot-shim") + fileops("udm-boot-links.sh", "udm-boot-shim/udm-boot-links.sh", 0o755),
                 [("udm-boot-shim/udm-boot-links.sh", len(SHIM_SCRIPT))])
        sysd = "data/etc/systemd/system"
        link = sysd + "/multi-user.target.wants/udm-boot-links.service"
        populate(6, mkdirs(".workdir", "data", "data/etc", "data/etc/systemd", sysd, sysd + "/multi-user.target.wants")
                 + fileops("udm-boot-links.service", sysd + "/udm-boot-links.service", 0o644)
                 + ["symlink %s /etc/systemd/system/udm-boot-links.service" % link],
                 [(sysd + "/udm-boot-links.service", len(SHIM_UNIT))])
        if "symlink" not in debugfs(6, ["-R", "stat /" + link]):
            die("overlay enable symlink was not created")
    procs = [(n, subprocess.Popen([tools["e2fsck"], "-fn", os.path.basename(images[n])], cwd=work,
                                  env=tool_env(tools["bindir"]), stdout=subprocess.PIPE, stderr=subprocess.STDOUT))
             for n in (1, 3, 4, 5, 6)]
    for n, p in procs:
        out = p.communicate()[0].decode("utf-8", "replace")
        if p.returncode:
            die("e2fsck found problems in %s:\n%s" % (images[n], out))
    print("  e2fsck clean on all ext4 images")
    return images


# =========================================================================== target (disk or image)
class Target(object):
    def __init__(self, path, size=None):
        if size:
            if os.path.exists(path) and (stat.S_ISBLK(os.stat(path).st_mode)
                                         or stat.S_ISCHR(os.stat(path).st_mode)):
                raise IOError("%s is a device, not a file" % path)
            make_sparse(path, size)
        self.f = open(path, "r+b", 0)

    def write(self, offset, data):
        self.f.seek(offset)
        view = memoryview(data)
        while len(view):
            n = self.f.write(view)
            if n == 0:
                raise IOError("device accepted no data")
            view = view[len(view) if n is None else n:]

    def read(self, offset, n):
        self.f.seek(offset)
        out = b""
        while len(out) < n:
            b = self.f.read(n - len(out))
            if not b:
                break
            out += b
        return out

    def flush(self):
        try:
            self.f.flush()
            os.fsync(self.f.fileno())
        except (IOError, OSError) as e:
            print("    warning: flush failed (%s)" % e)

    def close(self):
        if not self.f.closed:
            self.flush()
            self.f.close()


def write_partition(target, first_sector, image, zero_fill, log):
    base, pos, written = first_sector * SECTOR, 0, 0
    with open(image, "rb") as f:
        for b in iter(lambda: f.read(MIB), b""):
            if not zero_fill and pos >= ZERO_HEAD and b == ZERO:
                pos += len(b)
                continue
            target.write(base + pos, b)
            log.append((base + pos, len(b), hashlib.sha1(b).digest()))
            written += len(b)
            pos += len(b)
    print("    wrote %d MiB of %d MiB" % (written // MIB, pos // MIB))


def verify(target, log):
    bad = 0
    for off, n, digest in log:
        if hashlib.sha1(target.read(off, n)).digest() != digest:
            bad += 1
            print("    MISMATCH at byte offset %d" % off)
    return bad


# =========================================================================== disk discovery (Windows / Linux)
def ps(cmd):
    try:
        p = subprocess.Popen(["powershell", "-NoProfile", "-NonInteractive", "-Command", cmd],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except OSError:
        die("PowerShell was not found; this tool needs Windows PowerShell for disk handling")
    out, err = p.communicate()
    return p.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


def run_out(cmd):
    try:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except OSError as e:
        return 127, "", str(e)
    out, err = p.communicate()
    return p.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


def _rows(out):
    data = json.loads(out) if out.strip() else []
    return data if isinstance(data, list) else [data]


def clean_serial(s):
    return re.sub(r"[\s\.]+", "", s or "")


def _truthy(v):
    return v in (True, 1, "1", "true", "True")


def gib(size):
    return size / float(1 << 30)


# ---- Windows ----------------------------------------------------------------
def _list_disks_windows():
    rc, out, err = ps("Get-Disk | Select-Object Number,FriendlyName,SerialNumber,UniqueId,"
                      "@{n='BusType';e={[string]$_.BusType}},Size,@{n='PartitionStyle';e={[string]$_.PartitionStyle}},"
                      "IsSystem,IsBoot,IsOffline,IsReadOnly,LogicalSectorSize | ConvertTo-Json -Compress")
    if rc or not out.strip():
        die("Get-Disk failed: %s" % err.strip())
    disks = []
    for d in _rows(out):
        disks.append({
            "id": int(d["Number"]),
            "name": (d.get("FriendlyName") or "?").strip(),
            "serial": clean_serial(d.get("SerialNumber")),
            "size": int(d["Size"]),
            "bus": str(d.get("BusType") or "").upper(),
            "sector": int(d.get("LogicalSectorSize") or 0),
            "style": str(d.get("PartitionStyle") or ""),
            "dev": r"\\.\PhysicalDrive%s" % d["Number"],
            "system": bool(d.get("IsSystem") or d.get("IsBoot")),
            "readonly": bool(d.get("IsReadOnly")),
            "unique": str(d.get("UniqueId") or ""),
            "_extra": [],
        })
    return disks


# ---- Linux ------------------------------------------------------------------
_LINUX_COLS = "NAME,PATH,SIZE,TYPE,TRAN,MODEL,SERIAL,RM,RO,LOG-SEC,MOUNTPOINTS,FSTYPE,PKNAME,LABEL"


def _mounts(node):
    v = node.get("mountpoints", node.get("mountpoint"))
    if v in (None, ""):
        return []
    return v if isinstance(v, list) else [v]


def _lsblk_json(args):
    rc, out, err = run_out([bin_of("lsblk"), "-J", "-b"] + args)
    if (rc or not out.strip()) and any("MOUNTPOINTS" in a for a in args):   # util-linux < 2.37
        args = [a.replace("MOUNTPOINTS", "MOUNTPOINT") for a in args]
        rc, out, err = run_out([bin_of("lsblk"), "-J", "-b"] + args)
    if rc or not out.strip():
        die("lsblk is required on Linux but failed: %s" % (err.strip() or "no output"))
    try:
        return json.loads(out)
    except ValueError as e:
        die("could not parse lsblk output: %s" % e)


def _walk(node):
    yield node
    for c in node.get("children") or []:
        for x in _walk(c):
            yield x


def _pick(d, *names):
    for n in names:
        if d.get(n) not in (None, ""):
            return d[n]
    return None


def _swap_devices():
    out = []
    try:
        with open("/proc/swaps") as f:
            for line in f.readlines()[1:]:
                parts = line.split()
                if parts:
                    out.append(parts[0])
    except IOError:
        pass
    return set(out)


def _list_disks_linux():
    data = _lsblk_json(["-o", _LINUX_COLS])
    system_disk = _parent_disk(_mount_source("/"))
    swap = _swap_devices()
    disks = []
    for disk in data.get("blockdevices", []):
        if _pick(disk, "type") != "disk":
            continue
        nodes = list(_walk(disk))
        mounts = [m for n in nodes for m in _mounts(n)]
        fstypes = set((_pick(n, "fstype") or "") for n in nodes)
        dev = _pick(disk, "path") or ("/dev/" + (_pick(disk, "name") or ""))
        if not dev or dev == "/dev/":
            continue
        serial = (_pick(disk, "serial") or "").strip()
        wwn = ""
        if not serial:                           # some sticks expose only a WWN
            rc, out, _ = run_out([bin_of("lsblk"), "-no", "WWN", dev])
            if rc == 0 and out.strip():
                wwn = out.strip().splitlines()[0].strip()
        extra = []
        if mounts:
            extra.append("mounted at %s" % mounts[0])
        if fstypes & set(("LVM2_member", "linux_raid_member", "crypto_LUKS", "zfs_member")):
            extra.append("LVM/RAID/LUKS/ZFS member")
        if swap & set(_pick(n, "path") for n in nodes):
            extra.append("used as swap")
        disks.append({
            "id": dev,
            "name": (_pick(disk, "model") or os.path.basename(dev) or "?").strip(),
            "serial": serial,
            "size": int(_pick(disk, "size") or 0),
            "bus": (_pick(disk, "tran") or "").upper() or "?",
            "sector": int(_pick(disk, "log-sec") or 0),
            "style": "",
            "dev": dev,
            "system": system_disk in (dev, _parent_disk(dev)),
            "readonly": _truthy(_pick(disk, "ro")),
            "unique": "%s|%s|%s|%s" % (serial, wwn, _pick(disk, "size") or "",
                                       _pick(disk, "path") or ""),
            "_extra": extra,
        })
    return disks


def list_disks():
    return _list_disks_windows() if IS_WINDOWS else _list_disks_linux()


# ---- shared -----------------------------------------------------------------
def _mount_source(path):
    """The block device backing the filesystem that holds `path` (longest /proc/mounts prefix)."""
    if IS_WINDOWS:
        return None
    target = os.path.realpath(path)
    best_mnt, best_dev = "", None
    try:
        with open("/proc/mounts") as f:
            for line in f:
                parts = line.split()
                if len(parts) < 2:
                    continue
                dev, mnt = parts[0], parts[1]
                if (target == mnt or target.startswith(mnt.rstrip("/") + "/")) and len(mnt) > len(best_mnt):
                    best_mnt, best_dev = mnt, dev
    except IOError:
        return None
    return best_dev


def _parent_disk(dev):
    """Walk PKNAME up to the top-level whole disk (handles md/nested devices)."""
    if not dev:
        return None
    seen = set()
    while dev and dev not in seen:
        seen.add(dev)
        rc, out, _ = run_out([bin_of("lsblk"), "-no", "PKNAME", dev])
        name = out.strip().splitlines()[0].strip() if rc == 0 and out.strip() else ""
        if not name:
            break
        dev = "/dev/" + name
    return dev


def visible(disks, args):
    return [d for d in disks if d["bus"] == "USB" or args.allow_non_usb]


def describe(d):
    style = "  style=%s" % d["style"] if d["style"] else ""
    return "%s %s: %s  %.2f GiB  bus=%s  serial=%s%s%s" % (
        "Disk" if IS_WINDOWS else "Drive", d["id"], (d["name"] or "?"), gib(d["size"]),
        d["bus"] or "?", d["serial"] or "(none)", style, "  [SYSTEM]" if d["system"] else "")


def disk_problems(d, args):
    if d["size"] == 0:
        return ["no media inserted"]
    p = list(d.get("_extra") or [])
    if d["system"]:
        p.append("this is a system/boot disk")
    if d["readonly"]:
        p.append("disk is read-only (a write-protect switch?)")
    if d["bus"] != "USB" and not args.allow_non_usb:
        p.append("bus type is %s, not USB (override: --allow-non-usb)" % (d["bus"] or "?"))
    if d["sector"] != SECTOR:
        p.append("logical sector size is %s, need %d" % (d["sector"] or "unknown", SECTOR))
    if d["size"] // SECTOR < MIN_DISK_SECTORS:
        p.append("too small: %d bytes, layout needs %d" % (d["size"], MIN_DISK_SECTORS * SECTOR))
    if d["size"] > MAX_DISK_BYTES and not args.allow_large:
        p.append("larger than 128 GiB, probably not a spare stick (override: --allow-large)")
    return p


def refetch(disk, args):
    """Re-read the disk and abort if it was swapped or became ineligible since it was chosen."""
    now = [d for d in list_disks() if str(d["id"]) == str(disk["id"])]
    same = bool(now) and all(now[0].get(k) == disk.get(k) for k in ("name", "size", "serial", "unique"))
    if not same or disk_problems(now[0], args):
        die("disk %s changed or is no longer eligible; aborting" % disk["id"])
    return now[0]


def select_disk(args):
    disks = list_disks()
    usb = [d["id"] for d in disks if d["bus"] == "USB"]
    match = [d for d in disks if str(d["id"]) == str(args.disk)]
    if not match:
        die("no disk %s (see --list). USB disks: %s" % (args.disk, ", ".join(map(str, usb)) or "none found"))
    d = match[0]
    if d["bus"] != "USB" and not args.allow_non_usb:
        die("%s is not a USB disk (USB disks shown by --list: %s). To target another disk deliberately, "
            "add --allow-non-usb." % (args.disk, ", ".join(map(str, usb)) or "none found"))
    if not is_admin():
        die("run this from an Administrator prompt" if IS_WINDOWS else "run this as root (re-run with sudo)")
    print(describe(d))
    probs = disk_problems(d, args)
    if probs:
        die("refusing %s: %s" % (args.disk, "; ".join(probs)))
    return d


def disk_partitions(d):
    """Normalized [{num, label, size, mount}] for the confirmation prompt."""
    if IS_WINDOWS:
        rows = _rows(ps("Get-Partition -DiskNumber %d -ErrorAction SilentlyContinue | Select-Object PartitionNumber,"
                        "@{n='DriveLetter';e={[string]$_.DriveLetter}},Size,@{n='Type';e={[string]$_.Type}} "
                        "| ConvertTo-Json -Compress" % int(d["id"]))[1])
        return [{"num": r.get("PartitionNumber"), "label": r.get("DriveLetter") or "",
                 "size": int(r.get("Size") or 0), "mount": r.get("Type") or ""} for r in rows]
    data = _lsblk_json(["-o", _LINUX_COLS, d["dev"]])
    parts = []
    for node in data.get("blockdevices", []):
        for c in node.get("children") or []:
            if _pick(c, "type") == "part":
                mounts = _mounts(c)
                parts.append({"num": _pick(c, "name"), "label": _pick(c, "label") or "",
                              "size": int(_pick(c, "size") or 0), "mount": mounts[0] if mounts else ""})
    return parts


def guard_paths(d, paths):
    if IS_WINDOWS:
        letters = set(p["label"].upper() for p in disk_partitions(d)) - set([""])
        for p in paths:
            if os.path.splitdrive(p)[0][:1].upper() in letters:
                die("%s is on the disk being erased; use --workdir/--tools-dir on another drive" % p)
        return
    target = os.path.realpath(d["dev"])
    for p in paths:
        src = _mount_source(p)
        if src and os.path.realpath(_parent_disk(src)) == target:
            die("%s is on the disk being erased; use --workdir/--tools-dir on another disk" % p)


def confirm_disk(d, assume_yes=False):
    print("\n=== TARGET ===\n" + describe(d))
    for p in disk_partitions(d):
        print("   partition %s  %s  %.2f GiB  %s" % (
            p["num"], p["label"] or "-", gib(p["size"]), p["mount"] or "unmounted"))
    print("EVERYTHING on this disk will be destroyed.")
    if assume_yes:
        print(">>> --yes given: writing without a typed confirmation.")
        return
    serial = d["serial"]
    want = serial or "DISK %s" % d["id"]
    print(">>> Waiting for your input on the next line (nothing is written until it matches).")
    text = text_input("Type %s to confirm: " % (("this disk's serial number (%s)" % serial) if serial
                                               else "'%s' (no serial available)" % want))
    if text.strip().lower() != want.lower():
        die("confirmation did not match; nothing was written")


def wipe_table(d):
    """Remove existing signatures/partitioning before writing: Clear-Disk / wipefs / unmount+zero."""
    if IS_WINDOWS:
        rc, out, err = ps("Set-Disk -Number %d -IsOffline $false -ErrorAction SilentlyContinue; "
                          "Clear-Disk -Number %d -RemoveData -RemoveOEM -Confirm:$false" % (int(d["id"]), int(d["id"])))
        now = [x for x in list_disks() if str(x["id"]) == str(d["id"])]
        if rc and not (now and now[0]["style"] == "RAW"):
            die("Clear-Disk failed: %s\nClose Explorer windows and anything else using the stick, "
                "replug it and retry." % (err.strip() or out.strip()))
        return
    dev = d["dev"]
    wipefs = which("wipefs")
    if wipefs:
        rc, out, err = run_out([wipefs, "-a", dev])
        if rc:
            print("    wipefs: %s" % (err.strip() or out.strip()))
    else:
        with open(dev, "r+b", 0) as f:
            f.seek(0)
            f.write(ZERO)
            f.seek(max(0, d["size"] - MIB))
            f.write(b"\0" * min(MIB, d["size"]))


def drop_caches(path, disk):
    """After writing, drop cached data so the read-back hits the media (Windows: offline/online)."""
    if IS_WINDOWS:
        n = int(disk["id"])
        ps("Set-Disk -Number %d -IsOffline $true -ErrorAction SilentlyContinue; "
           "Set-Disk -Number %d -IsOffline $false -ErrorAction SilentlyContinue" % (n, n))
        return
    for tool in ("blockdev", "udevadm"):
        p = which(tool)
        if p:
            run_out([p, "--flushbufs", path] if tool == "blockdev" else [p, "settle"])


def activate(disk, path):
    if IS_WINDOWS:
        ps("Update-Disk -Number %d -ErrorAction SilentlyContinue" % int(disk["id"]))
        return
    for cmd in (("partprobe", path), ("blockdev", "--rereadpt", path)):
        p = which(cmd[0])
        if p and run_out([p] + list(cmd[1:]))[0] == 0:
            break
    p = which("udevadm")
    if p:
        run_out([p, "settle"])


def free_bytes(path):
    try:
        return shutil.disk_usage(path).free
    except AttributeError:      # Python 2
        if IS_WINDOWS:
            import ctypes
            n = ctypes.c_ulonglong(0)
            ctypes.windll.kernel32.GetDiskFreeSpaceExW(ctypes.c_wchar_p(path), None, None, ctypes.byref(n))
            return n.value
        st = os.statvfs(path)
        return st.f_bavail * st.f_frsize


# =========================================================================== interactive wizard
DISCLAIMER = """
  OVERRIDE MODE - READ THIS
  This lists disks that are NOT USB drives, including internal disks, and lifts the
  128 GiB size limit. Writing to the wrong disk destroys all of its data permanently.
  System and boot disks, disks with a sector size other than 512 bytes, and disks smaller
  than 14.56 GiB stay blocked even in override mode.
  You are solely responsible for choosing the right disk and for any data loss.
"""


def print_table(disks, args):
    print("\n  %-14s %-26s %10s  %-6s %-22s %s" % ("Disk", "Name", "Size", "Bus", "Serial", "Status"))
    ok = []
    for d in sorted(disks, key=lambda x: str(x["id"])):
        probs = disk_problems(d, args)
        if not probs:
            ok.append(d["id"])
        print("  %-14s %-26s %7.2f GiB  %-6s %-22s %s" % (
            d["id"], (d["name"] or "?")[:26], gib(d["size"]), d["bus"] or "?",
            (d["serial"] or "-")[:22], "OK" if not probs else "refused: " + "; ".join(probs)))
    return ok


def interactive(args):
    print("\n=== UDM Pro USB stick builder ===")
    if not is_admin():
        print("NOTE: not running as %s. You can look around, but writing a disk will fail."
              % ("Administrator" if IS_WINDOWS else "root"))
    target_hint = "disk number" if IS_WINDOWS else "device path (e.g. /dev/sdX)"
    while True:
        print("\nScanning drives ...")
        disks = list_disks()
        shown = visible(disks, args)
        ok = print_table(shown, args) if shown else []
        if not shown:
            print("  No USB drives found. Plug in the stick.")
        if len(disks) > len(shown):
            print("\n  (%d non-USB disk(s) hidden)" % (len(disks) - len(shown)))
        ans = text_input("\nType a %s, R to rescan, %sQ to quit: "
                         % (target_hint, "" if args.allow_non_usb else "O for override mode, ")).strip()
        low = ans.lower()
        if low in ("q", "quit", "exit"):
            sys.exit(0)
        if low in ("o", "override") and not args.allow_non_usb:
            print(DISCLAIMER)
            if text_input("  Type Agree to continue (anything else cancels): ").strip() == "Agree":
                args.allow_non_usb = args.allow_large = True
                print("  Override mode ON: all disks are listed.")
            else:
                print("  Cancelled; override stays off.")
        elif low in ("r", "rescan", ""):
            continue
        else:
            picked = [i for i in ok if str(i).lower() == low]
            if not picked:
                print("  %s is not one of the selectable disks above." % ans)
            else:
                args.disk = picked[0]
                break
    if not is_admin():
        die(("disk %s selected, but this is not an Administrator prompt.\nClose this window, open "
             "PowerShell as Administrator and run the script again." % args.disk) if IS_WINDOWS else
            ("disk %s selected, but this is not root. Re-run with sudo." % args.disk))
    print("\nFirmware: press Enter for the known-good default\n  %s" % DEFAULT_FW)
    fw = text_input("Firmware https URL or local .bin path: ").strip()
    if fw:
        args.firmware = fw


# =========================================================================== selftest
def selftest(args):
    results = []

    def check(label, good):
        results.append(bool(good))
        print("%-40s %s" % (label, "ok" if good else "FAIL"))

    primary, _ = build_gpt(60063744)     # the real 32 GB stick: CRCs taken from its dump
    check("GPT header CRC 1014c911", struct.unpack("<I", primary[528:532])[0] == 0x1014C911)
    check("GPT entries CRC 1587e227", struct.unpack("<I", primary[600:604])[0] == 0x1587E227)
    check("protective MBR entry", primary[446:462] == bytes(bytearray.fromhex(
        "00 00 02 00 ee ff ff ff 01 00 00 00 ff 7f 94 03")))
    for name, data, want in (("shim script", SHIM_SCRIPT, SHIM_SCRIPT_SHA256), ("shim unit", SHIM_UNIT, SHIM_UNIT_SHA256)):
        check(name + " sha256", hashlib.sha256(data.encode("ascii")).hexdigest() == want)
    check("partition layout ordered/aligned", all(
        PARTS[i][2] % 2048 == 0 and (i == 0 or PARTS[i][2] > PARTS[i - 1][3]) for i in range(len(PARTS))))
    if args.firmware and os.path.isfile(args.firmware):
        d = tempfile.mkdtemp()
        try:
            ver, _ = parse_firmware(args.firmware, d)
            check_payloads(ver, os.path.join(d, "uImage"), os.path.join(d, "rootfs"))
            check("firmware parse " + ver, True)
        finally:
            shutil.rmtree(d, ignore_errors=True)
    print("\nSELFTEST %s" % ("PASSED" if all(results) else "FAILED"))
    return 0 if all(results) else 1


# =========================================================================== main
def parse_args():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    a = ap.add_argument
    a("-i", "--interactive", action="store_true", help="guided mode (default with no arguments in a terminal)")
    a("--list", action="store_true", help="list USB disks and exit")
    a("--selftest", action="store_true", help="run offline self-checks and exit")
    a("--disk", help="ERASE and build: a Windows disk number, or a Linux device path (e.g. /dev/sdX)")
    a("--image", help="build into this disk-image file instead of a real disk (for testing)")
    a("-y", "--yes", action="store_true", help="skip the confirmation prompt and write (no serial to type)")
    a("--firmware", default=DEFAULT_FW, help="firmware https URL or local .bin (default: UDM Pro 6.0.11)")
    a("--list-firmware", action="store_true", help="list confirmed-working firmware versions and exit")
    a("--recovery", help="raw FIT image for the recovery partition (default: the firmware's kernel FIT)")
    a("--zero-recovery", action="store_true", help="leave the recovery partition empty")
    a("--no-shim", action="store_true", help="do not install the /dev/boot* shim")
    a("--workdir", help="scratch folder, needs ~6 GB free (default: next to this script)")
    a("--tools-dir", help="where e2fsprogs lives or is installed (default: next to this script)")
    a("--install-tools", action="store_true", help="install e2fsprogs via Cygwin without asking")
    a("--zero-fill", action="store_true", help="also write all-zero blocks; use on a previously used stick")
    a("--keep-work", action="store_true", help="keep the build files in <workdir>/build")
    a("--allow-non-usb", action="store_true", help="list and allow non-USB disks (dangerous)")
    a("--allow-large", action="store_true", help="allow disks over 128 GiB")
    args = ap.parse_args()
    if args.disk is not None and args.image:
        ap.error("--disk and --image are mutually exclusive")
    if args.interactive and args.image:
        ap.error("--interactive cannot be combined with --image")
    if args.image:
        if args.image.startswith(("\\\\.", "\\\\?")):
            ap.error("--image must be a file path, not a device path")
        if not IS_WINDOWS and os.path.abspath(args.image).startswith("/dev/"):
            ap.error("--image must be a regular file, not a device: %s" % args.image)
        if os.path.exists(args.image):
            try:
                st = os.stat(args.image)
            except OSError:
                st = None
            if st is not None and (stat.S_ISBLK(st.st_mode) or stat.S_ISCHR(st.st_mode)):
                ap.error("--image must be a regular file, not a device: %s" % args.image)
    if args.disk is not None:
        s = str(args.disk)
        if IS_WINDOWS:
            if not s.isdigit():
                ap.error("--disk on Windows takes a disk number (see --list)")
        else:
            if not s.startswith("/"):
                s = "/dev/" + s
            args.disk = s
    return args


def check_recovery(path):
    if not os.path.isfile(path):
        die("no such recovery file: %s" % path)
    size, total = os.path.getsize(path), fit_totalsize(path)
    if size > 32 * MIB:
        die("recovery image is %d bytes; the recovery partition is only 32 MiB" % size)
    if size < total:
        die("recovery image is truncated: %d bytes but its FIT header says %d" % (size, total))


def main():
    args = parse_args()
    here = os.path.dirname(os.path.abspath(__file__))
    if not PY3:
        print("NOTE: running on Python 2 (end of life); Python 3 is recommended.\n")
    if args.selftest:
        sys.exit(selftest(args))
    if args.list_firmware:
        for ver in sorted(KNOWN_FW):
            e = KNOWN_FW[ver]
            print("%s\n  uImage %s\n  rootfs %s" % (ver, e["uImage"], e["rootfs"]))
            if e.get("notes"):
                print("  %s" % e["notes"])
            if e.get("url"):
                print("  %s" % e["url"])
        return
    if args.list:
        disks = list_disks()
        shown = visible(disks, args)
        for d in shown:
            probs = disk_problems(d, args)
            print(describe(d) + ("\n     refused: " + "; ".join(probs) if probs else ""))
        if not shown:
            print("No USB disks found.")
        if len(disks) > len(shown):
            print("(%d non-USB disk(s) hidden; they cannot be written without --allow-non-usb)"
                  % (len(disks) - len(shown)))
        return
    if args.interactive or (args.disk is None and not args.image and sys.stdin.isatty()):
        interactive(args)
    elif args.disk is None and not args.image:
        sys.exit("give --disk N (see --list), --image FILE, or run without arguments for the guided mode")
    if not valid_fw(args.firmware):
        die("--firmware must be an https URL or an existing file: %s" % args.firmware)

    disk = select_disk(args) if args.disk is not None else None
    if args.recovery:
        check_recovery(args.recovery)
    work = os.path.abspath(args.workdir or os.path.join(here, "work"))
    build = os.path.join(work, "build")
    tools_dir = os.path.abspath(args.tools_dir or os.path.join(here, "tools"))
    if not os.path.isdir(work):
        os.makedirs(work)
    if free_bytes(work) < 6 * 1024 * MIB:
        die("need about 6 GB free in %s; free some space or pass --workdir on a bigger drive" % work)
    if disk:
        guard_paths(disk, [work, tools_dir, here])
    tools = find_tools(tools_dir) or install_cygwin(tools_dir, args.install_tools)
    print("Tools: %s" % tools["mke2fs"])

    try:
        fw = args.firmware if os.path.isfile(args.firmware) else os.path.join(work, "firmware.bin")
        cached = fw != args.firmware and os.path.isfile(fw) and os.path.getsize(fw) > 100 * MIB
        if fw != args.firmware and not cached:
            download(args.firmware, fw)
        shutil.rmtree(build, ignore_errors=True)
        os.makedirs(build)
        print("Extracting firmware ...")
        try:
            version, secs = parse_firmware(fw, build)
        except SystemExit:
            if cached:
                os.remove(fw)
                print("The cached firmware.bin was unusable and was deleted; run again to re-download.")
            raise
        print("  %s  sections: %s" % (version, ", ".join("%s(%d)" % s for s in secs)))
        uimage, rootfs = os.path.join(build, "uImage"), os.path.join(build, "rootfs")
        check_payloads(version, uimage, rootfs)

        if args.zero_recovery:
            recovery, note = None, "left empty (--zero-recovery)"
        elif args.recovery:
            recovery, note = args.recovery, "from %s" % args.recovery
        else:
            recovery, note = uimage, "firmware kernel FIT (as the stock updater does; inferred, untested on hardware)"
        if recovery and os.path.getsize(recovery) > 32 * MIB:
            die("recovery image is larger than the 32 MiB recovery partition")
        print("Recovery partition: %s\nBuilding partition images ..." % note)
        images = build_images(tools, build, uimage, rootfs, recovery, not args.no_shim)

        if disk:
            disk = refetch(disk, args)
            confirm_disk(disk, args.yes)
            disk = refetch(disk, args)
            print("Preparing disk (removing existing partitions) ...")
            wipe_table(disk)
            path, size = disk["dev"], None
            sectors = disk["size"] // SECTOR
        else:
            path, size, sectors = os.path.abspath(args.image), STOCK_DISK_SECTORS * SECTOR, STOCK_DISK_SECTORS
            print("Image mode: %s (%d sectors, same size as the stock card)" % (args.image, sectors))
        try:
            target = Target(path, size)
        except (IOError, OSError) as e:
            die("cannot open %s (%s). Close anything using the stick and retry." % (path, e))

        log = []
        try:
            for num, name, first, last in PARTS:     # partitions first: an interrupted run leaves no valid GPT
                print("  partition %d %s @ sector %d" % (num, name, first))
                write_partition(target, first, images[num], args.zero_fill, log)
            print("Writing GPT ...")
            primary, backup = build_gpt(sectors)
            for off, data in ((0, primary + b"\0" * ((2048 - 34) * SECTOR)), ((sectors - 33) * SECTOR, backup)):
                target.write(off, data)
                log.append((off, len(data), hashlib.sha1(data).digest()))
            target.flush()
            if disk:     # drop the OS cache so the read-back hits the media
                target.close()
                drop_caches(path, disk)
                target = Target(path)
            print("Verifying (reading everything back) ...")
            bad = verify(target, log)
        except (IOError, OSError) as e:
            try:
                target.write(0, b"\0" * 2048 * SECTOR)
            except (IOError, OSError):
                pass
            die("write failed (%s). The disk is partly written and unusable; replug it and re-run." % e)
        finally:
            target.close()
        if bad:
            die("%d block(s) failed verification; do NOT use this stick" % bad)
        if disk:
            activate(disk, path)
        print("\nDone. Verified %d regions." % len(log))
        print("Unplug the stick and put it in the UDM Pro." if disk else "Image written: %s" % args.image)
    finally:
        if not args.keep_work:
            shutil.rmtree(build, ignore_errors=True)


if __name__ == "__main__":
    main()
