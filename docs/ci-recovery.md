# CI 恢复与交接

这份说明让任何有仓库权限的维护者或 Agent 都能接手诊断、验证和发布。日常先用公开的安全失败摘要定位私有源码，不必先取得诊断私钥；只有摘要无法解释的问题才读取完整加密日志。

## 仓库与工作流

启动器源码位于维护者授权的私有源码仓库；本仓库仅保存公开介绍与构建工具。公开构建仓库是 [TaoXiaoBai/little-craft-launcher-releases](https://github.com/TaoXiaoBai/little-craft-launcher-releases)，通过 Actions Secrets 中的 `SOURCE_REPOSITORY` 和 `SOURCE_READ_TOKEN` 读取源码。接手者使用自己已授权的仓库连接，不需要读取或索取这些 Secret 的值。

读取摘要需要公开仓库的 Actions 读权限，派发需要 Actions 写权限，发布还需要 Contents 写权限；读取私有源码是另一个仓库的授权。遇到 401/403 时核对已有连接与具体缺失权限，让维护者授权连接，不让用户把令牌发到聊天里。

所有下列工作流都从公开仓库的默认分支 `main` 手动派发：

| 文件 | 用途与结果 |
| --- | --- |
| `prerelease.yml` | 完整构建、分组测试和打包。单平台不自动发布；显式 `source_commit` 生成不可发布的 test 证明。正常版本／默认主线构建生成 release 候选，`platform=both` 成功后会自动发布预发布版本。 |
| `publish-verified-prerelease.yml` | 检查两次指定构建的来源证明和安装包，再发布；`verify_only=true` 完整校验但不发布、不清理产物。 |
| `windows-build.yml` | 旧的动态 Windows 全量 CI，不产生发布包；`verify_access_only=true` 只检查源码访问，不能算构建或测试通过。源码由 `SOURCE_REF` Secret 或默认 `master` 选择，需要精确复现时优先用 `prerelease.yml`。 |
| `android-build.yml` | Android host 测试和 debug APK。`source_ref` 为完整私有提交 SHA；`upload_apk` 控制是否上传公开的七天 APK 产物，不创建桌面 release。 |
| `diagnose-windows-ui.yml` | 针对指定 UI profile 收集诊断，不代替整套发布验证。 |
| `diagnose-windows-bundle.yml` | 验证 Windows 单文件引导、运行库准备与提取，不发布。 |
| `benchmark-windows-build.yml` | 同一 runner 上比较构建调度耗时，不是完整测试或发布证据。 |

参数以公开仓库当前工作流为准，可用 `gh workflow view WORKFLOW.yml --repo TaoXiaoBai/little-craft-launcher-releases --yaml` 查看。私有仓库的 `native-ci.yml` 和 `native-static-ci.yml` 仅供明确需要且有额度时手动使用；当前会话不派发私有 CI。

## 先用无密钥诊断

在一份干净目录取得公开工具，或使用已有公开仓库 checkout 的最新 `main`。不要重置别人正在修改的 checkout。

```sh
public_checkout='/绝对路径/公开构建仓库'
private_checkout='/绝对路径/私有源码仓库'
gh repo clone TaoXiaoBai/little-craft-launcher-releases "$public_checkout"
gh run list --repo TaoXiaoBai/little-craft-launcher-releases --limit 10
```

选择失败 run 的 ID，然后运行：

```sh
python3 "$public_checkout/.github/scripts/ci_triage.py" \
  --repository TaoXiaoBai/little-craft-launcher-releases \
  --run-id RUN_ID \
  --source-root "$private_checkout"
```

同一 run 有多个平台时可加 `--platform windows-ui-companion` 等筛选；默认读取当前 attempt，排查历史重跑可明确加 `--attempt N`。工具使用只读 GitHub API，不要求新的 `gh --slurp` 参数，不修改工作区，也不执行下载内容。

定位命令在有源码权限的本地／云工作区运行。`--safe-only` 只输出安全 JSON，不读取源码；在公开 Actions 中应使用这个模式，不能把本地解析后的私有路径与代码贴回公开日志。已经下载的摘要可用 `--summary-file /路径/summary.json --source-root "$private_checkout"` 离线定位，此模式不带 `--run-id`、仓库或 attempt 参数，输出会明确标记来源尚未经 API 核验。需要更多上下文时可加 `--context 3`（允许 0–10）。

工作流上传 `safe-diagnostics-<platform>-attempt-N`，归档内部只有 `summary.json`，保留 14 天。平台包括 `windows-x64`、`linux-x86_64`、两种 `*-ui-companion`、`android`、`windows-build`、`windows-ui`、`windows-bundle` 和 `windows-benchmark`。

摘要包含 run/attempt、源码提交、受控错误类别、已公开的失败测试组、源码文件指纹及行列号。它不包含原始路径、源码、断言内容、测试数据行、用户名、令牌或原始日志。工具在私有 checkout 的对应 Git tree 中匹配文件指纹，通过 `git show` 只在本地显示实际文件与代码位置。

若源码提交尚未在本地，按工具提示从已经授权的私有 remote 获取该提交，然后建立隔离 worktree。不要把失败 run 的行号直接套到另一个提交：

```sh
git -C "$private_checkout" fetch origin SOURCE_COMMIT
git -C "$private_checkout" worktree add --detach /绝对路径/故障复现目录 SOURCE_COMMIT
```

`SOURCE_COMMIT` 必须是摘要里的完整 40 位 SHA。原 checkout 的未提交改动继续保留。修复后先进行相关本地复现，再跑完整云端门禁。

摘要不是原始日志的替代品。旧 run 可能早于摘要功能；过期产物、缺失 checkout、外部 SDK 错误或未识别格式也可能没有可映射位置。`sourceCommit=null`、`UNKNOWN_FAILURE` 或 `truncated=true` 都不能当成通过，此时继续看公开失败阶段，必要时本地解密完整日志。

## 完整日志与私钥

完整诊断仍以 `.p7m` 加密留存，当前加密诊断产物通常保留三天。原始日志不应重新上传到公开 issue、Actions artifact 或聊天。

| 平台／工作流 | 加密产物名 |
| --- | --- |
| Windows 主构建 | `encrypted-diagnostics-windows-x64` |
| Linux 主构建 | `encrypted-diagnostics-linux-x86_64` |
| Windows companion | `encrypted-diagnostics-windows-ui-companion` |
| Linux companion | `encrypted-diagnostics-linux-ui-companion` |
| Android | `encrypted-diagnostics-android` |
| 旧 Windows CI | `encrypted-diagnostics-windows-build` |
| UI 诊断 | `encrypted-native-ui-diagnostics` |
| 单文件包诊断 | `encrypted-windows-bundle-diagnostic` |
| 编译调度基准 | `encrypted-windows-build-benchmark` |

诊断私钥由维护者保管，工作区里的副本不能作为唯一备份。维护者应在自己管理的密码库或受控 Secret 存储中保留可恢复备份，并通过受控方式提供本地文件路径 `CI_DIAGNOSTICS_KEY_FILE`。读取私有源码的权限不等于取得完整日志解密权限。不要将私钥提交到公开或私有源码仓库——公开 runner 也可能读取源码仓库；不要把它放进公开工作流、安装包或聊天。

只有确实需要原始日志时，在本地受控临时目录下载和解密。例如 Windows companion：

```sh
umask 077
private_tmp="$(mktemp -d "${TMPDIR:-/tmp}/lcl-ci.XXXXXX")"
key_file="${CI_DIAGNOSTICS_KEY_FILE:?请设置维护者提供的本地密钥文件路径}"
gh run download RUN_ID --repo TaoXiaoBai/little-craft-launcher-releases \
  --name encrypted-diagnostics-windows-ui-companion --dir "$private_tmp/encrypted"
openssl cms -decrypt -binary -inform DER \
  -in "$private_tmp/encrypted/diagnostics-windows-ui-companion.p7m" \
  -inkey "$key_file" -out "$private_tmp/diagnostics.zip"
unzip -l "$private_tmp/diagnostics.zip"
```

不同工作流的 `.p7m` 文件名可能不同，先确认下载目录里的文件。解压只放在临时目录；只把修复和必要的安全结论写回仓库，结束后按维护者的保留策略清理本地明文。这里的证书只用于日志加密，不是 Windows 代码签名或 OAuth 密钥。

轮换时，在维护者控制的安全环境生成并备份新私钥，验证备份能够恢复，再通过 PR 仅更新公开仓库的 `.github/scripts/diagnostics-certificate.pem`。用一份自有测试日志执行加密／本地解密往返；旧私钥保留到旧密文和归档的保留期结束。记录新旧证书的 SHA-256 指纹，不能用新证书推断旧日志也可解密。日常修 CI 不需要轮换证书。

```sh
openssl x509 -in "$public_checkout/.github/scripts/diagnostics-certificate.pem" \
  -noout -fingerprint -sha256
```

## 先做不可发布的修复验证

本地验证完成后，先提交并推送修复分支，再固定该提交的完整 SHA；这一步可以在合入 `master` 之前进行。不要把尚未提交的本地文件当成该 SHA 已包含的内容。新代码的 schema 2 门禁是 Windows 44、Linux 45 个完整 CTest 组；schema 1 的 43/44 仅用于对应历史源码。局部复现可选某个 QtTest case，发布门禁仍必须执行完整分组与所有必要包检查，不能借用历史 PASS 或跳过失败行。

下面的 `N` 必须换成未使用的测试序号。`source_commit` 只接受完整 SHA 且只适用于单平台，不要与 `source_ref` 同时传；`source_ref` 是历史标签的受审查 CI-only 覆盖，不是普通选择源码的参数。

```sh
source_commit="$(git -C "$private_checkout" rev-parse HEAD)"
version_tag='v1.0.0-alpha.N'
gh workflow run prerelease.yml --repo TaoXiaoBai/little-craft-launcher-releases \
  --ref main -f tag="$version_tag" -f platform=windows -f source_commit="$source_commit"
gh workflow run prerelease.yml --repo TaoXiaoBai/little-craft-launcher-releases \
  --ref main -f tag="$version_tag" -f platform=linux -f source_commit="$source_commit"
```

这两次构建的证明用途是 `test`，不可拿去发布；即使该提交随后已经合入 `master`，也不能把旧证明改成 release。记下这两次新 run 的 ID 并监控结果，不要因暂时没输出而重复派发同版本。

```sh
gh run watch WINDOWS_RUN_ID --repo TaoXiaoBai/little-craft-launcher-releases --exit-status
gh run watch LINUX_RUN_ID --repo TaoXiaoBai/little-craft-launcher-releases --exit-status
```

若修复又提交了新 SHA，应重新构建候选。不要把不同源码时点、不同 attempt 或不同公开编译配置的证明拼起来。失败重跑还需遵守工作流的当前 attempt 完整证明要求；仅重跑失败 shard 不能冒充整个新 attempt 已验证。

## 制作可发布候选

修复验证通过并合入 `master` 后，选择尚未发布的 `v1.0.0-pre.N`，先在公开仓库提交该版本的 `.github/release-notes/<tag>.md`。正常构建不传 `source_commit`，由 resolver 读取同名私有 tag，或在没有该 tag 时锁定默认分支提交。

如果当前任务已经明确授权立即发布，可以用一次 `platform=both`：它只解析一次源码，并在两平台完整验证后自动发布。

```sh
version_tag='v1.0.0-pre.N'
gh workflow run prerelease.yml --repo TaoXiaoBai/little-craft-launcher-releases \
  --ref main -f tag="$version_tag" -f platform=both
```

若要先验证后审阅，正常派发两个单平台构建，再走下一节的只验证流程：

```sh
gh workflow run prerelease.yml --repo TaoXiaoBai/little-craft-launcher-releases \
  --ref main -f tag="$version_tag" -f platform=windows
gh workflow run prerelease.yml --repo TaoXiaoBai/little-craft-launcher-releases \
  --ref main -f tag="$version_tag" -f platform=linux
```

它们应生成 `purpose=release` 的证明。为避免两次派发期间主线移动，准备候选时可在私有仓库新增同名版本 tag，指向已验证的固定提交；先确认 tag 尚不存在，绝不移动旧 tag。若使用默认主线，则确保两次 resolver 取到同一提交，发布校验发现不一致时重建候选，不放宽检查。后续使用的是这对新的 release 候选 run ID，不是上一节的 test run。

## 先验证发布，再执行已有授权

两平台的 provenance 绑定对应 run/attempt、完整测试、同一源码及公开编译配置摘要，以及各自最终 ZIP／AppImage 的 SHA-256。发布还要求两份证明均为 release 用途。跨平台正常换行差异不要求 `sourceInputsSha256` 相同；源码或配置摘要不同、缺少 provenance、旧 attempt、包被改动都必须拒绝。

先做一次不写 release 的完整验证：

```sh
gh workflow run publish-verified-prerelease.yml \
  --repo TaoXiaoBai/little-craft-launcher-releases --ref main \
  -f tag="$version_tag" -f windows_run_id=WINDOWS_RUN_ID -f linux_run_id=LINUX_RUN_ID \
  -f verify_only=true
```

`verify_only=true` 仍下载并检查完整包与来源，但不创建 release、不删除构建产物。它也可检查 test 用途或旧 v1 证明，但通过不代表这些产物获得发布资格。缺少 provenance 不会回退到只看版本标题；应重新构建，不能补造或改写证明。

| 拒绝码 | 处理 |
| --- | --- |
| `RELEASE_PURPOSE_NOT_PUBLISHABLE` | 当前包是 test 用途或旧 v1 证明。合并修复后按正常版本／主线重新生成 release 候选，不改旧证明。 |
| `RELEASE_SOURCE_MISMATCH` / `RELEASE_CONFIGURATION_MISMATCH` | 两平台源码或公开编译配置不同。重新选择同一候选并构建，不能忽略摘要比较。 |
| `RELEASE_RUNTIME_DIGEST_MISMATCH` | 下载文件与对应证明不符。核对 run、attempt 和资产，不能重命名其他版本的包来补齐。 |
| `RELEASE_RECEIPT_MISSING` | 构建过早、缺失或证明已过期。重新构建；只看成功标题不能补上来源证明。 |
| `SAFE_SUMMARY_MISSING_USE_RUNBOOK` | 旧 run 或过期摘要。查看失败阶段，再按维护者可用的本地私钥／备份恢复旧日志，或固定源码重新复现。 |

确认当前任务已经授权发布后，使用同一版本和同一对已验证的 release 用途 run：

```sh
gh workflow run publish-verified-prerelease.yml \
  --repo TaoXiaoBai/little-craft-launcher-releases --ref main \
  -f tag="$version_tag" -f windows_run_id=WINDOWS_RUN_ID -f linux_run_id=LINUX_RUN_ID \
  -f verify_only=false
```

已有本次发布授权就继续执行，不需要再次询问。若任务只要求修 CI 或测试，停在通过的 `verify_only`，保留可审阅结果。`prerelease.yml platform=both` 是另一条会自动发布的入口，不能当成纯测试命令使用。

桌面 release 只应有 Windows 单 EXE ZIP 和 Linux AppImage 两个下载文件，标记为 prerelease 且 `latest=false`。查看发布结果和两个资产摘要，不能只凭 dispatch 成功报告发布成功：

```sh
gh release view "$version_tag" --repo TaoXiaoBai/little-craft-launcher-releases \
  --json tagName,isDraft,isPrerelease,assets,url
gh api "repos/TaoXiaoBai/little-craft-launcher-releases/releases/tags/$version_tag" \
  --jq '{draft,prerelease,assets:[.assets[]|{name,size,digest}]}'
```

旧 release、标签和资产不可覆盖；需要更正程序就创建下一个预发布版本。GitHub 自动生成的公开仓库 `Source code` 归档只包含公开介绍和构建工具，不是私有启动器源码。

## 接手时的完成记录

把修复提交／PR、实际 source SHA、公开 workflow SHA、run ID／attempt、完整测试与发布验证结果记在任务说明或 PR 中。明确区分本地验证、云端通过和实际发布；没有运行的检查不要写成已经通过。这样下一个 Agent 能直接继续，而不必寻找原聊天或某个人的记忆。
