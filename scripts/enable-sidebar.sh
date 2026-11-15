#!/bin/sh
set -eu

dir=$(cd "$(dirname "$0")/.." && pwd)
cfg="$HOME/.config/herdr/config.toml"

if grep -q -E '^\[ui(\.sidebar\.spaces|\.sidebar\.agents\.rows_by_agent)?\]' "$cfg"; then
  echo "$cfg already has a [ui] or sidebar rows table; merge $dir/sidebar.toml by hand."
  exit 1
fi

backup="$cfg.bak-$(date +%Y-%m-%d-%H%M%S)"
cp "$cfg" "$backup"
cat "$dir/sidebar.toml" >> "$cfg"
if ! herdr config check; then
  cp "$backup" "$cfg"
  echo "config check failed; restored $backup"
  exit 1
fi
herdr server reload-config
echo "sidebar rows enabled; backup at $backup"
