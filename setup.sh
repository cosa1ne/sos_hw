#!/bin/bash
set -e

# ==============================
# 환경 변수 자동 설정
# ==============================
USER_NAME="$(whoami)"
APP_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="$APP_DIR/sos"               # ← venv 폴더 이름 sos로 고정!
PYTHON_BIN="python3"
VENV_PATH="$VENV_DIR/bin/uvicorn"
MAIN_FILE="main2:app"
NGROK_BIN="/usr/local/bin/ngrok"
NGROK_CONFIG="$HOME/.config/ngrok/ngrok.yml"

# ==============================
# 1. Python venv(sos) 생성 및 패키지 설치
# ==============================
if [ ! -d "$VENV_DIR" ]; then
  echo "🐍 Python venv(sos) 생성 중..."
  $PYTHON_BIN -m venv "$VENV_DIR"
fi

echo "🐍 venv(sos) 활성화 및 패키지 설치..."
source "$VENV_DIR/bin/activate"
pip install --upgrade pip

if [ -f "$APP_DIR/requirements.txt" ]; then
  pip install -r "$APP_DIR/requirements.txt"
else
  echo "⚠️ requirements.txt가 없습니다. 필요한 패키지를 직접 설치하세요."
fi

deactivate

# ==============================
# 2. ngrok 설치 및 토큰/설정
# ==============================
if ! command -v ngrok &> /dev/null; then
  echo "🚇 ngrok 설치"
  ARCH="$(uname -m)"
  case "$ARCH" in
    aarch64|arm64)  NGROK_TGZ="ngrok-v3-stable-linux-arm64.tgz" ;;
    x86_64|amd64)   NGROK_TGZ="ngrok-v3-stable-linux-amd64.tgz" ;;
    *) echo "❌ 지원하지 않는 아키텍처: $ARCH"; exit 1 ;;
  esac
  NGROK_URL="https://bin.equinox.io/c/bNyj1mQVY4c/${NGROK_TGZ}"
  wget -q "$NGROK_URL"
  tar -xzf "$NGROK_TGZ"
  sudo mv ngrok /usr/local/bin
  rm "$NGROK_TGZ"
fi

if [ ! -f "$NGROK_CONFIG" ]; then
  echo "🔐 ngrok 토큰을 입력하세요 (공개하지 마세요!)"
  read -p "ngrok 토큰: " NGROK_TOKEN
  mkdir -p "$(dirname "$NGROK_CONFIG")"
  ngrok config add-authtoken "$NGROK_TOKEN"
  cat <<EOF > "$NGROK_CONFIG"
version: "3"
agent:
  authtoken: $NGROK_TOKEN
  
tunnels:
  my-fastapi:
    proto: http
    addr: 8000
    subdomain: scent-of-sound
EOF
fi

# ==============================
# 3. systemd 서비스 등록(FastAPI)
# ==============================
echo "🛠️ FastAPI systemd 서비스 작성 중..."
sudo tee /etc/systemd/system/fastapi.service > /dev/null <<EOF
[Unit]
Description=FastAPI App
After=network.target

[Service]
User=$USER_NAME
WorkingDirectory=$APP_DIR
ExecStart=$VENV_PATH $MAIN_FILE --host 0.0.0.0 --port 8000
Restart=always
Environment="PATH=$VENV_DIR/bin:/usr/bin:/bin"

[Install]
WantedBy=multi-user.target
EOF

# ==============================
# 4. systemd 서비스 등록(ngrok)
# ==============================
echo "🛠️ ngrok systemd 서비스 작성 중..."
sudo tee /etc/systemd/system/ngrok.service > /dev/null <<EOF
[Unit]
Description=ngrok tunnel for FastAPI
After=network.target fastapi.service

[Service]
User=$USER_NAME
ExecStart=$NGROK_BIN start --all --config $NGROK_CONFIG
Restart=on-failure

[Install]
WantedBy=multi-user.target
EOF

# ==============================
# 5. 서비스 등록/실행
# ==============================
sudo systemctl daemon-reexec
sudo systemctl daemon-reload
sudo systemctl enable fastapi.service
sudo systemctl enable ngrok.service
sudo systemctl restart fastapi.service
sleep 3
sudo systemctl restart ngrok.service

echo "✅ 모든 서비스가 정상적으로 설정 및 실행되었습니다!"
echo "🌏 ngrok 공개 URL은 'sudo journalctl -u ngrok -f' 또는 ngrok 대시보드에서 확인하세요."
