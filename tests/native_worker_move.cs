using System;
using System.IO;
using System.Collections.Generic;
using System.Threading.Tasks;
using System.Windows.Forms;
using ExperimentManagerDesktop;

static class WorkerMoveTests {
    static int count;
    static void Check(bool ok,string message){count++;if(!ok)throw new Exception(message);}
    static void Block(Action run){count++;try{run();}catch(IOException){return;}throw new Exception("Unsafe disk operation was accepted.");}
    internal static int Run(string folder) {
        int probes=0,reports=0,delays=0;bool cancelled=false;
        WorkerMigration.WaitForReady(()=>Task.FromResult(++probes<4?"other-work":""),s=>reports++,()=>{},()=>{delays++;return Task.FromResult(0);}).GetAwaiter().GetResult();
        Check(probes==4&&reports==1&&delays==3,"Queued migration did not wait, retry, or bound repeated status writes.");
        try {
            WorkerMigration.WaitForReady(()=>Task.FromResult("busy"),s=>{},()=>{if(cancelled)throw new OperationCanceledException();},()=>{cancelled=true;return Task.FromResult(0);}).GetAwaiter().GetResult();
            throw new Exception("Cancellation was ignored.");
        }catch(OperationCanceledException){count++;}
        Check(WorkerMigrationForm.KeepInBackground(true,CloseReason.UserClosing),"Closing the queue would interrupt migration.");
        Check(!WorkerMigrationForm.KeepInBackground(false,CloseReason.UserClosing),"Finished migration cannot close.");
        Check(!WorkerMigrationForm.KeepInBackground(true,CloseReason.WindowsShutDown),"Migration blocks Windows shutdown.");
        string oldSettings=App.SettingsDirectory;App.SettingsDirectory=Path.Combine(folder,"queue-lock");Directory.CreateDirectory(App.SettingsRoot);
        try {
            string owner=Path.Combine(App.SettingsRoot,"migration-owner.lock");
            Check(!WorkerMigration.Running,"Missing lock was treated as running.");
            using(new FileStream(owner,FileMode.OpenOrCreate,FileAccess.ReadWrite,FileShare.None))Check(WorkerMigration.Running,"Live migration owner was not detected.");
            Check(!WorkerMigration.Running,"Stale lock file blocked migration.");
        }finally{App.SettingsDirectory=oldSettings;}
        long gb=1073741824L,margin=64L*1024*1024;
        Check(WorkerDataMove.SpaceRequired(@"C:\Ubuntu\ext4.vhdx",@"E:\Worker",100*gb,40*gb)==140*gb+margin,"Cross-drive accounting lost the existing VHD.");
        Check(WorkerDataMove.SpaceRequired(@"E:\Ubuntu\ext4.vhdx",@"E:\Worker",100*gb,40*gb)==40*gb+margin,"Same-volume rename incorrectly needs a second disk copy.");
        new WorkerDataPlan {RequiredBytes=140*gb+margin,AvailableBytes=150*gb}.RequireSpace();
        Block(()=>new WorkerDataPlan {TargetFolder=@"E:\Worker",RequiredBytes=160*gb,AvailableBytes=150*gb}.RequireSpace());
        WorkerDataMove.RequireSupport("WSL 版本: 2.7.14.0\n内核: 6.18.33", "--manage --move");
        Check(WorkerMigration.SuccessfulWslReply(-1,new[]{"--help"},"--manage --move"),"Valid WSL help exit code rejected.");
        Check(!WorkerMigration.SuccessfulWslReply(-1,new[]{"--manage","fixture","--move","target"},"--manage --move"),"Move failure was ignored.");
        Check(!WorkerMigration.SuccessfulWslReply(-1,new[]{"--help"},"unavailable"),"Invalid help was accepted.");
        Block(()=>WorkerDataMove.RequireSupport("2.7.7.0","--manage --move"));
        Block(()=>WorkerDataMove.RequireSupport("2.7.14.0","--manage"));
        WorkerDataMove.RequireWindow("fixture","fixture\ndocker-desktop\n",true);
        Block(()=>WorkerDataMove.RequireWindow("fixture","fixture\nother-work\n",true));
        Block(()=>WorkerDataMove.RequireWindow("fixture","docker-desktop",false));
        for(int scenario=0;scenario<5;scenario++) {
            var journal=new Dictionary<string,object>();var commands=new List<string>();int reads=0,saved=0;
            Func<string[],string> wsl=args=>{commands.Add(String.Join("|",args));return args[0]=="--list"?(++reads==1?"fixture\ndocker-desktop":scenario==2?"new-other-work":""):"";};
            Func<string[],string> docker=args=>{
                commands.Add(String.Join("|",args));
                if(args[0]=="--context")return args[2]=="info"?(scenario==1?"wrong":"engine"):(scenario==3?"busy-container":"");
                if(args[1]=="stop") {Check(App.Flag(journal,"desktop_paused")&&saved>0,"Desktop stopped before recovery receipt.");if(scenario==4)throw new IOException("stop timed out");}
                return "";
            };
            Action pause=()=>WorkerDataMove.PauseDesktop("fixture","engine",journal,()=>saved++,wsl,docker);
            if(scenario==0)pause();else Block(pause);
            Check(commands.Contains("--shutdown")== (scenario==0),"WSL shutdown ran with other work or failed preflight.");
            if(scenario==1||scenario==3)Check(!App.Flag(journal,"desktop_paused")&&!commands.Contains("desktop|stop|--timeout|120"),"Preflight stopped Docker.");
            else {
                WorkerDataMove.ResumeDesktop("engine",journal,()=>saved++,docker);
                Check(!App.Flag(journal,"desktop_paused")&&commands.Contains("desktop|start|--timeout|120"),"Desktop not resumed after move/failure.");
            }
        }
        string root=Path.Combine(folder,"move-contracts");Directory.CreateDirectory(root);
        string metadataDisk=Path.Combine(root,"identity.vhdx");var data=new byte[2*1024*1024];var identifier=Guid.NewGuid();
        using(var memory=new MemoryStream(data))using(var writer=new BinaryWriter(memory)) {
            writer.Write(System.Text.Encoding.ASCII.GetBytes("vhdxfile"));memory.Position=192*1024;
            writer.Write(System.Text.Encoding.ASCII.GetBytes("regi"));writer.Write(0U);writer.Write(1U);writer.Write(0U);
            writer.Write(new Guid("8B7CA206-4790-4B9A-B8FE-575F050F886E").ToByteArray());writer.Write(1048576L);writer.Write(1048576U);writer.Write(1U);
            memory.Position=1048576;writer.Write(System.Text.Encoding.ASCII.GetBytes("metadata"));writer.Write((ushort)0);writer.Write((ushort)1);
            memory.Position=1048576+32;writer.Write(new Guid("BECA12AB-B2E6-4523-93EF-C309E000C746").ToByteArray());writer.Write(65536U);writer.Write(16U);
            memory.Position=1048576+65536;writer.Write(identifier.ToByteArray());
        }
        File.WriteAllBytes(metadataDisk,data);
        using(var attached=new FileStream(metadataDisk,FileMode.Open,FileAccess.ReadWrite,FileShare.ReadWrite))
            Check(WorkerDisk.Identifier(metadataDisk)==identifier.ToString("D"),"Identity reader cannot share a WSL disk handle.");
        foreach(int offset in new[]{0,192*1024,192*1024+8,192*1024+32,1048576,1048576+10,1048576+48,1048576+52}) {
            var invalid=(byte[])data.Clone();for(int j=0;j<4;j++)invalid[offset+j]=255;File.WriteAllBytes(metadataDisk,invalid);
            Block(()=>WorkerDisk.Identifier(metadataDisk));
        }
        File.WriteAllBytes(metadataDisk,new byte[12]);Block(()=>WorkerDisk.Identifier(metadataDisk));
        for(int scenario=0;scenario<6;scenario++) {
            string area=Path.Combine(root,scenario.ToString()),original=Path.Combine(area,"source"),destination=Path.Combine(area,"destination");
            Directory.CreateDirectory(original);string disk=Path.Combine(original,"ext4.vhdx"),target=Path.Combine(destination,"ext4.vhdx");
            File.WriteAllText(disk,"unique fixture disk "+scenario);
            var plan=new WorkerDataPlan {Distribution="fixture",SourceDisk=disk,TargetFolder=destination,TargetDisk=target,DistributionId="fixture-id",Moving=true,AvailableBytes=gb};
            var job=new Dictionary<string,object>{{"source","fixture"},{"path",Path.Combine(area,"journal")}};
            string registered=disk;int saves=0;var commands=new List<string>();
            Func<string,string> fingerprint=p=>File.ReadAllText(p).StartsWith("unique fixture disk")?"same-vhd-id":"different-vhd-id";
            Func<string[],string> command=args=>{
                commands.Add(String.Join("|",args));
                if(args[0]=="--list")return "unrelated-distro\n";
                if(args[0]=="--manage") {
                    Check(App.Flag(job,"move_started")&&saves>0,"Move started before its durable receipt.");
                    if(scenario==1)throw new IOException("copy failed");
                    Directory.CreateDirectory(destination);File.Move(disk,target);
                    if(scenario!=2)registered=target;
                    if(scenario==3)File.WriteAllText(target,"corrupted target");
                    if(scenario==4)throw new IOException("post-move CLI error");
                }
                return "";
            };
            if(scenario==5) {
                Block(()=>WorkerDataMove.Move(plan,job,()=>saves++,command,d=>disk,d=>"other-id",fingerprint));
                Check(commands.Count==0&&File.Exists(disk),"Changed distribution was modified.");continue;
            }
            Action move=()=>WorkerDataMove.Move(plan,job,()=>saves++,command,d=>registered,d=>"fixture-id",fingerprint);
            if(scenario==1||scenario==2||scenario==3)Block(move);else move();
            Check(commands.Count==3&&commands[0]=="--terminate|fixture"&&commands[2]=="--manage|fixture|--move|"+destination,"Move touched an unrelated distro or used export/unregister.");
            if(scenario==1)Check(File.Exists(disk)&&!App.Flag(job,"data_moved"),"Failed copy lost the original.");
            else if(scenario==2||scenario==3)Check(File.Exists(target)&&!App.Flag(job,"data_moved"),"Ambiguous move was accepted or target deleted.");
            else {
                Check(App.Flag(job,"data_moved")&&!File.Exists(disk)&&File.Exists(target),"Successful move was not recorded.");
                File.AppendAllText(target,"later boot changes the disk");
                Check(WorkerDataMove.VerifyLocation(job,d=>registered,d=>"fixture-id",fingerprint),"Recovery rejected a previously verified disk after boot.");
                Block(()=>WorkerDataMove.VerifyLocation(job,d=>registered,d=>"wrong-id",fingerprint));
            }
        }
        string occupied=Path.Combine(root,"occupied");Directory.CreateDirectory(occupied);File.WriteAllText(Path.Combine(occupied,"keep"),"user data");
        Block(()=>WorkerDataMove.ValidateTarget(Path.Combine(root,"source/ext4.vhdx"),occupied,null));
        Block(()=>WorkerDataMove.ValidateTarget(Path.Combine(root,"source/ext4.vhdx"),Path.Combine(root,"source/nested"),null));
        Block(()=>WorkerDataMove.ValidateTarget(Path.Combine(root,"source/ext4.vhdx"),Path.Combine(root,"backup/data"),Path.Combine(root,"backup")));
        Check(File.ReadAllText(Path.Combine(occupied,"keep"))=="user data","Target validation changed user files.");
        return count;
    }
}
