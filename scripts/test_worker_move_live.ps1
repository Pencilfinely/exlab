param([string]$Python='D:\anaconda3\python.exe',[string]$BuilderDistribution='Ubuntu')
$ErrorActionPreference='Stop'
Set-StrictMode -Version Latest
if($PSVersionTable.PSEdition -ne 'Desktop'){throw 'Use Windows PowerShell for the .NET Framework fixture.'}
$repo=(Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$id=[Guid]::NewGuid().ToString('N')
$fixture=[IO.Path]::GetFullPath((Join-Path $repo ('.test-runs\wsl-move-'+$id)))
$source=[IO.Path]::GetFullPath((Join-Path ([IO.Path]::GetTempPath()) ('ExLab-MoveTest-'+$id)))
$target=Join-Path $fixture 'destination'
$name='ExLab-MoveTest-'+$id
if(-not $fixture.StartsWith($repo+'\.test-runs\',[StringComparison]::OrdinalIgnoreCase) -or (Test-Path -LiteralPath $source)){throw 'Unsafe fixture paths.'}
if((New-Object IO.DriveInfo([IO.Path]::GetPathRoot($source))).AvailableFreeSpace -lt 2GB){throw 'Scratch drive needs 2 GB free.'}
New-Item -ItemType Directory -Path $fixture | Out-Null
function Invoke-Wsl([string[]]$Arguments,[bool]$Linux=$false) {
    $info=New-Object Diagnostics.ProcessStartInfo
    $info.FileName='wsl.exe';$info.Arguments=($Arguments | ForEach-Object {if($_.Contains('"')){throw 'Unsupported fixture argument.'};if($_ -match '\s'){'"'+$_+'"'}else{$_}}) -join ' '
    $info.UseShellExecute=$false;$info.CreateNoWindow=$true;$info.WindowStyle='Hidden'
    $info.RedirectStandardOutput=$true;$info.RedirectStandardError=$true
    $info.StandardOutputEncoding=if($Linux){[Text.Encoding]::UTF8}else{[Text.Encoding]::Unicode}
    $p=[Diagnostics.Process]::Start($info);$err=$p.StandardError.ReadToEndAsync();$out=$p.StandardOutput.ReadToEnd();$p.WaitForExit();$code=$p.ExitCode;$p.Dispose()
    if($code -ne 0){throw ('Fixture WSL operation failed: '+$Arguments[0]+' / '+$code+' / '+$out)}
    return $out.Trim()
}
$running=Invoke-Wsl -Arguments @('--list','--running','--quiet')
if($running.Split("`n").Trim() -notcontains $BuilderDistribution){throw 'The read-only fixture builder must already be running.'}
$linuxRepo=Invoke-Wsl -Arguments @('-d',$BuilderDistribution,'--exec','wslpath','-a',$repo) -Linux $true
$linuxFixture=$linuxRepo+'/.test-runs/wsl-move-'+$id
Invoke-Wsl -Arguments @('-d',$BuilderDistribution,'--exec','python3',($linuxRepo+'/scripts/build_wsl_move_fixture.py'),'--output',($linuxFixture+'/rootfs.tar')) -Linux $true
$exe=Join-Path $fixture 'ExLabWorker.exe'
& $Python -m scripts.build_desktop --role worker --output $exe
if($LASTEXITCODE -ne 0){throw 'Fixture app build failed.'}
$assembly=[Reflection.Assembly]::LoadFrom($exe)
$flags=[Reflection.BindingFlags]'Static,NonPublic'
$diskMethod=$assembly.GetType('ExperimentManagerDesktop.WorkerDisk').GetMethod('RegisteredDisk',$flags)
Add-Type -TypeDefinition @'
using System;using System.IO;using System.Reflection;using System.Collections.Generic;
public static class ExLabLiveMove {
 public static void Run(Assembly a,string distro,string target,string journal) {
  var f=BindingFlags.Static|BindingFlags.NonPublic;var m=a.GetType("ExperimentManagerDesktop.WorkerMigration");var move=a.GetType("ExperimentManagerDesktop.WorkerDataMove");var disk=a.GetType("ExperimentManagerDesktop.WorkerDisk");
  a.GetType("ExperimentManagerDesktop.App").GetField("SettingsDirectory",f).SetValue(null,journal);
  Func<string,string> registered=d=>(string)disk.GetMethod("RegisteredDisk",f).Invoke(null,new object[]{d});
  Func<string,string> identity=d=>(string)m.GetMethod("DistributionId",f).Invoke(null,new object[]{d});
  Func<string[],string> run=args=>{try{return (string)m.GetMethod("Management",f).Invoke(null,new object[]{args});}catch(TargetInvocationException error){throw error.InnerException;}};
  var plan=move.GetMethod("Plan",f).Invoke(null,new object[]{distro,target,journal,0L});
  var job=new Dictionary<string,object>{{"source",distro},{"path",journal}};
  Action save=()=>File.WriteAllText(Path.Combine(journal,"journal.json"),new System.Web.Script.Serialization.JavaScriptSerializer().Serialize(job));
  move.GetMethod("Move",f).Invoke(null,new object[]{plan,job,save,run,registered,identity,null});
  if(!job.ContainsKey("data_moved")||!(bool)job["data_moved"])throw new Exception("Live move was not verified.");
 }
 public static void VerifyAfterBoot(Assembly a,string journal) {
  var f=BindingFlags.Static|BindingFlags.NonPublic;var m=a.GetType("ExperimentManagerDesktop.WorkerMigration");var move=a.GetType("ExperimentManagerDesktop.WorkerDataMove");var disk=a.GetType("ExperimentManagerDesktop.WorkerDisk");
  var job=new System.Web.Script.Serialization.JavaScriptSerializer().Deserialize<Dictionary<string,object>>(File.ReadAllText(Path.Combine(journal,"journal.json")));
  Func<string,string> registered=d=>(string)disk.GetMethod("RegisteredDisk",f).Invoke(null,new object[]{d});
  Func<string,string> identity=d=>(string)m.GetMethod("DistributionId",f).Invoke(null,new object[]{d});
  if(!(bool)move.GetMethod("VerifyLocation",f).Invoke(null,new object[]{job,registered,identity,null}))throw new Exception("Identity changed after boot.");
 }
}
'@ -ReferencedAssemblies System.Web.Extensions
$imported=$false
try {
    Invoke-Wsl -Arguments @('--import',$name,$source,(Join-Path $fixture 'rootfs.tar'),'--version','2');$imported=$true
    $before=Invoke-Wsl -Arguments @('-d',$name,'--exec','/bin/cat','/fixture/preserved.txt') -Linux $true
    $uidBefore=Invoke-Wsl -Arguments @('-d',$name,'--exec','/usr/bin/id','-u') -Linux $true
    # Re-import only this scratch disk with --vhd, without booting it. This
    # reproduces the detached state after the production idle-Desktop/WSL
    # shutdown, while leaving every pre-existing distribution running here.
    Invoke-Wsl -Arguments @('--terminate',$name)
    $snapshot=Join-Path $fixture 'snapshot.vhdx'
    $registered=[string]$diskMethod.Invoke($null,[object[]]@($name))
    if([IO.Path]::GetDirectoryName($registered).TrimEnd('\') -ne $source.TrimEnd('\')){throw 'Unexpected scratch source: refusing unregister.'}
    $inputDisk=[IO.File]::Open($registered,[IO.FileMode]::Open,[IO.FileAccess]::Read,([IO.FileShare]::ReadWrite -bor [IO.FileShare]::Delete))
    try {$outputDisk=[IO.File]::Open($snapshot,[IO.FileMode]::CreateNew,[IO.FileAccess]::Write,[IO.FileShare]::None);try {$inputDisk.CopyTo($outputDisk)}finally{$outputDisk.Dispose()}}finally{$inputDisk.Dispose()}
    Invoke-Wsl -Arguments @('--unregister',$name);$imported=$false
    Invoke-Wsl -Arguments @('--import',$name,$source,$snapshot,'--vhd');$imported=$true
    $journal=Join-Path $fixture 'journal';New-Item -ItemType Directory -Path $journal | Out-Null
    [ExLabLiveMove]::Run($assembly,$name,$target,$journal)
    $after=Invoke-Wsl -Arguments @('-d',$name,'--exec','/bin/cat','/fixture/preserved.txt') -Linux $true
    $uidAfter=Invoke-Wsl -Arguments @('-d',$name,'--exec','/usr/bin/id','-u') -Linux $true
    [ExLabLiveMove]::VerifyAfterBoot($assembly,$journal)
    if($before -ne $after -or $uidBefore -ne '1000' -or $uidAfter -ne $uidBefore){throw 'Fixture content or default user changed.'}
    if(Test-Path -LiteralPath (Join-Path $source 'ext4.vhdx')){throw 'The source disk still occupies the scratch drive.'}
    [IO.File]::WriteAllText((Join-Path $fixture 'verified.json'),('{"status":"passed","cross_volume":'+([IO.Path]::GetPathRoot($source) -ne [IO.Path]::GetPathRoot($target)).ToString().ToLowerInvariant()+',"default_uid":1000}'))
    Write-Output 'PASS real WSL move: default user, registration identity and file contents retained; original disk removed by WSL.'
} finally {
    if($imported) {
        $registered=[string]$diskMethod.Invoke($null,[object[]]@($name))
        $base=[IO.Path]::GetFullPath([IO.Path]::GetDirectoryName($registered)).TrimEnd('\')
        if($name -ne ('ExLab-MoveTest-'+$id) -or ($base -ne $source.TrimEnd('\') -and $base -ne $target.TrimEnd('\'))){throw 'Unexpected fixture registration: refusing cleanup.'}
        Invoke-Wsl -Arguments @('--unregister',$name)
    }
}
