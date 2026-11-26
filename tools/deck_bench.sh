#!/bin/sh
# Steam Deck benchmark: a scripted Quick Race (Silverstone by default) run on the device itself, the AI driving the
# player's car, then a summary of the perf/cpu lines from the race (grid and driving). Runs through
# `flatpak-spawn --host` when started from inside a Flatpak (VS Code), so the game gets the host's GPU and display.
#
# usage: tools/deck_bench.sh <run name> [VAR=value ...]
#   EXE=<path to f1ce>    (default build-deck/f1ce, else the AppImage's extracted usr/bin/f1ce in $APPDIR)
#   GAME=<PS3_GAME dir>   (default: F1CE_GAME_DIR from ~/.config/f1ce-recomp/settings.conf or the launcher ini)
#   SECS=<s>              seconds of race after the 3D scene appears (default 60)
#   SAVE=<ps3_data dir>   save data with a profile (default ~/.local/share/f1ce-recomp/ps3_data, else dbg/menu)
#   PERF=1                also record a host `perf` profile of the race (perf.data in the run dir)
# The run directory is dbg/deck_<name>/ (log.txt, summary.txt). Shader cache: a copy of the user's in
# dbg/deck_cache (so runs start warm and never touch the user's own cache).
cd "$(dirname "$0")/.." || exit 1
R=$(pwd)
name="$1"; shift
[ -n "$name" ] || { echo "usage: $0 <run name> [VAR=value ...]"; exit 1; }

if [ -z "$GAME" ]; then
    GAME=$(sed -n 's/^F1CE_GAME_DIR=//p' "$HOME/.config/f1ce-recomp/settings.conf" 2>/dev/null | head -1)
    [ -n "$GAME" ] || GAME=$(sed -n 's/^path=//p' "$HOME/.local/share/f1ce-recomp/f1ce_launcher.ini" 2>/dev/null | head -1)
fi
[ -d "$GAME" ] || { echo "game folder not found: '$GAME' (set GAME=)"; exit 1; }
if [ -z "$EXE" ]; then
    if [ -x build-deck/f1ce ]; then EXE="$R/build-deck/f1ce"
    elif [ -n "$APPDIR" ] && [ -x "$APPDIR/usr/bin/f1ce" ]; then EXE="$APPDIR/usr/bin/f1ce"
    else echo "no executable: build build-deck/ or set EXE= / APPDIR="; exit 1; fi
fi
SAVE="${SAVE:-$HOME/.local/share/f1ce-recomp/ps3_data}"
[ -d "$SAVE" ] || SAVE="$R/dbg/menu/ps3_data"

mkdir -p dbg/deck_cache
[ -d dbg/deck_cache/vk ] || cp -r "$HOME/.cache/f1ce-recomp/shader_cache/." dbg/deck_cache/ 2>/dev/null

d="$R/dbg/deck_$name"; rm -rf "$d"; mkdir -p "$d"; cp -r "$SAVE" "$d/ps3_data"
# Press Start at 9 s, then cross every 4 s through Single Player -> Quick Race -> race (the AI drives once it starts)
IN="9:start"; for t in $(seq 13 4 137); do IN="$IN,$t:cross"; done

HOST=""; [ -f /.flatpak-info ] && HOST="flatpak-spawn --host"
PERFCMD=""; [ -n "$PERF" ] && PERFCMD="perf record -F 499 -g --call-graph=fp -o $d/perf.data --"

cat > "$d/run.sh" <<EOF
#!/bin/sh
cd "$d" || exit 1
export SDL_AUDIO_DRIVER=dummy F1CE_CACHE_DIR="$R/dbg/deck_cache" F1CE_SKIP_INTRO=1 F1CE_LANGUAGE=english \
  F1CE_RENDERER=vulkan F1CE_SCALE=1280x800 F1CE_FULLSCREEN=0 F1CE_THREAD_CPU=1 F1CE_AI_DRIVE=0 F1CE_SAVE_UI=0 \
  F1CE_QR_TRACK=\${F1CE_QR_TRACK:-silverstone} F1CE_QR_WEATHER=\${F1CE_QR_WEATHER:-sunny} \
  F1CE_QUIT_AFTER_3D=${SECS:-60} F1CE_INPUT="$IN"
for kv in "\$@"; do export "\$kv"; done
exec timeout ${TIMEOUT:-420} $PERFCMD "$EXE" "$GAME"
EOF
chmod +x "$d/run.sh"
# sensors once a second (race = after the log's "3D scene detected"): APU power, GPU clock/busy, mean CPU MHz
GPU=$(ls -d /sys/class/drm/card*/device 2>/dev/null | head -1)
HW=$(grep -l amdgpu /sys/class/hwmon/hwmon*/name 2>/dev/null | head -1); HW=${HW%/name}
(
    race=0
    while :; do
        [ $race = 0 ] && grep -q "3D scene detected" "$d/f1ce_recomp.log" 2>/dev/null && race=1
        mhz=$(awk '/cpu MHz/{s+=$4;n++} END{printf "%.0f", s/n}' /proc/cpuinfo)
        echo "$race $(($(cat $HW/power1_average 2>/dev/null || echo 0)/1000)) $(($(cat $HW/freq1_input 2>/dev/null || echo 0)/1000000)) $(cat $GPU/gpu_busy_percent 2>/dev/null || echo 0) $mhz"
        sleep 1
    done
) > "$d/sensors.txt" &
SP=$!
$HOST "$d/run.sh" "$@" > "$d/console.txt" 2>&1
rc=$?
kill $SP 2>/dev/null
cp "$d/f1ce_recomp.log" "$d/log.txt" 2>/dev/null
# summary: perf lines after the race scene, first 15 s (grid + start) and the rest (driving)
awk -v rc=$rc '
  /3D scene detected/ { f=1; t0=NR }
  f && /perf: [0-9]/ { n++; for(k=1;k<=NF;k++){ if($k=="fps") fps=$(k-1); if($k=="vertices") v=$(k+1); if($k=="textures") tx=$(k+1); if($k=="program") pr=$(k+1); if($k=="draws") dr=$(k-1) }
      if (n<=3) { gs+=fps; gn++ } else { ds+=fps; dn++; if (min==""||fps<min) min=fps; vs+=v; ts+=tx; ps+=pr; drs+=dr } }
  f && /cpu: [0-9.]+ cores/ { m++; if (m>3) { cs+=$3; cn++ } }
  f && /thread cpu: ppu/ { last=$0 }
  END {
    printf "exit %d, perf lines %d\n", rc, n
    if (gn) printf "grid/start fps avg %.1f (first %d lines)\n", gs/gn, gn
    if (dn) printf "driving fps avg %.1f min %.1f over %d lines | renderer ms: vertices %.1f textures %.1f program %.1f | draws %.0f\n", ds/dn, min, dn, vs/dn, ts/dn, ps/dn, drs/dn
    if (cn) printf "cores used avg %.2f\n", cs/cn
    if (last!="") { sub(/.*thread cpu:/, "threads:", last); print last }
  }' "$d/log.txt" > "$d/summary.txt"
awk '$1==1 { n++; p+=$2; g+=$3; b+=$4; c+=$5 } END { if (n) printf "race sensors (%d s): APU %.1f W, GPU %.0f MHz busy %.0f%%, CPU %.0f MHz\n", n, p/n/1000, g/n, b/n, c/n }' \
    "$d/sensors.txt" >> "$d/summary.txt"
cat "$d/summary.txt"
