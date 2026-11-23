package river.qa;

import android.app.Activity;
import android.app.Instrumentation;
import android.app.UiAutomation;
import android.accessibilityservice.AccessibilityServiceInfo;
import android.bluetooth.BluetoothAdapter;
import android.bluetooth.BluetoothDevice;
import android.bluetooth.BluetoothManager;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.graphics.Bitmap;
import android.graphics.BitmapFactory;
import android.graphics.ImageFormat;
import android.hardware.Sensor;
import android.hardware.SensorEvent;
import android.hardware.SensorEventListener;
import android.hardware.SensorManager;
import android.hardware.camera2.CameraAccessException;
import android.hardware.camera2.CameraCaptureSession;
import android.hardware.camera2.CameraCharacteristics;
import android.hardware.camera2.CameraDevice;
import android.hardware.camera2.CameraManager;
import android.hardware.camera2.CaptureRequest;
import android.hardware.camera2.CaptureResult;
import android.hardware.camera2.TotalCaptureResult;
import android.hardware.camera2.params.OutputConfiguration;
import android.hardware.camera2.params.SessionConfiguration;
import android.hardware.camera2.params.StreamConfigurationMap;
import android.hardware.fingerprint.FingerprintManager;
import android.location.GnssStatus;
import android.location.LocationListener;
import android.location.LocationManager;
import android.media.AudioAttributes;
import android.media.AudioDeviceInfo;
import android.media.AudioFormat;
import android.media.AudioManager;
import android.media.AudioRecord;
import android.media.AudioTrack;
import android.media.Image;
import android.media.ImageReader;
import android.media.MediaCodec;
import android.media.MediaCodecInfo;
import android.media.MediaCodecList;
import android.media.MediaExtractor;
import android.media.MediaFormat;
import android.media.MediaRecorder;
import android.net.ConnectivityManager;
import android.net.LinkAddress;
import android.net.LinkProperties;
import android.net.Network;
import android.net.NetworkCapabilities;
import android.net.NetworkRequest;
import android.os.Bundle;
import android.os.Handler;
import android.os.HandlerThread;
import android.os.SystemClock;
import android.os.VibrationEffect;
import android.os.Vibrator;
import android.os.VibratorManager;
import android.provider.AlarmClock;
import android.util.Range;
import android.util.Size;
import android.view.Surface;
import android.view.accessibility.AccessibilityWindowInfo;

import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.io.PrintWriter;
import java.io.StringWriter;
import java.lang.reflect.Method;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.ByteBuffer;
import java.time.Duration;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.Calendar;
import java.util.Collections;
import java.util.List;
import java.util.Locale;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.Executor;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicReference;

/**
 * The phone-qa helper. One test per instrumentation run:
 *
 *   am instrument -w -r -e test NAME [-e key value ...] river.qa/.QaInstrumentation
 *
 * The helper measures and reports; the host applies every pass rule. A run
 * ends with a terminal result bundle: result=DONE plus measurements, or
 * result=ERROR plus the exception. Tests that need the foreground send a
 * status with stage=NAME once the helper activity is resumed, then wait for
 * the host's go broadcast (river.qa.GO, extra stage=NAME) after the host has
 * checked `dumpsys activity top` itself.
 */
public class QaInstrumentation extends Instrumentation {
    static final String PKG = "river.qa";
    Bundle args;
    Context ctx;
    HandlerThread thread;
    Handler bg;
    final Object goLock = new Object();
    final List<String> goStages = new ArrayList<>();

    @Override
    public void onCreate(Bundle arguments) {
        super.onCreate(arguments);
        args = arguments;
        start();
    }

    @Override
    public void onStart() {
        super.onStart();
        ctx = getTargetContext();
        thread = new HandlerThread("river-qa");
        thread.start();
        bg = new Handler(thread.getLooper());
        BroadcastReceiver go = new BroadcastReceiver() {
            @Override
            public void onReceive(Context c, Intent i) {
                synchronized (goLock) {
                    goStages.add(String.valueOf(i.getStringExtra("stage")));
                    goLock.notifyAll();
                }
            }
        };
        ctx.registerReceiver(go, new IntentFilter("river.qa.GO"), null, bg, Context.RECEIVER_EXPORTED);
        Bundle out = new Bundle();
        String test = args.getString("test", "ping");
        try {
            run(test, out);
            out.putString("result", "DONE");
        } catch (Throwable t) {
            out.putString("result", "ERROR");
            out.putString("error", t.toString());
            StringWriter sw = new StringWriter();
            t.printStackTrace(new PrintWriter(sw));
            out.putString("stack", sw.toString().replace('\n', '|'));
        }
        out.putString("test", test);
        finish(Activity.RESULT_OK, out);
    }

    // ---------------------------------------------------------------- plumbing

    String arg(String k, String d) {
        String v = args.getString(k);
        return v == null ? d : v;
    }

    void status(String key, String value) {
        Bundle b = new Bundle();
        b.putString(key, value);
        sendStatus(1, b);
    }

    /** Tell the host the helper is in front, and wait for its go. */
    void ready(String stage) throws Exception {
        status("stage", stage);
        long end = SystemClock.elapsedRealtime() + 60000;
        synchronized (goLock) {
            while (!goStages.contains(stage)) {
                long left = end - SystemClock.elapsedRealtime();
                if (left <= 0) throw new IllegalStateException("host never confirmed stage " + stage);
                goLock.wait(left);
            }
        }
    }

    MainActivity startMain() throws Exception {
        Intent i = new Intent(Intent.ACTION_MAIN).setClassName(PKG, PKG + ".MainActivity")
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_CLEAR_TASK);
        Activity a = startActivitySync(i);
        waitForIdleSync();
        long end = SystemClock.elapsedRealtime() + 10000;
        while (!MainActivity.resumed && SystemClock.elapsedRealtime() < end) Thread.sleep(50);
        if (!MainActivity.resumed) throw new IllegalStateException("helper activity not resumed");
        return (MainActivity) a;
    }

    void upload(String name, byte[] data) throws Exception {
        String base = arg("upload", null);
        if (base == null) return;
        HttpURLConnection c = (HttpURLConnection) new URL(base + "/" + name).openConnection();
        c.setRequestMethod("POST");
        c.setDoOutput(true);
        c.setFixedLengthStreamingMode(data.length);
        c.setConnectTimeout(5000);
        c.setReadTimeout(20000);
        try (OutputStream o = c.getOutputStream()) {
            o.write(data);
        }
        int code = c.getResponseCode();
        c.disconnect();
        if (code != 200) throw new IllegalStateException("upload " + name + " got HTTP " + code);
    }

    static byte[] readAll(InputStream in) throws Exception {
        ByteArrayOutputStream b = new ByteArrayOutputStream();
        byte[] buf = new byte[16384];
        int n;
        while ((n = in.read(buf)) > 0) b.write(buf, 0, n);
        return b.toByteArray();
    }

    static String join(List<?> l) {
        StringBuilder sb = new StringBuilder();
        for (Object o : l) {
            if (sb.length() > 0) sb.append(',');
            sb.append(o);
        }
        return sb.toString();
    }

    Executor exec() {
        return r -> bg.post(r);
    }

    // ---------------------------------------------------------------- dispatch

    void run(String test, Bundle out) throws Exception {
        switch (test) {
            case "ping": out.putString("sdk", String.valueOf(android.os.Build.VERSION.SDK_INT)); break;
            case "wifi": wifi(out); break;
            case "gps": gps(out); break;
            case "bt_scan_mode": btScanMode(out); break;
            case "bt_set_scan_mode": btSetScanMode(out); break;
            case "bt_scan": btScan(out); break;
            case "bt_accept_pair": btAcceptPair(out); break;
            case "bt_unbond": btUnbond(out); break;
            case "bt_bond_state": btBondState(out); break;
            case "camera_list": cameraList(out); break;
            case "camera_live": cameraLive(out); break;
            case "camera_photo": cameraPhoto(out); break;
            case "audio": audio(out); break;
            case "fingerprint": fingerprint(out); break;
            case "sensors": sensors(out); break;
            case "vibrate": vibrate(out); break;
            case "foreground": startMain(); ready(arg("stage", "foreground")); break;
            case "rotate": rotate(out); break;
            case "back": back(out); break;
            case "alarm_set": alarmSet(out); break;
            case "alarm_dismiss": alarmDismiss(out); break;
            case "keyboard": keyboard(out); break;
            case "webview": webview(out); break;
            case "video": video(out); break;
            default: throw new IllegalArgumentException("unknown test " + test);
        }
    }

    // ---------------------------------------------------------------- Wi-Fi

    void wifi(Bundle out) throws Exception {
        startMain();
        ready("wifi");
        ConnectivityManager cm = ctx.getSystemService(ConnectivityManager.class);
        boolean bind = !"0".equals(arg("bind", "1"));
        AtomicReference<Network> net = new AtomicReference<>();
        CountDownLatch got = new CountDownLatch(1);
        ConnectivityManager.NetworkCallback cb = new ConnectivityManager.NetworkCallback() {
            @Override
            public void onAvailable(Network n) {
                net.set(n);
                got.countDown();
            }
        };
        NetworkRequest req = new NetworkRequest.Builder()
                .addTransportType(NetworkCapabilities.TRANSPORT_WIFI)
                .addCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET).build();
        cm.requestNetwork(req, cb, bg);
        try {
            if (!got.await(20, TimeUnit.SECONDS)) {
                out.putString("network", "none");
                return;
            }
            Network n = net.get();
            out.putString("network", n.toString());
            LinkProperties lp = cm.getLinkProperties(n);
            if (lp != null) {
                for (LinkAddress la : lp.getLinkAddresses()) {
                    if (la.getAddress() instanceof java.net.Inet4Address)
                        out.putString("wifi_ip", la.getAddress().getHostAddress());
                }
                out.putString("iface", String.valueOf(lp.getInterfaceName()));
            }
            fetch(out, "lan", arg("lan_url", null), bind ? n : null);
            fetch(out, "gen", arg("gen_url", null), bind ? n : null);
        } finally {
            cm.unregisterNetworkCallback(cb);
        }
    }

    void fetch(Bundle out, String key, String url, Network n) {
        if (url == null) return;
        try {
            HttpURLConnection c = (HttpURLConnection) (n != null ? n.openConnection(new URL(url))
                    : new URL(url).openConnection());
            c.setConnectTimeout(10000);
            c.setReadTimeout(10000);
            c.setUseCaches(false);
            c.setInstanceFollowRedirects(false);
            int code = c.getResponseCode();
            out.putString(key + "_code", String.valueOf(code));
            InputStream in = code < 400 ? c.getInputStream() : c.getErrorStream();
            String body = in == null ? "" : new String(readAll(in), "UTF-8");
            out.putString(key + "_body", body.length() > 300 ? body.substring(0, 300) : body);
            c.disconnect();
        } catch (Exception e) {
            out.putString(key + "_error", e.toString());
        }
    }

    // ---------------------------------------------------------------- GPS

    void gps(Bundle out) throws Exception {
        startMain();
        ready("gps");
        LocationManager lm = ctx.getSystemService(LocationManager.class);
        out.putString("location_enabled", String.valueOf(lm.isLocationEnabled()));
        final long t0 = SystemClock.elapsedRealtime();
        final long[] started = {-1};
        final int[] callbacks = {0};
        final float[] maxCn0 = {0};
        final int[] sats = {0};
        final long[] firstCn0 = {-1};
        CountDownLatch done = new CountDownLatch(1);
        GnssStatus.Callback gcb = new GnssStatus.Callback() {
            @Override
            public void onStarted() {
                started[0] = SystemClock.elapsedRealtime() - t0;
            }

            @Override
            public void onSatelliteStatusChanged(GnssStatus s) {
                callbacks[0]++;
                sats[0] = Math.max(sats[0], s.getSatelliteCount());
                for (int i = 0; i < s.getSatelliteCount(); i++) {
                    if (s.getCn0DbHz(i) > maxCn0[0]) maxCn0[0] = s.getCn0DbHz(i);
                    if (s.getCn0DbHz(i) > 0 && firstCn0[0] < 0) {
                        firstCn0[0] = SystemClock.elapsedRealtime() - t0;
                        done.countDown();
                    }
                }
            }
        };
        LocationListener ll = loc -> { };
        lm.registerGnssStatusCallback(exec(), gcb);
        lm.requestLocationUpdates(LocationManager.GPS_PROVIDER, 1000, 0, ll, thread.getLooper());
        try {
            done.await(Long.parseLong(arg("timeout_s", "120")), TimeUnit.SECONDS);
        } finally {
            lm.removeUpdates(ll);
            lm.unregisterGnssStatusCallback(gcb);
        }
        out.putString("started_ms", String.valueOf(started[0]));
        out.putString("status_callbacks", String.valueOf(callbacks[0]));
        out.putString("max_cn0", String.valueOf(maxCn0[0]));
        out.putString("satellites", String.valueOf(sats[0]));
        out.putString("first_cn0_ms", String.valueOf(firstCn0[0]));
    }

    // ---------------------------------------------------------------- Bluetooth

    BluetoothAdapter adapter() {
        return ctx.getSystemService(BluetoothManager.class).getAdapter();
    }

    void adopt() {
        getUiAutomation().adoptShellPermissionIdentity("android.permission.BLUETOOTH_PRIVILEGED",
                "android.permission.BLUETOOTH_CONNECT", "android.permission.BLUETOOTH_SCAN",
                "android.permission.BLUETOOTH_ADVERTISE");
    }

    void drop() {
        getUiAutomation().dropShellPermissionIdentity();
    }

    void btScanMode(Bundle out) {
        BluetoothAdapter a = adapter();
        out.putString("state", String.valueOf(a.getState()));
        out.putString("scan_mode", String.valueOf(a.getScanMode()));
    }

    void btSetScanMode(Bundle out) throws Exception {
        BluetoothAdapter a = adapter();
        int mode = Integer.parseInt(arg("mode", "21"));
        long timeout = Long.parseLong(arg("timeout_s", "120"));
        adopt();
        try {
            if (mode == BluetoothAdapter.SCAN_MODE_CONNECTABLE_DISCOVERABLE) {
                Method t = BluetoothAdapter.class.getMethod("setDiscoverableTimeout", Duration.class);
                out.putString("timeout_rc", String.valueOf(t.invoke(a, Duration.ofSeconds(timeout))));
            }
            Method m = BluetoothAdapter.class.getMethod("setScanMode", int.class);
            out.putString("rc", String.valueOf(m.invoke(a, mode)));
        } finally {
            drop();
        }
        long end = SystemClock.elapsedRealtime() + 5000;
        while (a.getScanMode() != mode && SystemClock.elapsedRealtime() < end) Thread.sleep(100);
        out.putString("scan_mode", String.valueOf(a.getScanMode()));
    }

    void btScan(Bundle out) throws Exception {
        startMain();
        ready("bt_scan");
        String want = arg("addr", "").toUpperCase(Locale.ROOT);
        BluetoothAdapter a = adapter();
        final List<String> seen = Collections.synchronizedList(new ArrayList<>());
        CountDownLatch found = new CountDownLatch(1);
        BroadcastReceiver r = new BroadcastReceiver() {
            @Override
            public void onReceive(Context c, Intent i) {
                BluetoothDevice d = i.getParcelableExtra(BluetoothDevice.EXTRA_DEVICE, BluetoothDevice.class);
                if (d == null) return;
                String addr = d.getAddress().toUpperCase(Locale.ROOT);
                seen.add(addr);
                if (addr.equals(want)) found.countDown();
            }
        };
        ctx.registerReceiver(r, new IntentFilter(BluetoothDevice.ACTION_FOUND), null, bg, Context.RECEIVER_EXPORTED);
        try {
            if (a.isDiscovering()) a.cancelDiscovery();
            out.putString("start_rc", String.valueOf(a.startDiscovery()));
            found.await(Long.parseLong(arg("timeout_s", "30")), TimeUnit.SECONDS);
        } finally {
            a.cancelDiscovery();
            ctx.unregisterReceiver(r);
        }
        out.putString("found", String.valueOf(found.getCount() == 0));
        out.putString("seen", String.valueOf(new java.util.HashSet<>(seen).size()));
    }

    void btAcceptPair(Bundle out) throws Exception {
        String want = arg("addr", "").toUpperCase(Locale.ROOT);
        BluetoothAdapter a = adapter();
        final String[] variant = {"none"};
        final boolean[] confirmed = {false};
        CountDownLatch bonded = new CountDownLatch(1);
        BroadcastReceiver pr = new BroadcastReceiver() {
            @Override
            public void onReceive(Context c, Intent i) {
                BluetoothDevice d = i.getParcelableExtra(BluetoothDevice.EXTRA_DEVICE, BluetoothDevice.class);
                int v = i.getIntExtra(BluetoothDevice.EXTRA_PAIRING_VARIANT, -1);
                variant[0] = String.valueOf(v);
                // Accept only forge, and only CONSENT (3; also Just Works).
                if (d != null && d.getAddress().toUpperCase(Locale.ROOT).equals(want) && v == 3) {
                    adopt();
                    try {
                        confirmed[0] = d.setPairingConfirmation(true);
                    } finally {
                        drop();
                    }
                    if (isOrderedBroadcast()) abortBroadcast();
                }
            }
        };
        BroadcastReceiver br = new BroadcastReceiver() {
            @Override
            public void onReceive(Context c, Intent i) {
                BluetoothDevice d = i.getParcelableExtra(BluetoothDevice.EXTRA_DEVICE, BluetoothDevice.class);
                int s = i.getIntExtra(BluetoothDevice.EXTRA_BOND_STATE, -1);
                if (d != null && d.getAddress().toUpperCase(Locale.ROOT).equals(want)
                        && s == BluetoothDevice.BOND_BONDED) bonded.countDown();
            }
        };
        IntentFilter pf = new IntentFilter(BluetoothDevice.ACTION_PAIRING_REQUEST);
        pf.setPriority(IntentFilter.SYSTEM_HIGH_PRIORITY - 1);
        ctx.registerReceiver(pr, pf, null, bg, Context.RECEIVER_EXPORTED);
        ctx.registerReceiver(br, new IntentFilter(BluetoothDevice.ACTION_BOND_STATE_CHANGED), null, bg,
                Context.RECEIVER_EXPORTED);
        try {
            status("stage", "pair-ready");
            long end = SystemClock.elapsedRealtime() + 1000 * Long.parseLong(arg("timeout_s", "60"));
            boolean fallbackTried = false;
            while (bonded.getCount() > 0 && SystemClock.elapsedRealtime() < end) {
                bonded.await(500, TimeUnit.MILLISECONDS);
                BluetoothDevice d = a.getRemoteDevice(want);
                // If the pairing broadcast never reached us (sent only to
                // Settings), confirm the bonding device directly.
                if (!confirmed[0] && !fallbackTried && d.getBondState() == BluetoothDevice.BOND_BONDING) {
                    Thread.sleep(1500);
                    if (!confirmed[0] && d.getBondState() == BluetoothDevice.BOND_BONDING) {
                        fallbackTried = true;
                        adopt();
                        try {
                            confirmed[0] = d.setPairingConfirmation(true);
                            out.putString("fallback_confirm", String.valueOf(confirmed[0]));
                        } finally {
                            drop();
                        }
                    }
                }
                if (d.getBondState() == BluetoothDevice.BOND_BONDED) bonded.countDown();
            }
        } finally {
            ctx.unregisterReceiver(pr);
            ctx.unregisterReceiver(br);
        }
        out.putString("variant", variant[0]);
        out.putString("confirmed", String.valueOf(confirmed[0]));
        adopt();
        try {
            out.putString("bond_state", String.valueOf(a.getRemoteDevice(want).getBondState()));
        } finally {
            drop();
        }
    }

    void btUnbond(Bundle out) throws Exception {
        String want = arg("addr", "").toUpperCase(Locale.ROOT);
        BluetoothDevice d = adapter().getRemoteDevice(want);
        adopt();
        try {
            if (d.getBondState() != BluetoothDevice.BOND_NONE) {
                Method m = BluetoothDevice.class.getMethod("removeBond");
                out.putString("rc", String.valueOf(m.invoke(d)));
            }
            long end = SystemClock.elapsedRealtime() + 15000;
            while (d.getBondState() != BluetoothDevice.BOND_NONE && SystemClock.elapsedRealtime() < end)
                Thread.sleep(200);
            out.putString("bond_state", String.valueOf(d.getBondState()));
        } finally {
            drop();
        }
    }

    void btBondState(Bundle out) {
        adopt();
        try {
            out.putString("bond_state", String.valueOf(adapter().getRemoteDevice(
                    arg("addr", "").toUpperCase(Locale.ROOT)).getBondState()));
        } finally {
            drop();
        }
    }

    // ---------------------------------------------------------------- cameras

    void cameraList(Bundle out) throws Exception {
        CameraManager cm = ctx.getSystemService(CameraManager.class);
        out.putString("ids", String.join(",", cm.getCameraIdList()));
    }

    CameraDevice open(CameraManager cm, String id) throws Exception {
        AtomicReference<CameraDevice> ref = new AtomicReference<>();
        AtomicReference<String> err = new AtomicReference<>();
        CountDownLatch l = new CountDownLatch(1);
        cm.openCamera(id, exec(), new CameraDevice.StateCallback() {
            @Override public void onOpened(CameraDevice c) { ref.set(c); l.countDown(); }
            @Override public void onDisconnected(CameraDevice c) { err.set("disconnected"); c.close(); l.countDown(); }
            @Override public void onError(CameraDevice c, int e) { err.set("error " + e); c.close(); l.countDown(); }
        });
        if (!l.await(10, TimeUnit.SECONDS)) throw new IllegalStateException("camera " + id + " open timed out");
        if (ref.get() == null) throw new IllegalStateException("camera " + id + " " + err.get());
        return ref.get();
    }

    CameraCaptureSession session(CameraDevice dev, List<Surface> surfaces) throws Exception {
        AtomicReference<CameraCaptureSession> ref = new AtomicReference<>();
        CountDownLatch l = new CountDownLatch(1);
        List<OutputConfiguration> outs = new ArrayList<>();
        for (Surface s : surfaces) outs.add(new OutputConfiguration(s));
        dev.createCaptureSession(new SessionConfiguration(SessionConfiguration.SESSION_REGULAR, outs, exec(),
                new CameraCaptureSession.StateCallback() {
                    @Override public void onConfigured(CameraCaptureSession s) { ref.set(s); l.countDown(); }
                    @Override public void onConfigureFailed(CameraCaptureSession s) { l.countDown(); }
                }));
        if (!l.await(10, TimeUnit.SECONDS) || ref.get() == null)
            throw new IllegalStateException("capture session failed");
        return ref.get();
    }

    static boolean manual(CameraCharacteristics ch) {
        int[] caps = ch.get(CameraCharacteristics.REQUEST_AVAILABLE_CAPABILITIES);
        if (caps == null) return false;
        for (int c : caps) if (c == CameraCharacteristics.REQUEST_AVAILABLE_CAPABILITIES_MANUAL_SENSOR) return true;
        return false;
    }

    static Size pick(Size[] sizes, int w, int h) {
        Size best = sizes[0];
        long target = (long) w * h, bestd = Long.MAX_VALUE;
        for (Size s : sizes) {
            long d = Math.abs((long) s.getWidth() * s.getHeight() - target);
            if (d < bestd) { bestd = d; best = s; }
        }
        return best;
    }

    /** Liveness: 10 YUV frames at maximum sensitivity and exposure. */
    void cameraLive(Bundle out) throws Exception {
        startMain();
        ready("camera_live");
        String id = arg("id", "0");
        int frames = Integer.parseInt(arg("frames", "10"));
        CameraManager cm = ctx.getSystemService(CameraManager.class);
        CameraCharacteristics ch = cm.getCameraCharacteristics(id);
        StreamConfigurationMap map = ch.get(CameraCharacteristics.SCALER_STREAM_CONFIGURATION_MAP);
        Size size = pick(map.getOutputSizes(ImageFormat.YUV_420_888), 640, 480);
        boolean man = manual(ch);
        out.putString("manual", String.valueOf(man));
        out.putString("size", size.toString());
        ImageReader reader = ImageReader.newInstance(size.getWidth(), size.getHeight(), ImageFormat.YUV_420_888, 4);
        final List<Long> stamps = Collections.synchronizedList(new ArrayList<>());
        final List<String> diffs = Collections.synchronizedList(new ArrayList<>());
        final List<String> means = Collections.synchronizedList(new ArrayList<>());
        final int[][] prev = {null};
        CountDownLatch got = new CountDownLatch(frames);
        reader.setOnImageAvailableListener(r -> {
            Image img = r.acquireNextImage();
            if (img == null) return;
            try {
                if (got.getCount() == 0) return;
                int[] s = sampleY(img, 64, 48);
                long sum = 0;
                for (int v : s) sum += v;
                means.add(String.format(Locale.ROOT, "%.2f", sum / (double) s.length));
                if (prev[0] != null) {
                    long d = 0;
                    for (int i = 0; i < s.length; i++) d += Math.abs(s[i] - prev[0][i]);
                    diffs.add(String.format(Locale.ROOT, "%.3f", d / (double) s.length));
                }
                prev[0] = s;
                stamps.add(img.getTimestamp());
                got.countDown();
            } finally {
                img.close();
            }
        }, bg);
        CameraDevice dev = open(cm, id);
        try {
            CameraCaptureSession ses = session(dev, Arrays.asList(reader.getSurface()));
            CaptureRequest.Builder b = dev.createCaptureRequest(CameraDevice.TEMPLATE_PREVIEW);
            b.addTarget(reader.getSurface());
            if (man) {
                Range<Integer> iso = ch.get(CameraCharacteristics.SENSOR_INFO_SENSITIVITY_RANGE);
                Range<Long> exp = ch.get(CameraCharacteristics.SENSOR_INFO_EXPOSURE_TIME_RANGE);
                long e = exp.getUpper();
                if (e >= 10_000_000L) e = (e / 10_000_000L) * 10_000_000L; // flicker-safe multiples of 10 ms
                b.set(CaptureRequest.CONTROL_AE_MODE, CaptureRequest.CONTROL_AE_MODE_OFF);
                b.set(CaptureRequest.SENSOR_SENSITIVITY, iso.getUpper());
                b.set(CaptureRequest.SENSOR_EXPOSURE_TIME, e);
                b.set(CaptureRequest.SENSOR_FRAME_DURATION, e);
                out.putString("iso", String.valueOf(iso.getUpper()));
                out.putString("exposure_ns", String.valueOf(e));
            } else {
                b.set(CaptureRequest.CONTROL_AE_MODE, CaptureRequest.CONTROL_AE_MODE_ON);
            }
            ses.setRepeatingRequest(b.build(), null, bg);
            boolean ok = got.await(Long.parseLong(arg("deadline_s", "20")), TimeUnit.SECONDS);
            ses.stopRepeating();
            ses.close();
            out.putString("complete", String.valueOf(ok));
        } finally {
            dev.close();
            reader.close();
        }
        out.putString("frames", String.valueOf(stamps.size()));
        out.putString("timestamps", join(stamps));
        out.putString("diffs", join(diffs));
        out.putString("means", join(means));
    }

    static int[] sampleY(Image img, int gw, int gh) {
        Image.Plane p = img.getPlanes()[0];
        ByteBuffer buf = p.getBuffer();
        int rs = p.getRowStride(), ps = p.getPixelStride();
        int w = img.getWidth(), h = img.getHeight();
        int[] out = new int[gw * gh];
        for (int y = 0; y < gh; y++) {
            int row = (y * h + h / 2) / gh;
            for (int x = 0; x < gw; x++) {
                int col = (x * w + w / 2) / gw;
                out[y * gw + x] = buf.get(row * rs + col * ps) & 0xff;
            }
        }
        return out;
    }

    /** Two JPEG stills at locked exposure: torch off, then torch on in the
     *  still request itself. Or one auto still (best effort, cameras 1, 2). */
    void cameraPhoto(Bundle out) throws Exception {
        startMain();
        ready("camera_photo");
        String id = arg("id", "0");
        boolean torchPair = "1".equals(arg("torch", "0"));
        CameraManager cm = ctx.getSystemService(CameraManager.class);
        CameraCharacteristics ch = cm.getCameraCharacteristics(id);
        StreamConfigurationMap map = ch.get(CameraCharacteristics.SCALER_STREAM_CONFIGURATION_MAP);
        Size js = pick(map.getOutputSizes(ImageFormat.JPEG), 1600, 1200);
        Size ys = pick(map.getOutputSizes(ImageFormat.YUV_420_888), 640, 480);
        ImageReader jpeg = ImageReader.newInstance(js.getWidth(), js.getHeight(), ImageFormat.JPEG, 2);
        ImageReader prev = ImageReader.newInstance(ys.getWidth(), ys.getHeight(), ImageFormat.YUV_420_888, 4);
        prev.setOnImageAvailableListener(r -> { Image i = r.acquireLatestImage(); if (i != null) i.close(); }, bg);
        final List<byte[]> shots = Collections.synchronizedList(new ArrayList<>());
        final CountDownLatch[] shot = {new CountDownLatch(1)};
        jpeg.setOnImageAvailableListener(r -> {
            Image i = r.acquireNextImage();
            if (i == null) return;
            ByteBuffer b = i.getPlanes()[0].getBuffer();
            byte[] d = new byte[b.remaining()];
            b.get(d);
            i.close();
            shots.add(d);
            shot[0].countDown();
        }, bg);
        boolean man = manual(ch);
        int iso = Integer.parseInt(arg("iso", "400"));
        long exp = Long.parseLong(arg("exposure_ns", "30000000"));
        CameraDevice dev = open(cm, id);
        List<String> flash = new ArrayList<>();
        try {
            CameraCaptureSession ses = session(dev, Arrays.asList(prev.getSurface(), jpeg.getSurface()));
            int n = torchPair ? 2 : 1;
            for (int k = 0; k < n; k++) {
                boolean torch = torchPair && k == 1;
                CaptureRequest.Builder p = dev.createCaptureRequest(CameraDevice.TEMPLATE_PREVIEW);
                CaptureRequest.Builder s = dev.createCaptureRequest(CameraDevice.TEMPLATE_STILL_CAPTURE);
                p.addTarget(prev.getSurface());
                s.addTarget(jpeg.getSurface());
                for (CaptureRequest.Builder b : new CaptureRequest.Builder[]{p, s}) {
                    if (man && torchPair) {
                        b.set(CaptureRequest.CONTROL_AE_MODE, CaptureRequest.CONTROL_AE_MODE_OFF);
                        b.set(CaptureRequest.SENSOR_SENSITIVITY, iso);
                        b.set(CaptureRequest.SENSOR_EXPOSURE_TIME, exp);
                        b.set(CaptureRequest.SENSOR_FRAME_DURATION, Math.max(exp, 33_333_333L));
                        b.set(CaptureRequest.CONTROL_AWB_MODE, CaptureRequest.CONTROL_AWB_MODE_DAYLIGHT);
                    }
                    b.set(CaptureRequest.FLASH_MODE, torch ? CaptureRequest.FLASH_MODE_TORCH
                            : CaptureRequest.FLASH_MODE_OFF);
                    b.set(CaptureRequest.JPEG_QUALITY, (byte) 90);
                }
                ses.setRepeatingRequest(p.build(), null, bg);
                Thread.sleep(torch ? 900 : 600);
                shot[0] = new CountDownLatch(1);
                final AtomicReference<TotalCaptureResult> res = new AtomicReference<>();
                ses.capture(s.build(), new CameraCaptureSession.CaptureCallback() {
                    @Override
                    public void onCaptureCompleted(CameraCaptureSession c, CaptureRequest r, TotalCaptureResult t) {
                        res.set(t);
                    }
                }, bg);
                if (!shot[0].await(10, TimeUnit.SECONDS)) throw new IllegalStateException("still " + k + " timed out");
                long end = SystemClock.elapsedRealtime() + 3000;
                while (res.get() == null && SystemClock.elapsedRealtime() < end) Thread.sleep(20);
                Integer fs = res.get() == null ? null : res.get().get(CaptureResult.FLASH_STATE);
                flash.add(String.valueOf(fs));
                ses.stopRepeating();
            }
            ses.close();
        } finally {
            dev.close();
            jpeg.close();
            prev.close();
        }
        out.putString("flash_states", join(flash));
        out.putString("manual", String.valueOf(man));
        for (int k = 0; k < shots.size(); k++) {
            byte[] d = shots.get(k);
            double[] st = luma(d);
            out.putString("luma" + k, String.format(Locale.ROOT, "%.2f", st[0]));
            out.putString("std" + k, String.format(Locale.ROOT, "%.2f", st[1]));
            upload("camera" + id + "-" + k + ".jpg", d);
        }
    }

    /** Mean and spatial standard deviation of full-range luma, 0-255. */
    static double[] luma(byte[] jpeg) {
        BitmapFactory.Options o = new BitmapFactory.Options();
        o.inSampleSize = 4;
        Bitmap b = BitmapFactory.decodeByteArray(jpeg, 0, jpeg.length, o);
        int w = b.getWidth(), h = b.getHeight();
        int[] px = new int[w * h];
        b.getPixels(px, 0, w, 0, 0, w, h);
        double sum = 0, sq = 0;
        for (int p : px) {
            double y = 0.299 * ((p >> 16) & 0xff) + 0.587 * ((p >> 8) & 0xff) + 0.114 * (p & 0xff);
            sum += y;
            sq += y * y;
        }
        double mean = sum / px.length;
        return new double[]{mean, Math.sqrt(Math.max(0, sq / px.length - mean * mean))};
    }

    // ---------------------------------------------------------------- audio

    void audio(Bundle out) throws Exception {
        startMain();
        ready("audio");
        AudioManager am = ctx.getSystemService(AudioManager.class);
        boolean unprocessed = "true".equals(am.getProperty(AudioManager.PROPERTY_SUPPORT_AUDIO_SOURCE_UNPROCESSED));
        int source = unprocessed ? MediaRecorder.AudioSource.UNPROCESSED : MediaRecorder.AudioSource.VOICE_RECOGNITION;
        out.putString("source", unprocessed ? "UNPROCESSED" : "VOICE_RECOGNITION");
        out.putString("music_volume", String.valueOf(am.getStreamVolume(AudioManager.STREAM_MUSIC)));
        int rate = 48000;
        int min = AudioRecord.getMinBufferSize(rate, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT);
        AudioRecord rec = new AudioRecord.Builder().setAudioSource(source)
                .setAudioFormat(new AudioFormat.Builder().setSampleRate(rate)
                        .setChannelMask(AudioFormat.CHANNEL_IN_MONO).setEncoding(AudioFormat.ENCODING_PCM_16BIT).build())
                .setBufferSizeInBytes(Math.max(min, rate)).build();
        // 1 kHz, gap, 2.5 kHz, gap, 1 kHz: 400 ms bursts, 300 ms gaps.
        int burst = rate * 4 / 10, gap = rate * 3 / 10;
        int[] freqs = {1000, 0, 2500, 0, 1000};
        int[] lens = {burst, gap, burst, gap, burst};
        int total = 0;
        for (int l : lens) total += l;
        short[] tone = new short[total];
        int pos = 0;
        double amp = Double.parseDouble(arg("amplitude", "0.5")) * 32767;
        for (int s = 0; s < freqs.length; s++) {
            for (int i = 0; i < lens[s]; i++) {
                double env = Math.min(1.0, Math.min(i, lens[s] - i) / (rate * 0.005)); // 5 ms ramps
                tone[pos++] = freqs[s] == 0 ? 0 : (short) (amp * env * Math.sin(2 * Math.PI * freqs[s] * i / rate));
            }
        }
        AudioTrack track = new AudioTrack.Builder()
                .setAudioAttributes(new AudioAttributes.Builder().setUsage(AudioAttributes.USAGE_MEDIA)
                        .setContentType(AudioAttributes.CONTENT_TYPE_MUSIC).build())
                .setAudioFormat(new AudioFormat.Builder().setSampleRate(rate)
                        .setChannelMask(AudioFormat.CHANNEL_OUT_MONO).setEncoding(AudioFormat.ENCODING_PCM_16BIT).build())
                .setTransferMode(AudioTrack.MODE_STATIC).setBufferSizeInBytes(total * 2).build();
        track.write(tone, 0, total);
        int recLen = rate * 1 + total + rate / 2;  // 1 s silence, the tones, 0.5 s tail
        short[] pcm = new short[recLen];
        int got = 0;
        int playAt = -1;
        rec.startRecording();
        try {
            while (got < recLen) {
                int n = rec.read(pcm, got, Math.min(4800, recLen - got));
                if (n <= 0) throw new IllegalStateException("AudioRecord.read returned " + n);
                got += n;
                if (playAt < 0 && got >= rate) {
                    playAt = got;
                    track.play();
                    Thread.sleep(50);
                    AudioDeviceInfo td = track.getRoutedDevice();
                    AudioDeviceInfo rd = rec.getRoutedDevice();
                    out.putString("out_device", td == null ? "null" : String.valueOf(td.getType()));
                    out.putString("in_device", rd == null ? "null" : String.valueOf(rd.getType()));
                }
            }
        } finally {
            rec.stop();
            rec.release();
            track.stop();
            track.release();
        }
        out.putString("rate", String.valueOf(rate));
        out.putString("play_at", String.valueOf(playAt));
        out.putString("samples", String.valueOf(got));
        ByteBuffer bb = ByteBuffer.allocate(got * 2).order(java.nio.ByteOrder.LITTLE_ENDIAN);
        for (int i = 0; i < got; i++) bb.putShort(pcm[i]);
        upload("audio.pcm", bb.array());
    }

    // ---------------------------------------------------------------- fingerprint, sensors, vibration

    void fingerprint(Bundle out) throws Exception {
        startMain();
        ready("fingerprint");
        FingerprintManager fm = ctx.getSystemService(FingerprintManager.class);
        out.putString("hardware_detected", String.valueOf(fm != null && fm.isHardwareDetected()));
    }

    void sensors(Bundle out) throws Exception {
        startMain();
        ready("sensors");
        SensorManager sm = ctx.getSystemService(SensorManager.class);
        Sensor acc = sm.getDefaultSensor(Sensor.TYPE_ACCELEROMETER);
        Sensor light = sm.getDefaultSensor(Sensor.TYPE_LIGHT);
        Sensor prox = sm.getDefaultSensor(Sensor.TYPE_PROXIMITY);
        final List<Double> mags = Collections.synchronizedList(new ArrayList<>());
        final List<Float> lights = Collections.synchronizedList(new ArrayList<>());
        final List<Float> proxs = Collections.synchronizedList(new ArrayList<>());
        SensorEventListener l = new SensorEventListener() {
            @Override
            public void onSensorChanged(SensorEvent e) {
                int t = e.sensor.getType();
                if (t == Sensor.TYPE_ACCELEROMETER) {
                    mags.add(Math.sqrt(e.values[0] * e.values[0] + e.values[1] * e.values[1] + e.values[2] * e.values[2]));
                } else if (t == Sensor.TYPE_LIGHT) {
                    lights.add(e.values[0]);
                } else if (t == Sensor.TYPE_PROXIMITY) {
                    proxs.add(e.values[0]);
                }
            }

            @Override
            public void onAccuracyChanged(Sensor s, int a) { }
        };
        for (Sensor s : new Sensor[]{acc, light, prox}) {
            if (s != null) sm.registerListener(l, s, SensorManager.SENSOR_DELAY_GAME, bg);
        }
        Thread.sleep(Long.parseLong(arg("ms", "3000")));
        sm.unregisterListener(l);
        out.putString("has", (acc != null) + "," + (light != null) + "," + (prox != null));
        out.putString("accel_n", String.valueOf(mags.size()));
        List<Double> sorted = new ArrayList<>(mags);
        Collections.sort(sorted);
        out.putString("accel_median", sorted.isEmpty() ? "nan" : String.valueOf(sorted.get(sorted.size() / 2)));
        out.putString("light", join(lights));
        out.putString("prox", join(proxs));
        out.putString("prox_max", prox == null ? "nan" : String.valueOf(prox.getMaximumRange()));
    }

    void vibrate(Bundle out) throws Exception {
        startMain();
        ready("vibrate");
        Vibrator v = ctx.getSystemService(VibratorManager.class).getDefaultVibrator();
        out.putString("has_vibrator", String.valueOf(v.hasVibrator()));
        out.putString("requested_at_ms", String.valueOf(System.currentTimeMillis()));
        v.vibrate(VibrationEffect.createWaveform(new long[]{0, 120, 120, 120, 120, 120}, -1));
        Thread.sleep(1200);
    }

    // ---------------------------------------------------------------- screen, back

    void rotate(Bundle out) throws Exception {
        startMain();
        synchronized (MainActivity.rotations) { MainActivity.rotations.clear(); }
        ready("rotate");
        long end = SystemClock.elapsedRealtime() + Long.parseLong(arg("timeout_s", "25")) * 1000;
        synchronized (MainActivity.rotations) {
            while (SystemClock.elapsedRealtime() < end) {
                List<Integer> r = MainActivity.rotations;
                int i90 = r.indexOf(Surface.ROTATION_90);
                if (i90 >= 0 && r.subList(i90, r.size()).contains(Surface.ROTATION_0)) break;
                MainActivity.rotations.wait(Math.max(1, end - SystemClock.elapsedRealtime()));
            }
            out.putString("rotations", join(MainActivity.rotations));
        }
    }

    /** A starts child B; the host swipes Back while this run is still alive
     *  (the app process dies when the instrumentation finishes). */
    void back(Bundle out) throws Exception {
        MainActivity a = startMain();
        runOnMainSync(a::startChild);
        waitForIdleSync();
        Thread.sleep(500);
        out.putString("child_started", "true");
        ready("back");
        Thread.sleep(500);
        out.putString("a_resumed_after", String.valueOf(MainActivity.resumed));
    }

    // ---------------------------------------------------------------- alarms

    void alarmSet(Bundle out) throws Exception {
        startMain();
        ready("alarm_set");
        int lead = Integer.parseInt(arg("lead_s", "45"));
        Calendar c = Calendar.getInstance();
        Calendar t = (Calendar) c.clone();
        t.set(Calendar.SECOND, 0);
        t.set(Calendar.MILLISECOND, 0);
        t.add(Calendar.MINUTE, 1);
        while ((t.getTimeInMillis() - c.getTimeInMillis()) < lead * 1000L) t.add(Calendar.MINUTE, 1);
        Intent i = new Intent(AlarmClock.ACTION_SET_ALARM)
                .setPackage("com.android.deskclock")
                .putExtra(AlarmClock.EXTRA_HOUR, t.get(Calendar.HOUR_OF_DAY))
                .putExtra(AlarmClock.EXTRA_MINUTES, t.get(Calendar.MINUTE))
                .putExtra(AlarmClock.EXTRA_SKIP_UI, true)
                .putExtra(AlarmClock.EXTRA_RINGTONE, AlarmClock.VALUE_RINGTONE_SILENT)
                .putExtra(AlarmClock.EXTRA_VIBRATE, false)
                .putExtra(AlarmClock.EXTRA_MESSAGE, arg("label", "river-qa"))
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
        ctx.startActivity(i);
        out.putString("now_ms", String.valueOf(c.getTimeInMillis()));
        out.putString("target_ms", String.valueOf(t.getTimeInMillis()));
        out.putString("hour", String.valueOf(t.get(Calendar.HOUR_OF_DAY)));
        out.putString("minute", String.valueOf(t.get(Calendar.MINUTE)));
        out.putString("tz", c.getTimeZone().getID());
    }

    void alarmDismiss(Bundle out) throws Exception {
        Intent i = new Intent(AlarmClock.ACTION_DISMISS_ALARM)
                .setPackage("com.android.deskclock")
                .putExtra(AlarmClock.EXTRA_ALARM_SEARCH_MODE, AlarmClock.ALARM_SEARCH_MODE_LABEL)
                .putExtra(AlarmClock.EXTRA_MESSAGE, arg("label", "river-qa"))
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
        ctx.startActivity(i);
        Thread.sleep(1500);
        out.putString("sent", "true");
    }

    // ---------------------------------------------------------------- keyboard

    void keyboard(Bundle out) throws Exception {
        UiAutomation ua = getUiAutomation();
        AccessibilityServiceInfo info = ua.getServiceInfo();
        info.flags |= AccessibilityServiceInfo.FLAG_RETRIEVE_INTERACTIVE_WINDOWS;
        ua.setServiceInfo(info);
        synchronized (KeyboardActivity.log) { KeyboardActivity.log.clear(); }
        Intent i = new Intent(Intent.ACTION_MAIN).setClassName(PKG, PKG + ".KeyboardActivity")
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_CLEAR_TASK);
        startActivitySync(i);
        waitForIdleSync();
        long end = SystemClock.elapsedRealtime() + 10000;
        boolean ime = false;
        while (!ime && SystemClock.elapsedRealtime() < end) {
            for (AccessibilityWindowInfo w : ua.getWindows()) {
                if (w.getType() == AccessibilityWindowInfo.TYPE_INPUT_METHOD) ime = true;
            }
            if (!ime) Thread.sleep(200);
        }
        out.putString("ime_window", String.valueOf(ime));
        ready("keyboard");
        String word = arg("word", "river");
        end = SystemClock.elapsedRealtime() + Long.parseLong(arg("timeout_s", "20")) * 1000;
        String text = "";
        while (SystemClock.elapsedRealtime() < end) {
            final String[] t = {""};
            runOnMainSync(() -> t[0] = KeyboardActivity.current == null ? "" : KeyboardActivity.current.text());
            text = t[0];
            if (text.trim().equals(word)) break;
            Thread.sleep(200);
        }
        out.putString("text", text);
        synchronized (KeyboardActivity.log) { out.putString("ime_log", join(KeyboardActivity.log)); }
    }

    // ---------------------------------------------------------------- WebView

    void webview(Bundle out) throws Exception {
        String url = arg("url", "");
        String token = arg("token", "");
        Intent i = new Intent(Intent.ACTION_MAIN).setClassName(PKG, PKG + ".WebActivity").putExtra("url", url)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_CLEAR_TASK);
        long t0 = SystemClock.elapsedRealtime();
        startActivitySync(i);
        ready("webview");
        long end = t0 + Long.parseLong(arg("timeout_s", "15")) * 1000;
        while (!token.equals(WebActivity.title) && SystemClock.elapsedRealtime() < end) Thread.sleep(100);
        out.putString("title", WebActivity.title);
        out.putString("elapsed_ms", String.valueOf(SystemClock.elapsedRealtime() - t0));
        Thread.sleep(1500); // let the beacon go out
    }

    // ---------------------------------------------------------------- video

    void video(Bundle out) throws Exception {
        startMain();
        ready("video");
        // Fetch the clip from the host test server into the cache.
        File clip = new File(ctx.getCacheDir(), "clip.mp4");
        HttpURLConnection c = (HttpURLConnection) new URL(arg("clip_url", "")).openConnection();
        try (InputStream in = c.getInputStream(); FileOutputStream fo = new FileOutputStream(clip)) {
            fo.write(readAll(in));
        }
        MediaExtractor ex = new MediaExtractor();
        ex.setDataSource(clip.getPath());
        int track = -1;
        MediaFormat fmt = null;
        for (int t = 0; t < ex.getTrackCount(); t++) {
            MediaFormat f = ex.getTrackFormat(t);
            if (f.getString(MediaFormat.KEY_MIME).startsWith("video/avc")) { track = t; fmt = f; }
        }
        if (track < 0) throw new IllegalStateException("no AVC track");
        ex.selectTrack(track);
        String force = arg("force_decoder", "");
        String name = null;
        boolean hw = false;
        for (MediaCodecInfo ci : new MediaCodecList(MediaCodecList.REGULAR_CODECS).getCodecInfos()) {
            if (ci.isEncoder() || ci.isAlias()) continue;
            boolean avc = false;
            for (String t : ci.getSupportedTypes()) if (t.equalsIgnoreCase("video/avc")) avc = true;
            if (!avc) continue;
            if (!force.isEmpty()) {
                if (ci.getName().equals(force)) { name = ci.getName(); hw = ci.isHardwareAccelerated(); }
                continue;
            }
            if (!ci.isHardwareAccelerated()) continue;
            if (ci.getCapabilitiesForType("video/avc").isFeatureRequired(
                    MediaCodecInfo.CodecCapabilities.FEATURE_SecurePlayback)) continue;
            name = ci.getName();
            hw = true;
            break;
        }
        if (name == null) throw new IllegalStateException("no usable AVC decoder" + (force.isEmpty() ? "" : " " + force));
        out.putString("decoder", name);
        out.putString("hardware", String.valueOf(hw));
        int w = fmt.getInteger(MediaFormat.KEY_WIDTH), h = fmt.getInteger(MediaFormat.KEY_HEIGHT);
        ImageReader reader = ImageReader.newInstance(w, h, ImageFormat.YUV_420_888, 4);
        String[] want = arg("sample", "0,30,60,90,120,149").split(",");
        final List<Integer> wantIdx = new ArrayList<>();
        for (String s : want) wantIdx.add(Integer.parseInt(s.trim()));
        final int[] count = {0};
        final List<String> errs = Collections.synchronizedList(new ArrayList<>());
        reader.setOnImageAvailableListener(r -> {
            Image img = r.acquireNextImage();
            if (img == null) return;
            try {
                int idx = count[0]++;
                if (wantIdx.contains(idx)) {
                    Image.Plane p = img.getPlanes()[0];
                    ByteBuffer b = p.getBuffer();
                    int rs = p.getRowStride(), ps = p.getPixelStride();
                    byte[] y = new byte[w * h];
                    for (int row = 0; row < h; row++)
                        for (int col = 0; col < w; col++) y[row * w + col] = b.get(row * rs + col * ps);
                    upload("frame-" + idx + ".gray", y);
                }
            } catch (Exception e) {
                errs.add(e.toString());
            } finally {
                img.close();
            }
        }, bg);
        boolean surface = "surface".equals(arg("output", "buffer"));
        out.putString("output", surface ? "surface" : "buffer");
        MediaCodec dec = MediaCodec.createByCodecName(name);
        if (!surface) {
            fmt.setInteger(MediaFormat.KEY_COLOR_FORMAT,
                    MediaCodecInfo.CodecCapabilities.COLOR_FormatYUV420Flexible);
        }
        dec.configure(fmt, surface ? reader.getSurface() : null, null, 0);
        dec.start();
        MediaCodec.BufferInfo bi = new MediaCodec.BufferInfo();
        boolean inEos = false, outEos = false;
        int rendered = 0;
        long end = SystemClock.elapsedRealtime() + 60000;
        try {
            while (!outEos && SystemClock.elapsedRealtime() < end) {
                if (!inEos) {
                    int ii = dec.dequeueInputBuffer(10000);
                    if (ii >= 0) {
                        ByteBuffer ib = dec.getInputBuffer(ii);
                        int n = ex.readSampleData(ib, 0);
                        if (n < 0) {
                            dec.queueInputBuffer(ii, 0, 0, 0, MediaCodec.BUFFER_FLAG_END_OF_STREAM);
                            inEos = true;
                        } else {
                            dec.queueInputBuffer(ii, 0, n, ex.getSampleTime(), 0);
                            ex.advance();
                        }
                    }
                }
                int oi = dec.dequeueOutputBuffer(bi, 10000);
                if (oi >= 0 && !surface) {
                    if ((bi.flags & MediaCodec.BUFFER_FLAG_END_OF_STREAM) != 0) outEos = true;
                    if (bi.size > 0) {
                        int idx = rendered++;
                        count[0]++;
                        if (wantIdx.contains(idx)) {
                            Image img = dec.getOutputImage(oi);
                            android.graphics.Rect crop = img.getCropRect();
                            Image.Plane p = img.getPlanes()[0];
                            ByteBuffer b = p.getBuffer();
                            int rs = p.getRowStride(), ps = p.getPixelStride();
                            byte[] y = new byte[w * h];
                            for (int row = 0; row < h; row++)
                                for (int col = 0; col < w; col++)
                                    y[row * w + col] = b.get((row + crop.top) * rs + (col + crop.left) * ps);
                            img.close();
                            upload("frame-" + idx + ".gray", y);
                        }
                    }
                    dec.releaseOutputBuffer(oi, false);
                } else if (oi >= 0) {
                    if ((bi.flags & MediaCodec.BUFFER_FLAG_END_OF_STREAM) != 0) outEos = true;
                    boolean render = bi.size > 0;
                    dec.releaseOutputBuffer(oi, render);
                    if (render) {
                        rendered++;
                        // Pace rendering so the ImageReader never drops a frame.
                        long wait = SystemClock.elapsedRealtime() + 2000;
                        while (count[0] < rendered && SystemClock.elapsedRealtime() < wait) Thread.sleep(2);
                    }
                }
            }
        } finally {
            dec.stop();
            dec.release();
            ex.release();
        }
        Thread.sleep(500);
        reader.close();
        out.putString("rendered", String.valueOf(rendered));
        out.putString("received", String.valueOf(count[0]));
        out.putString("width", String.valueOf(w));
        out.putString("height", String.valueOf(h));
        if (!errs.isEmpty()) out.putString("upload_errors", join(errs));
    }
}
