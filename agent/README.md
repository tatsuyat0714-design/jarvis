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

```sh
pkg install python android-tools git termux-api aapt
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
- 照合対象は `text` / `content-desc` / `hint` / `resource-id`、大文字小文字は区別しない
- 文言を持たない押せるコンテナ（LinearLayout など）は、子孫の文言で見つかる
- 面積 0・`enabled=false` の要素は候補から外す。候補は「押せる要素 → 面積が小さい順」
- `summarize()` は画面のハッシュも返し、操作後に画面が変わったかの判定に使う
- **パスワード欄**（`password="true"`）の文字列は読み取らず、LLM にも渡さない。入力も行わない
- `input text` は ASCII のみ（Android の制約）。引数はすべてシェル用にエスケープ・検証済み

## ステートマシン（agent.py）

```
IDLE ─> OBSERVE ─> PLAN ─┬─> ACT ─> VERIFY ─> OBSERVE …
                          ├─> CONFIRM ─承認─> ACT
                          │           └拒否─> DONE
                          └─ done ───> DONE        （例外・max_steps 超過は ERROR）
```

```sh
python -m jarvis_agent run "Wi-Fi の設定画面を開いて"
```

| 状態 | 内容 |
|---|---|
| OBSERVE | 端末情報（FAST のみ）＋ 画面要素の一覧。直前の VERIFY のダンプを流用し、dump は 1 手 1 回 |
| PLAN | プランナーが **次の一手だけ** を JSON で返す。毎回観測し直すので、想定外のダイアログにも追従できる |
| CONFIRM | 対象の文言に「削除・消去・アンインストール・購入・送信…」を含む操作は人に確認。無人実行（stdin が端末でない）では常に拒否 |
| ACT | `tap` / `type` / `key` / `launch` / `wait` の 5 種だけ。任意のシェルコマンドは実行できない |
| VERIFY | 画面ハッシュを比べ、変化が無ければ履歴に「画面変化なし」と残して次の PLAN に伝える |

- `LLMPlanner`: ローカル LLM に JSON を出させ、形式・アクション種別・キーコードを検査。不正なら 1 回だけ再要求
- `ScriptedPlanner`: 決まった手順を順に実行（定型タスク・テスト用）
- `max_steps`（既定 15）で無限ループを防止

## イベント駆動トリガー（events.py / daemon.py）

```sh
python -m jarvis_agent events          # 検出したイベントを表示するだけ（動作確認用）
python -m jarvis_agent daemon          # 常駐してルールに従いタスクを実行
python -m jarvis_agent daemon --poll   # logcat が使えない端末向け
```

- **logcat ストリームで待ち受け**: `logcat -b events` の `screen_toggled` / `battery_status` / `battery_level` /
  `wm_resume_activity` だけを端末側で絞って受け取る。イベントが無い間は read でブロックしているだけなので CPU を使わない
- 開始時刻を `-T` で端末の現在時刻にし、古いログで誤発火しない
- 値は StateTracker を通し、**変化したときだけ** イベントにする（最初の観測値は基準として扱うだけ）
- 切断時は 10 秒 → 20 秒 →…（最大 5 分）で再接続。3 回続けて失敗したらポーリングに切り替える
- ポーリングは画面 ON 中 60 秒、OFF 中 300 秒間隔

| イベント | ルール | タスク | クールダウン |
|---|---|---|---|
| 充電開始 | charging-maintenance | updates / unused / dupes | 20 時間。画面 ON 中なら **OFF になるまで先送り**（充電が外れたら取り消し） |
| 画面 OFF | screen-off-cleanup | optimize | 6 時間 |
| 電池 15% を下回る（非充電） | low-battery | optimize | 2 時間 |
| 画面 ON | screen-on-digest | 承認待ちがあれば通知 | 3 時間 |

重いタスク（unused / updates / dupes）は「充電中」か「電池 50% 以上」のときだけ実行する。
通知は `termux-notification`（`pkg install termux-api` と Termux:API アプリ）。無ければ stderr に出す。
Termux:Boot に `daemon` を登録すると再起動後も常駐する。

## タスク（tasks.py / dupes.py）

```sh
python -m jarvis_agent task optimize        # 実行して、提案を 1 件ずつ確認しながら実行
python -m jarvis_agent task dupes --llm     # 重複の「残す 1 つ」をローカル LLM に判断させる
python -m jarvis_agent task unused --save   # 確認せず承認待ちに保存
python -m jarvis_agent pending --apply      # 承認待ちを確認しながら実行
```

| タスク | 調べること | 提案（リスク） |
|---|---|---|
| optimize | 空き容量、`ps` の RSS 上位（前面アプリ・システムを除くユーザーアプリ） | `pm trim-caches`（低）、300MB 以上のバックグラウンドアプリを `am kill`（低） |
| unused | `dumpsys usagestats` の全期間から最終使用日時、候補だけ `firstInstallTime` | 30 日以上未使用のアプリをアンインストール（高）。インストール直後のアプリと Termux / Shizuku は除外 |
| updates | `dumpsys package` の versionCode / versionName を前回の記録と比較（更新・追加・削除） | `apk_dir` にある、インストール済みより新しい APK で `pm install -r`（高）。要 `pkg install aapt` |
| dupes | サイズ → 先頭 64KiB の SHA-256 → 全体の SHA-256。ハッシュはキャッシュ | 残す 1 つ以外をゴミ箱へ移動（高） |

### 安全のための設計

- **タスクは調べて提案するだけ**。変更は `apply()` を通ったときだけ起こる
- 提案は **(種類, 引数) の構造データ**で、シェル文字列は保存しない。`apply()` が引数を検証してからコマンドを組み立てる
  （パッケージ名の形式、実行直前の「ユーザーアプリか」の再確認、APK は `apk_dir` の中だけ）
- **無人では高リスクの提案を実行しない**。低リスクも `auto_apply_low_risk: true` のときだけ自動実行
- 使用履歴が取れなかったときは unused を中止する（「全アプリ未使用」と誤判定しないため）
- 重複の削除は **完全削除ではなくゴミ箱（`/sdcard/.jarvis_trash/日時/元のパス`）へ移動**。移動の直前に、残す側と移動する側を
  読み直してハッシュが一致することを確かめる。残す側が変わっていたら何もしない
- LLM の判断は **「残すファイルの番号」だけ**を返させる。パスを書かせないので、グループ外のファイルを指定できない。
  不正な応答は既定ルールに戻し、`keep: null`（迷う）なら何も消さない

### 設定（`~/.jarvis/config.json`、書いた項目だけ既定値を上書き）

```json
{
  "dupe_roots": ["/sdcard/DCIM", "/sdcard/Pictures", "/sdcard/Download"],
  "dupe_min_bytes": 1048576,
  "unused_days": 30,
  "unused_ignore": ["com.termux", "moe.shizuku.privileged.api"],
  "apk_dir": "/sdcard/Download",
  "bg_rss_mb": 300,
  "auto_apply_low_risk": false
}
```

状態は `~/.jarvis/`（`JARVIS_HOME` で変更可）に保存する: `state.json`（クールダウン・アプリ一覧・承認待ち）、
`hashes.json`（ハッシュキャッシュ）、`log.jsonl`（実行ログ）。
