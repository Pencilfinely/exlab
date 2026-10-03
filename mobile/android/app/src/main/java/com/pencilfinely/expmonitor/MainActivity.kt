package com.pencilfinely.expmonitor

import android.annotation.SuppressLint
import android.app.Activity
import android.graphics.Color
import android.net.http.SslError
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.text.InputType
import android.view.View
import android.view.ViewGroup
import android.view.WindowInsets
import android.webkit.*
import android.widget.*
import java.net.URI

/** Pairing stays in origin-scoped Web storage; no credentials or commands cross a JS bridge. */
class MainActivity : Activity() {
    private var browser: WebView? = null
    private var origin = ""
    private var pairing: String? = null
    private var failedLoad = false
    private var resumed = false
    private val handler = Handler(Looper.getMainLooper())
    private lateinit var updates: MonitorUpdateDialog
    private val reconnect = Runnable { if (resumed && failedLoad) browser?.loadUrl("$origin/mobile/" + (pairing?.let { "#pair=$it" } ?: "")) }
    private val green = Color.rgb(35, 79, 65)
    private fun dp(value: Int) = (value * resources.displayMetrics.density).toInt()

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) window.setDecorFitsSystemWindows(false)
        updates = MonitorUpdateDialog(this)
        val saved = getSharedPreferences("connection", MODE_PRIVATE)
        try {
            val previous = saved.getString("origin", "").orEmpty()
            if (previous.isEmpty()) showConnection()
            else {
                // Earlier versions already validated and explicitly selected a private HTTP origin.
                val connection = ControllerConnection.parse(previous, saved.getBoolean("allow_http", previous.startsWith("http://")))
                origin = connection.origin; openMonitor()
            }
        } catch (_: Exception) { showConnection() }
    }

    private fun layout(): LinearLayout {
        val root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setBackgroundColor(Color.rgb(244, 246, 241))
        }
        root.setOnApplyWindowInsetsListener { view, insets ->
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
                val safe = insets.getInsets(WindowInsets.Type.systemBars() or
                    WindowInsets.Type.displayCutout() or WindowInsets.Type.ime())
                view.setPadding(safe.left, safe.top, safe.right, safe.bottom)
                WindowInsets.CONSUMED
            } else {
                applyLegacyInsets(view, insets)
            }
        }
        setContentView(root)
        root.requestApplyInsets()
        return root
    }

    @Suppress("DEPRECATION")
    private fun applyLegacyInsets(view: View, insets: WindowInsets): WindowInsets {
        val cutout = insets.displayCutout
        view.setPadding(maxOf(insets.systemWindowInsetLeft, cutout?.safeInsetLeft ?: 0),
            maxOf(insets.systemWindowInsetTop, cutout?.safeInsetTop ?: 0),
            maxOf(insets.systemWindowInsetRight, cutout?.safeInsetRight ?: 0),
            maxOf(insets.systemWindowInsetBottom, cutout?.safeInsetBottom ?: 0))
        return insets.consumeSystemWindowInsets().consumeDisplayCutout()
    }

    private fun showConnection() {
        handler.removeCallbacks(reconnect); failedLoad = false; pairing = null
        browser?.let { (it.parent as? ViewGroup)?.removeView(it); it.stopLoading(); it.destroy() }
        browser = null
        val root = layout()
        val scroll = ScrollView(this)
        val form = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL; setPadding(dp(24), dp(32), dp(24), dp(24)) }
        scroll.addView(form); root.addView(scroll)
        form.addView(TextView(this).apply { setText(R.string.connection_title); textSize = 25f; setTextColor(green) })
        form.addView(TextView(this).apply { setText(R.string.connection_hint); setPadding(0, dp(18), 0, dp(18)) })
        val address = EditText(this).apply {
            setHint(R.string.address_hint)
            inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_URI
            setSingleLine(true)
            setText(getSharedPreferences("connection", MODE_PRIVATE).getString("origin", ""))
        }
        val allowHttp = CheckBox(this).apply { setText(R.string.allow_private_http); isChecked = getSharedPreferences("connection", MODE_PRIVATE).getBoolean("allow_http", false) }
        val error = TextView(this).apply { setTextColor(Color.rgb(156, 58, 52)); setPadding(0, dp(10), 0, dp(10)) }
        form.addView(address); form.addView(allowHttp); form.addView(error)
        form.addView(Button(this).apply {
            setText(R.string.connect)
            setOnClickListener {
                try {
                    val connection = ControllerConnection.parse(address.text.toString(), allowHttp.isChecked)
                    origin = connection.origin; pairing = connection.pairing
                    getSharedPreferences("connection", MODE_PRIVATE).edit().putString("origin", origin).putBoolean("allow_http", allowHttp.isChecked).apply()
                    openMonitor()
                } catch (failure: Exception) { error.text = failure.message ?: "请输入主控地址或配对链接" }
            }
        })
        form.addView(Button(this).apply { text = getString(R.string.monitor_check_version, BuildConfig.VERSION_NAME); setOnClickListener { updates.show() } })
    }

    private fun sameOrigin(value: String): Boolean = try {
        val a = URI(origin); val b = URI(value)
        fun port(uri: URI) = if (uri.port >= 0) uri.port else if (uri.scheme == "https") 443 else 80
        a.scheme == b.scheme && a.host.equals(b.host, ignoreCase = true) && port(a) == port(b) && b.rawUserInfo == null
    } catch (_: Exception) { false }

    @SuppressLint("SetJavaScriptEnabled")
    private fun openMonitor() {
        val root = layout()
        val toolbar = LinearLayout(this).apply { gravity = android.view.Gravity.END }
        val status = TextView(this).apply { text = ""; textSize = 12f; setPadding(dp(16), dp(10), dp(16), dp(10)); visibility = View.GONE }
        toolbar.addView(Button(this).apply { text = "连接"; setBackgroundColor(Color.TRANSPARENT); setTextColor(green); setOnClickListener { showConnection() } })
        toolbar.addView(Button(this).apply { text = "更新"; setBackgroundColor(Color.TRANSPARENT); setTextColor(green); setOnClickListener { updates.show() } })
        root.addView(toolbar); root.addView(status)
        val web = WebView(this)
        val frame = FrameLayout(this)
        val waiting = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL; gravity = android.view.Gravity.CENTER
            setPadding(dp(28), dp(28), dp(28), dp(28)); visibility = View.GONE
            addView(TextView(this@MainActivity).apply { setText(R.string.app_name); textSize = 25f; setTextColor(green) })
            addView(TextView(this@MainActivity).apply { text = "已保存配对 · 等待实验台上线"; textSize = 14f; setPadding(0, dp(18), 0, dp(24)) })
            addView(Button(this@MainActivity).apply { text = "立即重试"; setOnClickListener { handler.removeCallbacks(reconnect); handler.post(reconnect) } })
        }
        frame.addView(web, FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT))
        frame.addView(waiting, FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT))
        browser = web
        web.settings.apply {
            javaScriptEnabled = true
            domStorageEnabled = true
            allowFileAccess = false
            allowContentAccess = false
            mixedContentMode = WebSettings.MIXED_CONTENT_NEVER_ALLOW
            javaScriptCanOpenWindowsAutomatically = false
            setSupportMultipleWindows(false)
        }
        CookieManager.getInstance().setAcceptThirdPartyCookies(web, false)
        web.webViewClient = object : WebViewClient() {
            override fun shouldOverrideUrlLoading(view: WebView, request: WebResourceRequest): Boolean = !sameOrigin(request.url.toString())
            override fun shouldInterceptRequest(view: WebView, request: WebResourceRequest): WebResourceResponse? {
                if (sameOrigin(request.url.toString())) return null
                return WebResourceResponse("text/plain", "UTF-8", 403, "Forbidden", emptyMap(), "External resource blocked".byteInputStream())
            }
            override fun onReceivedSslError(view: WebView, handler: SslErrorHandler, error: SslError) {
                handler.cancel()
                failedLoad = true; waiting.visibility = View.VISIBLE; web.visibility = View.INVISIBLE
                status.visibility = View.VISIBLE
                status.setText(R.string.certificate_error)
            }
            override fun onPageCommitVisible(view: WebView, url: String) {
                if (sameOrigin(url) && !failedLoad) { pairing = null; handler.removeCallbacks(reconnect); status.visibility = View.GONE; waiting.visibility = View.GONE; web.visibility = View.VISIBLE }
            }
            override fun onPageStarted(view: WebView, url: String, favicon: android.graphics.Bitmap?) {
                failedLoad = false
            }
            override fun onReceivedError(view: WebView, request: WebResourceRequest, error: WebResourceError) {
                if (request.isForMainFrame) {
                    failedLoad = true; status.visibility = View.VISIBLE; status.text = "等待实验台上线，自动重连中…"
                    waiting.visibility = View.VISIBLE; web.visibility = View.INVISIBLE
                    handler.removeCallbacks(reconnect); if (resumed) handler.postDelayed(reconnect, 5000)
                }
            }
            override fun onReceivedHttpError(view: WebView, request: WebResourceRequest, response: WebResourceResponse) {
                if (request.isForMainFrame) {
                    status.visibility = View.VISIBLE; status.text = getString(R.string.http_error, response.statusCode)
                    failedLoad = true; waiting.visibility = View.VISIBLE; web.visibility = View.INVISIBLE
                    if (response.statusCode >= 500) { handler.removeCallbacks(reconnect); if (resumed) handler.postDelayed(reconnect, 5000) }
                }
            }
        }
        root.addView(frame, LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, 0, 1f))
        web.loadUrl("$origin/mobile/" + (pairing?.let { "#pair=$it" } ?: ""))
    }

    @Deprecated("Platform fallback is used on Android 9 and later")
    @Suppress("DEPRECATION")
    override fun onBackPressed() {
        val web = browser ?: return super.onBackPressed()
        web.evaluateJavascript("Boolean(window.ExperimentMobileBack && window.ExperimentMobileBack())") { handled ->
            if (browser === web && handled != "true") finish()
        }
    }
    override fun onPause() { resumed = false; handler.removeCallbacks(reconnect); browser?.onPause(); super.onPause() }
    override fun onResume() { super.onResume(); resumed = true; browser?.onResume(); updates.resume(); if (failedLoad) handler.post(reconnect) }
    override fun onDestroy() { handler.removeCallbacks(reconnect); updates.close(); browser?.destroy(); browser = null; super.onDestroy() }
}
