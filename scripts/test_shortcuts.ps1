$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$testRoot = Join-Path $repoRoot ('.runtime/shortcut-tests-' + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $testRoot | Out-Null
$harness = @'
using System;
using System.IO;
using ExperimentManagerDesktop;
class ShortcutTests {
    static void Require(bool value,string reason){if(!value)throw new Exception(reason);}
    static int Main(string[] args){
        foreach(bool worker in new[]{false,true}){
            App.Worker=worker;
            string root=Path.Combine(args[0],App.Role),packages=Path.Combine(root,"packages"),desktop=Path.Combine(root,"desktop"),menu=Path.Combine(root,"menu");
            Directory.CreateDirectory(packages);Directory.CreateDirectory(desktop);Directory.CreateDirectory(menu);
            string oldTarget=Path.Combine(packages,"0.5.1",App.Executable),target=Path.Combine(packages,"0.5.2",App.Executable);
            string foreign=Path.Combine(root,"foreign",App.Executable);
            foreach(string file in new[]{oldTarget,target,foreign}){
                Directory.CreateDirectory(Path.GetDirectoryName(file));File.WriteAllBytes(file,new byte[0]);
            }
            Require(App.Title==(worker?"ExLab Worker":"ExLab Center"),"Wrong application name");
            Require(App.LegacyShortcutName==(worker?"\u5b9e\u9a8c\u7b97\u529b.lnk":"\u5b9e\u9a8c\u53f0.lnk"),"Legacy shortcut name lost its Unicode characters");
            Require(App.OwnsShortcutTarget(target,packages),"New executable should be owned");
            Require(App.OwnsShortcutTarget(Path.Combine(packages,"0.4.5","Experiment"+App.Role+".exe"),packages),"Original executable alias should be owned");
            Require(!App.OwnsShortcutTarget(foreign,packages),"Unrelated application must not be owned");
            Require(!App.OwnsShortcutTarget(Path.Combine(packages+"-other",App.Executable),packages),"Prefix lookalike must not be owned");
            Require(!App.OwnsShortcutTarget(Path.Combine(packages,worker?"ExLabCenter.exe":"ExLabWorker.exe"),packages),"Other role must not be owned");
            string legacyDesktop=Path.Combine(desktop,App.LegacyShortcutName),oldMenu=Path.Combine(menu,"ExLab",worker?"Worker.lnk":"Center.lnk"),originalMenu=Path.Combine(menu,"Experiment Manager",App.Role+".lnk");
            foreach(string link in new[]{legacyDesktop,oldMenu,originalMenu})App.Shortcut(link,oldTarget,"");
            App.InstallShortcuts(desktop,menu,packages,target,true);
            Require(File.Exists(Path.Combine(desktop,App.ShortcutName))&&File.Exists(Path.Combine(menu,"ExLab",App.ShortcutName)),"New branded shortcuts missing");
            foreach(string link in new[]{legacyDesktop,oldMenu,originalMenu})Require(!File.Exists(link),"Owned legacy shortcut remains");
            App.Shortcut(legacyDesktop,foreign,"");App.InstallShortcuts(desktop,menu,packages,target,true);
            Require(File.Exists(legacyDesktop)&&!App.OwnedShortcut(legacyDesktop,packages),"Unrelated legacy-named link was changed");
            string branded=Path.Combine(desktop,App.ShortcutName);App.Shortcut(branded,foreign,"");
            bool rejected=false;try{App.InstallShortcuts(desktop,menu,packages,target,true);}catch(IOException){rejected=true;}
            Require(rejected&&!App.OwnedShortcut(branded,packages),"Unrelated name collision was overwritten");
            Console.WriteLine("PASS "+App.Title+": migrate original and 0.5.1 shortcuts, preserve unrelated targets and reject name collisions.");
        }
        return 0;
    }
}
'@
try {
    $source = Join-Path $testRoot 'ShortcutTests.cs'
    [IO.File]::WriteAllText($source, $harness, (New-Object System.Text.UTF8Encoding($false)))
    $compiler = Join-Path $env:WINDIR 'Microsoft.NET/Framework64/v4.0.30319/csc.exe'
    $references = @('System.Windows.Forms.dll','System.Drawing.dll','System.Web.Extensions.dll','System.IO.Compression.dll','System.IO.Compression.FileSystem.dll','Microsoft.CSharp.dll','System.Management.dll') | ForEach-Object { '/r:' + $_ }
    $sources = @(Get-ChildItem -LiteralPath (Join-Path $repoRoot 'deploy/desktop') -Filter '*.cs' -File | ForEach-Object { $_.FullName })
    $executable = Join-Path $testRoot 'ShortcutTests.exe'
    & $compiler /nologo /codepage:65001 /target:exe /main:ShortcutTests "/out:$executable" @references @sources $source
    if ($LASTEXITCODE -ne 0) { throw 'Shortcut test compilation failed' }
    & $executable $testRoot
    if ($LASTEXITCODE -ne 0) { throw 'Shortcut migration tests failed' }
} finally {
    $resolved = [IO.Path]::GetFullPath($testRoot)
    if (-not $resolved.StartsWith((Join-Path $repoRoot '.runtime') + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) { throw 'Test cleanup path is outside this workspace' }
    if (Test-Path -LiteralPath $resolved) { Remove-Item -LiteralPath $resolved -Recurse -Force }
}
