# Third-party notices

## LiteLLM model prices

`Sources/SWECore/Resources/pricing.json` is a trimmed copy of
[`model_prices_and_context_window.json`](https://github.com/BerriAI/litellm/blob/main/model_prices_and_context_window.json)
from LiteLLM, made with `script/trim-litellm-pricing.py`. The daily refresh
downloads the same file. It is used under the MIT License:

```
MIT License

Copyright (c) 2023 Berri AI

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## OpenUsage

The ledger's Claude and Codex parsing rules (ccusage-style dedup with the
sidechain pass, advisor iterations, the Codex fork replay gate, Codex service
tiers) and the fast-mode multipliers in `Resources/pricing-supplement.json`
follow OpenUsage, re-implemented here. OpenUsage is used under the MIT License:

```
MIT License

Copyright (c) 2026 Robin Ebers

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## Geist and Geist Mono

`Sources/SWECore/Resources/Geist.ttf`, `Geist-Italic.ttf`, `GeistMono.ttf`
and `GeistMono-Italic.ttf` are the variable fonts from
[Geist](https://github.com/vercel/geist-font) v1.7.2 (`Geist[wght].ttf`,
`Geist-Italic[wght].ttf`, `GeistMono[wght].ttf` and
`GeistMono-Italic[wght].ttf`, renamed). They are the app's
typefaces. Copyright 2024 The Geist Project Authors, used under the SIL Open
Font License 1.1, whose full text ships beside them as
`Sources/SWECore/Resources/Geist-OFL.txt`.

## Orca

The session search index (`Sources/SWECore/Search/`) ports Orca's
agent-source table, transcript merge and scan rules — the comment `Orca's`
marks those spots. Orca is used under the MIT License:

```
MIT License

Copyright (c) 2026 Lovecast Inc.

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## Switchboard

`Sources/SWECore/Usage/UsageSupport.swift` ports Switchboard's
`describePlan`, and the profile model (one config home and one desktop data
folder per profile) follows it. Switchboard is used under the MIT License:

```
MIT License

Copyright (c) 2026 Shayaan Sultan

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## Stats

The SMC sensor key map in `Sources/SWECore/Compute/Temperature.swift`
follows Stats (github.com/exelban/stats), used under the MIT License:

```
MIT License

Copyright (c) 2019 Serhiy Mytrovtsiy

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## Sparkle

`Sparkle.framework` ships inside the app bundle and drives update checks
and installs. Sparkle is used under the MIT License:

```
Copyright (c) 2006-2013 Andy Matuschak.
Copyright (c) 2009-2013 Elgato Systems GmbH.
Copyright (c) 2011-2014 Kornel Lesiński.
Copyright (c) 2015-2017 Mayur Pawashe.
Copyright (c) 2014 C.W. Betts.
Copyright (c) 2014 Petroules Corporation.
Copyright (c) 2014 Big Nerd Ranch.
All rights reserved.

Permission is hereby granted, free of charge, to any person obtaining a copy of
this software and associated documentation files (the "Software"), to deal in
the Software without restriction, including without limitation the rights to
use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of
the Software, and to permit persons to whom the Software is furnished to do so,
subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS
FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR
COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER
IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN
CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
```

Sparkle's own LICENSE also covers bundled components: bsdiff/bsdiff.c
(Copyright 2003-2005 Colin Percival, BSD), sais-lite (Copyright 2008-2010
Yuta Mori, MIT), a portable Ed25519 implementation (Copyright 2015 Orson
Peters, zlib-style) and SUSignatureVerifier.m (Copyright 2011 Mark Hamlin,
BSD). The full text is Sparkle's `LICENSE` file, which ships in the bundle as
`Contents/Resources/Licenses/sparkle.txt`.

## Zstandard

`Sources/CZStd/zstddeclib.inc` is Meta's zstd v1.5.7 single-file decoder,
generated from the `v1.5.7` tag. The Mac app links it to read a transcript
copy whose body is a zstd frame. It is used under the BSD License, which
ships in the bundle as `Contents/Resources/Licenses/zstd.txt` (`zstd.txt`):

```
BSD License

For Zstandard software

Copyright (c) Meta Platforms, Inc. and affiliates. All rights reserved.

Redistribution and use in source and binary forms, with or without modification,
are permitted provided that the following conditions are met:

 * Redistributions of source code must retain the above copyright notice, this
   list of conditions and the following disclaimer.

 * Redistributions in binary form must reproduce the above copyright notice,
   this list of conditions and the following disclaimer in the documentation
   and/or other materials provided with the distribution.

 * Neither the name Facebook, nor Meta, nor the names of its contributors may
   be used to endorse or promote products derived from this software without
   specific prior written permission.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND
ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE FOR
ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES
(INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES;
LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON
ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
(INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS
SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
```

## Bundled Python and its libraries

The app embeds a stripped CPython built by
[python-build-standalone](https://github.com/astral-sh/python-build-standalone)
(release 20261003, CPython 3.13.16; the project is MPL-2.0 and none of its
tooling ships in the app). The build links several libraries statically, and
`bundle-swe.sh` leaves Tcl/Tk, Tix and the X11 libraries out, so their
licences do not ship. Each licence text below ships in the bundle under
`Contents/Resources/Licenses/`, copied from `app/mac/Licenses/` (the texts
python-build-standalone publishes in the `python/licenses/` folder of its
release archive):

| Component | Licence | Bundled file |
|---|---|---|
| CPython 3.13 | PSF License Agreement | `python-cpython.txt` (also `swe/python/lib/python3.13/LICENSE.txt`) |
| libffi (`_ctypes`) | MIT | `python-libffi.txt` |
| Expat (`pyexpat`) | MIT | `python-expat.txt` |
| SQLite (`_sqlite3`) | public domain | `python-sqlite.txt` |
| xz / liblzma (`_lzma`) | 0BSD | `python-liblzma.txt` |
| bzip2 (`_bz2`) | bzip2 licence (BSD-style) | `python-bzip2.txt` |
| mpdecimal (`_decimal`) | BSD-2-Clause | `python-mpdecimal.txt` |
| libuuid (`_uuid`) | BSD-3-Clause | `python-libuuid.txt` |
| OpenSSL 3 (`_ssl`, `_hashlib`) | Apache-2.0 | `python-openssl-3.txt` |
| OpenSSL, python-build-standalone's second notice for the same link | OpenSSL and SSLeay licences | `python-openssl-1.1.txt` |
| zlib (`zlib`, `binascii`), loaded from macOS's `/usr/lib` | Zlib | `python-zlib.txt` |
| libedit (`readline`), loaded from macOS's `/usr/lib` | BSD-3-Clause | `python-libedit.txt` |
| ncurses (`_curses`), loaded from macOS's `/usr/lib` | X11 (MIT-style) | `python-ncurses.txt` |

The `libcrypto.3.dylib` that `bundle-swe.sh` builds for the Droid auth
decryptor is OpenSSL 3.5.9 LTS (Apache-2.0), pinned by checksum in that
script and built for the host architecture. Its licence ships beside it
as `Contents/Resources/swe/python/lib/OPENSSL-LICENSE.txt`.

## References without copied code

AIUsage (Apache 2.0, the per-file cursor with inode, size, mtime, offset and
fingerprints) and TokenTracker (MIT, its notes on Claude sub-agent
over-counting, Codex cached input, and Devin's `request_id` identity) informed
the design. No code was copied from either.

## Harness logos

`Sources/SWECore/Resources/harness-*.svg` are the logos of the coding
harnesses and providers SWE lists, imported by
`script/import-harness-icons.py`: Ant Group, Antigravity, Apify, Arcade, Ark,
Baidu Cloud, Brave, Browserbase, Cartesia, CheapestInference, Claude, Cloudflare, Codex, Cohere, Command Code, Crush,
State Cloud (Ctyun), DeepSeek, Exa, Featherless, Firecrawl, Fireworks AI, Gemini, Honeycomb,
Huawei Cloud, Hugging Face, iFlytek, JD Cloud, Meta, MiniMax, Mistral Vibe,
Moore Threads, NVIDIA, OpenCode, OpenRouter, Perplexity, Qoder CN, Qwen Code,
SenseNova, Sourcegraph, Standard Compute, Statsig, StepFun, Tencent Cloud,
TinyFish, Together AI, UCloud, China Unicom, Upstage, WorkOS and Zapier (51 logos)
keep their brand colours (`harness-<id>-color.svg`), the rest are
single-colour marks. Every logo is a trademark of its owner
and is used only to name that harness; none of them implies endorsement.
Where each came from is in `Resources/harness-icons.tsv`:

- Claude, Codex, Gemini, Copilot, Cursor, Grok, Kiro, Devin, OpenCode, Kilo,
  Pi, Antigravity, Kimi, ZCode, Command Code, Qoder, Qoder CN and Volcano
  Engine Ark come from TokenTracker (`dashboard/public/brand-logos` and
  `ProviderIcon.jsx`), most of them extracted there from LobeHub icons.
- Claude's colour mark, Qwen, Mistral, goose, Cline and the API key
  provider marks (OpenAI, DeepSeek, Z.ai, Yi, MiniMax, Xiaomi MiMo,
  StepFun, Groq, Upstage, Cerebras, OpenRouter, Together AI, Fireworks
  AI, Vercel, Nebius, Cohere, Perplexity, GitHub, Hugging Face, NVIDIA
  and Cloudflare) and the model-lab marks
  for Meta and Ant Group come from LobeHub icons
  (`@lobehub/icons-static-svg`).
- The inference plan and API provider marks for Tencent Cloud, Baidu
  Cloud, Huawei Cloud, iFlytek, SenseNova, KwaiKAT, Ollama, Poe, ZenMux,
  ElevenLabs and Atlas Cloud come from LobeHub icons
  (`@lobehub/icons-static-svg`), as do the status-page marks for
  AssemblyAI, Baseten, Lambda, Mistral AI, Stability AI, AI21, SambaNova,
  Windsurf, Replit, Manus, Lovable, Tavily, Morph, Anyscale, Langfuse and
  v0. Synthetic,
  CheapestInference, Standard
  Compute, TaoToken, JD Cloud, State Cloud (China Telecom), SCNet, Charm,
  LLM Gateway and UCloud come from each company's own site or brand kit;
  China Unicom's from its logo file on English Wikipedia; Moore Threads'
  is traced from its site's PNG, as no vector version is published. These
  are trademarks of their owners, used only to name them; where each came
  from is in `harness-icons.tsv`.
- npm, Homebrew, Nix and uv come from Simple Icons (simple-icons,
  CC0-1.0), as do the status-page marks for Docker, Sentry, Datadog,
  Supabase, DigitalOcean, Render, Upstash, Convex, Linear, Notion, Clerk
  and Twilio (Twilio's is pinned to simple-icons 13.21.0, the last
  release carrying it), Deepgram, Warp, Bitbucket, CircleCI, Buildkite,
  Travis CI, Netlify, Fly.io, PyPI, RubyGems, MongoDB, Prisma, Postman,
  Mailgun, Atlassian, Grafana, New Relic, ngrok, Figma, Discord,
  HashiCorp, Expo, RevenueCat, Snyk, Codecov, Linode (Akamai's mark),
  Sanity, Contentful, Temporal, Maven Central (the Apache Maven
  feather), StackBlitz (the bolt Bolt.new's mark reuses), Braintrust,
  ClickHouse, CockroachDB (Cockroach Labs), Loom, Make, PlanetScale,
  Prefect, Semaphore, Snowflake, Sonatype and Zapier, and the
  infrastructure marks for Neon, Turso and Railway; Routing.Run's
  planet mark from Phosphor Icons (`@phosphor-icons/core`, MIT) and
  Heroku's glyph from devicons/devicon (MIT, its plate subpaths dropped).
- The remaining status-page marks come from each company's own site,
  brand page or logo file: AgentMail, E2B, Ona, Pinecone, Runloop, Writer,
  Supermemory, incident.io, LaunchDarkly, Inngest, Astronomer, Cartesia,
  Browserbase, Arcade, Statsig, WorkOS and Honeycomb — several cropped to
  the symbol where the site ships only a wordmark — and Sourcegraph's
  swirl from its WorldVectorLogo copy. crates.io publishes no vector
  mark, so its row carries the Rust logo from rust-lang/rust-artwork,
  CC-BY-4.0 (https://creativecommons.org/licenses/by/4.0/). All are
  trademarks of their owners, used only to name
  them; where each came from is in `harness-icons.tsv`.
- Continue comes from its repository (continuedev/continue,
  `docs/logo/light.svg`); aider from its repository (Aider-AI/aider,
  Apache-2.0, the site's pinned-tab icon); Crush from the
  charmbracelet/crush repository (FSL-1.1-MIT); Droid (Factory), Forge,
  Augment, Blackbox and Freebuff from each product's own site. The
  Apache-2.0 and FSL-1.1-MIT texts follow at the end of this file.
- Featherless's current mark is composed locally: the rebranded F from
  the inline SVG on featherless.ai, on its favicon's yellow tile; the
  site states no licence, so the mark is trademark-only like the rest.
- Tailscale's dot-grid mark comes from tailscale.com's favicon (the
  squircle plate it sits on is dropped), and Artificial Analysis's
  geometric mark from the inline SVG in artificialanalysis.ai's header;
  both are trademark-only like the rest.
- The Services tab marks: Exa, Firecrawl and Brave come from LobeHub
  icons (`@lobehub/icons-static-svg`, MIT). Browser Use's mark is the
  inline SVG in browser-use.com's header, Parallel's is parallel.ai's
  favicon (the white plate it ships on is dropped), TinyFish's fish is
  pulled out of the logo file docs.tinyfish.ai ships, Apify's is
  apify.com's favicon and Replicate's is from Simple Icons (CC0-1.0);
  the trademark-only ones are like the rest.
- The `tool-*.svg` marks Settings draws for uninstalled terminals and
  editors — Ghostty, iTerm2, WezTerm, Cursor, Zed, VSCodium and Xcode —
  come from Simple Icons (simple-icons, CC0-1.0); Visual Studio Code's
  ribbon from devicons/devicon (MIT). Gram's mark (`tool-gram.png`) is
  its own app icon, and Devin's editor entry reuses the bundled Devin
  harness mark. All are trademarks of their owners, used only to name
  them.

TokenTracker is used under the MIT License (Copyright (c) 2026
xiufengsun; full text above, under OpenUsage, as the same notice). LobeHub
icons are used under the MIT License:

```
MIT License

Copyright (c) 2023 LobeHub

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

Phosphor Icons (Routing.Run's planet mark) are used under the MIT License:

```
MIT License

Copyright (c) 2023 Phosphor Icons

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## Apache License 2.0

The aider pinned-tab mark and the OpenSSL libraries inside the bundled
Python are licensed under the Apache License, Version 2.0:

```
                                 Apache License
                           Version 2.0, January 2004
                        http://www.apache.org/licenses/

   TERMS AND CONDITIONS FOR USE, REPRODUCTION, AND DISTRIBUTION

   1. Definitions.

      "License" shall mean the terms and conditions for use, reproduction,
      and distribution as defined by Sections 1 through 9 of this document.

      "Licensor" shall mean the copyright owner or entity authorized by
      the copyright owner that is granting the License.

      "Legal Entity" shall mean the union of the acting entity and all
      other entities that control, are controlled by, or are under common
      control with that entity. For the purposes of this definition,
      "control" means (i) the power, direct or indirect, to cause the
      direction or management of such entity, whether by contract or
      otherwise, or (ii) ownership of fifty percent (50%) or more of the
      outstanding shares, or (iii) beneficial ownership of such entity.

      "You" (or "Your") shall mean an individual or Legal Entity
      exercising permissions granted by this License.

      "Source" form shall mean the preferred form for making modifications,
      including but not limited to software source code, documentation
      source, and configuration files.

      "Object" form shall mean any form resulting from mechanical
      transformation or translation of a Source form, including but
      not limited to compiled object code, generated documentation,
      and conversions to other media types.

      "Work" shall mean the work of authorship, whether in Source or
      Object form, made available under the License, as indicated by a
      copyright notice that is included in or attached to the work
      (an example is provided in the Appendix below).

      "Derivative Works" shall mean any work, whether in Source or Object
      form, that is based on (or derived from) the Work and for which the
      editorial revisions, annotations, elaborations, or other modifications
      represent, as a whole, an original work of authorship. For the purposes
      of this License, Derivative Works shall not include works that remain
      separable from, or merely link (or bind by name) to the interfaces of,
      the Work and Derivative Works thereof.

      "Contribution" shall mean any work of authorship, including
      the original version of the Work and any modifications or additions
      to that Work or Derivative Works thereof, that is intentionally
      submitted to Licensor for inclusion in the Work by the copyright owner
      or by an individual or Legal Entity authorized to submit on behalf of
      the copyright owner. For the purposes of this definition, "submitted"
      means any form of electronic, verbal, or written communication sent
      to the Licensor or its representatives, including but not limited to
      communication on electronic mailing lists, source code control systems,
      and issue tracking systems that are managed by, or on behalf of, the
      Licensor for the purpose of discussing and improving the Work, but
      excluding communication that is conspicuously marked or otherwise
      designated in writing by the copyright owner as "Not a Contribution."

      "Contributor" shall mean Licensor and any individual or Legal Entity
      on behalf of whom a Contribution has been received by Licensor and
      subsequently incorporated within the Work.

   2. Grant of Copyright License. Subject to the terms and conditions of
      this License, each Contributor hereby grants to You a perpetual,
      worldwide, non-exclusive, no-charge, royalty-free, irrevocable
      copyright license to reproduce, prepare Derivative Works of,
      publicly display, publicly perform, sublicense, and distribute the
      Work and such Derivative Works in Source or Object form.

   3. Grant of Patent License. Subject to the terms and conditions of
      this License, each Contributor hereby grants to You a perpetual,
      worldwide, non-exclusive, no-charge, royalty-free, irrevocable
      (except as stated in this section) patent license to make, have made,
      use, offer to sell, sell, import, and otherwise transfer the Work,
      where such license applies only to those patent claims licensable
      by such Contributor that are necessarily infringed by their
      Contribution(s) alone or by combination of their Contribution(s)
      with the Work to which such Contribution(s) was submitted. If You
      institute patent litigation against any entity (including a
      cross-claim or counterclaim in a lawsuit) alleging that the Work
      or a Contribution incorporated within the Work constitutes direct
      or contributory patent infringement, then any patent licenses
      granted to You under this License for that Work shall terminate
      as of the date such litigation is filed.

   4. Redistribution. You may reproduce and distribute copies of the
      Work or Derivative Works thereof in any medium, with or without
      modifications, and in Source or Object form, provided that You
      meet the following conditions:

      (a) You must give any other recipients of the Work or
          Derivative Works a copy of this License; and

      (b) You must cause any modified files to carry prominent notices
          stating that You changed the files; and

      (c) You must retain, in the Source form of any Derivative Works
          that You distribute, all copyright, patent, trademark, and
          attribution notices from the Source form of the Work,
          excluding those notices that do not pertain to any part of
          the Derivative Works; and

      (d) If the Work includes a "NOTICE" text file as part of its
          distribution, then any Derivative Works that You distribute must
          include a readable copy of the attribution notices contained
          within such NOTICE file, excluding those notices that do not
          pertain to any part of the Derivative Works, in at least one
          of the following places: within a NOTICE text file distributed
          as part of the Derivative Works; within the Source form or
          documentation, if provided along with the Derivative Works; or,
          within a display generated by the Derivative Works, if and
          wherever such third-party notices normally appear. The contents
          of the NOTICE file are for informational purposes only and
          do not modify the License. You may add Your own attribution
          notices within Derivative Works that You distribute, alongside
          or as an addendum to the NOTICE text from the Work, provided
          that such additional attribution notices cannot be construed
          as modifying the License.

      You may add Your own copyright statement to Your modifications and
      may provide additional or different license terms and conditions
      for use, reproduction, or distribution of Your modifications, or
      for any such Derivative Works as a whole, provided Your use,
      reproduction, and distribution of the Work otherwise complies with
      the conditions stated in this License.

   5. Submission of Contributions. Unless You explicitly state otherwise,
      any Contribution intentionally submitted for inclusion in the Work
      by You to the Licensor shall be under the terms and conditions of
      this License, without any additional terms or conditions.
      Notwithstanding the above, nothing herein shall supersede or modify
      the terms of any separate license agreement you may have executed
      with Licensor regarding such Contributions.

   6. Trademarks. This License does not grant permission to use the trade
      names, trademarks, service marks, or product names of the Licensor,
      except as required for reasonable and customary use in describing the
      origin of the Work and reproducing the content of the NOTICE file.

   7. Disclaimer of Warranty. Unless required by applicable law or
      agreed to in writing, Licensor provides the Work (and each
      Contributor provides its Contributions) on an "AS IS" BASIS,
      WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
      implied, including, without limitation, any warranties or conditions
      of TITLE, NON-INFRINGEMENT, MERCHANTABILITY, or FITNESS FOR A
      PARTICULAR PURPOSE. You are solely responsible for determining the
      appropriateness of using or redistributing the Work and assume any
      risks associated with Your exercise of permissions under this License.

   8. Limitation of Liability. In no event and under no legal theory,
      whether in tort (including negligence), contract, or otherwise,
      unless required by applicable law (such as deliberate and grossly
      negligent acts) or agreed to in writing, shall any Contributor be
      liable to You for damages, including any direct, indirect, special,
      incidental, or consequential damages of any character arising as a
      result of this License or out of the use or inability to use the
      Work (including but not limited to damages for loss of goodwill,
      work stoppage, computer failure or malfunction, or any and all
      other commercial damages) as a result of this License or any
      claim of liability or other legal damages.

   9. Accepting Warranty or Additional Liability. While redistributing
      the Work or Derivative Works thereof, You may choose to offer,
      and charge a fee for, acceptance of support, warranty, indemnity,
      or other liability obligations and/or rights consistent with this
      License. However, in accepting such obligations, You may act only
      on Your own behalf and on Your sole responsibility, not on behalf
      of any other Contributor, and only if You agree to indemnify,
      defend, and hold each Contributor harmless for any liability
      incurred by, or claims asserted against, such Contributor by reason
      of your accepting any such warranty or additional liability.

   END OF TERMS AND CONDITIONS
```

## Functional Source License, Version 1.1, MIT Future License

The Crush mark comes from a repository licensed under FSL-1.1-MIT
(Copyright Charmbracelet, Inc.):

```
Functional Source License, Version 1.1, MIT Future License

Abbreviation: FSL-1.1-MIT

Terms and Conditions

Licensor ("We")

The party offering the Software under these Terms and Conditions.

The Software

The "Software" is each version of the software that we make available under
these Terms and Conditions, as indicated by our inclusion of these Terms and
Conditions with the Software.

License Grant

Subject to your compliance with this License Grant and the Patents,
Redistribution and Trademark clauses below, we hereby grant you the right to
use, copy, modify, create derivative works, publicly perform, publicly display
and redistribute the Software for any Permitted Purpose identified below.

Permitted Purpose

A Permitted Purpose is any purpose other than a Competing Use. A Competing Use
means making the Software available to others in a commercial product or
service that:

1. substitutes for the Software;

2. substitutes for any other product or service we offer using the Software
   that exists as of the date we make the Software available; or

3. offers the same or substantially similar functionality as the Software.

Permitted Purposes specifically include using the Software:

1. for your internal use and access;

2. for non-commercial education;

3. for non-commercial research; and

4. in connection with professional services that you provide to a licensee
   using the Software in accordance with these Terms and Conditions.

Patents

To the extent your use for a Permitted Purpose would necessarily infringe our
patents, the license grant above includes a license under our patents. If you
make a claim against any party that the Software infringes or contributes to
the infringement of any patent, then your patent license to the Software ends
immediately.

Redistribution

The Terms and Conditions apply to all copies, modifications and derivatives
of the Software.

If you redistribute any copies, modifications or derivatives of the Software,
you must include a copy of or a link to these Terms and Conditions and not
remove any copyright notices provided in or with the Software.

Disclaimer

THE SOFTWARE IS PROVIDED "AS IS" AND WITHOUT WARRANTIES OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING WITHOUT LIMITATION WARRANTIES OF FITNESS FOR A PARTICULAR
PURPOSE, MERCHANTABILITY, TITLE OR NON-INFRINGEMENT.

IN NO EVENT WILL WE HAVE ANY LIABILITY TO YOU ARISING OUT OF OR RELATED TO THE
SOFTWARE, INCLUDING INDIRECT, SPECIAL, INCIDENTAL OR CONSEQUENTIAL DAMAGES,
EVEN IF WE HAVE BEEN INFORMED OF THEIR POSSIBILITY IN ADVANCE.

Trademarks

Except for displaying the License Details and identifying us as the origin of
the Software, you have no right under these Terms and Conditions to use our
trademarks, trade names, service marks or product names.

Grant of Future License

We hereby irrevocably grant you an additional license to use the Software under
the MIT license that is effective on the second anniversary of the date we make
the Software available. On or after that date, you may use the Software under
the MIT license, in which case the following will apply:

Permission is hereby granted, free of charge, to any person obtaining a copy of
this software and associated documentation files (the "Software"), to deal in
the Software without restriction, including without limitation the rights to
use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies
of the Software, and to permit persons to whom the Software is furnished to do
so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```
