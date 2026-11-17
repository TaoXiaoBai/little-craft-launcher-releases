<div align="center">

<img src="assets/launcher-icon.png" width="80" height="80" alt="Little Craft Launcher 灯笼图标">

# Little Craft Launcher

**把时间留给方块，把版本、模组和存档交给启动器。**

面向 Windows 和 Linux 的 Minecraft Java 版原生启动器。一个实例，一套配置，让不同玩法各有归处。

[English](README.md) · **简体中文**

[版本下载与更新说明](https://github.com/TaoXiaoBai/little-craft-launcher-releases/releases) · [账号与数据](#账号与数据) · [反馈问题](https://github.com/TaoXiaoBai/little-craft-launcher-releases/issues)

Windows 10/11 x64 · Linux x86_64 · 十二套主题 + 一套测试主题 · Qt 6.8 / C++20 / QML

</div>

![启动台开发版界面预览](assets/launchpad-home.png)

> [!NOTE]
> **1.0.0 现处于预发布测试阶段，尚无正式版。** 请从 [Releases](https://github.com/TaoXiaoBai/little-craft-launcher-releases/releases) 下载可用版本。每个版本的发行说明会列出更新内容、运行要求和 SHA-256 校验值。

## 试用预发布版本

| 平台 | 下载文件 | 启动方式 |
| --- | --- | --- |
| Windows 10/11 x64 | Windows ZIP | 解压后运行唯一的 `mclauncher-qt.exe`，运行依赖已内置。 |
| Linux x86_64 · Ubuntu 22.04+ | Linux AppImage | 赋予执行权限后运行，支持 X11/XWayland 和 Wayland。 |

1. **下载并校验。** 从 [Releases](https://github.com/TaoXiaoBai/little-craft-launcher-releases/releases) 选择对应平台的文件，核对 SHA-256。[Windows 分发说明](docs/WINDOWS_DISTRIBUTION.md)
2. **添加账号，选择 Java。** 可以复用电脑上兼容的 Java，也可以在启动器中下载受管运行时。账号可用范围见下文[账号与数据](#账号与数据)。
3. **创建或导入实例。** 选择游戏版本和加载器，等文件下载完成后从首页启动。更换版本或整合包前，请先备份存档。

启动器不附带 Minecraft、Java 或账号凭据。

### Linux AppImage

没有 FUSE 时，可执行 `./LittleCraftLauncher-<版本>-linux-x86_64.AppImage --appimage-extract`，再运行 `./squashfs-root/AppRun`。包内附带 Qt，显卡驱动和桌面服务由系统提供。

Linux 支持游戏启动，以及官方运行时 Java 8/17/21/25 的下载管理，也可选择与游戏版本匹配的本机 Java。在线凭据保存需要可用的系统密钥环，见下文[账号与数据](#账号与数据)。

## 能帮你做什么

| 你想做的事 | 启动器如何帮忙 |
| --- | --- |
| 把不同周目分开 | 创建、复制和导入实例，分别设置游戏版本、Java、内存、启动参数和进服地址；需要分享时可导出整合包。 |
| 装上合适的内容 | 浏览模组、资源包和整合包，确认目标实例，预览依赖与变更后再安装。 |
| 下个版本接着用常用模组 | 收藏 Modrinth 项目，为新实例重新寻找兼容版本，不必照搬旧 JAR。 |
| 给存档留条退路 | 备份、导出世界，导入或恢复前先查看预览。 |
| 管理游戏周边文件 | 管理截图与原理图，查看或编辑 NBT 数据；编辑前请先备份文件。 |
| 和朋友一起玩 | 接入陶瓦联机，可创建或加入房间；首次下载组件前会征求同意，多设备联机仍在测试中。 |
| 看懂运行状态 | 查看任务、日志和诊断，启动前检查内存风险。内存提示不代表帧率或完整兼容性结论。 |
| 更新与语言 | 在关于页检查更新并打开发布页；界面可选简体中文、英文或自动选择，部分诊断和实验文案仍为中文。 |

当前范围：

- **加载器**：原版、Fabric、Quilt 可以直接安装。Forge 从 Minecraft **1.18** 起、NeoForge 从 **1.20.2** 起按官方目录自动发现兼容版本，安装需符合当前支持的安装器格式，具体组合以应用内可选项为准。旧版 Forge（如 1.12.2、1.16.5）及 NeoForge 1.20.1 尚未接入；安装器格式发生变化时仍可能需要适配。
- **整合包**：Modrinth 整合包可以在线预览并安装为新实例。CurseForge 需要仅限本次会话的授权 API key，目录中暂不能直接新建整合包实例，可以改用本地导入。

## 外观

在没有游戏、登录或文件操作进行时，可到**设置 → 外观**中切换。各主题以不同布局呈现相同的核心功能，支持减少动态效果，灯笼图标始终保持原色；也可选择本机图片作为窗口背景。新安装默认使用启动台，已有数据保留原来的主题。

| 主题 | 特点 |
| --- | --- |
| **启动台**（默认） | 左侧窄图标栏；首页最上方的“继续游玩”大卡片集中了启动按钮、实例切换和启动阻碍。可选 8 种预设强调色或任意颜色，以及亮色、深色、跟随系统。 |
| **Windows 11** | 按 Fluent 规范重做：Mica 风格外壳、圆角内容层、Fluent 控件，设置页“标题在页面上、设置项在卡片里”。 |
| **Material 3**、**Material 3 Expressive** | 带 FAB 的导航栏，M3 开关、滑块、进度条和选项卡，设置页按列表分区。 |
| **WinUI × MD3** | 取 WinUI 的结构，用 MD3 的组件和配色。 |
| **Koring · 轻玻璃** | Koring 原作壁纸、顶部居中胶囊导航；鼠标移到首页底部时显出启动胶囊。 |
| **留白 · Lumina** | 大留白、衬线标题、克制的横向导航，小窗口中自动改为纵向滚动。 |
| 旧主题 | 林影亮色、经典深色、石墨、矿物、岩沙，在外观设置中默认折叠。 |

| Windows 11 | Material 3 |
| --- | --- |
| ![Windows 11 主题首页](assets/winui3-fluent-home.png) | ![Material 3 主题首页](assets/material3-home.png) |
| **Koring · 轻玻璃** | **留白 · Lumina** |
| ![Koring · 轻玻璃主题首页](assets/koring-home-idle.png) | ![留白 · Lumina 主题首页](assets/lumina-home.png) |

截图来自 Linux 上的 Qt 开发窗口，使用演示数据；实际外观会随平台和版本变化。[截图来源与素材许可](assets/README.md)。

## 账号与数据

### Microsoft 登录

Microsoft 登录会打开系统浏览器，通过 Microsoft、Xbox 与 Minecraft 官方服务验证会话、Minecraft Java 版拥有权和角色资料。启动器不会要求你在其自身界面输入 Microsoft 账号密码。Windows 上保存的在线凭据使用 DPAPI 保护，不提供明文回退。

**测试状态：**发布版本已包含本项目的 Microsoft 客户端配置。请使用拥有 Minecraft Java 版的账号；真实账号的发行验收与 Mojang 服务准入仍待确认。

### 账号可用范围

| 地区检测结果 | 可用账号 |
| --- | --- |
| 中国大陆（`CN`，且检测结果仍有效） | Microsoft；LittleSkin 或你明确信任的其他外置认证服务；本机离线身份 |
| 其他国家和地区（含 `HK`、`MO`、`TW`） | 仅 Microsoft 官方账号，并验证 Minecraft 拥有权 |
| 未检测、检测中、检测失败或已过期 | 仅 Microsoft 官方账号，并验证 Minecraft 拥有权 |

LittleSkin 只是地址建议，不代表自动信任。外置认证仅在用户明确信任所选服务后，向该服务发送其凭据。**不要把 Microsoft 密码填入外置认证表单。** 外置或本机身份不代表 Minecraft 正版拥有权，也不授予官方在线服务访问权。

### Linux 系统密钥环

Linux 通过桌面的 Secret Service 密钥环保存在线凭据，例如 GNOME Keyring，或开启 Secret Service 集成的 KeePassXC。需要正常运行的桌面会话和可用、可解锁的密钥环；启动器不内置密钥环守护进程。服务缺失、解锁失败、取消或超时都会停止操作并提示错误，不会改存明文凭据。

### 本地数据

- 新安装默认在程序所在目录可写时使用其旁的 `mclauncher-qt-data`；不可写时回退到每用户本地应用数据目录。
- 程序旁已有的 `mclauncher-qt-data` 会继续沿用；如果程序旁没有该目录，但每用户目录中已有数据，则继续沿用已有数据，不另建程序旁的数据目录。下述显式选项优先于这套自动选择规则。
- `--portable` 使用 Windows EXE 或 Linux AppImage 旁的便携目录；AppImage 的便携数据保存在下载文件旁，不写入临时挂载目录。`--data-root` 指定一个绝对路径，优先级最高。
- 数据不会在目录之间自动迁移。备份或移动整个数据目录前，请先关闭游戏和启动器。

## 项目信息与反馈

项目由 [TaoXiaoBai](https://github.com/TaoXiaoBai) 维护。本仓库是公开的项目介绍、版本发布和[问题反馈](https://github.com/TaoXiaoBai/little-craft-launcher-releases/issues)入口。

启动器 QQ 交流群：**827507229**。

反馈时请附启动器版本、操作系统、复现步骤、预期与实际行为，以及相关的脱敏日志。分享前请自行检查日志；不要公开密码、访问令牌、账号库或私人存档。

## 源码与许可

应用源码暂未公开，待启动器稳定后再评估开源安排，时间尚未确定。本公开仓库不授予应用的一般性开源或任意再分发许可。

第三方组件与素材继续遵循各自的许可。适用的通知、许可文本及与版本匹配的依赖源码或可重链接材料，随预发布及正式版本提供，并在发布说明中给出获取入口。应用开发保持私有，不免除这些交付义务。

本项目是独立的第三方启动器，并非 Mojang 或 Microsoft 官方产品，亦不与外置认证服务运营方存在隶属关系。
