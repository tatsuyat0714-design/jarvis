# ジャービス オンデバイス・エージェント（Termux）

Termux 上で動き、ADB over TCP / Shizuku 経由で端末の状態を取得し、
ローカル LLM（llama.cpp / MLC LLM）へコンテキストとして渡す。外部 API は使わない。

- 0.1: 端末情報取得 → コンパクトなコンテキスト化 → ローカル LLM へストリーミング問い合わせ
- 0.2: UI 解析（uiautomator dump のキーワード高速パース）と、操作のステートマシン

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
pkg install python android-tools git
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

## 今後の実装順

1. **イベント駆動トリガー**: 画面点灯・充電開始・アプリ起動を契機に FAST プローブのみ実行（常時ポーリングしない）
2. **タスク**: キャッシュクリア / バックグラウンド監視、`dumpsys usagestats` による未使用アプリ抽出、バージョン差分チェック、サイズ→SHA-256 の重複検出（削除は CONFIRM 経由）
