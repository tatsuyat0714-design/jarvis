package com.tatsuya.jarvis;

import android.content.Context;
import android.media.AudioAttributes;
import android.media.AudioFormat;
import android.media.AudioTrack;
import android.os.Bundle;
import android.os.Handler;
import android.speech.tts.TextToSpeech;
import android.speech.tts.UtteranceProgressListener;

import java.io.File;
import java.nio.file.Files;

/**
 * 読み上げ。調声の値（VoiceFx.Params）に応じて 2 通りで鳴らす。
 * - 加工が要らない: TextToSpeech でそのまま話す（速い）。文の間を空けるときは文ごとに区切って無音を挟む
 * - 加工が要る   : 文ごとに WAV へ書き出し → つなぐ → VoiceFx で加工 → AudioTrack で鳴らす
 *   加工に失敗したら（WAV が読めない等）そのまま話す方に切り替える。
 * 話し始め・話し終わりは Listener に 1 回ずつ通知する（会話モードの「次を聞き取る」の合図になるため）。
 * 呼び出しはすべてメインスレッドから。
 */
final class Speaker {

    interface Listener {
        void onStart();
        void onEnd();
    }

    private final Context ctx;
    private final TextToSpeech tts;
    private final Handler main;
    private final Listener listener;
    private VoiceFx.Params params = new VoiceFx.Params();
    private int gen = 0;              // speak / stop のたびに増やす。古い通知・古い加工結果は捨てる
    private String[] pending;         // 加工用に書き出し中の文
    private AudioTrack track;
    private boolean started;

    Speaker(Context ctx, TextToSpeech tts, Handler main, Listener listener) {
        this.ctx = ctx;
        this.tts = tts;
        this.main = main;
        this.listener = listener;
        tts.setOnUtteranceProgressListener(new UtteranceProgressListener() {
            @Override public void onStart(String id) { main.post(() -> onTtsStart(id)); }
            @Override public void onDone(String id) { main.post(() -> onTtsDone(id, true)); }
            @Override public void onError(String id) { main.post(() -> onTtsDone(id, false)); }
        });
    }

    void setParams(VoiceFx.Params p) {
        params = p;
        applyDirect();
    }

    VoiceFx.Params params() { return params; }

    void speak(String text) {
        stop();
        String[] ss = VoiceFx.sentences(text == null ? "" : text);
        if (ss.length == 0) { listener.onEnd(); return; }
        // 文の間を空けないなら 1 つのまま渡す（文をまたいだ抑揚を崩さない）
        if (params.pauseMs < 1f) ss = new String[]{String.join(" ", ss)};
        started = false;
        if (params.needsProcessing()) synthesize(ss, 0);
        else speakDirect(ss);
    }

    void stop() {
        gen++;
        pending = null;
        tts.stop();
        releaseTrack();
        applyDirect();
        // 途中でやめた分の書き出しファイルを消す（読み込み中のものは開いたまま読み終えられる）
        File[] old = ctx.getCacheDir().listFiles((d, n) -> n.startsWith("voice_") && n.endsWith(".wav"));
        if (old != null) for (File f : old) f.delete();
    }

    void shutdown() {
        stop();
        tts.shutdown();
    }

    // ---------------------------------------------------------------- そのまま話す

    private void applyDirect() {
        tts.setSpeechRate(params.rate);
        tts.setPitch(params.pitch);
    }

    private void speakDirect(String[] ss) {
        applyDirect();
        Bundle b = new Bundle();
        b.putFloat(TextToSpeech.Engine.KEY_PARAM_VOLUME, Math.min(1f, params.gain));
        for (int i = 0; i < ss.length; i++) {
            tts.speak(ss[i], i == 0 ? TextToSpeech.QUEUE_FLUSH : TextToSpeech.QUEUE_ADD, b, id("d", i, ss.length));
            if (i < ss.length - 1 && params.pauseMs >= 1f) {
                tts.playSilentUtterance((long) params.pauseMs, TextToSpeech.QUEUE_ADD, "s" + gen);
            }
        }
    }

    // ---------------------------------------------------------------- 加工して話す

    private void synthesize(String[] ss, int i) {
        pending = ss;
        // 声の太さ: 読み上げを 1/f の高さ・速さで作り、f 倍の速さで再生する（途中で試し聞きの値が変わっても毎回合わせる）
        tts.setSpeechRate(VoiceFx.synthRate(params));
        tts.setPitch(VoiceFx.synthPitch(params));
        int r = tts.synthesizeToFile(ss[i], new Bundle(), part(gen, i), id("f", i, ss.length));
        if (r != TextToSpeech.SUCCESS) fallback();
    }

    private File part(int g, int i) { return new File(ctx.getCacheDir(), "voice_" + g + "_" + i + ".wav"); }

    private void fallback() {
        String[] ss = pending;
        pending = null;
        if (ss == null) return;
        speakDirect(ss);
    }

    private void processAndPlay(final int g, final String[] ss) {
        applyDirect();
        final VoiceFx.Params p = params;
        new Thread(() -> {
            VoiceFx.Pcm pcm = null;
            short[] out = null;
            try {
                VoiceFx.Pcm[] parts = new VoiceFx.Pcm[ss.length];
                for (int i = 0; i < ss.length; i++) {
                    File f = part(g, i);
                    parts[i] = VoiceFx.readWav(Files.readAllBytes(f.toPath()));
                    f.delete();
                }
                pcm = VoiceFx.join(parts, p.pauseMs);
                if (pcm != null) out = VoiceFx.process(pcm.samples, pcm.sampleRate, p);
            } catch (Exception | OutOfMemoryError ignored) {}
            final VoiceFx.Pcm src = pcm;
            final short[] samples = out;
            main.post(() -> {
                if (g != gen) return;
                if (samples == null || samples.length == 0 || !play(samples, src.sampleRate, p)) {
                    pending = ss;
                    fallback();
                }
            });
        }, "jarvis-voice").start();
    }

    private boolean play(short[] s, int rate, VoiceFx.Params p) {
        try {
            AudioTrack t = new AudioTrack.Builder()
                    .setAudioAttributes(new AudioAttributes.Builder()
                            .setUsage(AudioAttributes.USAGE_MEDIA)
                            .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH).build())
                    .setAudioFormat(new AudioFormat.Builder()
                            .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
                            .setSampleRate(rate)
                            .setChannelMask(AudioFormat.CHANNEL_OUT_MONO).build())
                    .setBufferSizeInBytes(s.length * 2)
                    .setTransferMode(AudioTrack.MODE_STATIC)
                    .build();
            if (t.write(s, 0, s.length) != s.length) { t.release(); return false; }
            t.setPlaybackRate(VoiceFx.playbackRate(rate, p));
            final int g = gen;
            t.setNotificationMarkerPosition(s.length);
            t.setPlaybackPositionUpdateListener(new AudioTrack.OnPlaybackPositionUpdateListener() {
                @Override public void onMarkerReached(AudioTrack a) { finish(g); }
                @Override public void onPeriodicNotification(AudioTrack a) {}
            }, main);
            track = t;
            t.play();
            notifyStart();
            // 端末によってはマーカーが来ないことがあるので、長さ + 余裕で必ず終わらせる
            long ms = (long) (1000.0 * s.length / VoiceFx.playbackRate(rate, p)) + 1500;
            main.postDelayed(() -> finish(g), ms);
            return true;
        } catch (Exception e) {
            releaseTrack();
            return false;
        }
    }

    private void finish(int g) {
        if (g != gen || track == null) return;
        releaseTrack();
        listener.onEnd();
    }

    private void releaseTrack() {
        if (track == null) return;
        try { track.stop(); } catch (Exception ignored) {}
        track.release();
        track = null;
    }

    // ---------------------------------------------------------------- 読み上げエンジンからの通知

    private String id(String kind, int i, int n) { return kind + gen + "_" + i + "_" + n; }

    /** id = 種類 + 世代 + "_" + 何文目 + "_" + 全文数。今の世代でなければ null。 */
    private int[] parse(String id, String kind) {
        if (id == null || !id.startsWith(kind)) return null;
        String[] a = id.substring(kind.length()).split("_");
        try {
            if (a.length != 3 || Integer.parseInt(a[0]) != gen) return null;
            return new int[]{Integer.parseInt(a[1]), Integer.parseInt(a[2])};
        } catch (NumberFormatException e) {
            return null;
        }
    }

    private void notifyStart() {
        if (started) return;
        started = true;
        listener.onStart();
    }

    private void onTtsStart(String id) {
        if (parse(id, "d") != null) notifyStart();
    }

    private void onTtsDone(String id, boolean ok) {
        int[] d = parse(id, "d");
        if (d != null) {
            if (d[0] == d[1] - 1) listener.onEnd();   // 最後の文が終わったら 1 回だけ
            return;
        }
        int[] f = parse(id, "f");
        if (f == null || pending == null) return;
        if (!ok) { fallback(); return; }
        if (f[0] + 1 < f[1]) synthesize(pending, f[0] + 1);
        else { String[] ss = pending; pending = null; processAndPlay(gen, ss); }
    }
}
