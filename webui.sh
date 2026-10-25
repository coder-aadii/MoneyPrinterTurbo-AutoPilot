#!/usr/bin/env sh

# If you could not download the model from the official site, you can use the mirror site.
# Just remove the comment of the following line .
# 如果你无法从官方网站下载模型，你可以使用镜像网站。
# 只需要移除下面一行的注释即可。

# export HF_ENDPOINT=https://hf-mirror.com

CURRENT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
export PYTHONPATH="$CURRENT_DIR${PYTHONPATH:+:$PYTHONPATH}"

# 0.0.0.0 只能表示“监听所有网卡”，不适合作为浏览器访问地址。
# macOS/Linux 下浏览器打开 http://0.0.0.0:8501 可能会经过代理或网关，
# 最终出现 502。默认绑定并打开 127.0.0.1，与 Windows 启动脚本保持一致。
MPT_WEBUI_HOST="${MPT_WEBUI_HOST:-127.0.0.1}"
MPT_WEBUI_PORT="${MPT_WEBUI_PORT:-8501}"

if [ -x "$CURRENT_DIR/.venv/bin/python" ]; then
  set -- "$CURRENT_DIR/.venv/bin/python" -m streamlit
elif command -v uv >/dev/null 2>&1; then
  set -- uv run streamlit
elif command -v streamlit >/dev/null 2>&1; then
  echo "***** Warning: using streamlit from PATH. If dependencies fail, run 'uv sync --frozen' first. *****"
  set -- streamlit
else
  echo "***** Neither project Python, uv, nor streamlit was found. Please install dependencies first. *****"
  exit 1
fi

port_available() {
  WEBUI_HOST="$MPT_WEBUI_HOST" WEBUI_PORT="$MPT_WEBUI_PORT" python3 - <<'PY' 2>/dev/null
import os
import socket
import sys

host = os.environ.get("WEBUI_HOST", "127.0.0.1")
port = int(os.environ.get("WEBUI_PORT", "8501"))
with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
    try:
        sock.bind((host, port))
    except OSError:
        sys.exit(1)
sys.exit(0)
PY
}

# Use the requested port exactly. Silently choosing 8502 can leave an older MPT
# instance serving the familiar 8501 URL while the updated app runs elsewhere.
if ! port_available; then
  echo "***** WebUI port $MPT_WEBUI_PORT is already in use on $MPT_WEBUI_HOST. *****"
  echo "***** Stop the older MPT terminal with Ctrl+C, then run ./start.sh again. *****"
  exit 1
fi

echo "***** WebUI address: http://$MPT_WEBUI_HOST:$MPT_WEBUI_PORT *****"
exec "$@" run "$CURRENT_DIR/webui/Main.py" \
  --server.address="$MPT_WEBUI_HOST" \
  --server.port="$MPT_WEBUI_PORT" \
  --browser.serverAddress="$MPT_WEBUI_HOST" \
  --browser.gatherUsageStats=False \
  --client.toolbarMode=minimal \
  --logger.hideWelcomeMessage=True \
  --server.showEmailPrompt=False \
  --server.enableCORS=True
