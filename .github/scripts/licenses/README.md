# ICU 73.2 notice

The Linux Qt 6.8.3 SDK ships ICU 73.2 shared libraries as a separate archive.
Its license is not part of the four Qt source-module license trees collected by
the build. `ICU-73.2-LICENSE.txt` is the complete, unmodified ICU notice from:

https://raw.githubusercontent.com/unicode-org/icu/680f521746a3bd6a86f25f25ee50a62d88b489cf/icu4c/LICENSE

- Official tag: `release-73-2`
- Bytes: 25,505
- SHA-256: `f3005e195ff74d8812cc1f182a1c446fab678d70a10e3dada497585befee5416`

The Linux packager checks that the bundled libraries resolve to version 73.2,
checks the notice hash, and copies it into both the loose notices and the
`SUPPORTING_MATERIALS.zip` companion. No application source is included here.
