package com.tatsuya.jarvis;

import org.json.JSONObject;

import java.nio.ByteBuffer;
import java.nio.ByteOrder;

/**
 * 調声（声の加工）。Android の読み上げ（TextToSpeech）が出せるのは「速さ・高さ・声の種類」だけなので、
 * それ以外は読み上げ結果の音声（WAV）をここで加工してから鳴らす。
 *
 * 声の太さ（フォルマント）: 再生速度を f 倍にすると、高さ・速さ・声道の響き（フォルマント）が全部 f 倍になる。
 * 読み上げ時に高さと速さを 1/f にしておけば、最終的に変わるのは声道の響きだけになる（f &lt; 1 で太く・大人っぽく）。
 * 低音・高音（シェルビング EQ）→ 機械っぽさ（短いコム）→ エコー → 響き（Schroeder リバーブ）→ 音量 の順に掛ける。
 *
 * Android に依存しない（org.json だけ）ので、PC の JUnit でそのまま試せる。
 */
public final class VoiceFx {

    /** 調声の値。既定値は「何も加工しない」。 */
    public static final class Params {
        public float rate = 1.0f;      // 話す速さ（読み上げエンジンに渡す）
        public float pitch = 0.85f;    // 声の高さ（同上）
        public float formant = 1.0f;   // 声の太さ。0.75〜1.35、小さいほど太い
        public float bass = 0f;        // 低音 dB（-12〜12）
        public float treble = 0f;      // 高音 dB（-12〜12）
        public float metal = 0f;       // 機械っぽさ 0〜1
        public float echo = 0f;        // エコーの強さ 0〜0.8
        public float echoMs = 180f;    // エコーの間隔 40〜600ms
        public float reverb = 0f;      // 響き 0〜1
        public float gain = 1.0f;      // 音量 0.3〜2.0
        public float pauseMs = 0f;     // 文と文の間に足す無音 0〜1000ms

        public static Params fromJson(String json) {
            Params p = new Params();
            if (json == null || json.isEmpty()) return p;
            try {
                JSONObject o = new JSONObject(json);
                p.rate = get(o, "rate", p.rate, 0.3f, 2.5f);
                p.pitch = get(o, "pitch", p.pitch, 0.3f, 2.0f);
                p.formant = get(o, "formant", p.formant, 0.75f, 1.35f);
                p.bass = get(o, "bass", p.bass, -12f, 12f);
                p.treble = get(o, "treble", p.treble, -12f, 12f);
                p.metal = get(o, "metal", p.metal, 0f, 1f);
                p.echo = get(o, "echo", p.echo, 0f, 0.8f);
                p.echoMs = get(o, "echoMs", p.echoMs, 40f, 600f);
                p.reverb = get(o, "reverb", p.reverb, 0f, 1f);
                p.gain = get(o, "gain", p.gain, 0.3f, 2.0f);
                p.pauseMs = get(o, "pauseMs", p.pauseMs, 0f, 1000f);
            } catch (Exception ignored) {}
            return p;
        }

        private static float get(JSONObject o, String k, float def, float min, float max) {
            double v = o.optDouble(k, def);
            if (Double.isNaN(v) || Double.isInfinite(v)) return def;
            return (float) Math.max(min, Math.min(max, v));
        }

        /** 読み上げエンジンだけで済むか（WAV の加工が要らないか）。音量は 1 以下なら読み上げ側で下げられる。 */
        public boolean needsProcessing() {
            return Math.abs(formant - 1f) > 0.005f || Math.abs(bass) > 0.05f || Math.abs(treble) > 0.05f
                    || metal > 0.005f || echo > 0.005f || reverb > 0.005f || gain > 1.005f;
        }
    }

    /** 読み込んだ音声（モノラル 16bit 相当）。 */
    public static final class Pcm {
        public final short[] samples;
        public final int sampleRate;

        public Pcm(short[] samples, int sampleRate) {
            this.samples = samples;
            this.sampleRate = sampleRate;
        }
    }

    private VoiceFx() {}

    // ---------------------------------------------------------------- WAV

    /** RIFF/WAVE（PCM 8/16bit、何チャンネルでも）を読む。ステレオ以上はモノラルに混ぜる。読めなければ null。 */
    public static Pcm readWav(byte[] b) {
        if (b == null || b.length < 12) return null;
        ByteBuffer bb = ByteBuffer.wrap(b).order(ByteOrder.LITTLE_ENDIAN);
        if (bb.getInt(0) != 0x46464952 || bb.getInt(8) != 0x45564157) return null; // "RIFF" "WAVE"
        int pos = 12, channels = 0, rate = 0, bits = 0, format = 0;
        while (pos + 8 <= b.length) {
            int id = bb.getInt(pos);
            long size = bb.getInt(pos + 4) & 0xffffffffL;
            int body = pos + 8;
            if (id == 0x20746d66 && body + 16 <= b.length) {          // "fmt "
                format = bb.getShort(body) & 0xffff;
                channels = bb.getShort(body + 2) & 0xffff;
                rate = bb.getInt(body + 4);
                bits = bb.getShort(body + 14) & 0xffff;
            } else if (id == 0x61746164) {                            // "data"
                if (format != 1 || channels < 1 || rate <= 0 || (bits != 16 && bits != 8)) return null;
                // 書きながら作られた WAV はサイズが 0 や最大値のことがあるので、残りの長さで抑える
                long avail = b.length - body;
                long len = (size == 0 || size > avail) ? avail : size;
                int frameBytes = channels * bits / 8;
                int frames = (int) (len / frameBytes);
                short[] out = new short[frames];
                for (int i = 0; i < frames; i++) {
                    int sum = 0;
                    for (int c = 0; c < channels; c++) {
                        int at = body + i * frameBytes + c * bits / 8;
                        sum += bits == 16 ? bb.getShort(at) : ((b[at] & 0xff) - 128) << 8;
                    }
                    out[i] = (short) (sum / channels);
                }
                return new Pcm(out, rate);
            }
            pos = body + (int) Math.min(size + (size & 1), Integer.MAX_VALUE - body);  // チャンクは偶数境界
        }
        return null;
    }

    /** 複数の音声を、間に無音を挟んでつなぐ（文ごとに読み上げた結果をまとめる）。サンプルレートは先頭に合わせる。 */
    public static Pcm join(Pcm[] parts, float pauseMs) {
        int rate = 0;
        for (Pcm p : parts) if (p != null) { rate = p.sampleRate; break; }
        if (rate == 0) return null;
        int total = 0, n = 0;
        for (Pcm p : parts) {
            if (p != null && p.sampleRate == rate) { total += p.samples.length; n++; }  // 違うレートのものは混ぜない
        }
        int gap = Math.round(rate * pauseMs / 1000f);
        short[] out = new short[total + gap * (n - 1)];
        int at = 0, seen = 0;
        for (Pcm p : parts) {
            if (p == null || p.sampleRate != rate) continue;
            System.arraycopy(p.samples, 0, out, at, p.samples.length);
            at += p.samples.length;
            if (++seen < n) at += gap;
        }
        return new Pcm(out, rate);
    }

    // ---------------------------------------------------------------- 加工

    /** 加工した音声を返す（エコーや響きの余韻の分だけ長くなる）。声の太さは再生速度で掛けるのでここでは扱わない。 */
    public static short[] process(short[] in, int rate, Params p) {
        int tail = tailSamples(rate, p);
        int n = in.length + tail;
        float[] x = new float[n];
        for (int i = 0; i < in.length; i++) x[i] = in[i] / 32768f;

        if (Math.abs(p.bass) > 0.05f) shelf(x, rate, 220f, p.bass, true);
        if (Math.abs(p.treble) > 0.05f) shelf(x, rate, 3500f, p.treble, false);
        if (p.metal > 0.005f) metal(x, rate, p.metal);
        if (p.echo > 0.005f) echo(x, rate, p.echo, p.echoMs);
        if (p.reverb > 0.005f) reverb(x, rate, p.reverb);

        // 音量。上げすぎて割れないよう、ピークが 0.98 を超えるなら全体を下げる（歪ませない）
        float peak = 0f;
        for (float v : x) peak = Math.max(peak, Math.abs(v));
        float g = p.gain;
        if (peak * g > 0.98f && peak > 0f) g = Math.min(g, 0.98f / peak);
        short[] out = new short[n];
        for (int i = 0; i < n; i++) out[i] = (short) Math.max(-32768, Math.min(32767, Math.round(x[i] * g * 32768f)));
        return trimSilence(out, in.length);
    }

    /** 声の太さのための設定。読み上げ時の高さ・速さ（1/f 倍）と、再生のサンプルレート（f 倍）。 */
    public static float synthPitch(Params p) { return p.pitch / p.formant; }

    public static float synthRate(Params p) { return p.rate / p.formant; }

    public static int playbackRate(int sampleRate, Params p) { return Math.round(sampleRate * p.formant); }

    static int tailSamples(int rate, Params p) {
        float sec = 0f;
        if (p.echo > 0.005f) {
            float fb = echoFeedback(p.echo);
            // 1% まで減衰するまでの回数 × 間隔
            int repeats = (int) Math.ceil(Math.log(0.01) / Math.log(Math.max(fb, 0.05f)));
            sec = Math.max(sec, repeats * p.echoMs / 1000f);
        }
        if (p.reverb > 0.005f) sec = Math.max(sec, 0.4f + 1.4f * p.reverb);
        if (p.metal > 0.005f) sec = Math.max(sec, 0.05f);
        return Math.round(rate * Math.min(sec, 3f));
    }

    private static float echoFeedback(float amount) { return 0.15f + 0.55f * amount; }

    /** 余韻の最後の無音を切る（ただし元の長さより短くはしない）。 */
    private static short[] trimSilence(short[] s, int minLen) {
        int end = s.length;
        while (end > minLen && Math.abs(s[end - 1]) < 8) end--;
        if (end == s.length) return s;
        short[] t = new short[end];
        System.arraycopy(s, 0, t, 0, end);
        return t;
    }

    /** RBJ のシェルビングフィルタ（S=1）。 */
    static void shelf(float[] x, int rate, float freq, float dB, boolean low) {
        double A = Math.pow(10, dB / 40.0);
        double w = 2 * Math.PI * Math.min(freq, rate * 0.45) / rate;
        double cs = Math.cos(w), sn = Math.sin(w);
        double alpha = sn / 2 * Math.sqrt(2);
        double sq = 2 * Math.sqrt(A) * alpha;
        double b0, b1, b2, a0, a1, a2;
        if (low) {
            b0 = A * ((A + 1) - (A - 1) * cs + sq);
            b1 = 2 * A * ((A - 1) - (A + 1) * cs);
            b2 = A * ((A + 1) - (A - 1) * cs - sq);
            a0 = (A + 1) + (A - 1) * cs + sq;
            a1 = -2 * ((A - 1) + (A + 1) * cs);
            a2 = (A + 1) + (A - 1) * cs - sq;
        } else {
            b0 = A * ((A + 1) + (A - 1) * cs + sq);
            b1 = -2 * A * ((A - 1) + (A + 1) * cs);
            b2 = A * ((A + 1) + (A - 1) * cs - sq);
            a0 = (A + 1) - (A - 1) * cs + sq;
            a1 = 2 * ((A - 1) - (A + 1) * cs);
            a2 = (A + 1) - (A - 1) * cs - sq;
        }
        b0 /= a0; b1 /= a0; b2 /= a0; a1 /= a0; a2 /= a0;
        double x1 = 0, x2 = 0, y1 = 0, y2 = 0;
        for (int i = 0; i < x.length; i++) {
            double in = x[i];
            double y = b0 * in + b1 * x1 + b2 * x2 - a1 * y1 - a2 * y2;
            x2 = x1; x1 = in; y2 = y1; y1 = y;
            x[i] = (float) y;
        }
    }

    /** 機械っぽさ: 数 ms の帰還つきコムフィルタ（金属的な共鳴）を、ゆっくり揺らして混ぜる。 */
    static void metal(float[] x, int rate, float amount) {
        int base = Math.max(1, Math.round(rate * 0.0035f));
        int depth = Math.max(1, Math.round(rate * 0.0012f));
        float fb = 0.35f + 0.5f * amount;
        float wet = 0.25f + 0.5f * amount;
        float[] buf = new float[base + depth + 2];
        int w = 0;
        for (int i = 0; i < x.length; i++) {
            double lfo = 0.5 + 0.5 * Math.sin(2 * Math.PI * 0.6 * i / rate);
            int d = base + (int) Math.round(depth * lfo);
            int r = w - d;
            if (r < 0) r += buf.length;
            float delayed = buf[r];
            float y = x[i] + fb * delayed;
            buf[w] = y;
            if (++w == buf.length) w = 0;
            x[i] = (1 - wet) * x[i] + wet * y * (1 - fb);
        }
    }

    /** エコー（帰還つきディレイ）。 */
    static void echo(float[] x, int rate, float amount, float ms) {
        int d = Math.max(1, Math.round(rate * ms / 1000f));
        float fb = echoFeedback(amount);
        float[] buf = new float[d];
        int w = 0;
        for (int i = 0; i < x.length; i++) {
            float delayed = buf[w];
            buf[w] = x[i] + fb * delayed;
            x[i] = x[i] + amount * delayed;
            if (++w == d) w = 0;
        }
    }

    /** 響き（Schroeder: 並列コム 4 本 → 直列オールパス 2 本）。amount で部屋の広さと混ぜる量が増える。 */
    static void reverb(float[] x, int rate, float amount) {
        float[] combMs = {29.7f, 37.1f, 41.1f, 43.7f};
        float fb = 0.70f + 0.18f * amount;
        float[] wet = new float[x.length];
        for (float ms : combMs) {
            int d = Math.max(1, Math.round(rate * ms / 1000f));
            float[] buf = new float[d];
            int w = 0;
            float lp = 0f;
            for (int i = 0; i < x.length; i++) {
                float out = buf[w];
                lp = out * 0.8f + lp * 0.2f;          // 高い音ほど早く減衰（自然な響きに）
                buf[w] = x[i] + fb * lp;
                wet[i] += out * 0.25f;
                if (++w == d) w = 0;
            }
        }
        for (float ms : new float[]{5.0f, 1.7f}) {
            int d = Math.max(1, Math.round(rate * ms / 1000f));
            float[] buf = new float[d];
            int w = 0;
            float g = 0.7f;
            for (int i = 0; i < wet.length; i++) {
                float b = buf[w];
                float y = -g * wet[i] + b;
                buf[w] = wet[i] + g * y;
                wet[i] = y;
                if (++w == d) w = 0;
            }
        }
        float mix = 0.15f + 0.45f * amount;
        for (int i = 0; i < x.length; i++) x[i] = x[i] * (1 - mix * 0.5f) + wet[i] * mix;
    }

    // ---------------------------------------------------------------- 文の区切り

    /** 文末（。！？!?と改行）で区切る。区切り文字は前の文に残す。空の文は捨てる。 */
    public static String[] sentences(String text) {
        java.util.ArrayList<String> out = new java.util.ArrayList<>();
        StringBuilder cur = new StringBuilder();
        for (int i = 0; i < text.length(); i++) {
            char c = text.charAt(i);
            if (c == '\n') { flush(out, cur); continue; }
            cur.append(c);
            if ("。！？!?".indexOf(c) >= 0) {
                // 「！？」のような連続は 1 つの区切りにまとめる
                while (i + 1 < text.length() && "。！？!?".indexOf(text.charAt(i + 1)) >= 0) cur.append(text.charAt(++i));
                flush(out, cur);
            }
        }
        flush(out, cur);
        return out.toArray(new String[0]);
    }

    private static void flush(java.util.ArrayList<String> out, StringBuilder cur) {
        String s = cur.toString().trim();
        if (!s.isEmpty()) out.add(s);
        cur.setLength(0);
    }
}
