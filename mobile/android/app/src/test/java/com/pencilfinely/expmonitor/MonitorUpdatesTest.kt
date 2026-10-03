package com.pencilfinely.expmonitor

import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test
import org.junit.Assume.assumeTrue
import java.nio.file.Files
import java.security.MessageDigest

class MonitorUpdatesTest {
    @Test fun publishedApkCanBeDiscoveredDownloadedAndVerified() {
        val expected = System.getenv("EXLAB_MONITOR_ONLINE_VERSION")
        assumeTrue("Online release check runs only after publishing", expected != null)
        val selected = MonitorUpdates.check("0.5.0") ?: error("Published Monitor update missing")
        assertEquals(expected, selected.version)
        val folder = Files.createTempDirectory("monitor-live-download").toFile()
        try {
            val (file, checksum) = MonitorUpdates.download(selected, folder) {}
            MonitorUpdates.verify(file, selected, checksum)
            assertTrue(file.length() > 0)
        } finally { folder.listFiles()?.forEach { it.delete() }; folder.delete() }
    }
    private fun release(number: String, apk: Boolean = true): JSONObject {
        val folder = "${MonitorUpdates.repository}/releases/download/v$number/"
        val assets = JSONArray().put(JSONObject().put("name", "SHA256SUMS.txt").put("state", "uploaded").put("browser_download_url", folder + "SHA256SUMS.txt"))
        if (apk) assets.put(JSONObject().put("name", "ExLabMonitor-$number-android-debug.apk").put("state", "uploaded").put("size", 3).put("browser_download_url", folder + "ExLabMonitor-$number-android-debug.apk"))
        return JSONObject().put("tag_name", "v$number").put("draft", false).put("prerelease", false).put("assets", assets)
    }

    @Test fun selectNewestMonitorEvenWhenCenterReleaseHasNoApk() {
        val metadata = JSONArray().put(release("0.7.0", false)).put(release("0.5.2")).put(release("0.6.0"))
        assertEquals("0.6.0", MonitorUpdates.select(metadata.toString(), "0.5.0")!!.version)
        assertNull(MonitorUpdates.select(metadata.toString(), "0.6.0"))
        assertNull(MonitorUpdates.select(JSONArray().put(release("0.9.0").put("prerelease", true)).toString(), "0.5.0"))
        assertNull(MonitorUpdates.select(JSONArray().put(release("0.9.0").put("draft", true)).toString(), "0.5.0"))
        assertTrue(MonitorUpdates.newer("0.10.0", "0.9.9"))
        assertFalse(MonitorUpdates.newer("999999999999999999999.1.0", "0.5.0"))
        assertFalse(MonitorUpdates.newer("0.5.2-beta", "0.5.0"))
    }

    @Test fun rejectDuplicatedUntrustedAndOversizedAssets() {
        val duplicate = release("0.5.2"); duplicate.getJSONArray("assets").put(duplicate.getJSONArray("assets").getJSONObject(1))
        assertThrows(IllegalArgumentException::class.java) { MonitorUpdates.select(JSONArray().put(duplicate).toString(), "0.5.0") }
        for (url in listOf("http://github.com/Pencilfinely/exlab/releases/download/v0.5.2/a.apk", "https://else.example/update.apk")) {
            val wrong = release("0.5.2"); wrong.getJSONArray("assets").getJSONObject(1).put("browser_download_url", url)
            assertThrows(IllegalArgumentException::class.java) { MonitorUpdates.select(JSONArray().put(wrong).toString(), "0.5.0") }
        }
        val huge = release("0.5.2"); huge.getJSONArray("assets").getJSONObject(1).put("size", 1024L * 1024 * 1024)
        assertThrows(IllegalArgumentException::class.java) { MonitorUpdates.select(JSONArray().put(huge).toString(), "0.5.0") }
        assertTrue(MonitorUpdates.allowed(MonitorUpdates.api, true))
        assertFalse(MonitorUpdates.allowed("https://api.github.com/repos/someone/else/releases", true))
        assertFalse(MonitorUpdates.allowed("https://github.com.evil.example/Pencilfinely/exlab/releases/download/v0.5.2/a.apk"))
        assertFalse(MonitorUpdates.allowed("https://user@github.com/Pencilfinely/exlab/releases/download/v0.5.2/a.apk"))
        assertFalse(MonitorUpdates.allowed("https://release-assets.githubusercontent.com/example"))
        assertTrue(MonitorUpdates.allowed("https://release-assets.githubusercontent.com/example", redirected=true))
    }

    @Test fun verifyHashesRejectTruncationCorruptionAndAmbiguousChecksums() {
        val selected = MonitorUpdates.select(JSONArray().put(release("0.5.2")).toString(), "0.5.0")!!
        val body = byteArrayOf(1, 2, 3)
        val digest = MessageDigest.getInstance("SHA-256").digest(body).joinToString("") { "%02x".format(it) }
        assertEquals(digest, MonitorUpdates.checksum("$digest  ${selected.name}\n", selected.name))
        assertThrows(IllegalArgumentException::class.java) { MonitorUpdates.checksum("$digest  other.apk", selected.name) }
        assertThrows(IllegalArgumentException::class.java) { MonitorUpdates.checksum("$digest  ${selected.name}\n$digest  ${selected.name}", selected.name) }
        val file = Files.createTempFile("monitor-update-test", ".apk").toFile()
        try {
            file.writeBytes(body); MonitorUpdates.verify(file, selected, digest)
            file.writeBytes(byteArrayOf(1, 2)); assertThrows(IllegalArgumentException::class.java) { MonitorUpdates.verify(file, selected, digest) }
            file.writeBytes(byteArrayOf(3, 2, 1)); assertThrows(IllegalArgumentException::class.java) { MonitorUpdates.verify(file, selected, digest) }
        } finally { file.delete() }
    }

    @Test fun restoreAddressesAndPastePairingStayInsideTrustedOrigins() {
        val credential = "a".repeat(43)
        val paired = ControllerConnection.parse("http://192.168.1.10:8765/mobile/#pair=$credential", true)
        assertEquals("http://192.168.1.10:8765", paired.origin); assertEquals(credential, paired.pairing)
        assertEquals(paired.origin, ControllerConnection.parse(paired.origin, true).origin)
        assertEquals("https://lab.example", ControllerConnection.parse("https://LAB.example", false).origin)
        for (address in listOf("http://192.168.1.10:8765", "http://public.example", "http://8.8.8.8"))
            assertThrows(IllegalArgumentException::class.java) { ControllerConnection.parse(address, false) }
        for (address in listOf("https://user@lab.example", "https://lab.example?token=x", "https://lab.example/mobile/#pair=$credential&extra=1", "file:///etc/passwd"))
            assertThrows(IllegalArgumentException::class.java) { ControllerConnection.parse(address, true) }
        assertFalse(ControllerConnection.privateHost("192.168.001.2"))
        assertFalse(ControllerConnection.privateHost("172.40.0.1"))
    }
}
