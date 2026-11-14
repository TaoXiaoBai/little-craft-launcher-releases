# 构建仓库协作说明

处理 CI、构建、诊断或发布任务前，先读 [CI 恢复与交接](docs/ci-recovery.md)。这里保存公开工具，启动器源码由维护者授权的私有仓库提供。

- 先用 `ci_triage.py` 读取当前 run/attempt 的安全摘要；源码文件指纹可在已授权的私有 checkout 中定位，不要求诊断私钥。没有源码权限时使用 `--safe-only`。
- 原始编译／测试日志只加密留存。不能把私钥、令牌、解密后的日志、私有源码或解析后的代码位置上传到公开日志、issue 或 artifact。
- 工作流保持手动触发；完整门禁为 schema 2 的 Windows 44／Linux 45 组，不得删用例、跳数据行、放宽断言或借用旧 PASS 来恢复绿色。
- 两平台发布必须同源码、同公开编译配置、匹配最终包哈希，且证明用途为 release。显式 `source_commit` 的 test 构建与旧 v1 证明只能只验证，不能改成发布资格。
- 独立发布先完成 `verify_only`，再按当前任务已有授权发布；已授权的 `platform=both` 流程会内置完整校验后自动发布。已有授权不重复询问，没有发布授权时仅保留验证产物。
- 桌面预发布只提供 Windows 单 EXE ZIP 与 Linux AppImage，保持 prerelease、`latest=false`，不改写旧 release 或资产。
