package com.pencilfinely.expmonitor

import org.json.JSONArray
import java.io.File
import java.io.InputStream
import java.io.ByteArrayOutputStream
import java.net.HttpURLConnection
import java.net.URI
import java.security.MessageDigest

data class MonitorRelease(val version: String, val name: String, val url: String, val size: Long,
                          val checksumUrl: String, val digest: String?)

/** Native update traffic is independent of the controller and carries no pairing credentials. */
object MonitorUpdates {
    const val repository = "https://github.com/Pencilfinely/exlab"
    const val api = "https://api.github.com/repos/Pencilfinely/exlab/releases?per_page=100"
    private const val maxApk = 128L * 1024 * 1024
    private fun version(value: String): List<Int>? {
        if (!Regex("[0-9]+\\.[0-9]+\\.[0-9]+").matches(value)) return null
        return value.split('.').map { it.toIntOrNull() ?: return null }
    }

    fun newer(candidate: String, current: String): Boolean {
        val a = version(candidate) ?: return false
        val b = version(current) ?: return false
        for (i in a.indices) if (a[i] != b[i]) return a[i] > b[i]
        return false
    }

    fun allowed(value: String, metadata: Boolean = false, redirected: Boolean = false): Boolean = try {
        val uri = URI(value)
        uri.scheme == "https" && uri.rawUserInfo == null && uri.rawFragment == null &&
            (uri.port == -1 || uri.port == 443) && when (uri.host?.lowercase()) {
                "api.github.com" -> metadata && uri.path == "/repos/Pencilfinely/exlab/releases"
                "github.com" -> !metadata && uri.rawQuery == null && uri.path.startsWith("/Pencilfinely/exlab/releases/download/")
                "release-assets.githubusercontent.com", "objects.githubusercontent.com" -> !metadata && redirected
                else -> false
            }
    } catch (_: Exception) { false }

    fun select(json: String, current: String): MonitorRelease? {
        val releases = JSONArray(json)
        var chosen: MonitorRelease? = null
        for (index in 0 until releases.length()) {
            val release = releases.getJSONObject(index)
            if (release.optBoolean("draft") || release.optBoolean("prerelease")) continue
            val tag = release.optString("tag_name")
            val number = tag.removePrefix("v")
            if (tag != "v$number" || !newer(number, current)) continue
            val expected = "ExLabMonitor-$number-android-debug.apk"
            val assets = release.optJSONArray("assets") ?: continue
            val apks = (0 until assets.length()).map { assets.getJSONObject(it) }.filter { it.optString("name") == expected }
            val sums = (0 until assets.length()).map { assets.getJSONObject(it) }.filter { it.optString("name") == "SHA256SUMS.txt" }
            if (apks.isEmpty()) continue // A Center-only release need not include a Monitor update.
            require(apks.size == 1 && sums.size == 1) { "发布包含重复安装包或缺少校验文件" }
            val apk = apks.single(); val checksums = sums.single()
            val url = apk.optString("browser_download_url"); val checksumUrl = checksums.optString("browser_download_url")
            require(allowed(url) && url == "$repository/releases/download/$tag/$expected" &&
                allowed(checksumUrl) && checksumUrl == "$repository/releases/download/$tag/SHA256SUMS.txt" &&
                apk.optString("state") == "uploaded" && checksums.optString("state") == "uploaded" &&
                apk.optLong("size") in 1..maxApk) { "发布安装包信息不完整" }
            val digest = apk.optString("digest").takeIf { Regex("sha256:[a-fA-F0-9]{64}").matches(it) }?.removePrefix("sha256:")?.lowercase()
            val candidate = MonitorRelease(number, expected, url, apk.getLong("size"), checksumUrl, digest)
            if (chosen == null || newer(number, chosen.version)) chosen = candidate
        }
        return chosen
    }

    fun checksum(text: String, name: String): String {
        val matches = text.lineSequence().mapNotNull { Regex("^([a-fA-F0-9]{64}) [ *](.+)$").matchEntire(it.trimEnd()) }
            .filter { it.groupValues[2] == name }.toList()
        require(matches.size == 1) { "安装包校验信息缺失或重复" }
        return matches.single().groupValues[1].lowercase()
    }

    private fun <T> read(value: String, metadata: Boolean = false, consume: (InputStream) -> T): T {
        var target = value
        repeat(6) { hop ->
            require(allowed(target, metadata, hop > 0)) { "更新地址不受信任" }
            val connection = URI(target).toURL().openConnection() as HttpURLConnection
            connection.instanceFollowRedirects = false
            connection.connectTimeout = 15000; connection.readTimeout = 20000
            connection.setRequestProperty("User-Agent", "ExLab-Monitor")
            connection.setRequestProperty("Accept", if (metadata) "application/vnd.github+json" else "application/octet-stream")
            try {
                val code = connection.responseCode
                if (code in listOf(301, 302, 303, 307, 308)) {
                    require(!metadata) { "更新源发生重定向，请稍后再试" }
                    target = URI(target).resolve(connection.getHeaderField("Location") ?: error("无效下载重定向")).toString()
                } else {
                    require(code == 200) { if (code == 403 || code == 429) "更新服务暂时繁忙，请稍后再试" else "更新服务返回 $code" }
                    return connection.inputStream.use(consume)
                }
            } finally { connection.disconnect() }
        }
        error("下载重定向次数过多")
    }

    private fun text(value: String, limit: Int, metadata: Boolean = false): String = read(value, metadata) { input ->
        val bytes = ByteArrayOutputStream(); val buffer = ByteArray(8192)
        while (true) {
            val n = input.read(buffer); if (n < 0) break
            require(bytes.size() + n <= limit) { "更新信息过大" }; bytes.write(buffer, 0, n)
        }
        bytes.toString("UTF-8")
    }

    fun check(current: String): MonitorRelease? = select(text(api, 4 * 1024 * 1024, true), current)

    fun verify(file: File, release: MonitorRelease, sha256: String) {
        require(file.isFile && file.length() == release.size) { "下载未完成，请重新下载" }
        val hash = MessageDigest.getInstance("SHA-256")
        file.inputStream().use { input -> val buffer = ByteArray(65536); while (true) { val n = input.read(buffer); if (n < 0) break; hash.update(buffer, 0, n) } }
        require(hash.digest().joinToString("") { "%02x".format(it) } == sha256) { "安装包校验失败，请重新下载" }
    }

    fun download(release: MonitorRelease, folder: File, progress: (Int) -> Unit): Pair<File, String> {
        val sha256 = checksum(text(release.checksumUrl, 256 * 1024), release.name)
        require(release.digest == null || release.digest == sha256) { "发布校验值不一致" }
        require(folder.isDirectory || folder.mkdirs()) { "无法保存安装包" }
        val file = File(folder, release.name)
        if (file.exists()) { try { verify(file, release, sha256); return file to sha256 } catch (_: IllegalArgumentException) { require(file.delete()) } }
        val partial = File(folder, release.name + ".part")
        try {
            read(release.url) { input -> partial.outputStream().use { output ->
                val buffer = ByteArray(65536); var count = 0L; var shown = -1
                while (true) {
                    if (Thread.currentThread().isInterrupted) throw InterruptedException()
                    val n = input.read(buffer); if (n < 0) break
                    count += n; require(count <= release.size) { "安装包超过声明大小" }; output.write(buffer, 0, n)
                    val percent = (count * 100 / release.size).toInt(); if (percent != shown) { progress(percent); shown = percent }
                }
            } }
            verify(partial, release, sha256)
            require(partial.renameTo(file)) { "无法保存已校验的安装包" }
            return file to sha256
        } finally { if (partial.exists()) partial.delete() }
    }
}
