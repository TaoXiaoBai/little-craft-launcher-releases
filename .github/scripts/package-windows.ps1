#Requires -Version 7.2
# Generic private-checkout builder. The caller keeps ALL output in private runner
# logs and publishes only OutputDirectory after this script succeeds.
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$SourceRoot,
    [Parameter(Mandatory)][string]$OutputDirectory,
    [Parameter(Mandatory)][ValidatePattern('\Av[0-9]+\.[0-9]+\.[0-9]+-[A-Za-z0-9][A-Za-z0-9.-]{0,60}\z')][string]$VersionTag
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
if (-not $IsWindows -or $env:GITHUB_ACTIONS -cne 'true' -or -not $env:RUNNER_TEMP) {
    throw 'Use an explicitly invoked Windows GitHub Actions runner.'
}

function Ordinary([string]$Path, [bool]$Directory = $false) {
    $full = [IO.Path]::GetFullPath($Path)
    $item = Get-Item -LiteralPath $full -Force
    if ($item.PSIsContainer -ne $Directory) { throw 'Unexpected input file type.' }
    $node = $full
    while ($node) {
        if ((Get-Item -LiteralPath $node -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) {
            throw 'Linked input paths are not supported.'
        }
        $node = Split-Path -Parent $node
    }
    return $full
}

function NewDirectory([string]$Path) {
    $full = [IO.Path]::GetFullPath($Path)
    $ancestor = $full
    while (-not (Test-Path -LiteralPath $ancestor)) { $ancestor = Split-Path -Parent $ancestor }
    $null = Ordinary $ancestor $true
    $null = [IO.Directory]::CreateDirectory($full)
    return (Ordinary $full $true)
}

function Relative([string]$Base, [string]$Path) {
    $value = [IO.Path]::GetRelativePath($Base, $Path).Replace('\', '/')
    if ($value -match '(^/|\\|:|(^|/)\.\.?(/|$))') { throw 'A file escaped its input directory.' }
    return $value
}

function Hash([string]$Path) {
    return (Get-FileHash -LiteralPath (Ordinary $Path) -Algorithm SHA256).Hash.ToLowerInvariant()
}

function NewText([string]$Path, [string]$Value) {
    $stream = [IO.File]::Open($Path, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
    try {
        $bytes = [Text.UTF8Encoding]::new($false).GetBytes($Value)
        $stream.Write($bytes, 0, $bytes.Length)
        $stream.Flush($true)
    } finally { $stream.Dispose() }
}

function CopyVerified([string]$From, [string]$To) {
    $before = Hash $From
    $null = NewDirectory (Split-Path -Parent $To)
    [IO.File]::Copy((Ordinary $From), $To, $false)
    if ((Hash $From) -cne $before -or (Hash $To) -cne $before) { throw 'A deployment input changed.' }
}

function CopyLegalTree([string]$From, [string]$To) {
    $base = Ordinary $From $true
    $files = @(Get-ChildItem -LiteralPath $base -Recurse -File -Force)
    if (-not $files.Count -or $files.Count -gt 20000) { throw 'Unexpected legal-material inventory.' }
    foreach ($item in Get-ChildItem -LiteralPath $base -Recurse -Force) {
        $null = Ordinary $item.FullName $item.PSIsContainer
    }
    foreach ($file in $files) { CopyVerified $file.FullName (Join-Path $To (Relative $base $file.FullName)) }
}

$source = Ordinary $SourceRoot $true
$native = Ordinary (Join-Path $source 'qt-native') $true
$output = [IO.Path]::GetFullPath($OutputDirectory)
if ($output -ieq $source -or $output.StartsWith($source + '\', [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Release output must be outside the private checkout.'
}
if ((Test-Path -LiteralPath $output) -and @(Get-ChildItem -LiteralPath (Ordinary $output $true) -Force).Count) {
    throw 'Release output must be a fresh empty directory.'
}
$null = NewDirectory $output
$stage = NewDirectory (Join-Path (Ordinary $env:RUNNER_TEMP $true) ('windows-release-' + [guid]::NewGuid().ToString('N')))
$logs = NewDirectory (Join-Path $env:RUNNER_TEMP 'private-build-logs/windows-stages')
$payload = NewDirectory (Join-Path $stage 'payload')
$legal = NewDirectory (Join-Path $stage 'legal-materials')
$scripts = Ordinary (Join-Path $native 'scripts') $true
$pwsh = Ordinary (Join-Path $PSHOME 'pwsh.exe')

# Each process has its own logs outside both payload and public output. Never
# stream compiler/test messages or these files to public workflow logs.
function RunProgram([string]$Program, [string[]]$Arguments, [string]$Label,
                    [int]$Minutes = 30, [hashtable]$ExtraEnvironment = @{}) {
    $psi = [Diagnostics.ProcessStartInfo]::new()
    $psi.FileName = Ordinary $Program
    $psi.WorkingDirectory = $native
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    foreach ($argument in $Arguments) { $psi.ArgumentList.Add($argument) }
    foreach ($name in @('SOURCE_REPOSITORY', 'SOURCE_READ_TOKEN', 'GH_TOKEN', 'GITHUB_TOKEN')) {
        $null = $psi.Environment.Remove($name)
    }
    foreach ($name in $ExtraEnvironment.Keys) { $psi.Environment[$name] = [string]$ExtraEnvironment[$name] }
    $stdout = [IO.File]::Open((Join-Path $logs ($Label + '.stdout.log')), [IO.FileMode]::CreateNew)
    $stderr = [IO.File]::Open((Join-Path $logs ($Label + '.stderr.log')), [IO.FileMode]::CreateNew)
    $process = [Diagnostics.Process]::new()
    $process.StartInfo = $psi
    try {
        if (-not $process.Start()) { throw 'Could not start a release build step.' }
        $outTask = $process.StandardOutput.BaseStream.CopyToAsync($stdout)
        $errTask = $process.StandardError.BaseStream.CopyToAsync($stderr)
        if (-not $process.WaitForExit($Minutes * 60000)) {
            $process.Kill($true)
            $process.WaitForExit()
            throw 'A release build step timed out.'
        }
        $null = $outTask.GetAwaiter().GetResult()
        $null = $errTask.GetAwaiter().GetResult()
        if ($process.ExitCode -ne 0) { throw 'A release build step failed; private logs are retained on the runner.' }
    } finally { $stdout.Dispose(); $stderr.Dispose(); $process.Dispose() }
}

foreach ($script in @('ci-bootstrap.ps1', 'ci-build-test.ps1', 'fetch-qt-license-sources.ps1')) {
    $null = Ordinary (Join-Path $scripts $script)
}
$sourceCommit = (& git -C $source rev-parse --verify HEAD)
if ($LASTEXITCODE -ne 0 -or $sourceCommit -cnotmatch '\A[0-9a-f]{40}\z') { throw 'Could not identify the private source revision.' }
RunProgram $pwsh @('-NoLogo', '-NoProfile', '-NonInteractive', '-File', (Join-Path $scripts 'ci-bootstrap.ps1'), '-Run') 'bootstrap'
RunProgram $pwsh @('-NoLogo', '-NoProfile', '-NonInteractive', '-File', (Join-Path $scripts 'ci-build-test.ps1'), '-Run', '-Mode', 'dynamic') 'build-and-all-local-tests' 100 @{ GITHUB_SHA = $sourceCommit }
RunProgram $pwsh @('-NoLogo', '-NoProfile', '-NonInteractive', '-File', (Join-Path $scripts 'fetch-qt-license-sources.ps1'), '-Run') 'qt-legal-materials'

$qt = Ordinary (Join-Path $native '.tools/Qt/6.8.3/mingw_64') $true
$mingw = Ordinary (Join-Path $native '.tools/Qt/Tools/mingw1310_64') $true
$qtSources = Ordinary (Join-Path $native '.tools/Qt/6.8.3/Src') $true
$exe = Ordinary (Join-Path $native 'build-ci/mclauncher-qt.exe')
$windeployqt = Ordinary (Join-Path $qt 'bin/windeployqt.exe')
$toolPath = (Join-Path $qt 'bin') + ';' + (Join-Path $mingw 'bin') + ";$env:SystemRoot\System32;$env:SystemRoot"
$buildCache = [IO.File]::ReadAllText((Ordinary (Join-Path $native 'build-ci/CMakeCache.txt')))
if ($buildCache -notmatch '(?m)^CMAKE_BUILD_TYPE:STRING=Release\s*$' -or
    $buildCache -notmatch '(?m)^MCL_STATIC_RELEASE:BOOL=OFF\s*$') { throw 'Expected a dynamic Release application.' }
CopyVerified $exe (Join-Path $payload 'mclauncher-qt.exe')
RunProgram (Join-Path $mingw 'bin/strip.exe') @('--strip-debug', (Join-Path $payload 'mclauncher-qt.exe')) 'strip-application' 5 @{ PATH = $toolPath }
RunProgram $windeployqt @('--release', '--compiler-runtime', '--qmldir', (Ordinary (Join-Path $native 'src/ui') $true), '--dir', $payload, (Join-Path $payload 'mclauncher-qt.exe')) 'deploy-runtime' 10 @{ PATH = $toolPath }
foreach ($dll in @('libgcc_s_seh-1.dll', 'libstdc++-6.dll', 'libwinpthread-1.dll')) {
    $expected = Ordinary (Join-Path $mingw ('bin/' + $dll))
    $target = Join-Path $payload $dll
    if (Test-Path -LiteralPath $target) {
        if ((Hash $target) -cne (Hash $expected)) { throw 'Unexpected compiler runtime version.' }
    } else { CopyVerified $expected $target }
}
foreach ($required in @('mclauncher-qt.exe', 'Qt6Core.dll', 'Qt6Gui.dll', 'Qt6Network.dll', 'Qt6Qml.dll', 'Qt6Quick.dll', 'platforms/qwindows.dll', 'imageformats/qwebp.dll')) {
    $null = Ordinary (Join-Path $payload $required)
}
# Only Qt's public SDK may supply loose QML/JS modules. Application resources
# stay inside the binary; a relative application import must not become an
# unintended loose source file in the distributable.
$qmlExtensions = @('.qml', '.js', '.mjs', '.qmltypes', '.metainfo')
$publicQmlHashes = [Collections.Generic.HashSet[string]]::new([StringComparer]::Ordinal)
foreach ($file in Get-ChildItem -LiteralPath (Join-Path $qt 'qml') -Recurse -File) {
    if ($file.Name -ceq 'qmldir' -or $file.Extension.ToLowerInvariant() -in $qmlExtensions) {
        $null = $publicQmlHashes.Add((Hash $file.FullName))
    }
}
foreach ($file in Get-ChildItem -LiteralPath $payload -Recurse -File) {
    if (($file.Name -ceq 'qmldir' -or $file.Extension.ToLowerInvariant() -in $qmlExtensions) -and
        -not $publicQmlHashes.Contains((Hash $file.FullName))) {
        throw 'A loose QML/JS file did not originate from the public Qt SDK.'
    }
}

# The source acquisition script extracts only public Qt legal material from
# locked official archives; no application source or build evidence is copied.
foreach ($module in @('qtbase', 'qtdeclarative', 'qtsvg', 'qtimageformats')) {
    $moduleRoot = Ordinary (Join-Path $qtSources $module) $true
    $versionFile = Ordinary (Join-Path $moduleRoot '.cmake.conf')
    if ([IO.File]::ReadAllText($versionFile) -notmatch 'set\(QT_REPO_MODULE_VERSION\s+"6\.8\.3"\)') {
        throw 'Unexpected Qt legal-material version.'
    }
    CopyLegalTree (Join-Path $moduleRoot 'LICENSES') (Join-Path $legal ('licenses/Qt/' + $module + '/LICENSES'))
    $thirdParty = Join-Path $moduleRoot 'src/3rdparty'
    if (Test-Path -LiteralPath $thirdParty) {
        CopyLegalTree $thirdParty (Join-Path $legal ('licenses/Qt/' + $module + '/third-party'))
    }
}
foreach ($name in @('LGPL-3.0-only.txt', 'GPL-3.0-only.txt')) {
    if ([IO.File]::ReadAllText((Ordinary (Join-Path $legal ('licenses/Qt/qtbase/LICENSES/' + $name)))).Length -lt 5000) {
        throw 'Incomplete Qt license text.'
    }
}
foreach ($group in @('gcc', 'mingw-w64', 'winpthreads')) {
    CopyLegalTree (Join-Path $mingw ('licenses/' + $group)) (Join-Path $legal ('licenses/MinGW/' + $group))
}
foreach ($pair in @(
    @('third_party/miniz/LICENSE', 'licenses/miniz/LICENSE.txt'),
    @('third_party/koring/LICENSE', 'licenses/koring/LICENSE'),
    @('third_party/koring/README.md', 'licenses/koring/README.md'),
    @('third_party/fonts/roboto/OFL.txt', 'licenses/fonts/roboto/OFL.txt'),
    @('third_party/fonts/roboto/README.md', 'licenses/fonts/roboto/README.md'),
    @('third_party/fonts/material-symbols/LICENSE', 'licenses/fonts/material-symbols/LICENSE'),
    @('third_party/fonts/material-symbols/README.md', 'licenses/fonts/material-symbols/README.md')
)) { CopyVerified (Join-Path $native $pair[0]) (Join-Path $legal $pair[1]) }

$notice = @"
Little Craft Launcher $VersionTag - Windows x64 pre-release

Extract this entire archive to a writable directory and run mclauncher-qt.exe.
Keep the runtime DLLs and plugin/QML directories alongside the executable.
This pre-release is unsigned. It includes no Java runtime, game files, accounts,
tokens, saves, application data, private build logs, or application source tree.

Qt 6.8.3 is dynamically linked. The Qt DLLs/plugins are replaceable: keep a
backup, obtain or build compatible Qt 6.8.3 MinGW x64 libraries with the same
modules/ABI, and replace the corresponding DLLs/plugins in this extracted
directory while the launcher is closed. No signature check prevents replacing
these Qt components. Qt legal texts and third-party notices are in licenses/.
Unmodified upstream Qt source is available at:
https://download.qt.io/archive/qt/6.8/6.8.3/submodules/
https://download.qt.io/archive/qt/6.8/6.8.3/
The separate SUPPORTING_MATERIALS.zip contains these legal notices and dynamic
Qt replacement instructions; it contains no application source or relink objects.

This runtime package does not grant an open-source license to the application.
Microsoft login requires a project-authorized Client ID; this build retains the
source revision's existing configuration. See the pre-release page for its status.
The packaged GUI startup/close check does not verify Minecraft or live login.
"@
NewText (Join-Path $legal 'README.txt') $notice
CopyLegalTree $legal $payload
[IO.Compression.ZipFile]::CreateFromDirectory($legal, (Join-Path $payload 'SUPPORTING_MATERIALS.zip'), [IO.Compression.CompressionLevel]::Optimal, $false)

function Inventory([string]$Directory) {
    $result = [Collections.Generic.List[object]]::new()
    $items = @(Get-ChildItem -LiteralPath $Directory -Recurse -Force)
    if ($items.Count -gt 50000) { throw 'Deployment inventory is unexpectedly large.' }
    foreach ($item in $items) {
        $null = Ordinary $item.FullName $item.PSIsContainer
        $relative = Relative $Directory $item.FullName
        if ($relative -match '(^|/)(\.git|\.github|build[^/]*|Testing|CMakeFiles|mclauncher-data|\.minecraft|saves|versions|libraries|natives|private-logs)(/|$)' -or
            $relative -match '(^|/)(secrets?|state|accounts?|credentials?|tokens?)([._-]|/|$)' -or
            $relative -match '(?i)\.(pem|key|pfx|p12|enc|sqlite|db|log|jar|7z)$') {
            throw 'A forbidden source, state, credential or build artifact entered the runtime stage.'
        }
        if ($item.PSIsContainer) { continue }
        if (-not $relative.StartsWith('licenses/', [StringComparison]::Ordinal)) {
            $extension = $item.Extension.ToLowerInvariant()
            if ($item.Name -cne 'qmldir' -and $extension -notin @('.exe', '.dll', '.qml', '.js', '.mjs', '.qmltypes', '.metainfo', '.qm', '.rcc', '.conf', '.json', '.txt', '.zip', '.png', '.svg', '.jpg', '.jpeg', '.webp', '.gif', '.ttf', '.otf', '.qsb', '.qmlc', '.jsc')) {
                throw 'Unexpected file type in the runtime stage.'
            }
            if (($extension -eq '.exe' -and $relative -cne 'mclauncher-qt.exe') -or
                ($extension -eq '.zip' -and $relative -cne 'SUPPORTING_MATERIALS.zip')) {
                throw 'Unexpected executable or nested archive in the runtime stage.'
            }
        }
        $result.Add([ordered]@{ path = $relative; size = $item.Length; sha256 = (Hash $item.FullName) })
    }
    return @($result | Sort-Object path)
}
$records = @(Inventory $payload)
NewText (Join-Path $payload 'runtime-manifest.json') ([ordered]@{
    schema = 1; version = $VersionTag; platform = 'windows-x64'; qt = '6.8.3'; linkage = 'dynamic'
    files = $records
} | ConvertTo-Json -Depth 6)
$records = @(Inventory $payload)
NewText (Join-Path $payload 'SHA256SUMS.txt') (($records | ForEach-Object { $_.sha256 + '  ' + $_.path }) -join "`n")
$records = @(Inventory $payload)
$archiveName = 'LittleCraftLauncher-' + $VersionTag + '-windows-x64.zip'
$archivePath = Join-Path $stage $archiveName
[IO.Compression.ZipFile]::CreateFromDirectory($payload, $archivePath, [IO.Compression.CompressionLevel]::Optimal, $false)

# Validate a fresh extraction of the actual distributable. All runtime state and
# diagnostics are outside that extraction; the SDK is absent from the child PATH.
$extracted = Join-Path $stage 'fresh-extraction'
[IO.Compression.ZipFile]::ExtractToDirectory($archivePath, $extracted)
function VerifyExtraction {
    $actual = @(Inventory $extracted)
    if ($actual.Count -ne $records.Count) { throw 'Archive extraction inventory differs.' }
    for ($index = 0; $index -lt $records.Count; $index++) {
        if ($actual[$index].path -cne $records[$index].path -or $actual[$index].size -ne $records[$index].size -or
            $actual[$index].sha256 -cne $records[$index].sha256) { throw 'Archive extraction identity differs.' }
    }
}
VerifyExtraction
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class PackagedGuiWindow {
    [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr window);
    [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr window, out uint pid);
    [DllImport("user32.dll")] public static extern bool PostMessage(IntPtr window, uint message, IntPtr w, IntPtr l);
}
'@
$dataRoot = Join-Path $stage 'smoke-data'
$psi = [Diagnostics.ProcessStartInfo]::new()
$psi.FileName = Join-Path $extracted 'mclauncher-qt.exe'
$psi.WorkingDirectory = $extracted
$psi.UseShellExecute = $false
$psi.CreateNoWindow = $true
$psi.RedirectStandardOutput = $true
$psi.RedirectStandardError = $true
$psi.ArgumentList.Add('--data-root')
$psi.ArgumentList.Add($dataRoot)
foreach ($name in @($psi.Environment.Keys | Where-Object { $_ -match '^(QT|QML|MCL|MCLAUNCHER)' -or $_ -in @('SOURCE_REPOSITORY', 'SOURCE_READ_TOKEN', 'GH_TOKEN', 'GITHUB_TOKEN') })) {
    $null = $psi.Environment.Remove($name)
}
$psi.Environment['PATH'] = "$env:SystemRoot\System32;$env:SystemRoot"
$psi.Environment['QT_QPA_PLATFORM'] = 'windows'
$psi.Environment['QT_QUICK_BACKEND'] = 'software'
$psi.Environment['QML_DISABLE_DISK_CACHE'] = '1'
$process = [Diagnostics.Process]::new()
$process.StartInfo = $psi
$stdoutTask = $null
$stderrTask = $null
$started = $false
try {
    if (-not $process.Start()) { throw 'The packaged application could not start.' }
    $started = $true
    $stdoutTask = $process.StandardOutput.ReadToEndAsync()
    $stderrTask = $process.StandardError.ReadToEndAsync()
    $watch = [Diagnostics.Stopwatch]::StartNew()
    $window = [IntPtr]::Zero
    while ($watch.Elapsed.TotalSeconds -lt 30 -and -not $process.HasExited) {
        $process.Refresh()
        if ($process.MainWindowHandle -ne [IntPtr]::Zero -and $process.MainWindowTitle -like '*Little Craft Launcher*' -and
            [PackagedGuiWindow]::IsWindowVisible($process.MainWindowHandle)) {
            $window = $process.MainWindowHandle
            break
        }
        $null = $process.WaitForExit(100)
    }
    if ($window -eq [IntPtr]::Zero -or $process.WaitForExit(1500)) { throw 'The packaged GUI did not remain visible.' }
    [uint32]$owner = 0
    $null = [PackagedGuiWindow]::GetWindowThreadProcessId($window, [ref]$owner)
    if ($owner -ne $process.Id) { throw 'GUI window ownership differs.' }
    $modules = @($process.Modules)
    $runtimes = @($modules | Where-Object { $_.ModuleName -match '^(Qt6.*|libgcc_s_seh-1|libstdc\+\+-6|libwinpthread-1|qwindows)\.dll$' })
    foreach ($module in $runtimes) {
        if (-not ([IO.Path]::GetFullPath($module.FileName)).StartsWith($extracted + '\', [StringComparison]::OrdinalIgnoreCase)) {
            throw 'The packaged GUI used a runtime outside its extraction.'
        }
    }
    foreach ($required in @('Qt6Core.dll', 'Qt6Gui.dll', 'Qt6Qml.dll', 'Qt6Quick.dll', 'qwindows.dll')) {
        if (-not @($runtimes | Where-Object ModuleName -eq $required).Count) { throw 'A required packaged runtime was not observed.' }
    }
    if (-not [PackagedGuiWindow]::PostMessage($window, 0x0010, [IntPtr]::Zero, [IntPtr]::Zero) -or
        -not $process.WaitForExit(15000) -or $process.ExitCode -ne 0) { throw 'The packaged GUI did not close normally.' }
    $state = [IO.File]::ReadAllText((Ordinary (Join-Path $dataRoot 'state.json'))) | ConvertFrom-Json
    if (@($state.accounts).Count -ne 0 -or @($state.instances).Count -ne 0) { throw 'Unexpected accounts or instances in the isolated smoke data.' }
    VerifyExtraction
} finally {
    if ($started -and -not $process.HasExited) { $process.Kill($true); $process.WaitForExit() }
    if ($null -ne $stdoutTask) { NewText (Join-Path $logs 'packaged-gui.stdout.log') $stdoutTask.GetAwaiter().GetResult() }
    if ($null -ne $stderrTask) { NewText (Join-Path $logs 'packaged-gui.stderr.log') $stderrTask.GetAwaiter().GetResult() }
    $process.Dispose()
}

CopyVerified $archivePath (Join-Path $output $archiveName)
if (@(Get-ChildItem -LiteralPath $output -Force).Count -ne 1) { throw 'Release output contains an unexpected file.' }
Write-Output 'Windows runtime archive verified: complete local tests and isolated packaged GUI startup/normal close passed.'
