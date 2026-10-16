# ジャービス オンデバイス・エージェント（Termux）

Termux 上で動き、ADB over TCP / Shizuku 経由で端末の状態を取得し、
ローカル LLM（llama.cpp / MLC LLM）へコンテキストとして渡す。外部 API は使わない。

- 0.1: 端末情報取得 → コンパクトなコンテキスト化 → ローカル LLM へストリーミング問い合わせ
- 0.2: UI 解析（uiautomator dump のキーワード高速パース）と、操作のステートマシン
- 0.3: イベント駆動トリガー（logcat 常駐）と定型タスク（最適化・未使用アプリ・更新・重複ファイル）

## 構成

```
agent/
├── bin/adbw.sh              ADB / Shizuku(rish) 共通ラッパー（接続確立・タイムアウト・バックエンド選択）
├── jarvis_agent/
│   ├── transport.py         adbw.sh を呼ぶ Transport（テスト用 FakeTransport あり）
│   ├── device.py            端末情報の一括取得とパース（DeviceProbe）
│   ├── context.py           LLM 用コンテキスト生成
│   ├── llm.py               ローカル推論サーバー（OpenAI 互換）クライアント
│   ├── ui.py                UI ダンプ解析（キーワード検索・座標算出）と tap / text / key / launch
│   ├── agent.py             ステートマシン（OBSERVE→PLAN→CONFIRM→ACT→VERIFY）とプランナー
│   ├── events.py            イベント検出（logcat ストリーム / ポーリング）と状態変化の追跡
│   ├── daemon.py            イベント → ルール → タスクの常駐ディスパッチャ
│   ├── tasks.py             定型タスクと、提案の検証・実行（apply）
│   ├── dupes.py             重複ファイル検出と「残す 1 つ」の判断（既定ルール / LLM）
│   ├── trash.py             重複整理のゴミ箱（記録・復元・完全削除）
│   ├── apk.py               APK のメタ情報の読み取り（aapt 不要）
│   ├── store.py             永続状態（~/.jarvis/）
│   └── __main__.py          メインコントローラー（CLI）
└── tests/                   python -m unittest discover -s tests
```

```
python CLI ──> WrapperTransport ──> adbw.sh ──┬─> adb -s 127.0.0.1:5555 shell   (ワイヤレスデバッグ)
                                              └─> rish -c                      (Shizuku)
     │
     ├─ DeviceProbe: 全セクションを 1 回の shell 呼び出しに束ねて取得 → パース
     ├─ context.render: 1 行 1 項目の短いテキストに変換
     └─ LocalLLM ──> http://127.0.0.1:8080/v1/chat/completions (SSE ストリーミング)
```

## 遅延対策

| 対策 | 内容 |
|---|---|
| 1 往復化 | 9 項目を `@@JARVIS:<name>` 区切りで 1 本のシェルスクリプトにまとめ、`adb shell` は 1 回だけ |
| 端末側で絞る | `dumpsys` の出力は端末側で `grep` してから転送 |
| FAST / SLOW 分割 | 電池・画面・前面アプリ・メモリだけ再取得（`--fast`）。機種名などは前回値を流用 |
| TTL キャッシュ | 5 秒以内の再取得は通信しない |
| 短いプロンプト | JSON ではなく短いテキスト。取れなかった項目は行ごと省略 |
| KV キャッシュ | 端末情報は user メッセージ側に置き、固定の system プロンプトを `cache_prompt` で再利用 |
| ストリーミング | 最初のトークンから表示。初トークン時間・合計時間を stderr に出す |

## セットアップ（Termux）

実機での確認手順は [DEVICE_TEST.md](DEVICE_TEST.md) にまとめている。

```sh
pkg install python android-tools git termux-api
termux-setup-storage   # 重複検出で /sdcard を読むため
git clone <このリポジトリ> && cd jarvis/agent
```

### 端末への接続（どちらか一方）

**A. ワイヤレスデバッグ（ADB over TCP）**

1. 開発者向けオプション → ワイヤレスデバッグを ON
2. 「ペア設定コードによるデバイスのペア設定」に表示される IP:ポート とコードで
   ```sh
   bin/adbw.sh pair 127.0.0.1:<ペア用ポート> <6桁コード>
   bin/adbw.sh connect 127.0.0.1:<接続用ポート>
   export JARVIS_ADB_HOST=127.0.0.1:<接続用ポート>
   ```
   ※ 接続用ポートは再起動のたびに変わる。

**B. Shizuku**

1. Shizuku アプリを起動し、「ターミナルアプリで使用」から `rish` と `rish_shizuku.dex` を書き出して Termux の PATH に置く
2. `export JARVIS_BACKEND=shizuku`

確認: `bin/adbw.sh doctor` で `uid=2000`（shell 権限）が出れば OK。

### ローカル LLM

llama.cpp の例（Termux 内でビルド、Vulkan/OpenCL が使える端末は GPU オフロード）:

```sh
llama-server -m model.gguf --host 127.0.0.1 --port 8080 -c 2048 -ngl 99
```

MLC LLM の場合は `mlc_llm serve <model> --host 127.0.0.1 --port 8080`。
どちらも OpenAI 互換 API なので `--llm-url` を合わせるだけで切り替えられる。
**ループバック以外の推論先は `LocalLLM` が拒否する**（HTTP_PROXY も無視）。

## 使い方

```sh
python -m jarvis_agent probe            # LLM に渡すコンテキストを表示
python -m jarvis_agent probe --json     # パース結果を JSON で
python -m jarvis_agent probe --fast     # 変化しやすい項目だけ
python -m jarvis_agent ask "ストレージと電池の状態を教えて"
python -m jarvis_agent ask "..." --echo # LLM を呼ばず送信内容を確認
python -m jarvis_agent capture my.txt   # 生出力を保存
python -m jarvis_agent --fixture my.txt probe   # 保存した出力で再現（実機不要）
```

出力例:

```
[device]
端末: Google Pixel 7 Android 14 (SDK 34)
電池: 82% charging via usb 31.2°C
画面: ON
前面アプリ: com.android.chrome
メモリ: 空き 2890MB / 7630MB
ストレージ: 空き 50.8GB / 109.9GB (54%使用)
ユーザーアプリ数: 87
Wi-Fi: ON HomeNet-5G
起動からの時間: 50.9h
```

機種によってパースが崩れた場合は `capture` した出力を `tests/fixtures/` に追加してテスト化する。

## UI 解析（ui.py）

```sh
python -m jarvis_agent ui screen               # 操作可能な要素だけを一覧（LLM に渡すのと同じ形）
python -m jarvis_agent ui find キャッシュ 戻る  # 一致した要素と中心座標
python -m jarvis_agent ui find 戻る --tap      # 最上位の候補をタップ
```

- **全画面の木を作らない**: `iterparse` でノードを 1 つずつ流し、キーワードに一致したものだけ残す
- **照合**: `text` / `content-desc` / `hint`（と子孫の文言）/ `resource-id` を、全角・半角・大文字小文字・空白の違いを
  吸収して比べる（「Ｗｉ－Ｆｉ」で「Wi-Fi」が見つかる）。候補は一致の良い順:
  文言と完全一致 → 文言の先頭が一致 → 文言に含む → resource-id に含む。同じなら小さい（具体的な）要素を先に
- 文言を持たない押せるコンテナ（LinearLayout など）は、子孫の文言で見つかる
- **スイッチ・チェックボックス** は `スイッチ「Wi-Fi」ON` のように状態付きで一覧に出す。ON/OFF の変化も画面の変化として数える
- スクロールできる一覧があるときは一覧に「続きがある」と書き、LLM がスクロールを選べるようにする
- `find_scrolling`: 画面に無い要素は一覧を下へスクロールしながら探す（最大 4 回。端まで来たら＝画面が変わらなくなったらやめる）
- `wait_for`: 読み込み中の画面などで、要素が現れるまで取り直して待つ
- 面積 0・`enabled=false` の要素は候補から外す
- **パスワード欄**（`password="true"`）の文字列は読み取らず、LLM にも渡さない。入力も行わない
- `input text` は ASCII のみ（Android の制約）。引数はすべてシェル用にエスケープ・検証済み

## ステートマシン（agent.py）

```
IDLE ─> OBSERVE ─> PLAN ─┬─> ACT ─> VERIFY ─> OBSERVE …
                          ├─> CONFIRM ─承認─> ACT
                          │           └拒否─> DONE
                          └─ done ───> DONE        （例外・max_steps 超過・安全のための停止は ERROR）
```

```sh
python -m jarvis_agent run "Wi-Fi の設定画面を開いて"
python -m jarvis_agent run "Wi-Fi をオンにして" --app com.android.settings   # 設定アプリ以外に移ったら止める
python -m jarvis_agent run --macro examples/open_wifi_settings.json          # 決まった手順を LLM なしで
```

| 状態 | 内容 |
|---|---|
| OBSERVE | 端末情報（FAST のみ）＋ 画面要素の一覧。直前の VERIFY のダンプを流用する |
| PLAN | プランナーが **次の一手だけ** を JSON で返す。毎回観測し直すので、想定外のダイアログにも追従できる |
| CONFIRM | 「削除・消去・アンインストール・購入・送信…」を含む操作は人に確認。**プランナーが書いたキーワードと、実際に押す要素の文言の両方** で判定する（id で指定されたボタンが「削除」でも確認が出る）。無人実行では常に拒否 |
| ACT | `tap` / `type` / `toggle` / `scroll` / `key` / `launch` / `wait` だけ。任意のシェルコマンドは実行できない。`tap` は画面に無ければスクロールして探す。`toggle` は既に目的の状態なら押さない |
| VERIFY | 固定時間は待たず、0.25 / 0.4 / 0.7 秒で取り直す。変化したあと同じ画面が 2 回続いたら落ち着いたとみなす（速い画面は速く、アニメーション途中の画面で判断しない） |

**止める条件**（安全のため ERROR で終える）

- `--app` を指定したとき、操作の結果それ以外のアプリに移った（通知・権限ダイアログなどシステムの画面と、
  始めた時の画面への戻りは可）。許可していないアプリの `launch` は実行前に断る
- 3 回続けて画面が変わらない
- 同じ画面で同じ操作を 3 回くり返した（2 回目に「次は別の操作を」と LLM に伝える）
- `max_steps`（既定 15）を超えた

**マクロ**（`--macro`）: 操作の配列を JSON で書く。各操作に `expect`（その後に現れるはずの要素）を書くと、
現れるまで待ち、現れなければ止める。確認のルール・止める条件は LLM の場合と同じ。マクロで `launch` するアプリは
自動で操作してよいアプリに含める。

```json
[
  {"action": "launch", "target": "com.android.settings", "expect": "ネットワーク"},
  {"action": "tap", "target": "ネットワークとインターネット", "expect": "Wi-Fi"},
  {"action": "toggle", "target": "Wi-Fi", "text": "on"}
]
```

各段階の時間（判断・操作・安定待ち）をログに出すので、どこが遅いかが分かる。

## イベント駆動トリガー（events.py / daemon.py）

```sh
python -m jarvis_agent events          # 検出したイベントを表示するだけ（動作確認用）
python -m jarvis_agent daemon          # 常駐してルールに従いタスクを実行
python -m jarvis_agent daemon --poll   # logcat が使えない端末向け
python -m jarvis_agent status          # 常駐中の daemon の状態
```

```
イベント源（logcat / ポーリング）─スレッド→ キュー → メインループ → ルール判定 → ワーカー（タスクを順に実行・中断可）
                                                      └ タイマー: 画面 OFF が idle_minutes 続いたら screen_idle
```

- **logcat ストリームで待ち受け**: `logcat -b events` の `screen_toggled` / `battery_status` / `battery_level` /
  `wm_resume_activity` だけを端末側で絞って受け取る。イベントが無い間は read でブロックしているだけなので CPU を使わない
- 開始時刻を `-T` で端末の現在時刻にし、古いログで誤発火しない
- 値は StateTracker を通し、**変化したときだけ** イベントにする（最初の観測値は基準として扱うだけ）
- **使っていない判定（screen_idle）**: 画面 OFF が `idle_minutes`（5 分）続いたら出す合成イベント。
  画面を一瞬消しただけ・ポケットで点いた程度では重い処理を始めない
- **タスクはワーカースレッドで実行** するので、数分かかる重複検出の間もイベントやタイマーを処理し続ける
- **中断**: 画面が ON になったら「使っていない間だけ」のルール（充電中の保守）を、充電が外れたら「充電中だけ」の
  ルールを中断する。重複検出はファイルを読む前ごとに中断を確かめ、そこまでのハッシュは保存して次回に使う。
  中断したルールは実行済みにせず、条件がそろう次の機会（充電中の次の screen_idle）にやり直す
- 重いタスク（unused / updates / dupes）は「充電中」か「電池 50% 以上」、かつ **電池温度が `heavy_max_temp_c`（40℃）未満**
  のときだけ実行する（電池の温度は `battery_level` イベントから読む）
- **切断と再接続**: 10 秒 → 20 秒 →…（最大 5 分）で再接続し、**切断中に起きた変化**（充電の抜き差しなど）も
  再接続時の状態の差からイベントにする。3 回続けて失敗したらポーリングに切り替える（画面 ON 中 60 秒、OFF 中 300 秒間隔）
- **二重起動しない**: `~/.jarvis/daemon.lock` で 1 つだけ動かす（Termux:Boot と手動起動が重なってもタスクは 1 回）
- **状態の見える化**: `~/.jarvis/status.json` に端末の状態・実行中のタスク・待ち・先送り・各ルールの前回実行・
  最近のイベントを書き、`status` で表示する
- 実行ログ `log.jsonl` は 1MB を超えたら `log.jsonl.1` に回す

| イベント | ルール | タスク | クールダウン |
|---|---|---|---|
| 充電開始 | charging-maintenance | updates / unused / dupes | 20 時間。使っていない状態（screen_idle）になるまで先送り。充電が外れたら取り消し |
| 画面 OFF が 5 分続く | idle-cleanup | optimize | 6 時間 |
| 電池 15% を下回る（非充電） | low-battery | optimize | 2 時間 |
| 画面 ON | screen-on-digest | 承認待ちがあれば通知 | 3 時間 |

**ルールの変更**（`~/.jarvis/config.json`）: 名前ごとに `enabled` / `cooldown_h` / `tasks` / `hours`（実行してよい時間帯、
日をまたいでもよい）を上書きできる。不正な値は無視して起動時に警告を出す。

```json
{
  "idle_minutes": 5,
  "heavy_max_temp_c": 40,
  "rules": {
    "charging-maintenance": {"hours": [0, 6], "cooldown_h": 24},
    "idle-cleanup": {"enabled": false}
  }
}
```

`hours` の外で条件がそろった「使っていない間だけ」のルールは、時間帯に入った後の次の screen_idle まで待つ。
通知は `termux-notification`（`pkg install termux-api` と Termux:API アプリ）。無ければ stderr に出す。
Termux:Boot に `daemon` を登録すると再起動後も常駐する。

## タスク（tasks.py / dupes.py）

```sh
python -m jarvis_agent task optimize        # 実行して、提案を 1 件ずつ確認しながら実行
python -m jarvis_agent task dupes --llm     # 重複の「残す 1 つ」をローカル LLM に判断させる
python -m jarvis_agent pending --apply --task dupes   # 重複の提案だけ確認（a で残りをまとめて実行）
python -m jarvis_agent trash list           # ゴミ箱の一覧
python -m jarvis_agent trash restore latest # 直近のバッチを元の場所へ戻す
python -m jarvis_agent trash purge --days 30  # 30 日より古いバッチを完全に削除（確認あり）
python -m jarvis_agent task unused --save   # 確認せず承認待ちに保存
python -m jarvis_agent pending --apply      # 承認待ちを確認しながら実行
```

| タスク | 調べること | 提案（リスク） |
|---|---|---|
| optimize | 空きメモリ・空き容量、アプリごとのキャッシュ（`dumpsys diskstats`）、裏で動くアプリのメモリ（履歴を記録） | キャッシュ削除（低）、常習的に重いアプリの終了（低）、ほとんど使わない常習アプリのバックグラウンド制限（高）。詳細は下記 |
| unused | 使用履歴（前面・表示・他アプリからの利用）、端末の機能に組み込まれたアプリ、アプリ容量、候補の `firstInstallTime` | 30 日以上未使用のアプリを無効化（高・戻せる）→ 無効のまま 30 日でアンインストール（高）。詳細は下記 |
| updates | `dumpsys package` の versionCode / versionName を前回の記録と比較（更新・追加・削除） | `apk_dir` にある、インストール済みより新しい APK で `pm install -r`（高）。詳細は下記 |
| dupes | サイズ → 先頭 64KiB の SHA-256 → 全体の SHA-256。ハッシュはキャッシュ | 残す 1 つ以外をゴミ箱へ移動（高） |

### 未使用アプリ（unused）

- **使ったかどうか** は、使用履歴の `lastTimeUsed`（前面に出した）・`lastTimeVisible`（画面に見えた）・
  `lastTimeComponentUsed`（ウィジェットや他のアプリから使われた）の最も新しい時刻で判断する
- **端末の機能に組み込まれているアプリは対象外**（開かなくても使われているため。消すと電話や入力が壊れる）:
  既定のホーム・SMS・電話・ブラウザ・アシスタントなどの役割を持つアプリ、有効なキーボード・ユーザー補助・
  通知リスナー・自動入力（パスワード管理）・常時接続 VPN、端末管理アプリ、ホーム画面に置いたウィジェット、ライブ壁紙。
  報告に「対象外」として名前を出す。実行の直前にも同じ確認をやり直す
- インストール直後のアプリ・`unused_ignore`・利用者が自分で無効にしたアプリは対象外
- アプリ本体 + データの容量（`dumpsys diskstats`）を表示し、**大きい順** に提案する
- **段階的に整理する**（`unused_action: "disable"`、既定）:
  1. まず **無効化**（`pm disable-user`）を提案。アプリは隠れて動かなくなるが、データは残る。
     `disabled undo <パッケージ>` / `disabled undo all` で戻せる
  2. 無効のまま `unused_uninstall_after_days`（30 日）たっても戻されなかったら、**アンインストール** を提案する
  3. 利用者が自分で有効に戻したアプリは記録から外し、通常の判定に戻す

  すぐにアンインストールを提案したい場合は `unused_action: "uninstall"`
- 残したいアプリの提案は `pending --dismiss <ID>` で見送る。`dismiss_days`（90 日）の間は同じ提案を出さない
  （他のタスクの提案にも使える）

```sh
python -m jarvis_agent disabled                # このエージェントが無効化したアプリの一覧
python -m jarvis_agent disabled undo all       # すべて有効に戻す
python -m jarvis_agent pending --dismiss <ID>  # 提案を見送る（ID は pending の左端）
```

### アプリ最適化（optimize）

1 回の shell 呼び出しで、ストレージ・メモリ・プロセスごとのメモリ・アプリごとのキャッシュ・前面アプリ・
ホームアプリ・キーボードをまとめて取得する。

- **触らないアプリ**: 前面アプリ・ホームアプリ・キーボード（止めると操作できなくなる）、`unused_ignore`（Termux・Shizuku 等）、
  `optimize_ignore`
- **キャッシュ削除** は、キャッシュ合計が `cache_min_mb`（500MB）以上か、ストレージ使用率が `storage_low_pct`（85%）以上の
  ときだけ提案する（毎回出すと承認待ちが埋まるため）。キャッシュの多いアプリ上位 5 件も報告する
- **バックグラウンドの監視**: 実行のたびに、裏で動くユーザーアプリのメモリ使用量を記録する（`bg_samples`、直近 `bg_window_days` 日）。
  `bg_rss_mb`（300MB）以上を `bg_chronic_count`（3）回以上記録したアプリを「常習」とする
- **終了（`am kill`）** を提案するのは、常習のアプリか、空きメモリが `mem_low_pct`（10%）を切っているときの重いアプリだけ
  （その時たまたま大きかっただけのアプリは止めない）
- **バックグラウンド制限**（Android 12 以降）: 常習で、`restrict_unused_days`（7 日）以上開いていないアプリを
  スタンバイバケット「restricted」にする提案（高リスク: 通知や同期が遅れることがある）。
  - 実行前のバケットを記録し、`restricted undo <パッケージ>` / `restricted undo all` で戻せる
  - 利用者がそのアプリを開くとシステムが自動で制限を外す。その場合 undo は記録を消すだけ
  - 使用履歴が読めないときは提案しない

```sh
python -m jarvis_agent restricted              # このエージェントが制限したアプリの一覧
python -m jarvis_agent restricted undo all     # すべて元に戻す
```

### 重複ファイル整理（dupes）

**どれを残すか**（既定ルール。`--llm` のときも、LLM の応答が使えなければこのルールに戻る）

1. 保護フォルダ（`dupe_protect`）にあるもの
2. 保存場所: カメラの元ファイル（DCIM）→ Pictures・Movies・Music・Documents など → その他 → Download →
   LINE・WhatsApp・Telegram などアプリが保存したコピー（親が Pictures でもアプリのフォルダならこちら）
3. 名前にコピー印（`(1)`・`のコピー`・`copy` など）が無いもの
4. 古いもの

保護フォルダのファイルは、残す側に選ばれなくても **ゴミ箱へは移さない**。グループ全体が保護フォルダ内なら提案しない。
LLM には保護されたファイルに `[保護]` を付けて見せる。

**ゴミ箱**（`trash_dir`、既定 `/sdcard/.jarvis_trash`）

- 1 回の確認セッションで移したファイルを 1 つの「バッチ」（日時フォルダ）にまとめ、`manifest.json` に
  元の場所・移動先・SHA-256・残したファイルを記録する。`.nomedia` を置くのでギャラリーには出ない
- `trash restore <バッチ>` で元の場所へ戻す。戻す前に、戻し先が `dupe_roots` の中か、移動元がそのバッチの中か、
  中身が記録の SHA-256 と同じか、元の場所に別のファイルが無いかを確かめ、満たさないものは戻さずに理由を表示する
  （manifest は共有ストレージにあり他のアプリから書き換えられうるため、記録をそのまま信用しない）
- `trash purge` だけが完全に削除する。端末での確認（y）が必須で、daemon からは実行されない
- v0.3 以前に移したバッチは記録が無いので一覧には出るが復元はできない（purge は可能）

**その他**

- 走査時に見つからなかったファイルのハッシュはキャッシュから消す（キャッシュが増え続けない）
- `dupe_exclude` に書いたフォルダは走査しない（フォルダ単位で比べるので `DCIM` を除外しても `DCIM2` は対象）
- `pending --apply` でゴミ箱への移動（とプロセス終了）の提案が続くときは `a` で残りをまとめて実行できる。
  アンインストールや APK のインストールなど取り消せないものは、必ず 1 件ずつ確認する

### 自動更新（updates）の流れ

1. `apk_dir`（既定 `/sdcard/Download`）直下の `.apk` を読む。AndroidManifest.xml（バイナリ XML）を自前で解析するので
   **aapt は不要**（読めない APK だけ、aapt があればそれで読む）
2. パッケージごとに **最も新しい版だけ** を候補にする
3. 次のものは提案せず、理由を報告に出す: 端末の方が新しい / 未インストール / 分割 APK の一部 / `minSdk` が端末より高い / 読めない
4. 提案には APK のパス・パッケージ名・versionCode（`versionCodeMajor` 込み）・**SHA-256** を記録する
5. 実行（承認）時に、もう一度すべて確かめてからインストールする:
   - SHA-256 が提案時と同じか（提案後に差し替えられていないか）
   - APK を読み直し、パッケージ名と versionCode が提案と一致するか
   - 端末の版が、まだ APK より古いか（その間に Play ストアで更新されていないか。ダウングレードはしない）
6. `pm install -r` の失敗は理由を日本語で示す（署名違い・容量不足・CPU 非対応・分割 APK など）
7. インストール後に端末の版を読み直し、期待どおりに上がったかを確かめてから記録を更新する

署名が違う APK（別の提供元が作ったもの）は Android 自体が更新を拒否する。このエージェントは署名の偽装や回避はしない。

### 安全のための設計

- **タスクは調べて提案するだけ**。変更は `apply()` を通ったときだけ起こる
- 提案は **(種類, 引数) の構造データ**で、シェル文字列は保存しない。`apply()` が引数を検証してからコマンドを組み立てる
  （パッケージ名の形式、実行直前の「ユーザーアプリか」の再確認、APK は `apk_dir` の中だけ）
- **無人では高リスクの提案を実行しない**。低リスクも `auto_apply_low_risk: true` のときだけ自動実行
- 使用履歴が取れなかったときは unused を中止する（「全アプリ未使用」と誤判定しないため）
- 重複の削除は **完全削除ではなくゴミ箱へ移動**し、記録から元に戻せる。移動の直前に、残す側と移動する側を
  読み直してハッシュが一致することを確かめる。残す側が変わっていたら何もしない。完全削除は `trash purge`（確認必須）だけ
- LLM の判断は **「残すファイルの番号」だけ**を返させる。パスを書かせないので、グループ外のファイルを指定できない。
  不正な応答は既定ルールに戻し、`keep: null`（迷う）なら何も消さない

### 設定（`~/.jarvis/config.json`、書いた項目だけ既定値を上書き）

```json
{
  "dupe_roots": ["/sdcard/DCIM", "/sdcard/Pictures", "/sdcard/Download"],
  "dupe_min_bytes": 1048576,
  "dupe_protect": ["/sdcard/DCIM/Camera"],
  "dupe_exclude": ["/sdcard/Download/keep"],
  "trash_dir": "/sdcard/.jarvis_trash",
  "unused_days": 30,
  "unused_action": "disable",
  "unused_uninstall_after_days": 30,
  "dismiss_days": 90,
  "unused_ignore": ["com.termux", "moe.shizuku.privileged.api"],
  "apk_dir": "/sdcard/Download",
  "bg_rss_mb": 300,
  "bg_window_days": 3,
  "bg_chronic_count": 3,
  "mem_low_pct": 10,
  "restrict_unused_days": 7,
  "cache_min_mb": 500,
  "storage_low_pct": 85,
  "optimize_ignore": [],
  "auto_apply_low_risk": false
}
```

状態は `~/.jarvis/`（`JARVIS_HOME` で変更可）に保存する: `state.json`（クールダウン・アプリ一覧・承認待ち）、
`hashes.json`（ハッシュキャッシュ）、`log.jsonl`（実行ログ）。
