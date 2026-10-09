#!/bin/sh
# sudo sh install.sh      - MPD + web panel on Debian/Raspberry Pi OS
set -e
[ "$(id -u)" = 0 ] || { echo "run with sudo"; exit 1; }
USER_NAME=${SUDO_USER:-player}
DIR=$(cd "$(dirname "$0")" && pwd)

apt-get update -q
DEBIAN_FRONTEND=noninteractive apt-get install -y -q --no-install-recommends \
    mpd python3-requests alsa-utils curl

# --- MPD: minimal config. Change the device to your card (see: aplay -l)
cp -n /etc/mpd.conf /etc/mpd.conf.bak 2>/dev/null || true
cat > /etc/mpd.conf <<'M'
music_directory    "/var/lib/mpd/music"
playlist_directory "/var/lib/mpd/playlists"
db_file            "/var/lib/mpd/tag_cache"
state_file         "/var/lib/mpd/state"
sticker_file       "/var/lib/mpd/sticker.sql"
user               "mpd"
bind_to_address    "localhost"
bind_to_address    "/run/mpd/socket"
local_permissions  "read,add,control,admin"
port               "6600"
filesystem_charset "UTF-8"
zeroconf_enabled   "no"

input {
    plugin "curl"
}

audio_output {
    type        "alsa"
    name        "DAC"
    device      "plughw:CARD=U192k,DEV=0"   # by NAME, never by card number
    mixer_type  "software"
    buffer_time "1000000"
}
M
mkdir -p /etc/systemd/system/mpd.service.d
printf '[Service]\nRuntimeDirectory=mpd\nRuntimeDirectoryMode=0755\nNice=-15\n' \
    > /etc/systemd/system/mpd.service.d/ymusic.conf

# --- web panel
sed "s#/home/player/ymusic#$DIR#; s#User=player#User=$USER_NAME#" \
    "$DIR/ymusic-web.service" > /etc/systemd/system/ymusic-web.service
systemctl daemon-reload
systemctl enable --now mpd.service ymusic-web.service
echo "panel: http://$(hostname -I | awk '{print $1}'):8080"
