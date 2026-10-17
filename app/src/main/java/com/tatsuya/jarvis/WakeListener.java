package com.tatsuya.jarvis;

import android.annotation.SuppressLint;
import android.media.AudioFormat;
import android.media.AudioRecord;
import android.media.MediaRecorder;
import android.os.Handler;
import android.os.Process;

/**
 * マイクの音を 10ms ずつ WakeWord.Detector に流し、呼びかけを見つけたらメインスレッドで知らせる。
 * 登録用の録音（record）も同じ設定（音声認識向けの入力・16kHz・モノラル）で録るので、登録時と常駐時の音がそろう。
 */
final class WakeListener {

    interface Callback {
        void onWake();
        /**
         * マイクが使えなくなった（通話や録音アプリに取られた等）。少し待って開き直す。
         * Android はほかのアプリがマイクを使っている間、後ろで動くアプリには無音（0 だけ）を渡すので、それも含む
         */
        void onMicLost();
    }

    private final WakeWord.Detector detector;
    private final Callback callback;
    private final Handler main;
    private volatile boolean running;
    private AudioRecord rec;
    private Thread thread;

    WakeListener(WakeWord.Detector detector, Handler main, Callback callback) {
        this.detector = detector;
        this.main = main;
        this.callback = callback;
    }

    @SuppressLint("MissingPermission")   // 呼び出し側でマイクの許可を確かめてから使う
    static AudioRecord open() {
        int min = AudioRecord.getMinBufferSize(WakeWord.RATE, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT);
        AudioRecord r = new AudioRecord(MediaRecorder.AudioSource.VOICE_RECOGNITION, WakeWord.RATE,
                AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT, Math.max(min, WakeWord.RATE / 2 * 2));
        if (r.getState() != AudioRecord.STATE_INITIALIZED) { r.release(); return null; }
        return r;
    }

    /** マイクを開いて聞き始める。開けなければ false。 */
    synchronized boolean start() {
        if (running) return true;
        try {
            rec = open();
            if (rec == null) return false;
            rec.startRecording();
            if (rec.getRecordingState() != AudioRecord.RECORDSTATE_RECORDING) { rec.release(); rec = null; return false; }
        } catch (Exception e) {
            if (rec != null) rec.release();
            rec = null;
            return false;
        }
        running = true;
        final AudioRecord r = rec;
        thread = new Thread(() -> {
            Process.setThreadPriority(Process.THREAD_PRIORITY_AUDIO);
            short[] hop = new short[WakeWord.HOP];
            int zeroHops = 0;
            while (running) {
                int n = 0;
                while (running && n < hop.length) {
                    int got = r.read(hop, n, hop.length - n);
                    if (got <= 0) {
                        if (running) { running = false; main.post(callback::onMicLost); }
                        break;
                    }
                    n += got;
                }
                if (!running) break;
                boolean zero = true;
                for (short v : hop) if (v != 0) { zero = false; break; }
                zeroHops = zero ? zeroHops + 1 : 0;
                if (zeroHops >= 300) {   // 3 秒間まったくの 0 = マイクを止められている
                    running = false;
                    main.post(callback::onMicLost);
                    break;
                }
                if (detector.push(hop)) main.post(callback::onWake);
            }
        }, "jarvis-wake");
        thread.start();
        return true;
    }

    synchronized void stop() {
        running = false;
        if (rec != null) {
            try { rec.stop(); } catch (Exception ignored) {}
        }
        if (thread != null) {
            try { thread.join(500); } catch (InterruptedException ignored) {}
            thread = null;
        }
        if (rec != null) { rec.release(); rec = null; }
    }

    boolean isRunning() { return running; }

    /** 登録用に ms ミリ秒録音する（呼び出したスレッドで待つ）。開けなければ null。 */
    static short[] record(int ms) {
        AudioRecord r = open();
        if (r == null) return null;
        short[] out = new short[WakeWord.RATE * ms / 1000];
        try {
            r.startRecording();
            int n = 0;
            while (n < out.length) {
                int got = r.read(out, n, Math.min(1600, out.length - n));
                if (got <= 0) return null;
                n += got;
            }
            return out;
        } catch (Exception e) {
            return null;
        } finally {
            try { r.stop(); } catch (Exception ignored) {}
            r.release();
        }
    }
}
