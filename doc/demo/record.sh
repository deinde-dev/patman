#!/bin/bash
# SPDX-License-Identifier: GPL-2.0+
#
# Record a short video of patman in a terminal, for the docs and website
#
# Usage: record.sh [U-BOOT-TREE [OUTPUT.webm]]
#
# U-BOOT-TREE is a U-Boot tree (default ~/u) with branches 'binj-us' and
# 'align-fix', which track 'us/master'. A scratch clone of it is made, so the
# tree and its database are not changed, and patman runs from this checkout.
# Nothing is sent: the demo uses 'patman send -n' and reads from patchwork.
#
# This needs Xvfb, xterm, xwininfo, tmux and ffmpeg. It writes OUTPUT.webm,
# with an MP4 copy and a poster image beside it.

set -e
HERE=$(dirname "$(readlink -f "$0")")
PATMAN_DIR=$(readlink -f "$HERE/../..")
TREE=$(readlink -f "${1:-$HOME/u}")
OUT=$(readlink -f "${2:-$PWD/patman-demo.webm}")
DISP=:99
W=1280
H=720
# Terminal size in characters. With a 13-point font each character is about
# 11x22 pixels, so with the 12-pixel border this fills the screen. Any more
# and the bottom rows are outside the video
COLS=114
ROWS=31
# Terminal background colour
BG='#1d1f21'
SESS=pdemo
# Longest pause, in seconds, to show while waiting for a command's output
MAX_PAUSE=1
# Seconds between lines of 'patman send' output
LINE_DELAY=0.1

WORK=$(mktemp -d)
cleanup() {
	[ -n "$FF" ] && kill -INT $FF 2>/dev/null && wait $FF 2>/dev/null || true
	tmux -L pdemo kill-session -t $SESS 2>/dev/null || true
	[ -n "$XT" ] && kill $XT 2>/dev/null || true
	[ -n "$XV" ] && kill $XV 2>/dev/null || true
}
trap 'cleanup; [ -d "$WORK" ] && rm -rf "$WORK"' EXIT

# A scratch clone with the two demo branches, tracking us/master
git clone -q --shared --no-checkout "$TREE" "$WORK/u"
cd "$WORK/u"
git remote rename origin local
git remote add us "$(git -C "$TREE" remote get-url us)"
git fetch -q "$TREE" refs/remotes/us/master:refs/remotes/us/master
for branch in binj-us align-fix; do
	git branch -q "$branch" "local/$branch"
	git branch -q -u us/master "$branch"
done
git remote remove local
git checkout -q binj-us
printf '[settings]\npatchwork_url: https://patchwork.ozlabs.org\n' > .patman

# Run patman from this checkout, under its usual name. The output of
# 'patman send' is longer than the screen, so show it a line at a time, so
# that it can be read as it scrolls past. Run it under script(1), so that
# it still sees a terminal and uses colour
mkdir "$WORK/bin"
cat > "$WORK/launch.py" <<EOF
import sys
from patman.__main__ import run_patman

# Name the command as when patman is installed, rather than giving the path
# to this file, e.g. in the --cc-cmd which 'patman send' shows
sys.argv[0] = 'patman'
run_patman()
EOF
cat > "$WORK/bin/patman" <<EOF
#!/bin/bash
export PYTHONPATH="$PATMAN_DIR"
if [ "\$1" = send ] && [ -t 1 ]; then
	script -qfec "python3 $WORK/launch.py \$*" /dev/null |
		while IFS= read -r line; do
			printf '%s\n' "\$line"
			sleep $LINE_DELAY
		done
	exit \${PIPESTATUS[0]}
fi
exec python3 "$WORK/launch.py" "\$@"
EOF
chmod +x "$WORK/bin/patman"
export PATH="$WORK/bin:$PATH"
patman patchwork set-project U-Boot us >/dev/null
patman upstream add us "$(git remote get-url us)" >/dev/null

Xvfb $DISP -screen 0 ${W}x${H}x24 -nolisten tcp &
XV=$!
sleep 1

tmux -L pdemo -f /dev/null new-session -d -s $SESS -x $COLS -y $ROWS \
	"env GIT_PAGER=cat PATH=$PATH PS1='\[\e[1;32m\]u-boot\[\e[0m\] (\[\e[33m\]\$(git branch --show-current)\[\e[0m\]) \$ ' bash --norc --noprofile"
tmux -L pdemo set -t $SESS status off
tmux -L pdemo send-keys -t $SESS "cd $WORK/u; clear" Enter
sleep 0.5

DISPLAY=$DISP xterm -fa 'DejaVu Sans Mono' -fs 13 \
	-geometry ${COLS}x${ROWS}+0+0 -bg "$BG" -fg '#e0e0e0' -bc -b 12 \
	-xrm 'XTerm*cursorColor: #e0e0e0' -e tmux -L pdemo attach -t $SESS &
XT=$!
sleep 1.5

# The terminal's window is a little smaller than the screen. Show just the
# window, centred on its own background colour, rounding its size down to
# even numbers as the video encoders need
TSIZE=$(DISPLAY=$DISP xwininfo -root -children |
	awk '/"XTerm"/ { sub(/\+.*/, "", $(NF - 1)); print $(NF - 1) }')
TW=$((${TSIZE%x*} / 2 * 2))
TH=$((${TSIZE#*x} / 2 * 2))
FRAME="crop=$TW:$TH:0:0,pad=$W:$H:(ow-iw)/2:(oh-ih)/2:color=0x${BG#\#}"

RAW=$WORK/raw.mkv
ffmpeg -loglevel error -y -f x11grab -draw_mouse 0 -framerate 25 \
	-video_size ${W}x${H} -i $DISP -c:v libx264rgb -crf 0 -preset ultrafast \
	$RAW &
FF=$!
START=$(date +%s.%N)
sleep 1

# Time since the recording started, in seconds
now() {
	awk "BEGIN { print $(date +%s.%N) - $START }"
}

# Parts of the recording to cut out, as an expression for ffmpeg's select
CUTS=0

# Check whether the shell is back at its prompt, i.e. its own process group
# has the terminal again
idle() {
	local pid
	pid=$(tmux -L pdemo display -p -t $SESS '#{pane_pid}')
	[ "$(ps -o tpgid= -p "$pid" | tr -d ' ')" = "$pid" ]
}

# Type a command a character at a time, run it, wait for it to finish, then
# leave its output on the screen for a while. A command can take longer than
# usual, e.g. when patchwork is slow, so wait until the shell is back at its
# prompt rather than for a fixed time. While the screen does not change for
# more than MAX_PAUSE seconds, cut the recording, so that the video does not
# depend on how fast the network is
run() {
	local cmd=$1 wait=${2:-2}
	local i t prev screen last= changed cut=
	for ((i = 0; i < ${#cmd}; i++)); do
		tmux -L pdemo send-keys -t $SESS -l "${cmd:i:1}"
		sleep 0.03
	done
	sleep 0.4
	tmux -L pdemo send-keys -t $SESS Enter
	changed=$(now)
	prev=$changed
	until idle; do
		t=$(now)
		screen=$(tmux -L pdemo capture-pane -p -t $SESS | md5sum)
		if [ "$screen" != "$last" ]; then
			# The screen changed after $prev, so cut up to there
			[ -n "$cut" ] && CUTS+="+between(t,$cut,$prev)"
			cut=
			last=$screen
			changed=$t
		elif [ -z "$cut" ] &&
			awk "BEGIN { exit !($t - $changed > $MAX_PAUSE) }"; then
			cut=$t
		fi
		prev=$t
		sleep 0.1
	done
	[ -n "$cut" ] && CUTS+="+between(t,$cut,$prev)"
	sleep "$wait"
}

# Show a comment, as the viewer's guide to what comes next
say() {
	run "# $1" 0.8
}

say "A branch with three commits, ready to send"
run "git log --oneline us/master.." 1.5
say "patman reads its settings from tags in the commits"
run "git log -1 | tail -13" 3.5
run "clear" 0.3
say "Check the series and see who it goes to, without sending"
run "patman send -n" 3.5
run "clear" 0.3
say "After review, see the new tags on patchwork"
run "git checkout -q align-fix" 0.5
run "patman status" 3
say "Collect them into a new branch, ready for v2"
run "patman status -d align-fix2" 1
# Take the poster image here, with the new branch just created
POSTER=$(now)
sleep 3
run "clear" 0.3
say "Or let patman track all your series"
run "patman series add -M" 1.5
run "patman series ls" 3.5

cleanup
FF=
trap - EXIT
VF="select='not($CUTS)',setpts=N/FRAME_RATE/TB,$FRAME"
ffmpeg -loglevel error -y -i $RAW -vf "$VF" -c:v libvpx-vp9 -b:v 0 -crf 30 \
	-row-mt 1 -pix_fmt yuv420p "$OUT"
# The slowest preset, tuned for flat images, makes the MP4 about a third
# smaller for the same quality
ffmpeg -loglevel error -y -i $RAW -vf "$VF" -c:v libx264 -crf 26 \
	-preset veryslow -tune animation -pix_fmt yuv420p -movflags +faststart \
	"${OUT%.webm}.mp4"
ffmpeg -loglevel error -y -ss "$POSTER" -i $RAW -vf "$FRAME" -frames:v 1 \
	"${OUT%.webm}.png"
echo "Cut: $CUTS"
echo "Wrote $OUT, ${OUT%.webm}.mp4 and ${OUT%.webm}.png"
