package com.tatsuya.jarvis;

import android.Manifest;
import android.app.Activity;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.content.pm.ResolveInfo;
import android.graphics.Color;
import android.media.AudioManager;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.provider.AlarmClock;
import android.speech.RecognitionListener;
import android.speech.RecognizerIntent;
import android.speech.SpeechRecognizer;
import android.speech.tts.TextToSpeech;
import android.speech.tts.UtteranceProgressListener;
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

/**
 * ジャービス本体。
 * 画面（見た目・会話のロジック）は assets/index.html の Web 技術で作り、
 * スマホの機能（声・マイク・タイマー・アラーム・アプリ起動・通信）はこの Java 側が担当する。
 * 2つは "Android" という名前の橋（JavascriptInterface）でつながっている。
 */
public class MainActivity extends Activity {

    private static final int REQ_MIC = 1;

    private WebView web;
    private TextToSpeech tts;
    private boolean ttsReady = false;
    private SpeechRecognizer recognizer;
    private SharedPreferences prefs;
    private final Handler main = new Handler(Looper.getMainLooper());
    private boolean pendingListen = false;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        Window w = getWindow();
        w.setStatusBarColor(Color.parseColor("#070B10"));
        w.setNavigationBarColor(Color.parseColor("#070B10"));
        w.addFlags(WindowManager.LayoutParams.FLAG_DRAWS_SYSTEM_BAR_BACKGROUNDS);
        setVolumeControlStream(AudioManager.STREAM_MUSIC);

        prefs = getSharedPreferences("jarvis", Context.MODE_PRIVATE);

        web = new WebView(this);
        web.setBackgroundColor(Color.parseColor("#070B10"));
        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setMediaPlaybackRequiresUserGesture(false);
        web.setWebChromeClient(new WebChromeClient());
        web.addJavascriptInterface(new Bridge(), "Android");
        setContentView(web);
        web.loadUrl("file:///android_asset/index.html");

        tts = new TextToSpeech(this, status -> {
            if (status == TextToSpeech.SUCCESS) {
                int r = tts.setLanguage(Locale.JAPAN);
                ttsReady = r != TextToSpeech.LANG_MISSING_DATA && r != TextToSpeech.LANG_NOT_SUPPORTED;
                tts.setPitch(prefs.getFloat("pitch", 0.9f));
                tts.setSpeechRate(prefs.getFloat("rate", 1.05f));
                tts.setOnUtteranceProgressListener(new UtteranceProgressListener() {
                    @Override public void onStart(String id) { js("jarvis.onSpeak('start')"); }
                    @Override public void onDone(String id) { js("jarvis.onSpeak('end')"); }
                    @Override public void onError(String id) { js("jarvis.onSpeak('end')"); }
                });
            }
        });
    }

    /** ASSIST（アシスタント呼び出し）で起動されたら、すぐ聞き取りを始める */
    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        if (Intent.ACTION_ASSIST.equals(intent.getAction())) js("jarvis.onAssist()");
    }

    @Override
    protected void onResume() {
        super.onResume();
        if (Intent.ACTION_ASSIST.equals(getIntent().getAction())) {
            getIntent().setAction(Intent.ACTION_MAIN);
            main.postDelayed(() -> js("jarvis.onAssist()"), 600);
        }
    }

    @Override
    public void onBackPressed() {
        js("jarvis.onBack()");
    }

    @Override
    protected void onDestroy() {
        if (tts != null) { tts.stop(); tts.shutdown(); }
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
        if (tts != null) tts.stop();
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
        if (code == REQ_MIC) {
            boolean ok = results.length > 0 && results[0] == PackageManager.PERMISSION_GRANTED;
            if (ok && pendingListen) startListening();
            else if (!ok) js("jarvis.onListenError('permission')");
            pendingListen = false;
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

    // ---------------- 通信（Gemini） ----------------

    private void geminiRequest(final String id, final String modelOverride, final String body) {
        new Thread(() -> {
            String result;
            try {
                String key = prefs.getString("gemini_key", "");
                String model = (modelOverride != null && !modelOverride.isEmpty())
                        ? modelOverride : prefs.getString("gemini_model", "gemini-flash-latest");
                if (key.isEmpty()) throw new Exception("NO_KEY");
                URL url = new URL("https://generativelanguage.googleapis.com/v1beta/models/"
                        + URLEncoder.encode(model, "UTF-8") + ":generateContent");
                HttpURLConnection c = (HttpURLConnection) url.openConnection();
                c.setRequestMethod("POST");
                c.setConnectTimeout(15000);
                c.setReadTimeout(60000);
                c.setDoOutput(true);
                c.setRequestProperty("Content-Type", "application/json; charset=utf-8");
                c.setRequestProperty("x-goog-api-key", key);
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
                    wrap.put("error", String.valueOf(e.getMessage()));
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
                if (tts == null || !ttsReady) { js("jarvis.onSpeak('end')"); return; }
                Bundle p = new Bundle();
                tts.speak(text, TextToSpeech.QUEUE_FLUSH, p, "u" + System.currentTimeMillis());
            });
        }

        @JavascriptInterface
        public void stopSpeak() { main.post(() -> { if (tts != null) tts.stop(); }); }

        @JavascriptInterface
        public void setVoice(float rate, float pitch) {
            prefs.edit().putFloat("rate", rate).putFloat("pitch", pitch).apply();
            main.post(() -> { if (tts != null) { tts.setSpeechRate(rate); tts.setPitch(pitch); } });
        }

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
        public void gemini(String id, String model, String body) { geminiRequest(id, model, body); }

        @JavascriptInterface
        public String getPref(String k) { return prefs.getString(k, ""); }

        @JavascriptInterface
        public void setPref(String k, String v) { prefs.edit().putString(k, v).apply(); }

        @JavascriptInterface
        public void exitApp() { main.post(() -> moveTaskToBack(true)); }
    }
}
