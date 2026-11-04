#!/usr/bin/env bash
set -euo pipefail

name="$1"

if ! [[ "$name" =~ ^[A-Za-z0-9_-]{1,32}$ ]]; then
    echo "Ошибка: недопустимое имя."
    exit 1
fi

exec bash /root/awg/manage_amneziawg.sh remove "$name" --yes