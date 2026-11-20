#!/bin/sh
# Rebuild the demo/测试 sample packages offline-capable (busybox debs are cached).
# Produces: samples/image-v1.tar, image-v2.tar, update-v2.tar, three broken
# packages and samples/demo.json.  Requires `ar`, `xz` and (first run only)
# network access to deb.debian.org; the generated tars are then self-contained.
set -eu
cd "$(dirname "$0")/.."
exec python3 -m tools.make_demo --out samples --cache tools/.cache
