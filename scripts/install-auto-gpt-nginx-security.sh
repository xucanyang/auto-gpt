#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SOURCE_DIR="${ROOT_DIR}/ops/nginx"
BACKUP_BASE="/root/auto-gpt-nginx-backups"
STAMP="$(date +%Y%m%dT%H%M%S%z)"
BACKUP_DIR="${BACKUP_BASE}/${STAMP}"
LOCK_FILE="/run/lock/auto-gpt-nginx-security.lock"
LEGACY_COMBINED_CONF="/etc/nginx/conf.d/cccy-apps.conf"
ACTIVE_MAIN_CONF="/etc/nginx/conf.d/01-auto-gpt.cccy.me.conf"
APPLIED=0

[[ "${EUID}" -eq 0 ]] || { echo "must run as root" >&2; exit 1; }
[[ -d "${SOURCE_DIR}" ]] || { echo "missing nginx source directory: ${SOURCE_DIR}" >&2; exit 1; }

exec 9>"${LOCK_FILE}"
flock -n 9 || { echo "another nginx security install is running" >&2; exit 1; }

FILES=(
  "00-auto-gpt-security.conf|/etc/nginx/conf.d/00-auto-gpt-security.conf"
  "snippets/auto-gpt-proxy-common.conf|/etc/nginx/snippets/auto-gpt-proxy-common.conf"
  "snippets/auto-gpt-cloudflare-realip.conf|/etc/nginx/snippets/auto-gpt-cloudflare-realip.conf"
  "vhosts/auto-gpt.cccy.me.conf|${ACTIVE_MAIN_CONF}"
  "vhosts/auto-plus.cccy.me.conf|/etc/nginx/conf.d/auto-plus.cccy.me.conf"
  "vhosts/auto-plus2.cccy.me.conf|/etc/nginx/conf.d/auto-plus2.cccy.me.conf"
)
STATE_PATHS=("${LEGACY_COMBINED_CONF}")

backup_path() {
  local path="$1"
  mkdir -p "${BACKUP_DIR}$(dirname "${path}")"
  if [[ -e "${path}" ]]; then
    cp -a "${path}" "${BACKUP_DIR}${path}"
  else
    : > "${BACKUP_DIR}${path}.absent"
  fi
}

restore_path() {
  local path="$1" backup="${BACKUP_DIR}${1}"
  if [[ -e "${backup}" ]]; then
    mkdir -p "$(dirname "${path}")"
    cp -a "${backup}" "${path}"
  elif [[ -e "${backup}.absent" ]]; then
    rm -f "${path}"
  fi
}

strip_legacy_auto_gpt_vhost() {
  local path="$1"
  [[ -f "${path}" ]] || return 0
  python3 - "${path}" <<'PY'
from __future__ import annotations

import os
import re
import stat
import sys
import tempfile
from pathlib import Path

path = Path(sys.argv[1])
source = path.read_text(encoding="utf-8")
lines = source.splitlines(keepends=True)
result: list[str] = []
index = 0
changed = False
server_start = re.compile(r"^\s*server\s*\{")
server_name = re.compile(r"^(?P<indent>\s*)server_name\s+(?P<names>[^;]+);(?P<tail>.*)$")

while index < len(lines):
    if not server_start.match(lines[index]):
        result.append(lines[index])
        index += 1
        continue

    block: list[str] = []
    depth = 0
    while index < len(lines):
        line = lines[index]
        block.append(line)
        code = line.split("#", 1)[0]
        depth += code.count("{") - code.count("}")
        index += 1
        if depth == 0:
            break

    rewritten: list[str] = []
    contains_target = False
    remaining_names: list[str] = []
    for line in block:
        ending = "\n" if line.endswith("\n") else ""
        body = line[:-1] if ending else line
        match = server_name.match(body)
        if match is None:
            rewritten.append(line)
            continue
        names = match.group("names").split()
        if "auto-gpt.cccy.me" not in names:
            rewritten.append(line)
            remaining_names.extend(names)
            continue
        contains_target = True
        kept = [name for name in names if name != "auto-gpt.cccy.me"]
        remaining_names.extend(kept)
        if kept:
            rewritten.append(
                f"{match.group('indent')}server_name {' '.join(kept)};{match.group('tail')}{ending}"
            )
        changed = True

    if contains_target and not remaining_names:
        changed = True
        continue
    result.extend(rewritten)

if not changed:
    raise SystemExit(0)

payload = "".join(result)
original = path.stat()
fd, raw_temp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
temp = Path(raw_temp)
try:
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temp, stat.S_IMODE(original.st_mode))
    os.chown(temp, original.st_uid, original.st_gid)
    os.replace(temp, path)
finally:
    temp.unlink(missing_ok=True)
PY
}

assert_active_main_vhost() {
  local rendered count
  rendered="$(nginx -T 2>&1)"
  grep -Fq "# configuration file ${ACTIVE_MAIN_CONF}:" <<<"${rendered}" || {
    echo "active auto-gpt vhost is not loaded from ${ACTIVE_MAIN_CONF}" >&2
    return 1
  }
  count="$(grep -Ec '^[[:space:]]*server_name[[:space:]]+auto-gpt\.cccy\.me;' <<<"${rendered}")"
  [[ "${count}" -eq 2 ]] || {
    echo "expected exactly two active auto-gpt.cccy.me server blocks, found ${count}" >&2
    return 1
  }
}

mkdir -p "${BACKUP_DIR}"
chmod 700 "${BACKUP_DIR}"

for entry in "${FILES[@]}"; do
  src_rel="${entry%%|*}"
  dest="${entry#*|}"
  src="${SOURCE_DIR}/${src_rel}"
  [[ -f "${src}" ]] || { echo "missing source file: ${src}" >&2; exit 1; }
  backup_path "${dest}"
done
for path in "${STATE_PATHS[@]}"; do
  backup_path "${path}"
done

rollback() {
  local entry dest path
  trap - ERR
  for entry in "${FILES[@]}"; do
    dest="${entry#*|}"
    restore_path "${dest}"
  done
  for path in "${STATE_PATHS[@]}"; do
    restore_path "${path}"
  done
  nginx -t >/dev/null 2>&1 || true
  systemctl reload nginx >/dev/null 2>&1 || true
  APPLIED=0
}

on_error() {
  status=$?
  if [[ "${APPLIED}" -eq 1 ]]; then
    rollback
  fi
  echo "nginx security install failed; restored backup ${BACKUP_DIR}" >&2
  exit "${status}"
}
trap on_error ERR

# Backups are complete. From this point any partial write must trigger a full rollback.
APPLIED=1
for entry in "${FILES[@]}"; do
  src_rel="${entry%%|*}"
  dest="${entry#*|}"
  install -D -m 0644 "${SOURCE_DIR}/${src_rel}" "${dest}"
done

# cccy-apps.conf historically bundled auto-gpt with unrelated sites. Move only
# auto-gpt to the dedicated hardened vhost while preserving every other server.
strip_legacy_auto_gpt_vhost "${LEGACY_COMBINED_CONF}"

nginx -t
assert_active_main_vhost
systemctl reload nginx
APPLIED=0
trap - ERR

printf 'nginx security config installed; backup=%s\n' "${BACKUP_DIR}"
