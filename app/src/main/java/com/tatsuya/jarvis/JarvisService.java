package com.tatsuya.jarvis;

import android.annotation.SuppressLint;
import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.ServiceInfo;
import android.graphics.Canvas;
import android.graphics.Color;
import android.graphics.Paint;
import android.graphics.PixelFormat;
import android.graphics.RadialGradient;
import android.graphics.Shader;
import android.Manifest;
import android.content.pm.PackageManager;
import android.media.AudioManager;
import android.media.ToneGenerator;
import android.os.Build;
import android.os.Handler;
import android.os.IBinder;
import android.os.Looper;
import android.os.SystemClock;
import android.os.VibrationEffect;
import android.os.Vibrator;
import android.provider.Settings;
import android.util.DisplayMetrics;
import android.view.Gravity;
import android.view.MotionEvent;
import android.view.View;
import android.view.WindowManager;

import java.util.List;

/**
 * ジャービスの常駐係。
 * ・通知欄に「話しかける」ボタン付きの通知を出し続ける
 * ・（許可があれば）他のアプリの上に浮かぶ光の玉を出す。タップで話しかけ、ドラッグで移動、長押しで隠す
 * ・（オンにしていれば）呼びかけ（「ジャービス」など）を待ち、聞こえたらすぐ聞き取りを始める。
 *   マイクの音は端末の中だけで調べ、録音もしない。アプリの画面が出ている間は画面側がマイクを使うので休む
 */
public class JarvisService extends Service {

    static final String CHANNEL = "jarvis_resident";
    static final String ACTION_STOP = "com.tatsuya.jarvis.STOP";
    static final String ACTION_ORB = "com.tatsuya.jarvis.ORB";   // 光の玉の表示/非表示を設定に合わせて更新
    private static final int NOTIF_ID = 7;
    private static final int CALL_NOTIF_ID = 8;
    static final String CALL_CHANNEL = "jarvis_call";

    private static JarvisService instance;
    private final Handler h = new Handler(Looper.getMainLooper());
    private List<float[][]> wakeTemplates;
    private float wakeThreshold;
    private WakeListener wake;
    private boolean micBlocked;      // Android 14 以降、後ろから起動されたためマイクを使えない（アプリを開けば直る）
    private boolean appVisible;      // アプリの画面が出ている（画面側がマイクを使う）
    private boolean waitingForApp;   // 呼ばれて画面を開いているところ
    private final Runnable retryWake = this::syncWake;

    private WindowManager wm;
    private OrbView orb;
    private WindowManager.LayoutParams lp;
    private SharedPreferences prefs;

    /** 外から常駐を開始する */
    static void start(Context c) {
        Intent i = new Intent(c, JarvisService.class);
        if (Build.VERSION.SDK_INT >= 26) c.startForegroundService(i); else c.startService(i);
    }

    static void stop(Context c) { c.stopService(new Intent(c, JarvisService.class)); }

    static PendingIntent listenIntent(Context c) {
        Intent i = new Intent(c, MainActivity.class)
                .setAction(MainActivity.ACTION_LISTEN)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_SINGLE_TOP);
        return PendingIntent.getActivity(c, 1, i, PendingIntent.FLAG_IMMUTABLE | PendingIntent.FLAG_UPDATE_CURRENT);
    }

    /** アプリの画面が出た・消えた（MainActivity から）。出ている間は呼びかけ待ちを休む */
    static void setAppVisible(boolean visible) {
        JarvisService s = instance;
        if (s == null) return;
        s.h.post(() -> {
            s.appVisible = visible;
            if (visible) s.waitingForApp = false;
            s.syncWake();
        });
    }

    /** 呼びかけ待ちの状態（設定画面の表示用） */
    static String wakeState() {
        JarvisService s = instance;
        if (s == null) return "stopped";
        if (s.wake != null) return "listening";
        if (s.micBlocked) return "blocked";
        if (s.appVisible) return "paused";
        return s.wakeWanted() ? "waiting" : "off";
    }

    @Override
    public void onCreate() {
        super.onCreate();
        instance = this;
        appVisible = MainActivity.visible;   // 画面を開いたまま常駐を始めたとき
        prefs = getSharedPreferences("jarvis", MODE_PRIVATE);
        wm = (WindowManager) getSystemService(WINDOW_SERVICE);
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        if (intent != null && ACTION_STOP.equals(intent.getAction())) {
            prefs.edit().putString("resident", "0").apply();
            stopWake();
            removeOrb();
            stopForeground(true);
            stopSelf();
            return START_NOT_STICKY;
        }
        loadWake();
        startForegroundTyped();
        syncOrb();
        syncWake();
        return START_STICKY;
    }

    /**
     * 呼びかけ待ちをするなら、マイクを使う常駐として開始する。Android 14 以降は、アプリが後ろにいるとき
     * （再起動直後など）はマイクを使う常駐を始められないので、その場合はマイクなしで始めて、
     * アプリが開かれたとき（onResume から start が呼ばれる）にやり直す。
     */
    private void startForegroundTyped() {
        boolean wantMic = wakeWanted();
        micBlocked = false;
        if (Build.VERSION.SDK_INT >= 34) {
            int type = ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE;
            if (wantMic) {
                try {
                    startForeground(NOTIF_ID, buildNotification(), type | ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE);
                    return;
                } catch (Exception e) {
                    micBlocked = true;
                }
            }
            startForeground(NOTIF_ID, buildNotification(), type);
        } else {
            startForeground(NOTIF_ID, buildNotification());
        }
    }

    private Notification buildNotification() {
        NotificationManager nm = (NotificationManager) getSystemService(NOTIFICATION_SERVICE);
        if (Build.VERSION.SDK_INT >= 26 && nm.getNotificationChannel(CHANNEL) == null) {
            NotificationChannel ch = new NotificationChannel(CHANNEL, "ジャービス常駐", NotificationManager.IMPORTANCE_LOW);
            ch.setDescription("ジャービスがいつでも呼べるように待機していることを表示します");
            ch.setShowBadge(false);
            nm.createNotificationChannel(ch);
        }
        Intent stop = new Intent(this, JarvisService.class).setAction(ACTION_STOP);
        PendingIntent stopPi = PendingIntent.getService(this, 2, stop, PendingIntent.FLAG_IMMUTABLE | PendingIntent.FLAG_UPDATE_CURRENT);
        Intent open = new Intent(this, MainActivity.class).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_SINGLE_TOP);
        PendingIntent openPi = PendingIntent.getActivity(this, 3, open, PendingIntent.FLAG_IMMUTABLE | PendingIntent.FLAG_UPDATE_CURRENT);

        Notification.Builder b = Build.VERSION.SDK_INT >= 26 ? new Notification.Builder(this, CHANNEL) : new Notification.Builder(this);
        String title = "ジャービス 待機中", text = "「話しかける」をタップすると、すぐに聞き取りを始めます";
        if (wakeWanted()) {
            String word = prefs.getString("wake_label", "ジャービス");
            if (micBlocked) {
                text = "アプリを一度開くと、呼びかけ待ちを再開します";
            } else {
                title = "ジャービス 待機中（呼びかけ待ち）";
                text = "「" + word + "」と呼ぶか、「話しかける」をタップ";
            }
        }
        b.setSmallIcon(R.drawable.ic_stat_jarvis)
                .setContentTitle(title)
                .setContentText(text)
                .setColor(Color.parseColor("#8FD8FF"))
                .setOngoing(true)
                .setShowWhen(false)
                .setContentIntent(openPi)
                .addAction(new Notification.Action.Builder(null, "🎙 話しかける", listenIntent(this)).build())
                .addAction(new Notification.Action.Builder(null, "常駐をやめる", stopPi).build());
        if (Build.VERSION.SDK_INT >= 31) b.setForegroundServiceBehavior(Notification.FOREGROUND_SERVICE_IMMEDIATE);
        return b.build();
    }

    // ---------------- 呼びかけ待ち ----------------

    private boolean hasMic() {
        return checkSelfPermission(Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED;
    }

    private void loadWake() {
        wakeTemplates = WakeWord.fromJson(prefs.getString("wake_templates", ""));
        float sens = WakeWord.DEFAULT_SENSITIVITY;
        try { sens = Float.parseFloat(prefs.getString("wake_sens", "")); } catch (Exception ignored) {}
        wakeThreshold = wakeTemplates.isEmpty() ? 0 : WakeWord.threshold(wakeTemplates, sens);
    }

    private boolean wakeWanted() {
        return "1".equals(prefs.getString("wake", "0")) && wakeTemplates != null && wakeTemplates.size() >= 2 && hasMic();
    }

    /** 今の状態に合わせて、マイクを開く・閉じる */
    private void syncWake() {
        h.removeCallbacks(retryWake);
        boolean want = wakeWanted() && !micBlocked && !appVisible && !waitingForApp;
        if (want && wake == null) {
            // 毎回新しい検出器にする（前回の途中経過を持ち越さない）
            WakeListener w = new WakeListener(new WakeWord.Detector(wakeTemplates, wakeThreshold), h, new WakeListener.Callback() {
                @Override public void onWake() { onWakeHeard(); }
                @Override public void onMicLost() {
                    wake = null;
                    h.postDelayed(retryWake, 15000);   // 通話などが終わったら戻れるよう、ときどき開き直す
                }
            });
            if (w.start()) wake = w;
            else h.postDelayed(retryWake, 15000);
        } else if (!want) {
            stopWake();
        }
    }

    private void stopWake() {
        h.removeCallbacks(retryWake);
        if (wake != null) { wake.stop(); wake = null; }
    }

    /** 呼びかけが聞こえた: すぐ合図して、画面を開いて聞き取りを始める */
    private void onWakeHeard() {
        if (wake == null) return;
        stopWake();               // 聞き取り（Google の音声認識）にマイクを譲る
        waitingForApp = true;
        acknowledge();
        try {
            startActivity(new Intent(this, MainActivity.class).setAction(MainActivity.ACTION_LISTEN)
                    .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_SINGLE_TOP));
        } catch (Exception ignored) {}
        // 画面を開けなかった（後ろからの起動を Android に止められた）ときは、通知で知らせて待ちに戻る
        h.postDelayed(() -> {
            if (waitingForApp && !appVisible) {
                waitingForApp = false;
                notifyCouldNotOpen();
                syncWake();
            }
        }, 3000);
    }

    /** 「ピッ」と短い振動。聞こえたことがすぐ分かるように */
    private void acknowledge() {
        try {
            ToneGenerator tg = new ToneGenerator(AudioManager.STREAM_MUSIC, 70);
            tg.startTone(ToneGenerator.TONE_PROP_ACK, 150);
            h.postDelayed(tg::release, 400);
        } catch (Exception ignored) {}
        try {
            Vibrator v = (Vibrator) getSystemService(VIBRATOR_SERVICE);
            if (v != null && v.hasVibrator()) v.vibrate(VibrationEffect.createOneShot(40, VibrationEffect.DEFAULT_AMPLITUDE));
        } catch (Exception ignored) {}
    }

    private void notifyCouldNotOpen() {
        NotificationManager nm = (NotificationManager) getSystemService(NOTIFICATION_SERVICE);
        if (nm.getNotificationChannel(CALL_CHANNEL) == null) {
            NotificationChannel ch = new NotificationChannel(CALL_CHANNEL, "呼びかけへの応答", NotificationManager.IMPORTANCE_HIGH);
            ch.setDescription("呼びかけが聞こえたのに画面を開けなかったときに知らせます");
            nm.createNotificationChannel(ch);
        }
        Notification n = new Notification.Builder(this, CALL_CHANNEL)
                .setSmallIcon(R.drawable.ic_stat_jarvis)
                .setContentTitle("お呼びでしょうか")
                .setContentText("タップで話しかけられます。「他のアプリの上に表示」を許可すると、次からすぐ開きます")
                .setContentIntent(listenIntent(this))
                .setAutoCancel(true)
                .setTimeoutAfter(60000)
                .build();
        nm.notify(CALL_NOTIF_ID, n);
    }

    // ---------------- 浮かぶ光の玉 ----------------

    private void syncOrb() {
        boolean want = "1".equals(prefs.getString("orb", "1"))
                && (Build.VERSION.SDK_INT < 23 || Settings.canDrawOverlays(this));
        if (want && orb == null) addOrb();
        if (!want) removeOrb();
    }

    @SuppressLint("ClickableViewAccessibility")
    private void addOrb() {
        DisplayMetrics dm = getResources().getDisplayMetrics();
        int size = Math.round(64 * dm.density);
        orb = new OrbView(this);
        lp = new WindowManager.LayoutParams(size, size,
                Build.VERSION.SDK_INT >= 26 ? WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY : WindowManager.LayoutParams.TYPE_PHONE,
                WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE | WindowManager.LayoutParams.FLAG_LAYOUT_NO_LIMITS,
                PixelFormat.TRANSLUCENT);
        lp.gravity = Gravity.TOP | Gravity.START;
        lp.x = prefs.getInt("orb_x", dm.widthPixels - size - Math.round(8 * dm.density));
        lp.y = prefs.getInt("orb_y", Math.round(dm.heightPixels * 0.55f));

        final float[] down = new float[4];
        final long[] downAt = new long[1];
        final boolean[] moved = new boolean[1];
        orb.setOnTouchListener((v, e) -> {
            switch (e.getAction()) {
                case MotionEvent.ACTION_DOWN:
                    down[0] = e.getRawX(); down[1] = e.getRawY(); down[2] = lp.x; down[3] = lp.y;
                    downAt[0] = SystemClock.uptimeMillis(); moved[0] = false;
                    orb.pressed = true; orb.invalidate();
                    return true;
                case MotionEvent.ACTION_MOVE:
                    float dx = e.getRawX() - down[0], dy = e.getRawY() - down[1];
                    if (Math.abs(dx) + Math.abs(dy) > 12 * dm.density) moved[0] = true;
                    if (moved[0]) {
                        lp.x = Math.round(down[2] + dx); lp.y = Math.round(down[3] + dy);
                        try { wm.updateViewLayout(orb, lp); } catch (Exception ignored) {}
                    }
                    return true;
                case MotionEvent.ACTION_UP:
                case MotionEvent.ACTION_CANCEL:
                    orb.pressed = false; orb.invalidate();
                    long held = SystemClock.uptimeMillis() - downAt[0];
                    if (moved[0]) {
                        // 画面の端に吸い付ける
                        lp.x = (lp.x + size / 2 < dm.widthPixels / 2) ? 0 : dm.widthPixels - size;
                        try { wm.updateViewLayout(orb, lp); } catch (Exception ignored) {}
                        prefs.edit().putInt("orb_x", lp.x).putInt("orb_y", lp.y).apply();
                    } else if (e.getAction() == MotionEvent.ACTION_UP) {
                        if (held > 650) {
                            // 長押し：光の玉を隠す（通知とクイック設定からはいつでも呼べる）
                            prefs.edit().putString("orb", "0").apply();
                            removeOrb();
                        } else {
                            try { listenIntent(this).send(); } catch (Exception ignored) {}
                        }
                    }
                    return true;
            }
            return false;
        });
        try { wm.addView(orb, lp); } catch (Exception e) { orb = null; }
    }

    private void removeOrb() {
        if (orb != null) {
            try { wm.removeView(orb); } catch (Exception ignored) {}
            orb.stop();
            orb = null;
        }
    }

    @Override
    public void onDestroy() {
        stopWake();
        removeOrb();
        if (instance == this) instance = null;
        super.onDestroy();
    }

    @Override
    public IBinder onBind(Intent intent) { return null; }

    /** 呼吸するように光る小さなホログラム玉 */
    static class OrbView extends View {
        private final Paint glow = new Paint(Paint.ANTI_ALIAS_FLAG);
        private final Paint ring = new Paint(Paint.ANTI_ALIAS_FLAG);
        private final Paint dot = new Paint(Paint.ANTI_ALIAS_FLAG);
        boolean pressed = false;
        private boolean running = true;
        private final long t0 = SystemClock.uptimeMillis();

        OrbView(Context c) {
            super(c);
            ring.setStyle(Paint.Style.STROKE);
            ring.setStrokeCap(Paint.Cap.ROUND);
            dot.setColor(Color.argb(230, 225, 248, 255));
        }

        void stop() { running = false; }

        @Override
        protected void onDraw(Canvas c) {
            float w = getWidth(), h = getHeight(), cx = w / 2f, cy = h / 2f, r = Math.min(w, h) / 2f;
            float t = (SystemClock.uptimeMillis() - t0) / 1000f;
            float breathe = 0.5f + 0.5f * (float) Math.sin(t * 1.6);
            float scale = pressed ? 0.9f : 1f;

            // 外側のにじみ
            glow.setShader(new RadialGradient(cx, cy, r * scale,
                    new int[]{Color.argb((int) (150 + 60 * breathe), 120, 210, 255), Color.argb(70, 40, 120, 200), Color.argb(0, 0, 60, 140)},
                    new float[]{0f, 0.45f, 1f}, Shader.TileMode.CLAMP));
            c.drawCircle(cx, cy, r * scale, glow);

            // 回る弧
            ring.setStrokeWidth(r * 0.07f);
            ring.setColor(Color.argb(200, 143, 216, 255));
            float rr = r * 0.62f * scale;
            for (int i = 0; i < 3; i++) {
                float start = (t * 70 + i * 120) % 360;
                c.drawArc(cx - rr, cy - rr, cx + rr, cy + rr, start, 70, false, ring);
            }
            ring.setStrokeWidth(r * 0.035f);
            ring.setColor(Color.argb(140, 143, 216, 255));
            float r2 = r * 0.8f * scale;
            for (int i = 0; i < 2; i++) {
                float start = (-t * 40 + i * 180) % 360;
                c.drawArc(cx - r2, cy - r2, cx + r2, cy + r2, start, 110, false, ring);
            }

            // 中心の光
            dot.setShader(new RadialGradient(cx, cy, r * 0.32f * scale,
                    new int[]{Color.argb(255, 240, 252, 255), Color.argb(160, 120, 210, 255), Color.argb(0, 0, 80, 160)},
                    new float[]{0f, 0.4f, 1f}, Shader.TileMode.CLAMP));
            c.drawCircle(cx, cy, r * (0.26f + 0.04f * breathe) * scale, dot);

            // 30fps 程度で更新（電池を食べすぎないように）
            if (running) postInvalidateDelayed(33);
        }
    }
}
