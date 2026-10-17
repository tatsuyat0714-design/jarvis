package com.tatsuya.jarvis;

import org.json.JSONArray;
import org.json.JSONObject;

import java.util.ArrayList;
import java.util.List;

/**
 * 呼びかけ（ウェイクワード）の検出。ネットにもモデルのダウンロードにも頼らず、端末の中だけで動く。
 *
 * 1. 利用者が呼びかけの言葉（例「ジャービス」）を数回録音 → 各回を MFCC の列（テンプレート）にする
 * 2. 常駐中はマイクの音を 10ms ごとに受け取り、声がしている間だけ MFCC を計算する（無音の間はほぼ何もしない）
 * 3. テンプレートとの距離を SPRING（ストリーミング部分列 DTW）で 1 フレームごとに更新する。
 *    言葉を言い終えた直後（距離が最小になった直後）に検出するので、文の途中に含まれていても、
 *    言い終わりの無音を待たずにすぐ反応する
 *
 * 声の高さや話す速さの多少の違いは DTW（時間方向の伸び縮み）と、音量に左右されない距離（コサイン）で吸収する。
 * Android に依存しない（org.json だけ）ので、PC の JUnit でそのまま試せる。
 */
public final class WakeWord {

    public static final int RATE = 16000;
    public static final int HOP = 160;          // 10ms
    static final int FRAME = 400;               // 25ms
    static final int NFFT = 512;
    static final int MELS = 26;
    static final int CEPS = 12;                 // c1〜c12（c0 = 音量は使わない）

    private WakeWord() {}

    // ---------------------------------------------------------------- MFCC

    /** 1 フレーム分の MFCC を計算する道具。窓・フィルタは作り置きして使い回す。 */
    public static final class Mfcc {
        private final float[] window = new float[FRAME];
        private final float[][] mel = new float[MELS][];
        private final int[] melStart = new int[MELS];
        private final float[][] dct = new float[CEPS][MELS];
        private final double[] re = new double[NFFT], im = new double[NFFT];
        private final double[] logMel = new double[MELS];

        public Mfcc() {
            for (int i = 0; i < FRAME; i++) window[i] = (float) (0.54 - 0.46 * Math.cos(2 * Math.PI * i / (FRAME - 1)));
            double lo = hzToMel(80), hi = hzToMel(7000);
            int[] bins = new int[MELS + 2];
            for (int i = 0; i < MELS + 2; i++) {
                double hz = melToHz(lo + (hi - lo) * i / (MELS + 1));
                bins[i] = (int) Math.floor((NFFT + 1) * hz / RATE);
            }
            for (int m = 0; m < MELS; m++) {
                int a = bins[m], b = Math.max(bins[m + 1], a + 1), c = Math.max(bins[m + 2], b + 1);
                float[] w = new float[c - a + 1];
                for (int k = a; k <= c; k++) {
                    w[k - a] = (float) (k <= b ? (k - a) / (double) (b - a) : (c - k) / (double) (c - b));
                }
                mel[m] = w;
                melStart[m] = a;
            }
            for (int k = 0; k < CEPS; k++) {
                for (int m = 0; m < MELS; m++) dct[k][m] = (float) Math.cos(Math.PI * (k + 1) * (m + 0.5) / MELS);
            }
        }

        private static double hzToMel(double hz) { return 2595 * Math.log10(1 + hz / 700); }

        private static double melToHz(double mel) { return 700 * (Math.pow(10, mel / 2595) - 1); }

        /** frame は FRAME サンプル（-1〜1）。プリエンファシス済みのものを渡す。 */
        public float[] compute(float[] frame) { return cepstrum(melPower(frame), null); }

        /** 帯域ごとの強さ（メルフィルタバンク）。 */
        public double[] melPower(float[] frame) {
            for (int i = 0; i < NFFT; i++) {
                re[i] = i < FRAME ? frame[i] * window[i] : 0;
                im[i] = 0;
            }
            fft(re, im);
            double[] out = new double[MELS];
            for (int m = 0; m < MELS; m++) {
                double e = 0;
                float[] w = mel[m];
                for (int j = 0; j < w.length; j++) {
                    int k = melStart[m] + j;
                    if (k > NFFT / 2) break;
                    e += w[j] * (re[k] * re[k] + im[k] * im[k]);
                }
                out[m] = e;
            }
            return out;
        }

        /**
         * 帯域の強さ → MFCC。noise（帯域ごとの雑音の強さ）があれば差し引く（スペクトル減算）ので、
         * 静かな部屋で登録した声と、雑音のある場所での呼びかけを比べやすくなる。
         */
        public float[] cepstrum(double[] power, double[] noise) {
            double peak = 0;
            for (int m = 0; m < MELS; m++) {
                double e = power[m];
                if (noise != null) e = Math.max(e - 2 * noise[m], 0);
                logMel[m] = e;
                peak = Math.max(peak, e);
            }
            // いちばん強い帯域より 30dB 以上弱い帯域は同じ値にそろえる（声のない帯域が雑音の大きさで揺れないように）
            double floor = peak * 1e-3 + 1e-10;
            for (int m = 0; m < MELS; m++) logMel[m] = Math.log(logMel[m] + floor);
            float[] c = new float[CEPS];
            for (int k = 0; k < CEPS; k++) {
                double s = 0;
                for (int m = 0; m < MELS; m++) s += dct[k][m] * logMel[m];
                c[k] = (float) s;
            }
            return c;
        }
    }

    /** 基数 2 の FFT（その場で書き換える）。 */
    static void fft(double[] re, double[] im) {
        int n = re.length;
        for (int i = 1, j = 0; i < n; i++) {
            int bit = n >> 1;
            for (; (j & bit) != 0; bit >>= 1) j ^= bit;
            j ^= bit;
            if (i < j) {
                double t = re[i]; re[i] = re[j]; re[j] = t;
                t = im[i]; im[i] = im[j]; im[j] = t;
            }
        }
        for (int len = 2; len <= n; len <<= 1) {
            double ang = -2 * Math.PI / len, wr = Math.cos(ang), wi = Math.sin(ang);
            for (int i = 0; i < n; i += len) {
                double cr = 1, ci = 0;
                for (int k = 0; k < len / 2; k++) {
                    int a = i + k, b = i + k + len / 2;
                    double xr = re[b] * cr - im[b] * ci, xi = re[b] * ci + im[b] * cr;
                    re[b] = re[a] - xr; im[b] = im[a] - xi;
                    re[a] += xr; im[a] += xi;
                    double t = cr * wr - ci * wi;
                    ci = cr * wi + ci * wr;
                    cr = t;
                }
            }
        }
    }

    /** 音量に左右されない距離（1 - コサイン類似度）。0〜2。 */
    static float dist(float[] a, float[] b) {
        double ab = 0, aa = 0, bb = 0;
        for (int i = 0; i < a.length; i++) { ab += a[i] * b[i]; aa += a[i] * a[i]; bb += b[i] * b[i]; }
        if (aa == 0 || bb == 0) return 1f;
        return (float) (1 - ab / Math.sqrt(aa * bb));
    }

    // ---------------------------------------------------------------- 音 → 特徴

    /** 録音全体を MFCC の列にする（フレームごとの音量も返す）。雑音は録音の静かな部分から見積もって差し引く。 */
    static List<float[]> features(short[] pcm, List<Float> energies) {
        Mfcc mfcc = new Mfcc();
        List<double[]> powers = new ArrayList<>();
        List<Float> en = new ArrayList<>();
        float[] frame = new float[FRAME];
        for (int start = 0; start + FRAME <= pcm.length; start += HOP) {
            double e = 0;
            float prev = start > 0 ? pcm[start - 1] / 32768f : 0f;
            for (int i = 0; i < FRAME; i++) {
                float x = pcm[start + i] / 32768f;
                frame[i] = x - 0.97f * prev;
                prev = x;
                e += x * x;
            }
            en.add((float) (e / FRAME));
            powers.add(mfcc.melPower(frame));
        }
        // 静かな方から 20% のフレームの平均を雑音とする
        double[] noise = null;
        if (!powers.isEmpty()) {
            Float[] sorted = en.toArray(new Float[0]);
            java.util.Arrays.sort(sorted);
            float limit = sorted[sorted.length / 5];
            noise = new double[MELS];
            int n = 0;
            for (int i = 0; i < powers.size(); i++) {
                if (en.get(i) > limit) continue;
                for (int m = 0; m < MELS; m++) noise[m] += powers.get(i)[m];
                n++;
            }
            for (int m = 0; m < MELS; m++) noise[m] /= Math.max(1, n);
        }
        List<float[]> out = new ArrayList<>();
        for (double[] p : powers) out.add(mfcc.cepstrum(p, noise));
        if (energies != null) energies.addAll(en);
        return out;
    }

    /**
     * 登録用の録音から、声のある部分だけを切り出してテンプレートにする。
     * 声が見つからない・短すぎる・長すぎるときは null。
     */
    public static float[][] template(short[] pcm) {
        List<Float> en = new ArrayList<>();
        List<float[]> f = features(pcm, en);
        if (f.size() < 10) return null;
        // 静かな方から 20% の音量を雑音の目安にし、その 5 倍（約 7dB）を超えるところを声とみなす
        float[] sorted = new float[en.size()];
        for (int i = 0; i < sorted.length; i++) sorted[i] = en.get(i);
        java.util.Arrays.sort(sorted);
        float floor = Math.max(sorted[sorted.length / 5], 1e-7f);
        float th = Math.max(floor * 5f, MIN_SPEECH_ENERGY);
        int first = -1, last = -1;
        for (int i = 0; i < en.size(); i++) if (en.get(i) > th) { if (first < 0) first = i; last = i; }
        if (first < 0) return null;
        first = Math.max(0, first - 1);
        last = Math.min(f.size() - 1, last + 1);
        int n = last - first + 1;
        if (n < MIN_FRAMES || n > MAX_FRAMES) return null;
        return f.subList(first, last + 1).toArray(new float[0][]);
    }

    static final int MIN_FRAMES = 20;           // 0.2 秒
    static final int MAX_FRAMES = 150;          // 1.5 秒
    static final float MIN_SPEECH_ENERGY = 1e-5f;   // これより小さい音は声とみなさない（約 -50dBFS）

    // ---------------------------------------------------------------- 比較

    /** 2 つの列の DTW 距離（両端を合わせる。経路の長さで割った平均）。 */
    public static float dtw(float[][] a, float[][] b) {
        int n = a.length, m = b.length;
        float[][] d = new float[n + 1][m + 1];
        int[][] len = new int[n + 1][m + 1];
        for (float[] row : d) java.util.Arrays.fill(row, Float.POSITIVE_INFINITY);
        d[0][0] = 0;
        for (int i = 1; i <= n; i++) {
            for (int j = 1; j <= m; j++) {
                float c = dist(a[i - 1], b[j - 1]);
                float best = d[i - 1][j - 1];
                int bl = len[i - 1][j - 1];
                if (d[i - 1][j] < best) { best = d[i - 1][j]; bl = len[i - 1][j]; }
                if (d[i][j - 1] < best) { best = d[i][j - 1]; bl = len[i][j - 1]; }
                d[i][j] = best + c;
                len[i][j] = bl + 1;
            }
        }
        return d[n][m] / len[n][m];
    }

    /**
     * 登録したテンプレート同士の近さから、検出のしきい値を決める。
     * sensitivity（0〜1）が大きいほど反応しやすい（そのぶん聞き間違いも増える）。
     */
    public static final float DEFAULT_SENSITIVITY = 0.6f;

    public static float threshold(List<float[][]> templates, float sensitivity) {
        double sum = 0;
        int n = 0;
        for (int i = 0; i < templates.size(); i++) {
            for (int j = i + 1; j < templates.size(); j++) { sum += dtw(templates.get(i), templates.get(j)); n++; }
        }
        double base = n == 0 ? 0.05 : sum / n;
        double s = Math.max(0, Math.min(1, sensitivity));
        // 登録した声どうしのばらつき（静かな部屋で録るとごく小さい）だけで決めると、少しの雑音や言い方の違いで
        // 反応しなくなるので、余裕を足す。合成音での試験では、既定（0.6）で同じ言葉の 97% を検出し、
        // 別の言葉 400 個には 1 度も反応しなかった（WakeWordTest）
        return (float) (base * 2 + 0.02 + 0.13 * s);
    }

    // ---------------------------------------------------------------- 保存

    public static String toJson(List<float[][]> templates) {
        JSONArray all = new JSONArray();
        try {
            for (float[][] t : templates) {
                JSONArray frames = new JSONArray();
                for (float[] f : t) {
                    JSONArray v = new JSONArray();
                    for (float x : f) v.put(Math.round(x * 1000) / 1000.0);
                    frames.put(v);
                }
                all.put(frames);
            }
            return new JSONObject().put("v", 1).put("t", all).toString();
        } catch (Exception e) {
            return "";
        }
    }

    public static List<float[][]> fromJson(String json) {
        List<float[][]> out = new ArrayList<>();
        if (json == null || json.isEmpty()) return out;
        try {
            JSONArray all = new JSONObject(json).getJSONArray("t");
            for (int i = 0; i < all.length(); i++) {
                JSONArray frames = all.getJSONArray(i);
                float[][] t = new float[frames.length()][];
                for (int j = 0; j < frames.length(); j++) {
                    JSONArray v = frames.getJSONArray(j);
                    if (v.length() != CEPS) return new ArrayList<>();
                    t[j] = new float[CEPS];
                    for (int k = 0; k < CEPS; k++) t[j][k] = (float) v.getDouble(k);
                }
                if (t.length >= MIN_FRAMES) out.add(t);
            }
        } catch (Exception e) {
            return new ArrayList<>();
        }
        return out;
    }

    // ---------------------------------------------------------------- 常駐中の検出

    /**
     * マイクの音を HOP（10ms）ずつ受け取り、呼びかけを見つけたら true を返す。スレッドは 1 本から使う。
     * 声がしていない間は音量を測るだけで、MFCC も DTW も計算しない。
     */
    public static final class Detector {
        private final float[][][] templates;
        private final float threshold;
        private final Mfcc mfcc = new Mfcc();
        private final float[] ring = new float[FRAME];
        private final float[] frame = new float[FRAME];
        private int filled = 0;
        private float prevSample = 0f;

        // 声の有無（雑音の大きさに合わせて自動で基準を動かす）
        private float noise = -1f;
        private int hangover = 0;
        private int silent = 0;
        private double[] noiseMel;        // 帯域ごとの雑音（声のない間に、ときどき測り直す）

        // SPRING の状態（テンプレートごと）
        private final float[][] cost, nextCost;
        private final int[][] steps, nextSteps, start, nextStart, stays, nextStays;
        private long t = 0;

        // 「いちばん近づいた瞬間」を待つための状態
        private float best = Float.POSITIVE_INFINITY;
        private int sinceBest = 0;
        private int refractory = 0;
        public float lastScore = Float.POSITIVE_INFINITY;   // 調整用（直近の最小距離 / しきい値）

        public Detector(List<float[][]> templates, float threshold) {
            this.templates = templates.toArray(new float[0][][]);
            this.threshold = threshold;
            int k = this.templates.length;
            cost = new float[k][];
            nextCost = new float[k][];
            steps = new int[k][];
            nextSteps = new int[k][];
            start = new int[k][];
            nextStart = new int[k][];
            stays = new int[k][];
            nextStays = new int[k][];
            for (int i = 0; i < k; i++) {
                int m = this.templates[i].length + 1;
                cost[i] = new float[m]; nextCost[i] = new float[m];
                steps[i] = new int[m]; nextSteps[i] = new int[m];
                start[i] = new int[m]; nextStart[i] = new int[m];
                stays[i] = new int[m]; nextStays[i] = new int[m];
            }
            resetSearch();
        }

        public boolean ready() { return templates.length > 0; }

        private void resetSearch() {
            for (int i = 0; i < cost.length; i++) {
                java.util.Arrays.fill(cost[i], Float.POSITIVE_INFINITY);
                java.util.Arrays.fill(steps[i], 0);
            }
            best = Float.POSITIVE_INFINITY;
            sinceBest = 0;
        }

        /** hop は HOP サンプル。呼びかけを見つけたら true。 */
        public boolean push(short[] hop) {
            // 25ms の窓を 10ms ずつずらす
            System.arraycopy(ring, HOP, ring, 0, FRAME - HOP);
            for (int i = 0; i < HOP; i++) ring[FRAME - HOP + i] = hop[i] / 32768f;
            if (filled < FRAME) { filled += HOP; if (filled < FRAME) return false; }
            t++;
            if (refractory > 0) refractory--;

            double e = 0;
            for (int i = FRAME - HOP; i < FRAME; i++) e += ring[i] * ring[i];
            float energy = (float) (e / HOP);
            if (noise < 0) noise = Math.max(energy, 1e-8f);
            boolean speech = energy > noise * 3f && energy > MIN_SPEECH_ENERGY;
            // 雑音の目安: 声のない間は素早く、声の間もごくゆっくり追う（大きな雑音が続いても固まらない）
            noise = speech ? noise * 0.999f + energy * 0.001f : noise * 0.95f + energy * 0.05f;
            noise = Math.max(noise, 1e-8f);

            float prev = prevSample;
            prevSample = ring[HOP - 1];   // 次の窓の先頭の 1 つ前のサンプル（プリエンファシス用）
            if (!speech && hangover == 0 && (noiseMel == null || t % 5 == 0)) {
                // 声のない間は 50ms に 1 回だけ帯域ごとの雑音を測る（FFT 1 回分。電池への影響はごくわずか）
                double[] p = mfcc.melPower(emphasize(prev));
                if (noiseMel == null) noiseMel = p;
                else for (int m = 0; m < MELS; m++) noiseMel[m] = noiseMel[m] * 0.8 + p[m] * 0.2;
            }
            if (speech) { hangover = 40; silent = 0; }
            else if (hangover > 0) hangover--;
            else {
                // 声が途切れた: 近づいていた候補があればここで確定する（無音で経路が少しずつ良くなり続け、
                // 「それ以上近づかない」を待てないことがあるため）
                if (silent++ == 0 && refractory == 0 && best < threshold) return fire();
                if (silent == 50) resetSearch();   // 0.5 秒の無音で探し直し
                return false;
            }

            float[] x = mfcc.cepstrum(mfcc.melPower(emphasize(prev)), noiseMel);
            return step(x);
        }

        private float[] emphasize(float prev) {
            for (int i = 0; i < FRAME; i++) {
                frame[i] = ring[i] - 0.97f * prev;
                prev = ring[i];
            }
            return frame;
        }

        /**
         * 特徴 1 フレーム分を進める（テスト用に直接呼べる）。
         * 入力 1 フレームごとにテンプレートの位置は 0〜2 進む。同じ位置に留まるのは 2 回まで（= 入力は 0.5〜3 倍に
         * 伸び縮みできる）。留まりを無制限にすると、言葉の後の無音がテンプレート末尾の 1 フレームに吸い込まれて
         * 平均距離を薄め、別の言葉でも反応してしまう。
         * 経路の長さ = 入力のフレーム数なので、平均距離どうしを公平に比べられる。
         */
        boolean step(float[] x) {
            float frameBest = Float.POSITIVE_INFINITY;
            for (int k = 0; k < templates.length; k++) {
                float[][] y = templates[k];
                int m = y.length;
                float[] D = cost[k], ND = nextCost[k];
                int[] L = steps[k], NL = nextSteps[k], S = start[k], NS = nextStart[k], R = stays[k], NR = nextStays[k];
                ND[0] = Float.POSITIVE_INFINITY;
                // 声の出だしは、音量で「声あり」と判定されるまでの数フレームを取りこぼすので、
                // テンプレートの最初の 15% のどこからでも始められるようにする
                int head = Math.max(1, m * 15 / 100);
                for (int i = 1; i <= m; i++) {
                    float c = dist(x, y[i - 1]);
                    // 候補: ここから始める（出だしの範囲だけ）/ 前のフレームで i-1・i-2・i にいた経路
                    float bc = Float.POSITIVE_INFINITY, bavg = Float.POSITIVE_INFINITY;
                    int bl = 0, bs = (int) t, br = 0;
                    if (i <= head) { bc = 0; bl = 0; bavg = c; }
                    for (int j = i; j >= Math.max(1, i - 2); j--) {
                        if (L[j] == 0 || D[j] == Float.POSITIVE_INFINITY) continue;
                        if (j == i && R[j] >= 2) continue;   // 同じ位置に留まりすぎない
                        float avg = (D[j] + c) / (L[j] + 1);
                        if (avg < bavg) { bavg = avg; bc = D[j]; bl = L[j]; bs = S[j]; br = j == i ? R[j] + 1 : 0; }
                    }
                    ND[i] = bc + c;
                    NL[i] = bl + 1;
                    NS[i] = bs;
                    NR[i] = br;
                }
                cost[k] = ND; nextCost[k] = D;
                steps[k] = NL; nextSteps[k] = L;
                start[k] = NS; nextStart[k] = S;
                stays[k] = NR; nextStays[k] = R;
                // 言い終わり（テンプレートの最後）に着いた経路の平均距離。長さが極端なものは除く
                int dur = NL[m];
                if (dur >= m / 2 && dur <= m * 2) frameBest = Math.min(frameBest, ND[m] / dur);
            }
            if (refractory > 0) return false;
            lastScore = Math.min(lastScore, frameBest / threshold);
            if (frameBest < best) { best = frameBest; sinceBest = 0; }
            else sinceBest++;
            // しきい値より近づき、その後 8 フレーム（80ms）それ以上近づかなければ「言い終えた」とみなす
            if (best < threshold && sinceBest >= 8) return fire();
            if (sinceBest > 60) { best = frameBest; sinceBest = 0; }   // 古い最小値は忘れる
            return false;
        }

        private boolean fire() {
            resetSearch();
            refractory = 150;   // 1.5 秒は続けて反応しない
            return true;
        }
    }
}
