package com.tatsuya.jarvis;

import static org.junit.Assert.assertArrayEquals;
import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertNotNull;
import static org.junit.Assert.assertNull;
import static org.junit.Assert.assertTrue;

import org.junit.Test;

import java.nio.ByteBuffer;
import java.nio.ByteOrder;

public class VoiceFxTest {

    private static final int RATE = 24000;

    /** f Hz の正弦波（振幅 amp）。 */
    private static short[] sine(double f, int n, double amp) {
        short[] s = new short[n];
        for (int i = 0; i < n; i++) s[i] = (short) Math.round(amp * 32767 * Math.sin(2 * Math.PI * f * i / RATE));
        return s;
    }

    private static double rms(short[] s, int from, int to) {
        double sum = 0;
        for (int i = from; i < to; i++) sum += (double) s[i] * s[i];
        return Math.sqrt(sum / Math.max(1, to - from));
    }

    private static int peak(short[] s) {
        int p = 0;
        for (short v : s) p = Math.max(p, Math.abs(v));
        return p;
    }

    private static byte[] wav(short[] pcm, int channels, int bits, int rate, boolean extraChunk, boolean zeroSize) {
        int data = pcm.length * bits / 8;
        int extra = extraChunk ? 8 + 3 + 1 : 0;   // 奇数長のチャンク + 詰め物
        ByteBuffer b = ByteBuffer.allocate(44 + extra + data).order(ByteOrder.LITTLE_ENDIAN);
        b.putInt(0x46464952).putInt(36 + extra + data).putInt(0x45564157);
        b.putInt(0x20746d66).putInt(16).putShort((short) 1).putShort((short) channels).putInt(rate)
                .putInt(rate * channels * bits / 8).putShort((short) (channels * bits / 8)).putShort((short) bits);
        if (extraChunk) { b.putInt(0x5453494c).putInt(3).put((byte) 1).put((byte) 2).put((byte) 3).put((byte) 0); }
        b.putInt(0x61746164).putInt(zeroSize ? 0 : data);
        for (short v : pcm) { if (bits == 16) b.putShort(v); else b.put((byte) ((v >> 8) + 128)); }
        return b.array();
    }

    // ---------------------------------------------------------------- 値の読み込み

    @Test public void defaultsDoNothing() {
        VoiceFx.Params p = VoiceFx.Params.fromJson("");
        assertFalse(p.needsProcessing());
        assertEquals(0.85f, p.pitch, 1e-6);
        short[] in = sine(440, 2400, 0.5);
        assertArrayEquals(in, VoiceFx.process(in, RATE, p));
    }

    @Test public void clampsAndIgnoresBadValues() {
        VoiceFx.Params p = VoiceFx.Params.fromJson("{\"rate\":9,\"pitch\":0.01,\"formant\":\"x\",\"bass\":-99,\"gain\":1e9}");
        assertEquals(2.5f, p.rate, 1e-6);
        assertEquals(0.3f, p.pitch, 1e-6);
        assertEquals(1.0f, p.formant, 1e-6);   // 数でなければ既定値
        assertEquals(-12f, p.bass, 1e-6);
        assertEquals(2.0f, p.gain, 1e-6);
        assertEquals(1.0f, VoiceFx.Params.fromJson("{broken").rate, 1e-6);
    }

    @Test public void needsProcessingOnlyForEffects() {
        assertFalse(VoiceFx.Params.fromJson("{\"rate\":1.4,\"pitch\":0.6,\"pauseMs\":300,\"gain\":0.5}").needsProcessing());
        for (String k : new String[]{"\"formant\":0.9", "\"bass\":3", "\"treble\":-3", "\"metal\":0.2",
                "\"echo\":0.3", "\"reverb\":0.3", "\"gain\":1.5"}) {
            assertTrue(k, VoiceFx.Params.fromJson("{" + k + "}").needsProcessing());
        }
    }

    // ---------------------------------------------------------------- WAV

    @Test public void readsWav16MonoWithExtraChunk() {
        short[] in = sine(300, 1000, 0.3);
        VoiceFx.Pcm p = VoiceFx.readWav(wav(in, 1, 16, 22050, true, false));
        assertNotNull(p);
        assertEquals(22050, p.sampleRate);
        assertArrayEquals(in, p.samples);
    }

    @Test public void readsStreamingWavWithZeroSize() {
        short[] in = sine(300, 500, 0.3);
        assertArrayEquals(in, VoiceFx.readWav(wav(in, 1, 16, RATE, false, true)).samples);
    }

    @Test public void mixesStereoAnd8bit() {
        short[] st = {1000, 3000, -2000, -4000};
        assertArrayEquals(new short[]{2000, -3000}, VoiceFx.readWav(wav(st, 2, 16, RATE, false, false)).samples);
        VoiceFx.Pcm p8 = VoiceFx.readWav(wav(new short[]{0, 12800, -12800}, 1, 8, RATE, false, false));
        assertArrayEquals(new short[]{0, 12800, -12800}, p8.samples);
    }

    @Test public void rejectsNonPcm() {
        assertNull(VoiceFx.readWav(null));
        assertNull(VoiceFx.readWav(new byte[]{1, 2, 3}));
        byte[] w = wav(new short[]{1, 2}, 1, 16, RATE, false, false);
        w[20] = 3;                                   // IEEE float
        assertNull(VoiceFx.readWav(w));
        byte[] truncated = new byte[30];
        System.arraycopy(wav(new short[]{1}, 1, 16, RATE, false, false), 0, truncated, 0, 30);
        assertNull(VoiceFx.readWav(truncated));
    }

    @Test public void joinsWithPause() {
        VoiceFx.Pcm a = new VoiceFx.Pcm(new short[]{1, 2}, 1000), b = new VoiceFx.Pcm(new short[]{3}, 1000);
        VoiceFx.Pcm odd = new VoiceFx.Pcm(new short[]{9, 9}, 2000);
        assertArrayEquals(new short[]{1, 2, 0, 0, 0, 3}, VoiceFx.join(new VoiceFx.Pcm[]{a, null, odd, b}, 3).samples);
        assertArrayEquals(new short[]{1, 2, 3}, VoiceFx.join(new VoiceFx.Pcm[]{a, b}, 0).samples);
        assertNull(VoiceFx.join(new VoiceFx.Pcm[]{null}, 100));
    }

    // ---------------------------------------------------------------- 加工

    @Test public void bassAndTrebleShelves() {
        short[] low = sine(80, RATE, 0.2), high = sine(8000, RATE, 0.2);
        VoiceFx.Params bass = VoiceFx.Params.fromJson("{\"bass\":12}");
        VoiceFx.Params treble = VoiceFx.Params.fromJson("{\"treble\":-12}");
        double lowGain = rms(VoiceFx.process(low, RATE, bass), 2400, RATE) / rms(low, 2400, RATE);
        double highUntouched = rms(VoiceFx.process(high, RATE, bass), 2400, RATE) / rms(high, 2400, RATE);
        double highCut = rms(VoiceFx.process(high, RATE, treble), 2400, RATE) / rms(high, 2400, RATE);
        assertEquals(Math.pow(10, 12 / 20.0), lowGain, 0.6);   // 約 +12dB（≒4 倍）
        assertEquals(1.0, highUntouched, 0.1);
        assertEquals(Math.pow(10, -12 / 20.0), highCut, 0.08);
    }

    @Test public void echoRepeatsAfterDelay() {
        short[] click = new short[RATE / 10];
        click[0] = 20000;
        VoiceFx.Params p = VoiceFx.Params.fromJson("{\"echo\":0.5,\"echoMs\":100}");
        short[] out = VoiceFx.process(click, RATE, p);
        assertTrue(out.length > click.length);                 // 余韻の分だけ伸びる
        int d = RATE / 10;
        assertTrue(Math.abs(out[d]) > 5000);                     // 100ms 後に 1 回目
        assertTrue(Math.abs(out[2 * d]) > 1000 && Math.abs(out[2 * d]) < Math.abs(out[d]));  // 減衰しながら続く
        assertEquals(0, out[d / 2]);
    }

    @Test public void reverbAddsDecayingTail() {
        short[] in = sine(500, RATE / 5, 0.5);
        short[] out = VoiceFx.process(in, RATE, VoiceFx.Params.fromJson("{\"reverb\":0.8}"));
        assertTrue(out.length > in.length + RATE / 4);
        double early = rms(out, in.length, in.length + RATE / 10);
        double late = rms(out, out.length - RATE / 10, out.length);
        assertTrue(early > 200);
        assertTrue(late < early);
    }

    @Test public void metalChangesTimbreButKeepsLength() {
        short[] in = sine(200, RATE / 2, 0.4);
        short[] out = VoiceFx.process(in, RATE, VoiceFx.Params.fromJson("{\"metal\":1}"));
        assertTrue(out.length >= in.length);
        double diff = 0;
        for (int i = 0; i < in.length; i++) diff += Math.abs(out[i] - in[i]);
        assertTrue(diff / in.length > 500);
    }

    @Test public void gainNeverClips() {
        short[] loud = sine(440, 4800, 0.9);
        short[] out = VoiceFx.process(loud, RATE, VoiceFx.Params.fromJson("{\"gain\":2,\"bass\":12,\"reverb\":1}"));
        assertTrue(peak(out) <= 32768 * 0.98 + 1);
        short[] quiet = sine(440, 4800, 0.1);
        assertEquals(2.0, peak(VoiceFx.process(quiet, RATE, VoiceFx.Params.fromJson("{\"gain\":2}"))) / (double) peak(quiet), 0.02);
    }

    @Test public void formantKeepsPitchAndSpeed() {
        VoiceFx.Params p = VoiceFx.Params.fromJson("{\"rate\":1.2,\"pitch\":0.9,\"formant\":0.8}");
        // 読み上げは 1/f、再生は f 倍 → 最終的な高さ・速さは指定どおり
        assertEquals(p.pitch, VoiceFx.synthPitch(p) * p.formant, 1e-6);
        assertEquals(p.rate, VoiceFx.synthRate(p) * p.formant, 1e-6);
        assertEquals(19200, VoiceFx.playbackRate(24000, p));
    }

    @Test public void tailIsBounded() {
        VoiceFx.Params p = VoiceFx.Params.fromJson("{\"echo\":0.8,\"echoMs\":600,\"reverb\":1}");
        assertTrue(VoiceFx.tailSamples(RATE, p) <= 3 * RATE);
        assertEquals(0, VoiceFx.tailSamples(RATE, new VoiceFx.Params()));
    }

    // ---------------------------------------------------------------- 文の区切り

    @Test public void splitsSentences() {
        assertArrayEquals(new String[]{"こんにちは。", "元気ですか！？", "はい", "OK?"},
                VoiceFx.sentences("こんにちは。元気ですか！？\nはい\n\n OK?"));
        assertArrayEquals(new String[0], VoiceFx.sentences("  \n "));
        assertArrayEquals(new String[]{"読点、では切らない"}, VoiceFx.sentences("読点、では切らない"));
    }
}
