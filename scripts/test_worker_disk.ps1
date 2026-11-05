$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
if ($PSVersionTable.PSEdition -ne 'Desktop') {
    & "$env:WINDIR\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File $PSCommandPath
    if ($LASTEXITCODE -ne 0) { throw 'Worker disk tests failed.' }
    exit 0
}
$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    if ($env:GITHUB_ACTIONS -eq 'true') { throw 'The disposable CI runner needs administrator rights for the VHD fixture.' }
    Write-Output 'SKIP VHD compaction fixture: administrator token unavailable.'
    exit 0
}
$repo = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$sandbox = [IO.Path]::GetFullPath((Join-Path $repo ('.test-runs\vhd-' + [Guid]::NewGuid().ToString('N'))))
if (-not $sandbox.StartsWith($repo + '\.test-runs\', [StringComparison]::OrdinalIgnoreCase)) { throw 'Fixture path escaped workspace.' }
New-Item -ItemType Directory -Path $sandbox | Out-Null
$exe = Join-Path $sandbox 'ExLabWorker.exe'
& python -m scripts.build_desktop --role worker --output $exe
if ($LASTEXITCODE -ne 0) { throw 'Worker fixture compilation failed.' }
$disk = Join-Path $sandbox 'fixture.vhdx'
if (Test-Path -LiteralPath $disk) { throw 'Fixture disk must not exist.' }
$commands = Join-Path $sandbox 'create.txt'
# No attach, partition, format or selection of physical disks is permitted.
[IO.File]::WriteAllText($commands, ('create vdisk file="' + $disk + '" maximum=64 type=expandable' + "`r`nexit`r`n"))
& diskpart /s $commands
if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $disk -PathType Leaf)) { throw 'Scratch VHD creation failed.' }
$assembly = [Reflection.Assembly]::LoadFrom($exe)
$identityMethod = $assembly.GetType('ExperimentManagerDesktop.WorkerDisk').GetMethod('Identifier', [Reflection.BindingFlags]'Static,NonPublic')
$method = $assembly.GetType('ExperimentManagerDesktop.WorkerDisk').GetMethod('CompactFile', [Reflection.BindingFlags]'Static,NonPublic')
Add-Type -TypeDefinition 'public static class ExLabVhdFixture { public static void Compact(System.Reflection.MethodInfo method,string path) { method.Invoke(null,new object[]{path}); } public static string Identity(System.Reflection.MethodInfo method,string path) { return (string)method.Invoke(null,new object[]{path}); } }'
$identityBefore = [ExLabVhdFixture]::Identity($identityMethod,[string]$disk)
[ExLabVhdFixture]::Compact($method, [string]$disk)
if (-not (Test-Path -LiteralPath $disk -PathType Leaf)) { throw 'Compaction removed its disk.' }
$copy=Join-Path $sandbox 'copied.vhdx';Copy-Item -LiteralPath $disk -Destination $copy
if($identityBefore -ne [ExLabVhdFixture]::Identity($identityMethod,[string]$copy)){throw 'Disk identity changed after compaction/copy.'}
Write-Output 'PASS native VHDX open/compact/close on a newly created, never-attached scratch disk.'
