<div align="center">

<img src="assets/launcher-icon.png" width="80" height="80" alt="Little Craft Launcher lantern">

# Little Craft Launcher

**Your worlds, your mods, your next adventure — kept in order.**

A native Minecraft Java Edition launcher for Windows and Linux, with separate instances for each way you play.

**English** · [简体中文](README.zh-CN.md)

[Downloads and release notes](https://github.com/TaoXiaoBai/little-craft-launcher-releases/releases) · [Accounts and data](#accounts-and-data) · [Report an issue](https://github.com/TaoXiaoBai/little-craft-launcher-releases/issues)

Windows 10/11 x64 · Linux x86_64 · Twelve themes + one experimental theme · Qt 6.8 / C++20 / QML

</div>

![Launchpad development preview](assets/launchpad-home.png)

> [!NOTE]
> **1.0.0 is in prerelease testing.** The pre.7 update adds Linux game launch, managed Java and system-keyring credential storage, with a single AppImage for Linux and a ZIP for Windows. Download available builds from [Releases](https://github.com/TaoXiaoBai/little-craft-launcher-releases/releases) and read their installation and account requirements. There is no stable release yet.
>
> Interface previews use Simplified Chinese. The screenshots show development windows, not a released Windows build.

## Try the prerelease

1. **Download and verify.** Get a build from [Releases](https://github.com/TaoXiaoBai/little-craft-launcher-releases/releases), read its notes and check the SHA-256. Extract the current Windows ZIP in full and run `mclauncher-qt.exe`; keep its DLLs and runtime folders together.
2. **Add an account and choose Java.** Which account types you can use depends on your region; see [Accounts and data](#accounts-and-data).
3. **Create or import an instance.** Pick a game version and loader, wait for the game files, then launch from Home. Back up your worlds before changing versions or modpacks.

Minecraft, Java and account credentials are not bundled.

### Linux AppImage

The pre.7 AppImage targets Ubuntu 22.04 or newer on x86_64, with X11/XWayland or Wayland. Make the downloaded file executable and run it. Without FUSE, use `./LittleCraftLauncher-<version>-linux-x86_64.AppImage --appimage-extract`, then run `./squashfs-root/AppRun`. Qt is bundled; graphics drivers and desktop services come from the system.

Linux supports game launch and downloads of the official Java 8/17/21/25 runtimes. You can also select a locally installed Java that matches the game version. Saving online credentials requires an available system keyring; see [Accounts and data](#accounts-and-data).

The older [v1.0.0-pre.4](https://github.com/TaoXiaoBai/little-craft-launcher-releases/releases/tag/v1.0.0-pre.4) remains an interface preview without these Linux features. Its TAR package must be extracted in full and started through `LittleCraftLauncher`; running its inner ELF directly can bypass the runtime setup. Existing pre.4 downloads are unchanged.

## What it does

| What you need | What the launcher provides |
| --- | --- |
| Separate setups | Create, copy and import instances, each with its own game version, Java, memory and launch settings. Export a modpack when you want to share one. |
| Compatible content | Browse mods, resource packs and modpacks; review dependencies, changes and the target instance before installing. |
| Favourite mods, reused | Save Modrinth projects and find compatible versions for another instance, instead of copying old JARs. |
| Worlds you can return to | Back up and export worlds, and preview an import or restore before applying it. |
| Useful feedback | Inspect tasks, logs and diagnostics, and review memory risks before launching. A memory advisory is not a compatibility or FPS verdict. |

Current scope:

- **Loaders**: Vanilla, Fabric and Quilt install directly. The native Forge installer currently targets Minecraft **1.20.1** only, and NeoForge **1.21.1** only. Other combinations are limited to what the app offers, with no compatibility promise.
- **Modpacks**: Modrinth packs can be previewed and installed as new instances. CurseForge needs an authorized API key for the current session, and its catalogue cannot create a modpack instance directly yet; local pack import works instead.

## Appearance

All twelve themes share the same pages and controls. Choose under **Settings → Appearance** when no game, sign-in or file operation is active; every theme supports reduced motion, and the lantern keeps its colours throughout. New installs start on Launchpad; existing data keeps its theme.

| Theme | What it looks like |
| --- | --- |
| **Launchpad** (`启动台`, default) | A narrow icon rail; Home opens with a "continue playing" card that holds the launch button, the instance switcher and anything blocking launch. Eight accent presets or any colour, in light, dark or system mode. |
| **Windows 11** | Rebuilt to Fluent: a Mica-style shell, a rounded content layer, Fluent controls, and Windows-style settings groups (the header on the page, the settings on a card). |
| **Material 3**, **Material 3 Expressive** | A navigation rail with a FAB; M3 switches, sliders, progress bars and tabs; Settings laid out as list sections. |
| **WinUI × MD3** | WinUI's structure with MD3's components and colours. |
| **Koring Glass** (`Koring · 轻玻璃`) | Koring's original wallpaper and centred top navigation; the launch capsule appears when you hover near the bottom of Home. |
| **Lumina** (`留白 · Lumina`) | Generous white space, serif headings and a quiet horizontal navigation; small windows switch to a scrolling column. |
| Legacy | Forest Light, Classic Dark, Graphite, Ore and Sand, folded away by default in the appearance settings. |

| Windows 11 | Material 3 |
| --- | --- |
| ![Windows 11 theme home](assets/winui3-fluent-home.png) | ![Material 3 theme home](assets/material3-home.png) |
| **Koring Glass** | **Lumina** |
| ![Koring Glass theme home](assets/koring-home-idle.png) | ![Lumina theme home](assets/lumina-home.png) |

These are real Qt development windows captured on Linux with software rendering and demonstration data. They do not establish Windows acceptance, real-account sign-in, game launch or frame rate. [Screenshot provenance and artwork credits](assets/README.md).

## Accounts and data

### Microsoft sign-in

The Microsoft sign-in implementation opens the system browser and uses the OAuth authorization-code flow with S256 PKCE and a local loopback callback. It then uses official Microsoft, Xbox and Minecraft services to validate the session, Minecraft Java Edition ownership and player profile. It does not ask users to enter their Microsoft password into the launcher's own interface. Microsoft login credentials saved on Windows are protected with DPAPI, without a plaintext fallback.

**Current status:** this project's own public-client configuration and relevant service access are still being prepared. Real-account Microsoft sign-in has not completed release acceptance. Application registration, service access and individual user consent are separate requirements; sign-in configuration must use a public Client ID owned by or explicitly authorized for this project.

### Account availability

| Region detection | Available accounts |
| --- | --- |
| Mainland China (`CN`, while the result is still valid) | Microsoft; LittleSkin or another external authentication service you explicitly trust; a local offline identity |
| Any other country or region (including `HK`, `MO` and `TW`) | Official Microsoft accounts only, with Minecraft ownership verified |
| Not yet detected, detecting, failed or expired | Official Microsoft accounts only, with Minecraft ownership verified |

Without Microsoft configuration, that sign-in flow is unavailable, so no account can be added where Microsoft is required.

LittleSkin is an address suggestion, not automatic trust. External authentication sends that service's credentials only after the user explicitly trusts the selected service. **Do not enter a Microsoft password into an external-authentication form.** External or local identities do not establish Minecraft ownership or grant access to official online services.

### Linux system keyring

Linux saves online credentials through the desktop's Secret Service keyring, such as GNOME Keyring or KeePassXC with Secret Service integration enabled. A working desktop session and an available, unlockable keyring are required. The launcher does not include a keyring daemon. A missing service, failed unlock, cancellation or timeout stops the operation with an error; credentials are not saved as plaintext instead.

### Local data

- New installs use per-user local application data; an existing `mclauncher-qt-data` beside the program stays in use.
- `--portable` uses the directory beside the Windows EXE or Linux AppImage. AppImage portable data stays beside the downloaded file, outside its temporary mount. `--data-root` sets an absolute path and takes precedence.
- Data never moves between locations on its own. Close the game and launcher before backing up or moving the whole directory.

## Feedback and project information

Maintained by [TaoXiaoBai](https://github.com/TaoXiaoBai). This repository is the public home for project information, releases and [issue reports](https://github.com/TaoXiaoBai/little-craft-launcher-releases/issues).

Launcher QQ community: **827507229**.

When reporting a problem, include the launcher version, operating system, reproduction steps, expected and actual behavior, and only the relevant redacted logs. Review logs before sharing them. Keep passwords, access tokens, account databases and private worlds out of public reports.

## Source and licensing

The application source is currently private. Opening the source will be evaluated after the launcher stabilizes; no date has been set. This public repository does not grant a general open-source or unrestricted application-redistribution license.

Third-party components and artwork retain their own licenses. Applicable notices, license texts and version-matched dependency sources or relinking materials are provided with each preview or stable release through its release notes. Keeping application development private does not remove those obligations.

This project is an independent third-party launcher, not an official Mojang or Microsoft product, and is not affiliated with external authentication providers.
