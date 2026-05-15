#!/usr/bin/env bash
set -euo pipefail

if [ "${USE_STORAGE_SYMLINKS:-0}" = "1" ]; then
  storage_root="${STORAGE_ROOT:-/app/storage}"
  mkdir -p "$storage_root"

  for dir in data runs artifacts screenshots public_outreach config backups; do
    target="$storage_root/$dir"
    link="/app/$dir"
    mkdir -p "$target"

    if [ -L "$link" ]; then
      continue
    fi

    if [ -d "$link" ]; then
      if [ "$(find "$link" -mindepth 1 -maxdepth 1 | head -n 1)" ]; then
        cp -an "$link/." "$target/"
      fi
      rm -rf "$link"
    fi

    ln -s "$target" "$link"
  done
fi

exec "$@"
