# Windows 下载与启动

从 `v1.0.0-pre.10` 起，Windows ZIP 只包含一个 `mclauncher-qt.exe`，Qt、插件、
QML 和编译器运行库都已封装在程序内。解压后直接运行这个 EXE 即可。
更早版本的动态包需要完整解压，并保留相邻的 DLL 和运行库目录。

首次运行自动校验并展开运行库，后续启动校验并复用缓存。缓存位于当前用户的
`LocalAppData/LittleCraftLauncher/runtime/`。账号、实例和游戏文件继续使用原来的
数据目录，便携模式继续使用外层 EXE 旁的 `mclauncher-qt-data`。
Minecraft 所需的 Java 继续在启动器的 Java 管理中下载或选择。

已知运行库文件缺失或损坏时，程序会准备完整副本并切换。遇到无法识别的缓存
内容时保留原目录并报告错误，避免误删用户放入的文件。

需要查看许可材料或替换兼容的 Qt 动态库时，在终端运行：

```text
mclauncher-qt.exe --extract-runtime <一个尚不存在的目录>
```

完整动态运行时及 `SUPPORTING_MATERIALS.zip` 会展开到指定目录。关闭程序后可以
替换兼容的 Qt 库，再运行展开目录中的 EXE。展开后的动态运行时需要保留相邻的
DLL、插件和 QML 文件。

Windows 程序目前尚未使用可信发布者证书签名，SmartScreen 的“发布者未知”提示
仍可能出现。单文件封装解决运行依赖的分发问题，不改变签名或发布者信誉状态。
