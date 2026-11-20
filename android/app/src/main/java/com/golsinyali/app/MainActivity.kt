package com.golsinyali.app

import android.annotation.SuppressLint
import android.app.Activity
import android.content.Intent
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.webkit.WebResourceRequest
import android.webkit.WebResourceResponse
import android.webkit.WebView
import androidx.webkit.WebViewAssetLoader
import androidx.webkit.WebViewClientCompat

class MainActivity : Activity() {

    private lateinit var web: WebView

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        window.statusBarColor = 0xFF0F1115.toInt()

        val loader = WebViewAssetLoader.Builder()
            .addPathHandler("/assets/", WebViewAssetLoader.AssetsPathHandler(this))
            .build()

        web = WebView(this)
        web.setBackgroundColor(0xFF0F1115.toInt())
        web.settings.javaScriptEnabled = true
        web.settings.domStorageEnabled = true
        web.webViewClient = object : WebViewClientCompat() {
            override fun shouldInterceptRequest(view: WebView, request: WebResourceRequest): WebResourceResponse? =
                loader.shouldInterceptRequest(request.url)

            override fun shouldOverrideUrlLoading(view: WebView, request: WebResourceRequest): Boolean {
                val u = request.url
                if (!request.isForMainFrame || u.host == "appassets.androidplatform.net") return false
                // Dış bağlantılar (indirme, GitHub, ntfy) tarayıcıda/uygulamasında açılsın
                runCatching { startActivity(Intent(Intent.ACTION_VIEW, u)) }
                return true
            }
        }
        setContentView(web)
        web.loadUrl(startUrl(intent))
    }

    private fun startUrl(intent: Intent?): String {
        val version = runCatching {
            val info = packageManager.getPackageInfo(packageName, 0)
            if (Build.VERSION.SDK_INT >= 28) info.longVersionCode else @Suppress("DEPRECATION") info.versionCode.toLong()
        }.getOrDefault(0L)
        val tab = intent?.data?.host ?: "canli"
        return "https://appassets.androidplatform.net/assets/www/index.html?v=$version#$tab"
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        val tab = intent.data?.host ?: return
        web.evaluateJavascript("window.showTab && showTab('${tab.replace("'", "")}')", null)
    }

    @Deprecated("Deprecated in Java")
    override fun onBackPressed() {
        if (web.canGoBack()) web.goBack() else @Suppress("DEPRECATION") super.onBackPressed()
    }
}
