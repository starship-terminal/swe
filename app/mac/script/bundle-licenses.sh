#!/usr/bin/env bash
# Copy the licence texts SWE.app has to carry into Contents/Resources/Licenses.
#
#   app/mac/script/bundle-licenses.sh <path/to/SWE.app>
#   app/mac/script/bundle-licenses.sh --list      # the file names, one a line
#
# Licenses/python-*.txt are the texts python-build-standalone publishes for
# the pinned CPython (bundle-swe.sh's header says which libraries they cover
# and how to refresh them); sparkle.txt is Sparkle's LICENSE from its SwiftPM
# artifact, which also covers the components Sparkle bundles.
# THIRD_PARTY_NOTICES.md names each file. The list below is the one place
# that says what must ship: a file missing from Licenses/ or empty fails the
# run, so a deleted or renamed text cannot slip out unnoticed.
#
# Overrides, for tests: SWE_LICENSES_SRC (default app/mac/Licenses) and
# SWE_SPARKLE_LICENSE (default .build/artifacts/sparkle/Sparkle/LICENSE).
set -euo pipefail

here="$(cd "$(dirname "$0")/.." && pwd)"

python_texts="python-bzip2.txt python-cpython.txt python-expat.txt
python-libedit.txt python-libffi.txt python-liblzma.txt python-libuuid.txt
python-mpdecimal.txt python-ncurses.txt python-openssl-1.1.txt
python-openssl-3.txt python-sqlite.txt python-zlib.txt zstd.txt"

if [ "${1:-}" = "--list" ]; then
  printf '%s\n' $python_texts sparkle.txt
  exit 0
fi

bundle="${1:?usage: bundle-licenses.sh <path/to/SWE.app> | --list}"
src="${SWE_LICENSES_SRC:-$here/Licenses}"
sparkle="${SWE_SPARKLE_LICENSE:-$here/.build/artifacts/sparkle/Sparkle/LICENSE}"
dest="$bundle/Contents/Resources/Licenses"

mkdir -p "$dest"
for name in $python_texts; do
  if [ ! -s "$src/$name" ]; then
    echo "bundle-licenses: $src/$name missing or empty" >&2
    exit 1
  fi
  cp "$src/$name" "$dest/$name"
done
if [ ! -s "$sparkle" ]; then
  echo "bundle-licenses: $sparkle missing or empty; run swift build first" >&2
  exit 1
fi
cp "$sparkle" "$dest/sparkle.txt"
