#Requires -Version 7.2
# Diagnostic-only entry point. The workflow redirects ALL output privately and
# uploads only encrypted logs; this script never packages or publishes binaries.
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$SourceRoot,
    [string]$PrivateLogDirectory = ''
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
if (-not $IsWindows -or $env:GITHUB_ACTIONS -cne 'true' -or -not $env:RUNNER_TEMP) {
    throw 'Use only on an explicitly invoked Windows GitHub Actions runner.'
}

function Ordinary([string]$Path, [bool]$Directory = $false) {
    $full = [IO.Path]::GetFullPath($Path)
    $item = Get-Item -LiteralPath $full -Force
    if ($item.PSIsContainer -ne $Directory) { throw "Unexpected input type: $full" }
    for ($node = $full; $node; $node = Split-Path -Parent $node) {
        if ((Get-Item -LiteralPath $node -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) {
            throw "Linked input refused: $node"
        }
    }
    return $full
}

$source = Ordinary $SourceRoot $true
$native = Ordinary (Join-Path $source 'qt-native') $true
if (-not $PrivateLogDirectory) { $PrivateLogDirectory = Join-Path $env:RUNNER_TEMP 'private-native-ui-logs' }
$logs = [IO.Path]::GetFullPath($PrivateLogDirectory)
if ($logs -ieq $source -or $logs.StartsWith($source + '\', [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Private diagnostics must be outside the source checkout.'
}
$null = [IO.Directory]::CreateDirectory($logs)
$logs = Ordinary $logs $true
$build = Join-Path $native 'build-native-ui-diagnostic'
if (Test-Path -LiteralPath $build) { throw 'Diagnostic compilation requires a fresh build directory.' }
$steps = [Collections.Generic.List[object]]::new()
$failure = $null
$sourceSha = $null
$fontFiles = @()

# Keep subprocess stdout/stderr as direct private files. Exceptions retain their
# labels, exit codes and original stack trace in the outer private workflow log.
function RunLogged([string]$Program, [string[]]$Arguments, [string]$Label,
                   [int]$Seconds, [hashtable]$Environment = @{}, [switch]$AllowFailure) {
    $psi = [Diagnostics.ProcessStartInfo]::new()
    $psi.FileName = Ordinary $Program
    $psi.WorkingDirectory = $native
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    foreach ($argument in $Arguments) { $psi.ArgumentList.Add($argument) }
    foreach ($name in @('SOURCE_REPOSITORY', 'SOURCE_READ_TOKEN', 'GH_TOKEN', 'GITHUB_TOKEN',
            'QTDIR', 'QT_HOST_PATH', 'QT_PLUGIN_PATH', 'QT_QPA_PLATFORM_PLUGIN_PATH', 'QT_QPA_FONTDIR',
            'QML_IMPORT_PATH', 'QML2_IMPORT_PATH', 'CMAKE_PREFIX_PATH', 'CC', 'CXX',
            'CFLAGS', 'CXXFLAGS', 'LDFLAGS', 'MCL_DATA_ROOT', 'MCL_NATIVE_DATA_ROOT',
            'MCL_PORTABLE_DIR', 'MCL_TEST_JAVA', 'MCL_AUTHLIB_REAL_FETCH', 'MCL_REGION_REAL_PROBE')) {
        $null = $psi.Environment.Remove($name)
    }
    foreach ($name in $Environment.Keys) { $psi.Environment[$name] = [string]$Environment[$name] }
    $stdout = [IO.File]::Open((Join-Path $logs ($Label + '.stdout.log')), [IO.FileMode]::CreateNew)
    $stderr = [IO.File]::Open((Join-Path $logs ($Label + '.stderr.log')), [IO.FileMode]::CreateNew)
    $process = [Diagnostics.Process]::new()
    $process.StartInfo = $psi
    $timer = [Diagnostics.Stopwatch]::StartNew()
    $exitCode = -1
    $timedOut = $false
    try {
        if (-not $process.Start()) { throw "Could not start diagnostic stage: $Label" }
        $outTask = $process.StandardOutput.BaseStream.CopyToAsync($stdout)
        $errTask = $process.StandardError.BaseStream.CopyToAsync($stderr)
        if (-not $process.WaitForExit($Seconds * 1000)) {
            $timedOut = $true
            $process.Kill($true)
            $process.WaitForExit()
        }
        $null = $outTask.GetAwaiter().GetResult()
        $null = $errTask.GetAwaiter().GetResult()
        $exitCode = $process.ExitCode
        if ($timedOut -and -not $AllowFailure) { throw "Diagnostic stage $Label exceeded its $Seconds second bound." }
        if ($exitCode -ne 0 -and -not $AllowFailure) { throw "Diagnostic stage $Label exited with code $exitCode." }
    } finally {
        $timer.Stop()
        $stdout.Dispose()
        $stderr.Dispose()
        $process.Dispose()
        $steps.Add([ordered]@{ name = $Label; exitCode = $exitCode; timedOut = $timedOut; elapsedMs = $timer.ElapsedMilliseconds })
    }
}

try {
    $sourceSha = (& git -C $source rev-parse --verify HEAD)
    if ($LASTEXITCODE -ne 0 -or $sourceSha -cnotmatch '\A[0-9a-f]{40}\z') { throw 'Cannot verify the diagnostic source revision.' }
    $bootstrap = Ordinary (Join-Path $native 'scripts/ci-bootstrap.ps1')
    $pwsh = Ordinary (Join-Path $PSHOME 'pwsh.exe')
    RunLogged $pwsh @('-NoLogo', '-NoProfile', '-NonInteractive', '-File', $bootstrap, '-Run') 'bootstrap' 900

    $qt = Ordinary (Join-Path $native '.tools/Qt/6.8.3/mingw_64') $true
    $mingw = Ordinary (Join-Path $native '.tools/Qt/Tools/mingw1310_64/bin') $true
    $cmake = Ordinary (Join-Path $native '.tools/Qt/Tools/CMake_64/bin/cmake.exe')
    $ninja = Ordinary (Join-Path $native '.tools/Qt/Tools/Ninja/ninja.exe')
    $scratch = Join-Path $build 'ci-tmp'
    $screenshots = Join-Path $logs 'screenshots-baseline'
    $null = [IO.Directory]::CreateDirectory($scratch)
    $null = [IO.Directory]::CreateDirectory($screenshots)
    $environment = @{
        PATH = "$mingw;$(Join-Path $qt 'bin');$env:SystemRoot\System32;$env:SystemRoot"
        TEMP = $scratch; TMP = $scratch; TMPDIR = $scratch
        QT_QPA_PLATFORM = 'offscreen'; QT_QUICK_BACKEND = 'software'; QML_DISABLE_DISK_CACHE = '1'
        QT_FORCE_STDERR_LOGGING = '1'
        QT_PLUGIN_PATH = (Join-Path $qt 'plugins'); QML_IMPORT_PATH = (Join-Path $qt 'qml')
        MCL_NATIVE_UI_EVIDENCE = $screenshots
    }
    $configure = @('-S', $native, '-B', $build, '-G', 'Ninja',
        "-DCMAKE_MAKE_PROGRAM=$ninja", "-DCMAKE_C_COMPILER=$(Join-Path $mingw 'gcc.exe')",
        "-DCMAKE_CXX_COMPILER=$(Join-Path $mingw 'g++.exe')", "-DCMAKE_PREFIX_PATH=$qt",
        '-DCMAKE_BUILD_TYPE=Release', '-DMCL_STATIC_RELEASE=OFF', '-DMCL_CORE_ONLY=OFF',
        '-DBUILD_TESTING=ON', '-DCMAKE_FIND_USE_PACKAGE_REGISTRY=OFF',
        '-DCMAKE_FIND_USE_SYSTEM_PACKAGE_REGISTRY=OFF')
    RunLogged $cmake $configure 'configure-native-ui' 300 $environment
    # No other application or test executable is built by this diagnostic.
    RunLogged $cmake @('--build', $build, '--target', 'native_ui_tests', '--parallel', '2') 'build-native-ui' 1500 $environment
    $test = Ordinary (Join-Path $build 'native_ui_tests.exe')
    $textLog = Join-Path $logs 'native-ui.txt'
    RunLogged $test @('-o', ($textLog + ',txt'), '-o', '-,txt') 'native-ui-baseline' 600 $environment -AllowFailure

    # Qt's Windows offscreen integration uses FreeType rather than the native
    # Windows font database. Test the system-font-directory hypothesis without
    # changing application code or distributing any operating-system font.
    $fontRoot = Join-Path $env:WINDIR 'Fonts'
    $fontFiles = @(foreach ($name in @('msyh.ttc', 'msyhbd.ttc', 'msyhl.ttc')) {
        $path = Join-Path $fontRoot $name
        $exists = Test-Path -LiteralPath $path -PathType Leaf
        [ordered]@{ name = $name; exists = $exists; size = $(if ($exists) { (Get-Item -LiteralPath $path).Length } else { 0 }) }
    })
    $fontEnvironment = $environment.Clone()
    $fontEnvironment['QT_QPA_FONTDIR'] = $fontRoot
    $fontScreenshots = Join-Path $logs 'screenshots-system-fonts'
    $null = [IO.Directory]::CreateDirectory($fontScreenshots)
    $fontEnvironment['MCL_NATIVE_UI_EVIDENCE'] = $fontScreenshots
    $fontLog = Join-Path $logs 'native-ui-system-fonts.txt'
    RunLogged $test @('sharedChromeAndControlsStayReadableAcrossThemesAndSizes',
        'commonControlsPreserveActivationAndFitSmallWindows', '-o', ($fontLog + ',txt'), '-o', '-,txt') `
        'native-ui-system-fonts' 600 $fontEnvironment -AllowFailure
    foreach ($requiredLog in @($textLog, $fontLog)) {
        if (-not (Test-Path -LiteralPath $requiredLog -PathType Leaf) -or (Get-Item -LiteralPath $requiredLog).Length -eq 0) {
            throw "QtTest did not produce its required direct text log: $requiredLog"
        }
    }
    if (@($steps | Where-Object { $_.name -like 'native-ui-*' -and ($_.exitCode -ne 0 -or $_.timedOut) }).Count) {
        throw 'Both diagnostic profiles ran; at least one reported a failure. See their separate private QtTest logs.'
    }
} catch {
    $failure = ($_ | Format-List * -Force | Out-String)
    [IO.File]::WriteAllText((Join-Path $logs 'failure.txt'), $failure, [Text.UTF8Encoding]::new($false))
    throw
} finally {
    [ordered]@{
        schema = 1; sourceCommit = $sourceSha; workflowCommit = $env:GITHUB_SHA
        scope = 'Diagnostic native_ui only; not a release gate or a binary distribution'
        platform = 'Windows'; qpa = 'offscreen'; qtQuickBackend = 'software'
        nativeUiTimeoutSeconds = 600; success = ($null -eq $failure); systemFontFiles = $fontFiles
        profiles = @('Full suite without QT_QPA_FONTDIR', 'Two targeted cases with QT_QPA_FONTDIR pointing to Windows Fonts')
        steps = $steps.ToArray()
    } | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath (Join-Path $logs 'diagnostic-result.json') -Encoding utf8
}
