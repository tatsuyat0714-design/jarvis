package com.tatsuya.jarvis;

import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.Executor;

/**
 * 読み上げの進行役。1 回の返事を「セッション」として扱い、文が届くたびにすぐ話し始める（AI の返事を待ち切らない）。
 *   begin() → append(文) を何度でも → finish()
 * 調声の値（VoiceFx.Params）に応じて 2 通りで鳴らす。
 * - 加工が要らない: 文ごとに読み上げエンジンの待ち行列へ入れる（届いた文から順に話す）
 * - 加工が要る   : 文ごとに音声を書き出し → VoiceFx で加工 → 出力（ストリーム）へ流す。
 *                  1 文目を鳴らしている間に 2 文目を書き出すので、加工しても待ち時間は 1 文分だけ。
 *   加工に失敗したら、その文から先は（流した分が鳴り終わってから）加工せずに話す。
 * 話し始め・話し終わりは Listener にセッションごと 1 回ずつ通知する（会話モードの「次を聞き取る」の合図）。
 * 話し終わりは finish() の後、最後の音が鳴り終わってから。
 *
 * Android の部品（TextToSpeech・AudioTrack）は Engine / Output の向こう側（AndroidVoice）にあるので、
 * この進行役は PC の JUnit でそのまま試せる。呼び出しと通知はすべて 1 本のスレッド（メイン）で行う。
 */
final class Speaker {

    interface Listener {
        void onStart();
        void onEnd();
    }

    /** 読み上げエンジン。結果は Speaker.onUtteranceStart / onUtteranceDone でメインスレッドに返す。 */
    interface Engine {
        void setVoice(float rate, float pitch);
        /** 待ち行列の後ろに足して話す。 */
        void speak(String text, float volume, String id);
        void silence(long ms, String id);
        /** 音声を書き出す（話さない）。始められなければ false。 */
        boolean synthesize(String text, String id);
        /** 書き出した音声（WAV）を取り出して消す。別スレッドから呼ぶ。 */
        byte[] takeAudio(String id);
        void stop();
    }

    /** 加工した音を順に鳴らす出力。 */
    interface Output {
        int sampleRate();
        void add(short[] samples);
        /** これ以上足さない。鳴り終わったら onDone。 */
        void close();
        /** すぐ止める（onDone は呼ばない）。 */
        void cancel();
    }

    interface OutputFactory {
        /** onStarted・onDone は別スレッドから呼ばれてよい（Speaker がメインに戻す）。開けなければ null。 */
        Output open(int sampleRate, int playbackRate, Runnable onStarted, Runnable onDone);
    }

    private final Engine engine;
    private final OutputFactory outputs;
    private final Executor main;       // メインスレッドへ戻す
    private final Executor fxWorker;   // 加工（順番どおり 1 本で）
    private final Listener listener;
    private VoiceFx.Params params = new VoiceFx.Params();

    // セッションの状態（gen が変わったら古い通知・古い加工結果は捨てる）
    private int gen = 0;
    private boolean active, closed, started, ended;
    private boolean fx;                       // 加工して鳴らすセッションか
    private VoiceFx.Params sessionParams;
    // そのまま話す
    private int issued, done;
    // 加工して話す
    private final ArrayDeque<String> toSynth = new ArrayDeque<>();
    private final List<String> fxTexts = new ArrayList<>();    // 書き出しに回した文（番号順）
    private int synthesized, processed;
    private int fxToken;                      // 加工をやめたら増やす（遅れて届いた加工結果を捨てる）
    private String synthText;                 // 書き出し中の文
    private final List<String> deferredDirect = new ArrayList<>();   // 加工した音が鳴り終わってから、そのまま話す文
    private Output output;

    Speaker(Engine engine, OutputFactory outputs, Executor main, Executor fxWorker, Listener listener) {
        this.engine = engine;
        this.outputs = outputs;
        this.main = main;
        this.fxWorker = fxWorker;
        this.listener = listener;
    }

    void setParams(VoiceFx.Params p) {
        params = p;
        if (!active || !fx) applyDirect();
    }

    VoiceFx.Params params() { return params; }

    boolean isActive() { return active; }

    /** 文章をまとめて話す。 */
    void speak(String text) {
        begin();
        // 文の間を空けないなら 1 つのまま渡す（文をまたいだ抑揚を崩さない）
        if (params.pauseMs < 1f) append(text);
        else for (String s : VoiceFx.sentences(text == null ? "" : text)) append(s);
        finish();
    }

    /** 新しい返事を始める（話している途中のものは止める）。 */
    void begin() {
        stop();
        active = true;
        closed = started = ended = false;
        sessionParams = params;
        fx = params.needsProcessing();
        issued = done = 0;
        synthesized = processed = 0;
        synthText = null;
        toSynth.clear();
        fxTexts.clear();
        deferredDirect.clear();
    }

    /** 文（または文のかたまり）を足す。届いた順にすぐ話し始める。 */
    void append(String text) {
        if (!active || closed || text == null || text.trim().isEmpty()) return;
        String t = text.trim();
        if (fx) {
            toSynth.add(t);
            synthNext();
        } else if (output != null) {
            deferredDirect.add(t);   // 加工した音がまだ鳴っている（途中で加工をやめた）
        } else {
            speakDirect(t);
        }
    }

    /** もう文は来ない。鳴らし終えたら onEnd。 */
    void finish() {
        if (!active || closed) return;
        closed = true;
        checkEnd();
    }

    void stop() {
        gen++;
        active = false;
        toSynth.clear();
        synthText = null;
        engine.stop();
        if (output != null) { output.cancel(); output = null; }
        applyDirect();
    }

    // ---------------------------------------------------------------- そのまま話す

    private void applyDirect() { engine.setVoice(params.rate, params.pitch); }

    private void speakDirect(String text) {
        applyDirect();
        if (issued > 0 && params.pauseMs >= 1f) engine.silence((long) params.pauseMs, "s" + gen + "_" + issued);
        engine.speak(text, Math.min(1f, params.gain), "d" + gen + "_" + issued);
        issued++;
    }

    // ---------------------------------------------------------------- 加工して話す

    /** 書き出しは 1 文ずつ順番に（読み上げエンジンは同時に 1 つしか書き出せない）。 */
    private void synthNext() {
        if (synthText != null || toSynth.isEmpty()) return;
        synthText = toSynth.poll();
        // 声の太さ: 読み上げを 1/f の高さ・速さで作り、f 倍の速さで再生する
        engine.setVoice(VoiceFx.synthRate(sessionParams), VoiceFx.synthPitch(sessionParams));
        int i = fxTexts.size();
        fxTexts.add(synthText);
        if (!engine.synthesize(synthText, "f" + gen + "_" + i)) fallbackToDirect(i);
    }

    private void onSynthesized(int i, boolean ok) {
        if (!ok) { fallbackToDirect(i); return; }
        final int g = gen, token = fxToken;
        final VoiceFx.Params p = sessionParams;
        final String id = "f" + gen + "_" + i;
        synthText = null;
        synthesized++;
        applyDirect();
        fxWorker.execute(() -> {
            short[] out = null;
            int rate = 0;
            try {
                VoiceFx.Pcm pcm = VoiceFx.readWav(engine.takeAudio(id));
                if (pcm != null) { out = VoiceFx.process(pcm.samples, pcm.sampleRate, p); rate = pcm.sampleRate; }
            } catch (Exception | OutOfMemoryError ignored) {}
            final short[] samples = out;
            final int sr = rate;
            main.execute(() -> {
                if (g != gen || token != fxToken) return;
                if (samples == null || samples.length == 0 || !enqueue(samples, sr, p)) {
                    fallbackToDirect(i);   // この文から先は加工せずに話す
                    return;
                }
                processed++;
                checkEnd();
            });
        });
        synthNext();   // 鳴らしている間に次の文を書き出す
    }

    private boolean enqueue(short[] s, int rate, VoiceFx.Params p) {
        if (output == null) {
            final int g = gen;
            output = outputs.open(rate, VoiceFx.playbackRate(rate, p),
                    () -> main.execute(() -> { if (g == gen) notifyStart(); }),
                    () -> main.execute(() -> onOutputDone(g)));
            if (output == null) return false;
        } else if (output.sampleRate() != rate) {
            return false;
        }
        if (processed > 0 && p.pauseMs >= 1f) output.add(new short[Math.round(rate * p.pauseMs / 1000f)]);
        output.add(s);
        return true;
    }

    /**
     * 加工できなかった: 番号 from の文から先は加工せずに話す。すでに加工して流した分を鳴らし切ってから話すので、
     * 順番が入れ替わったり重なったりしない。
     */
    private void fallbackToDirect(int from) {
        List<String> rest = new ArrayList<>(fxTexts.subList(Math.min(from, fxTexts.size()), fxTexts.size()));
        rest.addAll(toSynth);
        toSynth.clear();
        synthText = null;
        fx = false;
        fxToken++;
        synthesized = processed;
        applyDirect();
        if (output != null) {
            deferredDirect.addAll(rest);
            output.close();   // 鳴り終われば onOutputDone で話す
        } else {
            for (String t : rest) speakDirect(t);
        }
        checkEnd();
    }

    private void onOutputDone(int g) {
        if (g != gen) return;
        output = null;
        List<String> rest = new ArrayList<>(deferredDirect);
        deferredDirect.clear();
        for (String t : rest) speakDirect(t);
        checkEnd();
    }

    // ---------------------------------------------------------------- 終わりの判定

    private void checkEnd() {
        if (!active || !closed || ended) return;
        boolean directDone = done >= issued;
        boolean fxDone = synthText == null && toSynth.isEmpty() && processed >= synthesized && deferredDirect.isEmpty();
        if (!directDone || !fxDone) return;
        if (output != null) { output.close(); return; }   // 鳴り終わったら onOutputDone からもう一度来る
        ended = true;
        active = false;
        if (!started) { started = true; listener.onStart(); }
        listener.onEnd();
    }

    private void notifyStart() {
        if (started) return;
        started = true;
        listener.onStart();
    }

    // ---------------------------------------------------------------- 読み上げエンジンからの通知（メインスレッド）

    /** id = 種類 + 世代 + "_" + 番号。今の世代でなければ -1。 */
    private int parse(String id, String kind) {
        if (id == null || !id.startsWith(kind)) return -1;
        String[] a = id.substring(kind.length()).split("_");
        try {
            if (a.length != 2 || Integer.parseInt(a[0]) != gen) return -1;
            return Integer.parseInt(a[1]);
        } catch (NumberFormatException e) {
            return -1;
        }
    }

    void onUtteranceStart(String id) {
        if (parse(id, "d") >= 0) notifyStart();
    }

    void onUtteranceDone(String id, boolean ok) {
        if (parse(id, "d") >= 0) {
            done++;
            checkEnd();
            return;
        }
        int f = parse(id, "f");
        if (f >= 0 && synthText != null && f == fxTexts.size() - 1) onSynthesized(f, ok);
    }
}
