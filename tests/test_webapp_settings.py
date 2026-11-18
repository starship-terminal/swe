"""The Settings page: its QR encoder, its data, and its pair/revoke writes."""

import json
import re
import shutil
import struct
import subprocess
import tempfile
import threading
import unittest
import xml.etree.ElementTree as ET
import zlib
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from swe.github import gh
from swe.remote import pairing
from swe.remote.hub import save_hub_config
from swe.webapp.pages import PageContext, PageError, _qr
from swe.webapp.pages import settings as settings_page

from tests.test_remote_hub import HubBase
from tests.test_webapp import app_js_function

# ---------------------------------------------------------------- QR helpers

# "HELLO" at level M, version 1, mask 4: decoded by zbarimg when it was
# recorded, and kept so a change to the encoder shows up without one.
HELLO_M = [
    "#######.##.#..#######",
    "#.....#..##.#.#.....#",
    "#.###.#..####.#.###.#",
    "#.###.#.#..#..#.###.#",
    "#.###.#.#...#.#.###.#",
    "#.....#.#.##..#.....#",
    "#######.#.#.#.#######",
    "........#####........",
    "#...#.######.#####..#",
    "...###..#.###..#.####",
    "#.##..#.#.##..###..#.",
    "###..#...#...##.#....",
    "..#.###..#..###...##.",
    "........###.###..#.##",
    "#######.##..##...#.#.",
    "#.....#....##..#...#.",
    "#.###.#.#..#..###.#.#",
    "#.###.#....##....#.##",
    "#.###.#..###..####...",
    "#.....#..#...##......",
    "#######.#...#####.#.#",
]

PAIR_URL = "https://home-server.tail0000.ts.net:8477/#pair=0Ol_Evny-OSYVP3QLNEl6GxapePaqECo"
DEEP_LINK = ("quiver://pair?host=home-server.tail0000.ts.net&port=8477"
             "&token=oiXzLUI8gplt6g-ofdf9jvOSUE9C6eZz&scheme=https")

# Format information, by (level, mask), from ISO/IEC 18004 table C.1,
# written out here rather than computed, so the encoder's BCH code is
# checked against the standard's own values.
_FORMAT_LEVEL_BITS = {_qr.LOW: 1, _qr.MEDIUM: 0, _qr.QUARTILE: 3, _qr.HIGH: 2}


def _format_word(ecl, mask):
    data = _FORMAT_LEVEL_BITS[ecl] << 3 | mask
    rem = data
    for _ in range(10):
        rem = (rem << 1) ^ ((rem >> 9) * 0x537)
    return ((data << 10) | rem) ^ 0x5412


# Alignment pattern centres per version, ISO/IEC 18004 Annex E, typed
# in from the standard rather than computed.
ALIGNMENT = {
    1: [], 2: [6, 18], 3: [6, 22], 4: [6, 26], 5: [6, 30], 6: [6, 34],
    7: [6, 22, 38], 8: [6, 24, 42], 9: [6, 26, 46], 10: [6, 28, 50],
    11: [6, 30, 54], 12: [6, 32, 58], 13: [6, 34, 62], 14: [6, 26, 46, 66],
    15: [6, 26, 48, 70], 16: [6, 26, 50, 74], 17: [6, 30, 54, 78], 18: [6, 30, 56, 82],
    19: [6, 30, 58, 86], 20: [6, 34, 62, 90], 21: [6, 28, 50, 72, 94],
    22: [6, 26, 50, 74, 98], 23: [6, 30, 54, 78, 102], 24: [6, 28, 54, 80, 106],
    25: [6, 32, 58, 84, 110], 26: [6, 30, 58, 86, 114], 27: [6, 34, 62, 90, 118],
    28: [6, 26, 50, 74, 98, 122], 29: [6, 30, 54, 78, 102, 126],
    30: [6, 26, 52, 78, 104, 130], 31: [6, 30, 56, 82, 108, 134],
    32: [6, 34, 60, 86, 112, 138], 33: [6, 30, 58, 86, 114, 142],
    34: [6, 34, 62, 90, 118, 146], 35: [6, 30, 54, 78, 102, 126, 150],
    36: [6, 24, 50, 76, 102, 128, 154], 37: [6, 28, 54, 80, 106, 132, 158],
    38: [6, 32, 58, 84, 110, 136, 162], 39: [6, 26, 54, 82, 110, 138, 166],
    40: [6, 30, 58, 86, 114, 142, 170],
}

# The eight data masks in the standard's own terms, i the row and j the
# column (table 10), written apart from the encoder's (x, y) lambdas.
MASKS = {
    0: lambda i, j: (i + j) % 2 == 0,
    1: lambda i, j: i % 2 == 0,
    2: lambda i, j: j % 3 == 0,
    3: lambda i, j: (i + j) % 3 == 0,
    4: lambda i, j: (i // 2 + j // 3) % 2 == 0,
    5: lambda i, j: (i * j) % 2 + (i * j) % 3 == 0,
    6: lambda i, j: ((i * j) % 2 + (i * j) % 3) % 2 == 0,
    7: lambda i, j: ((i * j) % 3 + (i + j) % 2) % 2 == 0,
}

# Byte-mode capacity (characters) from the standard's table 7, a sample
# that spans every block layout the tables hold.
BYTE_CAPACITY = {
    (1, "L"): 17, (1, "M"): 14, (1, "Q"): 11, (1, "H"): 7,
    (2, "M"): 26, (3, "M"): 42, (4, "M"): 62, (5, "Q"): 60, (7, "H"): 64,
    (10, "M"): 213, (15, "L"): 520, (20, "L"): 858, (20, "M"): 666, (20, "Q"): 482,
    (20, "H"): 382, (40, "L"): 2953, (40, "M"): 2331, (40, "Q"): 1663, (40, "H"): 1273,
}


def _function_map(version):
    """Which modules are function patterns, from the standard's layout:
    finders with their separators and format areas, timing, alignment
    patterns, the dark module and the version blocks."""
    size = version * 4 + 17
    func = [[False] * size for _ in range(size)]

    def mark(r0, r1, c0, c1):
        for r in range(max(r0, 0), min(r1, size)):
            for c in range(max(c0, 0), min(c1, size)):
                func[r][c] = True

    mark(0, 9, 0, 9)              # top-left finder, separator, format bits
    mark(0, 9, size - 8, size)    # top-right
    mark(size - 8, size, 0, 9)    # bottom-left (with the dark module)
    mark(6, 7, 0, size)           # timing row
    mark(0, size, 6, 7)           # timing column
    centres = ALIGNMENT[version]
    corners = {(centres[0], centres[0]), (centres[0], centres[-1]), (centres[-1], centres[0])} if centres else set()
    for r in centres:
        for c in centres:
            if (r, c) not in corners:  # those three sit on the finders
                mark(r - 2, r + 3, c - 2, c + 3)
    if version >= 7:
        mark(0, 6, size - 11, size - 8)
        mark(size - 11, size - 8, 0, 6)
    return func


# GF(256) tables built here, apart from the encoder's multiply routine.
_EXP = [0] * 512
_LOG = [0] * 256
_x = 1
for _i in range(255):
    _EXP[_i] = _x
    _LOG[_x] = _i
    _x <<= 1
    if _x & 0x100:
        _x ^= 0x11D
for _i in range(255, 512):
    _EXP[_i] = _EXP[_i - 255]


def _syndromes(block, nsym):
    """Each syndrome of a received block; all zero for a valid codeword."""
    out = []
    for i in range(nsym):
        acc = 0
        for b in block:
            acc = (_EXP[_LOG[acc] + i] if acc else 0) ^ b
        out.append(acc)
    return out


def _decode(modules):
    """A small reader, apart from the encoder's drawing code (its layout,
    masks and Galois field are this file's own): read the
    format bits, unmask, collect codewords, de-interleave the blocks,
    check every block's Reed-Solomon syndromes and return the bytes of a
    single byte-mode segment, with the level and mask it found."""
    size = len(modules)
    version = (size - 17) // 4
    bits = 0
    # First copy of the format word, around the top-left finder.
    coords = [(8, i) for i in range(6)] + [(8, 7), (8, 8), (7, 8)] + [(14 - i, 8) for i in range(9, 15)]
    for i, (x, y) in enumerate(coords):
        bits |= int(modules[y][x]) << i
    found = [(e, m) for e in range(4) for m in range(8) if _format_word(e, m) == bits]
    assert len(found) == 1, f"format bits {bits:015b} match nothing"
    ecl, mask = found[0]
    # Second copy must agree.
    bits2 = 0
    coords2 = [(size - 1 - i, 8) for i in range(8)] + [(8, size - 15 + i) for i in range(8, 15)]
    for i, (x, y) in enumerate(coords2):
        bits2 |= int(modules[y][x]) << i
    assert bits2 == bits, "the two format copies differ"

    func = _function_map(version)
    raw = []
    for right in range(size - 1, 0, -2):
        if right <= 6:
            right -= 1
        for vert in range(size):
            for j in range(2):
                x = right - j
                y = (size - 1 - vert) if ((right + 1) & 2) == 0 else vert
                if not func[y][x]:
                    raw.append(int(modules[y][x]) ^ int(MASKS[mask](y, x)))
    codewords = [int("".join(map(str, raw[i:i + 8])), 2) for i in range(0, len(raw) // 8 * 8, 8)]

    nblocks = _qr._NUM_ERROR_CORRECTION_BLOCKS[ecl][version]
    necc = _qr._ECC_CODEWORDS_PER_BLOCK[ecl][version]
    total = _qr._get_num_raw_data_modules(version) // 8
    short = nblocks - total % nblocks
    shortlen = total // nblocks - necc  # data bytes in a short block
    lens = [shortlen + (0 if b < short else 1) for b in range(nblocks)]
    blocks = [[] for _ in range(nblocks)]
    k = 0
    for i in range(max(lens)):
        for b in range(nblocks):
            if i < lens[b]:
                blocks[b].append(codewords[k])
                k += 1
    for i in range(necc):
        for b in range(nblocks):
            blocks[b].append(codewords[k])
            k += 1
    data = []
    for b, blk in enumerate(blocks):
        assert _syndromes(blk, necc) == [0] * necc, f"block {b} fails its RS check"
        data += blk[:lens[b]]
    bitstr = "".join(f"{b:08b}" for b in data)
    assert bitstr[:4] == "0100", "not a byte-mode segment"
    width = 8 if version <= 9 else 16
    n = int(bitstr[4:4 + width], 2)
    start = 4 + width
    payload = bytes(int(bitstr[start + 8 * i:start + 8 * i + 8], 2) for i in range(n))
    return payload, ecl, mask


def _svg_modules(svg):
    """The module grid an SVG from ``to_svg`` draws, read from its path."""
    root = ET.fromstring(svg)
    ns = "{http://www.w3.org/2000/svg}"
    dim = int(root.get("viewBox").split()[2])
    grid = [[False] * dim for _ in range(dim)]
    path = root.find(f"{ns}path").get("d")
    for x, y in re.findall(r"M(\d+),(\d+)h1v1h-1z", path):
        grid[int(y)][int(x)] = True
    return grid


def _png(grid, path, scale=4):
    """A greyscale PNG of ``grid`` (border already included), stdlib only."""
    dim = len(grid) * scale
    raw = bytearray()
    for py in range(dim):
        raw.append(0)
        row = grid[py // scale]
        for px in range(dim):
            raw.append(0 if row[px // scale] else 255)

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    Path(path).write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", dim, dim, 8, 0, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(bytes(raw)))
        + chunk(b"IEND", b""))


# ---------------------------------------------------------------- QR tests


class TestQrEncoder(unittest.TestCase):
    def test_known_pattern_for_hello(self):
        q = _qr.encode_text("HELLO", _qr.MEDIUM, boost_ecl=False)
        self.assertEqual((q.version, q.mask), (1, 4))
        drawn = ["".join("#" if m else "." for m in row) for row in q.modules]
        self.assertEqual(drawn, HELLO_M)

    def test_alignment_positions_match_the_standard(self):
        for version, want in ALIGNMENT.items():
            q = _qr.QrCode.__new__(_qr.QrCode)
            q.version, q.size = version, version * 4 + 17
            self.assertEqual(q._get_alignment_pattern_positions(), want, version)

    def test_masks_match_the_standard(self):
        for mask, rule in MASKS.items():
            for i in range(30):
                for j in range(30):
                    self.assertEqual(_qr._MASK_PATTERNS[mask](j, i), rule(i, j), (mask, i, j))

    def test_byte_capacity_matches_the_standard(self):
        for (version, level), chars in BYTE_CAPACITY.items():
            ecl = _qr.LEVELS[level]
            q = _qr.encode_bytes(b"a" * chars, ecl, boost_ecl=False, min_version=version, max_version=version)
            self.assertEqual((q.version, q.ecl), (version, ecl))
            with self.assertRaises(_qr.DataTooLong, msg=(version, level)):
                _qr.encode_bytes(b"a" * (chars + 1), ecl, boost_ecl=False,
                                 min_version=version, max_version=version)

    def test_function_patterns_are_where_the_standard_puts_them(self):
        for version in (1, 2, 7, 14, 25, 40):
            q = _qr.QrCode.__new__(_qr.QrCode)
            q.version, q.size, q.ecl = version, version * 4 + 17, _qr.MEDIUM
            q.modules = [[False] * q.size for _ in range(q.size)]
            q._isfunction = [[False] * q.size for _ in range(q.size)]
            q._draw_function_patterns()
            self.assertEqual(q._isfunction, _function_map(version), version)
            for r in ALIGNMENT[version]:
                for c in ALIGNMENT[version]:
                    if (r, c) in ((6, 6), (6, q.size - 7), (q.size - 7, 6)):
                        continue
                    self.assertTrue(q.modules[r][c])           # centre dark
                    self.assertFalse(q.modules[r - 1][c])      # light ring
                    self.assertTrue(q.modules[r - 2][c - 2])   # dark border

    def test_finders_timing_and_dark_module(self):
        q = _qr.encode_text(PAIR_URL)
        n = q.size
        finder = ["#######", "#.....#", "#.###.#", "#.###.#", "#.###.#", "#.....#", "#######"]
        for ox, oy in ((0, 0), (n - 7, 0), (0, n - 7)):
            got = ["".join("#" if q.modules[oy + y][ox + x] else "." for x in range(7)) for y in range(7)]
            self.assertEqual(got, finder, (ox, oy))
        for i in range(8, n - 8):
            self.assertEqual(q.modules[6][i], i % 2 == 0)
            self.assertEqual(q.modules[i][6], i % 2 == 0)
        self.assertTrue(q.modules[n - 8][8])

    def test_version_information_on_large_codes(self):
        q = _qr.encode_bytes(b"x" * 300, _qr.MEDIUM)
        self.assertGreaterEqual(q.version, 7)
        bits = 0
        for i in range(18):
            bits |= int(q.modules[i // 3][q.size - 11 + i % 3]) << i
        self.assertEqual(bits >> 12, q.version)
        # The 12 check bits make the 18-bit word divisible by the BCH generator.
        rem = bits
        for shift in range(17, 11, -1):
            if rem >> shift & 1:
                rem ^= 0x1F25 << (shift - 12)
        self.assertEqual(rem, 0)

    def test_reads_back_at_every_level_and_across_versions(self):
        for ecl in (_qr.LOW, _qr.MEDIUM, _qr.QUARTILE, _qr.HIGH):
            for n in (1, 17, 40, 120, 400, 1200):
                data = bytes((i * 37 + n) % 256 for i in range(n))
                try:
                    q = _qr.encode_bytes(data, ecl, boost_ecl=False)
                except _qr.DataTooLong:
                    continue
                payload, got_ecl, got_mask = _decode(q.modules)
                self.assertEqual(payload, data, (ecl, n, q.version))
                self.assertEqual((got_ecl, got_mask), (ecl, q.mask))

    def test_every_mask_reads_back(self):
        for mask in range(8):
            q = _qr.encode_text(DEEP_LINK, mask=mask)
            self.assertEqual(_decode(q.modules)[0], DEEP_LINK.encode())

    def test_level_is_boosted_while_the_version_holds(self):
        q = _qr.encode_text("HELLO", _qr.MEDIUM)
        self.assertEqual((q.version, q.ecl), (1, _qr.HIGH))

    def test_too_long_is_refused(self):
        with self.assertRaises(_qr.DataTooLong):
            _qr.encode_bytes(b"x" * 3000, _qr.HIGH)

    def test_svg_draws_the_modules_with_a_quiet_zone(self):
        q = _qr.encode_text(PAIR_URL)
        grid = _svg_modules(q.to_svg(border=4))
        self.assertEqual(len(grid), q.size + 8)
        self.assertFalse(any(grid[0]) or any(row[0] for row in grid))
        inner = [row[4:-4] for row in grid[4:-4]]
        self.assertEqual(inner, q.modules)
        self.assertEqual(_decode(inner)[0], PAIR_URL.encode())

    def test_svg_label_is_escaped(self):
        svg = _qr.svg("x", label='a "b" <c>')
        self.assertIn('aria-label="a &quot;b&quot; &lt;c&gt;"', svg)
        ET.fromstring(svg)

    @unittest.skipUnless(shutil.which("zbarimg"), "zbarimg is not installed")
    def test_zbarimg_reads_the_svg(self):
        with tempfile.TemporaryDirectory() as tmp:
            for text in (PAIR_URL, DEEP_LINK, "HELLO", "x" * 500):
                png = Path(tmp) / "qr.png"
                _png(_svg_modules(_qr.svg(text)), png)
                out = subprocess.run(["zbarimg", "-q", "--raw", str(png)],
                                     capture_output=True, text=True, timeout=30)
                self.assertEqual(out.stdout.rstrip("\n"), text)


# ---------------------------------------------------------------- page data


class _Settings(HubBase):
    def setUp(self):
        super().setUp()
        self.home = self.srv.home
        patcher = patch("swe.remote.catalog.tailscale_ip", return_value="100.64.0.7")
        patcher.start()
        self.addCleanup(patcher.stop)

    def ctx(self, device_id=None):
        return PageContext(home=self.home, state=self.srv.state, device_id=device_id)

    def device(self, name):
        token = pairing.mint_pair_token(self.home)
        return pairing.exchange(self.home, token, name)

    def origin(self):
        return f"http://127.0.0.1:{self.srv.port}"

    def post(self, action, body, cred, origin=True):
        headers = {"Authorization": f"Bearer {cred['credential']}", "Content-Type": "application/json"}
        if origin:
            headers["Origin"] = self.origin()
        status, data = self.srv.request("POST", f"/v1/app/settings/{action}", body=body, headers=headers)
        return status, json.loads(data)


class TestGet(_Settings):
    def test_needs_a_device(self):
        self.assertEqual(self.srv.request("GET", "/v1/app/settings")[0], 401)

    def test_remote_lists_devices_and_marks_this_one(self):
        me = self.device("laptop")
        phone = self.device("phone")
        status, data = self.srv.request("GET", "/v1/app/settings",
                                        headers={"Authorization": f"Bearer {me['credential']}"})
        self.assertEqual(status, 200, data)
        body = json.loads(data)
        rows = {d["name"]: d for d in body["remote"]["devices"]}
        self.assertTrue(rows["laptop"]["this_device"])
        self.assertFalse(rows["phone"]["this_device"])
        self.assertEqual(rows["phone"]["id"], phone["device_id"])
        self.assertNotIn("credential_hash", rows["phone"])
        self.assertTrue(body["remote"]["hub"]["ok"])
        self.assertEqual(body["remote"]["token_ttl_s"], pairing.TOKEN_TTL_S)
        self.assertEqual(body["machine"], self.srv.state.machine["name"])

    def test_remote_devices_carry_display_name_and_meta(self):
        # A rename (or pair-time metadata) saved on the phone must show
        # in this list too — it once sent only the original name.
        phone = self.device("phone")
        pairing.update_device(
            self.home, phone["device_id"],
            display_name="Dev's iPhone", kind="iphone", os="iOS 18.5",
            node_name="test-iphone.ts.net")
        laptop = self.device("laptop")
        status, data = self.srv.request(
            "GET", "/v1/app/settings",
            headers={"Authorization": f"Bearer {laptop['credential']}"})
        self.assertEqual(status, 200, data)
        rows = {
            d["id"]: d
            for d in json.loads(data)["remote"]["devices"]
        }
        row = rows[phone["device_id"]]
        self.assertEqual(row["display_name"], "Dev's iPhone")
        self.assertEqual(row["kind"], "iphone")
        self.assertEqual(row["os"], "iOS 18.5")
        self.assertEqual(row["node_name"], "test-iphone.ts.net")
        self.assertNotIn("credential_hash", row)

    def test_remote_setup_is_its_own_part_and_reads_this_machine(self):
        from swe.remote import setup

        made_up = setup.Facts(platform="darwin", exe=None, status=None)
        seen = {}

        def fake_gather(home=None, **_kw):
            seen["home"] = home
            return made_up

        with patch.object(setup, "gather", fake_gather):
            body = settings_page.get(self.ctx(), {"part": "remote_setup"})
        report = body["remote_setup"]
        self.assertEqual(seen["home"], self.home)
        self.assertFalse(report["ready"])
        self.assertEqual(report["steps"][0]["id"], "tailscale.installed")
        self.assertEqual(report["steps"][0]["state"], setup.NEEDS_YOU)
        self.assertNotIn("remote_setup", settings_page.get(self.ctx(), {}), "the main read stays quick")

    def test_hub_address_comes_from_the_saved_config(self):
        save_hub_config(self.home, "127.0.0.1", 8477, "https://box.example.ts.net:8477")
        remote = settings_page.get(self.ctx(), {})["remote"]
        self.assertEqual(remote["hub_addr"], "127.0.0.1:8477")
        self.assertEqual(remote["public_url"], "https://box.example.ts.net:8477")

    def test_pending_requests_carry_no_code_or_secret(self):
        pairing.request_pair(self.home, {"node_id": "n1", "node_name": "pixel", "login": "me@x"}, "pixel")
        rows = settings_page.get(self.ctx(), {})["remote"]["requests"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["node_name"], "pixel")
        self.assertFalse({"code", "poll_secret", "poll_secret_hash"} & set(rows[0]))

    def test_providers_are_masked_and_nothing_is_written(self):
        keys = self.home / ".swe" / "secrets" / "providers" / ".api_keys"
        keys.parent.mkdir(parents=True)
        secret = "sk-proj-SECRETSECRETSECRET9876"
        keys.write_text(f"# OpenAI\nexport OPENAI_API_KEY={secret}\nexport EMPTY_KEY=\"\"\n")
        body = settings_page.get(self.ctx(), {})
        self.assertEqual(body["providers"]["keys"], [
            {"name": "OPENAI_API_KEY", "provider": "OpenAI", "has_value": True, "masked": "sk-proj-••••••••9876"},
            {"name": "EMPTY_KEY", "provider": "OpenAI", "has_value": False, "masked": ""},
        ])
        # "SECRET" alone can't be the needle: LANGFUSE_SECRET_KEY is an
        # env var name, not a value. SECRETSECRET only lives in the key.
        self.assertNotIn("SECRETSECRET", json.dumps(body))
        self.assertEqual(body["providers"]["path"], "~/.swe/secrets/providers/.api_keys")
        self.assertRegex(body["providers"]["stamp"], r"^[0-9a-f]{64}$")
        self.assertFalse((self.home / ".swe" / "config" / "providers.json").exists())
        catalog = {r["id"]: r for r in body["providers"]["catalog"]}
        self.assertEqual(catalog["gemini"]["env_vars"], ["GEMINI_API_KEY", "GOOGLE_API_KEY"])
        self.assertEqual(catalog["anthropic"]["name"], "Anthropic")
        # The catalogue's Set badge: a provider has_key when the key file
        # sets any of its variables. OPENAI_API_KEY is set; EMPTY_KEY is a
        # variable no catalog provider claims.
        self.assertTrue(catalog["openai"]["has_key"])
        self.assertFalse(catalog["anthropic"]["has_key"])

    def test_catalog_has_key_needs_a_value_not_only_the_variable(self):
        keys = self.home / ".swe" / "secrets" / "providers" / ".api_keys"
        keys.parent.mkdir(parents=True)
        keys.write_text("export GEMINI_API_KEY=\nexport ANTHROPIC_API_KEY=\"\"\n")
        catalog = {r["id"]: r for r in settings_page.get(self.ctx(), {})["providers"]["catalog"]}
        self.assertFalse(catalog["gemini"]["has_key"])
        self.assertFalse(catalog["anthropic"]["has_key"])
        keys.write_text("export GOOGLE_API_KEY=abc123\n")
        catalog = {r["id"]: r for r in settings_page.get(self.ctx(), {})["providers"]["catalog"]}
        self.assertTrue(catalog["gemini"]["has_key"])

    def test_providers_follow_providers_json_and_its_removals(self):
        config = self.home / ".swe" / "config"
        config.mkdir(parents=True)
        (config / "providers.json").write_text(json.dumps({
            "acme": {"env_vars": ["ACME_API_KEY"], "url": "https://acme.example/keys"},
            "_removed": ["openai"],
        }))
        before = (config / "providers.json").read_text()
        ids = {r["id"] for r in settings_page.get(self.ctx(), {})["providers"]["catalog"]}
        self.assertIn("acme", ids)
        self.assertNotIn("openai", ids)
        self.assertIn("anthropic", ids)
        self.assertEqual((config / "providers.json").read_text(), before)

    def test_a_non_text_kind_is_rejected_like_the_other_fields(self):
        # providers.json's own validation catches a bad kind first; the
        # row builder refuses it too so a registry change can't smuggle
        # one into the payload.
        with self.assertRaises(TypeError):
            settings_page._catalog_row("acme", {"env_vars": ["A"], "kind": 7})

    def test_the_payload_lists_the_plan_variables_for_the_tab_split(self):
        prov = settings_page.get(self.ctx(), {})["providers"]
        # The page keeps these on Inference however their headings read;
        # the list is the same table test_remote_plan_providers keeps in
        # step with the Mac's InferenceCatalog.all.
        self.assertIn("VERCEL_TOKEN_PASS_KEY", prov["plan_vars"])
        self.assertIn("GLM_CODING_PLAN_KEY", prov["plan_vars"])
        self.assertEqual(prov["plan_vars"], sorted(set(prov["plan_vars"])))

    def test_the_catalog_carries_each_provider_s_kind(self):
        catalog = {r["id"]: r for r in settings_page.get(self.ctx(), {})["providers"]["catalog"]}
        # DEFAULT_INFRA's services land as infra; inference providers and
        # a custom entry with no kind read as inference.
        self.assertEqual(catalog["vercel"]["kind"], "infra")
        self.assertEqual(catalog["vercel"]["env_vars"], ["VERCEL_TOKEN"])
        self.assertEqual(catalog["netlify"]["kind"], "infra")
        self.assertEqual(catalog["openai"]["kind"], "inference")
        self.assertEqual(catalog["agentmail"]["kind"], "services")
        self.assertEqual(catalog["agentmail"]["env_vars"], ["AGENTMAIL_API_KEY"])
        self.assertEqual(catalog["agentmail"]["url"], "https://console.agentmail.to")
        config = self.home / ".swe" / "config"
        config.mkdir(parents=True)
        (config / "providers.json").write_text(json.dumps({
            "acme": {"env_vars": ["ACME_API_KEY"], "url": "https://acme.example/keys"},
            "deploybox": {"env_vars": ["DEPLOYBOX_TOKEN"], "kind": "infra"},
        }))
        catalog = {r["id"]: r for r in settings_page.get(self.ctx(), {})["providers"]["catalog"]}
        self.assertEqual(catalog["acme"]["kind"], "inference")
        self.assertEqual(catalog["deploybox"]["kind"], "infra")
        # A saved infra key marks the service's Set badge too.
        keys = self.home / ".swe" / "secrets" / "providers" / ".api_keys"
        keys.parent.mkdir(parents=True)
        keys.write_text("export DEPLOYBOX_TOKEN=tok123\n")
        catalog = {r["id"]: r for r in settings_page.get(self.ctx(), {})["providers"]["catalog"]}
        self.assertTrue(catalog["deploybox"]["has_key"])

    def test_a_corrupt_providers_json_is_reported_not_raised(self):
        config = self.home / ".swe" / "config"
        config.mkdir(parents=True)
        (config / "providers.json").write_text("{nope")
        prov = settings_page.get(self.ctx(), {})["providers"]
        self.assertEqual(prov["catalog"], [])
        self.assertTrue(prov["error"])

    def test_a_provider_with_bad_field_types_is_reported_not_raised(self):
        config = self.home / ".swe" / "config"
        config.mkdir(parents=True)
        (config / "providers.json").write_text(json.dumps({
            "acme": {"env_vars": "ACME_API_KEY"},
        }))
        body = settings_page.get(self.ctx(), {})
        self.assertEqual(body["providers"]["catalog"], [])
        self.assertIn("Can't read the providers", body["providers"]["error"])
        self.assertTrue(body["remote"]["hub"]["ok"])

    def test_a_keys_file_that_is_not_utf8_is_reported(self):
        keys = self.home / ".swe" / "secrets" / "providers" / ".api_keys"
        keys.parent.mkdir(parents=True)
        keys.write_bytes(b"export A=\xff\n")
        prov = settings_page.get(self.ctx(), {})["providers"]
        self.assertEqual((prov["keys"], prov["stamp"]), ([], None))
        self.assertIn("UTF-8", prov["error"])

    def test_a_keys_file_in_a_symlink_loop_is_reported(self):
        keys = self.home / ".swe" / "secrets" / "providers" / ".api_keys"
        keys.parent.mkdir(parents=True)
        keys.symlink_to(keys.parent / "loop")
        (keys.parent / "loop").symlink_to(keys)
        body = settings_page.get(self.ctx(), {})
        self.assertEqual(body["providers"]["keys"], [])
        self.assertTrue(body["providers"]["error"])
        self.assertTrue(body["remote"]["hub"]["ok"])

    def test_provider_links_are_web_links_only(self):
        config = self.home / ".swe" / "config"
        config.mkdir(parents=True)
        (config / "providers.json").write_text(json.dumps({
            "acme": {"env_vars": ["ACME_API_KEY"], "url": "javascript:alert(1)"},
        }))
        rows = {r["id"]: r for r in settings_page.get(self.ctx(), {})["providers"]["catalog"]}
        self.assertIsNone(rows["acme"]["url"])
        self.assertTrue(rows["openai"]["url"].startswith("https://"))

    def test_github_part(self):
        env = gh.GhEnv(installed=True, authenticated=True, login="octo")
        with patch("swe.github.shared.probe", return_value=env) as probe:
            body = settings_page.get(self.ctx(), {"part": "github", "refresh": "1"})
        probe.assert_called_once_with(self.home, refresh=True)
        self.assertEqual(body["github"]["env"]["status"], "ok")
        self.assertEqual(body["github"]["env"]["login"], "octo")
        self.assertIsNone(body["github"]["catalog"])

    def test_unknown_part_is_refused(self):
        with self.assertRaises(PageError) as caught:
            settings_page.get(self.ctx(), {"part": "secrets"})
        self.assertEqual(caught.exception.status, 400)


# ---------------------------------------------------------------- writes


class TestPair(_Settings):
    def test_pair_mints_a_token_that_pairs_a_device(self):
        me = self.device("laptop")
        status, body = self.post("pair", {}, me)
        self.assertEqual(status, 200, body)
        t = body["ticket"]
        self.assertEqual((t["host"], t["port"]), ("100.64.0.7", 8477))
        self.assertEqual(t["url"], f"http://100.64.0.7:8477/#pair={t['token']}")
        self.assertTrue(t["deep_link"].startswith("quiver://pair?host=100.64.0.7&port=8477&token="))
        self.assertEqual(t["expires_in_s"], pairing.TOKEN_TTL_S)
        # The QR codes carry exactly the two links.
        for key, text in (("web", t["url"]), ("app", t["deep_link"])):
            grid = _svg_modules(t["qr"][key])
            self.assertEqual(_decode([row[4:-4] for row in grid[4:-4]])[0], text.encode())
        # And the token is one the hub honours, once.
        status, data = self.srv.request("POST", "/v1/pair", {"token": t["token"], "name": "phone"})
        self.assertEqual(status, 200, data)
        status, _ = self.srv.request("POST", "/v1/pair", {"token": t["token"], "name": "again"})
        self.assertEqual(status, 403)

    def test_pair_links_use_the_public_url_behind_serve(self):
        save_hub_config(self.home, "127.0.0.1", 8477, "https://box.example.ts.net:8477")
        t = settings_page.post(self.ctx(), "pair", {})["ticket"]
        self.assertEqual(t["url"], f"https://box.example.ts.net:8477/#pair={t['token']}")
        q = parse_qs(urlsplit(t["deep_link"]).query)
        self.assertEqual((q["host"], q["port"], q["scheme"]), (["box.example.ts.net"], ["8477"], ["https"]))

    def test_pair_links_use_the_advertised_address(self):
        self.srv.state.base_url = "http://100.64.0.9:9000"
        t = settings_page.post(self.ctx(), "pair", {})["ticket"]
        self.assertEqual(t["url"], f"http://100.64.0.9:9000/#pair={t['token']}")

    def test_cross_site_and_unpaired_posts_are_refused(self):
        me = self.device("laptop")
        headers = {"Authorization": f"Bearer {me['credential']}", "Origin": "https://evil.example",
                   "Content-Type": "application/json"}
        status, _ = self.srv.request("POST", "/v1/app/settings/pair", body={}, headers=headers)
        self.assertEqual(status, 403)
        status, _ = self.srv.request("POST", "/v1/app/settings/pair", body={})
        self.assertEqual(status, 401)
        # No token was minted: the one device() spent is all there was.
        tokens = json.loads(pairing.tokens_file(self.home).read_text())["tokens"]
        self.assertEqual(tokens, [])

    def test_unknown_action(self):
        me = self.device("laptop")
        status, body = self.post("approve", {}, me)
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "unknown_action")


class TestKeys(_Settings):
    """Providers › Inference writes: add, replace, delete, reveal."""

    SECRET = "sk-ant-api03-SECRETSECRET1234"

    def setUp(self):
        super().setUp()
        self.keys = self.home / ".swe" / "secrets" / "providers" / ".api_keys"
        self.keys.parent.mkdir(parents=True)
        self.keys.write_text("# OpenAI\nexport OPENAI_API_KEY=sk-proj-OLDOLDOLDOLDOLD0000\n")

    def stamp(self):
        return settings_page.get(self.ctx(), {})["providers"]["stamp"]

    def call(self, action, **body):
        return settings_page.post(self.ctx(), action, body)

    def refused(self, action, **body):
        with self.assertRaises(PageError) as caught:
            self.call(action, **body)
        return caught.exception

    def test_add_files_a_new_key_under_its_provider_and_answers_no_value(self):
        reply = self.call("key_set", name="ANTHROPIC_API_KEY", value=f"  {self.SECRET}\n",
                          provider="anthropic", stamp=self.stamp())
        # The catalog in the reply names LANGFUSE_SECRET_KEY, so the
        # needle is the doubled run only the written value carries.
        self.assertNotIn("SECRETSECRET", json.dumps(reply))
        self.assertEqual(self.keys.read_text(), "# OpenAI\nexport OPENAI_API_KEY=sk-proj-OLDOLDOLDOLDOLD0000\n"
                         f'\n# Anthropic\nexport ANTHROPIC_API_KEY="{self.SECRET}"\n')
        self.assertEqual(self.keys.stat().st_mode & 0o777, 0o600)
        names = [k["name"] for k in reply["providers"]["keys"]]
        self.assertEqual(names, ["OPENAI_API_KEY", "ANTHROPIC_API_KEY"])
        backup = self.home / ".swe" / "backups" / "api-keys" / "api_keys.bak"
        self.assertIn("OLDOLD", backup.read_text())
        self.assertEqual(backup.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual((self.home / ".swe" / "secrets" / ".gitignore").read_text().splitlines()[-1], "*")

    def test_replace_keeps_the_line_in_place(self):
        self.keys.write_text("# OpenAI\nexport OPENAI_API_KEY=old # mine\nexport B=1\n")
        self.call("key_set", name="OPENAI_API_KEY", value='new"$', stamp=self.stamp())
        self.assertEqual(self.keys.read_text(), '# OpenAI\nexport OPENAI_API_KEY="new\\"\\$"\nexport B=1\n')

    def test_an_infra_key_files_under_its_service_heading(self):
        reply = self.call("key_set", name="VERCEL_TOKEN", value="vk_secret",
                          provider="vercel", stamp=self.stamp())
        self.assertNotIn("vk_secret", json.dumps(reply))
        self.assertEqual(self.keys.read_text(), "# OpenAI\nexport OPENAI_API_KEY=sk-proj-OLDOLDOLDOLDOLD0000\n"
                         '\n# Vercel\nexport VERCEL_TOKEN="vk_secret"\n')

    def test_a_second_variable_of_a_provider_joins_its_group(self):
        self.keys.write_text("# GEMINI\nexport GEMINI_API_KEY=a\n\n# Other\nexport X=1\n")
        self.call("key_set", name="GOOGLE_API_KEY", value="b", provider="gemini", stamp=self.stamp())
        self.assertEqual(self.keys.read_text(), '# GEMINI\nexport GEMINI_API_KEY=a\nexport GOOGLE_API_KEY="b"\n\n# Other\nexport X=1\n')

    def test_remove_drops_the_line_and_an_empty_heading(self):
        self.call("key_remove", name="OPENAI_API_KEY", stamp=self.stamp())
        self.assertEqual(self.keys.read_text(), "")

    def test_value_answers_only_when_asked(self):
        self.assertEqual(self.call("key_value", name="OPENAI_API_KEY"),
                         {"name": "OPENAI_API_KEY", "value": "sk-proj-OLDOLDOLDOLDOLD0000"})

    def test_a_stale_stamp_is_refused_and_nothing_is_written(self):
        stamp = self.stamp()
        self.keys.write_text("export EDITED=1\n")
        error = self.refused("key_set", name="OPENAI_API_KEY", value="x", stamp=stamp)
        self.assertEqual((error.status, error.code), (409, "changed_on_disk"))
        self.assertEqual(self.keys.read_text(), "export EDITED=1\n")

    def test_refusals_never_echo_a_value(self):
        stamp = self.stamp()
        cases = [
            ("key_set", {"name": "OPENAI_API_KEY", "value": "a\nb" + self.SECRET, "stamp": stamp}, (422, "bad_value")),
            ("key_set", {"name": "OPENAI_API_KEY", "value": "   ", "stamp": stamp}, (422, "bad_value")),
            ("key_set", {"name": "OPENAI_API_KEY", "value": 7, "stamp": stamp}, (422, "bad_value")),
            ("key_set", {"name": "1BAD", "value": self.SECRET, "stamp": stamp}, (422, "bad_name")),
            ("key_set", {"name": "NEW_KEY", "value": self.SECRET, "stamp": stamp}, (422, "bad_provider")),
            ("key_set", {"name": "NEW_KEY", "value": self.SECRET, "provider": "nope", "stamp": stamp}, (404, "unknown_provider")),
            ("key_set", {"name": "NEW_KEY", "value": self.SECRET, "provider": "anthropic", "stamp": stamp}, (422, "bad_name")),
            ("key_remove", {"name": "MISSING", "stamp": stamp}, (404, "not_found")),
            ("key_value", {"name": "MISSING"}, (404, "not_found")),
        ]
        before = self.keys.read_text()
        for action, body, want in cases:
            error = self.refused(action, **body)
            self.assertEqual((error.status, error.code), want, (action, body.get("name")))
            self.assertNotIn("SECRET", error.message)
            self.assertNotIn("OLDOLD", error.message)
        self.assertEqual(self.keys.read_text(), before)

    def test_keys_over_the_route(self):
        cred = self.device("phone")
        stamp = self.stamp()
        body = {"name": "ANTHROPIC_API_KEY", "value": self.SECRET, "provider": "anthropic", "stamp": stamp}
        status, _ = self.srv.request("POST", "/v1/app/settings/key_set", body=body, headers={
            "Authorization": f"Bearer {cred['credential']}", "Origin": "https://evil.example"})
        self.assertEqual(status, 403)
        status, data = self.post("key_set", body, cred)
        self.assertEqual(status, 200, data)
        self.assertNotIn("SECRETSECRET", json.dumps(data))
        _, page = self.srv.request("GET", "/v1/app/settings", headers={
            "Authorization": f"Bearer {cred['credential']}", "Origin": self.origin()})
        self.assertNotIn(b"SECRETSECRET", page)
        status, data = self.post("key_value", {"name": "ANTHROPIC_API_KEY"}, cred)
        self.assertEqual((status, data["value"]), (200, self.SECRET))


class TestRevoke(_Settings):
    def test_revoke_another_device(self):
        me = self.device("laptop")
        phone = self.device("phone")
        status, body = self.post("revoke", {"device_id": phone["device_id"]}, me)
        self.assertEqual(status, 200, body)
        self.assertEqual(body["revoked"], phone["device_id"])
        self.assertFalse(body["this_device"])
        rows = {d["id"]: d for d in body["devices"]}
        self.assertTrue(rows[phone["device_id"]]["revoked"])
        self.assertTrue(pairing.load_devices(self.home)[phone["device_id"]]["revoked_at"])
        # The phone is signed out; this browser is not.
        status, _ = self.srv.request("GET", "/v1/app/settings",
                                     headers={"Authorization": f"Bearer {phone['credential']}"})
        self.assertEqual(status, 401)
        status, _ = self.srv.request("GET", "/v1/app/settings",
                                     headers={"Authorization": f"Bearer {me['credential']}"})
        self.assertEqual(status, 200)

    def test_this_device_needs_an_explicit_confirm(self):
        me = self.device("laptop")
        for body in ({"device_id": me["device_id"]},
                     {"device_id": me["device_id"], "confirm_this_device": "yes"}):
            status, reply = self.post("revoke", body, me)
            self.assertEqual(status, 409)
            self.assertEqual(reply["error"]["code"], "confirm_this_device")
        self.assertFalse(pairing.load_devices(self.home)[me["device_id"]]["revoked_at"])
        status, reply = self.post("revoke", {"device_id": me["device_id"], "confirm_this_device": True}, me)
        self.assertEqual(status, 200, reply)
        self.assertTrue(reply["this_device"])
        status, _ = self.srv.request("GET", "/v1/app/settings",
                                     headers={"Authorization": f"Bearer {me['credential']}"})
        self.assertEqual(status, 401)

    def test_refusals(self):
        me = self.device("laptop")
        phone = self.device("phone")
        pairing.revoke_device(self.home, phone["device_id"])
        def revoked():
            return {d: bool(r.get("revoked_at")) for d, r in pairing.load_devices(self.home).items()}

        before = revoked()
        for body, code, want in (
            ({}, 400, "bad_device"),
            ({"device_id": 7}, 400, "bad_device"),
            ({"device_id": "x" * 65}, 400, "bad_device"),
            ({"device_id": "0123456789abcdef"}, 404, "unknown_device"),
            ({"device_id": phone["device_id"]}, 409, "already_revoked"),
        ):
            status, reply = self.post("revoke", body, me)
            self.assertEqual((status, reply["error"]["code"]), (code, want), body)
        self.assertEqual(revoked(), before)

    def test_cross_site_revoke_is_refused(self):
        me = self.device("laptop")
        phone = self.device("phone")
        headers = {"Authorization": f"Bearer {me['credential']}", "Origin": "https://evil.example",
                   "Content-Type": "application/json"}
        status, _ = self.srv.request("POST", "/v1/app/settings/revoke",
                                     body={"device_id": phone["device_id"]}, headers=headers)
        self.assertEqual(status, 403)
        self.assertFalse(pairing.load_devices(self.home)[phone["device_id"]]["revoked_at"])


class TestOwnerTailnetCaller(_Settings):
    """The hub owner's own tailnet device, let in without pairing: its
    ``device_id`` is ``tailnet:<node>``, which no devices.json row has."""

    OWNER_IP = "100.64.0.9"

    def setUp(self):
        super().setUp()
        state = self.srv.state
        state.trust_proxy = True
        state.proxy_peer = lambda server, peer: True
        state.owner_login = "dev@github"
        state.whois = lambda ip: ({"node_id": "n-laptop", "login": "dev@github", "tagged": False}
                                  if ip == self.OWNER_IP else None)

    def headers(self, post=False):
        h = {"X-Forwarded-For": self.OWNER_IP, "X-Forwarded-Proto": "https", "X-Swe-Pair": "1"}
        if post:
            h.update({"Origin": f"https://127.0.0.1:{self.srv.port}", "Content-Type": "application/json"})
        return h

    def owner_post(self, action, body):
        status, data = self.srv.request("POST", f"/v1/app/settings/{action}", body=body,
                                        headers=self.headers(post=True))
        return status, json.loads(data)

    def test_get_says_tailnet_and_marks_no_device(self):
        self.device("phone")
        status, data = self.srv.request("GET", "/v1/app/settings", headers=self.headers())
        self.assertEqual(status, 200, data)
        remote = json.loads(data)["remote"]
        self.assertEqual(remote["caller"], {"kind": "tailnet", "node": "n-laptop"})
        self.assertFalse(any(d["this_device"] for d in remote["devices"]))

    def test_paired_caller_is_a_device(self):
        me = self.device("laptop")
        caller = settings_page.get(self.ctx(me["device_id"]), {})["remote"]["caller"]
        self.assertEqual(caller, {"kind": "device", "device_id": me["device_id"]})

    def test_its_tailnet_sign_in_is_listed_and_revocable(self):
        status, data = self.srv.request("GET", "/v1/app/settings", headers=self.headers())
        self.assertEqual(status, 200, data)
        signins = json.loads(data)["remote"]["signins"]
        self.assertEqual([(r["id"], r["this_device"], r["revoked"]) for r in signins],
                         [("tailnet:n-laptop", True, False)])
        status, reply = self.owner_post("revoke", {"device_id": "tailnet:n-laptop"})
        self.assertEqual((status, reply["error"]["code"]), (409, "confirm_this_device"))
        status, reply = self.owner_post(
            "revoke", {"device_id": "tailnet:n-laptop", "confirm_this_device": True})
        self.assertEqual(status, 200, reply)
        self.assertTrue(reply["this_device"])
        self.assertTrue(reply["signins"][0]["revoked"])
        # The browser is out: the same request no longer gets in.
        status, _ = self.srv.request("GET", "/v1/app/settings", headers=self.headers())
        self.assertEqual(status, 401)
        with self.assertRaises(PageError) as caught:
            settings_page.post(self.ctx("tailnet:n-gone"), "revoke",
                               {"device_id": "tailnet:n-gone"})
        self.assertEqual(caught.exception.code, "unknown_device")

    def test_owner_can_pair_and_revoke_other_devices(self):
        phone = self.device("phone")
        status, reply = self.owner_post("revoke", {"device_id": phone["device_id"]})
        self.assertEqual(status, 200, reply)
        self.assertFalse(reply["this_device"])
        status, reply = self.owner_post("pair", {})
        self.assertEqual(status, 200, reply)
        self.assertTrue(reply["ticket"]["token"])


class TestSessionSearch(_Settings):
    def setUp(self):
        super().setUp()
        # Each background pass the page starts signals when it ends, so a
        # test waits for that pass itself rather than polling the lock
        # (which also read "idle" before a pass had started).
        self.passes = threading.Semaphore(0)
        real = settings_page._first_pass

        def tracked(home):
            try:
                real(home)
            finally:
                self.passes.release()

        patcher = patch.object(settings_page, "_first_pass", side_effect=tracked)
        patcher.start()
        self.addCleanup(patcher.stop)

    def wait_idle(self):
        from swe.sessions import search_writer

        self.assertTrue(self.passes.acquire(timeout=30), "the pass never finished")
        self.assertFalse(search_writer.is_running(self.home))

    def get(self, cred):
        status, data = self.srv.request("GET", "/v1/app/settings",
                                        headers={"Authorization": f"Bearer {cred['credential']}"})
        self.assertEqual(status, 200, data)
        return json.loads(data)["session_search"]

    def test_status_is_off_with_no_index(self):
        ss = self.get(self.device("laptop"))
        self.assertEqual((ss["enabled"], ss["running"], ss["exists"]), (False, False, False))

    def test_turning_it_on_indexes_now_and_off_stops_the_hub(self):
        from swe.sessions import search_index, search_writer

        projects = Path(self.home) / ".claude" / "projects" / "p"
        projects.mkdir(parents=True)
        (projects / "a.jsonl").write_text(json.dumps({
            "type": "user", "sessionId": "s1", "timestamp": "2026-10-01T10:00:00Z",
            "message": {"content": "turned on from settings"}}, separators=(",", ":")) + "\n")
        me = self.device("laptop")
        status, data = self.post("session_search", {"enabled": True}, me)
        self.assertEqual(status, 200, data)
        self.assertTrue(data["session_search"]["enabled"])
        self.wait_idle()
        ss = self.get(me)
        self.assertEqual((ss["sessions"], ss["last_pass"]["indexed"]), (1, 1))
        self.assertEqual(len(search_index.search(self.home, "settings")), 1)
        status, data = self.post("session_search", {"enabled": False}, me)
        self.assertEqual(status, 200, data)
        self.assertFalse(search_writer.is_enabled(self.home))

    def test_enabled_must_be_a_boolean(self):
        status, data = self.post("session_search", {"enabled": "yes"}, self.device("laptop"))
        self.assertEqual((status, data["error"]["code"]), (400, "bad_enabled"))

    def test_a_foreign_origin_cannot_turn_it_on(self):
        from swe.sessions import search_writer

        me = self.device("laptop")
        headers = {"Authorization": f"Bearer {me['credential']}", "Origin": "https://evil.example",
                   "Content-Type": "application/json"}
        status, _ = self.srv.request("POST", "/v1/app/settings/session_search",
                                     body={"enabled": True}, headers=headers)
        self.assertEqual(status, 403)
        self.assertFalse(search_writer.is_enabled(self.home))

    def test_a_failed_first_pass_is_recorded(self):
        from swe.sessions import search_writer

        with patch.object(search_writer, "open_index", side_effect=search_writer.IndexUnavailable("too new")):
            settings_page._first_pass(Path(self.home))
        self.assertFalse(search_writer.is_running(self.home))
        self.assertEqual(search_writer.status(self.home)["last_error"]["message"], "too new")

    def write_session(self):
        projects = Path(self.home) / ".claude" / "projects" / "p"
        projects.mkdir(parents=True)
        (projects / "a.jsonl").write_text(json.dumps({
            "type": "user", "sessionId": "s1", "timestamp": "2026-10-01T10:00:00Z",
            "message": {"content": "resync and clear fixture"}}, separators=(",", ":")) + "\n")

    def test_resync_reruns_the_pass(self):
        from swe.sessions import search_index

        self.write_session()
        me = self.device("laptop")
        self.post("session_search", {"enabled": True}, me)
        self.wait_idle()
        first = self.get(me)["last_pass"]
        status, data = self.post("session_search", {"resync": True}, me)
        self.assertEqual(status, 200, data)
        self.wait_idle()
        ss = self.get(me)
        # The pass ran again end to end: the file it already consumed is
        # "current" rather than indexed twice.
        self.assertTrue(ss["enabled"])
        self.assertEqual(ss["sessions"], 1)
        self.assertGreaterEqual(ss["last_pass"]["finished_at"], first["finished_at"])
        self.assertGreaterEqual(ss["last_pass"]["files"], 1)
        self.assertEqual(len(search_index.search(self.home, "fixture")), 1)

    def test_clear_wipes_the_index_and_rebuilds_when_enabled(self):
        self.write_session()
        me = self.device("laptop")
        self.post("session_search", {"enabled": True}, me)
        self.wait_idle()
        self.assertEqual(self.get(me)["sessions"], 1)
        # Hold the rebuild so the cleared state can't race the answer:
        # the gate's release is what lets the index fill again.
        started, release, done = threading.Event(), threading.Event(), threading.Event()
        real_pass = settings_page._first_pass

        def gated(home):
            try:
                started.set()
                release.wait(30)
                real_pass(home)
            finally:
                done.set()

        try:
            with patch.object(settings_page, "_first_pass", side_effect=gated):
                status, data = self.post("session_search", {"clear": True}, me)
            self.assertEqual(status, 200, data)
            self.assertTrue(started.wait(10), "an enabled index rebuilds")
            ss = self.get(me)
            self.assertTrue(ss["enabled"], "clear keeps the switch on")
            self.assertEqual(ss["sessions"], 0)
            self.assertIsNone(ss["last_pass"])
        finally:
            release.set()
        self.assertTrue(done.wait(30), "the rebuild finished")
        self.assertEqual(self.get(me)["sessions"], 1)

    def test_clear_while_off_wipes_without_a_rebuild(self):
        self.write_session()
        me = self.device("laptop")
        self.post("session_search", {"enabled": True}, me)
        self.wait_idle()
        self.post("session_search", {"enabled": False}, me)
        status, data = self.post("session_search", {"clear": True}, me)
        self.assertEqual(status, 200, data)
        ss = data["session_search"]
        # The file stays — a writer holding it open keeps the inode —
        # but every row is gone and nothing rebuilds while it is off.
        self.assertEqual((ss["enabled"], ss["sessions"], ss["running"]), (False, 0, False))
        self.assertIsNone(ss["last_error"])
        self.assertIsNone(ss["last_pass"])

    def test_clear_while_a_pass_holds_the_lock_is_409(self):
        from swe.sessions import search_writer
        from swe.paths import search_index_for

        me = self.device("laptop")
        self.post("session_search", {"enabled": True}, me)
        self.wait_idle()
        lock = search_writer._Lock(search_index_for(Path(self.home)).parent / search_writer.LOCK_FILE)
        lock.__enter__()
        try:
            status, data = self.post("session_search", {"clear": True}, me)
        finally:
            lock.__exit__()
        self.assertEqual(status, 409, data)
        self.assertEqual(data["error"]["code"], "busy")
        self.assertTrue(self.get(me)["exists"], "a refused clear deleted nothing")


class TestSync(_Settings):
    """Sync now: swe gh sync in process, against a fake gh on PATH."""

    def setUp(self):
        super().setUp()
        from swe.github import shared as G

        patcher = patch.object(G, "CACHE", G.TtlCache())
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_sync_fills_the_hubs_catalog_and_answers_with_the_card(self):
        from swe import paths
        from swe.github import catalog
        from tests.test_webapp_repositories import FakeGh

        FakeGh(self, orgs=["acme"], lists={"octo": ["dots"], "acme": ["sdk"]})
        me = self.device("laptop")
        status, body = self.post("sync", {}, me)
        self.assertEqual(status, 200, body)
        self.assertEqual(body["failed"], [])
        self.assertEqual(body["github"]["env"]["login"], "octo")
        self.assertEqual(body["github"]["catalog"]["repos"], 2)
        self.assertEqual(catalog.status(paths.github_catalog_for(self.home))["repos"], 2)

    def test_sync_probes_gh_afresh_so_a_switched_account_counts(self):
        from swe import paths
        from swe.github import catalog
        from swe.github import shared as G
        from tests.test_webapp_repositories import FakeGh

        # The card read the old account a moment ago; gh is now signed in as octo.
        G.CACHE.get(("env", G.home_key(self.home)),
                    lambda: gh.GhEnv(installed=True, authenticated=True, login="old-account"))
        FakeGh(self, orgs=[], lists={"octo": ["dots"]})
        status, body = self.post("sync", {}, self.device("laptop"))
        self.assertEqual(status, 200, body)
        self.assertEqual(body["github"]["env"]["login"], "octo")
        self.assertEqual(catalog.status(paths.github_catalog_for(self.home))["owners"], ["octo"])

    def test_skipped_owners_are_named(self):
        from tests.test_webapp_repositories import FakeGh

        FakeGh(self, orgs=["acme"], lists={"octo": ["dots"]})
        status, body = self.post("sync", {}, self.device("laptop"))
        self.assertEqual(status, 200)
        self.assertEqual([f["owner"] for f in body["failed"]], ["acme"])
        self.assertEqual(body["github"]["catalog"]["repos"], 1)

    def test_a_signed_out_gh_is_refused_in_its_setup_words(self):
        from tests.test_webapp_repositories import FakeGh

        fake = FakeGh(self, signed_out=True)
        status, body = self.post("sync", {}, self.device("laptop"))
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "gh_auth_required")
        self.assertIn("gh auth login", body["error"]["message"])
        self.assertEqual([c[:2] for c in fake.calls()], [["auth", "status"]])

    def test_one_sync_at_a_time(self):
        settings_page._SYNC_LOCK.acquire()
        try:
            with self.assertRaises(PageError) as raised:
                settings_page.post(self.ctx(), "sync", {})
        finally:
            settings_page._SYNC_LOCK.release()
        self.assertEqual((raised.exception.status, raised.exception.code), (409, "busy"))

    def test_cross_site_sync_is_refused(self):
        from tests.test_webapp_repositories import FakeGh

        fake = FakeGh(self)
        headers = {"Authorization": f"Bearer {self.device('phone')['credential']}",
                   "Origin": "https://evil.example", "Content-Type": "application/json"}
        status, _ = self.srv.request("POST", "/v1/app/settings/sync", body={}, headers=headers)
        self.assertEqual(status, 403)
        self.assertEqual(fake.calls(), [])


@unittest.skipUnless(shutil.which("git"), "git is not installed")
class TestHubSync(_Settings):
    """Files › Hub sync: swe sync status and swe sync on the hub's ~/.swe,
    against a throwaway bare remote."""

    def setUp(self):
        super().setUp()
        import os

        from swe.paths import swe_dir_for
        from tests.test_sync import GIT_ENV, git

        patcher = patch.dict(os.environ, GIT_ENV)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.git = git
        self.hub = swe_dir_for(self.home)
        self.hub.mkdir(parents=True, exist_ok=True)
        from tests._tempdir import temp_dir

        self.remote = Path(temp_dir(self))
        git(self.remote, "init", "-q", "--bare", "-b", "main")
        (self.hub / "AGENTS.md").write_text("# hub\n")
        (self.hub / ".gitignore").write_text("data/\ncache/\nsecrets/\n")
        git(self.hub, "init", "-q", "-b", "main")
        git(self.hub, "remote", "add", "origin", str(self.remote))
        git(self.hub, "add", "-A")
        git(self.hub, "commit", "-q", "-m", "seed")
        git(self.hub, "push", "-q", "-u", "origin", "main")
        relink = patch("swe.sync.repo.default_relink", return_value=None)
        relink.start()
        self.addCleanup(relink.stop)

    def get_part(self, cred, query="part=hub_sync"):
        status, data = self.srv.request(
            "GET", f"/v1/app/settings?{query}",
            headers={"Authorization": f"Bearer {cred['credential']}"})
        return status, json.loads(data)

    def test_status_part(self):
        (self.hub / "skills").mkdir()
        (self.hub / "skills" / "x.md").write_text("x\n")
        status, body = self.get_part(self.device("laptop"), "part=hub_sync&fetch=1")
        self.assertEqual(status, 200, body)
        card = body["hub_sync"]
        self.assertTrue(card["ready"])
        self.assertTrue(card["fetched"])
        self.assertEqual(card["mode"], "single")
        self.assertEqual([c["path"] for c in card["changes"]], ["skills/x.md"])

    def test_sync_commits_pushes_and_answers_with_the_card(self):
        (self.hub / "AGENTS.md").write_text("# edited\n")
        status, body = self.post("hub_sync", {}, self.device("laptop"))
        self.assertEqual(status, 200, body)
        self.assertEqual(body["result"]["result"], "synced")
        self.assertEqual(body["hub_sync"]["changes"], [])
        self.assertEqual(self.git(self.remote, "show", "main:AGENTS.md"), "# edited")

    def test_relink_runs_under_the_config_lock(self):
        from swe.webapp.pages import CONFIG_LOCK
        from tests._tempdir import temp_dir

        other = Path(temp_dir(self)) / "clone"
        self.git(other.parent, "clone", "-q", str(self.remote), str(other))
        (other / "skills.md").write_text("new\n")
        self.git(other, "add", "-A")
        self.git(other, "commit", "-q", "-m", "elsewhere")
        self.git(other, "push", "-q", "origin", "main")
        held = []

        def relink(home, changed, base=None):
            held.append(CONFIG_LOCK.locked())
            return {"changed": len(changed), "linked": 0, "marketplaces": [],
                    "mcp": None, "notes": []}

        with patch("swe.sync.repo.default_relink", side_effect=relink):
            status, body = self.post("hub_sync", {}, self.device("laptop"))
        self.assertEqual(status, 200, body)
        self.assertEqual(held, [True])
        self.assertFalse(CONFIG_LOCK.locked())

    def test_dry_run_changes_nothing(self):
        (self.hub / "AGENTS.md").write_text("# edited\n")
        before = self.git(self.remote, "rev-parse", "main")
        status, body = self.post("hub_sync", {"dry_run": True}, self.device("laptop"))
        self.assertEqual(status, 200, body)
        self.assertEqual(body["result"]["result"], "would-sync")
        self.assertEqual(self.git(self.remote, "rev-parse", "main"), before)
        self.assertEqual(len(body["hub_sync"]["changes"]), 1)

    def test_a_hub_that_is_not_a_repo_is_a_409(self):
        shutil.rmtree(self.hub / ".git")
        status, body = self.post("hub_sync", {}, self.device("laptop"))
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "sync_failed")
        status, body = self.get_part(self.device("laptop"))
        self.assertFalse(body["hub_sync"]["ready"])

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_card_lines_under_node(self):
        js = Path(__file__).resolve().parents[1] / "src/swe/webapp/static/pages/settings.js"
        src = (
            "const hubSyncCount = (count, word) => `${count} ${word}${count === 1 ? '' : 's'}`;\n"
            + app_js_function("hubSyncLines", js) + app_js_function("hubSyncOutcome", js)
            + "const device = {mode: 'device', branch: 'device/desk', base: 'origin/main',"
              " upstream: 'origin/device/desk', ahead: 2, behind: 0, base_behind: 1,"
              " changes: [{}, {}], groups: {skills: 1, 'AGENTS.md': 1}, held_back: [], fetched: true};\n"
              "const conflict = {result: 'conflict', conflicts: ['AGENTS.md']};\n"
              "const synced = {result: 'synced', commit: {files: [{}]}, merges: [{commits: 3}],"
              " pushed: true, branch: 'main', relinked: {}};\n"
              "const pushFailed = {result: 'push-failed', push_error: 'rejected', relinked: {notes: ['n1'],"
              " marketplaces: [{harness: 'claude', marketplace: 'mine', error: 'no claude'}]}};\n"
              "process.stdout.write(JSON.stringify([hubSyncLines(device), hubSyncOutcome(conflict),"
              " hubSyncOutcome(synced), hubSyncOutcome(pushFailed)]));\n")
        done = subprocess.run(["node", "-e", src], capture_output=True, text=True, timeout=30)
        self.assertEqual(done.returncode, 0, done.stderr)
        lines, conflict, synced, push_failed = json.loads(done.stdout)
        self.assertEqual(push_failed["text"], "Committed and merged, but the push failed: rejected. "
                                              "Sync again to retry. n1 mine not registered in claude: no claude.")
        self.assertEqual(lines["repo"], "Device branch device/desk: merges origin/main in, "
                                        "pushes only device/desk.")
        self.assertEqual(lines["changes"], "2 uncommitted files: AGENTS.md 1, skills 1. "
                                           "2 commits to push · 1 new commit on origin/main.")
        self.assertTrue(conflict["danger"])
        self.assertIn("AGENTS.md", conflict["text"])
        self.assertEqual(synced["text"], "Committed 1 file, merged 3 commits, pushed main, "
                                         "re-linked every harness.")

    def test_one_hub_sync_at_a_time(self):
        settings_page._HUB_SYNC_LOCK.acquire()
        try:
            with self.assertRaises(PageError) as raised:
                settings_page.post(self.ctx(), "hub_sync", {})
        finally:
            settings_page._HUB_SYNC_LOCK.release()
        self.assertEqual((raised.exception.status, raised.exception.code), (409, "busy"))


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class TestRevokeThisBrowserScript(unittest.TestCase):
    """pages/settings.js under node, with app.js's real pairForget():
    revoking this browser's own row drops its credential from
    localStorage and stops the remote poll; revoking another device
    leaves both alone. The poll itself stops at its first 401."""

    STATIC = Path(__file__).resolve().parents[1] / "src/swe/webapp/static"

    def run_js(self, api_status, post_data, body, hash_="#/settings?section=integrations"):
        app = (self.STATIC / "app.js").read_text(encoding="utf-8")
        forget = re.search(r"^function pairForget\(\) \{.*?^\}", app, re.S | re.M).group(0)
        prelude = (
            "const store = new Map([['swe.remote', '{\"credential\":\"c\"}']]);\n"
            "globalThis.localStorage = { getItem: (k) => store.get(k) ?? null, setItem: (k, v) => store.set(k, v),"
            " removeItem: (k) => store.delete(k) };\n"
            "globalThis.location = { hash: " + json.dumps(hash_) + " };\n"
            "const listeners = {};\n"
            "globalThis.document = { documentElement: { dataset: {} }, head: { appendChild() {} },"
            " getElementById: () => null, createElement: () => ({}), querySelectorAll: () => [],"
            " hidden: false, addEventListener: (t, f) => { (listeners[t] = listeners[t] || []).push(f); } };\n"
            "let tick = null, cleared = 0, gates = [];\n"
            "globalThis.setInterval = (fn) => { tick = fn; return 7; };\n"
            "globalThis.clearInterval = (id) => { if (id === 7) { cleared++; tick = null; } };\n"
            "const routeGen = 0;\n"
            "function registerPage() {}\n"
            "function renderGate(msg) { gates.push(msg); }\n"
            "function renderSettings() {}\n" + app_js_function("currentRoute") + app_js_function("syncThemeColor") +
            "let api = async () => ({ status: " + str(api_status) + ", data: null });\n"
            "const apiPost = async () => ({ status: 200, data: " + post_data + " });\n"
            + forget + "\n")
        src = prelude + (self.STATIC / "pages/settings.js").read_text(encoding="utf-8") + "\n" + body
        done = subprocess.run(["node", "-e", src], capture_output=True, text=True, timeout=30)
        self.assertEqual(done.returncode, 0, done.stderr)
        return json.loads(done.stdout)

    REVOKE = ("settingsPage.data = { remote: { devices: [{ id: 'd1', this_device: %s }] } };\n"
              "settingsPage.setupBusy = true;\nsettingsPollRequests();\n"
              "settingsRevoke('d1').then(() => process.stdout.write(JSON.stringify("
              "{ stored: store.has('swe.remote'), signedOut: !!settingsPage.signedOut,"
              " polling: settingsPage.reqPoll !== null })));\n")

    def test_revoking_this_browser_forgets_its_credential(self):
        # Off the page, so the revoke doesn't repaint a DOM this stub lacks.
        out = self.run_js(200, "{ this_device: true }", self.REVOKE % "true", "#/overview")
        self.assertEqual(out, {"stored": False, "signedOut": True, "polling": False})

    def test_revoking_another_device_keeps_it(self):
        out = self.run_js(200, "{ devices: [] }", self.REVOKE % "false", "#/overview")
        self.assertEqual(out, {"stored": True, "signedOut": False, "polling": True})

    def test_the_poll_stops_at_a_401(self):
        out = self.run_js(401, "{}", "settingsPollRequests();\n"
                          "(async () => { await tick(); const after = tick; "
                          "process.stdout.write(JSON.stringify({ gates: gates.length, cleared, after: after === null,"
                          " polling: settingsPage.reqPoll !== null })); })();\n")
        self.assertEqual(out, {"gates": 1, "cleared": 1, "after": True, "polling": False})

    def test_a_hidden_tab_skips_the_poll_and_showing_it_reads_at_once(self):
        """#1554: a background tab's ticks read nothing; coming back reads
        once without waiting for the next tick, and only one listener is
        added however often the poll restarts."""
        out = self.run_js(200, "{}", (
            "let reads = 0;\n"
            "api = async () => { reads++; return { status: 200, data: null }; };\n"
            "settingsPollRequests();\n"
            "(async () => {\n"
            "  await tick(); const visible = reads;\n"
            "  document.hidden = true;\n"
            "  await tick(); await tick(); const hidden = reads - visible;\n"
            "  document.hidden = false;\n"
            "  for (const f of listeners.visibilitychange) await f();\n"
            "  const shown = reads - visible;\n"
            "  settingsStopPoll(); settingsPollRequests(); settingsStopPoll();\n"
            "  for (const f of listeners.visibilitychange) await f();\n"
            "  process.stdout.write(JSON.stringify({ visible, hidden, shown, after: reads - visible,"
            " listeners: listeners.visibilitychange.length }));\n"
            "})();\n"))
        self.assertEqual(out, {"visible": 1, "hidden": 0, "shown": 1, "after": 1, "listeners": 1})


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class TestRevealedKeysScript(unittest.TestCase):
    """pages/settings.js under node: a key value the eye revealed is
    masked again once the page moves to another section or the other
    Providers tab, so coming back never shows it in plain text."""

    STATIC = Path(__file__).resolve().parents[1] / "src/swe/webapp/static"

    def test_switching_section_or_tab_masks_revealed_values(self):
        prelude = (
            "globalThis.localStorage = { getItem: () => null, setItem() {}, removeItem() {} };\n"
            "globalThis.location = { hash: '#/settings?section=providers' };\n"
            "globalThis.history = { replaceState(_s, _t, url) { location.hash = url; } };\n"
            "const button = (data) => ({ dataset: data, onclick: null });\n"
            "const sectionBtn = button({ section: 'general' }), tabBtn = button({ providersTab: 'harnesses' });\n"
            "const panel = { querySelector: () => null, querySelectorAll: (sel) =>\n"
            "  sel === '[data-section]' ? [sectionBtn] : sel === '[data-providers-tab]' ? [tabBtn] : [] };\n"
            "globalThis.document = { documentElement: { dataset: {} }, head: { appendChild() {} },\n"
            "  getElementById: (id) => (id === 'panel' ? panel : null), querySelector: () => null,\n"
            "  createElement: () => ({}), querySelectorAll: () => [] };\n"
            "const routeGen = 0;\n"
            "function registerPage() {}\n" + app_js_function("currentRoute") + app_js_function("syncThemeColor"))
        body = (
            "let paints = 0; paintSettings = () => { paints++; };\n"
            "const out = [];\n"
            "for (const btn of [sectionBtn, tabBtn]) {\n"
            "  settingsPage.revealed.set('OPENAI_API_KEY', 'sk-test-value');\n"
            "  const gen = settingsPage.revealGen;\n"
            "  bindSettings('providers');\n"
            "  btn.onclick();\n"
            "  out.push([settingsPage.revealed.size, settingsPage.revealGen > gen]);\n"
            "}\n"
            "process.stdout.write(JSON.stringify({ out, paints }));\n")
        src = prelude + (self.STATIC / "pages/settings.js").read_text(encoding="utf-8") + "\n" + body
        done = subprocess.run(["node", "-e", src], capture_output=True, text=True, timeout=30)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(json.loads(done.stdout), {"out": [[0, True], [0, True]], "paints": 2})


if __name__ == "__main__":
    unittest.main()
