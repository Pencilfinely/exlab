package com.pencilfinely.expmonitor

import android.app.Activity
import android.app.AlertDialog
import android.content.ActivityNotFoundException
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.provider.Settings
import android.widget.LinearLayout
import android.widget.ProgressBar
import android.widget.TextView
import androidx.core.content.FileProvider
import java.io.File
import java.util.concurrent.Executors
import java.util.concurrent.Future

class MonitorUpdateDialog(private val activity: Activity) {
    private val executor = Executors.newSingleThreadExecutor()
    private var task: Future<*>? = null
    private var dialog: AlertDialog? = null
    private var release: MonitorRelease? = null
    private var downloaded: Pair<File, String>? = null
    private var pendingInstall = false

    fun show() {
        if (dialog?.isShowing == true) return
        val text = TextView(activity).apply { textSize = 14f; text = activity.getString(R.string.update_intro, BuildConfig.VERSION_NAME) }
        val progress = ProgressBar(activity, null, android.R.attr.progressBarStyleHorizontal).apply { visibility = android.view.View.GONE }
        val content = LinearLayout(activity).apply {
            orientation = LinearLayout.VERTICAL
            val padding = (24 * resources.displayMetrics.density).toInt(); setPadding(padding, padding / 2, padding, padding / 2)
            addView(text); addView(progress)
        }
        val window = AlertDialog.Builder(activity).setTitle("ExLab Monitor · 更新").setView(content)
            .setPositiveButton("检查更新", null).setNeutralButton("导出安装包", null).setNegativeButton("稍后", null).create()
        dialog = window
        window.setOnDismissListener { task?.cancel(true); pendingInstall = false }
        fun ui(action: () -> Unit) { activity.runOnUiThread { if (!activity.isDestroyed && window.isShowing) action() } }
        fun busy(value: Boolean) {
            window.getButton(AlertDialog.BUTTON_POSITIVE).isEnabled = !value
            window.getButton(AlertDialog.BUTTON_NEUTRAL).isEnabled = !value && downloaded != null
            progress.visibility = if (value) android.view.View.VISIBLE else android.view.View.GONE
        }
        fun check() {
            release = null; downloaded = null; busy(true); progress.isIndeterminate = true; text.text = "正在检查新版本…"
            task = executor.submit {
                try {
                    val result = MonitorUpdates.check(BuildConfig.VERSION_NAME)
                    ui {
                        release = result; busy(false)
                        text.text = if (result == null) activity.getString(R.string.update_latest, BuildConfig.VERSION_NAME) else activity.getString(R.string.update_available, result.version, result.size / 1048576.0)
                        window.getButton(AlertDialog.BUTTON_POSITIVE).text = if (result == null) "再次检查" else "下载更新"
                    }
                } catch (failure: Exception) { ui { busy(false); text.text = failure.message ?: "检查失败，请稍后重试" } }
            }
        }
        window.setOnShowListener {
            window.getButton(AlertDialog.BUTTON_NEUTRAL).isEnabled = false
            window.getButton(AlertDialog.BUTTON_NEUTRAL).setOnClickListener {
                try {
                    val file = verifiedApk()
                    val uri = FileProvider.getUriForFile(activity, activity.packageName + ".updates", file)
                    activity.startActivity(Intent.createChooser(Intent(Intent.ACTION_SEND).apply {
                        type = "application/vnd.android.package-archive"; putExtra(Intent.EXTRA_STREAM, uri)
                        addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
                    }, "导出新版安装包"))
                } catch (failure: Exception) { text.text = failure.message ?: "无法导出安装包" }
            }
            window.getButton(AlertDialog.BUTTON_POSITIVE).setOnClickListener {
                if (release == null) check()
                else if (downloaded != null) install()
                else {
                    val selected = release ?: return@setOnClickListener
                    busy(true); progress.isIndeterminate = false; progress.progress = 0; text.text = activity.getString(R.string.update_downloading, selected.version)
                    task = executor.submit {
                        try {
                            val result = MonitorUpdates.download(selected, File(activity.cacheDir, "updates")) { percent -> ui { progress.progress = percent; text.text = activity.getString(R.string.update_progress, selected.version, percent) } }
                            ui {
                                downloaded = result
                                try { verifiedApk(); busy(false); text.text = activity.getString(R.string.update_verified, selected.version); window.getButton(AlertDialog.BUTTON_POSITIVE).text = "安装更新" }
                                catch (failure: Exception) { downloaded = null; busy(false); text.text = failure.message ?: "安装包验证失败" }
                            }
                        } catch (failure: Exception) { ui { busy(false); text.text = failure.message ?: "下载失败，请重试" } }
                    }
                }
            }
            check()
        }
        window.show()
    }

    private fun verifiedApk(): File {
        val selected = release ?: error("请先检查更新")
        val (file, sha256) = downloaded ?: error("请先下载更新")
        MonitorUpdates.verify(file, selected, sha256)
        @Suppress("DEPRECATION")
        val current = activity.packageManager.getPackageInfo(activity.packageName, PackageManager.GET_SIGNING_CERTIFICATES)
        @Suppress("DEPRECATION")
        val apk = activity.packageManager.getPackageArchiveInfo(file.absolutePath, PackageManager.GET_SIGNING_CERTIFICATES) ?: error("安装包无法解析")
        require(apk.packageName == activity.packageName && apk.versionName == selected.version && apk.longVersionCode > current.longVersionCode) { "安装包的应用或版本不匹配" }
        val installedSigners = current.signingInfo?.apkContentsSigners?.map { it.toCharsString() }?.toSet().orEmpty()
        val newSigners = apk.signingInfo?.apkContentsSigners?.map { it.toCharsString() }?.toSet().orEmpty()
        require(installedSigners.isNotEmpty() && installedSigners == newSigners) { "安装包签名与当前应用不同，不能覆盖更新" }
        return file
    }

    private fun install() {
        try {
            val file = verifiedApk()
            if (!activity.packageManager.canRequestPackageInstalls()) {
                AlertDialog.Builder(activity).setTitle("允许安装更新")
                    .setMessage("请在系统中允许 ExLab Monitor 安装更新，返回后继续。")
                    .setPositiveButton("打开设置") { _, _ ->
                        try { pendingInstall = true; activity.startActivity(Intent(Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES, Uri.parse("package:" + activity.packageName))) }
                        catch (_: Exception) { pendingInstall = false; message("此兼容环境未提供安装权限设置。可使用「导出安装包」覆盖安装，保留原应用数据。") }
                    }.setNegativeButton("稍后", null).show()
                return
            }
            pendingInstall = false
            val uri = FileProvider.getUriForFile(activity, activity.packageName + ".updates", file)
            activity.startActivity(Intent(Intent.ACTION_VIEW).setDataAndType(uri, "application/vnd.android.package-archive").addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION))
        } catch (_: ActivityNotFoundException) { message("此兼容环境未提供安装器。可使用「导出安装包」覆盖安装，保留原应用数据。") }
        catch (failure: Exception) { message(failure.message ?: "无法启动安装更新") }
    }

    private fun message(value: String) { AlertDialog.Builder(activity).setTitle("更新").setMessage(value).setPositiveButton("知道了", null).show() }
    fun resume() {
        if (!pendingInstall) return
        try { if (activity.packageManager.canRequestPackageInstalls()) install() }
        catch (_: Exception) { pendingInstall = false; message("此兼容环境未提供安装权限检查。可使用「导出安装包」覆盖安装，保留原应用数据。") }
    }
    fun close() { dialog?.dismiss(); executor.shutdownNow() }
}
