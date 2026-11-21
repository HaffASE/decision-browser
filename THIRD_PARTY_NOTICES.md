# Third-party notices

These notices cover third-party material, not a license grant for Decision Browser.
Decision Browser itself is licensed under MIT; see [LICENSE](LICENSE).

## uv

The npm launcher downloads uv separately from Astral's official release service;
the uv binary is not bundled in this repository's wheel or npm artifact. The
bootstrap pins version 0.13.7 and each supported archive's SHA-256. uv is licensed
under MIT OR Apache-2.0; see the [upstream license policy](https://docs.astral.sh/uv/reference/policies/license/)
and [pinned release](https://github.com/astral-sh/uv/releases/tag/0.13.7).

## jev-ra

`scripts/benchmarks/patches/jev-ra-openai.patch` distributes upstream source
context. Preserve this notice with that patch and any copied upstream excerpts.
Pinned source and license:
https://github.com/brnyxx/jev-ra/blob/721fc73d4571d4e4ad90dab65481e52f5b2f2531/LICENSE

MIT License

Copyright (c) 2026 Brian Kim

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

## jev-ultrafast

The benchmark adapters load a separately supplied upstream checkout rather than
vendoring its package. No jev-ultrafast source patch is bundled. Preserve this
notice for any upstream-derived integration excerpts; inclusion is conservative
and does not imply that upstream dependencies are shipped in our wheel.
Pinned source and license:
https://github.com/browser-use/jev-ultrafast/blob/8251e272398011cdef648965204d1c20cd838333/LICENSE

MIT License

Copyright (c) 2026 Browser Use

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
