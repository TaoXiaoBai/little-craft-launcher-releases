<div align="center">

<img src="assets/launcher-icon.png" width="80" height="80" alt="Little Craft Launcher 灯笼图标">

# Little Craft Launcher

**把时间留给方块，把版本、模组和存档交给启动器。**

面向 Windows 的 Minecraft Java 版原生启动器。一个实例，一套配置，让不同玩法各有归处。

[English](README.md) · **简体中文**

[版本下载与更新说明](https://github.com/TaoXiaoBai/little-craft-launcher-releases/releases) · [账号与数据](#账号与数据) · [反馈问题](https://github.com/TaoXiaoBai/little-craft-launcher-releases/issues)

Windows 10/11 x64 · 十一套主题 · Qt Quick

</div>

> **1.0.0 准备中，本仓库尚未发布正式安装包。** 本页介绍开发版本的功能，界面仍在升级；各发行版本的支持范围与安装方式以对应发布说明为准。

## 每种玩法，都有自己的位置

| 你想做的事 | 启动器如何帮忙 |
| --- | --- |
| 把不同周目分开 | 创建、复制和导入实例，分别管理游戏版本、Java、内存和启动参数；需要分享配置时可导出整合包。 |
| 装上合适的内容 | 浏览模组、资源包和整合包，确认目标实例，预览依赖与变更后再安装。 |
| 下个版本接着用常用模组 | 收藏 Modrinth 项目，为新实例重新寻找兼容版本，无需照搬旧 JAR。 |
| 给存档留条退路 | 备份、导出世界，导入或恢复前先查看预览。 |
| 看懂运行状态 | 查看任务、日志和诊断，启动前检查内存风险；内存提示不代表帧率或完整兼容性结论。 |
| 用顺手的界面 | 十一套主题共用页面和控件，提供不同导航布局，并支持减少动态效果。 |

**加载器范围：**支持原版、Fabric、Quilt 的安装入口；原生 Forge 安装器当前面向 Minecraft **1.20.1**，NeoForge 面向 **1.21.1**。请以界面可选组合为准，其他版本不作兼容承诺。

**整合包：**Modrinth 整包可在线预览并安装到新实例。CurseForge 使用仅限本次会话的授权 API key，其目录暂不支持直接新建整包实例，可使用本地整包导入。

## 选一种习惯的外观

林影亮色、经典深色、石墨、矿物、岩沙、Windows 11、Material 3、WinUI × MD3、Material 3 Expressive，**Koring · 轻玻璃**，以及 **留白 · Lumina**，共十一套主题。它们使用同一套 Qt Quick 应用，灯笼始终保留原色。

在**设置 → 外观**中选择主题。Koring · 轻玻璃采用顶部胶囊导航和页内侧栏；留白 · Lumina 采用大留白、衬线标题和横向导航，首页将当前实例、启动操作与实用摘要并列，小窗口中自动转为可滚动的纵向布局。

![留白 · Lumina 开发版界面预览](assets/lumina-home.png)

<details>
<summary>查看小窗口预览</summary>

![留白 · Lumina 开发版小窗口预览](assets/lumina-home-small.png)

</details>

以上为真实 Qt 开发窗口，使用合成演示数据，在 Linux 下以软件渲染采集。它们不是 Windows 验收证明，也不是已发布版本截图。[截图来源与设计参考](assets/README.md)。

## 正式版本发布后三步开始

1. **下载发布包。** 从 [Releases](https://github.com/TaoXiaoBai/little-craft-launcher-releases/releases) 下载，阅读发行说明并核对 SHA-256；按该版本说明安装或完整解压。
2. **配置账号和 Java。** 确认数据位置，选择或检测兼容的 Java。Microsoft 登录的可用性取决于下方说明的项目应用注册与服务准入配置。
3. **创建或导入实例。** 选择游戏版本和加载器，准备游戏文件，再从首页启动。更换版本或整合包前，先备份存档。

启动器不附带 Minecraft、Java 或账号凭据。各版本发布说明会列明包类型、系统要求及匹配的配套材料。

## 账号与数据

### Microsoft 登录

Microsoft 登录实现通过系统浏览器打开授权页面，采用 OAuth 授权码、S256 PKCE 和本机回调，再通过 Microsoft、Xbox 与 Minecraft 官方服务验证会话、Minecraft Java 版拥有权和角色资料。启动器不会要求用户在其自身界面输入 Microsoft 账号密码。Windows 上保存的 Microsoft 登录凭据使用 DPAPI 加密，不提供明文回退。

**当前状态：**本项目自己的公共客户端配置与相关服务准入仍在准备，真实 Microsoft 账号登录的发行验收尚未完成。应用注册、服务准入与用户授权是分别需要完成的事项；登录配置须使用本项目自有或获明确授权的公共 Client ID。

### 账号可用范围

仅新鲜有效的中国大陆 `CN` 地域检测结果允许明确受信任的外置认证或用途受限的本机身份。其他地区（含 `HK`／`MO`／`TW`），以及未知或过期的检测结果，要求 Microsoft 认证和 Minecraft 拥有权验证；缺少 Microsoft 配置会阻止该登录流程。

LittleSkin 只是地址建议，不代表自动信任。外置认证仅在用户明确信任所选服务后，向该服务发送其凭据。**不要把 Microsoft 密码填入外置认证表单。** 外置或本机身份不代表 Minecraft 正版拥有权，也不授予官方在线服务访问权。

### 本地数据

新安装默认使用每用户本地应用数据目录；已有程序旁的 `mclauncher-qt-data` 会继续沿用。`--portable` 显式选择便携目录，`--data-root` 指定绝对路径且优先级更高，数据不会自动迁移。备份或移动整个数据目录前，请先关闭游戏和启动器。

## 项目信息与反馈

项目由 [TaoXiaoBai](https://github.com/TaoXiaoBai) 维护。本仓库是公开的项目介绍、版本发布和[问题反馈](https://github.com/TaoXiaoBai/little-craft-launcher-releases/issues)入口。

反馈时请附启动器版本、Windows 版本、复现步骤、预期与实际行为，以及相关的脱敏日志。分享前请自行检查日志；不要公开密码、访问令牌、账号库或私人存档。

## 源码与许可

应用源码暂未公开，待启动器稳定后再评估开源安排，时间尚未确定。本公开仓库不授予应用的一般性开源或任意再分发许可。

第三方组件与素材继续遵循各自的许可。适用的通知、许可文本及与版本匹配的源码或可重链接材料，将随正式版本提供，并在发布说明中给出获取入口。应用开发保持私有，不免除这些交付义务。

本项目是独立的第三方启动器，并非 Mojang 或 Microsoft 官方产品，亦不与外置认证服务运营方存在隶属关系。
