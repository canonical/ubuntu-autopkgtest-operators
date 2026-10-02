#!/bin/sh
set -eu

for unit in 10 11; do
    for index in $(seq 1 12); do
        juju run "dispatcher/$unit" set-worker-count \
            arch=riscv64 count=2 index="$index"
    done
done

for unit in 10 11; do
    juju run "dispatcher/$unit" reconcile-worker-units
done
