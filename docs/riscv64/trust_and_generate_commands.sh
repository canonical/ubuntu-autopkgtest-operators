#!/bin/sh

# Adapt dispatcher unit numbers to the orchestrator deployment.
targets="janitor/leader dispatcher/10 dispatcher/11 dispatcher/12"

case "$#:${1:-}" in
    0:|1:--apply-trust) ;;
    *)
        printf 'Usage: %s [--apply-trust]\n' "$0" >&2
        exit 2
        ;;
esac

trust_name() {
    case "$1" in
        janitor/leader) printf 'janitor\n' ;;
        dispatcher/*) printf 'dispatcher-%s\n' "${1#*/}" ;;
        *)
            printf 'unsupported target: %s\n' "$1" >&2
            return 1
            ;;
    esac
}

# shellcheck disable=SC2013 # ssh would consume stdin in a while-read loop
for host in $(cat ip); do
    hostname="$(ssh "$host" hostname)" || continue
    index="$(printf '%s\n' "$hostname" | awk -F '-' '{print $3 + 0}')"

    if [ "${1:-}" = "--apply-trust" ]; then
        for target in $targets; do
            name="$(trust_name "$target")" || exit 2
            # shellcheck disable=SC2029 # $name is meant to expand locally
            if ! ssh "$host" "lxc config trust add --name $name" >/dev/null; then
                printf 'failed to create %s token for %s\n' "$name" "$host" >&2
                continue 2
            fi
        done
    fi

    output="$(ssh "$host" lxc config trust list-tokens -c nt -f csv)" ||
        continue

    for target in $targets; do
        name="$(trust_name "$target")" || exit 2

        token="$(printf '%s\n' "$output" |
            awk -F ',' -v name="$name" '
                $1 == name { token = $NF; count++ }
                END {
                    if (count > 1) exit 1
                    if (count == 1) print token
                }
            ')" || {
                printf 'multiple tokens for %s on %s\n' "$name" "$host" >&2
                continue
            }

        if [ -z "$token" ]; then
            printf 'no token for %s on %s\n' "$name" "$host" >&2
            continue
        fi

        printf 'juju run %s add-remote arch=riscv64 index=%s token=%s\n' \
            "$target" "$index" "$token"
    done
done
