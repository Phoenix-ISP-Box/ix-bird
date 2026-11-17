#!/bin/sh
# Parse the generated configuration with a real BIRD 2 (no daemon started): bird -p.
# Usage: tests/bird-parse.sh [bird-binary]   (needs: python3 + pyyaml, bird 2.x)
set -eu
BIRD=${1:-bird}
ROOT=$(cd "$(dirname "$0")/.." && pwd)
T=$(mktemp -d)
trap 'rm -rf "$T"' EXIT
python3 "$ROOT/tools/ix-bird-gen" --services "$ROOT/examples/services.yaml" --out "$T/out" "$ROOT"/examples/customers/*.yaml >/dev/null
mkdir -p "$T/customers"
for f in "$T"/out/*.bird; do cp "$f" "$T/customers/$(basename "$f" .bird).conf"; done
echo "# keeps the include glob non-empty" > "$T/customers/00_placeholder.conf"
cat > "$T/bird.conf" <<CONF
router id 192.0.2.254;
protocol device { }
include "$T/out/common.conf";
include "$T/customers/*.conf";
CONF
"$BIRD" -p -c "$T/bird.conf"
echo "bird-parse: OK ($("$BIRD" --version 2>&1 | head -1))"
# and with no customers at all (fresh router)
rm "$T"/customers/[!0]*.conf
"$BIRD" -p -c "$T/bird.conf"
echo "bird-parse: OK with no customers"
