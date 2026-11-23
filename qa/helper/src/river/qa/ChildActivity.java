package river.qa;

import android.app.Activity;
import android.os.Bundle;
import android.widget.TextView;

/** Child B for the Back check. */
public class ChildActivity extends Activity {
    @Override
    protected void onCreate(Bundle b) {
        super.onCreate(b);
        TextView t = new TextView(this);
        t.setText("river-qa child");
        t.setTextSize(32);
        setContentView(t);
    }
}
