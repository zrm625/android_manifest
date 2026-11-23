package river.qa;

import android.app.Activity;
import android.content.Intent;
import android.content.res.Configuration;
import android.os.Bundle;
import android.view.WindowManager;
import android.widget.TextView;

import java.util.ArrayList;
import java.util.List;

/** The helper activity the checks bring to the front. Records resume state
 *  and configuration changes (for the rotation check). */
public class MainActivity extends Activity {
    static volatile boolean resumed;
    static volatile MainActivity current;
    static final List<Integer> rotations = new ArrayList<>();

    @Override
    protected void onCreate(Bundle b) {
        super.onCreate(b);
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON
                | WindowManager.LayoutParams.FLAG_TURN_SCREEN_ON
                | WindowManager.LayoutParams.FLAG_SHOW_WHEN_LOCKED);
        setShowWhenLocked(true);
        setTurnScreenOn(true);
        TextView t = new TextView(this);
        t.setText("river-qa");
        t.setTextSize(32);
        setContentView(t);
        current = this;
    }

    @Override
    protected void onResume() {
        super.onResume();
        current = this;
        resumed = true;
    }

    @Override
    protected void onPause() {
        resumed = false;
        super.onPause();
    }

    @Override
    public void onConfigurationChanged(Configuration c) {
        super.onConfigurationChanged(c);
        synchronized (rotations) {
            rotations.add(getDisplay().getRotation());
            rotations.notifyAll();
        }
    }

    /** For the Back check: A starts child B. */
    public void startChild() {
        startActivity(new Intent(this, ChildActivity.class));
    }
}
