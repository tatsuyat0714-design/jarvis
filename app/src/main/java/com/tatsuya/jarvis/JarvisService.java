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
import android.os.Build;
import android.os.IBinder;
import android.os.SystemClock;
import android.provider.Settings;
import android.util.DisplayMetrics;
import android.view.Gravity;
import android.view.MotionEvent;
import android.view.View;
import android.view.WindowManager;

/**
 * ジャービスの常駐係。
 * ・通知欄に「話しかける」ボタン付きの通知を出し続ける
 * ・（許可があれば）他のアプリの上に浮かぶ光の玉を出す。タップで話しかけ、ドラッグで移動、長押しで隠す
 */
public class JarvisService extends Service {

    static final String CHANNEL = "jarvis_resident";
    static final String ACTION_STOP = "com.tatsuya.jarvis.STOP";
    static final String ACTION_ORB = "com.tatsuya.jarvis.ORB";   // 光の玉の表示/非表示を設定に合わせて更新
    private static final int NOTIF_ID = 7;

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

    @Override
    public void onCreate() {
        super.onCreate();
        prefs = getSharedPreferences("jarvis", MODE_PRIVATE);
        wm = (WindowManager) getSystemService(WINDOW_SERVICE);
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        if (intent != null && ACTION_STOP.equals(intent.getAction())) {
            prefs.edit().putString("resident", "0").apply();
            removeOrb();
            stopForeground(true);
            stopSelf();
            return START_NOT_STICKY;
        }
        Notification n = buildNotification();
        if (Build.VERSION.SDK_INT >= 34) {
            startForeground(NOTIF_ID, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE);
        } else {
            startForeground(NOTIF_ID, n);
        }
        syncOrb();
        return START_STICKY;
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
        b.setSmallIcon(R.drawable.ic_stat_jarvis)
                .setContentTitle("ジャービス 待機中")
                .setContentText("「話しかける」をタップすると、すぐに聞き取りを始めます")
                .setColor(Color.parseColor("#8FD8FF"))
                .setOngoing(true)
                .setShowWhen(false)
                .setContentIntent(openPi)
                .addAction(new Notification.Action.Builder(null, "🎙 話しかける", listenIntent(this)).build())
                .addAction(new Notification.Action.Builder(null, "常駐をやめる", stopPi).build());
        if (Build.VERSION.SDK_INT >= 31) b.setForegroundServiceBehavior(Notification.FOREGROUND_SERVICE_IMMEDIATE);
        return b.build();
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
        removeOrb();
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
