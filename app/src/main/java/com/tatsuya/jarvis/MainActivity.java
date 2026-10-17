package com.tatsuya.jarvis;

import android.Manifest;
import android.app.Activity;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.content.pm.ResolveInfo;
import android.database.Cursor;
import android.graphics.Color;
import android.hardware.camera2.CameraCharacteristics;
import android.hardware.camera2.CameraManager;
import android.media.AudioManager;
import android.net.Uri;
import android.os.BatteryManager;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.provider.AlarmClock;
import android.provider.CalendarContract;
import android.provider.ContactsContract;
import android.speech.tts.Voice;
import android.speech.RecognitionListener;
import android.speech.RecognizerIntent;
import android.speech.SpeechRecognizer;
import android.speech.tts.TextToSpeech;
import android.view.Window;
import android.view.WindowManager;
import android.webkit.JavascriptInterface;
import android.webkit.WebChromeClient;
import android.webkit.WebSettings;
import android.webkit.WebView;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.net.URLEncoder;
import java.nio.charset.StandardCharsets;
import java.text.Normalizer;
import java.util.ArrayList;
import java.util.List;
import java.util.Locale;
import java.util.Set;

/**
 * ジャービス本体。
 * 画面（見た目・会話のロジック）は assets/index.html の Web 技術で作り、
 * スマホの機能（声・マイク・タイマー・アラーム・アプリ起動・通信）はこの Java 側が担当する。
 * 2つは "Android" という名前の橋（JavascriptInterface）でつながっている。
 */
public class MainActivity extends Activity {

    private static final int REQ_MIC = 1;
    private static final int REQ_CONTACTS = 2;
    private static final int REQ_NOTIF = 3;
    static final String ACTION_LISTEN = "com.tatsuya.jarvis.LISTEN";

    private WebView web;
    private TextToSpeech tts;
    private Speaker speaker;
    private boolean ttsReady = false;
    private SpeechRecognizer recognizer;
    private SharedPreferences prefs;
    private final Handler main = new Handler(Looper.getMainLooper());
    private boolean pendingListen = false;
    private final List<float[][]> wakeEnroll = new ArrayList<>();   // 登録中の呼びかけ（保存するまでメモリだけ）
    private volatile boolean wakeTesting = false;
    static volatile boolean visible = false;   // 画面が出ているか（常駐側が呼びかけ待ちを休むかの判断に使う）

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        Window w = getWindow();
        w.setStatusBarColor(Color.parseColor("#02060B"));
        w.setNavigationBarColor(Color.parseColor("#02060B"));
        w.addFlags(WindowManager.LayoutParams.FLAG_DRAWS_SYSTEM_BAR_BACKGROUNDS);
        setVolumeControlStream(AudioManager.STREAM_MUSIC);

        prefs = getSharedPreferences("jarvis", Context.MODE_PRIVATE);

        web = new WebView(this);
        web.setBackgroundColor(Color.parseColor("#02060B"));
        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setMediaPlaybackRequiresUserGesture(false);
        web.setWebChromeClient(new WebChromeClient());
        web.addJavascriptInterface(new Bridge(), "Android");
        setContentView(web);
        web.loadUrl("file:///android_asset/index.html");

        if ("1".equals(prefs.getString("resident", "0"))) {
            try { JarvisService.start(this); } catch (Exception ignored) {}
        }

        tts = new TextToSpeech(this, status -> {
            if (status == TextToSpeech.SUCCESS) {
                int r = tts.setLanguage(Locale.JAPAN);
                ttsReady = r != TextToSpeech.LANG_MISSING_DATA && r != TextToSpeech.LANG_NOT_SUPPORTED;
                applyVoiceName(prefs.getString("voice_name", ""));
                speaker = new Speaker(this, tts, main, new Speaker.Listener() {
                    @Override public void onStart() { js("jarvis.onSpeak('start')"); }
                    @Override public void onEnd() { js("jarvis.onSpeak('end')"); }
                });
                speaker.setParams(loadVoiceFx());
            }
        });
    }

    /** ASSIST（アシスタント呼び出し）で起動されたら、すぐ聞き取りを始める */
    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        setIntent(intent);
        if (isCallIntent(intent)) {
            intent.setAction(Intent.ACTION_MAIN);
            // 画面はもう読み込み済みなので、待たずに聞き取りを始める（呼びかけからの反応を速く）
            main.postDelayed(() -> js("jarvis.onAssist(true)"), 30);
        }
    }

    @Override
    protected void onPause() {
        super.onPause();
        wakeTesting = false;
        visible = false;
        JarvisService.setAppVisible(false);   // 画面が隠れたら呼びかけ待ちに戻る
    }

    @Override
    protected void onResume() {
        super.onResume();
        visible = true;
        JarvisService.setAppVisible(true);    // 画面が出ている間は画面側がマイクを使う
        if ("1".equals(prefs.getString("resident", "0"))) {
            try { JarvisService.start(this); } catch (Exception ignored) {}
        }
        if (isCallIntent(getIntent())) {
            getIntent().setAction(Intent.ACTION_MAIN);
            main.postDelayed(() -> js("jarvis.onAssist(true)"), 700);
        }
    }

    private static boolean isCallIntent(Intent i) {
        if (i == null) return false;
        String a = i.getAction();
        return Intent.ACTION_ASSIST.equals(a) || ACTION_LISTEN.equals(a);
    }

    @Override
    public void onBackPressed() {
        js("jarvis.onBack()");
    }

    @Override
    protected void onDestroy() {
        if (speaker != null) speaker.shutdown();
        else if (tts != null) { tts.stop(); tts.shutdown(); }
        if (recognizer != null) recognizer.destroy();
        super.onDestroy();
    }

    /** JavaScript 側の関数を呼ぶ（メインスレッドで） */
    private void js(final String code) {
        main.post(() -> { if (web != null) web.evaluateJavascript("try{" + code + "}catch(e){}", null); });
    }

    private static String q(String s) { return JSONObject.quote(s == null ? "" : s); }

    // ---------------- 音声認識 ----------------

    private void startListening() {
        if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
            pendingListen = true;
            requestPermissions(new String[]{Manifest.permission.RECORD_AUDIO}, REQ_MIC);
            return;
        }
        if (!SpeechRecognizer.isRecognitionAvailable(this)) {
            js("jarvis.onListenError('unavailable')");
            return;
        }
        stopSpeaking();
        if (recognizer == null) {
            recognizer = SpeechRecognizer.createSpeechRecognizer(this);
            recognizer.setRecognitionListener(new RecognitionListener() {
                @Override public void onReadyForSpeech(Bundle b) { js("jarvis.onListen('ready')"); }
                @Override public void onBeginningOfSpeech() { js("jarvis.onListen('speech')"); }
                @Override public void onRmsChanged(float v) { js("jarvis.onLevel(" + v + ")"); }
                @Override public void onBufferReceived(byte[] b) {}
                @Override public void onEndOfSpeech() { js("jarvis.onListen('end')"); }
                @Override public void onError(int error) { js("jarvis.onListenError('" + error + "')"); }
                @Override public void onResults(Bundle b) {
                    ArrayList<String> r = b.getStringArrayList(SpeechRecognizer.RESULTS_RECOGNITION);
                    js("jarvis.onHeard(" + q(r != null && !r.isEmpty() ? r.get(0) : "") + ")");
                }
                @Override public void onPartialResults(Bundle b) {
                    ArrayList<String> r = b.getStringArrayList(SpeechRecognizer.RESULTS_RECOGNITION);
                    if (r != null && !r.isEmpty()) js("jarvis.onPartial(" + q(r.get(0)) + ")");
                }
                @Override public void onEvent(int t, Bundle b) {}
            });
        }
        Intent i = new Intent(RecognizerIntent.ACTION_RECOGNIZE_SPEECH);
        i.putExtra(RecognizerIntent.EXTRA_LANGUAGE_MODEL, RecognizerIntent.LANGUAGE_MODEL_FREE_FORM);
        i.putExtra(RecognizerIntent.EXTRA_LANGUAGE, "ja-JP");
        i.putExtra(RecognizerIntent.EXTRA_PARTIAL_RESULTS, true);
        i.putExtra(RecognizerIntent.EXTRA_MAX_RESULTS, 1);
        recognizer.startListening(i);
    }

    @Override
    public void onRequestPermissionsResult(int code, String[] perms, int[] results) {
        super.onRequestPermissionsResult(code, perms, results);
        if (code == REQ_CONTACTS) {
            boolean ok = results.length > 0 && results[0] == PackageManager.PERMISSION_GRANTED;
            js("jarvis.onContactsPermission(" + ok + ")");
        }
        if (code == REQ_MIC) {
            boolean ok = results.length > 0 && results[0] == PackageManager.PERMISSION_GRANTED;
            if (ok && pendingListen) startListening();
            else if (!ok) js("jarvis.onListenError('permission')");
            pendingListen = false;
            js("jarvis.onMicPermission(" + ok + ")");   // 呼びかけの登録の途中なら続きを促す
        }
    }

    // ---------------- アプリ起動 ----------------

    private static String norm(String s) {
        if (s == null) return "";
        String n = Normalizer.normalize(s, Normalizer.Form.NFKC).toLowerCase(Locale.ROOT);
        n = n.replaceAll("[\\s・\\-_.]", "");
        // カタカナ → ひらがな（「ライン」と「らいん」を同じ扱いに）
        StringBuilder b = new StringBuilder();
        for (char c : n.toCharArray()) b.append(c >= 'ァ' && c <= 'ヶ' ? (char) (c - 0x60) : c);
        return b.toString();
    }

    /** よく言われる呼び方 → アプリ名に含まれる文字 */
    private static String alias(String n) {
        switch (n) {
            case "らいん": return "line";
            case "ゆーちゅーぶ": case "ようつべ": return "youtube";
            case "くろーむ": return "chrome";
            case "じーめーる": case "めーる": return "gmail";
            case "ぐーぐるまっぷ": case "まっぷ": case "ちず": case "地図": return "マップ";
            case "いんすた": case "いんすたぐらむ": return "instagram";
            case "えっくす": case "ついったー": return "x";
            case "かめら": return "かめら";
            case "せってい": return "設定";
            case "でんわ": return "電話";
            case "とけい": return "時計";
            case "すぽてぃふぁい": return "spotify";
            default: return n;
        }
    }

    private String openAppByName(String name) {
        PackageManager pm = getPackageManager();
        Intent q = new Intent(Intent.ACTION_MAIN).addCategory(Intent.CATEGORY_LAUNCHER);
        List<ResolveInfo> apps = pm.queryIntentActivities(q, 0);
        String want = norm(alias(norm(name)));
        if (want.isEmpty()) return "";
        ResolveInfo best = null;
        int bestScore = 0;
        for (ResolveInfo ri : apps) {
            String label = ri.loadLabel(pm).toString();
            String l = norm(label);
            int score = 0;
            if (l.equals(want)) score = 100;
            else if (l.startsWith(want)) score = 80;
            else if (l.contains(want)) score = 60;
            else if (want.contains(l) && l.length() >= 2) score = 40;
            else if (norm(ri.activityInfo.packageName).contains(want) && want.length() >= 3) score = 30;
            if (score > bestScore) { bestScore = score; best = ri; }
        }
        if (best == null) return "";
        Intent launch = pm.getLaunchIntentForPackage(best.activityInfo.packageName);
        if (launch == null) return "";
        launch.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
        startActivity(launch);
        return best.loadLabel(pm).toString();
    }

    private boolean micGranted() {
        if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED) return true;
        main.post(() -> requestPermissions(new String[]{Manifest.permission.RECORD_AUDIO}, REQ_MIC));
        return false;
    }

    /** 常駐を設定に合わせて開き直す（呼びかけの登録・感度の変更を反映） */
    private void restartResident() {
        main.post(() -> {
            if ("1".equals(prefs.getString("resident", "0"))) {
                try { JarvisService.start(this); } catch (Exception ignored) {}
            }
        });
    }

    // ---------------- 声の種類・調声 ----------------

    private void stopSpeaking() {
        if (speaker != null) speaker.stop();
        else if (tts != null) tts.stop();
    }

    /** 保存された調声の値。古い版の「速さ・高さ」だけの設定も引き継ぐ。 */
    private VoiceFx.Params loadVoiceFx() {
        VoiceFx.Params p = VoiceFx.Params.fromJson(prefs.getString("voice_fx", ""));
        if (prefs.getString("voice_fx", "").isEmpty()) {
            p.rate = prefs.getFloat("rate", p.rate);
            p.pitch = prefs.getFloat("pitch", p.pitch);
        }
        return p;
    }

    private boolean applyVoiceName(String name) {
        if (tts == null || name == null || name.isEmpty()) return false;
        try {
            Set<Voice> vs = tts.getVoices();
            if (vs == null) return false;
            for (Voice v : vs) {
                if (v.getName().equals(name)) { tts.setVoice(v); return true; }
            }
        } catch (Exception ignored) {}
        return false;
    }

    // ---------------- 連絡先 ----------------

    /** 名前で連絡先を探す → {表示名, 電話番号}。番号そのものならそのまま返す */
    private String[] resolveContact(String who) throws Exception {
        String digits = who.replaceAll("[^0-9+]", "");
        if (digits.length() >= 3 && digits.length() >= who.replaceAll("[\\s-]", "").length() - 1) {
            return new String[]{digits, digits};
        }
        if (checkSelfPermission(Manifest.permission.READ_CONTACTS) != PackageManager.PERMISSION_GRANTED) {
            main.post(() -> requestPermissions(new String[]{Manifest.permission.READ_CONTACTS}, REQ_CONTACTS));
            throw new Exception("PERMISSION");
        }
        Uri uri = Uri.withAppendedPath(ContactsContract.CommonDataKinds.Phone.CONTENT_FILTER_URI, Uri.encode(who));
        try (Cursor c = getContentResolver().query(uri,
                new String[]{ContactsContract.CommonDataKinds.Phone.DISPLAY_NAME, ContactsContract.CommonDataKinds.Phone.NUMBER},
                null, null, null)) {
            if (c != null && c.moveToFirst()) return new String[]{c.getString(0), c.getString(1)};
        }
        return null;
    }

    private String result(boolean ok, String key, String value) {
        try {
            JSONObject o = new JSONObject();
            o.put("ok", ok);
            if (key != null) o.put(key, value);
            return o.toString();
        } catch (Exception e) { return "{\"ok\":" + ok + "}"; }
    }

    private boolean startSafely(Intent i) {
        try {
            i.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
            startActivity(i);
            return true;
        } catch (Exception e) { return false; }
    }

    // ---------------- ふつうのWeb取得（天気・ニュース・Wikipedia） ----------------

    private void httpGet(final String id, final String urlStr) {
        new Thread(() -> {
            String out;
            try {
                HttpURLConnection c = (HttpURLConnection) new URL(urlStr).openConnection();
                c.setConnectTimeout(12000);
                c.setReadTimeout(20000);
                c.setRequestProperty("User-Agent", "JarvisPersonalAssistant/0.2 (Android; personal use)");
                c.setRequestProperty("Accept-Language", "ja");
                int code = c.getResponseCode();
                String text = readAll(code >= 400 ? c.getErrorStream() : c.getInputStream());
                JSONObject o = new JSONObject();
                o.put("status", code);
                o.put("body", text);
                out = o.toString();
            } catch (Exception e) {
                out = "{\"status\":0,\"error\":" + q(e.getClass().getSimpleName() + ": " + e.getMessage()) + "}";
            }
            js("jarvis.onGemini(" + q(id) + "," + q(out) + ")");
        }).start();
    }

    // ---------------- 通信（Gemini） ----------------

    /** provider: "gemini" または "groq" */
    private void aiRequest(final String id, final String provider, final String modelOverride, final String body) {
        new Thread(() -> {
            String result;
            try {
                boolean groq = "groq".equals(provider);
                String key = prefs.getString(groq ? "groq_key" : "gemini_key", "");
                if (key.isEmpty()) throw new Exception("NO_KEY");
                URL url;
                if (groq) {
                    url = new URL("https://api.groq.com/openai/v1/chat/completions");
                } else {
                    String model = (modelOverride != null && !modelOverride.isEmpty())
                            ? modelOverride : prefs.getString("gemini_model", "gemini-flash-latest");
                    url = new URL("https://generativelanguage.googleapis.com/v1beta/models/"
                            + URLEncoder.encode(model, "UTF-8") + ":generateContent");
                }
                HttpURLConnection c = (HttpURLConnection) url.openConnection();
                c.setRequestMethod("POST");
                c.setConnectTimeout(15000);
                c.setReadTimeout(90000);
                c.setDoOutput(true);
                c.setRequestProperty("Content-Type", "application/json; charset=utf-8");
                if (groq) c.setRequestProperty("Authorization", "Bearer " + key);
                else c.setRequestProperty("x-goog-api-key", key);
                try (OutputStream os = c.getOutputStream()) { os.write(body.getBytes(StandardCharsets.UTF_8)); }
                int code = c.getResponseCode();
                InputStream is = code >= 400 ? c.getErrorStream() : c.getInputStream();
                String text = readAll(is);
                JSONObject wrap = new JSONObject();
                wrap.put("status", code);
                wrap.put("body", text);
                result = wrap.toString();
            } catch (Exception e) {
                try {
                    JSONObject wrap = new JSONObject();
                    wrap.put("status", 0);
                    wrap.put("error", "NO_KEY".equals(e.getMessage()) ? "NO_KEY"
                            : e.getClass().getSimpleName() + ": " + e.getMessage());
                    result = wrap.toString();
                } catch (Exception ignored) { result = "{\"status\":0}"; }
            }
            js("jarvis.onGemini(" + q(id) + "," + q(result) + ")");
        }).start();
    }

    private static String readAll(InputStream is) throws Exception {
        if (is == null) return "";
        ByteArrayOutputStream bo = new ByteArrayOutputStream();
        byte[] buf = new byte[8192];
        int n;
        while ((n = is.read(buf)) > 0) bo.write(buf, 0, n);
        is.close();
        return bo.toString("UTF-8");
    }

    // ---------------- JavaScript から呼べる機能 ----------------

    private class Bridge {
        @JavascriptInterface
        public void speak(String text) {
            main.post(() -> {
                if (speaker == null || !ttsReady) { js("jarvis.onSpeak('end')"); return; }
                speaker.speak(text);
            });
        }

        @JavascriptInterface
        public void stopSpeak() { main.post(MainActivity.this::stopSpeaking); }

        /** 速さと高さだけ変える（古い画面からの呼び出し用）。ほかの調声の値はそのまま */
        @JavascriptInterface
        public void setVoice(float rate, float pitch) {
            prefs.edit().putFloat("rate", rate).putFloat("pitch", pitch).apply();
            main.post(() -> {
                if (speaker == null) return;
                VoiceFx.Params p = speaker.params();
                p.rate = rate;
                p.pitch = pitch;
                speaker.setParams(p);
            });
        }

        /** 調声の値をまとめて設定する（JSON。範囲外の値は端に丸める）。保存して次の読み上げから使う */
        @JavascriptInterface
        public void setVoiceFx(String json) {
            VoiceFx.Params p = VoiceFx.Params.fromJson(json);
            prefs.edit().putString("voice_fx", json == null ? "" : json)
                    .putFloat("rate", p.rate).putFloat("pitch", p.pitch).apply();
            main.post(() -> { if (speaker != null) speaker.setParams(p); });
        }

        /** 試し聞き用。保存せずに今の声だけ変える（設定画面を保存せず閉じたら保存済みの値で呼び直す） */
        @JavascriptInterface
        public void previewVoiceFx(String json) {
            VoiceFx.Params p = VoiceFx.Params.fromJson(json);
            main.post(() -> { if (speaker != null) speaker.setParams(p); });
        }

        @JavascriptInterface
        public String getVoiceFx() { return prefs.getString("voice_fx", ""); }

        @JavascriptInterface
        public void listen() { main.post(MainActivity.this::startListening); }

        @JavascriptInterface
        public void stopListen() { main.post(() -> { if (recognizer != null) recognizer.stopListening(); }); }

        /** スマホの時計アプリでタイマーを開始（画面を閉じても鳴る） */
        @JavascriptInterface
        public boolean setTimer(int seconds, String label) {
            try {
                Intent i = new Intent(AlarmClock.ACTION_SET_TIMER)
                        .putExtra(AlarmClock.EXTRA_LENGTH, seconds)
                        .putExtra(AlarmClock.EXTRA_MESSAGE, label == null || label.isEmpty() ? "ジャービス" : label)
                        .putExtra(AlarmClock.EXTRA_SKIP_UI, true)
                        .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
                startActivity(i);
                return true;
            } catch (Exception e) { return false; }
        }

        /** スマホの時計アプリにアラームを登録 */
        @JavascriptInterface
        public boolean setAlarm(int hour, int minute, String label) {
            try {
                Intent i = new Intent(AlarmClock.ACTION_SET_ALARM)
                        .putExtra(AlarmClock.EXTRA_HOUR, hour)
                        .putExtra(AlarmClock.EXTRA_MINUTES, minute)
                        .putExtra(AlarmClock.EXTRA_MESSAGE, label == null || label.isEmpty() ? "ジャービス" : label)
                        .putExtra(AlarmClock.EXTRA_SKIP_UI, true)
                        .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
                startActivity(i);
                return true;
            } catch (Exception e) { return false; }
        }

        /** アプリ名で起動。開けたアプリの表示名を返す（見つからなければ空文字） */
        @JavascriptInterface
        public String openApp(String name) {
            try { return openAppByName(name); } catch (Exception e) { return ""; }
        }

        /** インストール済みアプリ名の一覧（AIに渡す用） */
        @JavascriptInterface
        public String listApps() {
            JSONArray a = new JSONArray();
            PackageManager pm = getPackageManager();
            Intent q = new Intent(Intent.ACTION_MAIN).addCategory(Intent.CATEGORY_LAUNCHER);
            for (ResolveInfo ri : pm.queryIntentActivities(q, 0)) a.put(ri.loadLabel(pm).toString());
            return a.toString();
        }

        @JavascriptInterface
        public void gemini(String id, String model, String body) { aiRequest(id, "gemini", model, body); }

        @JavascriptInterface
        public void groq(String id, String body) { aiRequest(id, "groq", "", body); }

        @JavascriptInterface
        public String getPref(String k) { return prefs.getString(k, ""); }

        @JavascriptInterface
        public void setPref(String k, String v) { prefs.edit().putString(k, v).apply(); }

        @JavascriptInterface
        public void exitApp() { main.post(() -> moveTaskToBack(true)); }

        // ----- 呼びかけ待ち -----

        /** 設定画面に出す状態。JSON: {on, samples, enrolling, sens, label, state, overlay, mic} */
        @JavascriptInterface
        public String wakeInfo() {
            try {
                return new JSONObject()
                        .put("on", "1".equals(prefs.getString("wake", "0")))
                        .put("samples", WakeWord.fromJson(prefs.getString("wake_templates", "")).size())
                        .put("enrolling", wakeEnroll.size())
                        .put("sens", prefs.getString("wake_sens", String.valueOf(WakeWord.DEFAULT_SENSITIVITY)))
                        .put("label", prefs.getString("wake_label", "ジャービス"))
                        .put("state", JarvisService.wakeState())
                        .put("overlay", android.provider.Settings.canDrawOverlays(MainActivity.this))
                        .put("mic", checkSelfPermission(Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED)
                        .toString();
            } catch (Exception e) {
                return "{}";
            }
        }

        /** 登録をやり直す（録った分を捨てる） */
        @JavascriptInterface
        public void wakeEnrollReset() { main.post(wakeEnroll::clear); }

        /** 登録用に 1 回録音する。結果は jarvis.onWakeSample(ok, 理由, 録れた回数) で返す */
        @JavascriptInterface
        public void wakeRecord() {
            if (!micGranted()) { js("jarvis.onWakeSample(false,'permission',0)"); return; }
            new Thread(() -> {
                short[] pcm = WakeListener.record(2500);
                float[][] t = pcm == null ? null : WakeWord.template(pcm);
                main.post(() -> {
                    String why = pcm == null ? "mic" : t == null ? "unclear" : "";
                    if (t != null) wakeEnroll.add(t);
                    js("jarvis.onWakeSample(" + (t != null) + ",'" + why + "'," + wakeEnroll.size() + ")");
                });
            }, "jarvis-enroll").start();
        }

        /** 録った呼びかけを保存して、呼びかけ待ちを始める。2 回以上必要 */
        @JavascriptInterface
        public boolean wakeSave(String label) {
            if (wakeEnroll.size() < 2) return false;
            prefs.edit().putString("wake_templates", WakeWord.toJson(wakeEnroll))
                    .putString("wake_label", label == null || label.trim().isEmpty() ? "ジャービス" : label.trim())
                    .putString("wake", "1").putString("resident", "1").apply();
            main.post(wakeEnroll::clear);
            restartResident();
            return true;
        }

        @JavascriptInterface
        public void setWake(boolean on) {
            prefs.edit().putString("wake", on ? "1" : "0").apply();
            if (on) prefs.edit().putString("resident", "1").apply();
            restartResident();
        }

        @JavascriptInterface
        public void setWakeSensitivity(float s) {
            prefs.edit().putString("wake_sens", String.valueOf(Math.max(0f, Math.min(1f, s)))).apply();
            restartResident();
        }

        /**
         * 反応のテスト（画面を開いたまま最大 seconds 秒）。いちばん近づいた度合い（1 以下で反応）を
         * jarvis.onWakeTest(近さ, 反応したか, 終わったか) で 0.3 秒ごとに返す
         */
        @JavascriptInterface
        public void wakeTest(int seconds) {
            List<float[][]> t = WakeWord.fromJson(prefs.getString("wake_templates", ""));
            if (t.size() < 2 || !micGranted()) { js("jarvis.onWakeTest(-1,false,true)"); return; }
            float sens = WakeWord.DEFAULT_SENSITIVITY;
            try { sens = Float.parseFloat(prefs.getString("wake_sens", "")); } catch (Exception ignored) {}
            WakeWord.Detector d = new WakeWord.Detector(t, WakeWord.threshold(t, sens));
            wakeTesting = true;
            new Thread(() -> {
                android.media.AudioRecord r = WakeListener.open();
                if (r == null) { js("jarvis.onWakeTest(-1,false,true)"); wakeTesting = false; return; }
                short[] hop = new short[WakeWord.HOP];
                long end = System.currentTimeMillis() + Math.max(3, Math.min(30, seconds)) * 1000L, next = 0;
                boolean hit = false;
                try {
                    r.startRecording();
                    while (wakeTesting && System.currentTimeMillis() < end) {
                        int n = 0;
                        while (n < hop.length) { int g = r.read(hop, n, hop.length - n); if (g <= 0) break; n += g; }
                        if (n < hop.length) break;
                        hit |= d.push(hop);
                        if (System.currentTimeMillis() >= next || hit) {
                            float sc = d.lastScore;
                            d.lastScore = Float.POSITIVE_INFINITY;
                            js("jarvis.onWakeTest(" + (Float.isInfinite(sc) ? 9 : sc) + "," + hit + ",false)");
                            hit = false;
                            next = System.currentTimeMillis() + 300;
                        }
                    }
                } catch (Exception ignored) {
                } finally {
                    try { r.stop(); } catch (Exception ignored) {}
                    r.release();
                    wakeTesting = false;
                    js("jarvis.onWakeTest(9,false,true)");
                }
            }, "jarvis-waketest").start();
        }

        @JavascriptInterface
        public void wakeTestStop() { wakeTesting = false; }

        @JavascriptInterface
        public void wakeClear() {
            prefs.edit().remove("wake_templates").putString("wake", "0").apply();
            restartResident();
        }

        // ----- 声 -----
        @JavascriptInterface
        public String listVoices() {
            JSONArray a = new JSONArray();
            try {
                Set<Voice> vs = tts == null ? null : tts.getVoices();
                if (vs != null) for (Voice v : vs) {
                    Locale l = v.getLocale();
                    if (l == null || !"ja".equals(l.getLanguage())) continue;
                    JSONObject o = new JSONObject();
                    o.put("name", v.getName());
                    o.put("network", v.isNetworkConnectionRequired());
                    o.put("quality", v.getQuality());
                    a.put(o);
                }
            } catch (Exception ignored) {}
            return a.toString();
        }

        @JavascriptInterface
        public void setVoiceName(String name) {
            prefs.edit().putString("voice_name", name).apply();
            main.post(() -> {
                if (name == null || name.isEmpty()) { if (tts != null) tts.setLanguage(Locale.JAPAN); }
                else applyVoiceName(name);
            });
        }

        // ----- スマホ本体 -----
        @JavascriptInterface
        public boolean torch(boolean on) {
            try {
                CameraManager cm = (CameraManager) getSystemService(Context.CAMERA_SERVICE);
                for (String id : cm.getCameraIdList()) {
                    Boolean f = cm.getCameraCharacteristics(id).get(CameraCharacteristics.FLASH_INFO_AVAILABLE);
                    if (Boolean.TRUE.equals(f)) { cm.setTorchMode(id, on); return true; }
                }
            } catch (Exception ignored) {}
            return false;
        }

        /** action: up / down / mute / unmute / max / set(percent) / vibrate / normal。戻り値は現在の音量% */
        @JavascriptInterface
        public int volume(String action, int percent) {
            try {
                AudioManager am = (AudioManager) getSystemService(Context.AUDIO_SERVICE);
                int s = AudioManager.STREAM_MUSIC, max = am.getStreamMaxVolume(s);
                switch (action) {
                    case "up": am.adjustStreamVolume(s, AudioManager.ADJUST_RAISE, AudioManager.FLAG_SHOW_UI);
                        am.adjustStreamVolume(s, AudioManager.ADJUST_RAISE, 0); break;
                    case "down": am.adjustStreamVolume(s, AudioManager.ADJUST_LOWER, AudioManager.FLAG_SHOW_UI);
                        am.adjustStreamVolume(s, AudioManager.ADJUST_LOWER, 0); break;
                    case "mute": am.adjustStreamVolume(s, AudioManager.ADJUST_MUTE, AudioManager.FLAG_SHOW_UI); break;
                    case "unmute": am.adjustStreamVolume(s, AudioManager.ADJUST_UNMUTE, AudioManager.FLAG_SHOW_UI); break;
                    case "max": am.setStreamVolume(s, max, AudioManager.FLAG_SHOW_UI); break;
                    case "set": am.setStreamVolume(s, Math.round(max * Math.max(0, Math.min(100, percent)) / 100f), AudioManager.FLAG_SHOW_UI); break;
                    case "vibrate": am.setRingerMode(AudioManager.RINGER_MODE_VIBRATE); break;
                    case "normal": am.setRingerMode(AudioManager.RINGER_MODE_NORMAL); break;
                    default: break;
                }
                return Math.round(100f * am.getStreamVolume(s) / max);
            } catch (Exception e) { return -1; }
        }

        @JavascriptInterface
        public String battery() {
            try {
                BatteryManager bm = (BatteryManager) getSystemService(Context.BATTERY_SERVICE);
                JSONObject o = new JSONObject();
                o.put("level", bm.getIntProperty(BatteryManager.BATTERY_PROPERTY_CAPACITY));
                o.put("charging", bm.isCharging());
                return o.toString();
            } catch (Exception e) { return "{}"; }
        }

        /** 電話アプリを番号入りで開く（発信ボタンは本人が押す） */
        @JavascriptInterface
        public String dial(String who) {
            try {
                String[] c = resolveContact(who);
                if (c == null) return result(false, "error", "NOT_FOUND");
                boolean ok = startSafely(new Intent(Intent.ACTION_DIAL, Uri.parse("tel:" + Uri.encode(c[1]))));
                return result(ok, "name", c[0]);
            } catch (Exception e) { return result(false, "error", e.getMessage()); }
        }

        /** SMSを下書き状態で開く（送信ボタンは本人が押す） */
        @JavascriptInterface
        public String sms(String who, String body) {
            try {
                String[] c = resolveContact(who);
                if (c == null) return result(false, "error", "NOT_FOUND");
                Intent i = new Intent(Intent.ACTION_SENDTO, Uri.parse("smsto:" + Uri.encode(c[1])));
                i.putExtra("sms_body", body);
                return result(startSafely(i), "name", c[0]);
            } catch (Exception e) { return result(false, "error", e.getMessage()); }
        }

        /** 文章を共有（app="line" ならLINEの送り先選択画面へ） */
        @JavascriptInterface
        public boolean share(String text, String app) {
            Intent i = new Intent(Intent.ACTION_SEND).setType("text/plain").putExtra(Intent.EXTRA_TEXT, text);
            if ("line".equals(app)) {
                i.setPackage("jp.naver.line.android");
                if (startSafely(i)) return true;
                i.setPackage(null);
            }
            return startSafely(Intent.createChooser(i, "共有"));
        }

        @JavascriptInterface
        public boolean openUrl(String url) { return startSafely(new Intent(Intent.ACTION_VIEW, Uri.parse(url))); }

        @JavascriptInterface
        public boolean navigate(String dest) {
            if (startSafely(new Intent(Intent.ACTION_VIEW, Uri.parse("google.navigation:q=" + Uri.encode(dest))))) return true;
            return startSafely(new Intent(Intent.ACTION_VIEW, Uri.parse("geo:0,0?q=" + Uri.encode(dest))));
        }

        /** カレンダーの予定作成画面を、中身を入れた状態で開く（保存は本人が押す） */
        @JavascriptInterface
        public boolean addEvent(String title, double beginMs, double endMs, String place) {
            Intent i = new Intent(Intent.ACTION_INSERT).setData(CalendarContract.Events.CONTENT_URI)
                    .putExtra(CalendarContract.Events.TITLE, title)
                    .putExtra(CalendarContract.EXTRA_EVENT_BEGIN_TIME, (long) beginMs)
                    .putExtra(CalendarContract.EXTRA_EVENT_END_TIME, (long) endMs);
            if (place != null && !place.isEmpty()) i.putExtra(CalendarContract.Events.EVENT_LOCATION, place);
            return startSafely(i);
        }

        @JavascriptInterface
        public void httpGet(String id, String url) { MainActivity.this.httpGet(id, url); }

        // ----- 常駐 -----
        @JavascriptInterface
        public void setResident(boolean on) {
            prefs.edit().putString("resident", on ? "1" : "0").apply();
            main.post(() -> {
                if (on) {
                    if (android.os.Build.VERSION.SDK_INT >= 33
                            && checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) {
                        requestPermissions(new String[]{Manifest.permission.POST_NOTIFICATIONS}, REQ_NOTIF);
                    }
                    try { JarvisService.start(MainActivity.this); } catch (Exception ignored) {}
                } else {
                    JarvisService.stop(MainActivity.this);
                }
            });
        }

        @JavascriptInterface
        public boolean isResident() { return "1".equals(prefs.getString("resident", "0")); }

        @JavascriptInterface
        public void setOrb(boolean on) {
            prefs.edit().putString("orb", on ? "1" : "0").apply();
            if (isResident()) main.post(() -> { try { JarvisService.start(MainActivity.this); } catch (Exception ignored) {} });
        }

        @JavascriptInterface
        public boolean isOrb() { return "1".equals(prefs.getString("orb", "1")); }

        @JavascriptInterface
        public boolean canOverlay() {
            return android.os.Build.VERSION.SDK_INT < 23 || android.provider.Settings.canDrawOverlays(MainActivity.this);
        }

        /** 「他のアプリの上に表示」の許可画面を開く */
        @JavascriptInterface
        public void openOverlaySettings() {
            startSafely(new Intent(android.provider.Settings.ACTION_MANAGE_OVERLAY_PERMISSION,
                    Uri.parse("package:" + getPackageName())));
        }

        /** 「デフォルトのアシスタント」設定画面を開く */
        @JavascriptInterface
        public void openAssistantSettings() {
            if (!startSafely(new Intent(android.provider.Settings.ACTION_VOICE_INPUT_SETTINGS)))
                startSafely(new Intent(android.provider.Settings.ACTION_SETTINGS));
        }
    }
}
