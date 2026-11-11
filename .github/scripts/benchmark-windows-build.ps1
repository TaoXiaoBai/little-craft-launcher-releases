#Requires -Version 7.2
# Build-only diagnostic. The workflow redirects every output stream privately.
[CmdletBinding()]
param([Parameter(Mandatory)][string]$SourceRoot)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Get-BenchmarkOrdinaryPath([string]$Path, [bool]$Directory = $false) {
    $full = [IO.Path]::GetFullPath($Path)
    $attributes = [IO.File]::GetAttributes($full)
    if ((($attributes -band [IO.FileAttributes]::Directory) -ne 0) -ne $Directory) { throw 'Unexpected benchmark input type.' }
    for ($node = $full; $node; $node = Split-Path -Parent $node) {
        if ([IO.File]::GetAttributes($node) -band [IO.FileAttributes]::ReparsePoint) { throw 'Linked benchmark inputs are refused.' }
    }
    return $full
}

function New-BenchmarkBuildDirectory([string]$Path, [string]$Checkout) {
    $full = [IO.Path]::GetFullPath($Path)
    $relative = [IO.Path]::GetRelativePath([IO.Path]::GetFullPath($Checkout), $full)
    if (-not ($relative -eq '..' -or $relative.StartsWith('..' + [IO.Path]::DirectorySeparatorChar) -or [IO.Path]::IsPathRooted($relative))) {
        throw 'Benchmark builds must be outside the source checkout.'
    }
    $null = Get-BenchmarkOrdinaryPath (Split-Path -Parent $full) $true
    if (Test-Path -LiteralPath $full) { throw 'Each benchmark requires a nonexistent build directory.' }
    $null = [IO.Directory]::CreateDirectory($full)
    return (Get-BenchmarkOrdinaryPath $full $true)
}

function Get-BenchmarkConfigureArguments([string]$Native, [string]$Build, [string]$Qt,
        [string]$MinGW, [string]$Ninja, [ValidateSet('ON', 'OFF')][string]$OriginDepends) {
    # Match ci-build-test.ps1's dynamic configuration. This is the sole variable.
    @('-S', $Native, '-B', $Build, '-G', 'Ninja', "-DCMAKE_MAKE_PROGRAM=$Ninja",
      "-DCMAKE_C_COMPILER=$(Join-Path $MinGW 'gcc.exe')", "-DCMAKE_CXX_COMPILER=$(Join-Path $MinGW 'g++.exe')",
      "-DCMAKE_PREFIX_PATH=$Qt", '-DCMAKE_BUILD_TYPE=Release', '-DMCL_STATIC_RELEASE=OFF',
      '-DMCL_CORE_ONLY=OFF', '-DBUILD_TESTING=ON', '-DCMAKE_NINJA_FORCE_RESPONSE_FILE=ON',
      "-DCMAKE_AUTOGEN_ORIGIN_DEPENDS=$OriginDepends", '-DCMAKE_FIND_USE_PACKAGE_REGISTRY=OFF',
      '-DCMAKE_FIND_USE_SYSTEM_PACKAGE_REGISTRY=OFF')
}

function New-BenchmarkProcessInfo([string]$Program, [string[]]$Arguments, [string]$WorkingDirectory,
        [hashtable]$Environment = @{}) {
    $psi = [Diagnostics.ProcessStartInfo]::new()
    $psi.FileName = Get-BenchmarkOrdinaryPath $Program
    $psi.WorkingDirectory = $WorkingDirectory
    $psi.UseShellExecute = $false; $psi.CreateNoWindow = $true
    $psi.RedirectStandardOutput = $true; $psi.RedirectStandardError = $true
    foreach ($argument in $Arguments) { $psi.ArgumentList.Add($argument) }
    # Same compiler/QML sanitization as the dynamic CI runner, plus credentials.
    foreach ($key in @('SOURCE_REPOSITORY', 'SOURCE_READ_TOKEN', 'GH_TOKEN', 'GITHUB_TOKEN',
        'QTDIR', 'QT_HOST_PATH', 'QT_PLUGIN_PATH', 'QT_QPA_PLATFORM_PLUGIN_PATH', 'QML_IMPORT_PATH',
        'QML2_IMPORT_PATH', 'QML_DISABLE_DISK_CACHE', 'QML_FORCE_DISK_CACHE', 'QML_DISK_CACHE',
        'QML_DISK_CACHE_PATH', 'QV4_FORCE_INTERPRETER', 'CMAKE_PREFIX_PATH', 'CC', 'CXX', 'CFLAGS',
        'CXXFLAGS', 'LDFLAGS', 'NODE_OPTIONS', 'MCL_DATA_ROOT', 'MCL_NATIVE_DATA_ROOT', 'MCL_PORTABLE_DIR',
        'MCL_TEST_JAVA', 'MCL_AUTHLIB_REAL_FETCH', 'MCL_REGION_REAL_PROBE', 'MCL_MS_CLIENT_ID',
        'MCL_MS_REDIRECT_URI', 'MCL_NATIVE_UI_EVIDENCE', 'MCL_EXTERNAL_AUTH_UI_EVIDENCE',
        'MCL_WINMD_UI_EVIDENCE', 'MCL_MOD_FAVORITES_UI_EVIDENCE', 'MCL_UI_REGRESSION_EVIDENCE', 'MCL_CRASH_UI_EVIDENCE')) {
        $null = $psi.Environment.Remove($key)
    }
    foreach ($key in $Environment.Keys) { $psi.Environment[$key] = [string]$Environment[$key] }
    return $psi
}

function Invoke-BenchmarkTool([string]$Program, [string[]]$Arguments, [string]$Label,
        [string]$WorkingDirectory, [string]$Logs, [hashtable]$Environment = @{}, [int]$Minutes = 20) {
    $psi = New-BenchmarkProcessInfo $Program $Arguments $WorkingDirectory $Environment
    $stdout = [IO.File]::Open((Join-Path $Logs ($Label + '.stdout.log')), [IO.FileMode]::CreateNew)
    $stderr = [IO.File]::Open((Join-Path $Logs ($Label + '.stderr.log')), [IO.FileMode]::CreateNew)
    $process = [Diagnostics.Process]::new(); $process.StartInfo = $psi
    $timer = [Diagnostics.Stopwatch]::StartNew(); $code = -1; $timedOut = $false
    try {
        if (-not $process.Start()) { throw 'Could not start benchmark subprocess.' }
        $a = $process.StandardOutput.BaseStream.CopyToAsync($stdout)
        $b = $process.StandardError.BaseStream.CopyToAsync($stderr)
        if (-not $process.WaitForExit($Minutes * 60000)) { $timedOut = $true; $process.Kill($true); $process.WaitForExit() }
        $null = $a.GetAwaiter().GetResult(); $null = $b.GetAwaiter().GetResult(); $code = $process.ExitCode
    } finally { $timer.Stop(); $stdout.Dispose(); $stderr.Dispose(); $process.Dispose() }
    if ($timedOut -or $code -ne 0) { throw "Benchmark subprocess failed: $Label" }
    return [ordered]@{ seconds = $timer.Elapsed.TotalSeconds; exitCode = $code }
}

function Get-BenchmarkDigest($Value) {
    $bytes = [Text.UTF8Encoding]::new($false).GetBytes(($Value | ConvertTo-Json -Depth 10 -Compress))
    return [Convert]::ToHexString([Security.Cryptography.SHA256]::HashData($bytes)).ToLowerInvariant()
}

function Get-BenchmarkToolIdentity([string[]]$Paths) {
    foreach ($path in $Paths) {
        $full = Get-BenchmarkOrdinaryPath $path
        [ordered]@{ path = $full; sha256 = (Get-FileHash -LiteralPath $full -Algorithm SHA256).Hash.ToLowerInvariant() }
    }
}

function Copy-BenchmarkBuildMetadata([string]$Build, [string]$Destination) {
    $null = [IO.Directory]::CreateDirectory($Destination)
    $relativeFiles = @('.ninja_log', 'build.ninja', 'CMakeFiles/rules.ninja', 'CMakeCache.txt',
        'generated/auth_build_config.h', 'generated/application_build_config.h')
    $cmakeFiles = Join-Path $Build 'CMakeFiles'
    if (Test-Path -LiteralPath $cmakeFiles) {
        foreach ($directory in Get-ChildItem -LiteralPath $cmakeFiles -Directory -Filter '*_autogen.dir') {
            $relativeFiles += 'CMakeFiles/' + $directory.Name + '/AutogenInfo.json'
        }
    }
    foreach ($relative in $relativeFiles) {
        $from = Join-Path $Build $relative
        if (-not (Test-Path -LiteralPath $from)) { continue }
        $from = Get-BenchmarkOrdinaryPath $from
        if ((Get-Item -LiteralPath $from -Force).Length -gt 16MB) { throw 'Build metadata exceeds its diagnostic bound.' }
        $to = Join-Path $Destination $relative
        $null = [IO.Directory]::CreateDirectory((Split-Path -Parent $to))
        [IO.File]::Copy($from, $to, $false)
    }
}

function Get-BenchmarkPublicSummary($State) {
    # Construct an allowlisted schema; never serialize private exception bodies,
    # build paths, source inventories, command lines or hardware model strings.
    if (($State.buildParallel -isnot [int] -and $State.buildParallel -isnot [long]) -or
        $State.buildParallel -lt 1 -or $State.buildParallel -gt 4) { throw 'Invalid benchmark parallelism.' }
    $rounds = @(foreach ($round in $State.rounds) {
        if ($round.originDepends -cnotin @('ON', 'OFF') -or $round.index -notin @(1, 2, 3)) { throw 'Invalid benchmark round.' }
        $configureSeconds = [double]$round.configureSeconds; $buildSeconds = [double]$round.buildSeconds
        foreach ($seconds in @($configureSeconds, $buildSeconds)) {
            if ([double]::IsNaN($seconds) -or [double]::IsInfinity($seconds) -or $seconds -lt 0) { throw 'Invalid benchmark duration.' }
        }
        [ordered]@{ index = [int]$round.index; originDepends = $round.originDepends
            configureSeconds = [Math]::Round($configureSeconds, 3); buildSeconds = [Math]::Round($buildSeconds, 3) }
    })
    if ($rounds.Count -gt 3 -or ($State.success -and ($rounds.Count -ne 3 -or
        ($rounds.originDepends -join ',') -cne 'ON,OFF,ON' -or ($rounds.index -join ',') -cne '1,2,3'))) {
        throw 'A successful comparison requires all three ordered ON/OFF/ON builds.'
    }
    foreach ($key in @('sourceCommit', 'workflowCommit')) {
        if ($State[$key] -and $State[$key] -cnotmatch '\A[0-9a-f]{40}\z') { throw 'Invalid benchmark identity.' }
    }
    foreach ($key in @('sourceSnapshotSha256', 'toolchainSha256')) {
        if ($State[$key] -and $State[$key] -cnotmatch '\A[0-9a-f]{64}\z') { throw 'Invalid benchmark input digest.' }
    }
    [ordered]@{ schemaVersion = 1; kind = 'WINDOWS_AUTOGEN_BUILD_BENCHMARK'
        status = $(if ($State.success) { 'PASS_BUILD_ONLY' } else { 'FAILED_BUILD_ONLY' })
        sourceCommit = $State.sourceCommit; workflowCommit = $State.workflowCommit
        sourceSnapshotSha256 = $State.sourceSnapshotSha256; toolchainSha256 = $State.toolchainSha256
        buildParallel = [int]$State.buildParallel; rounds = $rounds
        releaseEligible = $false; testsExecuted = $false; runtimePackaged = $false; buildCacheRestored = $false
        interpretation = 'Three fresh builds on one VM; first ON is an initial observation. Compare later OFF/ON while allowing filesystem-cache and host drift.' }
}

if (-not $IsWindows -or $env:GITHUB_ACTIONS -cne 'true' -or -not $env:RUNNER_TEMP) { throw 'An explicitly invoked Windows Actions runner is required.' }
$source = Get-BenchmarkOrdinaryPath $SourceRoot $true
$native = Get-BenchmarkOrdinaryPath (Join-Path $source 'qt-native') $true
$logs = Join-Path (Get-BenchmarkOrdinaryPath $env:RUNNER_TEMP $true) 'private-build-benchmark-logs'
$null = [IO.Directory]::CreateDirectory($logs); $logs = Get-BenchmarkOrdinaryPath $logs $true
$runRoot = New-BenchmarkBuildDirectory (Join-Path $env:RUNNER_TEMP ('mcl-build-benchmark-' + [guid]::NewGuid().ToString('N'))) $source
$state = [ordered]@{ success = $false; sourceCommit = $null; workflowCommit = $env:GITHUB_SHA
    sourceSnapshotSha256 = $null; toolchainSha256 = $null; buildParallel = [Math]::Max(1, [Math]::Min(4, [Environment]::ProcessorCount))
    rounds = @(); failure = $null; sourceSnapshot = @(); hardware = $null; toolIdentity = @() }
$failure = $null
try {
    $state.sourceCommit = (& git -C $source rev-parse --verify HEAD)
    if ($LASTEXITCODE -ne 0 -or $state.sourceCommit -cnotmatch '\A[0-9a-f]{40}\z') { throw 'Cannot identify benchmark source.' }
    $pwsh = Get-BenchmarkOrdinaryPath (Join-Path $PSHOME 'pwsh.exe')
    $bootstrap = Get-BenchmarkOrdinaryPath (Join-Path $native 'scripts/ci-bootstrap.ps1')
    $null = Invoke-BenchmarkTool $pwsh @('-NoLogo', '-NoProfile', '-NonInteractive', '-File', $bootstrap, '-Run') 'bootstrap' $native $logs
    $python = Get-BenchmarkOrdinaryPath (Get-Command python -CommandType Application).Source
    $verifier = Get-BenchmarkOrdinaryPath (Join-Path $PSScriptRoot 'verify-cache-inputs.py')
    $null = Invoke-BenchmarkTool $python @($verifier, '--kind', 'windows-archives', '--source-root', $source,
        '--cache-root', (Join-Path $env:RUNNER_TEMP 'mcl-ci-archives')) 'verify-public-archives' $native $logs
    if ($env:GITHUB_OUTPUT) { [IO.File]::AppendAllText($env:GITHUB_OUTPUT, "toolchain_verified=true`n", [Text.UTF8Encoding]::new($false)) }
    . (Get-BenchmarkOrdinaryPath (Join-Path $native 'scripts/static-build-snapshot.ps1'))
    $state.sourceSnapshot = @(Get-StaticSourceSnapshot $native)
    $state.sourceSnapshotSha256 = Get-BenchmarkDigest $state.sourceSnapshot
    $state.toolchainSha256 = (Get-FileHash -LiteralPath (Join-Path $native 'ci/toolchain-lock.json') -Algorithm SHA256).Hash.ToLowerInvariant()
    $cpu = @(Get-CimInstance Win32_Processor)
    $state.hardware = [ordered]@{ cpuModels = @($cpu.Name); logicalProcessors = [Environment]::ProcessorCount
        physicalCoresReported = ($cpu | Measure-Object NumberOfCores -Sum).Sum
        memoryBytes = (Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory; imageVersion = $env:ImageVersion }
    $qt = Get-BenchmarkOrdinaryPath (Join-Path $native '.tools/Qt/6.8.3/mingw_64') $true
    $mingw = Get-BenchmarkOrdinaryPath (Join-Path $native '.tools/Qt/Tools/mingw1310_64/bin') $true
    $cmake = Get-BenchmarkOrdinaryPath (Join-Path $native '.tools/Qt/Tools/CMake_64/bin/cmake.exe')
    $ninja = Get-BenchmarkOrdinaryPath (Join-Path $native '.tools/Qt/Tools/Ninja/ninja.exe')
    $tools = @($cmake, $ninja, (Join-Path $mingw 'gcc.exe'), (Join-Path $mingw 'g++.exe'),
        (Join-Path $qt 'bin/qmake.exe'), (Join-Path $qt 'include/QtCore/qconfig.h'))
    $state.toolIdentity = @(Get-BenchmarkToolIdentity $tools)
    $toolDigest = Get-BenchmarkDigest $state.toolIdentity
    $null = Invoke-BenchmarkTool $cmake @('--version') 'cmake-version' $native $logs
    $null = Invoke-BenchmarkTool $ninja @('--version') 'ninja-version' $native $logs
    $authHash = $null; $index = 0
    foreach ($origin in @('ON', 'OFF', 'ON')) {
        ++$index
        Assert-StaticSourceSnapshot $native $state.sourceSnapshot
        if ((Get-BenchmarkDigest @(Get-BenchmarkToolIdentity $tools)) -cne $toolDigest) { throw 'The compiler or SDK identity changed.' }
        $build = New-BenchmarkBuildDirectory (Join-Path $runRoot ('round-' + $index)) $source
        $scratch = Join-Path $build 'ci-tmp'; $null = [IO.Directory]::CreateDirectory($scratch)
        $environment = @{ PATH = "$mingw;$(Join-Path $qt 'bin');$env:SystemRoot\System32;$env:SystemRoot"
            TEMP = $scratch; TMP = $scratch; TMPDIR = $scratch; QT_QPA_PLATFORM = 'windows'; QT_QUICK_BACKEND = 'software'
            QT_QPA_FONTDIR = (Join-Path $env:SystemRoot 'Fonts'); QT_FORCE_STDERR_LOGGING = '1'; QML_DISABLE_DISK_CACHE = '1'
            QML_IMPORT_PATH = (Join-Path $qt 'qml'); QT_PLUGIN_PATH = (Join-Path $qt 'plugins') }
        try {
            $configure = @(Get-BenchmarkConfigureArguments $native $build $qt $mingw $ninja $origin)
            $configured = Invoke-BenchmarkTool $cmake $configure ('round-' + $index + '-configure') $native $logs $environment 5
            $header = Get-BenchmarkOrdinaryPath (Join-Path $build 'generated/auth_build_config.h')
            if (-not [IO.File]::ReadAllText($header).Contains('a104e744-8786-4c93-89ec-dd128fb58c91')) { throw 'The default public authentication configuration changed.' }
            $actualAuthHash = (Get-FileHash -LiteralPath $header -Algorithm SHA256).Hash.ToLowerInvariant()
            if ($authHash -and $actualAuthHash -cne $authHash) { throw 'Authentication configuration differs between rounds.' }
            $authHash = $actualAuthHash
            $built = Invoke-BenchmarkTool $cmake @('--build', $build, '--parallel', [string]$state.buildParallel) ('round-' + $index + '-build') $native $logs $environment 20
            $state.rounds += [ordered]@{ index = $index; originDepends = $origin; configureSeconds = $configured.seconds
                buildSeconds = $built.seconds; authConfigSha256 = $authHash }
        } finally { Copy-BenchmarkBuildMetadata $build (Join-Path $logs ('round-' + $index + '-metadata')) }
        Assert-StaticSourceSnapshot $native $state.sourceSnapshot
        if ((Get-BenchmarkDigest @(Get-BenchmarkToolIdentity $tools)) -cne $toolDigest) { throw 'The compiler or SDK identity changed.' }
    }
    $state.success = $true
} catch { $failure = $_.Exception.Message; $state.failure = $failure }
finally {
    $state | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath (Join-Path $logs 'private-result.json') -Encoding utf8
    Get-BenchmarkPublicSummary $state | ConvertTo-Json -Depth 6 |
        Set-Content -LiteralPath (Join-Path $env:RUNNER_TEMP 'windows-build-benchmark-summary.json') -Encoding utf8
}
if ($failure) { exit 1 }
