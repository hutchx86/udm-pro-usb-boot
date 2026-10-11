---
name: project-udm-pro-boot-fix
description: "How a UDM Pro boots from replacement USB storage, and the squashfs padding requirement"
metadata:
  type: project
---

How a UDM Pro boots from replacement USB storage (a USB stick standing in for the internal
storage), using an official Ubiquiti firmware `.bin` as the source of the kernel and rootfs.

## Facts

- The UDM Pro's internal storage is generic USB mass storage behind a card-reader bridge
  chip, not a native eMMC controller; there is no eMMC boot0/boot1.
- GPT layout (identical across the reference units): `boot` (64 MiB), `recovery` (32 MiB),
  `root` (2 GiB), `log` (1 GiB), `persistent` (2 GiB), `overlay` (rest). All type 8300.
- U-Boot's compiled-in bootcmd (the env partitions mtd1/mtd2 are blank on these units):
  - normal path: `ext4load usb 0 $addr uImage` from the `boot` partition's ext4; falls back
    to `uImage.bkp` + `rootfs.bkp`.
  - recovery path: raw `usb read $addr 0x20800 0x10000` — a raw sector read of the
    `recovery` partition, bypassing any filesystem.
  - `root` is an ext4 filesystem holding a squashfs file named `rootfs` (+ `rootfs.bkp`),
    loop-mounted as the OS.
- `.bin` package: the `kernel` section is the FIT (`uImage`); the `rootfs` section is the
  squashfs, already padded to 4096 bytes.

## Squashfs padding

A squashfs's superblock `bytes_used` is not the file's on-disk size. A real UBNT-written
`rootfs` is padded with zeros up to the next 4096-byte boundary past `bytes_used` (one
reference file: 915,968,000 bytes with `bytes_used` 915,965,594). A file cut exactly at
`bytes_used` fails to loop-mount with `Invalid argument`, and no `SQUASHFS error` appears in
dmesg.

Rule: any squashfs extracted from a firmware bundle for use as a loop-mountable rootfs must
be padded to the next 4096-byte boundary past `bytes_used` before writing it to disk. The
builder does this automatically.
