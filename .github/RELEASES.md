# Building a preview from private source

The public repository contains packaging tools and project information. It does
not contain the launcher's application source. The release workflow runs only
when manually dispatched on the default branch.

1. Create an immutable prerelease tag in the private source repository, such as
   `v1.0.0-pre.1`. Both platforms check out that exact tag; the optional
   `SOURCE_REF` used by the older access-check workflow is not used here.
   An optional full `source_ref` commit may supply a reviewed test-only fix.
   CI compares it to the immutable release tag and permits changes only to
   `qt-native/tests/`, `ci-build-test.ps1` and the CI README. Application
   sources, resources, configuration and toolchain inputs must remain identical.
2. Keep `SOURCE_REPOSITORY` and the repository-scoped, Contents-read
   `SOURCE_READ_TOKEN` in this repository's Actions secrets. The source checkout
   never persists the token. No source-write token is required by CI.
3. Run **Windows and Linux prerelease**, specifying that tag. Windows uses the
   source repository's hash-locked Qt 6.8.3 / MinGW toolchain and all 42 local
   test groups. Linux uses Qt 6.8.3 on Ubuntu 22.04 and the complete discovered
   local CTest suite. Both produce a fresh dynamic application and check an
   extraction of the actual runtime archive without the developer SDK.
4. Publication waits for both jobs. The isolated publishing job receives only
   final runtime packages and the four matching, unmodified Qt dependency-source
   archives. It creates SHA-256 checksums and publishes with `prerelease=true`
   and `latest=false`. A failed upload leaves a draft, not a partial public
   release. A tag already associated with a release must not be reused.

For a packaging-only repair, select `windows` or `linux` to run that platform's
full build, tests and startup check without publishing. After both platform jobs
succeed, **Publish verified prerelease packages** can combine their exact run
IDs. It checks repository/workflow/tag identity, platform-job success, package
metadata and dependency hashes before publication. It never accepts a failed
platform job or substitutes a historical test report for a new package check.

Windows archives must be extracted in full. Linux is an experimental interface
preview: the current game resolver, managed Java and credential storage are
still Windows-oriented. The release notes describe this boundary explicitly.
Neither platform bundles Java, Minecraft, user accounts or game data.

Only public toolchain archives and the public Linux Qt SDK are cached. Private
source/build trees, compiler output, test logs and user state are never uploaded
in plaintext. The only loose QML/JS allowed in a Windows runtime comes from the
public Qt SDK. Application QML resources remain embedded in the executable;
binary distribution is not a promise of protection against resource extraction.

## Encrypted diagnostics

Detailed build output stays on the ephemeral runner. The workflow additionally
encrypts a bounded diagnostic ZIP with `scripts/diagnostics-certificate.pem`,
then retains only its CMS `.p7m` ciphertext for three days. The corresponding
private key belongs to the build operator and must never be committed or placed
on a public runner. To inspect a downloaded diagnostic artifact locally:

```sh
openssl cms -decrypt -binary -inform DER -in diagnostics.p7m \
  -inkey /private/path/diagnostics.key -out /private/path/diagnostics.zip
```

Decrypted diagnostics can contain private application source lines. Keep them
private. Replacing the certificate is necessary when changing operators or if
the matching private key is lost. The public workflow cannot recover that key.
