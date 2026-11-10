# Building a preview from private source

The public repository contains packaging tools and project information. It does
not contain the launcher's application source. The release workflow runs only
when manually dispatched on the default branch.

1. Create an immutable prerelease tag in the private source repository, such as
   `v1.0.0-pre.5`. Both platforms check out that exact tag; the optional
   `SOURCE_REF` used by the older access-check workflow is not used here.
   An optional full `source_ref` commit may supply a reviewed test-only fix.
   The private-source comparison gate rejects application, resource and runtime
   toolchain changes; any test-configuration exception must match its explicitly
   reviewed inputs. This override is not a way to ship different application code.
2. Keep `SOURCE_REPOSITORY` and the repository-scoped, Contents-read
   `SOURCE_READ_TOKEN` in this repository's Actions secrets. The source checkout
   never persists the token. No source-write token is required by CI.
3. Run **Windows and Linux prerelease**, specifying that tag. Windows uses the
   source repository's hash-locked Qt 6.8.3 / MinGW toolchain and its complete
   required local test suite. Linux uses Qt 6.8.3, including Qt Wayland and QtDBus,
   on Ubuntu 22.04 and runs the complete discovered local CTest suite. Both
   produce a fresh dynamic application. Package startup checks use the actual
   Windows ZIP or Linux AppImage rather than a developer-SDK launch; Linux
   checks XCB and Wayland separately and exercises extraction without FUSE.
4. Publication waits for both jobs. The isolated publishing job receives only
   the Windows ZIP, Linux AppImage and five matching, unmodified Qt dependency
   source archives: four from the Windows `release-qt-dependency-sources`
   artifact, plus Qt Wayland from Linux's separate `release-qt-wayland-source`
   artifact. These seven inputs are verified before only the two runtime
   packages are published. Checksums and matching dependency-source acquisition
   details appear in the release notes. Publication uses `prerelease=true` and
   `latest=false`; a failed upload leaves a draft, not a partial public release.
   A tag already associated with a release must not be reused.

Windows and Linux software-rendered CI enable
`MCL_SERIALIZE_SOFTWARE_UI=ON`. The full UI matrices share CTest's renderer
resource lock, while `--parallel 2` still allows backend groups to run alongside
one UI group. Each UI suite starts with its own empty QML compilation cache.
All registered functions, data rows and assertions remain enabled. Linux software-rendering budgets include the complete 167-row UI regression and 183-row local UI matrix, including their final scroll walks and About-page rows.

The existing `v1.0.0-pre.4` release is frozen. Verification and cleanup retain
its Linux TAR and four-source-archive contract; they do not rewrite that release
or require its historical package to acquire Qt Wayland. New prerelease tags
use the AppImage and five-source-archive contract.

For a packaging-only repair, select `windows` or `linux` to run that platform's
full build, tests and startup check without publishing. After both platform jobs
succeed, **Publish verified prerelease packages** can combine their exact run
IDs. It checks repository/workflow/tag identity, platform-job success, package
metadata and dependency hashes before publication. It never accepts a failed
platform job or substitutes a historical test report for a new package check.

Windows archives must be extracted in full. Linux is delivered as one x86_64
AppImage; add execute permission before starting it. Its host baseline remains
Ubuntu 22.04 or a compatible system, an X11 or Wayland desktop and working host
graphics drivers. If FUSE is unavailable, use `--appimage-extract-and-run`, or
`--appimage-extract` followed by `squashfs-root/AppRun`. Extracting also provides
a writable tree for replacing compatible Qt libraries, plugins and QML modules;
the package's supporting materials describe this dynamic-library replacement.

The Linux package contract introduced for pre.5 includes Linux x86_64 game
natives, version rules and classpath handling. Managed Java supports the
official Mojang Java 8, 17, 21 and 25 runtimes, validating individual file hashes,
executable permissions and safe in-tree links. Java is downloaded when requested;
neither platform bundles Java, Minecraft, user accounts or game data.

Linux credential storage uses QtDBus to access the session's Secret Service
asynchronously. The host must supply an enabled, unlockable provider, such as
GNOME Keyring or KeePassXC with its Secret Service integration enabled. The
AppImage does not bundle a keyring daemon or configure the host provider. A
missing session bus or provider, an unresolved locked collection, cancellation
or timeout produces an explicit failure, never a plaintext credential fallback.

AppImage startup restores the host library-search environment for launched Java
and browser processes. For AppImage launches in portable mode, persistent data
belongs beside the original carrier, not inside its temporary mount or
extraction tree.

Local tests and package-startup checks do not establish real-account login,
an actual Minecraft session or compatibility with particular GPU hardware and
drivers; those require separate acceptance results.

Only verified public dependency-source and toolchain archives, pinned deployment
tools and public Qt SDKs are cached. Private source/build trees, compiler output,
test logs and user state are never uploaded in plaintext. Loose QML/JS in either
runtime comes from the public Qt SDK. Application QML resources remain embedded
in the executable; binary distribution is not a promise of protection against
resource extraction.

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
