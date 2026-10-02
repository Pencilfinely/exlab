param([string]$AssetDirectory, [switch]$Online)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$version = (Get-Content -LiteralPath (Join-Path $repoRoot 'VERSION') -Raw).Trim()
if (-not $AssetDirectory) { $AssetDirectory = Join-Path $repoRoot ('dist/' + $version + '/legacy-updater') }
$AssetDirectory = (Resolve-Path -LiteralPath $AssetDirectory).Path
$runtimeRoot = Join-Path $repoRoot '.runtime'
$testRoot = Join-Path $runtimeRoot ('legacy-update-tests-' + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $testRoot -Force | Out-Null
$compiler = Join-Path $env:WINDIR 'Microsoft.NET/Framework64/v4.0.30319/csc.exe'
$utf8 = New-Object System.Text.UTF8Encoding($false)
try {
    # Compile the updater actually shipped in 0.4.5, with no source substitutions.
    # Online mode downloads and self-tests installers; it never installs them or
    # stops any running Center, Worker, experiment, Docker or WSL environment.
    $legacySource = & git -c "safe.directory=$repoRoot" -C $repoRoot show 'v0.4.5:deploy/desktop/DesktopUpdates.cs'
    if ($LASTEXITCODE -ne 0) { throw 'The v0.4.5 tag is required for the legacy updater check.' }
    $sourcePath = Join-Path $testRoot 'LegacyDesktopUpdates.cs'
    [IO.File]::WriteAllText($sourcePath, ($legacySource -join "`n") + "`n", $utf8)
    $probePath = Join-Path $testRoot 'LegacyProbe.cs'
    [IO.File]::WriteAllText($probePath, @'
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Threading;
using System.Web.Script.Serialization;
using ExperimentManagerDesktop;

static class LegacyProbe {
    static void Require(bool condition, string message) { if(!condition)throw new Exception(message); }
    static int Main(string[] args) {
        try {
            string assets=args[0], version=args[2], root=args[3];
            bool online=args[1]=="online";
            var json=new JavaScriptSerializer();
            var files=new List<object>();
            string tag="v"+version;
            Require(UpdateService.Repository=="https://github.com/Pencilfinely/experiment-manager", "Legacy origin was modified.");
            Require(!UpdateService.AllowedRequestUri(new Uri("https://api.github.com/repos/Pencilfinely/exlab/releases?per_page=100"),true,false), "Legacy redirect policy was modified.");
            foreach(string file in Directory.GetFiles(assets))files.Add(new {
                name=Path.GetFileName(file), size=new FileInfo(file).Length, state="uploaded",
                browser_download_url=UpdateService.Repository+"/releases/download/"+tag+"/"+Path.GetFileName(file)
            });
            string metadata=json.Serialize(new[]{new {tag_name=tag,draft=false,prerelease=false,body="ExLab migration",assets=files}});
            var results=new List<object>();
            foreach(bool worker in new[]{false,true}) {
                string role=worker?"Worker":"Controller";
                UpdateRelease release=online?UpdateService.Check("0.4.5",worker):UpdateService.SelectRelease(metadata,"0.4.5",worker);
                Require(release!=null&&release.Version==version,role+": migration release missing.");
                string expectedHash=UpdateService.ParseChecksum(File.ReadAllText(Path.Combine(assets,"SHA256SUMS.txt")),release.AssetName);
                if(online)Require(release.Sha256==expectedHash,role+": published checksum differs from verified build.");
                else release.Sha256=expectedHash;
                string cache=Path.Combine(root,role);
                Directory.CreateDirectory(cache);
                string installer;
                if(online)installer=UpdateService.Download(release,cache,null,CancellationToken.None);
                else {
                    installer=Path.Combine(cache,release.AssetName);
                    using(var source=File.OpenRead(Path.Combine(assets,release.AssetName)))
                        UpdateService.SaveDownload(source,release,installer,null,CancellationToken.None);
                }
                UpdateService.ValidateDownloaded(release,installer);
                string report=Path.Combine(cache,"self-test.json");
                using(var process=Process.Start(new ProcessStartInfo(installer,"--self-test --report \""+report+"\""){
                    UseShellExecute=false,CreateNoWindow=true,WindowStyle=ProcessWindowStyle.Hidden})) {
                    Require(process.WaitForExit(60000)&&process.ExitCode==0,role+": installer self-test failed.");
                }
                var details=json.Deserialize<Dictionary<string,object>>(File.ReadAllText(report));
                Require(Convert.ToString(details["status"])=="passed"&&Convert.ToString(details["version"])==version&&Convert.ToString(details["role"])==role,role+": original client handoff checks failed.");
                results.Add(new {role=role,version=version,sha256=release.Sha256,source=release.AssetUrl,status="passed"});
                Console.WriteLine("PASS unchanged 0.4.5 updater: "+role+" "+(online?"live discovery/download":"offline selection/download")+", SHA-256, installer self-test and handoff metadata.");
            }
            File.WriteAllText(args[4],json.Serialize(results));
            return 0;
        } catch(Exception error){Console.Error.WriteLine(error);return 1;}
    }
}
'@, $utf8)
    $probeExe = Join-Path $testRoot 'LegacyProbe.exe'
    & $compiler /nologo /target:exe /langversion:5 "/out:$probeExe" /r:System.Web.Extensions.dll $sourcePath $probePath
    if ($LASTEXITCODE -ne 0) { throw 'Legacy updater probe compilation failed.' }
    $mode = if ($Online) { 'online' } else { 'offline' }
    $report = Join-Path $runtimeRoot ('legacy-updater-' + $mode + '.json')
    & $probeExe $AssetDirectory $mode $version $testRoot $report
    if ($LASTEXITCODE -ne 0) { throw 'Legacy updater compatibility check failed.' }
} finally {
    $resolved = [IO.Path]::GetFullPath($testRoot)
    if (-not $resolved.StartsWith($runtimeRoot + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Legacy test cleanup target is outside the workspace.'
    }
    if (Test-Path -LiteralPath $resolved) { Remove-Item -LiteralPath $resolved -Recurse -Force }
}
