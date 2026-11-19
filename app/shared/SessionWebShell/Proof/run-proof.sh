#!/bin/sh
# Runs the G2 proof: starts the stub hub on 127.0.0.1, generates the
# harness project, runs its UI tests on an iPhone simulator, and leaves
# screenshots and timing JSON in the output folder.
#
#   Proof/run-proof.sh [output-dir] [simulator name]
#
# Defaults: Proof/build/proof-output and "iPhone 18 Pro". A relative
# output folder is taken from where you run the script. The simulator is
# booted if needed and left booted; shut it down with
# `xcrun simctl shutdown "<name>"` when done.
set -eu
here="$(cd "$(dirname "$0")" && pwd)"
out="${1:-$here/build/proof-output}"
device="${2:-iPhone 18 Pro}"
mkdir -p "$out"
# Absolute before the cd below, so the stub's log and the test runner's
# output land where the caller asked.
out="$(cd "$out" && pwd)"
cd "$here"

python3 stub_hub.py --port 8765 --control 8766 2>"$out/stub-hub.log" &
stub=$!
trap 'kill "$stub" 2>/dev/null || true' EXIT INT TERM

xcodegen generate --quiet
rm -f "$out"/*.png "$out"/*.json
xcrun simctl boot "$device" 2>/dev/null || true
xcrun simctl bootstatus "$device" -b >/dev/null

rm -rf "$here/build/ShellProof.xcresult"
TEST_RUNNER_PROOF_SHOT_DIR="$out" xcodebuild test \
  -project ShellProof.xcodeproj -scheme ShellProof \
  -destination "platform=iOS Simulator,name=$device" \
  -derivedDataPath "$here/build/DerivedData" \
  -resultBundlePath "$here/build/ShellProof.xcresult" \
  -collect-test-diagnostics never \
  2>&1 | tee "$out/xcodebuild.log" | grep -E "Test Case|error:|failed|passed|\*\* TEST"
status=$(grep -c '\*\* TEST SUCCEEDED \*\*' "$out/xcodebuild.log" || true)
echo "screenshots and numbers: $out"
test "$status" -ge 1
