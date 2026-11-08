# Document safety test font

`DejaVuSans.ttf` is the unmodified DejaVu Sans 2.37 font from the
[upstream release](https://github.com/dejavu-fonts/dejavu-fonts/releases/tag/version_2_37),
specifically `dejavu-sans-ttf-2.37.zip`. `DejaVuSans.LICENSE` is the license
included in that same archive.

The embedded-font rebuild guard needs a real TrueType program on both Linux
and macOS. This test-only fixture removes an accidental dependency on a
Linux distribution's font installation; it is never served or imported by
production code. Preserve the complete font rather than subsetting it, so the
guard continues to exercise copying a full embedded program through the PDF
rebuild.

SHA-256 of the archive: `5c6e497a2f36552cb5ffb112c413a6af39c0f3c47653662b90b4fa6499822fd7`.

SHA-256 of the font: `7da195a74c55bef988d0d48f9508bd5d849425c1770dba5d7bfc6ce9ed848954`.
