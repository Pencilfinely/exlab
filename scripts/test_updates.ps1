param()

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

# The installer subprocess checks exercise .NET Framework, as shipped. Match
# the desktop test runner when CI invokes this script from PowerShell 7.
if ($PSVersionTable.PSEdition -ne 'Desktop') {
    $frameworkShell = Join-Path $env:WINDIR 'System32\WindowsPowerShell\v1.0\powershell.exe'
    & $frameworkShell -NoProfile -ExecutionPolicy Bypass -File $PSCommandPath
    if ($LASTEXITCODE -ne 0) { throw "Native update tests failed ($LASTEXITCODE)." }
    exit 0
}

# Compile against the same .NET Framework/C# version as the shipped desktop app.
# The harness exercises deterministic streams; it never contacts GitHub, creates
# UI, starts a worker, or executes an installer.
$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$runtimeRoot = [IO.Path]::GetFullPath((Join-Path $repoRoot '.runtime'))
$testRoot = Join-Path $runtimeRoot ('update-tests-' + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $testRoot -Force | Out-Null
$compiler = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
if (-not (Test-Path -LiteralPath $compiler -PathType Leaf)) {
    $compiler = Join-Path $env:WINDIR 'Microsoft.NET\Framework\v4.0.30319\csc.exe'
}
$harness = @'
using System;
using System.Collections.Generic;
using System.IO;
using System.Security.Cryptography;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using System.Web.Script.Serialization;
using ExperimentManagerDesktop;

static class UpdateTests {
    static int assertions;
    static void Assert(bool condition, string message) {
        assertions++;
        if(!condition) throw new Exception(message);
    }
    static void Fails<T>(Action action, string message) where T : Exception {
        assertions++;
        try { action(); } catch(T) { return; }
        throw new Exception(message);
    }
    static string Hash(byte[] bytes) {
        using(var sha = SHA256.Create()) return BitConverter.ToString(sha.ComputeHash(bytes)).Replace("-", "").ToLowerInvariant();
    }
    static Dictionary<string,object> Asset(string tag, string name, long size) {
        return new Dictionary<string,object> { { "name", name }, { "size", size }, { "state", "uploaded" },
            { "browser_download_url", UpdateService.Repository + "/releases/download/" + tag + "/" + name } };
    }
    static Dictionary<string,object> Release(string version, bool draft, bool preview, string role = "controller") {
        string tag = "v" + version;
        string name = "ExperimentManager-" + version + "-windows-" + role + "-x64-Setup.exe";
        return new Dictionary<string,object> { { "tag_name", tag }, { "draft", draft }, { "prerelease", preview },
            { "body", "Release notes" }, { "assets", new object[] { Asset(tag, name, 1024), Asset(tag, "SHA256SUMS.txt", 100) } } };
    }
    static string Json(params object[] releases) { return new JavaScriptSerializer().Serialize(releases); }
    static UpdateRelease FileRelease(byte[] bytes) {
        string version = "0.3.0-rc.2", tag = "v" + version;
        string name = "ExperimentManager-" + version + "-windows-controller-x64-Setup.exe";
        string download = UpdateService.Repository + "/releases/download/" + tag + "/";
        return new UpdateRelease { Version = version, Tag = tag, AssetName = name,
            AssetUrl = download + name, ChecksumUrl = download + "SHA256SUMS.txt", Size = bytes.Length, Sha256 = Hash(bytes) };
    }
    static void NoPartials(string root) { Assert(Directory.GetFiles(root, "*.part").Length == 0, "A partial download was left behind."); }
    static async Task QueueTests(string root) {
        var settings=new Dictionary<string,object>();
        Assert(!DesktopLifecycle.ShouldEnableCompute(settings,true,false),"Startup enabled compute without opting in.");
        settings["auto_enable_compute"]=true;
        Assert(DesktopLifecycle.ShouldEnableCompute(settings,true,false),"Configured startup did not enable compute.");
        Assert(!DesktopLifecycle.ShouldEnableCompute(settings,false,false),"Center tried to start worker compute.");
        settings["auto_enable_compute"]="true";
        Assert(!DesktopLifecycle.ShouldEnableCompute(settings,true,false),"Malformed startup flag enabled compute.");
        Assert(DesktopLifecycle.ShouldEnableCompute(settings,true,true),"Update handoff lost the running worker's restart choice.");
        var release=FileRelease(new byte[]{(byte)'M',(byte)'Z',1,2,3});
        var now=new DateTime(2026,10,7,0,0,0,DateTimeKind.Utc);
        string persisted="";
        var json=new JavaScriptSerializer();
        Action<UpdateQueueState> save=state=>persisted=json.Serialize(state);
        var queue=new DesktopUpdateQueue(null,"0.3.0-rc.1",save);
        int downloads=0,probes=0,installs=0,changes=0;
        queue.Changed=()=>changes++;
        Func<UpdateRelease,Action<long,long>,CancellationToken,Task<string>> download=(item,progress,token)=>{
            downloads++;return Task.FromResult(Path.Combine(root,item.AssetName));
        };
        bool ready=false;
        Func<Task<Dictionary<string,object>>> inspect=()=>{
            probes++;return Task.FromResult(new Dictionary<string,object>{{"ready_for_update",ready},{"detail","Experiment still running"}});
        };
        Func<UpdateRelease,string,Task> install=(item,path)=>{
            installs++;Assert(!queue.CanCancel,"Installation handoff remained cancelable.");return Task.FromResult(0);
        };
        queue.Enqueue(release,true,true);
        queue.Enqueue(release,true,true);
        await queue.Tick(now,true,download,inspect,install);
        Assert(downloads==1&&probes==0&&installs==0,"Busy client failed to download independently or stopped work.");
        Assert(queue.State.Stage=="waiting"&&queue.Active,"Downloaded installation request was lost while busy.");
        await queue.Tick(now.AddSeconds(6),false,download,inspect,install);
        Assert(probes==1&&installs==0&&downloads==1,"Readiness wait repeated the download or installed while unsafe.");
        await queue.Tick(now.AddSeconds(7),false,download,inspect,install);
        Assert(probes==1,"Wait ignored its scheduled retry time.");
        var restored=json.Deserialize<UpdateQueueState>(persisted);
        queue=new DesktopUpdateQueue(restored,"0.3.0-rc.1",save);
        ready=true;
        await queue.Tick(now.AddSeconds(12),false,download,inspect,install);
        Assert(installs==1&&downloads==1&&queue.State.Stage=="handoff","Restart lost a waiting request or duplicated download.");
        await queue.Tick(now.AddMinutes(1),false,download,inspect,install);
        Assert(installs==1,"Installer was started repeatedly after handoff.");
        var updated=new DesktopUpdateQueue(json.Deserialize<UpdateQueueState>(persisted),release.Version,save);
        Assert(!updated.Active&&updated.State.Stage=="completed","Installed update was queued again on startup.");
        Assert(changes>0,"Queue changes never reached the UI observer.");

        queue=new DesktopUpdateQueue(null,"0.3.0-rc.1",save);
        int failedDownloads=0;
        Func<UpdateRelease,Action<long,long>,CancellationToken,Task<string>> unreliable=(item,progress,token)=>{
            failedDownloads++;if(failedDownloads==1)throw new IOException("Offline");return download(item,progress,token);
        };
        queue.Enqueue(release,false,false);
        await queue.Tick(now,false,unreliable,inspect,install);
        Assert(queue.State.Stage=="retry"&&queue.State.Failures==1,"Download error did not remain queued.");
        await queue.Tick(now.AddSeconds(1),false,unreliable,inspect,install);
        Assert(failedDownloads==1,"Download retry ignored backoff.");
        await queue.Tick(now.AddSeconds(31),false,unreliable,inspect,install);
        Assert(queue.State.Stage=="downloaded"&&installs==1,"Download-only request installed without consent.");
        queue.Enqueue(release,true,false);
        queue.Enqueue(release,true,true);
        Assert(!queue.State.Automatic,"Enabling automatic updates took ownership of a manual request.");
        int beforeDownload=downloads;
        await queue.Tick(now.AddMinutes(2),false,download,inspect,(item,path)=>{throw new IOException("Installer cannot start");});
        Assert(queue.State.Stage=="retry"&&queue.State.Downloaded!=null&&downloads==beforeDownload,"Failed install lost verified download.");

        queue=new DesktopUpdateQueue(null,"0.3.0-rc.1",save);
        var blocked=new TaskCompletionSource<string>();
        int activeDownloads=0;
        Func<UpdateRelease,Action<long,long>,CancellationToken,Task<string>> pending=(item,progress,token)=>{
            activeDownloads++;token.Register(()=>blocked.TrySetCanceled());return blocked.Task;
        };
        queue.Enqueue(release,true,false);
        var running=queue.Tick(now,false,pending,inspect,install);
        await queue.Tick(now,false,pending,inspect,install);
        Assert(activeDownloads==1,"Concurrent polling started duplicate downloads.");
        queue.Cancel();await running;
        Assert(!queue.Active&&!queue.InFlight&&queue.State.Stage=="idle","Cancellation resurrected the request.");
        Assert(json.Deserialize<UpdateQueueState>(persisted).Release==null,"Canceled request persisted as active.");
        var halfDownloaded=new UpdateQueueState{Release=release,Stage="downloading",InstallRequested=true};
        Assert(new DesktopUpdateQueue(halfDownloaded,"0.3.0-rc.1",save).State.Stage=="queued","Interrupted download did not recover.");

        queue=new DesktopUpdateQueue(new UpdateQueueState{Release=release,Stage="waiting",InstallRequested=true,
            Downloaded=Path.Combine(root,release.AssetName)},"0.3.0-rc.1",save);
        beforeDownload=downloads;ready=false;
        await queue.Tick(now,false,download,inspect,install,(item,path)=>{throw new InvalidDataException("Cached download changed");});
        Assert(downloads==beforeDownload+1&&queue.State.Stage=="waiting","Invalid restored cache was not downloaded again.");
        int handoffs=0;
        queue=new DesktopUpdateQueue(null,"0.3.0-rc.1",state=>{
            if(state.Stage=="handoff")throw new IOException("Disk full after installer started");save(state);
        });
        queue.Enqueue(release,true,false);ready=true;
        await queue.Tick(now,false,download,inspect,(item,path)=>{handoffs++;return Task.FromResult(0);});
        Assert(handoffs==1&&queue.State.Stage=="handoff","Receipt write failure stranded a launched installer.");
        await queue.Tick(now.AddMinutes(1),false,download,inspect,install);
        Assert(handoffs==1,"Handoff receipt error launched the installer twice.");

        string installer=Path.Combine(root,release.AssetName),other=Path.Combine(root,"other-setup.exe");
        byte[] bytes=new byte[]{(byte)'M',(byte)'Z',1,2,3};
        File.WriteAllBytes(installer,bytes);File.WriteAllBytes(other,bytes);
        var cleanup=InstallerCleanup.Record(installer,release.Version,false,123,456);
        string app=Path.Combine(root,"ExLabCenter.exe");
        Assert(!InstallerCleanup.TryDelete(cleanup,app,(pid,started)=>true)&&File.Exists(installer),"Running installer was deleted.");
        Assert(InstallerCleanup.TryDelete(cleanup,app,(pid,started)=>false)&&!File.Exists(installer),"Finished installer was not deleted.");
        Assert(File.Exists(other),"Cleanup removed an unrelated package.");
        Assert(InstallerCleanup.TryDelete(cleanup,app,(pid,started)=>false),"Cleanup was not idempotent.");
        File.WriteAllBytes(installer,bytes);cleanup=InstallerCleanup.Record(installer,release.Version,false,123,456);
        File.WriteAllBytes(installer,new byte[]{(byte)'M',(byte)'Z',9,2,3});
        Fails<InvalidDataException>(()=>InstallerCleanup.TryDelete(cleanup,app,(pid,started)=>false),"Changed installer was silently deleted.");
        Assert(File.Exists(installer),"Changed installer did not remain for inspection.");
        Fails<InvalidDataException>(()=>InstallerCleanup.TryDelete(cleanup,installer,(pid,started)=>false),"Cleanup deleted the running application.");
        cleanup.Path=other;
        Fails<InvalidDataException>(()=>InstallerCleanup.TryDelete(cleanup,app,(pid,started)=>false),"Unrelated executable was accepted for cleanup.");
        File.Delete(installer);File.Delete(other);
    }
    static int Main(string[] args) {
        try { Run(args[0]); Console.WriteLine("PASS update service: " + assertions + " assertions; semantic versions, channels, exact role assets, HTTPS origins, checksum, truncation, cache finalization and cancellation."); return 0; }
        catch(Exception ex) { Console.Error.WriteLine(ex); return 1; }
    }
    static void Run(string root) {
        QueueTests(root).GetAwaiter().GetResult();
        Assert(DesktopLifecycle.CanExitUnconfiguredWorker("",new string[0]),"Fresh worker without WSL cannot exit.");
        Assert(DesktopLifecycle.CanExitUnconfiguredWorker("",new[]{"docker-desktop","docker-desktop-data"}),"Docker Desktop alone was mistaken for a configured worker.");
        Assert(!DesktopLifecycle.CanExitUnconfiguredWorker("Ubuntu",new string[0]),"Configured worker skipped backend shutdown.");
        Assert(!DesktopLifecycle.CanExitUnconfiguredWorker("",new[]{"Ubuntu"}),"Existing WSL environment bypassed shutdown checks.");
        Assert(!DesktopLifecycle.CanExitUnconfiguredWorker("",null),"Unknown WSL inventory permitted unchecked exit.");
        Assert(!DesktopLifecycle.CanExitUnconfiguredWorker("",new[]{""}),"Malformed WSL registration permitted unchecked exit.");
        Assert(UpdateService.BackendMatchesRelease("0.3.0rc4","0.3.0-rc.4"),"Python RC version never confirmed new backend startup.");
        Assert(UpdateService.BackendMatchesRelease("0.3.0-rc.4","v0.3.0-rc.4"),"SemVer backend spelling was rejected.");
        Assert(!UpdateService.BackendMatchesRelease("0.3.0rc1","0.3.0-rc.4"),"Old backend was reported as upgraded.");
        Assert(!UpdateService.BackendMatchesRelease("unknown","0.3.0-rc.4"),"Unknown backend was reported as upgraded.");
        Assert(!UpdateService.BackendMatchesRelease(null,"0.3.0-rc.4"),"Missing backend version was reported as upgraded.");
        DesktopLifecycle.RequireInstallReady(new Dictionary<string,object>{{"running",false},{"ready_for_install",true}},false);
        assertions++;
        Fails<InvalidOperationException>(() => DesktopLifecycle.RequireInstallReady(
            new Dictionary<string,object>{{"running",true},{"ready_for_install",true}},false),
            "Manual installer accepted an idle but still running legacy backend.");
        Fails<InvalidOperationException>(() => DesktopLifecycle.RequireInstallReady(
            new Dictionary<string,object>{{"running",false},{"ready_for_install",false}},true),
            "Worker installation ignored an unsafe local execution state.");
        Fails<InvalidOperationException>(() => DesktopLifecycle.RequireInstallReady(
            new Dictionary<string,object>{{"ready_for_install",true}},false),
            "Manual installer inferred that an unknown backend was stopped.");
        var statuses = new Queue<Dictionary<string,object>>(new[] {
            new Dictionary<string,object>{{"running",true},{"status","unresponsive"}},
            new Dictionary<string,object>{{"running",false},{"status","shutting_down"}},
            new Dictionary<string,object>{{"running",false},{"status","stopped"}}
        });
        int observed = 0, delays = 0;
        DesktopLifecycle.WaitForStopped(() => Task.FromResult(statuses.Dequeue()), state => observed++, null,
            () => { delays++; return Task.FromResult(0); }).GetAwaiter().GetResult();
        Assert(observed == 3 && delays == 2, "Exit completed while service was still running or shutting down.");
        Fails<InvalidDataException>(() => DesktopLifecycle.WaitForStopped(
            () => Task.FromResult(new Dictionary<string,object>{{"status","stopped"}}), null, null).GetAwaiter().GetResult(),
            "Missing running flag was treated as a stopped backend.");
        Fails<IOException>(() => DesktopLifecycle.WaitForStopped(
            () => Task.FromResult(new Dictionary<string,object>{{"running",true},{"status","exit_failed"},{"detail","Checkpoint failed"}}),
            null, null).GetAwaiter().GetResult(), "Failed checkpoint exit was ignored.");
        Fails<IOException>(() => DesktopLifecycle.WaitForStopped(
            () => Task.FromResult(new Dictionary<string,object>{{"running",false},{"status","unknown"}}),
            null, TimeSpan.Zero).GetAwaiter().GetResult(), "Unknown status permitted update handoff.");
        Fails<IOException>(() => DesktopLifecycle.WaitForStopped(
            () => Task.FromResult(new Dictionary<string,object>{{"running",true},{"status","stopping"}}),
            null, TimeSpan.Zero).GetAwaiter().GetResult(), "Stop timeout was silently accepted.");
        Fails<IOException>(() => DesktopLifecycle.WaitForStopped(
            () => Task.FromResult(new Dictionary<string,object>{{"running",false},{"status","failed"}}),
            null, null, finalStatus:"stopped").GetAwaiter().GetResult(),
            "Worker crash was treated as completed experiment shutdown.");
        Assert(UpdateService.CompareVersions("0.3.0-rc.10", "0.3.0-rc.2") > 0, "RC versions must compare numerically.");
        Assert(UpdateService.CompareVersions("v0.3.0", "0.3.0-rc.99") > 0, "Stable must follow prerelease.");
        Assert(UpdateService.CompareVersions("1.0.0+build.2", "1.0.0+build.1") == 0, "Build metadata must not change precedence.");
        Assert(UpdateService.CompareVersions("0.10.0", "0.9.99") > 0, "Version components must compare numerically.");
        Assert(UpdateService.CompareVersions("1.0.0-alpha.1", "1.0.0-alpha.beta") < 0, "Numeric prerelease identifiers must come first.");
        Assert(UpdateService.CompareVersions("1.0.0-alpha", "1.0.0-alpha.1") < 0, "Shorter matching prerelease must come first.");
        Assert(UpdateService.CompareVersions("99999999999999999999999999.0.0", "9999999999999999999999999.0.0") > 0, "Large numeric components must not overflow.");
        Fails<FormatException>(() => UpdateService.CompareVersions("1.0.0-rc.01", "1.0.0"), "Numeric leading zero accepted.");
        Fails<FormatException>(() => UpdateService.CompareVersions("01.0.0", "1.0.0"), "Core leading zero accepted.");
        Fails<FormatException>(() => UpdateService.CompareVersions("../1.0.0", "1.0.0"), "Unsafe version accepted.");

        var rc2 = Release("0.3.0-rc.2", false, true);
        var rc10 = Release("0.3.0-rc.10", false, true);
        var stable = Release("0.3.0", false, false);
        var future = Release("2.0.0-rc.1", false, true);
        var draft = Release("9.0.0", true, false);
        var selected = UpdateService.SelectRelease(Json(rc10, draft, rc2), "0.3.0-rc.1", false);
        Assert(selected.Version == "0.3.0-rc.10", "API order or draft affected maximum version.");
        Assert(UpdateService.SelectRelease(Json(rc2, stable), "0.3.0-rc.1", false).Version == "0.3.0", "Preview cannot advance to stable.");
        Assert(UpdateService.SelectRelease(Json(future, stable, draft), "0.2.0", false).Version == "0.3.0", "Stable selected a preview or draft.");
        Assert(UpdateService.SelectRelease(Json(Release("3.0.0", false, true), stable), "0.2.0", false).Version == "0.3.0", "GitHub prerelease flag was ignored.");
        Assert(UpdateService.SelectRelease(Json(Release("3.0.0-rc.1", false, false), stable), "0.2.0", false).Version == "0.3.0", "SemVer prerelease flag was ignored.");
        Assert(UpdateService.SelectRelease(Json(stable, rc10), "0.3.0", false) == null, "Current version offered downgrade/reinstall.");
        Assert(UpdateService.SelectRelease(Json(rc2), "0.3.0-rc.1", true) == null, "Worker selected Controller installer.");
        Assert(UpdateService.SelectRelease(Json(Release("0.3.0-rc.2", false, true, "worker")), "0.3.0-rc.1", true).AssetName.EndsWith("worker-x64-Setup.exe"), "Worker asset missing.");
        var missingChecksum = Release("0.4.0", false, false);
        missingChecksum["assets"] = new object[] { ((object[])missingChecksum["assets"])[0] };
        Assert(UpdateService.SelectRelease(Json(missingChecksum), "0.3.0", false) == null, "Unchecked release offered.");
        Fails<InvalidDataException>(() => UpdateService.SelectRelease("{}", "0.3.0", false), "Invalid release list accepted.");
        var wrongRepo = Release("0.4.0", false, false);
        ((Dictionary<string,object>)((object[])wrongRepo["assets"])[0])["browser_download_url"] = "https://github.com/other/repository/releases/download/v0.4.0/test.exe";
        Fails<InvalidDataException>(() => UpdateService.SelectRelease(Json(wrongRepo), "0.3.0", false), "Other repository accepted.");

        string tag = "v0.3.0-rc.2", name = "ExperimentManager-0.3.0-rc.2-windows-controller-x64-Setup.exe";
        string url = UpdateService.Repository + "/releases/download/" + tag + "/" + name;
        UpdateService.ValidateAssetUri(url, tag, name);
        Assert(UpdateService.AllowedRequestUri(new Uri(url), false, true), "Renamed repository initial download rejected.");
        Assert(UpdateService.AllowedRequestUri(new Uri(UpdateService.LegacyRepository + "/releases/download/" + tag + "/" + name), false, false), "Legacy repository download rejected.");
        Assert(!UpdateService.AllowedRequestUri(new Uri(url + "?redirect=1"), false, true), "Query accepted at repository download origin.");
        var renamed = Release("0.5.0", false, false);
        var renamedAsset = (Dictionary<string,object>)((object[])renamed["assets"])[0];
        renamedAsset["name"] = "ExLab-0.5.0-windows-controller-x64-Setup.exe";
        renamedAsset["browser_download_url"] = UpdateService.Repository + "/releases/download/v0.5.0/" + renamedAsset["name"];
        Assert(UpdateService.SelectRelease(Json(renamed), "0.4.5", false).AssetName == (string)renamedAsset["name"], "Renamed installer not selected.");
        Fails<InvalidDataException>(() => UpdateService.ValidateAssetUri(url.Replace("https:", "http:"), tag, name), "HTTP accepted.");
        Fails<InvalidDataException>(() => UpdateService.ValidateAssetUri(url + "?redirect=1", tag, name), "Query accepted in initial download.");
        Fails<InvalidDataException>(() => UpdateService.ValidateAssetUri(url.Replace("github.com", "github.com.evil.invalid"), tag, name), "Lookalike domain accepted.");
        Fails<InvalidDataException>(() => UpdateService.ValidateAssetUri(url.Replace(tag, "v0.3.0-rc.1"), tag, name), "Different release accepted.");
        Assert(UpdateService.AllowedRequestUri(new Uri("https://release-assets.githubusercontent.com/asset?sig=1"), false, false), "GitHub asset redirect rejected.");
        Assert(!UpdateService.AllowedRequestUri(new Uri("https://release-assets.githubusercontent.com/asset"), false, true), "Direct CDN origin accepted.");
        Assert(!UpdateService.AllowedRequestUri(new Uri("https://evil.githubusercontent.com/asset"), false, false), "Arbitrary GitHub subdomain accepted.");
        Assert(!UpdateService.AllowedRequestUri(new Uri("https://github.com/other/repo/releases/download/v1/a.exe"), false, false), "Cross-repository redirect accepted.");
        Assert(!UpdateService.AllowedRequestUri(new Uri("http://release-assets.githubusercontent.com/asset"), false, false), "HTTP redirect accepted.");
        Assert(!UpdateService.AllowedRequestUri(new Uri("https://user@release-assets.githubusercontent.com/asset"), false, false), "Userinfo accepted.");
        Assert(!UpdateService.AllowedRequestUri(new Uri("https://release-assets.githubusercontent.com:444/asset"), false, false), "Unexpected port accepted.");
        Assert(UpdateService.AllowedRequestUri(new Uri(UpdateService.ReleasesApi), true, true), "Public API rejected.");
        Assert(!UpdateService.AllowedRequestUri(new Uri("https://api.github.com/repos/other/repo/releases?per_page=100"), true, false), "Other API repository accepted.");

        byte[] bytes = new byte[300000]; bytes[0] = (byte)'M'; bytes[1] = (byte)'Z';
        for(int i = 2; i < bytes.Length; i++) bytes[i] = (byte)(i % 251);
        string hash = Hash(bytes);
        Assert(UpdateService.ParseChecksum(hash.ToUpperInvariant() + "  " + name + "\n", name) == hash, "Text checksum not accepted.");
        Assert(UpdateService.ParseChecksum("\uFEFF" + hash + " *" + name + "\r\n", name) == hash, "Binary/BOM checksum not accepted.");
        Fails<InvalidDataException>(() => UpdateService.ParseChecksum(hash + "  other.exe", name), "Different file checksum accepted.");
        Fails<InvalidDataException>(() => UpdateService.ParseChecksum(hash + "  " + name + "\n" + hash + "  " + name, name), "Duplicate checksum accepted.");
        Fails<InvalidDataException>(() => UpdateService.ParseChecksum("garbage  " + name, name), "Invalid checksum accepted.");

        string target = Path.Combine(root, "installer.exe");
        var file = FileRelease(bytes);
        long received = 0, announced = 0;
        using(var input = new MemoryStream(bytes)) {
            Assert(UpdateService.SaveDownload(input, file, target, (done, total) => { received = done; announced = total; }, CancellationToken.None) == target, "Wrong final path.");
        }
        Assert(received == bytes.Length && announced == bytes.Length, "Progress is incorrect.");
        UpdateService.ValidateFile(file, target, CancellationToken.None);
        Assert(Hash(File.ReadAllBytes(target)) == hash, "Final download differs from source.");
        NoPartials(root);
        string cached = Path.Combine(root, file.AssetName);
        File.WriteAllBytes(cached, bytes);
        Assert(UpdateService.Download(file, root, null, CancellationToken.None) == cached, "Valid cache was not reused without network.");
        UpdateService.ValidateDownloaded(file, cached);
        var altered = (byte[])bytes.Clone(); altered[20] ^= 1;
        File.WriteAllBytes(cached, altered);
        Fails<InvalidDataException>(() => UpdateService.ValidateDownloaded(file, cached), "Installer tampering after download was ignored.");
        File.Delete(cached);
        File.WriteAllText(target, "old cache");
        using(var input = new MemoryStream(bytes)) UpdateService.SaveDownload(input, file, target, null, CancellationToken.None);
        UpdateService.ValidateFile(file, target, CancellationToken.None);
        NoPartials(root);

        var badHash = FileRelease(bytes); badHash.Sha256 = new string('0', 64);
        Fails<InvalidDataException>(() => UpdateService.ValidateFile(badHash, target, CancellationToken.None), "Bad hash accepted.");
        using(var input = new MemoryStream(bytes)) Fails<InvalidDataException>(() => UpdateService.SaveDownload(input, badHash, target, null, CancellationToken.None), "Bad hash finalized.");
        Assert(Hash(File.ReadAllBytes(target)) == hash, "Bad download replaced good cache.");
        NoPartials(root);
        using(var input = new MemoryStream(new byte[] { (byte)'M', (byte)'Z' }))
            Fails<InvalidDataException>(() => UpdateService.SaveDownload(input, file, target, null, CancellationToken.None), "Truncated installer accepted.");
        NoPartials(root);
        var undersized = FileRelease(bytes); undersized.Size = bytes.Length - 1;
        using(var input = new MemoryStream(bytes)) Fails<InvalidDataException>(() => UpdateService.SaveDownload(input, undersized, target, null, CancellationToken.None), "Oversized installer accepted.");
        NoPartials(root);
        byte[] invalidExe = Encoding.UTF8.GetBytes("This is an HTML error.");
        using(var input = new MemoryStream(invalidExe)) Fails<InvalidDataException>(() => UpdateService.SaveDownload(input, FileRelease(invalidExe), target, null, CancellationToken.None), "Non-EXE finalized.");
        NoPartials(root);

        using(var cancel = new CancellationTokenSource()) {
            cancel.Cancel();
            Fails<OperationCanceledException>(() => UpdateService.Download(file, root, null, cancel.Token), "Download contacted network after cancellation.");
            using(var input = new MemoryStream(bytes)) Fails<OperationCanceledException>(() => UpdateService.SaveDownload(input, file, target, null, cancel.Token), "Initial cancellation ignored.");
        }
        NoPartials(root);
        using(var cancel = new CancellationTokenSource()) {
            using(var input = new MemoryStream(bytes)) Fails<OperationCanceledException>(() => UpdateService.SaveDownload(input, file, target, (done, total) => cancel.Cancel(), cancel.Token), "Midstream cancellation ignored.");
        }
        NoPartials(root);
        Assert(Hash(File.ReadAllBytes(target)) == hash, "Canceled download replaced good cache.");
        using(var input = new MemoryStream(bytes)) Fails<IOException>(() => UpdateService.SaveDownload(input, file, target, (done, total) => { throw new IOException("Simulated disconnect"); }, CancellationToken.None), "Disconnect unexpectedly succeeded.");
        NoPartials(root);
        Assert(Hash(File.ReadAllBytes(target)) == hash, "Failed download replaced good cache.");
    }
}
'@

try {
    $testSource = Join-Path $testRoot 'UpdateTests.cs'
    $testExe = Join-Path $testRoot 'UpdateTests.exe'
    [IO.File]::WriteAllText($testSource, $harness, (New-Object Text.UTF8Encoding $false))
    & $compiler /nologo /target:exe /optimize+ /langversion:5 /r:System.Web.Extensions.dll ("/out:" + $testExe) (Join-Path $repoRoot 'deploy/desktop/DesktopUpdates.cs') (Join-Path $repoRoot 'deploy/desktop/DesktopUpdateQueue.cs') $testSource
    if ($LASTEXITCODE -ne 0) { throw 'Update service compilation failed.' }
    & $testExe $testRoot
    if ($LASTEXITCODE -ne 0) { throw 'Update service tests failed.' }

    # Exercise the installer's real subprocess boundary with an isolated status
    # executable. No application settings, service, browser or installer runs.
    $probeRuntime = Join-Path $testRoot 'runtime'
    New-Item -ItemType Directory -Path $probeRuntime | Out-Null
    $fakePythonSource = Join-Path $testRoot 'StatusFixture.cs'
    @'
using System;
using System.IO;
using System.Threading;
static class StatusFixture {
    static int Main(string[] args) {
        if(args.Length == 2 && args[0] == "--hold") {
            var limit=DateTime.UtcNow.AddSeconds(10);
            while(!File.Exists(args[1])&&DateTime.UtcNow<limit)Thread.Sleep(20);
            return File.Exists(args[1])?0:3;
        }
        if(args.Length != 5 || args[0] != "-m" || args[1] != "expman.desktop" ||
            args[2] != "controller-install-status" || args[3] != "--root")return 2;
        Console.Write(File.ReadAllText(Path.Combine(args[4],"status.json")));
        return 0;
    }
}
'@ | Set-Content -LiteralPath $fakePythonSource -Encoding UTF8
    & $compiler /nologo /target:exe ("/out:" + (Join-Path $probeRuntime 'python.exe')) $fakePythonSource
    if ($LASTEXITCODE -ne 0) { throw 'Isolated status fixture compilation failed.' }
    $probeSource = Join-Path $testRoot 'InstallProbeTests.cs'
    @'
using System;
using System.IO;
using System.Collections.Generic;
using System.Diagnostics;
using System.Reflection;
using System.Reflection.Emit;
using System.Threading;
using System.Threading.Tasks;
using ExperimentManagerDesktop;
static class InstallProbeTests {
    static int assertions;
    static void Assert(bool condition,string message) {assertions++;if(!condition)throw new Exception(message);}
    static void Reject(Action action,string message) {
        assertions++;try{action();}catch(InvalidOperationException){return;}catch(ArgumentException){return;}catch(NullReferenceException){return;}
        throw new Exception(message);
    }
    static Dictionary<string,object> Stopped() {
        return new Dictionary<string,object>{{"running",false},{"ready_for_install",true},{"status","stopped"},
            {"config","/home/user/worker data/node \u7b97\u529b.json"},{"node_id","existing-node"},{"backend","detached"},
            {"service_root","/home/user/service state"},{"installed_version","0.3.0rc5"}};
    }
    static Dictionary<string,object> Installed(Dictionary<string,object> stopped,string version="0.4.0") {
        var result=new Dictionary<string,object>(stopped);result["installed_version"]=version;return result;
    }
    static void ComputeActivation() {
        int probes=0,starts=0,confirmations=0,delays=0;
        bool ready=DesktopRuntime.EnsureDocker(
            ()=>Task.FromResult(new Dictionary<string,object>{{"docker_ready",++probes>=3}}),
            state=>{confirmations++;return true;},()=>{starts++;return Task.FromResult(0);},message=>{},
            ()=>{delays++;return Task.FromResult(0);}).GetAwaiter().GetResult();
        Assert(ready&&probes==3&&starts==1&&confirmations==1&&delays==2,
            "Explicit compute activation did not start Docker and wait for WSL readiness.");
        ready=DesktopRuntime.EnsureDocker(
            ()=>Task.FromResult(new Dictionary<string,object>{{"docker_ready",true}}),
            state=>{throw new Exception("Ready Docker requested startup consent.");},
            ()=>{throw new Exception("Ready Docker was started again.");},message=>{}).GetAwaiter().GetResult();
        Assert(ready,"Already-ready Docker prevented compute activation.");
        starts=0;
        ready=DesktopRuntime.EnsureDocker(
            ()=>Task.FromResult(new Dictionary<string,object>{{"docker_ready",false}}),state=>false,
            ()=>{starts++;return Task.FromResult(0);},message=>{}).GetAwaiter().GetResult();
        Assert(!ready&&starts==0,"Canceled Docker startup still enabled compute.");
        bool timedOut=false;
        try {
            DesktopRuntime.EnsureDocker(
                ()=>Task.FromResult(new Dictionary<string,object>{{"docker_ready",false}}),state=>true,
                ()=>{starts++;return Task.FromResult(0);},message=>{},timeout:TimeSpan.Zero).GetAwaiter().GetResult();
        } catch(IOException){timedOut=true;}
        Assert(timedOut&&starts==1,"Unavailable Docker was incorrectly accepted after startup timeout.");
    }
    static void WorkerCompletion() {
        string package="/mnt/c/Package with spaces/\u7b97\u529b";
        foreach(string backend in new[]{"detached","systemd"}) {
            var stopped=Stopped();stopped["backend"]=backend;int calls=0;string[] observed=null;
            InstallerForm.CompleteWorkerInstallation(package,"0.4.0",stopped,argv=>{calls++;observed=argv;return Installed(stopped);});
            Assert(calls==1,"Stopped paired worker did not install its new backend exactly once.");
            string[] expected={package+"/Client-Worker.sh","install","--no-start","--backend",backend,
                "--config",(string)stopped["config"],"--service-root",(string)stopped["service_root"]};
            Assert(observed.Length==expected.Length,"Unexpected worker installation options.");
            for(int i=0;i<expected.Length;i++)Assert(observed[i]==expected[i],"Installer changed argv or split a configuration path: "+i);
            Assert((string)stopped["installed_version"]=="0.3.0rc5"&&!(bool)stopped["running"],"Installer mutated the stopped-state evidence.");
        }
        var defaults=Stopped();defaults.Remove("backend");defaults.Remove("service_root");
        InstallerForm.CompleteWorkerInstallation(package,"0.4.0",defaults,argv=>{
            Assert(argv.Length==7&&argv[4]=="detached","Legacy worker did not default to detached or omitted service root was invented.");return Installed(defaults);
        });
        var pep=Stopped();
        InstallerForm.CompleteWorkerInstallation(package,"0.4.1-rc.2",pep,argv=>Installed(pep,"0.4.1rc2"));assertions++;
        foreach(bool absent in new[]{true,false}) {
            var unpaired=Stopped();if(absent)unpaired.Remove("config");else unpaired["config"]="";int calls=0;
            InstallerForm.CompleteWorkerInstallation(package,"0.4.0",unpaired,argv=>{calls++;throw new Exception("Unpaired worker invoked its installer.");});
            Assert(calls==0,"First-time unpaired installation started worker setup.");
        }
        foreach(var unsafeState in new[] {
            new Dictionary<string,object>(),
            new Dictionary<string,object>{{"running",true},{"ready_for_install",true}},
            new Dictionary<string,object>{{"running",false},{"ready_for_install",false}},
            new Dictionary<string,object>{{"running","false"},{"ready_for_install",true}},
            new Dictionary<string,object>{{"running",false},{"ready_for_install","true"}}
        }) {
            int calls=0;Reject(()=>InstallerForm.CompleteWorkerInstallation(package,"0.4.0",unsafeState,argv=>{calls++;return Installed(Stopped());}),
                "Unverified worker state allowed installation.");Assert(calls==0,"Unsafe worker invoked the install command.");
        }
        var unknownBackend=Stopped();unknownBackend["backend"]="unknown";int unknownCalls=0;
        Reject(()=>InstallerForm.CompleteWorkerInstallation(package,"0.4.0",unknownBackend,argv=>{unknownCalls++;return Installed(unknownBackend);}),
            "Unknown original service backend was replaced.");Assert(unknownCalls==0,"Unknown service backend invoked an installer.");
        foreach(string version in new[]{"0.3.0rc5","unknown","","0.4.1"}) {
            var stopped=Stopped();Reject(()=>InstallerForm.CompleteWorkerInstallation(package,"0.4.0",stopped,argv=>Installed(stopped,version)),
                "Old, absent, unknown or unexpected backend version was reported as updated.");
        }
        foreach(string state in new[]{"failed","error","selection_required","pairing_required","running","unknown",""}) {
            var stopped=Stopped();var result=Installed(stopped);result["status"]=state;
            Reject(()=>InstallerForm.CompleteWorkerInstallation(package,"0.4.0",stopped,argv=>result),"Unconfirmed status was reported as an installed stopped worker: "+state);
        }
        foreach(object running in new object[]{true,"false",null}) {
            var stopped=Stopped();var result=Installed(stopped);if(running==null)result.Remove("running");else result["running"]=running;
            Reject(()=>InstallerForm.CompleteWorkerInstallation(package,"0.4.0",stopped,argv=>result),"Missing, unknown or running process state was accepted.");
        }
        foreach(string identity in new[]{"config","node_id"}) {
            foreach(bool missing in new[]{false,true}) {
                var stopped=Stopped();var result=Installed(stopped);if(missing)result.Remove(identity);else result[identity]="different";
                Reject(()=>InstallerForm.CompleteWorkerInstallation(package,"0.4.0",stopped,argv=>result),"Worker installation changed or forgot the existing "+identity);
            }
        }
        var original=Stopped();bool propagated=false;
        try{InstallerForm.CompleteWorkerInstallation(package,"0.4.0",original,argv=>{throw new IOException("simulated WSL failure");});}
        catch(IOException){propagated=true;}
        Assert(propagated,"WSL installation failure was swallowed.");
        Reject(()=>InstallerForm.CompleteWorkerInstallation(package,"0.4.0",null,argv=>Installed(original)),"Null readiness state was accepted.");
        Reject(()=>InstallerForm.CompleteWorkerInstallation(package,"0.4.0",original,argv=>null),"Null installation result was reported successful.");
    }
    static void InstallerWiring() {
        var method=typeof(InstallerForm).GetMethod("Install",BindingFlags.NonPublic|BindingFlags.Static);
        var opcodes=new Dictionary<short,OpCode>();
        foreach(var field in typeof(OpCodes).GetFields(BindingFlags.Public|BindingFlags.Static)) {
            var opcode=(OpCode)field.GetValue(null);opcodes[opcode.Value]=opcode;
        }
        byte[] code=method.GetMethodBody().GetILAsByteArray();int verify=-1,complete=-1,version=-1,write=-1;
        for(int offset=0;offset<code.Length;) {
            int position=offset;short key=code[offset++];if(key==0xfe)key=unchecked((short)(0xfe00|code[offset++]));
            OpCode opcode=opcodes[key];int size;
            switch(opcode.OperandType) {
                case OperandType.InlineNone:size=0;break;
                case OperandType.ShortInlineBrTarget:case OperandType.ShortInlineI:case OperandType.ShortInlineVar:size=1;break;
                case OperandType.InlineVar:size=2;break;
                case OperandType.InlineI8:case OperandType.InlineR:size=8;break;
                case OperandType.InlineSwitch:size=4+4*BitConverter.ToInt32(code,offset);break;
                default:size=4;break;
            }
            if(opcode.OperandType==OperandType.InlineMethod&&(opcode==OpCodes.Call||opcode==OpCodes.Callvirt)) {
                var called=method.Module.ResolveMethod(BitConverter.ToInt32(code,offset));
                if(called.DeclaringType==typeof(InstallerForm)&&called.Name=="VerifyBackendStopped")verify=position;
                if(called.DeclaringType==typeof(InstallerForm)&&called.Name=="CompleteWorkerInstallation")complete=position;
                if(called.DeclaringType==typeof(App)&&called.Name=="Write"&&write<0)write=position;
            }
            if(opcode==OpCodes.Ldstr&&method.Module.ResolveString(BitConverter.ToInt32(code,offset))=="installed_version")version=position;
            offset+=size;
        }
        Assert(verify>=0&&complete>verify&&version>complete&&write>version,
            "Real installer must verify stopped state, install the WSL backend, then record installed_version and write settings.");
    }
    sealed class MemoryFixture {
        internal readonly List<string> Calls=new List<string>();
        internal string Inventory="Ubuntu\r\ndocker-desktop\r\n",After="",Identity="owned-daemon",Containers="",DesktopStatus="stopped";
        internal bool FailStop,FailInfo;int inventories;
        internal Task<string> Run(int timeout,string executable,string[] argv) {
            string file=Path.GetFileName(executable);Calls.Add(file+" "+String.Join(" ",argv));
            if(file=="wsl.exe") {
                if(argv[0]=="--list")return Task.FromResult(inventories++==0?Inventory:After);
                if(argv[0]=="--shutdown"||argv[0]=="--terminate")return Task.FromResult("");
            } else {
                if(argv[0]=="--context"&&argv[2]=="info") {if(FailInfo)throw new IOException("daemon unavailable");return Task.FromResult(Identity);}
                if(argv[0]=="--context"&&argv[2]=="ps")return Task.FromResult(Containers);
                if(argv[0]=="desktop"&&argv[1]=="status")return Task.FromResult("{\"Status\":\""+DesktopStatus+"\"}");
                if(argv[0]=="desktop"&&argv[1]=="stop") {if(FailStop)throw new IOException("unsupported stop CLI");return Task.FromResult("");}
            }
            throw new Exception("Unexpected memory-release command: "+Calls[Calls.Count-1]);
        }
        internal string Release(Dictionary<string,object> state,bool explicitClose) {
            return DesktopRuntime.Release("Ubuntu",state,explicitClose,Run).GetAwaiter().GetResult();
        }
        internal bool Called(string suffix) {return Calls.Exists(call=>call.EndsWith(suffix,StringComparison.Ordinal));}
    }
    static Dictionary<string,object> IdleRuntime(bool otherProcesses=false) {
        return new Dictionary<string,object>{{"can_stop_docker",true},{"can_terminate_wsl",!otherProcesses},
            {"docker_id","owned-daemon"},{"docker_ready",true},{"agents_stopped",true}};
    }
    sealed class WorkerReleaseFixture {
        internal readonly MemoryFixture Native=new MemoryFixture();
        internal readonly List<string> Commands=new List<string>();
        internal readonly Queue<Dictionary<string,object>> Statuses=new Queue<Dictionary<string,object>>();
        internal bool Consent=true,FailInspection,MissingInspection;
        internal int Prompts,StoppedCallbacks;
        internal Func<Task> Delay=()=>Task.FromResult(true);
        internal WorkerReleaseFixture() {
            Statuses.Enqueue(new Dictionary<string,object>{{"running",true},{"status","online"}});
            Statuses.Enqueue(new Dictionary<string,object>{{"running",true},{"status","shutting_down"}});
            Statuses.Enqueue(new Dictionary<string,object>{{"running",false},{"status","stopped"}});
        }
        internal Task<Dictionary<string,object>> Command(string action) {
            Commands.Add(action);
            if(action=="status")return Task.FromResult(Statuses.Dequeue());
            if(action=="deactivate")return Task.FromResult(new Dictionary<string,object>{{"running",true},{"status","shutting_down"}});
            if(action=="runtime-release") {
                Assert(StoppedCallbacks==1&&Statuses.Count==0,"Resource inspection preceded confirmed worker shutdown and dormancy.");
                if(FailInspection)throw new IOException("inspection unavailable");
                return Task.FromResult(MissingInspection?null:IdleRuntime(true));
            }
            throw new Exception("Unified worker stop invoked an unrelated command: "+action);
        }
        internal Task<Dictionary<string,object>> Prepare() {
            return DesktopRuntime.PrepareWorkerRelease("Ubuntu",Command,()=>{Prompts++;return Consent;},null,
                ()=>{StoppedCallbacks++;Commands.Add("dormant");},Native.Run,Delay);
        }
    }
    static void UnifiedWorkerRelease() {
        var active=new WorkerReleaseFixture();var ready=active.Prepare().GetAwaiter().GetResult();
        Assert(active.Prompts==1&&active.StoppedCallbacks==1,"Active compute did not use one stop confirmation and become dormant.");
        Assert(String.Join(",",active.Commands)=="status,deactivate,status,status,dormant,runtime-release",
            "Unified stop skipped saving, completion verification or resource inspection.");
        Assert(DesktopRuntime.NeedsOtherProcessConfirmation(ready),"Other Ubuntu sessions bypassed release confirmation.");
        var gate=new TaskCompletionSource<bool>();var draining=new WorkerReleaseFixture{Delay=()=>gate.Task};
        var pending=draining.Prepare();
        Assert(!pending.IsCompleted&&draining.StoppedCallbacks==0&&!draining.Commands.Contains("runtime-release"),
            "WSL release began while experiment save/stop was still pending.");
        gate.SetResult(true);pending.GetAwaiter().GetResult();
        Assert(draining.StoppedCallbacks==1,"Completed graceful stop did not proceed to resource release.");
        var canceled=new WorkerReleaseFixture{Consent=false};
        Assert(canceled.Prepare().GetAwaiter().GetResult()==null,"Declining stop did not cancel the unified flow.");
        Assert(canceled.StoppedCallbacks==0&&canceled.Commands.Count==1&&canceled.Native.Calls.Count==1,
            "Canceled deactivation changed the worker or Docker/WSL runtime.");
        var retry=new WorkerReleaseFixture();retry.Statuses.Clear();
        for(int i=0;i<2;i++)retry.Statuses.Enqueue(new Dictionary<string,object>{{"running",false},{"status","stopped"}});
        retry.Prepare().GetAwaiter().GetResult();
        Assert(retry.Prompts==0&&retry.Commands.Contains("runtime-release"),"Already deactivated compute could not retry release without another stop prompt.");
        var closed=new WorkerReleaseFixture();closed.Native.Inventory="docker-desktop\n";
        var closedState=closed.Prepare().GetAwaiter().GetResult();
        Assert(closed.StoppedCallbacks==1&&closed.Commands.Count==1&&closed.Commands[0]=="dormant"&&closed.Prompts==0,
            "Retrying a stopped Ubuntu launched its worker command transport.");
        Assert(App.Flag(closedState,"can_terminate_wsl")&&!DesktopRuntime.NeedsOtherProcessConfirmation(closedState),
            "Stopped Ubuntu unnecessarily asked to close other sessions.");
        Assert(closed.Native.Calls.Count==3&&!closed.Native.Calls.Exists(call=>call.Contains("--exec")),
            "Stopped Ubuntu was awakened during Docker inspection.");
        var offline=new WorkerReleaseFixture();offline.Native.Inventory="";offline.Native.FailInfo=true;
        var offlineState=offline.Prepare().GetAwaiter().GetResult();
        Assert(!DesktopRuntime.NeedsOtherProcessConfirmation(offlineState),"Already stopped Ubuntu asked for unrelated session consent when Docker was disconnected.");
        foreach(var failure in new[] {
            new Dictionary<string,object>{{"running",false},{"status","exit_failed"},{"detail","save failed"}},
            new Dictionary<string,object>{{"running",false},{"status","failed"}},
            new Dictionary<string,object>{{"status","stopped"}}
        }) {
            var rejected=new WorkerReleaseFixture();rejected.Statuses.Clear();
            rejected.Statuses.Enqueue(new Dictionary<string,object>{{"running",true},{"status","online"}});
            rejected.Statuses.Enqueue(failure);bool failed=false;
            try{rejected.Prepare().GetAwaiter().GetResult();}catch(IOException){failed=true;}catch(InvalidDataException){failed=true;}
            Assert(failed&&rejected.StoppedCallbacks==0&&!rejected.Commands.Contains("runtime-release"),
                "Failed or unverified shutdown allowed marking compute stopped and releasing WSL.");
        }
        var inspectionFailure=new WorkerReleaseFixture{FailInspection=true};bool inspectionFailed=false;
        try{inspectionFailure.Prepare().GetAwaiter().GetResult();}catch(IOException){inspectionFailed=true;}
        Assert(inspectionFailed&&inspectionFailure.StoppedCallbacks==1,"Resource inspection failure lost the already stopped worker state.");
        var missingInspection=new WorkerReleaseFixture{MissingInspection=true};bool missingFailed=false;
        try{missingInspection.Prepare().GetAwaiter().GetResult();}catch(InvalidDataException){missingFailed=true;}
        Assert(missingFailed&&missingInspection.StoppedCallbacks==1,"Missing resource inspection was mistaken for canceled or successful release.");
        var blocked=IdleRuntime(true);blocked["can_stop_docker"]=false;
        Assert(!DesktopRuntime.NeedsOtherProcessConfirmation(blocked),"Other live containers offered a force-close confirmation.");
        var supervised=IdleRuntime(true);supervised["agents_stopped"]=false;
        Assert(!DesktopRuntime.NeedsOtherProcessConfirmation(supervised),"Another live agent offered a force-close confirmation.");
        var disconnected=IdleRuntime(true);disconnected["docker_ready"]=false;disconnected["can_stop_docker"]=false;
        Assert(DesktopRuntime.NeedsOtherProcessConfirmation(disconnected),"Disconnected Docker hid other Ubuntu sessions from confirmation.");
    }
    static void MemoryRelease() {
        var inactive=new MemoryFixture();
        var inactiveState=DesktopRuntime.InspectStoppedDistribution(inactive.Run).GetAwaiter().GetResult();
        Assert(App.Flag(inactiveState,"can_stop_docker")&&App.Flag(inactiveState,"agents_stopped"),"Stopped Ubuntu could not release a verified idle Windows Docker.");
        Assert(inactive.Calls.Count==2&&!inactive.Calls.Exists(call=>call.StartsWith("wsl.exe ")),"Inspecting a stopped Ubuntu woke WSL.");
        var inactiveBusy=new MemoryFixture{Containers="another-live-container"};
        var busyState=DesktopRuntime.InspectStoppedDistribution(inactiveBusy.Run).GetAwaiter().GetResult();
        Assert(App.Flag(busyState,"docker_ready")&&!App.Flag(busyState,"can_stop_docker"),"Stopped Ubuntu inspection ignored another live container.");
        var inactiveOffline=new MemoryFixture{FailInfo=true};
        var offlineState=DesktopRuntime.InspectStoppedDistribution(inactiveOffline.Run).GetAwaiter().GetResult();
        Assert(!App.Flag(offlineState,"docker_ready")&&!App.Flag(offlineState,"can_stop_docker"),"Unavailable Windows Docker was reported as verified idle.");
        var automatic=new MemoryFixture();automatic.Release(IdleRuntime(true),false);
        Assert(automatic.Called("desktop stop"),"Automatic release did not stop a verified idle Docker.");
        Assert(!automatic.Calls.Exists(call=>call.StartsWith("wsl.exe ")),"Automatic release closed Ubuntu despite its other user processes.");
        var full=new MemoryFixture();string fullDetail=full.Release(IdleRuntime(true),true);
        Assert(full.Called("wsl.exe --shutdown")&&!full.Called("wsl.exe --terminate Ubuntu"),"Explicit release did not shut down the otherwise idle WSL VM.");
        Assert(full.Calls[full.Calls.Count-1]=="wsl.exe --list --running --quiet","Release did not verify closure without waking WSL.");
        Assert(fullDetail.Contains("Center"),"Memory release omitted that Center remains available.");
        var other=new MemoryFixture{Inventory="Ubuntu\nDebian\ndocker-desktop\n",After="Debian\n"};other.Release(IdleRuntime(true),true);
        Assert(other.Called("wsl.exe --terminate Ubuntu")&&!other.Called("wsl.exe --shutdown"),"Explicit memory release shut down another WSL distribution.");
        var unknown=new MemoryFixture{DesktopStatus="running",After="docker-desktop\n"};
        var disconnected=new Dictionary<string,object>{{"can_stop_docker",false},{"can_terminate_wsl",false},{"docker_ready",false},{"agents_stopped",true}};
        unknown.Release(disconnected,true);
        Assert(unknown.Called("wsl.exe --terminate Ubuntu")&&!unknown.Called("wsl.exe --shutdown"),"Unverified Docker authorized global WSL shutdown.");
        Assert(!unknown.Called("desktop stop"),"Disconnected Docker was stopped without container verification.");
        var alreadyOff=new MemoryFixture();alreadyOff.Release(disconnected,true);
        Assert(alreadyOff.Called("desktop status --format json")&&alreadyOff.Called("wsl.exe --shutdown"),"Already-stopped Docker prevented reclaiming the WSL VM.");
        Assert(!alreadyOff.Called("desktop stop")&&!alreadyOff.Calls.Exists(call=>call.Contains(" info ")),"Memory release started or reconnected an already-stopped Docker.");
        var noConsent=new MemoryFixture();noConsent.Release(disconnected,false);
        Assert(noConsent.Calls.Count==0,"Disconnected runtime was closed without explicit consent.");
        var live=new MemoryFixture();var busy=IdleRuntime();busy["can_stop_docker"]=false;live.Release(busy,true);
        Assert(live.Calls.Count==0,"Other running containers were ignored by explicit memory release.");
        var agent=new MemoryFixture();var supervised=IdleRuntime(true);supervised["agents_stopped"]=false;agent.Release(supervised,true);
        Assert(agent.Calls.Count==0,"Explicit release ignored another live compute agent.");
        foreach(bool changedIdentity in new[]{true,false}) {
            var race=new MemoryFixture();if(changedIdentity)race.Identity="different-daemon";else race.Containers="new-live-container";
            race.Release(IdleRuntime(),true);
            Assert(!race.Called("desktop stop")&&!race.Calls.Exists(call=>call.StartsWith("wsl.exe ")),"Changed Docker identity or new containers did not block VM shutdown.");
        }
        var unreadable=new MemoryFixture{FailInfo=true};unreadable.Release(IdleRuntime(),true);
        Assert(!unreadable.Calls.Exists(call=>call.StartsWith("wsl.exe ")),"Failed Docker recheck allowed closing Ubuntu.");
        var oldDocker=new MemoryFixture{FailStop=true,After="docker-desktop\n"};oldDocker.Release(IdleRuntime(),true);
        Assert(oldDocker.Called("wsl.exe --terminate Ubuntu")&&!oldDocker.Called("wsl.exe --shutdown"),"Unsupported Docker stop CLI allowed global WSL shutdown.");
        var restarted=new MemoryFixture{After="Ubuntu\n"};string restartedDetail=restarted.Release(IdleRuntime(),true);
        Assert(restartedDetail!=fullDetail,"An immediate restart by another application was reported as successful memory release.");
        foreach(var fixture in new[]{automatic,full,other,unknown,alreadyOff,noConsent,live,agent,unreadable,oldDocker,restarted})
            Assert(!fixture.Calls.Exists(call=>call.Contains("--exec")||call.Contains("--unregister")||call.Contains(" desktop start")||call.Contains(" wsl.exe -d")),"Memory release launched WSL/Docker or removed a distribution.");
    }
    static int Main(string[] args) {
        try {
            WorkerCompletion();InstallerWiring();ComputeActivation();UnifiedWorkerRelease();MemoryRelease();
            Assert(DesktopRuntime.CanShutdownAll("Ubuntu",new[]{"Ubuntu","docker-desktop","docker-desktop-data"}),"Idle ExLab runtime should allow full WSL shutdown.");
            Assert(!DesktopRuntime.CanShutdownAll("Ubuntu",new[]{"Ubuntu","Debian"}),"Another WSL distribution must prevent full shutdown.");
            Assert(!DesktopRuntime.CanShutdownAll("Ubuntu",new[]{"Ubuntu","docker-desktop-project"}),"A Docker-like name is not authority to shut down a user distribution.");
            App.Worker=false;
            string data=Path.Combine(args[0],"legacy data");Directory.CreateDirectory(data);
            string status=Path.Combine(data,"status.json");
            var settings=new Dictionary<string,object>();
            File.WriteAllText(status,"{\"running\":false,\"status\":\"stopped\",\"ready_for_install\":true,\"unverified_nodes\":1}");
            var stoppedState=InstallerForm.VerifyBackendStopped(args[0],data,settings);
            Assert(stoppedState!=null&&App.Text(stoppedState,"status")=="stopped"&&stoppedState.ContainsKey("unverified_nodes"),
                "Install readiness probe stopped returning its original status evidence.");
            App.Worker=true;
            Assert(InstallerForm.VerifyBackendStopped(args[0],data,settings)==null,"Fresh unconfigured worker unexpectedly probed WSL.");
            App.Worker=false;
            foreach(string invalid in new[] {
                "{\"running\":true,\"status\":\"running\",\"version\":\"0.3.0rc1\",\"ready_for_install\":false}",
                "{\"running\":true,\"status\":\"unresponsive\",\"ready_for_install\":false}",
                "{\"running\":false,\"status\":\"stopped\",\"ready_for_update\":true}"
            }) {
                File.WriteAllText(status,invalid);bool refused=false;
                try {InstallerForm.VerifyBackendStopped(args[0],data,settings);}
                catch(InvalidOperationException){refused=true;}
                if(!refused)throw new Exception("Installer accepted an unverified or old running backend.");
            }
            string helper=Path.Combine(args[0],"runtime","python.exe");
            string release=Path.Combine(args[0],"release-client");
            using(var child=Process.Start(new ProcessStartInfo(helper,App.Arguments("--hold",release)){UseShellExecute=false,CreateNoWindow=true})) {
                var finish=Task.Run(()=>{Thread.Sleep(100);File.WriteAllText(release,"exit");});
                App.WaitForPreviousClient(new[]{"--wait-pid",child.Id.ToString(),"--wait-start",child.StartTime.ToUniversalTime().Ticks.ToString()});
                if(!child.HasExited)throw new Exception("Installer continued before previous client exited.");
                finish.GetAwaiter().GetResult();
            }
            File.Delete(release);
            using(var child=Process.Start(new ProcessStartInfo(helper,App.Arguments("--hold",release)){UseShellExecute=false,CreateNoWindow=true})) {
                App.WaitForPreviousClient(new[]{"--wait-pid",child.Id.ToString(),"--wait-start",(child.StartTime.ToUniversalTime().Ticks+1).ToString()});
                if(child.HasExited)throw new Exception("Installer waited on a reused process identity.");
                File.WriteAllText(release,"exit");
                child.WaitForExit(5000);
            }
            Console.WriteLine("PASS installer/runtime: "+assertions+" assertions; worker installation, identity, unified deactivation/release, graceful completion, cancellation, dormant retry, other-process consent, distro scope and closure verification.");
            return 0;
        } catch(Exception ex){Console.Error.WriteLine(ex);return 1;}
    }
}
'@ | Set-Content -LiteralPath $probeSource -Encoding UTF8
    $probeExe = Join-Path $testRoot 'InstallProbeTests.exe'
    $references = @('System.Windows.Forms.dll','System.Drawing.dll','System.Web.Extensions.dll','System.IO.Compression.dll','System.IO.Compression.FileSystem.dll','Microsoft.CSharp.dll','System.Management.dll')
    $compileArgs = @('/nologo','/target:exe','/langversion:5','/main:InstallProbeTests',('/out:' + $probeExe))
    $compileArgs += $references | ForEach-Object { '/r:' + $_ }
    $compileArgs += @('ExperimentApp.cs','DesktopUpdates.cs','DesktopUpdateForm.cs','DesktopUpdateQueue.cs','DesktopIcons.cs','BrowserAppWindow.cs','WorkerGpuForm.cs','DesktopRuntime.cs') | ForEach-Object { Join-Path $repoRoot ('deploy/desktop/' + $_) }
    $compileArgs += $probeSource
    & $compiler @compileArgs
    if ($LASTEXITCODE -ne 0) { throw 'Installer probe tests compilation failed.' }
    & $probeExe $testRoot
    if ($LASTEXITCODE -ne 0) { throw 'Installer subprocess checks failed.' }
}
finally {
    # Only remove this test's freshly created directory beneath the workspace.
    $resolvedTestRoot = [IO.Path]::GetFullPath($testRoot)
    if (-not $resolvedTestRoot.StartsWith($runtimeRoot + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Test cleanup target is outside the workspace runtime directory.'
    }
    if (Test-Path -LiteralPath $resolvedTestRoot) { Remove-Item -LiteralPath $resolvedTestRoot -Recurse -Force }
}
