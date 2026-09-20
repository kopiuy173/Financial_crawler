#!/usr/bin/env bash
# =============================================================================
# rotate_keys.sh — 密钥轮换助手（写入全部持有处 + 探活/复验，失败自动回滚）
#
# 用法：
#   bash deploy/rotate_keys.sh --check                             # 只盘点，不改文件
#   bash deploy/rotate_keys.sh --deepseek [--dify-dataset]         # 交互粘贴（不回显）
#   bash deploy/rotate_keys.sh --deepseek --keys-file ~/k.txt      # 非交互（文件须 600）
#   bash deploy/rotate_keys.sh --deepseek --no-verify --no-restart # 离线演练
#   bash deploy/rotate_keys.sh --deepseek --harden-rc              # 顺带 chmod 600 rc 文件
#
# 次序（1、6 在控制台做，其余本脚本做）：
#   1) 控制台新建 key（先别删旧的）
#   2) 写入全部持有处，改前逐文件备份 <file>.bak.<时间戳>
#   3) 真实 API 探活；改后校验不过则自动回滚
#   4) chmod 600 收敛权限
#   5) 重启守护并用干净环境复验解析链路
#   6) 控制台删除旧 key
#
# 范围：仅 DEEPSEEK_API_KEY / DIFY_API_KEY 两把对外 key；Dify 内部密钥不在本脚本内。
# =============================================================================
set -euo pipefail

SELF_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
PROJ=$(cd "$SELF_DIR/.." && pwd)
HOME_DIR=${HOME:-/root}
DS_FILE="$HOME_DIR/.deepseek_key"
RC_FILES=("$HOME_DIR/.zshrc" "$HOME_DIR/.bashrc")
DIFY_MIG_ENV="$PROJ/dify_migration/.env"
PROJ_ENV="$PROJ/.env"
SERVICE=${SERVICE:-fin-news-scheduler}
DIFY_BASE=${DIFY_BASE:-http://127.0.0.1}
TS=$(date +%Y%m%d-%H%M%S)
BAK_SUFFIX=".bak.$TS"
DO_CHECK=0; DO_DS=0; DO_DIFY=0; DO_VERIFY=1; DO_RESTART=1; DO_HARDEN_RC=0; KEYS_FILE=""
PY=${PY:-python3}

TMPFILES=()
cleanup() { local f; for f in ${TMPFILES[@]+"${TMPFILES[@]}"}; do rm -f "$f"; done; }
trap cleanup EXIT

log()  { printf '%s\n' "$*"; }
warn() { printf '[warn] %s\n' "$*" >&2; }
die()  { printf '[error] %s\n' "$*" >&2; exit 1; }

# --- 密钥读写（不回显明文）---

# 取文件里 KEY 的值（容忍 export 前缀/引号）
get_kv() {
  sed -nE "s/^[[:space:]]*(export[[:space:]]+)?$2=(.*)$/\2/p" "$1" 2>/dev/null \
    | head -1 | tr -d '"'"'"' '
}

# 指纹（判断两处是否同一个 key）
fp() { printf %s "$1" | sha256sum | awk '{print substr($1,1,12)}'; }

# 合法字符白名单
valid_val() { case ${1:-} in ''|*[!A-Za-z0-9._:@/+-]*) return 1;; *) return 0;; esac; }

# 就地替换 / 追加 KEY=VALUE（保留原行位置，不写引号）
set_kv() {
  local file=$1 key=$2 val=$3 style=${4:-plain} repl
  [ -f "$file" ] || : > "$file"
  if [ "$style" = export ]; then repl="export $key=$val"; else repl="$key=$val"; fi
  if grep -qE "^[[:space:]]*(export[[:space:]]+)?$key=" "$file"; then
    sed -i -E "s|^[[:space:]]*(export[[:space:]]+)?$key=.*|$repl|" "$file"
  else
    printf '%s\n' "$repl" >> "$file"
  fi
}

# GET 探活，只输出 HTTP 状态码（密钥经 curl -K 传，不进 argv）
api_code() {
  local cf; cf=$(mktemp); TMPFILES+=("$cf"); chmod 600 "$cf"
  printf 'header = "Authorization: Bearer %s"\nsilent\noutput = "/dev/null"\nwrite-out = "%%{http_code}"\nmax-time = 20\n' "$1" > "$cf"
  curl -K "$cf" "$2" 2>/dev/null || echo 000
}

backup()  { local f=$1; [ -f "$f" ] || return 0; cp -p "$f" "$f$BAK_SUFFIX"; chmod 600 "$f$BAK_SUFFIX"; }
rollback(){ local f=$1; [ -f "$f$BAK_SUFFIX" ] || return 0; cp -p "$f$BAK_SUFFIX" "$f"; }

# 干净环境复验（env -u：等价守护进程条件）
probe_len() {
  env -u DEEPSEEK_API_KEY -u DIFY_API_KEY "$PY" - "$PROJ" "$1" <<'EOF' 2>/dev/null || echo 0
import sys
sys.path.insert(0, sys.argv[1])
import model_interface as mi
print(len(mi._secret(sys.argv[2])))
EOF
}

# --- 盘点（只读）--------------------------------------------------------------
cmd_check() {
  log "=== 持有处盘点（只给 sha256 前 12 位 + 探活码）==="
  printf '  %-44s %-30s %s\n' '文件(权限)' 'DEEPSEEK_API_KEY' 'DIFY_API_KEY'
  local f perm ds dify c1 c2 p
  for f in "$DS_FILE" "${RC_FILES[@]}" "$PROJ_ENV" "$DIFY_MIG_ENV" "$PROJ/.env.example" "$PROJ/dify_migration/.env.example"; do
    [ -f "$f" ] || continue
    perm=$(stat -c '%a' "$f")
    ds=$(get_kv "$f" DEEPSEEK_API_KEY); dify=$(get_kv "$f" DIFY_API_KEY)
    if [ -n "$ds" ]; then c1="fp=$(fp "$ds")  http=$(api_code "$ds" https://api.deepseek.com/models)"; else c1='-'; fi
    if [ -n "$dify" ]; then c2="fp=$(fp "$dify")  http=$(api_code "$dify" "$DIFY_BASE/v1/datasets?page=1&limit=1")"; else c2='-'; fi
    printf '  %-44s %-30s %s\n' "$f ($perm)" "$c1" "$c2"
  done
  log ""
  log "  同一 fp = 同一个 key；http=200 = 该值当前仍有效"
  log "  解析优先级：环境变量 > ~/.deepseek_key > 项目 .env"
  log ""
  log "=== 守护进程的真正生效值（干净环境复验，只给长度）==="
  log "  DEEPSEEK_API_KEY len=$(probe_len DEEPSEEK_API_KEY)    DIFY_API_KEY len=$(probe_len DIFY_API_KEY)"
  log "  守护注入的环境: $(systemctl --user show -p Environment --value "$SERVICE" 2>/dev/null | tr -d '\r')"
  log ""
  log "=== 权限收敛建议（含明文密钥却非 600）==="
  for f in "${RC_FILES[@]}"; do
    [ -f "$f" ] || continue
    p=$(stat -c '%a' "$f")
    if [ "$p" != 600 ]; then log "  chmod 600 $f    # 当前 $p"; fi
  done
  if [ -f "$DIFY_MIG_ENV" ]; then
    p=$(stat -c '%a' "$DIFY_MIG_ENV")
    if [ "$p" != 600 ]; then log "  chmod 600 $DIFY_MIG_ENV    # 当前 $p"; fi
  fi
  log ""
  log "=== Dify 栈 ==="
  timeout 15 docker ps --format '  {{.Names}}\t{{.Image}}' 2>/dev/null | head -12 || warn "  docker 不可用"
  return 0
}

# --- 读取新值（交互不回显，或 --keys-file）---
read_new() {
  local label=$1 v1 v2
  if [ -n "$KEYS_FILE" ]; then get_kv "$KEYS_FILE" "$label"; return 0; fi
  [ -t 0 ] || die "非交互环境：请用 --keys-file <600权限文件> 提供 $label"
  while :; do
    printf '  粘贴新的 %s（回车结束，不回显）: ' "$label" >&2; read -rs v1 || return 1; printf '\n' >&2
    printf '  再粘贴一次确认: ' >&2; read -rs v2 || return 1; printf '\n' >&2
    [ "$v1" = "$v2" ] || { warn "两次输入不一致，重来"; continue; }
    valid_val "$v1" || { warn "含非法字符（仅允许 A-Za-z0-9._:@/+-），重来"; continue; }
    printf '%s' "$v1"; return 0
  done
}

# --- 轮换 DeepSeek key ---------------------------------------------------------
rotate_deepseek() {
  local new=$1 code f got rc_ok=1 targets=()
  log "--- 轮换 DEEPSEEK_API_KEY（${#new} 字符）---"
  if [ "$DO_VERIFY" = 1 ]; then
    code=$(api_code "$new" https://api.deepseek.com/models)
    log "  新值探活 api.deepseek.com/models → $code"
    if [ "$code" != 200 ]; then
      die "新值探活失败（$code）：一个文件都不改。请确认已在控制台新建 key，且复制时无多余空格"
    fi
  else
    warn "  --no-verify：跳过探活与复验（演练模式）"
  fi
  targets=("$DS_FILE" "$DIFY_MIG_ENV")
  for f in "${RC_FILES[@]}"; do [ -f "$f" ] && targets+=("$f"); done
  for f in "${targets[@]}"; do backup "$f" || die "备份 $f 失败，中止（未改动任何文件）"; done
  log "  已备份 ${#targets[@]} 个文件（后缀 $BAK_SUFFIX，权限 600）"
  set_kv "$DS_FILE" DEEPSEEK_API_KEY "$new" || rc_ok=0
  for f in "${RC_FILES[@]}"; do
    [ -f "$f" ] || continue
    set_kv "$f" DEEPSEEK_API_KEY "$new" export || rc_ok=0
  done
  set_kv "$DIFY_MIG_ENV" DEEPSEEK_API_KEY "$new" || rc_ok=0
  if [ "$rc_ok" != 1 ]; then
    warn "写入过程出错 → 回滚"
    for f in "${targets[@]}"; do rollback "$f"; done
    die "已回滚，全部文件保持原样（检查磁盘权限/空间后重试）"
  fi
  chmod 600 "$DS_FILE" "$DIFY_MIG_ENV" 2>/dev/null || true
  if [ "$DO_VERIFY" = 1 ]; then
    got=$(probe_len DEEPSEEK_API_KEY)
    if [ "$got" = "${#new}" ]; then
      log "  复验：干净环境（env -u DEEPSEEK_API_KEY）解析到长度 $got ✔"
    else
      warn "复验失败（解析到长度 $got，期望 ${#new}）→ 回滚"
      for f in "${targets[@]}"; do rollback "$f"; done
      die "已回滚"
    fi
  fi
  log "  写入完成；旧值此刻仍有效，确认无误后再去控制台删除它"
}

# --- 轮换 Dify 数据集 key ------------------------------------------------------
rotate_dify() {
  local new=$1 code got
  log "--- 轮换 DIFY_API_KEY（Dify 数据集密钥，形如 dataset-…；${#new} 字符）---"
  if [ "$DO_VERIFY" = 1 ]; then
    code=$(api_code "$new" "$DIFY_BASE/v1/datasets?page=1&limit=1")
    log "  新值探活 $DIFY_BASE/v1/datasets → $code"
    if [ "$code" != 200 ]; then
      die "新值探活失败（$code）：未改任何文件。确认它是“知识库数据集”的密钥，而非应用密钥"
    fi
  fi
  backup "$PROJ_ENV" || die "备份 $PROJ_ENV 失败，中止"
  log "  已备份 $PROJ_ENV$BAK_SUFFIX"
  if ! set_kv "$PROJ_ENV" DIFY_API_KEY "$new"; then rollback "$PROJ_ENV"; die "写入失败，已回滚"; fi
  chmod 600 "$PROJ_ENV"
  if [ "$DO_VERIFY" = 1 ]; then
    got=$(probe_len DIFY_API_KEY)
    if [ "$got" = "${#new}" ]; then
      log "  复验：干净环境解析到长度 $got ✔"
    else
      rollback "$PROJ_ENV"; die "复验失败（解析到长度 $got）→ 已回滚"
    fi
  fi
  log "  端到端建议（可选）:"
  log "    cd $PROJ && set -a && . ./.env && set +a && $PY dify_migration/python/sync_news_daily.py --date <交易日> --dry-run"
}

# --- 重启守护并复验 -----------------------------------------------------------
restart_daemon() {
  log "--- 重启守护（密钥在模块级读取，必须重启才生效）---"
  if [ "$DO_RESTART" != 1 ]; then
    warn "  --no-restart：跳过。请手动执行 systemctl --user restart $SERVICE"
    return 0
  fi
  systemctl --user restart "$SERVICE" \
    || die "systemctl --user restart $SERVICE 失败（查看 systemctl --user status $SERVICE）"
  sleep 3
  log "  状态=$(systemctl --user is-active "$SERVICE")  PID=$(pgrep -f news_scheduler.py | head -1)"
  log "  最近日志: $(tail -1 "$PROJ/logs/pipeline.log" 2>/dev/null || echo '(无)')"
}

# --- 入口 ---------------------------------------------------------------------
usage() {
  awk 'NR>1 { if (/^#/) { sub(/^# ?/, ""); print; next } exit }' "$0"
  exit 0
}

main() {
  local k p
  while [ "$#" -gt 0 ]; do
    case $1 in
      --check)        DO_CHECK=1 ;;
      --deepseek)     DO_DS=1 ;;
      --dify-dataset) DO_DIFY=1 ;;
      --keys-file)    shift; KEYS_FILE=${1:-} ;;
      --keys-file=*)  KEYS_FILE=${1#*=} ;;
      --no-verify)    DO_VERIFY=0 ;;
      --no-restart)   DO_RESTART=0 ;;
      --harden-rc)    DO_HARDEN_RC=1 ;;
      -h|--help)      usage ;;
      *)              die "未知参数：$1（用 -h 看用法）" ;;
    esac
    shift
  done
  if [ -n "$KEYS_FILE" ]; then
    [ -f "$KEYS_FILE" ] || die "--keys-file 不存在：$KEYS_FILE"
    p=$(stat -c '%a' "$KEYS_FILE")
    if [ "$p" != 600 ]; then warn "$KEYS_FILE 权限为 $p，建议 chmod 600"; fi
  fi
  if [ "$DO_HARDEN_RC" = 1 ]; then
    for f in "${RC_FILES[@]}"; do
      [ -f "$f" ] || continue
      chmod 600 "$f"
      log "  chmod 600 $f"
    done
  fi
  if [ "$DO_CHECK" = 1 ] || { [ "$DO_DS" = 0 ] && [ "$DO_DIFY" = 0 ]; }; then
    cmd_check
  fi
  if [ "$DO_DS" = 1 ]; then
    k=$(read_new DEEPSEEK_API_KEY)
    valid_val "$k" || die "没有拿到可用的 DEEPSEEK_API_KEY（--keys-file 里需有 DEEPSEEK_API_KEY=…）"
    rotate_deepseek "$k"
  fi
  if [ "$DO_DIFY" = 1 ]; then
    k=$(read_new DIFY_API_KEY)
    valid_val "$k" || die "没有拿到可用的 DIFY_API_KEY（--keys-file 里需有 DIFY_API_KEY=…）"
    rotate_dify "$k"
  fi
  if [ "$DO_DS" = 1 ] || [ "$DO_DIFY" = 1 ]; then
    restart_daemon
    log ""
    log "=== 收尾清单 ==="
    log "  1) 回控制台删除旧 key：DeepSeek → platform.deepseek.com/api_keys；"
    log "     Dify → http://localhost/ → 知识库 → 数据集 → API → 删除旧密钥"
    log "  2) 复核无残留：bash deploy/rotate_keys.sh --check"
    log "  3) 备份文件确认无用后删除：rm -f <file>$BAK_SUFFIX"
  fi
}

main "$@"
