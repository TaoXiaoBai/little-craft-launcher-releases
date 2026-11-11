#Requires -Version 7.2
# Runs only on the Windows release runner. All diagnostics remain in private
# logs; the wrapper is copied alone, without the build checkout or Qt on PATH.
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$Executable,
    [Parameter(Mandatory)][string]$PayloadDirectory,
    [Parameter(Mandatory)][string]$PayloadSha256,
    [Parameter(Mandatory)][string]$StageDirectory,
    [Parameter(Mandatory)][string]$LogDirectory
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
if (-not $IsWindows -or $env:GITHUB_ACTIONS -cne 'true' -or $PayloadSha256 -cnotmatch '\A[0-9a-f]{64}\z') {
    throw 'Use an explicitly invoked Windows release runner and a verified payload.'
}

function Ordinary([string]$Path, [bool]$Directory = $false) {
    $full = [IO.Path]::GetFullPath($Path)
    $node = $full
    $attributes = [IO.File]::GetAttributes($node)
    if ((($attributes -band [IO.FileAttributes]::Directory) -ne 0) -ne $Directory) { throw 'Unexpected smoke input type.' }
    while ($node) {
        if ($attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Linked smoke paths are not supported.' }
        $node = Split-Path -Parent $node
        if ($node) { $attributes = [IO.File]::GetAttributes($node) }
    }
    return $full
}
function NewDirectory([string]$Path) {
    if (Test-Path -LiteralPath $Path) { throw 'Smoke directories must be fresh.' }
    $parent = Split-Path -Parent $Path
    $null = Ordinary $parent $true
    $null = [IO.Directory]::CreateDirectory($Path)
    return (Ordinary $Path $true)
}
function Digest([string]$Path) {
    return (Get-FileHash -LiteralPath (Ordinary $Path) -Algorithm SHA256).Hash.ToLowerInvariant()
}
function CanonicalPath([string]$Path) {
    # Win32 reports loaded modules with or without the extended local-drive
    # prefix. Treat those spellings equally, while rejecting UNC/device paths.
    if ($Path.StartsWith('\\?\', [StringComparison]::Ordinal)) {
        if ($Path -cnotmatch '\A\\\\\?\\[A-Za-z]:\\') { throw 'An unexpected extended process path was reported.' }
        $Path = $Path.Substring(4)
    }
    if ($Path.StartsWith('\\', [StringComparison]::Ordinal)) { throw 'A non-local process path was reported.' }
    return [IO.Path]::GetFullPath($Path).TrimEnd('\')
}
function SamePath([string]$Left, [string]$Right) {
    return [string]::Equals((CanonicalPath $Left), (CanonicalPath $Right), [StringComparison]::OrdinalIgnoreCase)
}

$wrapper = Ordinary $Executable
$payload = Ordinary $PayloadDirectory $true
$stage = Ordinary $StageDirectory $true
$logs = Ordinary $LogDirectory $true
$root = NewDirectory (Join-Path $stage '单文件 独立运行 smoke')
$cacheBase = NewDirectory (Join-Path $root '运行 缓存')
$cache = Join-Path $cacheBase $PayloadSha256
$profile = NewDirectory (Join-Path $root '隔离 profile')
$localAppData = NewDirectory (Join-Path $profile 'Local AppData')
$appData = NewDirectory (Join-Path $profile 'Roaming AppData')
$foreignQt = NewDirectory (Join-Path $root 'foreign Qt must not load')
# Provide a valid competing platform plugin outside the cache. If an inherited
# platform-plugin path wins, the loaded-module ownership assertion catches it.
$foreignPlatforms = NewDirectory (Join-Path $foreignQt 'platforms')
[IO.File]::Copy((Ordinary (Join-Path $payload 'platforms/qwindows.dll')), (Join-Path $foreignPlatforms 'qwindows.dll'), $false)
$expected = [Collections.Generic.Dictionary[string,object]]::new([StringComparer]::Ordinal)
foreach ($entry in Get-ChildItem -LiteralPath $payload -Recurse -Force) {
    $null = Ordinary $entry.FullName $entry.PSIsContainer
    if (-not $entry.PSIsContainer) {
        $relative = [IO.Path]::GetRelativePath($payload, $entry.FullName)
        $expected.Add($relative, @{ Size = $entry.Length; Hash = (Digest $entry.FullName) })
    }
}
if (-not $expected.Count) { throw 'Empty expected payload.' }

function AssertRuntime([string]$Directory) {
    $base = Ordinary $Directory $true
    $files = @(Get-ChildItem -LiteralPath $base -Recurse -Force | ForEach-Object {
        $null = Ordinary $_.FullName $_.PSIsContainer
        if (-not $_.PSIsContainer) { $_ }
    })
    if ($files.Count -ne $expected.Count) { throw 'The prepared runtime inventory changed or contains user state.' }
    foreach ($file in $files) {
        $relative = [IO.Path]::GetRelativePath($base, $file.FullName)
        if (-not $expected.ContainsKey($relative) -or $file.Length -ne $expected[$relative].Size -or
            (Digest $file.FullName) -cne $expected[$relative].Hash) { throw 'The prepared runtime differs from its verified payload.' }
    }
}
function CopyAlone([string]$Name) {
    $directory = NewDirectory (Join-Path $root $Name)
    $destination = Join-Path $directory 'mclauncher-qt.exe'
    [IO.File]::Copy($wrapper, $destination, $false)
    if ((Digest $destination) -cne (Digest $wrapper) -or @(Get-ChildItem -LiteralPath $directory -Force).Count -ne 1) {
        throw 'The independent executable copy differs.'
    }
    return $destination
}
function StartInfo([string]$Program, [string[]]$Arguments, [bool]$Diagnostics = $false) {
    $psi = [Diagnostics.ProcessStartInfo]::new()
    $psi.FileName = Ordinary $Program
    $psi.WorkingDirectory = Split-Path -Parent $Program
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    # The Qt CLI writes UTF-8 JSON bytes. Windows' default redirected reader
    # otherwise uses the OEM code page and corrupts Chinese paths before JSON
    # and data-root comparisons. The GUI diagnostic reader uses this setup too.
    $psi.StandardOutputEncoding = [Text.UTF8Encoding]::new($false)
    $psi.StandardErrorEncoding = [Text.UTF8Encoding]::new($false)
    foreach ($argument in $Arguments) { $psi.ArgumentList.Add($argument) }
    foreach ($name in @($psi.Environment.Keys | Where-Object { $_ -match '^(QT|QML|MCL|MCLAUNCHER)' -or $_ -in @('SOURCE_REPOSITORY', 'SOURCE_READ_TOKEN', 'GH_TOKEN', 'GITHUB_TOKEN') })) {
        $null = $psi.Environment.Remove($name)
    }
    $psi.Environment['PATH'] = "$env:SystemRoot\System32;$env:SystemRoot"
    $psi.Environment['LOCALAPPDATA'] = $localAppData
    $psi.Environment['APPDATA'] = $appData
    $psi.Environment['USERPROFILE'] = $profile
    $psi.Environment['MCL_BUNDLE_CACHE_ROOT'] = $cacheBase
    if ($Diagnostics) { $psi.Environment['MCL_BUNDLE_DIAGNOSTICS'] = '1' }
    # Deliberately foreign SDK/plugin/import paths must never leak to the child.
    foreach ($name in @('QT_PLUGIN_PATH', 'QT_QPA_PLATFORM_PLUGIN_PATH', 'QML_IMPORT_PATH', 'QML2_IMPORT_PATH', 'QTDIR', 'QT_HOST_PATH')) {
        $psi.Environment[$name] = $foreignQt
    }
    $psi.Environment['QT_QPA_PLATFORM_PLUGIN_PATH'] = $foreignPlatforms
    $psi.Environment['QT_QPA_PLATFORM'] = 'windows'
    $psi.Environment['QT_QUICK_BACKEND'] = 'software'
    $psi.Environment['QML_DISABLE_DISK_CACHE'] = '1'
    return $psi
}
function InvokeCli([string]$Program, [string[]]$Arguments, [string]$Label, [int]$ExpectedExit = 0) {
    $process = [Diagnostics.Process]::new()
    $process.StartInfo = StartInfo $Program $Arguments
    $stdoutTask = $null; $stderrTask = $null; $started = $false
    try {
        if (-not $process.Start()) { throw 'The independent executable could not start.' }
        $started = $true
        $stdoutTask = $process.StandardOutput.ReadToEndAsync()
        $stderrTask = $process.StandardError.ReadToEndAsync()
        if (-not $process.WaitForExit(120000)) { throw 'An independent CLI smoke timed out.' }
        $stdout = $stdoutTask.GetAwaiter().GetResult()
        $stderr = $stderrTask.GetAwaiter().GetResult()
        if ($process.ExitCode -ne $ExpectedExit) { throw 'The independent CLI exit status was not forwarded.' }
        return $stdout
    } finally {
        if ($started -and -not $process.HasExited) { $process.Kill($true); $process.WaitForExit() }
        if ($null -ne $stdoutTask) { [IO.File]::WriteAllText((Join-Path $logs ($Label + '.stdout.log')), $stdoutTask.GetAwaiter().GetResult()) }
        if ($null -ne $stderrTask) { [IO.File]::WriteAllText((Join-Path $logs ($Label + '.stderr.log')), $stderrTask.GetAwaiter().GetResult()) }
        $process.Dispose()
    }
}
function AssertCliRoot([string]$Program, [string[]]$Arguments, [string]$Root, [string]$Label) {
    $result = (InvokeCli $Program $Arguments $Label) | ConvertFrom-Json
    if ($result.status -cne 'NATIVE_STATE_READ' -or -not (SamePath $result.dataRoot $Root) -or @($result.instances).Count) {
        throw 'CLI arguments or the native data root were not preserved.'
    }
    $state = [IO.File]::ReadAllText((Ordinary (Join-Path $Root 'state.json'))) | ConvertFrom-Json
    if (@($state.accounts).Count -or @($state.instances).Count) { throw 'Unexpected user data in the isolated profile.' }
    AssertRuntime $cache
}

$standalone = CopyAlone 'clean 中文 launcher'
$data = Join-Path $root '独立 data with spaces'
AssertCliRoot $standalone @('--cli', '--data-root', $data) $data 'bundle-cold-cli'
$cachedExe = Join-Path $cache 'mclauncher-qt.exe'
$created = (Get-Item -LiteralPath (Ordinary $cache $true)).CreationTimeUtc.Ticks
$written = (Get-Item -LiteralPath (Ordinary $cachedExe)).LastWriteTimeUtc.Ticks
$version = InvokeCli $standalone @('--cli', '--version') 'bundle-version'
if ($version -notmatch '\d+\.\d+' -or $version -match 'LCL_BUNDLE_CHILD_PID') { throw 'CLI version output changed or leaked diagnostics.' }
AssertCliRoot $standalone @('--cli', ('--data-root=' + $data + '\'), '--') $data 'bundle-hot-cli'
if ((Get-Item -LiteralPath $cache).CreationTimeUtc.Ticks -ne $created -or (Get-Item -LiteralPath $cachedExe).LastWriteTimeUtc.Ticks -ne $written) {
    throw 'A valid hot cache was replaced instead of reused.'
}
$invalid = (InvokeCli $standalone @('--cli', '--data-root', $data, '--timeout', '9') 'bundle-invalid-cli' 1) | ConvertFrom-Json
if ($invalid.status -cne 'FAILED') { throw 'A child CLI validation failure was not forwarded.' }

# Damaged ordinary files are repaired, without placing user state in the cache.
[IO.File]::Delete((Ordinary (Join-Path $cache 'Qt6Core.dll')))
AssertCliRoot $standalone @('--cli', '--data-root', $data) $data 'bundle-missing-cache'
[IO.File]::WriteAllBytes((Ordinary (Join-Path $cache 'Qt6Quick.dll')), [byte[]]@(0, 1, 2, 3))
AssertCliRoot $standalone @('--cli', '--data-root', $data) $data 'bundle-corrupt-cache'

# A runtime cache is never a place to erase unexpected user files. Refuse this
# tree in place, then recover normally once the owner removes the extra entry.
$unowned = NewDirectory (Join-Path $cache 'mclauncher-qt-data')
$unownedMarker = Join-Path $unowned 'preserve-user-marker.txt'
[IO.File]::WriteAllText($unownedMarker, 'user data must never be moved or removed')
$unownedHash = Digest $unownedMarker
$null = InvokeCli $standalone @('--cli', '--data-root', $data) 'bundle-unexpected-cache-refused' 1
if (-not (Test-Path -LiteralPath $unownedMarker) -or (Digest $unownedMarker) -cne $unownedHash) {
    throw 'Unexpected cache user data was moved, removed or changed.'
}
[IO.File]::Delete($unownedMarker)
[IO.Directory]::Delete($unowned)
AssertCliRoot $standalone @('--cli', '--data-root', $data) $data 'bundle-unexpected-cache-cleared'

# Rebuilding a corrupt cache must not truncate a hardlink's external target.
$hardlinkTarget = Join-Path $root 'external fixture must remain.bin'
[IO.File]::WriteAllBytes($hardlinkTarget, [byte[]]@(4, 3, 2, 1))
$hardlinkHash = Digest $hardlinkTarget
$hardlinkCacheFile = Ordinary (Join-Path $cache 'Qt6Quick.dll')
[IO.File]::Delete($hardlinkCacheFile)
$null = New-Item -ItemType HardLink -Path $hardlinkCacheFile -Target $hardlinkTarget
AssertCliRoot $standalone @('--cli', '--data-root', $data) $data 'bundle-hardlink-cache-repair'
if ((Digest $hardlinkTarget) -cne $hardlinkHash) { throw 'Cache repair modified an external hardlink target.' }

$portableExe = CopyAlone 'portable 中文 launcher'
$portableData = Join-Path (Split-Path -Parent $portableExe) 'mclauncher-qt-data'
AssertCliRoot $portableExe @('--cli', '--portable', '--') $portableData 'bundle-portable-cli'
$marker = Join-Path $portableData 'preserve-user-marker.txt'
[IO.File]::WriteAllText($marker, 'existing portable user data')
$markerHash = Digest $marker
AssertCliRoot $portableExe @('--cli') $portableData 'bundle-existing-portable-cli'
if ((Digest $marker) -cne $markerHash) { throw 'Existing portable user data changed.' }

# Detect an already existing neighboring profile even before --portable was used.
$legacyExe = CopyAlone 'legacy 中文 launcher'
$legacyData = NewDirectory (Join-Path (Split-Path -Parent $legacyExe) 'mclauncher-qt-data')
$legacyMarker = Join-Path $legacyData 'preserve-user-marker.txt'
[IO.File]::WriteAllText($legacyMarker, 'legacy portable user data')
$legacyHash = Digest $legacyMarker
AssertCliRoot $legacyExe @('--cli') $legacyData 'bundle-legacy-portable-cli'
$explicitData = Join-Path $root 'explicit 优先 data'
AssertCliRoot $legacyExe @('--cli', '--portable', ('--data-root=' + $explicitData)) $explicitData 'bundle-explicit-root-cli'
if ((Digest $legacyMarker) -cne $legacyHash) { throw 'Legacy portable user data changed.' }

$replaceable = Join-Path $root '可替换 Qt runtime'
$null = InvokeCli $standalone @('--extract-runtime', $replaceable) 'bundle-extract-runtime'
AssertRuntime $replaceable
# Replacement delivery must preserve an existing directory rather than erase it.
$null = InvokeCli $standalone @('--extract-runtime', $legacyData) 'bundle-existing-extract-rejected' 1
if ((Digest $legacyMarker) -cne $legacyHash) { throw 'Runtime extraction changed an existing directory.' }

Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class BundleGuiWindow {
    [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr window);
    [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr window, out uint pid);
    [DllImport("user32.dll")] public static extern bool PostMessage(IntPtr window, uint message, IntPtr w, IntPtr l);
}
'@
$guiData = Join-Path $root 'GUI 独立 data'
# The GUI must prove a second cold preparation, rather than depend on any of
# the preceding CLI/extraction cases having prepared its runtime.
$cacheBase = NewDirectory (Join-Path $root 'GUI cold 运行缓存')
$cache = Join-Path $cacheBase $PayloadSha256
$cachedExe = Join-Path $cache 'mclauncher-qt.exe'
$outer = [Diagnostics.Process]::new()
$outer.StartInfo = StartInfo $standalone @('--data-root', $guiData) $true
$child = $null; $outTail = $null; $lineTask = $null; $errTask = $null; $started = $false
$prefix = [Text.StringBuilder]::new()
try {
    if (-not $outer.Start()) { throw 'The independent GUI wrapper could not start.' }
    $started = $true
    $errTask = $outer.StandardError.ReadToEndAsync()
    $watch = [Diagnostics.Stopwatch]::StartNew()
    while ($watch.Elapsed.TotalSeconds -lt 60) {
        $lineTask = $outer.StandardOutput.ReadLineAsync()
        while (-not $lineTask.IsCompleted -and $watch.Elapsed.TotalSeconds -lt 60) { $null = $outer.WaitForExit(100) }
        if (-not $lineTask.IsCompleted) { throw 'The wrapper did not report its child process.' }
        $line = $lineTask.GetAwaiter().GetResult()
        $lineTask = $null
        if ($null -eq $line) { throw 'The wrapper closed stdout without reporting its child process.' }
        $null = $prefix.AppendLine($line)
        if ($line -cmatch '\ALCL_BUNDLE_CHILD_PID=([1-9][0-9]*)\z') {
            $child = [Diagnostics.Process]::GetProcessById([int]$Matches[1])
            break
        }
    }
    if ($null -eq $child -or $child.Id -eq $outer.Id) { throw 'The GUI child process was not identified.' }
    $outTail = $outer.StandardOutput.ReadToEndAsync()
    $watch.Restart()
    $window = [IntPtr]::Zero
    while ($watch.Elapsed.TotalSeconds -lt 30 -and -not $child.HasExited -and -not $outer.HasExited) {
        $child.Refresh()
        if ($child.MainWindowHandle -ne [IntPtr]::Zero -and $child.MainWindowTitle -like '*Little Craft Launcher*' -and
            [BundleGuiWindow]::IsWindowVisible($child.MainWindowHandle)) { $window = $child.MainWindowHandle; break }
        $null = $child.WaitForExit(100)
    }
    if ($window -eq [IntPtr]::Zero -or $child.WaitForExit(1500)) { throw 'The independent GUI did not remain visible.' }
    # The PID is emitted while the new process is still suspended. Inspect its
    # modules only after the window proves that the Windows loader has finished.
    if (-not (SamePath $child.MainModule.FileName $cachedExe)) { throw 'The GUI child is outside the verified runtime cache.' }
    [uint32]$owner = 0
    $null = [BundleGuiWindow]::GetWindowThreadProcessId($window, [ref]$owner)
    if ($owner -ne $child.Id) { throw 'The visible GUI belongs to a different process.' }
    $runtimes = @($child.Modules | Where-Object { $_.ModuleName -match '^(Qt6.*|libgcc_s_seh-1|libstdc\+\+-6|libwinpthread-1|qwindows)\.dll$' })
    foreach ($module in $runtimes) {
        if (-not (CanonicalPath $module.FileName).StartsWith((CanonicalPath $cache) + '\', [StringComparison]::OrdinalIgnoreCase)) {
            throw 'The independent GUI loaded an external runtime.'
        }
    }
    foreach ($name in @('Qt6Core.dll', 'Qt6Gui.dll', 'Qt6Qml.dll', 'Qt6Quick.dll', 'qwindows.dll')) {
        if (-not @($runtimes | Where-Object ModuleName -eq $name).Count) { throw 'A required independent GUI runtime was not observed.' }
    }
    if (-not [BundleGuiWindow]::PostMessage($window, 0x0010, [IntPtr]::Zero, [IntPtr]::Zero) -or
        -not $child.WaitForExit(15000) -or $child.ExitCode -ne 0 -or
        -not $outer.WaitForExit(15000) -or $outer.ExitCode -ne 0) { throw 'The independent GUI and wrapper did not close normally.' }
    $state = [IO.File]::ReadAllText((Ordinary (Join-Path $guiData 'state.json'))) | ConvertFrom-Json
    if (@($state.accounts).Count -or @($state.instances).Count) { throw 'Unexpected state in the GUI profile.' }
    AssertRuntime $cache
} finally {
    if ($null -ne $child -and -not $child.HasExited) { $child.Kill($true); $child.WaitForExit() }
    if ($started -and -not $outer.HasExited) { $outer.Kill($true); $outer.WaitForExit() }
    if ($null -ne $lineTask) { $null = $prefix.AppendLine($lineTask.GetAwaiter().GetResult()) }
    if ($started -and $null -eq $outTail) { $outTail = $outer.StandardOutput.ReadToEndAsync() }
    if ($null -ne $outTail) { [IO.File]::WriteAllText((Join-Path $logs 'bundle-gui.stdout.log'), $prefix.ToString() + $outTail.GetAwaiter().GetResult()) }
    if ($null -ne $errTask) { [IO.File]::WriteAllText((Join-Path $logs 'bundle-gui.stderr.log'), $errTask.GetAwaiter().GetResult()) }
    if ($null -ne $child) { $child.Dispose() }
    $outer.Dispose()
}
Write-Output 'Independent single-executable CLI, cache, portable data and native GUI checks passed.'
