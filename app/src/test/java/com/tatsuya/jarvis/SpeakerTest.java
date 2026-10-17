package com.tatsuya.jarvis;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;

import org.junit.Before;
import org.junit.Test;

import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

/** 読み上げの進行（文が届くたびに話す・加工・失敗時の切り替え・始まり/終わりの通知）を偽のエンジンで確かめる。 */
public class SpeakerTest {

    /** 呼ばれたことを記録する偽の読み上げエンジン。 */
    static class FakeEngine implements Speaker.Engine {
        final List<String> log = new ArrayList<>();
        final Map<String, byte[]> audio = new HashMap<>();
        boolean synthOk = true;
        byte[] nextAudio = wav(new short[]{100, 200, 300}, 22050);

        @Override public void setVoice(float rate, float pitch) { log.add(String.format("voice %.2f %.2f", rate, pitch)); }
        @Override public void speak(String text, float volume, String id) { log.add("speak " + text + " " + id); }
        @Override public void silence(long ms, String id) { log.add("silence " + ms); }
        @Override public boolean synthesize(String text, String id) {
            log.add("synth " + text + " " + id);
            if (synthOk) audio.put(id, nextAudio);
            return synthOk;
        }
        @Override public byte[] takeAudio(String id) { return audio.remove(id); }
        @Override public void stop() { log.add("stop"); }

        List<String> only(String prefix) {
            List<String> out = new ArrayList<>();
            for (String s : log) if (s.startsWith(prefix)) out.add(s);
            return out;
        }
    }

    static class FakeOutput implements Speaker.Output {
        final int rate;
        final List<short[]> chunks = new ArrayList<>();
        boolean closed, cancelled;
        Runnable onStarted, onDone;

        FakeOutput(int rate) { this.rate = rate; }
        @Override public int sampleRate() { return rate; }
        @Override public void add(short[] s) { chunks.add(s); if (chunks.size() == 1) onStarted.run(); }
        @Override public void close() { closed = true; }
        @Override public void cancel() { cancelled = true; }
    }

    static byte[] wav(short[] pcm, int rate) {
        ByteBuffer b = ByteBuffer.allocate(44 + pcm.length * 2).order(ByteOrder.LITTLE_ENDIAN);
        b.putInt(0x46464952).putInt(36 + pcm.length * 2).putInt(0x45564157);
        b.putInt(0x20746d66).putInt(16).putShort((short) 1).putShort((short) 1).putInt(rate).putInt(rate * 2)
                .putShort((short) 2).putShort((short) 16);
        b.putInt(0x61746164).putInt(pcm.length * 2);
        for (short v : pcm) b.putShort(v);
        return b.array();
    }

    FakeEngine engine;
    List<FakeOutput> outputs;
    ArrayDeque<Runnable> mainQ, workQ;
    List<String> events;
    Speaker sp;

    @Before public void setUp() {
        engine = new FakeEngine();
        outputs = new ArrayList<>();
        mainQ = new ArrayDeque<>();
        workQ = new ArrayDeque<>();
        events = new ArrayList<>();
        sp = new Speaker(engine, (rate, playbackRate, started, done) -> {
            FakeOutput o = new FakeOutput(rate);
            o.onStarted = started;
            o.onDone = done;
            outputs.add(o);
            return o;
        }, mainQ::add, workQ::add, new Speaker.Listener() {
            @Override public void onStart() { events.add("start"); }
            @Override public void onEnd() { events.add("end"); }
        });
    }

    /** メインスレッドと加工スレッドに積まれた処理を、なくなるまで実行する。 */
    void pump() {
        while (!mainQ.isEmpty() || !workQ.isEmpty()) {
            if (!workQ.isEmpty()) workQ.poll().run();
            if (!mainQ.isEmpty()) mainQ.poll().run();
        }
    }

    void useFx() {
        sp.setParams(VoiceFx.Params.fromJson("{\"rate\":1.0,\"pitch\":1.0,\"reverb\":0.3}"));
    }

    // ---------------------------------------------------------------- そのまま話す

    @Test public void speaksEachSentenceAsItArrives() {
        sp.begin();
        sp.append("こんにちは。");
        assertEquals(Arrays.asList("speak こんにちは。 d1_0"), engine.only("speak"));   // 待たずに話し始める
        sp.append("今日は晴れです。");
        sp.onUtteranceStart("d1_0");
        assertEquals(Arrays.asList("start"), events);
        sp.onUtteranceDone("d1_0", true);
        sp.onUtteranceDone("d1_1", true);
        assertEquals(Arrays.asList("start"), events);   // finish 前は、言い終えても終わりにしない
        sp.finish();
        assertEquals(Arrays.asList("start", "end"), events);
    }

    @Test public void endsAfterLastSentenceWhenFinishedEarly() {
        sp.begin();
        sp.append("一。");
        sp.append("二。");
        sp.finish();
        sp.onUtteranceStart("d1_0");
        sp.onUtteranceDone("d1_0", true);
        assertEquals(Arrays.asList("start"), events);
        sp.onUtteranceDone("d1_1", false);   // 失敗でも話し終わりとして数える
        assertEquals(Arrays.asList("start", "end"), events);
        sp.onUtteranceDone("d1_1", true);    // 余分な通知では 2 回終わらない
        assertEquals(2, events.size());
    }

    @Test public void emptyReplyStillStartsAndEnds() {
        sp.begin();
        sp.append("  ");
        sp.finish();
        assertEquals(Arrays.asList("start", "end"), events);
    }

    @Test public void newReplyStopsOldAndIgnoresItsCallbacks() {
        sp.begin();
        sp.append("古い。");
        sp.begin();
        assertTrue(engine.log.contains("stop"));
        sp.append("新しい。");
        sp.finish();
        sp.onUtteranceDone("d1_0", true);   // 古い返事の通知
        assertEquals(Arrays.asList(), events);
        sp.onUtteranceDone("d2_0", true);
        assertEquals(Arrays.asList("start", "end"), events);
    }

    @Test public void pauseBetweenSentences() {
        sp.setParams(VoiceFx.Params.fromJson("{\"pauseMs\":300}"));
        sp.speak("一つ目。二つ目。");
        List<String> heard = new ArrayList<>();
        for (String e : engine.log) if (e.startsWith("speak") || e.startsWith("silence")) heard.add(e);
        assertEquals(Arrays.asList("speak 一つ目。 d1_0", "silence 300", "speak 二つ目。 d1_1"), heard);
    }

    @Test public void speakKeepsWholeTextWithoutPause() {
        sp.speak("一つ目。二つ目。");
        assertEquals(Arrays.asList("speak 一つ目。二つ目。 d1_0"), engine.only("speak"));
    }

    // ---------------------------------------------------------------- 加工して話す

    @Test public void processesOneSentenceWhileNextIsSynthesized() {
        useFx();
        sp.begin();
        sp.append("一。");
        sp.append("二。");
        assertEquals(Arrays.asList("synth 一。 f1_0"), engine.only("synth"));   // 書き出しは 1 つずつ
        sp.onUtteranceDone("f1_0", true);
        assertEquals(2, engine.only("synth").size());                           // すぐ次を書き出す
        pump();
        assertEquals(1, outputs.size());
        assertEquals(Arrays.asList("start"), events);                           // 1 文目が鳴り始めた
        sp.onUtteranceDone("f1_1", true);
        sp.finish();
        pump();
        FakeOutput o = outputs.get(0);
        assertTrue(o.closed);
        assertEquals(2, o.chunks.size());
        assertEquals(Arrays.asList("start"), events);                           // 鳴り終わるまで終わらない
        o.onDone.run();
        pump();
        assertEquals(Arrays.asList("start", "end"), events);
    }

    @Test public void synthesisUsesFormantCompensation() {
        sp.setParams(VoiceFx.Params.fromJson("{\"rate\":1.2,\"pitch\":0.9,\"formant\":0.8}"));
        sp.begin();
        sp.append("太い声。");
        // 1/f の高さ・速さで書き出す
        assertTrue(engine.log.contains("voice 1.50 1.13"));
    }

    @Test public void unreadableAudioFallsBackAfterPlayedPart() {
        useFx();
        sp.begin();
        sp.append("一。");
        sp.onUtteranceDone("f1_0", true);
        pump();                                   // 一 は加工して流した
        engine.nextAudio = new byte[]{1, 2, 3};   // 二 からは読めない
        sp.append("二。");
        sp.onUtteranceDone("f1_1", true);
        sp.append("三。");                        // 三 は書き出し中に失敗が分かる
        pump();
        FakeOutput o = outputs.get(0);
        assertTrue(o.closed);
        assertTrue(engine.only("speak").isEmpty());   // 鳴っている間はそのまま話さない（重ならない）
        sp.append("四。");                             // 失敗後に届いた文も後ろに並ぶ
        sp.finish();
        o.onDone.run();
        pump();
        assertEquals(Arrays.asList("speak 二。 d1_0", "speak 三。 d1_1", "speak 四。 d1_2"), engine.only("speak"));
        assertEquals(Arrays.asList("start"), events);
        sp.onUtteranceDone("d1_0", true);
        sp.onUtteranceDone("d1_1", true);
        sp.onUtteranceDone("d1_2", true);
        assertEquals(Arrays.asList("start", "end"), events);
    }

    @Test public void synthesisRefusedSpeaksDirectly() {
        useFx();
        engine.synthOk = false;
        sp.begin();
        sp.append("一。");
        sp.append("二。");
        sp.finish();
        assertEquals(Arrays.asList("speak 一。 d1_0", "speak 二。 d1_1"), engine.only("speak"));
        assertTrue(outputs.isEmpty());
        sp.onUtteranceDone("d1_0", true);
        sp.onUtteranceDone("d1_1", true);
        assertEquals(Arrays.asList("start", "end"), events);
    }

    @Test public void stopCancelsOutputAndDropsLateResults() {
        useFx();
        sp.begin();
        sp.append("一。");
        sp.onUtteranceDone("f1_0", true);
        pump();
        sp.append("二。");
        sp.onUtteranceDone("f1_1", true);
        sp.stop();                 // 加工中に止めた
        pump();
        assertTrue(outputs.get(0).cancelled);
        assertEquals(1, outputs.get(0).chunks.size());   // 止めた後の加工結果は流さない
        assertFalse(sp.isActive());
    }

    @Test public void fxSessionKeepsItsSettingsWhenPreviewChanges() {
        useFx();
        sp.begin();
        sp.append("一。");
        sp.setParams(new VoiceFx.Params());   // 話している途中で設定画面の値が変わった
        sp.onUtteranceDone("f1_0", true);
        pump();
        assertEquals(1, outputs.size());       // 始めた返事は最後まで加工したまま
    }
}
