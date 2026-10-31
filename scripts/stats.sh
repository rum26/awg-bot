#!/usr/bin/env bash
set -euo pipefail

bash /root/awg/manage_amneziawg.sh stats --json 2>/dev/null |
  grep -oP '^\[.*\]$'