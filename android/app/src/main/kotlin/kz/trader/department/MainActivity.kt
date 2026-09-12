package kz.trader.department

import android.annotation.SuppressLint
import android.content.Context
import android.graphics.Bitmap
import android.net.http.SslError
import android.os.Bundle
import android.view.View
import android.webkit.HttpAuthHandler
import android.webkit.SslErrorHandler
import android.webkit.WebResourceError
import android.webkit.WebResourceRequest
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.EditText
import android.widget.TextView
import androidx.activity.OnBackPressedCallback
import androidx.appcompat.app.AppCompatActivity
import androidx.swiperefreshlayout.widget.SwipeRefreshLayout
import com.google.android.material.dialog.MaterialAlertDialogBuilder
import com.google.android.material.floatingactionbutton.FloatingActionButton

/** Оболочка над веб-панелью отдела: адрес сервера и пароль хранятся на телефоне. */
class MainActivity : AppCompatActivity() {
    private lateinit var web: WebView
    private lateinit var swipe: SwipeRefreshLayout
    private lateinit var errorBox: View
    private lateinit var errorText: TextView
    private val prefs by lazy { getSharedPreferences("trader", Context.MODE_PRIVATE) }

    private val url: String get() = prefs.getString("url", "") ?: ""
    private val password: String get() = prefs.getString("password", "") ?: ""

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)
        web = findViewById(R.id.web)
        swipe = findViewById(R.id.swipe)
        errorBox = findViewById(R.id.errorBox)
        errorText = findViewById(R.id.errorText)

        with(web.settings) {
            javaScriptEnabled = true
            domStorageEnabled = true
            databaseEnabled = true
            useWideViewPort = true
            loadWithOverviewMode = true
            builtInZoomControls = false
            mediaPlaybackRequiresUserGesture = false
            cacheMode = android.webkit.WebSettings.LOAD_DEFAULT
            mixedContentMode = android.webkit.WebSettings.MIXED_CONTENT_ALWAYS_ALLOW
        }
        web.setBackgroundColor(getColor(R.color.bg))
        web.webViewClient = object : WebViewClient() {
            override fun onReceivedHttpAuthRequest(view: WebView, handler: HttpAuthHandler, host: String, realm: String) {
                if (password.isNotEmpty()) handler.proceed("admin", password) else { handler.cancel(); showSetup() }
            }
            override fun onReceivedSslError(view: WebView, handler: SslErrorHandler, error: SslError) { handler.proceed() }
            override fun shouldOverrideUrlLoading(view: WebView, request: WebResourceRequest): Boolean {
                val target = request.url
                val base = android.net.Uri.parse(url)
                return if (target.host == base.host) false else { startActivity(android.content.Intent(android.content.Intent.ACTION_VIEW, target)); true }
            }
            override fun onPageStarted(view: WebView, url: String?, favicon: Bitmap?) { errorBox.visibility = View.GONE }
            override fun onPageFinished(view: WebView, url: String?) { swipe.isRefreshing = false }
            override fun onReceivedError(view: WebView, request: WebResourceRequest, error: WebResourceError) {
                if (request.isForMainFrame) {
                    swipe.isRefreshing = false
                    errorText.text = "${error.description}\n$url"
                    errorBox.visibility = View.VISIBLE
                }
            }
        }
        swipe.setColorSchemeColors(getColor(R.color.accent))
        swipe.setProgressBackgroundColorSchemeColor(getColor(R.color.card))
        swipe.setOnRefreshListener { web.reload() }
        findViewById<View>(R.id.retry).setOnClickListener { load() }
        findViewById<View>(R.id.openSettings).setOnClickListener { showSetup() }
        findViewById<FloatingActionButton>(R.id.fabSettings).setOnClickListener { showSetup() }

        onBackPressedDispatcher.addCallback(this, object : OnBackPressedCallback(true) {
            override fun handleOnBackPressed() { if (web.canGoBack()) web.goBack() else finish() }
        })

        if (url.isEmpty()) showSetup() else load()
    }

    private fun load() {
        errorBox.visibility = View.GONE
        web.loadUrl(url)
    }

    private fun showSetup() {
        val view = layoutInflater.inflate(R.layout.dialog_setup, null)
        val urlField = view.findViewById<EditText>(R.id.url)
        val passField = view.findViewById<EditText>(R.id.password)
        urlField.setText(url)
        passField.setText(password)
        MaterialAlertDialogBuilder(this)
            .setTitle(R.string.setup_title)
            .setView(view)
            .setCancelable(url.isNotEmpty())
            .setPositiveButton(R.string.setup_save) { _, _ ->
                var u = urlField.text.toString().trim()
                if (u.isNotEmpty() && !u.startsWith("http")) u = "http://$u"
                prefs.edit().putString("url", u.trimEnd('/')).putString("password", passField.text.toString()).apply()
                if (u.isNotEmpty()) load() else showSetup()
            }
            .show()
    }

    override fun onResume() { super.onResume(); if (url.isNotEmpty() && web.url == null) load() }
}
