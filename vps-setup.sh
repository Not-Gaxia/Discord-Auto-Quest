#!/usr/bin/env bash
# First-time VPS setup (Ubuntu 22.04/24.04, ทดสอบกับ Oracle ARM แล้ว)
# รันครั้งเดียว:  curl -fsSL <raw-url> -o vps-setup.sh && bash vps-setup.sh
# หรือ:           git clone เองแล้วรันไฟล์นี้
set -euo pipefail

echo "==> [1/4] packages พื้นฐาน"
sudo apt-get update -qq
sudo apt-get install -y -qq git ca-certificates curl gnupg

echo "==> [2/4] Docker (official repo)"
if ! command -v docker >/dev/null 2>&1; then
  sudo install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
    | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
  sudo chmod a+r /etc/apt/keyrings/docker.gpg
  # shellcheck disable=SC1091
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
  https://download.docker.com/linux/ubuntu \
  $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
    | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
  sudo apt-get update -qq
  sudo apt-get install -y -qq docker-ce docker-ce-cli containerd.io \
    docker-buildx-plugin docker-compose-plugin
fi
sudo usermod -aG docker "$USER" 2>/dev/null || true

echo "==> [3/4] clone repo"
if [ ! -d Discord-Auto-Quest ]; then
  git clone https://github.com/Gaxia-XP/Discord-Auto-Quest
fi
cd Discord-Auto-Quest

echo "==> [4/4] env template"
if [ ! -f .env ]; then
  cp .env.example .env
  echo "สร้าง .env แล้ว — ต้องแก้ก่อนรัน (ดูข้างล่าง)"
else
  echo ".env มีอยู่แล้ว — ข้าม"
fi

echo ""
echo "--- ขั้นต่อไป (ทำเอง 2 นาที) ---"
echo "1. nano .env  → ใส่ DISCORD_BOT_TOKEN / OWNER_ID / ENCRYPTION_KEY"
echo "   (ย้ายจาก Railway = ก๊อป ENCRYPTION_KEY ค่าเดิมมาด้วย ไม่งั้น token เก่าอ่านไม่ได้)"
echo "2. docker compose up -d --build"
echo "   (ถ้าเพิ่งถูก add เข้า group docker ให้ logout/login ก่อน หรือใช้ sudo)"
echo "3. docker compose logs -f bot  →  เห็น 'database connected' = ใช้ได้"
echo "4. ใน Discord: /setup_panel"
