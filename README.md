# Little Craft Launcher — Android game runtime (LGPL component)

This branch is the complete corresponding source of the **game runtime** that
the Little Craft Launcher Android client uses to run Minecraft: Java Edition.
It is kept apart from the launcher itself, which only calls it through a fixed
interface.

The runtime is [Amethyst](https://github.com/AngelAuraMC/Amethyst-Android)
(successor of PojavLauncher, based on Boardwalk), licensed under the
**GNU Lesser General Public License v3.0**. See `LICENSE.md`.

## What is here

| File | Meaning |
| --- | --- |
| `UPSTREAM` | The exact upstream repository and commit the runtime is built from. |
| `patches/*.patch` | Every change Little Craft Launcher makes on top of that commit, applied in name order with `git am`. Empty means the upstream source is used unmodified. |
| `LICENSE.md` | Licence notice and pointers to the licence texts. |

## Rebuilding it yourself

```sh
. ./UPSTREAM
git clone --recursive "$UPSTREAM_REPOSITORY" runtime && cd runtime
git checkout "$UPSTREAM_COMMIT" && git submodule update --init --recursive
for p in ../patches/*.patch; do [ -e "$p" ] && git am "$p"; done
./gradlew :app_pojavlauncher:testDebugUnitTest --tests "net.kdt.pojavlaunch.lcl.*"
./gradlew :app_pojavlauncher:assembleDebug
```

The **Android runtime** workflow of this repository does exactly this. Each
released Android build of the launcher states the commit of this branch it was
built from, so the matching source can always be retrieved.
