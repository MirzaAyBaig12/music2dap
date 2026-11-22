#!/usr/bin/env bash
# Install the external tools the GUI uses (yt-dlp, ffmpeg, ffprobe).
# Windows users do not need this: Music2DAP-setup.exe bundles them.
set -euo pipefail

need() { command -v "$1" >/dev/null 2>&1; }

if [[ -f /etc/NIXOS ]]; then
  cat <<'EOF'
NixOS detected. Add the tools to your system/home config (recommended):

  environment.systemPackages = with pkgs; [ yt-dlp ffmpeg ];

or for a one-off shell:

  nix shell nixpkgs#yt-dlp nixpkgs#ffmpeg -c music2dap

The Nix CLI package already pulls these in via its wrapper.
EOF
  exit 0
fi

if ! need sudo; then
  echo "sudo is required to install packages." >&2
  exit 1
fi

if need apt-get; then
  sudo apt-get update
  sudo apt-get install -y ffmpeg yt-dlp
elif need dnf; then
  sudo dnf install -y ffmpeg yt-dlp
elif need pacman; then
  sudo pacman -S --needed --noconfirm ffmpeg yt-dlp
elif need brew; then
  brew install ffmpeg yt-dlp
else
  echo "Unsupported package manager. Install ffmpeg and yt-dlp manually." >&2
  exit 1
fi

echo
echo "Installed:"
command -v yt-dlp || true
command -v ffmpeg || true
command -v ffprobe || true
echo
echo "In Music2DAP open Settings (gear icon) and use 'Check tools'."
