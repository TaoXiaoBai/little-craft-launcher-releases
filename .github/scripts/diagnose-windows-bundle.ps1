#Requires -Version 7.2
# Diagnostic only: reuse one immutable public runtime; compile only the new
# private native bootstrap. No test proof or distributable is produced/uploaded.
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$SourceRoot,
    [Parameter(Mandatory)][ValidatePattern('\A[0-9a-f]{40}\z')][string]$SourceRef
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
if (-not $IsWindows -or $env:GITHUB_ACTIONS -cne 'true' -or -not $env:RUNNER_TEMP) {
    throw 'Use an explicitly invoked Windows diagnostic runner.'
}

# Load the exact guarded subprocess/file helpers used by normal packaging,
# without executing its bootstrap, full application build or publication path.
$tokens = $null; $parseErrors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile((Join-Path $PSScriptRoot 'package-windows.ps1'), [ref]$tokens, [ref]$parseErrors)
$required = @('Ordinary', 'NewDirectory', 'Hash', 'CMakePath', 'NewText', 'CopyVerified', 'RunProgram')
$functions = @($ast.FindAll({ param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -cin $required }, $true))
if ($parseErrors.Count -or $functions.Count -ne $required.Count) { throw 'The reviewed packaging helpers are unavailable.' }
foreach ($function in $functions) { Invoke-Expression $function.Extent.Text }
$source = Ordinary $SourceRoot $true
$native = Ordinary (Join-Path $source 'qt-native') $true
$sourceCommit = (& git -C $source rev-parse --verify HEAD)
if ($LASTEXITCODE -ne 0 -or $sourceCommit -cne $SourceRef) { throw 'The diagnostic checkout differs from its exact requested source revision.' }
$logs = NewDirectory (Join-Path $env:RUNNER_TEMP 'private-windows-bundle-logs/stages')
$stage = NewDirectory (Join-Path $env:RUNNER_TEMP ('windows-bundle-diagnostic-' + [guid]::NewGuid().ToString('N')))
$python = Ordinary (Get-Command python -CommandType Application -ErrorAction Stop).Source
$pwsh = Ordinary (Join-Path $PSHOME 'pwsh.exe')

function Get-DiagnosticToolAssets($Lock) {
    $pins = @{
        '7zr.exe' = @(593408, 'd2c0045523cf053a6b43f9315e9672fc2535f06aeadd4ffa53c729cd8b2b6dfe', '')
        'mingw.7z' = @(112089140, 'a7b502294a903b64fccd4a41ba39f48d3f6c9a5ece167f0cfaeac920d15b344f', '.')
        'cmake.7z' = @(27883601, '257ff96c0e9fa6765c8c247132cf3a2a02b2a5ba360d32097b3605674196cabf', 'Tools/CMake_64')
        'ninja.7z' = @(233312, 'f85026ed07850918109f80d82039f1ae3af8e74d26d806bbeb6501c994102a04', 'Tools/Ninja')
    }
    if ($Lock.schemaVersion -ne 1 -or $Lock.qt -cne '6.8.3' -or $Lock.mingw -cne '13.1.0' -or
        $Lock.cmake -cne '3.30.5' -or $Lock.ninja -cne '1.12.1') { throw 'Unsupported diagnostic toolchain lock.' }
    $selected = @($Lock.assets | Where-Object { $_.file -cin $pins.Keys })
    if ($selected.Count -ne $pins.Count -or @($selected.file | Sort-Object -Unique).Count -ne $pins.Count) { throw 'The native diagnostic toolchain inventory is incomplete.' }
    foreach ($asset in $selected) {
        $pin = $pins[$asset.file]
        $uri = [uri]$asset.url
        if ($asset.bytes -ne $pin[0] -or $asset.sha256 -cne $pin[1] -or [string]$asset.destination -cne $pin[2] -or
            $uri.Scheme -cne 'https' -or $uri.UserInfo -or $uri.Host -notin @('download.qt.io', 'github.com')) {
            throw 'A native diagnostic toolchain input differs from its reviewed pin.'
        }
    }
    return $selected
}
function Download-Pinned([uri]$Uri, [string]$Destination, [long]$MaximumBytes, [string]$ExpectedHash) {
    if ($Uri.Scheme -cne 'https' -or $Uri.UserInfo -or $Uri.Host -notin @('download.qt.io', 'github.com')) { throw 'Unapproved diagnostic input origin.' }
    $handler = [Net.Http.HttpClientHandler]::new()
    $handler.AllowAutoRedirect = $true; $handler.MaxAutomaticRedirections = 5
    $client = [Net.Http.HttpClient]::new($handler)
    $client.Timeout = [TimeSpan]::FromMinutes(5)
    $response = $null; $inputStream = $null; $outputStream = $null
    $deadline = [Threading.CancellationTokenSource]::new([TimeSpan]::FromMinutes(5))
    try {
        $response = $client.GetAsync($Uri, [Net.Http.HttpCompletionOption]::ResponseHeadersRead).GetAwaiter().GetResult()
        $null = $response.EnsureSuccessStatusCode()
        if ($response.RequestMessage.RequestUri.Scheme -cne 'https') { throw 'Insecure diagnostic download redirect.' }
        $inputStream = $response.Content.ReadAsStreamAsync().GetAwaiter().GetResult()
        $outputStream = [IO.File]::Open($Destination, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
        $buffer = [byte[]]::new(256KB); $total = 0L
        while (($read = $inputStream.ReadAsync($buffer, 0, $buffer.Length, $deadline.Token).GetAwaiter().GetResult()) -gt 0) {
            $total += $read
            if ($total -gt $MaximumBytes) { throw 'A diagnostic download exceeded its size bound.' }
            $outputStream.Write($buffer, 0, $read)
        }
        $outputStream.Flush($true); $outputStream.Dispose(); $outputStream = $null
        if (-not $total -or (Hash $Destination) -cne $ExpectedHash) { throw 'A diagnostic input failed SHA-256 verification.' }
    } finally {
        if ($null -ne $outputStream) { $outputStream.Dispose() }
        if ($null -ne $inputStream) { $inputStream.Dispose() }
        if ($null -ne $response) { $response.Dispose() }
        $deadline.Dispose(); $client.Dispose(); $handler.Dispose()
    }
}

$lock = [IO.File]::ReadAllText((Ordinary (Join-Path $native 'ci/toolchain-lock.json'))) | ConvertFrom-Json
$assets = @(Get-DiagnosticToolAssets $lock)
$downloads = NewDirectory (Join-Path $stage 'public-toolchain-inputs')
$restored = Join-Path $env:RUNNER_TEMP 'mcl-ci-archives'
foreach ($asset in $assets) {
    $destination = Join-Path $downloads $asset.file
    $cached = Join-Path $restored $asset.file
    if (Test-Path -LiteralPath $cached) {
        if ((Get-Item -LiteralPath (Ordinary $cached)).Length -ne $asset.bytes -or (Hash $cached) -cne $asset.sha256) { throw 'A restored public toolchain archive failed its pin.' }
        CopyVerified $cached $destination
    } else { Download-Pinned ([uri]$asset.url) $destination $asset.bytes $asset.sha256 }
    if ((Get-Item -LiteralPath (Ordinary $destination)).Length -ne $asset.bytes) { throw 'Unexpected public toolchain archive size.' }
}
$tools = NewDirectory (Join-Path $stage 'native-tools')
$extractor = Ordinary (Join-Path $downloads '7zr.exe')
foreach ($asset in $assets | Where-Object file -cne '7zr.exe') {
    $destination = Join-Path $tools $asset.destination
    $null = NewDirectory $destination
    RunProgram $extractor @('x', '-y', '-bd', ('-o' + $destination), (Join-Path $downloads $asset.file)) ('extract-' + $asset.file) 3
}
$mingw = Ordinary (Join-Path $tools 'Tools/mingw1310_64/bin') $true
$cmake = Ordinary (Join-Path $tools 'Tools/CMake_64/bin/cmake.exe')
$ninja = Ordinary (Join-Path $tools 'Tools/Ninja/ninja.exe')
$toolPath = $mingw + ";$env:SystemRoot\System32;$env:SystemRoot"

$tag = 'v1.0.0-pre.9'
$inner = Join-Path $stage 'pre9-immutable-runtime.zip'
Download-Pinned ([uri]'https://github.com/TaoXiaoBai/little-craft-launcher-releases/releases/download/v1.0.0-pre.9/LittleCraftLauncher-v1.0.0-pre.9-windows-x64.zip') $inner 512MB 'de1bcbea0f08c840db9faa91acadc3bb37f2c61aa7981fbf9c1e48e8bb590194'
$format = Ordinary (Join-Path $PSScriptRoot 'windows_bundle_format.py')
$index = Join-Path $stage 'bundle.index'
RunProgram $python @($format, 'create-index', '--payload', $inner, '--tag', $tag, '--output', $index) 'bundle-index' 3
$payload = Join-Path $stage 'validated-pre9-payload'
[IO.Compression.ZipFile]::ExtractToDirectory($inner, $payload)
$build = Join-Path $stage 'bootstrap-build'
RunProgram $cmake @('-S', (Ordinary (Join-Path $native 'windows-bundle') $true), '-B', $build, '-G', 'Ninja',
    "-DCMAKE_MAKE_PROGRAM=$(CMakePath $ninja)", "-DCMAKE_C_COMPILER=$(CMakePath (Ordinary (Join-Path $mingw 'gcc.exe')))",
    "-DCMAKE_CXX_COMPILER=$(CMakePath (Ordinary (Join-Path $mingw 'g++.exe')))", "-DCMAKE_RC_COMPILER=$(CMakePath (Ordinary (Join-Path $mingw 'windres.exe')))",
    '-DCMAKE_BUILD_TYPE=Release', '-DCMAKE_FIND_USE_PACKAGE_REGISTRY=OFF', '-DCMAKE_FIND_USE_SYSTEM_PACKAGE_REGISTRY=OFF',
    "-DMCL_BUNDLE_PAYLOAD=$(CMakePath $inner)", "-DMCL_BUNDLE_INDEX=$(CMakePath $index)", "-DMCL_BUNDLE_RELEASE_TAG=$tag") 'bundle-configure' 3 @{ PATH = $toolPath }
RunProgram $cmake @('--build', $build, '--parallel', '2') 'bundle-build' 5 @{ PATH = $toolPath }
RunProgram (Ordinary (Join-Path $tools 'Tools/CMake_64/bin/ctest.exe')) @('--test-dir', $build, '--output-on-failure', '-C', 'Release') 'bundle-native-tests' 2 @{ PATH = $toolPath }
$outer = Ordinary (Join-Path $build 'mclauncher-qt.exe')
RunProgram $python @($format, 'inspect', '--exe', $outer, '--tag', $tag) 'bundle-inspect' 3
$single = NewDirectory (Join-Path $stage 'single-executable')
CopyVerified $outer (Join-Path $single 'mclauncher-qt.exe')
$zip = Join-Path $stage 'diagnostic-single-executable.zip'
[IO.Compression.ZipFile]::CreateFromDirectory($single, $zip, [IO.Compression.CompressionLevel]::Optimal, $false)
RunProgram $python @($format, 'inspect-zip', '--archive', $zip, '--tag', $tag) 'bundle-inspect-zip' 3
$extracted = Join-Path $stage 'diagnostic-zip-extraction'
[IO.Compression.ZipFile]::ExtractToDirectory($zip, $extracted)
RunProgram $pwsh @('-NoLogo', '-NoProfile', '-NonInteractive', '-File', (Ordinary (Join-Path $PSScriptRoot 'smoke-windows-bundle.ps1')),
    '-Executable', (Ordinary (Join-Path $extracted 'mclauncher-qt.exe')), '-PayloadDirectory', $payload,
    '-PayloadSha256', (Hash $inner), '-StageDirectory', $stage, '-LogDirectory', $logs) 'bundle-independent-smoke' 12
Write-Output 'Standalone Windows bootstrap diagnostic passed against the immutable public pre.9 runtime. This is not release test coverage and produces no published package.'
