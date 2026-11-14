#!/bin/sh
# Copies the converted projects into examples/ (run it after regenerating them):
#   examples/update.sh [OPENQUEST_PROJECT] [ROAD_PROJECT]
# Defaults: ../openquest-escoria and ../road-escoria, next to this repository.
# Leaves out what Godot or the editor make locally (.godot/, build/, nbproject/).
set -e
here=$(cd "$(dirname "$0")" && pwd)
oq=${1:-$here/../../openquest-escoria}
road=${2:-$here/../../road-escoria}
for pair in "$oq:openquest-escoria" "$road:road-escoria"; do
  src=${pair%%:*}
  dst=$here/${pair##*:}
  if [ ! -f "$src/project.godot" ]; then
    echo "no Godot project in $src" >&2
    exit 1
  fi
  rsync -a --delete --exclude .godot/ --exclude build/ --exclude nbproject/ "$src/" "$dst/"
  echo "$dst: $(du -sh "$dst" | cut -f1)"
done
