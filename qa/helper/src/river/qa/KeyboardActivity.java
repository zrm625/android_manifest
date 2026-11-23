package river.qa;

import android.app.Activity;
import android.content.Context;
import android.os.Bundle;
import android.view.WindowManager;
import android.view.inputmethod.EditorInfo;
import android.view.inputmethod.InputConnection;
import android.view.inputmethod.InputConnectionWrapper;
import android.view.inputmethod.InputMethodManager;
import android.widget.EditText;
import android.widget.LinearLayout;

import java.util.ArrayList;
import java.util.List;

/** An EditText whose InputConnection logs what the IME commits. */
public class KeyboardActivity extends Activity {
    static volatile KeyboardActivity current;
    static volatile boolean resumed;
    static final List<String> log = new ArrayList<>();
    LoggingEditText field;

    static class LoggingEditText extends EditText {
        LoggingEditText(Context c) { super(c); }

        @Override
        public InputConnection onCreateInputConnection(EditorInfo info) {
            InputConnection ic = super.onCreateInputConnection(info);
            if (ic == null) return null;
            return new InputConnectionWrapper(ic, false) {
                @Override
                public boolean commitText(CharSequence text, int pos) {
                    synchronized (log) { log.add("commit:" + text); }
                    return super.commitText(text, pos);
                }

                @Override
                public boolean setComposingText(CharSequence text, int pos) {
                    synchronized (log) { log.add("compose:" + text); }
                    return super.setComposingText(text, pos);
                }
            };
        }
    }

    @Override
    protected void onCreate(Bundle b) {
        super.onCreate(b);
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        LinearLayout l = new LinearLayout(this);
        l.setOrientation(LinearLayout.VERTICAL);
        field = new LoggingEditText(this);
        field.setSingleLine(true);
        field.setTextSize(28);
        field.setInputType(android.text.InputType.TYPE_CLASS_TEXT
                | android.text.InputType.TYPE_TEXT_FLAG_NO_SUGGESTIONS);
        l.addView(field);
        setContentView(l);
        current = this;
    }

    @Override
    protected void onResume() {
        super.onResume();
        resumed = true;
        field.requestFocus();
        field.postDelayed(() -> {
            InputMethodManager imm = getSystemService(InputMethodManager.class);
            imm.showSoftInput(field, InputMethodManager.SHOW_IMPLICIT);
        }, 300);
    }

    @Override
    protected void onPause() {
        resumed = false;
        super.onPause();
    }

    String text() {
        return field.getText().toString();
    }
}
