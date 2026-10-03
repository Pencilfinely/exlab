package com.pencilfinely.expmonitor

import java.net.URI

data class ControllerConnection(val origin: String, val pairing: String?) {
    companion object {
        fun privateHost(host: String): Boolean {
            if (host == "localhost") return true
            val parts = host.split('.').map { it.toIntOrNull() ?: return false }
            if (parts.size != 4 || parts.any { it !in 0..255 } || parts.joinToString(".") != host) return false
            return parts[0] == 10 || parts[0] == 127 || (parts[0] == 192 && parts[1] == 168) ||
                (parts[0] == 172 && parts[1] in 16..31) || (parts[0] == 100 && parts[1] in 64..127)
        }

        fun parse(value: String, allowHttp: Boolean): ControllerConnection {
            val uri = URI(value.trim()); val scheme = uri.scheme?.lowercase(); val host = uri.host?.lowercase()
            require((scheme == "http" || scheme == "https") && host != null &&
                uri.rawUserInfo == null && uri.rawQuery == null && (uri.port == -1 || uri.port in 1..65535) &&
                (uri.path.isNullOrEmpty() || uri.path == "/" || uri.path == "/mobile/")) { "请输入主控地址或配对链接" }
            if (scheme == "http") require(allowHttp && privateHost(host)) { "请确认这是可信私网中的 HTTP 地址" }
            val fragment = uri.rawFragment
            require(fragment == null || (uri.path == "/mobile/" && Regex("pair=[A-Za-z0-9_-]{20,512}").matches(fragment))) { "配对链接格式不正确" }
            val origin = URI(scheme, null, host, uri.port, null, null, null).toString()
            return ControllerConnection(origin, fragment?.removePrefix("pair="))
        }
    }
}
