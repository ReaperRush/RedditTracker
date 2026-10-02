#!/usr/bin/env bash
# Install or update the Reddit tracker on a fresh Ubuntu/Debian VPS:
#
#   curl -fsSL https://raw.githubusercontent.com/ReaperRush/RedditTracker/master/deploy/setup-vps.sh | sudo bash
#
# It asks for your Telegram bot token and chat ID the first time, runs the
# tracker as a systemd service that starts on boot and restarts if it
# crashes, and sends a test message. Run the same command again to update.
#
# Non-interactive: pass TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=... to sudo.
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/ReaperRush/RedditTracker.git}"
BRANCH="${BRANCH:-master}"
APP_DIR=/opt/reddit-tracker
APP_USER=reddittracker
SERVICE=reddit-tracker
STATE_DIR=/var/lib/reddit-tracker
ENV_FILE="$APP_DIR/.env"

say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
die() { printf '\nError: %s\n' "$*" >&2; exit 1; }

# Set KEY=value in .env, replacing an existing KEY= line if there is one.
set_env() {
  python3 - "$ENV_FILE" "$1" "$2" <<'PY'
import re, sys
path, key, value = sys.argv[1:]
text = open(path).read()
line = f"{key}={value}"
text, n = re.subn(rf"^{re.escape(key)}=.*$", lambda _: line, text, flags=re.M)
if not n:
    text += f"\n{line}\n"
open(path, "w").write(text)
PY
}

ask() {  # ask VAR "Prompt": read from the terminal unless VAR is already set
  local var=$1 prompt=$2
  if [ -z "${!var:-}" ]; then
    [ -r /dev/tty ] || die "$var is not set and there's no terminal to ask on."
    # shellcheck disable=SC2229  # deliberately reads into the variable named by $var
    read -rp "$prompt: " "$var" </dev/tty
  fi
  [ -n "${!var}" ] || die "$var can't be empty."
}

main() {
  [ "$(id -u)" -eq 0 ] || die "run this as root (put sudo in front of bash)."
  command -v apt-get >/dev/null || die "this script supports Debian/Ubuntu only."

  say "Installing system packages"
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y -qq git python3 python3-venv ca-certificates >/dev/null

  id "$APP_USER" &>/dev/null ||
    useradd --system --no-create-home --home-dir "$STATE_DIR" --shell /usr/sbin/nologin "$APP_USER"

  say "Fetching the latest code"
  if [ -d "$APP_DIR/.git" ]; then
    git -C "$APP_DIR" fetch -q origin "$BRANCH"
    git -C "$APP_DIR" reset -q --hard "origin/$BRANCH"  # .env and .venv are untouched
  else
    git clone -q --branch "$BRANCH" "$REPO_URL" "$APP_DIR"
  fi

  say "Installing Python dependencies"
  [ -x "$APP_DIR/.venv/bin/python" ] || python3 -m venv "$APP_DIR/.venv"
  "$APP_DIR/.venv/bin/pip" install -q --disable-pip-version-check -r "$APP_DIR/requirements.txt"

  local first_install=false
  if [ ! -f "$ENV_FILE" ]; then
    first_install=true
    say "Configuring Telegram"
    ask TELEGRAM_BOT_TOKEN "Telegram bot token (from @BotFather)"
    ask TELEGRAM_CHAT_ID "Telegram chat ID"
    cp "$APP_DIR/.env.example" "$ENV_FILE"
    set_env WHATSAPP_PROVIDER telegram
    set_env TELEGRAM_BOT_TOKEN "$TELEGRAM_BOT_TOKEN"
    set_env TELEGRAM_CHAT_ID "$TELEGRAM_CHAT_ID"
  fi
  # Secrets live here: readable by root and the service only.
  chown "root:$APP_USER" "$ENV_FILE"
  chmod 640 "$ENV_FILE"

  if $first_install; then
    say "Sending a test message"
    (cd "$APP_DIR" && runuser -u "$APP_USER" -- "$APP_DIR/.venv/bin/python" reddit_tracker.py --test-message) ||
      { rm -f "$ENV_FILE"; die "the test message failed; check the token and chat ID, then run this again."; }
  fi

  say "Setting up the background service"
  cat >"/etc/systemd/system/$SERVICE.service" <<UNIT
[Unit]
Description=Reddit new-post tracker
After=network-online.target
Wants=network-online.target

[Service]
User=$APP_USER
WorkingDirectory=$APP_DIR
ExecStart=$APP_DIR/.venv/bin/python reddit_tracker.py
Environment=STATE_FILE=$STATE_DIR/state.json
Environment=PYTHONUNBUFFERED=1
StateDirectory=$SERVICE
Restart=always
RestartSec=10
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
UNIT
  systemctl daemon-reload
  systemctl enable -q "$SERVICE"
  systemctl restart "$SERVICE"

  say "Checking that it can reach Reddit (takes ~20s)"
  sleep 20
  journalctl -u "$SERVICE" -n 6 --no-pager -o cat || true

  cat <<DONE

Done. The tracker is running and will start automatically after reboots.

  Live logs:        journalctl -u $SERVICE -f
  Change settings:  sudo nano $ENV_FILE && sudo systemctl restart $SERVICE
  Update:           re-run the same curl command
  Stop / start:     sudo systemctl stop $SERVICE / sudo systemctl start $SERVICE
DONE
}

main "$@"  # everything is in main() so a partial download can't run half a script
