#!/usr/bin/env bash
# Put swe inside SWE.app, with a Python of its own.
#
#   app/mac/script/bundle-swe.sh <path/to/SWE.app>
#
# A new Mac has no swe and no Python 3.11, so the app carries both:
#
#   Contents/Resources/swe/bin/swe       a shell shim, what the app runs
#   Contents/Resources/swe/python/       python-build-standalone CPython
#     lib/python3.13/site-packages/swe      this checkout's src/swe
#     lib/python3.13/site-packages/quiver   this checkout's src/quiver shim
#
# swe needs nothing outside the standard library, so the package is copied
# in as source rather than pip-installed: no build backend, no network
# beyond the one Python download, and no console script with an absolute
# shebang that breaks when the app moves. Bytecode is compiled here and the
# shim runs with -B, so Python never writes __pycache__ into the signed
# bundle.
#
# The download is pinned by version and checksum and kept in
# SWE_PYTHON_CACHE (default ~/.swe/cache/build/python), so rebuilds
# don't fetch it again. It matches the host architecture, as the swift build
# does. build-app.sh signs the binaries afterwards.
#
# Licences: the app/mac/Licenses/python-*.txt texts, which bundle-licenses.sh
# copies into Contents/Resources/Licenses, come from the python/licenses/
# folder of the pinned release's full archive (cpython-*-pgo+lto-full.tar.zst; the
# install_only archive used here carries only CPython's). They cover what
# the interpreter links statically (libffi, expat, sqlite, xz, bzip2,
# mpdecimal, libuuid, OpenSSL) and what macOS provides and the interpreter
# loads from /usr/lib (zlib, libedit, ncurses). Tcl/Tk, Tix and the X11
# libraries are trimmed below, so their texts stay out. When `release` or
# `python_version` changes, check that archive's python/licenses/ and
# PYTHON.json (build_info.extensions[*].license_paths) for new libraries,
# `otool -L` the interpreter, and update Licenses/ and THIRD_PARTY_NOTICES.md.
# The Droid decryptor's libcrypto is a separate pin (`openssl_version`
# below); bumping it means re-checking its LICENSE.txt still ships.
set -euo pipefail

bundle="${1:?usage: bundle-swe.sh <path/to/SWE.app>}"
here="$(cd "$(dirname "$0")/.." && pwd)"
repo="$(cd "$here/../.." && pwd)"

release="20261003"
python_version="3.13.16"
case "$(uname -m)" in
  arm64)
    triple="aarch64-apple-darwin"
    sha256="9e01f63bbb08576cd9c8bc2d0564d098cb30c8453a0cd4bcf6aef458f6d2a147"
    ;;
  x86_64)
    triple="x86_64-apple-darwin"
    sha256="b4dad38ba6a344555ccb71a1b08caad0a6c0dda88c5803658bc95bd7f04e9f5c"
    ;;
  *) echo "bundle-swe: unsupported architecture $(uname -m)" >&2; exit 1 ;;
esac
archive="cpython-${python_version}+${release}-${triple}-install_only_stripped.tar.gz"
url="https://github.com/astral-sh/python-build-standalone/releases/download/${release}/${archive//+/%2B}"

cache="${SWE_PYTHON_CACHE:-${QUIVER_PYTHON_CACHE:-$HOME/.swe/cache/build/python}}"
# See install-app.sh: creating the cache under ~/.swe would block the
# ~/.quiver -> ~/.swe move, so it runs here first too.
if [ ! -e "$HOME/.swe" ] && [ ! -L "$HOME/.swe" ] \
    && [ -d "$HOME/.quiver" ] && [ ! -L "$HOME/.quiver" ]; then
  mv "$HOME/.quiver" "$HOME/.swe" 2>/dev/null \
    && ln -s .swe "$HOME/.quiver" 2>/dev/null
fi
mkdir -p "$cache"
tarball="$cache/$archive"
verify() { [ "$(shasum -a 256 "$1" | cut -d' ' -f1)" = "$sha256" ]; }
if [ ! -f "$tarball" ] || ! verify "$tarball"; then
  echo "bundle-swe: downloading Python $python_version ($triple)" >&2
  # A download of its own, renamed into place only once verified: several
  # sessions build at once, and a shared temporary name let one build
  # overwrite or move another's half-written file.
  partial="$(mktemp "$cache/$archive.XXXXXX")"
  if ! curl --fail --location --silent --show-error --retry 3 -o "$partial" "$url"; then
    rm -f "$partial"
    echo "bundle-swe: couldn't download $url" >&2
    exit 1
  fi
  if ! verify "$partial"; then
    rm -f "$partial"
    echo "bundle-swe: checksum mismatch for $archive" >&2
    exit 1
  fi
  mv -f "$partial" "$tarball"
fi

dest="$bundle/Contents/Resources/swe"
rm -rf "$dest"
mkdir -p "$dest/bin"
tar -xzf "$tarball" -C "$dest"   # unpacks to $dest/python

# Trim what swe never uses: Tk, the test suite, pip, headers, the static
# library, the scripts that would carry absolute shebangs, and the
# libpython dylib, which is only for embedding (the interpreter binary has
# Python linked in statically; check with otool -L).
py="$dest/python"
stdlib="$py/lib/python3.13"
rm -rf "$py/include" "$py/share" "$py/lib/pkgconfig" "$py"/lib/libpython* \
       "$py"/lib/itcl* "$py"/lib/tcl* "$py"/lib/tk* "$py"/lib/thread* \
       "$py"/lib/libtcl* "$py"/lib/libtk* \
       "$stdlib"/config-3.13-* "$stdlib/test" "$stdlib/idlelib" \
       "$stdlib/tkinter" "$stdlib/turtledemo" "$stdlib/ensurepip" \
       "$stdlib"/lib-dynload/_tkinter* "$stdlib"/site-packages/pip*
find "$py/bin" -mindepth 1 ! -name 'python3' ! -name 'python3.13' -exec rm -rf {} +
find "$py" -name '__pycache__' -type d -prune -exec rm -rf {} +

# OpenSSL for the Droid auth decryptor. pbs links libcrypto into the
# interpreter statically, and under the app's signature loading an
# outside dylib (e.g. /usr/lib/libcrypto.dylib) aborts the process, so a
# libcrypto that codesign then signs with the app goes in lib/, the only
# place _load_libcrypto looks when SWE_BUNDLED_PYTHON is set.
#
# The dylib is built here from a pinned OpenSSL release, once per
# architecture, and kept in the Python cache. A Mac with no Homebrew
# (the usual case on a machine that only has Nix) used to warn and ship
# the app with Droid decryption off.
openssl_version="3.6.5"
openssl_sha256="a2157c2830efdec3788939b00c9b0638306d3f0bbb76dc4832ee503bb397df98"
openssl_tag="openssl-${openssl_version}"
openssl_archive="${openssl_tag}.tar.gz"
openssl_url="https://github.com/openssl/openssl/releases/download/${openssl_tag}/${openssl_archive}"
# darwin64-arm64-cc / darwin64-x86_64-cc. uname -m, not $triple: the
# Python triple says aarch64, and OpenSSL's Configure does not.
case "$(uname -m)" in
  arm64) openssl_target="darwin64-arm64-cc" ;;
  x86_64) openssl_target="darwin64-x86_64-cc" ;;
  *) echo "bundle-swe: unsupported architecture $(uname -m)" >&2; exit 1 ;;
esac
libcrypto="${SWE_LIBCRYPTO:-${QUIVER_LIBCRYPTO:-$cache/libcrypto-${openssl_version}-${openssl_target}.dylib}}"
license="${SWE_LIBCRYPTO_LICENSE:-${QUIVER_LIBCRYPTO_LICENSE:-$cache/openssl-${openssl_version}-LICENSE.txt}}"
if [ -z "${SWE_LIBCRYPTO:-${QUIVER_LIBCRYPTO:-}}" ]; then
  if [ ! -f "$libcrypto" ] || [ ! -f "$license" ]; then
    echo "bundle-swe: building OpenSSL $openssl_version ($openssl_target)" >&2
    src_tar="$cache/$openssl_archive"
    verify_openssl() { [ "$(shasum -a 256 "$1" | cut -d' ' -f1)" = "$openssl_sha256" ]; }
    if [ ! -f "$src_tar" ] || ! verify_openssl "$src_tar"; then
      partial="$(mktemp "$cache/$openssl_archive.XXXXXX")"
      if ! curl --fail --location --silent --show-error --retry 3 -o "$partial" "$openssl_url"; then
        rm -f "$partial"
        echo "bundle-swe: couldn't download $openssl_url" >&2
        exit 1
      fi
      if ! verify_openssl "$partial"; then
        rm -f "$partial"
        echo "bundle-swe: checksum mismatch for $openssl_archive" >&2
        exit 1
      fi
      mv -f "$partial" "$src_tar"
    fi
    work="$(mktemp -d "$cache/openssl-build.XXXXXX")"
    tar -xzf "$src_tar" -C "$work"
    # no-apps: the openssl CLI is not bundled. no-shared is not set, so
    # Configure emits libcrypto.3.dylib. Prefix is discarded; install_name
    # is rewritten to @rpath below so the dylib loads from python/lib.
    # Configure and make print pages of progress; keep that in a log and
    # show the tail only when the build fails.
    build_log="$work/build.log"
    if ! (cd "$work/$openssl_tag" && \
          ./Configure "$openssl_target" no-apps --prefix="$work/prefix" \
            --libdir=lib >"$build_log" 2>&1 && \
          make -j"$(sysctl -n hw.ncpu)" build_libs >>"$build_log" 2>&1 && \
          make install_dev >>"$build_log" 2>&1); then
      echo "bundle-swe: OpenSSL $openssl_version failed to build" >&2
      tail -n 40 "$build_log" >&2 || true
      rm -rf "$work"
      exit 1
    fi
    built="$work/prefix/lib/libcrypto.3.dylib"
    if [ ! -f "$built" ]; then
      rm -rf "$work"
      echo "bundle-swe: OpenSSL built no libcrypto.3.dylib" >&2
      exit 1
    fi
    staged="$(mktemp "$cache/libcrypto.XXXXXX")"
    cp "$built" "$staged"
    chmod 755 "$staged"
    install_name_tool -id "@rpath/libcrypto.3.dylib" "$staged"
    mv -f "$staged" "$libcrypto"
    chmod 644 "$libcrypto"
    cp "$work/$openssl_tag/LICENSE.txt" "$license"
    rm -rf "$work"
  fi
fi
if [ ! -f "$libcrypto" ]; then
  echo "bundle-swe: libcrypto.3.dylib not found at $libcrypto" >&2
  exit 1
fi
if [ ! -f "$license" ]; then
  echo "bundle-swe: no OpenSSL LICENSE.txt at $license; set SWE_LIBCRYPTO_LICENSE" >&2
  exit 1
fi
cp "$libcrypto" "$py/lib/libcrypto.3.dylib"
chmod 755 "$py/lib/libcrypto.3.dylib"
install_name_tool -id "@rpath/libcrypto.3.dylib" "$py/lib/libcrypto.3.dylib"
cp "$license" "$py/lib/OPENSSL-LICENSE.txt"

# The package, without anything a previous run left behind.
site="$stdlib/site-packages"
mkdir -p "$site"
cp -R "$repo/src/swe" "$site/swe"
cp -R "$repo/src/quiver" "$site/quiver"
find "$site/swe" "$site/quiver" -name '__pycache__' -type d -prune -exec rm -rf {} +

# unchecked-hash: the .pyc files are trusted as they are, so nothing
# compares source timestamps or rewrites them at run time.
"$py/bin/python3" -I -m compileall -q -j 0 --invalidation-mode unchecked-hash "$stdlib" >/dev/null

cat > "$dest/bin/swe" <<'SHIM'
#!/bin/sh
# swe as bundled with SWE.app. -I keeps the user's PYTHONPATH and site
# packages out; -B keeps Python from writing into the signed bundle.
root="$(cd "$(dirname "$0")/.." && pwd -P)"
SWE_BUNDLED_PYTHON=1 exec "$root/python/bin/python3" -I -B -c \
  'import sys; sys.argv[0] = "swe"; from swe.cli import main; sys.exit(main())' "$@"
SHIM
chmod 755 "$dest/bin/swe"

# Fails the build if the bundled swe can't start, in a home of its own so
# nothing touches the builder's ~/.swe.
probe_home="$(mktemp -d)"
trap 'rm -rf "$probe_home"' EXIT
if ! out="$(HOME="$probe_home" "$dest/bin/swe" version --json 2>&1)" \
   || ! printf '%s' "$out" | grep -q '"name": *"swe"'; then
  echo "bundle-swe: the bundled swe didn't start: $out" >&2
  exit 1
fi
# When a libcrypto was bundled, the decryptor must actually load it —
# loading the wrong one crashes this interpreter, so probe now.
if [ -n "$libcrypto" ] && ! SWE_BUNDLED_PYTHON=1 "$py/bin/python3" -I -B -c \
    'import sys; sys.path.insert(0, sys.argv[1]); \
     from swe.harness.rate_limits import _load_libcrypto; \
     sys.exit(0 if _load_libcrypto() is not None else 1)' "$site"; then
  echo "bundle-swe: the bundled libcrypto did not load" >&2
  exit 1
fi
