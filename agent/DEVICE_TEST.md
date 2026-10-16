# 実機での動作確認手順

エージェント（`agent/`）を実際の Android 端末で確認するための手順。
上から順に進め、各ステップの「確認すること」を満たしたら次へ進む。
うまくいかなかったときは、そのステップの「うまくいかないとき」を見て、最後の「結果の報告」に記録する。

- 所要時間: 全部で 1〜2 時間（LLM のモデルのダウンロード時間を除く）
- 端末の条件: Android 11 以上（ワイヤレスデバッグを使うため）。Wi-Fi に接続していること
- **このテストで消えるもの**: アプリのキャッシュのみ（ステップ 9）。アンインストールや APK のインストールは、確認で `N` と答えれば実行されない。ファイル移動は専用のテスト用フォルダの中だけで行う

---

## ステップ 0. 準備

1. **Termux を F-Droid または GitHub Releases から入れる**（Google Play 版は古く、パッケージが入らない）
2. Termux:API アプリも同じ入手元から入れる（通知に使う）
3. Termux で:

   ```sh
   pkg update
   pkg install python android-tools git termux-api aapt
   termux-setup-storage          # 「ファイルへのアクセス」を許可する
   git clone https://github.com/tatsuyat0714-design/jarvis.git
   cd jarvis/agent
   python -m unittest discover -s tests
   ```

4. 端末の設定 → アプリ → Termux → 電池 →「制限なし」（バックグラウンドで止められないように）

**確認すること**
- [ ] テストが `OK` で終わる（端末上の Python でも同じ結果になるか）

---

## ステップ 1. 端末への接続

A（ワイヤレスデバッグ）か B（Shizuku）のどちらか。両方試せるなら両方。

### A. ワイヤレスデバッグ

1. 設定 → デバイス情報 →「ビルド番号」を 7 回タップして開発者向けオプションを有効にする
2. 開発者向けオプション →「ワイヤレスデバッグ」を ON
3. **画面を分割して**、片方に Termux、もう片方に「ワイヤレスデバッグ」の画面を出す
   （ペア設定のダイアログは別のアプリに切り替えると閉じてしまうため）
4. 「ペア設定コードによるデバイスのペア設定」を押し、表示されたポートと 6 桁のコードで:

   ```sh
   bin/adbw.sh pair 127.0.0.1:<ペア用ポート> <6桁コード>
   ```

5. ワイヤレスデバッグの画面に出ている「IP アドレスとポート」の **ポート**（ペア用とは別）で接続:

   ```sh
   bin/adbw.sh connect 127.0.0.1:<接続用ポート>
   ```

6. 接続用ポートは Wi-Fi の切り替えや再起動で変わる。固定の 5555 番にしておくと楽（再起動までは有効）:

   ```sh
   adb -s 127.0.0.1:<接続用ポート> tcpip 5555
   bin/adbw.sh connect 127.0.0.1:5555
   echo 'export JARVIS_BACKEND=adb' >> ~/.bashrc   # 既定の接続先が 127.0.0.1:5555 なので接続先の設定は不要
   source ~/.bashrc
   ```

   5555 番に固定しない場合は、接続先を毎回指定する必要がある:
   `echo 'export JARVIS_ADB_HOST=127.0.0.1:<接続用ポート>' >> ~/.bashrc`

### B. Shizuku

1. Shizuku を入れて、ワイヤレスデバッグ経由で起動する（Shizuku アプリの案内どおり）
2. Shizuku →「ターミナルアプリで Shizuku を使用」→ ファイルを書き出す（`rish` と `rish_shizuku.dex`）
3. 書き出した `rish` を開き、`RISH_APPLICATION_ID` を `com.termux` にする
4. Termux で:

   ```sh
   cp /sdcard/<書き出し先>/rish /sdcard/<書き出し先>/rish_shizuku.dex $PREFIX/bin/
   chmod 700 $PREFIX/bin/rish
   chmod 400 $PREFIX/bin/rish_shizuku.dex   # Android 14 以降は dex が書き込み可能だと読み込まれない
   echo 'export JARVIS_BACKEND=shizuku' >> ~/.bashrc
   source ~/.bashrc
   ```

5. 初回は Shizuku の許可ダイアログで Termux を許可する

### 共通の確認

```sh
bin/adbw.sh doctor
```

**確認すること**
- [ ] `connected=yes`
- [ ] `uid=2000`（shell 権限）

**うまくいかないとき**
- `ADB 接続失敗`: ワイヤレスデバッグが OFF になっていないか（Wi-Fi が切れると自動で OFF になる）。ポートが変わっていないか
- `adb devices` に同じ端末が 2 つ出るのは正常（mDNS で見つかった分）。`-s` で接続先を指定しているので問題ない

---

## ステップ 2. 端末情報（probe）

```sh
python -m jarvis_agent probe
python -m jarvis_agent probe --json
```

**確認すること**（設定画面や通知領域の表示と比べる）
- [ ] 端末: メーカー・機種名・Android バージョンが正しい
- [ ] 電池: 残量 %、充電中かどうか、充電の種類（USB / AC / ワイヤレス）、温度
- [ ] 画面: `ON`
- [ ] 前面アプリ: `com.termux`
- [ ] メモリ・ストレージの空きが、設定 → ストレージ の表示とおおむね合う
- [ ] Wi-Fi: ON と SSID
- [ ] 最後の `取得失敗:` 行が **出ない**
- [ ] stderr の `(取得 NNms)`: 目安は 300ms 以下。記録しておく

```sh
python -m jarvis_agent probe --fast    # 2 回目以降は速くなるか（変化しやすい項目だけ取得）
```

**うまくいかないとき**
- 項目が欠けている・値がおかしい → 生の出力を保存して報告する:

  ```sh
  python -m jarvis_agent capture /sdcard/Download/jarvis_capture.txt
  ```

  ※ このファイルには SSID やアプリ名が入る。共有する前に中身を見て、見せたくない部分は消す

---

## ステップ 3. UI 解析（ui）

解析されるのは「今操作中の画面」なので、Termux で入力すると Termux の画面が解析されてしまう。
そこで、**数秒待ってから実行** するようにして、その間に設定アプリへ切り替える。

1. Termux で次を入力して Enter → すぐに設定アプリのトップ画面へ切り替える → 10 秒待って Termux に戻る:

   ```sh
   sleep 5; python -m jarvis_agent ui screen
   ```

2. 同じ要領で:

   ```sh
   sleep 5; python -m jarvis_agent ui find ネットワーク
   sleep 5; python -m jarvis_agent ui find ネットワーク --tap
   ```

**確認すること**
- [ ] `ui screen` に、設定画面に見えているボタン・入力欄が並ぶ（`[screen] com.android.settings`）
- [ ] 文言の無いアイコンボタンも「戻る」「検索」などの説明で出る
- [ ] `ui find` の `center=(x, y)` が、その項目の位置としてもっともらしい
- [ ] `--tap` で「ネットワークとインターネット」が開く
- [ ] 実行時間の体感（`uiautomator dump` は 1 回 0.5〜2 秒かかる）

**うまくいかないとき**
- `uiautomator dump に失敗しました`: 画面のアニメーション中は失敗しやすい。少し待ってもう一度
- 機種によって文言が違う（「ネットワークとインターネット」「接続」など）。画面に出ている言葉で探す
- 一覧がおかしいときは、UI のダンプを保存して報告する:

  ```sh
  bin/adbw.sh shell "uiautomator dump /sdcard/Download/jarvis_ui.xml"
  ```

---

## ステップ 4. ローカル LLM

1. llama.cpp を入れて、小さめのモデルを用意する（例: 1.5B〜3B の Instruct モデルの Q4_K_M 量子化 GGUF。日本語に強いもの）

   ```sh
   pkg install llama-cpp   # パッケージが無い場合は llama.cpp をソースからビルドする
   mkdir -p ~/models       # ここに .gguf を置く（ダウンロードは初回だけ）
   ```

2. **別の Termux セッション**（左端からスワイプ →「NEW SESSION」）でサーバーを起動:

   ```sh
   llama-server -m ~/models/<モデル>.gguf --host 127.0.0.1 --port 8080 -c 2048
   ```

3. 元のセッションで:

   ```sh
   python -m jarvis_agent ask "電池とストレージの状態を一言で" --echo   # 送る内容の確認（LLM は呼ばない）
   python -m jarvis_agent ask "電池とストレージの状態を一言で"
   python -m jarvis_agent ask "電池とストレージの状態を一言で"           # 2 回目（プロンプトキャッシュが効くか）
   ```

**確認すること**
- [ ] 返事が 1 文字目から順に表示される（ストリーミング）
- [ ] 返事の内容が `[device]` の実測値と矛盾しない
- [ ] stderr の `初トークン NNNms / 合計 NNNms` を 1 回目・2 回目とも記録する（2 回目の初トークンが短くなっていれば KV キャッシュが効いている）

**うまくいかないとき**
- `推論サーバーに接続できません`: サーバーのセッションが動いているか、ポートが 8080 か
- 遅すぎる場合は、より小さいモデルか、より強い量子化（Q4_0 など）を試す。GPU オフロード（`-ngl`）が使えるかは端末と llama.cpp のビルドによる

---

## ステップ 5. ステートマシン（run）

LLM サーバーを動かしたまま、Termux から実行する。エージェントが自分で設定アプリを起動するので、そのまま見ていればよい。
終わったら Termux に戻ってログを見る。

```sh
python -m jarvis_agent run "設定アプリでWi-Fiの画面を開いて"
```

**確認すること**
- [ ] stderr に `[OBSERVE] → [PLAN] → [ACT] → [VERIFY] → …` が流れ、最後に `[DONE]` になる
- [ ] 実際に Wi-Fi の画面が開く
- [ ] 1 手あたりの時間を記録する（dump と LLM の合計）

次に、**確認が出ること** を見る（必ず `N` と答える）:

1. Termux で次を入力して Enter → 10 秒以内に、設定 → アプリ → 適当なアプリ（例: Chrome）→ ストレージとキャッシュ、の画面を開く:

   ```sh
   sleep 10; python -m jarvis_agent run "キャッシュを削除して"
   ```

2. 少し待ってから Termux に戻る（確認待ちの間は何もタップされない）

- [ ] `⚠ 「キャッシュを削除」をタップ … を実行しますか？ [y/N]` と聞かれる
- [ ] `N` と答えると `[DONE] ユーザーが拒否: …` で終わり、何もタップされない

**うまくいかないとき**
- `形式エラー` が続いて `[ERROR]`: モデルが JSON を出せていない。より指示に従うモデルを試す
- 画面が変わらず同じ操作を繰り返す: 最大 15 手で止まる。`--max-steps 5` で短くして様子を見る

---

## ステップ 6. イベント（events）

**画面 OFF 中も止まらないように**、先に:

```sh
termux-wake-lock
```

```sh
python -m jarvis_agent events
```

表示されたまま、次の操作をする:

| 操作 | 期待する出力 |
|---|---|
| 電源ボタンで画面 OFF → 数秒待つ → ON | `screen_off` → `screen_on` |
| 充電ケーブルを挿す → 抜く | `charging_start` → `charging_stop` |
| ホームに戻って Chrome を開く | `app_foreground {'package': 'com.android.chrome', …}` |

**確認すること**
- [ ] 上の 3 つが出る
- [ ] 操作してから表示されるまでの遅れ（目安 1 秒以内）
- [ ] 何も操作していない間は何も出ず、CPU を使っていない（`top` で python がほぼ 0%）
- [ ] 画面 OFF 中に接続が切れない（10 分ほど OFF にしたあと、画面 ON が表示されるか）

**うまくいかないとき**
- 何も出ない → logcat の生データを確認する:

  ```sh
  bin/adbw.sh shell "logcat -b events -d | grep -E 'screen_toggled|battery_status|resume_activity' | tail -n 20"
  ```

  タグ名が違う機種がある。この出力を報告する
- logcat が使えない場合はポーリングで確認（反応は最大 60 秒遅れる）:

  ```sh
  python -m jarvis_agent events --poll
  ```

- 画面 OFF 中に `接続エラー` になる: Wi-Fi の省電力でワイヤレスデバッグが切れている可能性。Shizuku でも試す

---

## ステップ 7. タスク（調べるだけ）

まず **提案を保存するだけ**（`--save`）で、何も変更されないことを確かめる。

```sh
python -m jarvis_agent task optimize --save
python -m jarvis_agent task unused --save
python -m jarvis_agent task updates --save
python -m jarvis_agent pending
```

**確認すること**
- [ ] optimize: 空き容量と、裏で大きくメモリを使っているアプリが出る。提案に「全アプリのキャッシュを削除」
- [ ] unused: 「N 日以上未使用」のアプリが、実際にしばらく使っていないアプリになっている
  - 最近使ったアプリ・入れたばかりのアプリ・Termux / Shizuku が **含まれていない**こと（含まれていたら報告）
- [ ] updates: 1 回目は「初回のため基準を記録」。どれかのアプリを Play ストアで更新してからもう一度実行すると `更新: <パッケージ> 旧 → 新` が出る
- [ ] 各タスクの実行時間（`(NNNms)`）を記録する

**うまくいかないとき**
- `アプリ一覧または使用履歴を取得できませんでした`: 安全のため中止している。次の出力を報告する:

  ```sh
  bin/adbw.sh shell "dumpsys usagestats | grep -m 5 'package='"
  ```

- `バージョン情報を取得できませんでした`:

  ```sh
  bin/adbw.sh shell "dumpsys package packages | grep -m 6 -E 'Package \[|versionCode=|versionName='"
  ```

---

## ステップ 8. 重複ファイル（テスト用フォルダで）

本物の写真フォルダを対象にする前に、テスト用フォルダだけで確かめる。

1. テスト用のファイルを作る:

   ```sh
   mkdir -p /sdcard/jarvis_test/DCIM /sdcard/jarvis_test/Download
   head -c 3000000 /dev/urandom > /sdcard/jarvis_test/DCIM/IMG_0001.jpg
   cp /sdcard/jarvis_test/DCIM/IMG_0001.jpg "/sdcard/jarvis_test/Download/IMG_0001 (1).jpg"
   head -c 3000000 /dev/urandom > /sdcard/jarvis_test/Download/other.jpg   # 同じサイズだが別物
   ```

2. 対象をテスト用フォルダだけにする（`~/.jarvis/config.json`）:

   ```sh
   mkdir -p ~/.jarvis
   echo '{"dupe_roots": ["/sdcard/jarvis_test"], "trash_dir": "/sdcard/jarvis_test_trash"}' > ~/.jarvis/config.json
   ```

3. 実行（既定ルール → LLM の順。LLM は ステップ 4 のサーバーを動かしておく）:

   ```sh
   python -m jarvis_agent task dupes          # 提案を聞かれたら y
   ```

**確認すること**
- [ ] `重複 1 グループ` と出る（`other.jpg` は重複扱いされない）
- [ ] 残す側が `DCIM/IMG_0001.jpg`、ゴミ箱へ移す側が `Download/IMG_0001 (1).jpg`
- [ ] `y` と答えると、`/sdcard/jarvis_test_trash/<日時>/…/IMG_0001 (1).jpg` に移動している
- [ ] DCIM 側のファイルは残っている

LLM での判断:

```sh
cp /sdcard/jarvis_test/DCIM/IMG_0001.jpg "/sdcard/jarvis_test/Download/IMG_0001 (1).jpg"
python -m jarvis_agent task dupes --llm     # 今回は N と答える
```

- [ ] 提案の理由に `llm:` と LLM の理由が出る（`heuristic:` になっていたら LLM の応答が使えなかった）

後片付け:

```sh
rm -rf /sdcard/jarvis_test /sdcard/jarvis_test_trash
rm ~/.jarvis/config.json     # 既定の対象フォルダに戻す
```

---

## ステップ 9. 提案の実行（pending --apply）

ステップ 7 で保存した提案を、1 件ずつ確認しながら実行する。
**「全アプリのキャッシュを削除」だけ `y`、それ以外（アンインストール・終了）は `N`** と答える。

```sh
python -m jarvis_agent pending --apply
python -m jarvis_agent pending
```

**確認すること**
- [ ] キャッシュ削除で `キャッシュを削除しました（+N.NGB）` のように空き容量の変化が出る
- [ ] `N` と答えた提案は、次の `pending` にまだ残っている
- [ ] 実行した提案は `pending` から消えている

---

## ステップ 10. 常駐（daemon）

```sh
termux-wake-lock
python -m jarvis_agent daemon
```

次の流れを順に試し、stderr のログと通知を見る:

| 操作 | 期待するログ / 通知 |
|---|---|
| 画面 OFF | `event screen_off` → `task optimize: …（承認待ち +N）`、通知「ジャービス: optimize」 |
| 画面 ON | `event screen_on`、通知「承認待ちの提案が N 件あります」 |
| 画面 ON のまま充電ケーブルを挿す | `rule charging-maintenance: 画面 ON のため OFF まで先送り` |
| そのまま画面 OFF | `task updates` / `task unused` / `task dupes` が順に実行される |

※ 最後の dupes は既定の対象フォルダ（DCIM・Pictures・Download など）のハッシュを計算するので、初回は数分かかることがある。
無人なので提案は承認待ちに入るだけで、ファイルは動かない

**確認すること**
- [ ] 上の流れどおりに動く
- [ ] 2 回目の画面 OFF では optimize が **動かない**（6 時間のクールダウン）
- [ ] 30 分ほど放置したときの電池の減り（設定 → 電池 で Termux の使用量を見る）
- [ ] Wi-Fi を一度 OFF → ON にしたとき、`接続エラー（N 回目、…秒後に再接続）` のあと復帰するか
  （ワイヤレスデバッグは Wi-Fi OFF で切れるので、復帰には再度 `connect` が必要な場合がある。挙動を記録する）

クールダウンを消してやり直すとき:

```sh
python -c "import json,pathlib; p=pathlib.Path.home()/'.jarvis/state.json'; d=json.loads(p.read_text()); d.pop('last_run',None); p.write_text(json.dumps(d))"
```

（任意）再起動後も自動で動かすには、Termux:Boot を入れて:

```sh
mkdir -p ~/.termux/boot
cat > ~/.termux/boot/jarvis-agent <<'EOF'
#!/data/data/com.termux/files/usr/bin/sh
termux-wake-lock
cd ~/jarvis/agent && python -m jarvis_agent daemon >> ~/.jarvis/daemon.log 2>&1
EOF
chmod +x ~/.termux/boot/jarvis-agent
```

※ ワイヤレスデバッグは再起動で OFF になるので、ADB の場合は再起動後に接続し直す必要がある

---

## 結果の報告

次の表を埋めて、うまくいかなかったステップの出力（各ステップの「うまくいかないとき」で保存したもの）と一緒に共有する。
`~/.jarvis/log.jsonl` にタスクの実行ログがある。

| ステップ | 結果（OK / NG / 未実施） | 数値・メモ |
|---|---|---|
| 0. 準備（端末でのテスト） | | |
| 1. 接続（ADB / Shizuku） | | uid= |
| 2. probe | | 取得 ms、欠けた項目 |
| 3. UI 解析 | | dump の秒数 |
| 4. ローカル LLM | | モデル名、初トークン ms（1 回目 / 2 回目） |
| 5. ステートマシン | | 1 手の秒数、確認が出たか |
| 6. イベント | | 遅れ、出なかったイベント |
| 7. タスク（調べるだけ） | | 各タスクの ms、unused の誤判定 |
| 8. 重複ファイル | | |
| 9. 提案の実行 | | キャッシュで空いた GB |
| 10. 常駐 | | 30 分の電池消費 |

端末: 機種名 / Android バージョン / 接続方法（ADB か Shizuku）も添える。
