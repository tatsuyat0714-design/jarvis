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
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.LinkedBlockingQueue;

/** Speaker の Android 側の部品: 読み上げエンジン（TextToSpeech）と、加工した音を流す出力（AudioTrack）。 */
final class AndroidVoice implements Speaker.Engine {

    private final Context ctx;
    private final TextToSpeech tts;

    private AndroidVoice(Context ctx, TextToSpeech tts) {
        this.ctx = ctx;
        this.tts = tts;
    }

    /** 準備のできた TextToSpeech から Speaker を作る。 */
    static Speaker create(Context ctx, TextToSpeech tts, Handler main, Speaker.Listener listener) {
        AndroidVoice v = new AndroidVoice(ctx, tts);
        ExecutorService worker = Executors.newSingleThreadExecutor();
        Speaker s = new Speaker(v, Stream::open, main::post, worker, listener);
        tts.setOnUtteranceProgressListener(new UtteranceProgressListener() {
            @Override public void onStart(String id) { main.post(() -> s.onUtteranceStart(id)); }
            @Override public void onDone(String id) { main.post(() -> s.onUtteranceDone(id, true)); }
            @Override public void onError(String id) { main.post(() -> s.onUtteranceDone(id, false)); }
        });
        v.cleanup();
        return s;
    }

    @Override public void setVoice(float rate, float pitch) {
        tts.setSpeechRate(rate);
        tts.setPitch(pitch);
    }

    @Override public void speak(String text, float volume, String id) {
        Bundle b = new Bundle();
        b.putFloat(TextToSpeech.Engine.KEY_PARAM_VOLUME, volume);
        tts.speak(text, TextToSpeech.QUEUE_ADD, b, id);
    }

    @Override public void silence(long ms, String id) { tts.playSilentUtterance(ms, TextToSpeech.QUEUE_ADD, id); }

    private File file(String id) { return new File(ctx.getCacheDir(), "voice_" + id + ".wav"); }

    @Override public boolean synthesize(String text, String id) {
        return tts.synthesizeToFile(text, new Bundle(), file(id), id) == TextToSpeech.SUCCESS;
    }

    @Override public byte[] takeAudio(String id) {
        File f = file(id);
        try {
            return Files.readAllBytes(f.toPath());
        } catch (Exception e) {
            return null;
        } finally {
            f.delete();
        }
    }

    @Override public void stop() {
        tts.stop();
        cleanup();
    }

    /** 途中でやめた分の書き出しファイルを消す（読み込み中のものは開いたまま読み終えられる）。 */
    private void cleanup() {
        File[] old = ctx.getCacheDir().listFiles((d, n) -> n.startsWith("voice_") && n.endsWith(".wav"));
        if (old != null) for (File f : old) f.delete();
    }

    /** 加工済みの音を順に流すストリーム再生。書き込みは専用スレッドで（write が鳴り終わるまで待つため）。 */
    static final class Stream implements Speaker.Output {
        private static final short[] END = new short[0];
        private final int rate;
        private final AudioTrack track;
        private final LinkedBlockingQueue<short[]> queue = new LinkedBlockingQueue<>();
        private volatile boolean cancelled, closed;

        private Stream(AudioTrack track, int rate) {
            this.track = track;
            this.rate = rate;
        }

        static Speaker.Output open(int rate, int playbackRate, Runnable onStarted, Runnable onDone) {
            try {
                int min = AudioTrack.getMinBufferSize(rate, AudioFormat.CHANNEL_OUT_MONO, AudioFormat.ENCODING_PCM_16BIT);
                AudioTrack t = new AudioTrack.Builder()
                        .setAudioAttributes(new AudioAttributes.Builder()
                                .setUsage(AudioAttributes.USAGE_MEDIA)
                                .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH).build())
                        .setAudioFormat(new AudioFormat.Builder()
                                .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
                                .setSampleRate(rate)
                                .setChannelMask(AudioFormat.CHANNEL_OUT_MONO).build())
                        .setBufferSizeInBytes(Math.max(min, rate / 5 * 2))
                        .setTransferMode(AudioTrack.MODE_STREAM)
                        .build();
                t.setPlaybackRate(playbackRate);
                Stream s = new Stream(t, rate);
                new Thread(() -> s.run(onStarted, onDone), "jarvis-voice-out").start();
                return s;
            } catch (Exception e) {
                return null;
            }
        }

        @Override public int sampleRate() { return rate; }

        @Override public void add(short[] s) { if (!closed) queue.add(s); }

        @Override public void close() {
            if (closed) return;
            closed = true;
            queue.add(END);
        }

        @Override public void cancel() {
            cancelled = true;
            queue.add(END);
        }

        private void run(Runnable onStarted, Runnable onDone) {
            long written = 0;
            boolean playing = false;
            try {
                while (!cancelled) {
                    short[] s = queue.take();
                    if (s == END) break;
                    if (!playing) { track.play(); playing = true; onStarted.run(); }
                    int off = 0;
                    while (off < s.length && !cancelled) {
                        int n = track.write(s, off, s.length - off);
                        if (n <= 0) { cancelled = true; break; }
                        off += n;
                    }
                    written += off;
                }
                // 書き込んだ分が鳴り終わるまで待つ（端末によっては位置が進まないことがあるので上限つき）
                long limit = System.currentTimeMillis() + 2000 + written * 1000L / Math.max(1, rate);
                while (!cancelled && playing && (track.getPlaybackHeadPosition() & 0xffffffffL) < written
                        && System.currentTimeMillis() < limit) {
                    Thread.sleep(20);
                }
            } catch (Exception ignored) {
            } finally {
                try { track.stop(); } catch (Exception ignored) {}
                track.release();
                if (!cancelled) onDone.run();
            }
        }
    }
}
