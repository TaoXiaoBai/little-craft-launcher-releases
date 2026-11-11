#Requires -Version 7.2
# Exercise the real smoke assertions and process setup without executing a PE
# or loading private application code. The release runner also runs the actual
# wrapper/CLI/GUI cases from smoke-windows-bundle.ps1 against the final ZIP.
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$tokens = $null; $errors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile((Join-Path $PSScriptRoot 'smoke-windows-bundle.ps1'), [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw 'The independent Windows smoke script did not parse.' }
$required = @('Ordinary', 'Digest', 'CanonicalPath', 'SamePath', 'AssertRuntime', 'StartInfo', 'InvokeCli')
$functions = @($ast.FindAll({ param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -cin $required }, $true))
if ($functions.Count -ne $required.Count) { throw 'The independent smoke helpers are incomplete.' }
foreach ($function in $functions) { Invoke-Expression $function.Extent.Text }
$script:checks = 0
function Check([bool]$Condition, [string]$Message) { if (-not $Condition) { throw $Message }; ++$script:checks }
function Reject([scriptblock]$Action) { $rejected = $false; try { & $Action | Out-Null } catch { $rejected = $true }; Check $rejected 'A damaged or unowned runtime was accepted.' }
$root = Join-Path ([IO.Path]::GetTempPath()) ('mcl-bundle-smoke-test-' + [guid]::NewGuid().ToString('N'))
$null = [IO.Directory]::CreateDirectory($root)
$savedToken = $env:SOURCE_READ_TOKEN
$savedQt = $env:QT_PLUGIN_PATH
try {
    Check (SamePath 'C:\用户 data\cache\mclauncher-qt.exe' '\\?\C:\用户 data\cache\mclauncher-qt.exe') 'Extended local module paths differ from ordinary local paths.'
    Reject { CanonicalPath '\\?\UNC\server\share\mclauncher-qt.exe' }
    Reject { CanonicalPath '\\.\C:\cache\mclauncher-qt.exe' }
    Reject { CanonicalPath '\\server\share\mclauncher-qt.exe' }
    $runtime = Join-Path $root 'runtime'
    $null = [IO.Directory]::CreateDirectory($runtime)
    $file = Join-Path $runtime 'mclauncher-qt.exe'
    [IO.File]::WriteAllText($file, 'inert fixture')
    $expected = [Collections.Generic.Dictionary[string,object]]::new([StringComparer]::Ordinal)
    $expected.Add('mclauncher-qt.exe', @{ Size = (Get-Item -LiteralPath $file).Length; Hash = (Digest $file) })
    AssertRuntime $runtime
    Check $true 'An intact runtime was rejected.'
    [IO.File]::WriteAllText($file, 'changed fixture')
    Reject { AssertRuntime $runtime }
    [IO.File]::WriteAllText($file, 'inert fixture')
    $state = Join-Path $runtime 'state.json'
    [IO.File]::WriteAllText($state, '{}')
    Reject { AssertRuntime $runtime }
    [IO.File]::Delete($state)
    [IO.File]::Delete($file)
    Reject { AssertRuntime $runtime }
    [IO.File]::WriteAllText($file, 'inert fixture')
    $link = Join-Path $root 'linked-runtime'
    $null = New-Item -ItemType $(if ($IsWindows) { 'Junction' } else { 'SymbolicLink' }) -Path $link -Target $runtime
    try { Reject { AssertRuntime $link } } finally { (Get-Item -LiteralPath $link -Force).Delete() }

    $localAppData = Join-Path $root 'owned local'
    $appData = Join-Path $root 'owned roaming'
    $profile = Join-Path $root 'owned profile'
    $cacheBase = Join-Path $root 'owned cache'
    $foreignQt = Join-Path $root 'foreign Qt'
    $foreignPlatforms = Join-Path $foreignQt 'platforms'
    $env:SOURCE_READ_TOKEN = 'inert-test-secret'
    $env:QT_PLUGIN_PATH = 'inherited-qt-must-be-cleared'
    $arguments = @('--cli', '--data-root', 'C:\用户 data\ends with slash\', '--', 'literal "quoted" text')
    $psi = StartInfo $file $arguments $true
    Check (-not $psi.UseShellExecute -and $psi.RedirectStandardOutput -and $psi.RedirectStandardError) 'Smoke process streams are not private.'
    Check ($psi.StandardOutputEncoding.CodePage -eq 65001 -and $psi.StandardErrorEncoding.CodePage -eq 65001) 'Child JSON or diagnostic bytes would use the Windows OEM code page.'
    Check ($psi.Environment['PATH'] -ceq "$env:SystemRoot\System32;$env:SystemRoot") 'Qt or a compiler remained on the child PATH.'
    Check (-not $psi.Environment.ContainsKey('SOURCE_READ_TOKEN')) 'A private source token reached the child.'
    Check ($psi.Environment['LOCALAPPDATA'] -ceq $localAppData -and $psi.Environment['APPDATA'] -ceq $appData -and $psi.Environment['USERPROFILE'] -ceq $profile) 'The child profile was not isolated.'
    Check ($psi.Environment['MCL_BUNDLE_CACHE_ROOT'] -ceq $cacheBase -and $psi.Environment['MCL_BUNDLE_DIAGNOSTICS'] -ceq '1') 'The owned cache or child PID diagnostics are missing.'
    Check ($psi.Environment['QT_PLUGIN_PATH'] -ceq $foreignQt -and $psi.Environment['QT_QPA_PLATFORM_PLUGIN_PATH'] -ceq $foreignPlatforms) 'The foreign Qt environment was not exercised.'
    Check ($psi.ArgumentList.Count -eq $arguments.Count) 'Argument boundaries were not preserved.'
    for ($i = 0; $i -lt $arguments.Count; ++$i) { Check ($psi.ArgumentList[$i] -ceq $arguments[$i]) 'A Unicode, space, quote or trailing slash argument changed.' }
    $quiet = StartInfo $file @('--cli')
    Check (-not $quiet.Environment.ContainsKey('MCL_BUNDLE_DIAGNOSTICS')) 'CLI stdout would include diagnostics by default.'

    # Reproduce the byte boundary, using a real subprocess that writes raw UTF-8
    # rather than inheriting the host console encoding. This covers the actual
    # InvokeCli ReadToEndAsync/GetResult path used for Windows native JSON.
    $logs = Join-Path $root 'private fixture logs'
    $null = [IO.Directory]::CreateDirectory($logs)
    $mock = @'
$json = '{"dataRoot":"D:/a/_temp/单文件 独立运行 smoke/独立 data with spaces","instances":[],"status":"NATIVE_STATE_READ"}'
$out = [Console]::OpenStandardOutput()
$bytes = [Text.Encoding]::UTF8.GetBytes($json)
$out.Write($bytes, 0, $bytes.Length)
$out.Flush()
$err = [Console]::OpenStandardError()
$bytes = [Text.Encoding]::UTF8.GetBytes('中文错误诊断')
$err.Write($bytes, 0, $bytes.Length)
$err.Flush()
'@
    $command = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($mock))
    $pwsh = Join-Path $PSHOME $(if ($IsWindows) { 'pwsh.exe' } else { 'pwsh' })
    $json = (InvokeCli $pwsh @('-NoLogo', '-NoProfile', '-NonInteractive', '-EncodedCommand', $command) 'utf8-native-cli-fixture') | ConvertFrom-Json
    Check ($json.dataRoot -ceq 'D:/a/_temp/单文件 独立运行 smoke/独立 data with spaces' -and $json.status -ceq 'NATIVE_STATE_READ') 'Raw UTF-8 CLI JSON changed its Chinese data root.'
    Check ([IO.File]::ReadAllText((Join-Path $logs 'utf8-native-cli-fixture.stderr.log')) -ceq '中文错误诊断') 'Raw UTF-8 stderr diagnostics changed.'
} finally {
    $env:SOURCE_READ_TOKEN = $savedToken
    $env:QT_PLUGIN_PATH = $savedQt
    [IO.Directory]::Delete($root, $true)
}
Write-Output "Independent bundle smoke assertions verified: $script:checks checks."
