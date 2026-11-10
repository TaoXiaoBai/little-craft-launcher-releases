"""Shared two-package release metadata and dependency-source directions."""

import re

QT_SOURCE_BASE_URL = "https://download.qt.io/archive/qt/6.8/6.8.3/submodules/"
QT_SOURCE_ARCHIVES = {
    "qtbase-everywhere-src-6.8.3.zip": (74010483, "992bf7766e214a341ef793eb3665fb784787d2fd666955f5f507f4c6f1f770dd"),
    "qtdeclarative-everywhere-src-6.8.3.zip": (83650162, "3ba85e9807958bd61606ce72af189be312396030b9946ca79d9e38347306d88e"),
    "qtsvg-everywhere-src-6.8.3.zip": (2651981, "90643c1ba05245abedc2bcd9d4f745568dc27a1b37dd3ba861fd19d8ac6a7c46"),
    "qtimageformats-everywhere-src-6.8.3.zip": (2999913, "30b81e4f641cdbd608ae8c01a48677338d1c69545117c5552063b63262c7eb1b"),
}


def runtime_names(tag):
    if not re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+-(pre|alpha|beta|rc)\.[0-9]+", tag):
        raise ValueError("A prerelease tag is required")
    return (f"LittleCraftLauncher-{tag}-windows-x64.zip",
            f"LittleCraftLauncher-{tag}-linux-x86_64.tar.gz")


def release_notes(repository, tag, hashes, changes=None):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]+", repository):
        raise ValueError("Invalid release repository")
    windows, linux = runtime_names(tag)
    if set(hashes) != {windows, linux} or any(
            not re.fullmatch(r"[0-9a-f]{64}", value) for value in hashes.values()):
        raise ValueError("Both runtime checksums are required")
    base = f"https://github.com/{repository}/releases/download/{tag}"
    intro = (changes or "用于测试的预发布版本，欢迎反馈使用中遇到的问题。").strip()
    lines = [intro, "", "## 下载", "",
             "| 平台 | 下载 | 启动方式 |", "| --- | --- | --- |",
             f"| Windows 10/11 x64 | [Windows ZIP]({base}/{windows}) | 完整解压后运行 `mclauncher-qt.exe` |",
             f"| Linux x86_64 · Ubuntu 22.04+ | [Linux 实验预览]({base}/{linux}) | 解压后运行 `./LittleCraftLauncher` |",
             "", "## 当前限制", "",
             "- Microsoft 登录暂不可用；LittleSkin 已配置。",
             "- Linux 暂不支持游戏启动、在线凭据保存和托管 Java 下载。",
             "- Windows 程序尚未签名。", "",
             f"[使用说明](https://github.com/{repository}#readme) · [反馈问题](https://github.com/{repository}/issues)",
             "", "<details>", "<summary>SHA-256 校验</summary>", "",
             "| 文件 | SHA-256 |", "| --- | --- |"]
    lines += [f"| {name} | `{hashes[name]}` |" for name in (windows, linux)]
    lines += ["", "</details>", "", "<details>",
              "<summary>第三方依赖与许可</summary>", "",
              "包内 `SUPPORTING_MATERIALS.zip` 提供许可文本、第三方说明及 Qt 动态库替换方法。",
              "", "对应 Qt 6.8.3 的官方源码归档如下；使用启动器无需下载：", "",
              "| Qt 依赖源码 | SHA-256 |", "| --- | --- |"]
    lines += [f"| [{name}]({QT_SOURCE_BASE_URL}{name}) | `{digest}` |"
              for name, (_, digest) in QT_SOURCE_ARCHIVES.items()]
    lines += ["", "启动器源码暂未公开。GitHub 自动生成的 `Source code` 压缩包只包含此公开仓库的介绍和发布工具。",
              "", "</details>", ""]
    return "\n".join(lines)


def verify_published_assets(tag, assets, body):
    """Refuse cleanup until both runtime uploads and source directions exist."""
    expected = set(runtime_names(tag))
    if (len(assets) != 2 or {item.get("name") for item in assets} != expected
            or not isinstance(body, str)):
        raise ValueError("A release must contain exactly the two runtime packages")
    for item in assets:
        digest = item.get("digest", "")
        if (item.get("state") != "uploaded" or not isinstance(item.get("size"), int)
                or isinstance(item["size"], bool) or item["size"] <= 0
                or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest)
                or f"| {item['name']} | `{digest[7:]}` |" not in body):
            raise ValueError("A runtime upload or its published checksum is incomplete")
    for name, (_, digest) in QT_SOURCE_ARCHIVES.items():
        if f"({QT_SOURCE_BASE_URL}{name})" not in body or f"`{digest}`" not in body:
            raise ValueError("Matching dependency-source acquisition details are missing")
