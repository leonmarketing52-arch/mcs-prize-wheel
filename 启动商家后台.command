#!/bin/zsh
set -eu
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"
cd -- "$(dirname -- "$0")"
if ! command -v python3 >/dev/null 2>&1; then
  print "未找到 Python 3，无法启动商家后台。"
  read -r "?按回车关闭。"
  exit 1
fi
if ! python3 merchant_server.py --open; then
  read -r "?启动失败。按回车关闭。"
  exit 1
fi
