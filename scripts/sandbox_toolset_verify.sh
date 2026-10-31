#!/usr/bin/env bash
# Run every probe of the sandbox toolset manifest inside a built sandbox image,
# under the sandbox runner's container limits (read-only root, every
# capability dropped, 512m memory, tmpfs home/workdir, the disk-backed
# install volume, no network), as the image's unprivileged sandbox user.
#
#   scripts/sandbox_toolset_verify.sh sandbox-skill:latest
#
# Exits non-zero when any tool the manifest promises is missing or broken, or
# when the baked built-in Skills (/opt/manor/skills) are missing or altered.
set -euo pipefail

image="${1:?usage: $0 <sandbox image>}"

exec docker run --rm --pull=never \
  --read-only --cap-drop ALL --security-opt no-new-privileges \
  --memory 512m --pids-limit 256 --network none \
  --tmpfs /tmp:exec --tmpfs /var/tmp \
  --tmpfs /skill:exec,size=256m,mode=1777 \
  --tmpfs /root:exec,size=128m \
  --tmpfs /home/sandbox:exec,size=512m,uid=1000,gid=1000,mode=0755 \
  --mount type=volume,dst=/opt/sandbox-local \
  --workdir /skill \
  "$image" python3 /opt/manor/toolset/sandbox_toolset.py verify
