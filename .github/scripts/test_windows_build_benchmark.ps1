#Requires -Version 7.2
# Execute only the diagnostic's real helper functions against disposable files.
[CmdletBinding()]
param()
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$tokens = $null; $errors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile((Join-Path $PSScriptRoot 'benchmark-windows-build.ps1'), [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw 'The benchmark script did not parse.' }
$functions = @($ast.FindAll({ param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -match 'Benchmark' }, $true))
if ($functions.Count -ne 9) { throw 'Expected the complete benchmark helper set.' }
foreach ($function in $functions) { Invoke-Expression $function.Extent.Text }
$script:checks = 0
function Check([bool]$Condition, [string]$Message) { if (-not $Condition) { throw $Message }; ++$script:checks }
function Reject([scriptblock]$Action, [string]$Message) {
    $rejected = $false; try { & $Action | Out-Null } catch { $rejected = $true }; Check $rejected $Message
}
function Put([string]$Path, [string]$Text) {
    $null = [IO.Directory]::CreateDirectory((Split-Path -Parent $Path)); [IO.File]::WriteAllText($Path, $Text)
}
$root = Join-Path ([IO.Path]::GetTempPath()) ('build-benchmark-contract-' + [guid]::NewGuid().ToString('N'))
$null = [IO.Directory]::CreateDirectory($root)
try {
    $checkout = Join-Path $root 'checkout'; $null = [IO.Directory]::CreateDirectory($checkout)
    $build = New-BenchmarkBuildDirectory (Join-Path $root 'round-1') $checkout
    Check ([IO.Directory]::Exists($build)) 'A fresh build was not created.'
    Reject { New-BenchmarkBuildDirectory $build $checkout } 'An existing build directory was reused.'
    Reject { New-BenchmarkBuildDirectory (Join-Path $checkout 'build') $checkout } 'A build was created inside the checkout.'
    Reject { New-BenchmarkBuildDirectory $checkout $checkout } 'The checkout was accepted as a build directory.'
    $linked = Join-Path $root 'linked-parent'; $type = if ($IsWindows) { 'Junction' } else { 'SymbolicLink' }
    $null = New-Item -ItemType $type -Path $linked -Target $build
    try { Reject { New-BenchmarkBuildDirectory (Join-Path $linked 'nested') $checkout } 'A linked build ancestor was accepted.' }
    finally { (Get-Item -LiteralPath $linked -Force).Delete() }

    $qt = Join-Path $root 'qt'; $mingw = Join-Path $root 'mingw'; $ninja = Join-Path $root 'ninja.exe'
    $on = @(Get-BenchmarkConfigureArguments $checkout $build $qt $mingw $ninja 'ON')
    $off = @(Get-BenchmarkConfigureArguments $checkout $build $qt $mingw $ninja 'OFF')
    $different = @(Compare-Object $on $off)
    Check ($different.Count -eq 2 -and ($different.InputObject -ccontains '-DCMAKE_AUTOGEN_ORIGIN_DEPENDS=ON') -and
        ($different.InputObject -ccontains '-DCMAKE_AUTOGEN_ORIGIN_DEPENDS=OFF')) 'The comparison changed more than origin dependencies.'
    foreach ($flag in @('-DCMAKE_BUILD_TYPE=Release', '-DMCL_STATIC_RELEASE=OFF', '-DMCL_CORE_ONLY=OFF',
        '-DBUILD_TESTING=ON', '-DCMAKE_NINJA_FORCE_RESPONSE_FILE=ON', '-DCMAKE_FIND_USE_PACKAGE_REGISTRY=OFF',
        '-DCMAKE_FIND_USE_SYSTEM_PACKAGE_REGISTRY=OFF', "-DCMAKE_MAKE_PROGRAM=$ninja", "-DCMAKE_PREFIX_PATH=$qt",
        "-DCMAKE_C_COMPILER=$(Join-Path $mingw 'gcc.exe')", "-DCMAKE_CXX_COMPILER=$(Join-Path $mingw 'g++.exe')")) {
        Check ($on -ccontains $flag) 'A dynamic CI configuration flag was omitted.'
    }
    Check (-not @($on | Where-Object { $_ -match 'MICROSOFT|AUTOGEN_PARALLEL' }).Count) 'The benchmark overrode authentication or introduced another autogen variable.'

    $pwsh = Join-Path $PSHOME $(if ($IsWindows) { 'pwsh.exe' } else { 'pwsh' })
    $saved = @{}
    try {
        foreach ($key in @('SOURCE_READ_TOKEN', 'GH_TOKEN', 'CXXFLAGS', 'MCL_MS_CLIENT_ID', 'QML_DISK_CACHE_PATH')) {
            $saved[$key] = [Environment]::GetEnvironmentVariable($key); [Environment]::SetEnvironmentVariable($key, 'PRIVATE_MARKER')
        }
        $psi = New-BenchmarkProcessInfo $pwsh @('-NoProfile') $root @{ TEMP = $build }
        foreach ($key in $saved.Keys) { Check (-not $psi.Environment.ContainsKey($key)) 'Private or compiler state leaked into a child process.' }
        Check ($psi.Environment['TEMP'] -ceq $build) 'The selected fresh scratch directory was not used.'
    } finally { foreach ($key in $saved.Keys) { [Environment]::SetEnvironmentVariable($key, $saved[$key]) } }
    $logs = Join-Path $root 'private-logs'; $null = [IO.Directory]::CreateDirectory($logs)
    $result = Invoke-BenchmarkTool $pwsh @('-NoLogo', '-NoProfile', '-NonInteractive', '-Command', "Write-Output 'PRIVATE_MARKER'; [Console]::Error.WriteLine('PRIVATE_ERROR')") 'fixture' $root $logs
    Check ($result.exitCode -eq 0 -and $result.seconds -ge 0) 'The real child process timing was not recorded.'
    Check ([IO.File]::ReadAllText((Join-Path $logs 'fixture.stdout.log')).Contains('PRIVATE_MARKER')) 'Private stdout was not retained.'
    Check ([IO.File]::ReadAllText((Join-Path $logs 'fixture.stderr.log')).Contains('PRIVATE_ERROR')) 'Private stderr was not retained.'
    Reject { Invoke-BenchmarkTool $pwsh @('-NoProfile', '-Command', 'exit 7') 'failure' $root $logs } 'A failed build subprocess was accepted.'

    foreach ($name in @('.ninja_log', 'build.ninja', 'CMakeFiles/rules.ninja', 'CMakeCache.txt',
        'generated/auth_build_config.h', 'generated/application_build_config.h', 'CMakeFiles/example_autogen.dir/AutogenInfo.json')) {
        Put (Join-Path $build $name) 'PRIVATE_METADATA'
    }
    Put (Join-Path $build 'private-application.exe') 'DO_NOT_UPLOAD'
    Put (Join-Path $build 'CMakeFiles/private-code.obj') 'DO_NOT_UPLOAD'
    $metadata = Join-Path $logs 'metadata'; Copy-BenchmarkBuildMetadata $build $metadata
    Check (@(Get-ChildItem -LiteralPath $metadata -Recurse -File -Force).Count -eq 7) 'Unexpected build files entered encrypted diagnostic metadata.'
    Check (-not @(Get-ChildItem -LiteralPath $metadata -Recurse -File -Force | Where-Object Extension -cin @('.exe', '.obj')).Count) 'Compiled application outputs were collected.'

    $state = [ordered]@{ success = $true; sourceCommit = ('1' * 40); workflowCommit = ('2' * 40)
        sourceSnapshotSha256 = ('3' * 64); toolchainSha256 = ('4' * 64); buildParallel = 4
        rounds = @(for ($i = 1; $i -le 3; $i++) { [ordered]@{ index = $i; originDepends = @('ON', 'OFF', 'ON')[$i - 1]; configureSeconds = 1.1; buildSeconds = 2.2; privatePath = 'PRIVATE_MARKER' } })
        privatePath = 'PRIVATE_MARKER'; failure = 'PRIVATE_MARKER'; hardware = 'PRIVATE_MARKER' }
    $summary = Get-BenchmarkPublicSummary $state; $json = $summary | ConvertTo-Json -Depth 6
    Check (-not $json.Contains('PRIVATE_MARKER')) 'The public summary leaked private state.'
    Check (-not $summary.releaseEligible -and -not $summary.testsExecuted -and -not $summary.runtimePackaged -and -not $summary.buildCacheRestored) 'A build-only diagnostic was promoted to a release gate.'
    $state.sourceSnapshotSha256 = 'PRIVATE_MARKER'
    Reject { Get-BenchmarkPublicSummary $state } 'An invalid digest could leak into the public summary.'
    $state.sourceSnapshotSha256 = '3' * 64
    foreach ($key in @('configureSeconds', 'buildSeconds')) {
        $savedSeconds = $state.rounds[1][$key]
        foreach ($badSeconds in @(-1.0, [double]::NaN, [double]::PositiveInfinity, [double]::NegativeInfinity)) {
            $state.rounds[1][$key] = $badSeconds
            Reject { Get-BenchmarkPublicSummary $state } 'An invalid duration was accepted in the public summary.'
        }
        $state.rounds[1][$key] = $savedSeconds
    }
    foreach ($parallel in @(0, 5, 1.5)) {
        $state.buildParallel = $parallel
        Reject { Get-BenchmarkPublicSummary $state } 'Out-of-range or fractional compile parallelism was accepted.'
    }
    $state.buildParallel = 4
    $allRounds = $state.rounds; $state.rounds = @($allRounds[0], $allRounds[1])
    Reject { Get-BenchmarkPublicSummary $state } 'Missing rounds were marked as a successful comparison.'
    $state.success = $false
    Check ((Get-BenchmarkPublicSummary $state).status -ceq 'FAILED_BUILD_ONLY') 'A partial failed diagnostic did not retain its failure status.'
    $state.success = $true; $state.rounds = $allRounds; $state.rounds[1].originDepends = 'ON'
    Reject { Get-BenchmarkPublicSummary $state } 'The wrong ON/OFF/ON order was accepted.'
    $state.rounds[1].originDepends = 'OFF'; $state.rounds[1].index = 1
    Reject { Get-BenchmarkPublicSummary $state } 'Duplicate round indexes were accepted.'
    $state.rounds[1].index = 2
    Check ((Get-BenchmarkDigest @('unchanged')) -ceq (Get-BenchmarkDigest @('unchanged'))) 'Stable input hashes differ.'
    Check ((Get-BenchmarkDigest @('before')) -cne (Get-BenchmarkDigest @('after'))) 'Changed input hashes were accepted.'
    Write-Output "PASS: $script:checks build-only benchmark contract checks."
} finally { Remove-Item -LiteralPath $root -Recurse -Force }
