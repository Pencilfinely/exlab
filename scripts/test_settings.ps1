$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$testRoot = Join-Path $repoRoot ('.runtime/settings-tests-' + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $testRoot | Out-Null
$harness = @'
using System;
using System.Collections.Generic;
using System.IO;
using ExperimentManagerDesktop;
class SettingsTests {
    static int checks;
    static void Require(bool value,string reason){checks++;if(!value)throw new Exception(reason);}
    static void Invalid(Action action){bool failed=false;try{action();}catch(InvalidDataException){failed=true;}Require(failed,"Invalid settings selected an empty workspace.");}
    static int Main(string[] args){
        try{return Run(args);}catch(Exception error){while(error!=null){Console.WriteLine(error.GetType().FullName+": "+error.Message);error=error.InnerException;}return 1;}
    }
    static int Run(string[] args){
        string folder=args[0],path=Path.Combine(folder,"settings.json"),root=Path.Combine(folder,"Original Center");
        App.Worker=false;App.SettingsDirectory=folder;
        var settings=new Dictionary<string,object>{{"data_root",root},{"auto_update",true},{"hub_initialized",true},{"workspace_id","original"}};
        App.SaveSettings(settings);
        string retryPath=Path.Combine(folder,"replace-retry.json");App.Write(retryPath,new{generation=1});int replacements=0;
        App.Write(retryPath,new{generation=2},(temporary,destination)=>{
            if(++replacements==1)throw new IOException("Unable to remove the file to be replaced.",unchecked((int)0x80070497));
            File.Replace(temporary,destination,null);
        });
        Require(replacements==2&&App.Text(App.Read(retryPath),"generation")=="2","Busy destination error 1175 lost the pending settings write.");
        bool unsafeRetried=false;replacements=0;
        try{App.Write(retryPath,new{generation=3},(temporary,destination)=>{replacements++;throw new IOException("Replacement moved",unchecked((int)0x80070498));});}
        catch(IOException){unsafeRetried=true;}
        Require(unsafeRetried&&replacements==1&&App.Text(App.Read(retryPath),"generation")=="2","A partial replacement error was blindly retried.");
        Require(DesktopSettings.PhysicalPath(path).EndsWith(Path.GetFullPath(path),StringComparison.OrdinalIgnoreCase),"Settings diagnostics did not identify the opened physical file.");
        Require(DesktopSettings.Root(App.ReadSettings())==root,"Custom workspace did not survive reopening.");
        Require(App.Flag(App.ReadSettings(),"auto_update"),"Automation preference was lost.");
        File.Delete(path);
        Require(DesktopSettings.Root(App.ReadSettings())==root,"Missing settings ignored backup.");
        File.WriteAllText(path,"{interrupted write");
        Require(DesktopSettings.Root(App.ReadSettings())==root,"Corrupt settings ignored backup.");
        App.SaveSettings(App.ReadSettings());
        File.Delete(path);File.Delete(path+".bak");
        var recovered=App.ReadSettings();
        Require(DesktopSettings.Root(recovered)==root&&App.Text(recovered,"workspace_id")=="original", "Independent workspace binding was lost.");
        Require(App.Flag(recovered,"hub_initialized"),"Workspace identity guard was lost during recovery.");
        DesktopSettings.SelectRoot(recovered,Path.Combine(folder,"Second Center"));
        Require(!recovered.ContainsKey("workspace_id")&&!recovered.ContainsKey("hub_initialized"),"Explicitly selecting another workspace retained the old identity.");
        App.SaveSettings(recovered);
        Require(DesktopSettings.Root(App.ReadSettings())==Path.Combine(folder,"Second Center"),"Explicit selection was not persisted.");
        File.Delete(path);
        App.Write(path+".bak",settings);
        var latest=App.ReadSettings();
        Require(DesktopSettings.Root(latest)==Path.Combine(folder,"Second Center")&&!latest.ContainsKey("workspace_id")&&!latest.ContainsKey("hub_initialized"),"Latest binding inherited a different workspace identity from backup.");
        Require(DesktopRuntime.StartupArguments(root)=="--background --data-root "+App.Quote(root),"Login startup omitted the custom workspace.");
        App.Worker=true;
        Require(DesktopRuntime.StartupArguments("")=="--background","Worker startup inherited Center arguments.");
        App.Worker=false;
        File.Delete(Path.Combine(folder,"controller-root.json"));File.Delete(path+".bak");
        foreach(string invalid in new[]{"null","[]","{broken","{\"data_root\":\"\"}","{\"data_root\":\"relative\"}"}){
            File.WriteAllText(path,invalid);Invalid(()=>App.ReadSettings());
        }
        Console.WriteLine("PASS "+checks+" Center settings checks: restart, backups, missing/corrupt settings, workspace binding, explicit selection and login arguments.");
        return 0;
    }
}
'@
try {
    $source = Join-Path $testRoot 'SettingsTests.cs'
    [IO.File]::WriteAllText($source, $harness, (New-Object System.Text.UTF8Encoding($false)))
    $compiler = Join-Path $env:WINDIR 'Microsoft.NET/Framework64/v4.0.30319/csc.exe'
    $references = @('System.Windows.Forms.dll','System.Drawing.dll','System.Web.Extensions.dll','System.IO.Compression.dll','System.IO.Compression.FileSystem.dll','Microsoft.CSharp.dll','System.Management.dll') | ForEach-Object { '/r:' + $_ }
    $sources = @(Get-ChildItem -LiteralPath (Join-Path $repoRoot 'deploy/desktop') -Filter '*.cs' -File | ForEach-Object { $_.FullName })
    $executable = Join-Path $testRoot 'SettingsTests.exe'
    & $compiler /nologo /codepage:65001 /target:exe /main:SettingsTests "/out:$executable" @references @sources $source
    if ($LASTEXITCODE -ne 0) { throw 'Settings test compilation failed' }
    & $executable $testRoot
    if ($LASTEXITCODE -ne 0) { throw 'Settings tests failed' }
} finally {
    $resolved = [IO.Path]::GetFullPath($testRoot)
    if (-not $resolved.StartsWith((Join-Path $repoRoot '.runtime') + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) { throw 'Test cleanup path is outside this workspace' }
    if (Test-Path -LiteralPath $resolved) { Remove-Item -LiteralPath $resolved -Recurse -Force }
}
