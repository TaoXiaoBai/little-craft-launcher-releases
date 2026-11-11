#Requires -Version 7.2
# Runs the actual packaging functions against disposable files, without invoking
# a build or requiring Windows native APIs. No application source is loaded.
[CmdletBinding()]
param()
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$tokens = $null; $parseErrors = $null
$packageScript = Join-Path $PSScriptRoot 'package-windows.ps1'
$ast = [Management.Automation.Language.Parser]::ParseFile($packageScript, [ref]$tokens, [ref]$parseErrors)
if ($parseErrors.Count) { throw 'The Windows packaging script did not parse.' }
$required = @('Ordinary', 'Relative', 'Hash', 'NewText', 'AssertPublicQmlFiles', 'Inventory', 'WriteRuntimeMetadata')
$functions = @($ast.FindAll({ param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -cin $required }, $true))
if ($functions.Count -ne $required.Count) { throw 'The packaging test helpers are incomplete.' }
foreach ($function in $functions) { Invoke-Expression $function.Extent.Text }

$script:checks = 0
function Check([bool]$Condition, [string]$Message) {
    if (-not $Condition) { throw $Message }
    ++$script:checks
}
function Reject([scriptblock]$Action, [string]$Message) {
    $rejected = $false
    try { & $Action | Out-Null } catch { $rejected = $true }
    Check $rejected $Message
}
function Put([string]$Path, [string]$Contents) {
    $null = [IO.Directory]::CreateDirectory((Split-Path -Parent $Path))
    [IO.File]::WriteAllText($Path, $Contents, [Text.UTF8Encoding]::new($false))
}

$root = Join-Path ([IO.Path]::GetTempPath()) ('mcl-runtime-inventory-test-' + [guid]::NewGuid().ToString('N'))
$null = [IO.Directory]::CreateDirectory($root)
try {
    $sdk = Join-Path $root 'sdk'
    $runtime = Join-Path $root 'runtime'
    foreach ($name in @('Button.qml', 'helper.js', 'helper.mjs', 'plugins.qmltypes', 'module.metainfo', 'qmldir')) {
        Put (Join-Path $sdk ('qml/QtQuick/' + $name)) ('public SDK ' + $name)
        Put (Join-Path $runtime ('qml/QtQuick/' + $name)) ('public SDK ' + $name)
    }
    Put (Join-Path $runtime 'mclauncher-qt.exe') 'fixture runtime'
    $ordinaryFile = Join-Path $runtime 'mclauncher-qt.exe'
    Check ((Ordinary $ordinaryFile) -ceq [IO.Path]::GetFullPath($ordinaryFile)) 'An ordinary file was not accepted.'
    Check ((Ordinary $runtime $true) -ceq [IO.Path]::GetFullPath($runtime)) 'An ordinary directory was not accepted.'
    Reject { Ordinary '' } 'An empty path was accepted.'
    Reject { Ordinary $runtime } 'A directory was accepted as a file.'
    Reject { Ordinary $ordinaryFile $true } 'A file was accepted as a directory.'
    Reject { Ordinary (Join-Path $runtime 'missing.dll') } 'A missing file was accepted.'
    Reject { Ordinary (Join-Path $ordinaryFile 'child.dll') } 'A file was accepted as an ancestor directory.'
    $linkedRoot = Join-Path $root 'linked-runtime'
    $linkType = if ($IsWindows) { 'Junction' } else { 'SymbolicLink' }
    $null = New-Item -ItemType $linkType -Path $linkedRoot -Target $runtime
    try {
        Reject { Ordinary $linkedRoot $true } 'A linked directory or Windows junction was accepted.'
        Reject { Ordinary (Join-Path $linkedRoot 'mclauncher-qt.exe') } 'A linked ancestor or Windows junction was accepted.'
    } finally { (Get-Item -LiteralPath $linkedRoot -Force).Delete() }
    AssertPublicQmlFiles $runtime $sdk
    Check $true 'All supported public QML file types should pass.'

    foreach ($name in @('Button.qml', 'helper.js', 'helper.mjs', 'plugins.qmltypes', 'module.metainfo', 'qmldir')) {
        $file = Join-Path $runtime ('qml/QtQuick/' + $name)
        Put $file 'private application code'
        Reject { AssertPublicQmlFiles $runtime $sdk } ('Changed module bytes were accepted: ' + $name)
        Put $file ('public SDK ' + $name)
    }

    $wrongPath = Join-Path $runtime 'qml/QtQuick/Private.qml'
    Put $wrongPath 'public SDK Button.qml'
    Reject { AssertPublicQmlFiles $runtime $sdk } 'A public hash at an unknown SDK path was accepted.'
    [IO.File]::Delete($wrongPath)

    $outsideModules = Join-Path $runtime 'Private.qml'
    Put $outsideModules 'public SDK Button.qml'
    Reject { AssertPublicQmlFiles $runtime $sdk } 'Loose QML outside the module directory was accepted.'
    [IO.File]::Delete($outsideModules)

    # A separate ordinary SDK path may not authorize a linked deployment file.
    if (-not $IsWindows) {
        $linked = Join-Path $runtime 'qml/QtQuick/helper.js'
        [IO.File]::Delete($linked)
        $null = New-Item -ItemType SymbolicLink -Path $linked -Target (Join-Path $sdk 'qml/QtQuick/helper.js')
        Reject { AssertPublicQmlFiles $runtime $sdk } 'A linked QML file was accepted.'
        [IO.File]::Delete($linked)
        Put $linked 'public SDK helper.js'

        $sdkLink = Join-Path $sdk 'qml/QtQuick/helper.js'
        [IO.File]::Delete($sdkLink)
        $null = New-Item -ItemType SymbolicLink -Path $sdkLink -Target $linked
        Reject { AssertPublicQmlFiles $runtime $sdk } 'A linked SDK origin was accepted.'
        [IO.File]::Delete($sdkLink)
        Put $sdkLink 'public SDK helper.js'
    }

    $before = @(Get-ChildItem -LiteralPath $runtime -Recurse -File)
    $records = @(WriteRuntimeMetadata $runtime 'v1.0.0-pre.999')
    $manifest = [IO.File]::ReadAllText((Join-Path $runtime 'runtime-manifest.json')) | ConvertFrom-Json
    Check ($manifest.version -ceq 'v1.0.0-pre.999') 'The requested version was not recorded.'
    Check ($manifest.files.Count -eq $before.Count) 'The manifest does not cover exactly the original runtime.'
    Check ($records.Count -eq $before.Count + 2) 'The final inventory omitted metadata or duplicated a file.'
    $sums = @([IO.File]::ReadAllLines((Join-Path $runtime 'SHA256SUMS.txt')))
    Check ($sums.Count -eq $before.Count + 1) 'The sums must include the manifest and omit their own file.'
    foreach ($record in $records) {
        $path = Join-Path $runtime $record.path
        $digest = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant()
        Check ($record.sha256 -ceq $digest -and $record.size -eq (Get-Item -LiteralPath $path).Length) 'The final inventory has stale bytes.'
        if ($record.path -cne 'SHA256SUMS.txt') {
            Check ($sums -ccontains ($digest + '  ' + $record.path)) 'The sums omitted or misstated a runtime file.'
        }
    }
    foreach ($record in $manifest.files) {
        Check ($record.sha256 -ceq (Get-FileHash -LiteralPath (Join-Path $runtime $record.path) -Algorithm SHA256).Hash.ToLowerInvariant()) 'A manifest checksum differs from the file.'
    }
    Reject { WriteRuntimeMetadata $runtime 'v1.0.0-pre.999' } 'Existing metadata was silently overwritten.'

    $forbidden = Join-Path $runtime 'accounts.json'
    Put $forbidden '{}'
    Reject { Inventory $runtime } 'Forbidden account data was accepted by the final inventory.'
    [IO.File]::Delete($forbidden)

    # The optimized scan must still detect a payload change between the first
    # inventory and the final inventory, instead of describing mixed bytes.
    $changing = Join-Path $root 'changing-runtime'
    Put (Join-Path $changing 'mclauncher-qt.exe') 'before metadata'
    $originalNewText = (Get-Item Function:NewText).ScriptBlock
    try {
        function NewText([string]$Path, [string]$Value) {
            & $originalNewText $Path $Value
            if ([IO.Path]::GetFileName($Path) -ceq 'SHA256SUMS.txt') {
                Put (Join-Path (Split-Path -Parent $Path) 'mclauncher-qt.exe') 'changed after metadata'
            }
        }
        Reject { WriteRuntimeMetadata $changing 'v1.0.0-pre.999' } 'A runtime mutation while writing metadata was accepted.'
    } finally { Set-Item Function:NewText $originalNewText }
    Write-Output "PASS: $script:checks runtime origin and metadata checks."
} finally {
    Remove-Item -LiteralPath $root -Recurse -Force
}
