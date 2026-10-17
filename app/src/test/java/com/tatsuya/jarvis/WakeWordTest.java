package com.tatsuya.jarvis;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertNotNull;
import static org.junit.Assert.assertNull;
import static org.junit.Assert.assertTrue;

import org.junit.Test;

import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;
import java.util.Random;

/**
 * 実際の声の代わりに、声に似た合成音（声帯の振動 = 倍音の列 に、口の形 = フォルマント を掛けたもの）で試す。
 * 「言葉」は母音のような区間の並び。速さ・高さ・音量・雑音を変えた別の発話でも検出でき、
 * 別の言葉や雑音では反応しないことを確かめる。
 */
public class WakeWordTest {

    private static final int R = WakeWord.RATE;

    /** 区間ごとの (第 1 フォルマント, 第 2 フォルマント) Hz。 */
    static final double[][] JARVIS = {{300, 2200}, {800, 1200}, {800, 1200}, {280, 2300}, {350, 1400}};
    static final double[][] OTHER = {{500, 900}, {350, 1900}, {650, 1000}, {300, 800}};
    static final double[][] OTHER2 = {{700, 1700}, {450, 2500}, {600, 1300}};

    /** 合成の発話。tempo は 1 が基準（大きいほど速い）、f0 は声の高さ、amp は音量。 */
    static short[] word(double[][] formants, double tempo, double f0, double amp, long seed) {
        int segLen = (int) (R * 0.12 / tempo);
        int n = segLen * formants.length;
        short[] out = new short[n];
        Random rnd = new Random(seed);
        double phase = 0;
        for (int i = 0; i < n; i++) {
            // 区間の境目はなめらかにつなぐ
            double pos = (double) i / segLen;
            int a = Math.min((int) pos, formants.length - 1), b = Math.min(a + 1, formants.length - 1);
            double fr = Math.min(1, Math.max(0, (pos - a - 0.7) / 0.3));
            double F1 = formants[a][0] * (1 - fr) + formants[b][0] * fr;
            double F2 = formants[a][1] * (1 - fr) + formants[b][1] * fr;
            double f = f0 * (1 + 0.03 * Math.sin(2 * Math.PI * 4 * i / R));   // 少し揺れる高さ
            phase += 2 * Math.PI * f / R;
            double v = 0;
            for (int h = 1; h * f < 7000; h++) {
                double hf = h * f;
                double g = res(hf, F1, 90) + 0.6 * res(hf, F2, 120) + 0.15 * res(hf, 2800, 200);
                v += g / h * Math.sin(h * phase);
            }
            double env = Math.min(1, Math.min(i, n - i) / (R * 0.02));
            out[i] = (short) Math.max(-32768, Math.min(32767, amp * 32767 * env * v * 0.25 + rnd.nextGaussian() * 30));
        }
        return out;
    }

    private static double res(double f, double center, double bw) {
        double d = (f - center) / bw;
        return 1 / (1 + d * d);
    }

    static short[] silence(double sec, double noise, long seed) {
        Random rnd = new Random(seed);
        short[] s = new short[(int) (R * sec)];
        for (int i = 0; i < s.length; i++) s[i] = (short) (rnd.nextGaussian() * noise);
        return s;
    }

    static short[] concat(short[]... parts) {
        int n = 0;
        for (short[] p : parts) n += p.length;
        short[] out = new short[n];
        int at = 0;
        for (short[] p : parts) { System.arraycopy(p, 0, out, at, p.length); at += p.length; }
        return out;
    }

    static short[] addNoise(short[] s, double noise, long seed) {
        Random rnd = new Random(seed);
        short[] out = new short[s.length];
        for (int i = 0; i < s.length; i++) out[i] = (short) Math.max(-32768, Math.min(32767, s[i] + rnd.nextGaussian() * noise));
        return out;
    }

    /** 登録: 3 回の発話（少しずつ違う）を前後に無音を付けて録音したもの。 */
    static List<float[][]> enroll() {
        List<float[][]> t = new ArrayList<>();
        double[][] v = {{1.0, 120, 0.5}, {0.92, 125, 0.4}, {1.08, 116, 0.6}};
        for (int i = 0; i < v.length; i++) {
            short[] rec = concat(silence(0.4, 40, i), word(JARVIS, v[i][0], v[i][1], v[i][2], i), silence(0.5, 40, 10 + i));
            float[][] tpl = WakeWord.template(rec);
            assertNotNull("enroll " + i, tpl);
            t.add(tpl);
        }
        return t;
    }

    /** ストリームを流し、検出したサンプル位置の一覧を返す。 */
    static List<Integer> run(WakeWord.Detector d, short[] stream) {
        List<Integer> hits = new ArrayList<>();
        short[] hop = new short[WakeWord.HOP];
        for (int at = 0; at + WakeWord.HOP <= stream.length; at += WakeWord.HOP) {
            System.arraycopy(stream, at, hop, 0, WakeWord.HOP);
            if (d.push(hop)) hits.add(at + WakeWord.HOP);
        }
        return hits;
    }

    // ---------------------------------------------------------------- 部品

    @Test public void fftMatchesDft() {
        int n = 16;
        double[] re = new double[n], im = new double[n], x = new double[n];
        Random r = new Random(1);
        for (int i = 0; i < n; i++) { x[i] = r.nextGaussian(); re[i] = x[i]; }
        WakeWord.fft(re, im);
        for (int k = 0; k < n; k++) {
            double sr = 0, si = 0;
            for (int i = 0; i < n; i++) { sr += x[i] * Math.cos(2 * Math.PI * k * i / n); si -= x[i] * Math.sin(2 * Math.PI * k * i / n); }
            assertEquals(sr, re[k], 1e-9);
            assertEquals(si, im[k], 1e-9);
        }
    }

    @Test public void distanceIgnoresLoudness() {
        float[] a = {1, 2, 3}, b = {2, 4, 6}, c = {-1, -2, -3};
        assertEquals(0f, WakeWord.dist(a, b), 1e-6);
        assertEquals(2f, WakeWord.dist(a, c), 1e-6);
    }

    @Test public void templateTrimsSilenceAndRejectsBadRecordings() {
        short[] w = word(JARVIS, 1, 120, 0.5, 1);
        float[][] t = WakeWord.template(concat(silence(0.5, 40, 1), w, silence(0.5, 40, 2)));
        assertNotNull(t);
        int expect = w.length / WakeWord.HOP;
        assertTrue("frames " + t.length, Math.abs(t.length - expect) <= 10);
        assertNull(WakeWord.template(silence(2, 40, 3)));                                  // 声がない
        assertNull(WakeWord.template(concat(silence(0.3, 40, 4), new short[160], silence(0.3, 40, 5))));
        short[] longWord = word(new double[][]{{300, 2200}, {800, 1200}, {500, 900}, {350, 1900},
                {650, 1000}, {300, 800}, {700, 1700}, {450, 2500}, {600, 1300}, {300, 2200}, {800, 1200},
                {500, 900}, {350, 1900}, {650, 1000}}, 1, 120, 0.5, 6);
        assertNull(WakeWord.template(concat(silence(0.3, 40, 7), longWord, silence(0.3, 40, 8))));  // 長すぎる
    }

    @Test public void sameWordIsCloserThanOtherWords() {
        List<float[][]> t = enroll();
        float same = WakeWord.dtw(t.get(0), t.get(1));
        float other = WakeWord.dtw(t.get(0), WakeWord.template(concat(silence(0.3, 40, 1), word(OTHER, 1, 120, 0.5, 9), silence(0.3, 40, 2))));
        assertTrue(same + " vs " + other, same * 2 < other);
    }

    @Test public void jsonRoundTrip() {
        List<float[][]> t = enroll();
        List<float[][]> back = WakeWord.fromJson(WakeWord.toJson(t));
        assertEquals(t.size(), back.size());
        assertEquals(t.get(1).length, back.get(1).length);
        assertEquals(t.get(1)[5][3], back.get(1)[5][3], 1e-3);
        assertTrue(WakeWord.fromJson("{bad").isEmpty());
        assertTrue(WakeWord.fromJson("").isEmpty());
        assertTrue(WakeWord.fromJson("{\"t\":[[[1,2]]]}").isEmpty());   // 次元が違う
    }

    @Test public void sensitivityRaisesThreshold() {
        List<float[][]> t = enroll();
        assertTrue(WakeWord.threshold(t, 1) > WakeWord.threshold(t, 0));
    }

    // ---------------------------------------------------------------- 検出

    @Test public void detectsRightAfterTheWordEnds() {
        List<float[][]> t = enroll();
        WakeWord.Detector d = new WakeWord.Detector(t, WakeWord.threshold(t, WakeWord.DEFAULT_SENSITIVITY));
        // 別の言葉 → 間 → 呼びかけ（速さ・高さ・音量を変えたもの）→ 続けて用件の言葉
        short[] wake = word(JARVIS, 0.95, 130, 0.35, 42);
        short[] before = concat(silence(1.0, 60, 1), word(OTHER, 1, 120, 0.5, 2), silence(0.6, 60, 3));
        short[] stream = addNoise(concat(before, wake, word(OTHER2, 1, 120, 0.5, 4), silence(1.0, 60, 5)), 80, 6);
        List<Integer> hits = run(d, stream);
        assertEquals("hits " + hits, 1, hits.size());
        int wakeEnd = before.length + wake.length;
        int delayMs = (hits.get(0) - wakeEnd) * 1000 / R;
        assertTrue("delay " + delayMs + "ms", delayMs > -150 && delayMs < 250);   // 言い終わりの直後
    }

    @Test public void ignoresOtherWordsAndNoise() {
        List<float[][]> t = enroll();
        WakeWord.Detector d = new WakeWord.Detector(t, WakeWord.threshold(t, WakeWord.DEFAULT_SENSITIVITY));
        List<short[]> parts = new ArrayList<>();
        Random r = new Random(3);
        for (int i = 0; i < 12; i++) {
            parts.add(silence(0.3 + r.nextDouble() * 0.5, 60, i));
            parts.add(word(i % 2 == 0 ? OTHER : OTHER2, 0.85 + r.nextDouble() * 0.3, 100 + r.nextInt(80), 0.3 + r.nextDouble() * 0.4, 100 + i));
        }
        parts.add(silence(1, 3000, 99));   // 大きな雑音（ザー）
        assertEquals(Arrays.asList(), run(d, addNoise(concat(parts.toArray(new short[0][])), 80, 7)));
    }

    @Test public void detectsEachTimeButNotTwiceForOneCall() {
        List<float[][]> t = enroll();
        WakeWord.Detector d = new WakeWord.Detector(t, WakeWord.threshold(t, WakeWord.DEFAULT_SENSITIVITY));
        short[] stream = concat(silence(0.5, 60, 1), word(JARVIS, 1.05, 110, 0.5, 7), silence(2.0, 60, 2),
                word(JARVIS, 0.9, 140, 0.25, 8), silence(1.0, 60, 3));
        assertEquals(2, run(d, addNoise(stream, 60, 9)).size());
    }

    /** いろいろな言い方・雑音で、検出率と誤反応を数える（既定の感度）。 */
    @Test public void detectionRateAndFalseAlarms() {
        List<float[][]> t = enroll();
        float th = WakeWord.threshold(t, WakeWord.DEFAULT_SENSITIVITY);
        Random r = new Random(11);
        int det = 0, n = 60;
        for (int i = 0; i < n; i++) {
            short[] w = word(JARVIS, 0.85 + r.nextDouble() * 0.3, 105 + r.nextInt(40), 0.2 + r.nextDouble() * 0.4, 1000 + i);
            short[] s = addNoise(concat(silence(0.5, 60, i), w, silence(0.8, 60, i + 1)), 40 + r.nextDouble() * 110, i);
            if (!run(new WakeWord.Detector(t, th), s).isEmpty()) det++;
        }
        int fp = 0, m = 120;
        for (int i = 0; i < m; i++) {
            int segs = 2 + r.nextInt(5);
            double[][] f = new double[segs][];
            for (int k = 0; k < segs; k++) f[k] = new double[]{250 + r.nextInt(600), 800 + r.nextInt(1800)};
            short[] w = word(f, 0.85 + r.nextDouble() * 0.3, 100 + r.nextInt(80), 0.2 + r.nextDouble() * 0.5, 5000 + i);
            fp += run(new WakeWord.Detector(t, th), addNoise(concat(silence(0.3, 60, i), w, silence(0.6, 60, i + 1)), 40 + r.nextDouble() * 110, i)).size();
        }
        assertTrue("detected " + det + "/" + n, det >= n * 0.9);
        assertTrue("false alarms " + fp + "/" + m, fp <= 2);
    }

    @Test public void lowerSensitivityIsStricter() {
        List<float[][]> t = enroll();
        short[] far = addNoise(concat(silence(0.5, 60, 1), word(JARVIS, 0.86, 144, 0.2, 77), silence(0.8, 60, 2)), 140, 3);
        int strict = run(new WakeWord.Detector(t, WakeWord.threshold(t, 0f)), far).size();
        int loose = run(new WakeWord.Detector(t, WakeWord.threshold(t, 1f)), far).size();
        assertTrue(strict <= loose);
        assertEquals(1, loose);
    }

    @Test public void quietRoomCostsAlmostNothing() {
        List<float[][]> t = enroll();
        WakeWord.Detector d = new WakeWord.Detector(t, WakeWord.threshold(t, WakeWord.DEFAULT_SENSITIVITY));
        short[] quiet = silence(60, 50, 1);
        long t0 = System.nanoTime();
        assertTrue(run(d, quiet).isEmpty());
        long ms = (System.nanoTime() - t0) / 1_000_000;
        assertTrue("60s of silence took " + ms + "ms", ms < 300);   // 声がない間は MFCC を計算しない
    }
}
