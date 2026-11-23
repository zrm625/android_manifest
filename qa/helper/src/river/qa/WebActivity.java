package river.qa;

import android.app.Activity;
import android.os.Bundle;
import android.webkit.WebChromeClient;
import android.webkit.WebView;
import android.webkit.WebViewClient;

/** A WebView with JavaScript on; loads the URL it is started with. */
public class WebActivity extends Activity {
    static volatile String title = "";
    static volatile boolean resumed;

    @Override
    protected void onCreate(Bundle b) {
        super.onCreate(b);
        title = "";
        WebView w = new WebView(this);
        w.getSettings().setJavaScriptEnabled(true);
        w.setWebViewClient(new WebViewClient());
        w.setWebChromeClient(new WebChromeClient() {
            @Override
            public void onReceivedTitle(WebView v, String t) {
                title = t == null ? "" : t;
            }
        });
        setContentView(w);
        w.loadUrl(getIntent().getStringExtra("url"));
    }

    @Override
    protected void onResume() {
        super.onResume();
        resumed = true;
    }

    @Override
    protected void onPause() {
        resumed = false;
        super.onPause();
    }
}
