#!/bin/sh
# Merge history from one or more boxes (Linux server or OpenWrt) into one file.
#
#   scripts/history-merge.sh -o OUTPUT INPUT...
#
# Each INPUT is a logs/ directory (its history-*.jsonl.gz archives and
# history.jsonl) or a single .jsonl / .jsonl.gz file. OUTPUT must not exist yet:
# nothing is overwritten, and no input is changed or deleted.
#
# Records are sorted by their "timestamp" (both backends write UTC, so text order
# is time order). Only byte-identical lines are dropped as duplicates; records
# that merely share a timestamp are all kept. Lines without a timestamp
# (e.g. a torn last line) are kept too, at the start, where readers skip them.
#
# Plain POSIX sh + awk + sort + gzip, so it also runs on a router's BusyBox. sort
# holds the data in memory or /tmp (RAM on OpenWrt): merge large histories on a
# Linux box. To use the result, stop the dashboard, keep the old history.jsonl,
# and put OUTPUT in its place.
set -eu

usage() { echo "usage: $0 -o OUTPUT INPUT..." >&2; exit 2; }

out=""
while getopts o: opt; do
    case "$opt" in
        o) out=$OPTARG ;;
        *) usage ;;
    esac
done
shift $((OPTIND - 1))
[ -n "$out" ] && [ $# -gt 0 ] || usage
if [ -e "$out" ] || [ -L "$out" ]; then
    echo "error: $out already exists; pick a new name" >&2
    exit 1
fi

# Every input file, oldest archives first (order only matters for the report).
files=""
for input in "$@"; do
    if [ -d "$input" ]; then
        found=0
        for f in "$input"/history-*.jsonl.gz "$input"/history.jsonl; do
            [ -f "$f" ] || continue
            files="$files$f
"
            found=1
        done
        [ "$found" = 1 ] || { echo "error: no history files in $input" >&2; exit 1; }
    elif [ -f "$input" ]; then
        files="$files$input
"
    else
        echo "error: $input not found" >&2
        exit 1
    fi
done

tmp="$out.partial.$$"
trap 'rm -f "$tmp"' EXIT  # only this run's unfinished output
echo "$files" | while IFS= read -r f; do
    [ -n "$f" ] || continue
    echo "reading $f" >&2
    case "$f" in
        *.gz) gzip -dc "$f" ;;
        *) cat "$f" ;;
    esac
    # A file without a trailing newline must not fuse with the next file's first line.
    echo
done | awk '
    NF == 0 { next }
    {
        ts = ""
        if (match($0, /"timestamp": *"[^"]*"/)) {
            ts = substr($0, RSTART, RLENGTH)
            sub(/^"timestamp": *"/, "", ts)
            sub(/"$/, "", ts)
        }
        print ts "\t" $0
    }' | LC_ALL=C sort -u | awk '{ print substr($0, index($0, "\t") + 1) }' > "$tmp"

read_lines=0
for f in $(echo "$files"); do
    case "$f" in
        *.gz) n=$(gzip -dc "$f" | awk 'NF { n++ } END { print n + 0 }') ;;
        *) n=$(awk 'NF { n++ } END { print n + 0 }' "$f") ;;
    esac
    read_lines=$((read_lines + n))
done
written=$(awk 'END { print NR }' "$tmp")
untimed=$(awk '!/"timestamp": *"/ { n++ } END { print n + 0 }' "$tmp")
mv "$tmp" "$out"
trap - EXIT
echo "wrote $out: $written records from $read_lines lines;" \
    "$((read_lines - written)) exact duplicates dropped, $untimed lines without a timestamp kept" >&2
