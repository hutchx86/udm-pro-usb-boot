#!/bin/sh
# Creates /dev/boot and /dev/boot1..6 compatibility links for a UDM Pro whose
# internal storage is not a "Generic" USB card reader. The stock kernel only
# names the disk "boot" when the SCSI vendor starts with "Generic"; the
# firmware updater hard-codes /dev/boot1 and /dev/boot3.
# Lives on /persistent so it survives firmware updates.

# Never touch a real kernel-provided block node (stock hardware).
if [ -b /dev/boot ] && [ ! -L /dev/boot ]; then
	exit 0
fi

n=0
while [ ! -e /dev/disk/by-partlabel/boot ] && [ "$n" -lt 60 ]; do
	sleep 1
	n=$((n + 1))
done
[ -e /dev/disk/by-partlabel/boot ] || { echo "no 'boot' partition found" >&2; exit 1; }

bootpart=$(readlink -f /dev/disk/by-partlabel/boot)
disk=$(lsblk -no PKNAME "$bootpart" | head -n 1)
[ -n "$disk" ] || { echo "cannot determine parent disk of $bootpart" >&2; exit 1; }

ln -sfn "$disk" /dev/boot

i=1
for label in boot recovery root log persistent overlay; do
	p=$(readlink -f "/dev/disk/by-partlabel/$label" 2>/dev/null)
	if [ -b "$p" ] && ! { [ -e "/dev/boot$i" ] && [ ! -L "/dev/boot$i" ]; }; then
		ln -sfn "$(basename "$p")" "/dev/boot$i"
	fi
	i=$((i + 1))
done
