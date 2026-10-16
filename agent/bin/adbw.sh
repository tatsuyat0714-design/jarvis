#!/usr/bin/env bash
# adbw.sh — ADB / Shizuku 共通ラッパー（Termux 用）
#
# Python コントローラーはこのスクリプト経由でしかデバイスに触らない。
# バックエンドの切り替え・接続確立・タイムアウトをここに集約する。
#
#   JARVIS_BACKEND   adb | shizuku | auto   (既定: auto = rish があれば Shizuku、無ければ ADB)
#   JARVIS_ADB_HOST  ADB over TCP の接続先  (既定: 127.0.0.1:5555)
#   JARVIS_TIMEOUT   1コマンドの上限秒数    (既定: 10)
#
# 使い方:
#   adbw.sh pair <host:port> <code>   ワイヤレスデバッグのペアリング（初回のみ）
#   adbw.sh connect [host:port]       ADB over TCP に接続
#   adbw.sh shell <script>            デバイス側シェルで実行（stdout/exit code をそのまま返す）
#   adbw.sh status                    使用中のバックエンドと接続状態を表示
#   adbw.sh doctor                    環境チェック

set -u

BACKEND="${JARVIS_BACKEND:-auto}"
ADB_HOST="${JARVIS_ADB_HOST:-127.0.0.1:5555}"
TIMEOUT="${JARVIS_TIMEOUT:-10}"

die() { echo "adbw: $*" >&2; exit 2; }

have() { command -v "$1" >/dev/null 2>&1; }

with_timeout() {
    if have timeout; then timeout "$TIMEOUT" "$@"; else "$@"; fi
}

# $(...) のサブシェル内で die しても親は止まらないため、グローバル変数に解決する
resolve_backend() {
    case "$BACKEND" in
        adb|shizuku) ;;
        auto)
            if have rish; then BACKEND=shizuku
            elif have adb; then BACKEND=adb
            else die "adb も rish も見つかりません（pkg install android-tools / Shizuku の rish を PATH に）"
            fi ;;
        *) die "未知のバックエンド: $BACKEND" ;;
    esac
}

adb_connected() {
    adb devices 2>/dev/null | awk -v h="$ADB_HOST" '$1==h && $2=="device"{f=1} END{exit !f}'
}

ensure_adb() {
    have adb || die "adb がありません: pkg install android-tools"
    adb_connected && return 0
    with_timeout adb connect "$ADB_HOST" >/dev/null 2>&1
    adb_connected || die "ADB 接続失敗: $ADB_HOST（ワイヤレスデバッグを有効にし、adbw.sh pair → connect を実行）"
}

cmd_shell() {
    [ $# -ge 1 ] || die "shell: 実行するスクリプトを指定してください"
    local script="$*"
    resolve_backend
    case "$BACKEND" in
        shizuku) with_timeout rish -c "$script" ;;
        adb)     ensure_adb; with_timeout adb -s "$ADB_HOST" shell "$script" ;;
    esac
}

cmd_status() {
    resolve_backend
    echo "backend=$BACKEND"
    if [ "$BACKEND" = adb ]; then
        echo "host=$ADB_HOST"
        if adb_connected; then echo "connected=yes"; else echo "connected=no"; fi
    else
        if rish -c 'id -u' >/dev/null 2>&1; then echo "connected=yes"; else echo "connected=no"; fi
    fi
}

cmd_doctor() {
    for t in adb rish timeout python; do
        if have "$t"; then echo "ok   $t -> $(command -v "$t")"; else echo "miss $t"; fi
    done
    cmd_status
    echo "uid=$(cmd_shell 'id -u' 2>/dev/null || echo '?')  (2000=shell 権限, 0=root)"
}

sub="${1:-}"; [ $# -gt 0 ] && shift
case "$sub" in
    pair)    have adb || die "adb がありません"; [ $# -eq 2 ] || die "pair <host:port> <code>"; adb pair "$1" "$2" ;;
    connect) [ $# -ge 1 ] && ADB_HOST="$1"; ensure_adb && echo "connected $ADB_HOST" ;;
    shell)   cmd_shell "$@" ;;
    status)  cmd_status ;;
    doctor)  cmd_doctor ;;
    *)       sed -n '2,18p' "$0" | sed 's/^# \{0,1\}//'; exit 1 ;;
esac
