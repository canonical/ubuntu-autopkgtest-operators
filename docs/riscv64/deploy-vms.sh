#!/bin/sh

set -eu

# Select the U-Boot image explicitly. Image names may not be unique.
image='<image-id>'

for i in $(seq -w 01 12); do
    openstack --os-compute-api-version 2.67 server create \
        --block-device "source_type=image,uuid=$image,destination_type=volume,volume_size=200,volume_type=Ceph_NVMe,boot_index=0,delete_on_termination=true" \
        --flavor '<riscv64-flavor>' \
        --nic 'net-id=<network-id>' \
        --key-name '<key-name>' \
        --security-group default \
        --user-data riscv-userdata.yaml \
        "autopkgtest-remote-$i"
done
