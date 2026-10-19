#!/bin/bash
# PHONE-133 build-003: the web call page in headless Chrome against the real `sessionphone web`.
# PHONE-136: plus the QR's URL, https://qr.localhost:P/#code=<code>, a second address with no
# passkey yet (Chrome resolves *.localhost to loopback itself, and treats it as localhost).
# PHONE-139: plus a scratch page on ANOTHER origin (http://127.0.0.1:Q, python's http.server on a
# spare port) that opens the call with target=_blank, and one that frames it (drive.mjs 8, 9).
# Copied from settings@19ecdee tests/browser/run.sh (https mode): a self-signed localhost cert on
# a spare port, a temp Chrome profile, a virtual authenticator (drive.mjs). Here also a fake mic
# playing a tone wav, and the enrol code from the real `sessionphone web enrol`.
#   tests/browser/run.sh          (or: make browser)
# SCRATCH ONLY: a temp root (never ~/.sessionphone), no --legs (so no socket a keeper could
# find), the server bound to 127.0.0.1, no tailscale serve, nothing to :8890/:2022/:8880.
# Needs Google Chrome, node >= 22 (global WebSocket/fetch), openssl and the repo's venv
# (make install, or uv sync). Not collected by pytest.
set -u
HERE=$(cd "$(dirname "$0")" && pwd); REPO=$(cd "$HERE/../.." && pwd)
PY=${PY:-"$REPO/.venv/bin/python"}; SP=${SP:-"$REPO/.venv/bin/sessionphone"}
CHROME=${CHROME:-"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"}
T=$(mktemp -d); R="$T/root"
spare() { "$PY" -c 'import socket;s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1])'; }
P=$(spare); C=$(spare); Q=$(spare); B=https://localhost:$P; O=http://127.0.0.1:$Q; QB=https://qr.localhost:$P
openssl req -x509 -newkey rsa:2048 -nodes -keyout "$T/key.pem" -out "$T/cert.pem" -days 1 -subj /CN=localhost \
  -addext subjectAltName=DNS:localhost,DNS:qr.localhost 2>/dev/null
# the fake mic: 440 Hz, 48 kHz mono s16, 20 s (Chrome loops it). The sandboxed audio service
# cannot read a file ("Failed to read ... as input to the fake device"), so only THAT service is
# unsandboxed (AudioServiceSandbox below), never the whole browser (--no-sandbox).
"$PY" -c 'import math,struct,sys,wave
w=wave.open(sys.argv[1],"wb"); w.setnchannels(1); w.setsampwidth(2); w.setframerate(48000)
w.writeframes(b"".join(struct.pack("<h",int(8000*math.sin(2*math.pi*440*i/48000))) for i in range(48000*20))); w.close()' "$T/tone.wav"
# the other origin: Talk's phone button (design §5.4) as a link, and a page that frames the call
mkdir "$T/other"
printf '<!doctype html><title>other app</title>\n<a id="open" href="%s/" target="_blank" rel="noopener noreferrer">Call</a>\n' \
  "$B" > "$T/other/link.html"
printf '<!doctype html><title>other app</title>\n<iframe id="framed" src="%s/"></iframe>\n' "$B" > "$T/other/frame.html"
unset SESSIONPHONE_ROOT DC_ROOT                       # nothing reaches the live root by default either
SRV=; CHR=; OTH=
# Background jobs of a script start with SIGINT ignored, so they are stopped with TERM. The trap
# also covers a drive that dies half-way: nothing of this run outlives it.
stop() { [ -n "$CHR$SRV$OTH" ] && kill $CHR $SRV $OTH 2>/dev/null; wait 2>/dev/null; CHR=; SRV=; OTH=; }
trap 'stop; if [ -n "${KEEP:-}" ]; then echo "kept $T"; else rm -rf "${T:?}"; fi' EXIT   # KEEP=1: keep the logs
PYTHONUNBUFFERED=1 "$SP" web --root "$R" --port "$P" --tls "$T/cert.pem" "$T/key.pem" --origin "$B" --origin "$QB" \
  > "$T/server.log" 2>&1 &
SRV=$!
"$PY" -m http.server --bind 127.0.0.1 --directory "$T/other" "$Q" > "$T/other.log" 2>&1 &
OTH=$!
"$CHROME" --headless=new --remote-debugging-port="$C" --user-data-dir="$T/chrome" --ignore-certificate-errors \
  --no-first-run --no-default-browser-check --use-fake-ui-for-media-stream --use-fake-device-for-media-stream \
  --use-file-for-fake-audio-capture="$T/tone.wav" --disable-features=AudioServiceSandbox \
  about:blank > "$T/chrome.log" 2>&1 &
CHR=$!
for _ in $(seq 75); do curl -s "http://127.0.0.1:$C/json/version" >/dev/null && curl -sk "$B/" >/dev/null \
  && curl -s "$O/link.html" >/dev/null && break
  sleep 0.2; done
# The code from the real CLI, minted BESIDE the running server (it replaces the one the server
# printed at start, and the server reads it from disk: design OQ4)
CODE=$("$SP" web enrol --root "$R" | awk '/^enrol code:/{print $3}')
[ -n "$CODE" ] || { echo "FAIL: sessionphone web enrol printed no code"; exit 1; }
# step 10 (PHONE-136) mints its own fresh code with the same CLI: step 3 spends this one
node "$HERE/drive.mjs" "$C" "$B" "$CODE" "$O" "$QB" "$T/server.log" "$SP" web --port "$P" enrol --root "$R"; RC=$?
echo "--- server log"; grep -E '"call"|"conn"|"auth"|calls:' "$T/server.log"
ok() { if eval "$2"; then echo "PASS $1"; else echo "FAIL $1"; RC=1; fi; }
ok "the server logged an echo call that started and ended on hangup" \
  "grep -q '\"call\": [0-9]*, \"event\": \"started\", \"mode\": \"echo\"' '$T/server.log' && grep -q '\"call\": [0-9]*, \"event\": \"ended\", \"why\": \"hangup\"' '$T/server.log'"
ok "the server logged a passkey enrol and a passkey login, both ok" \
  "grep -q '\"auth\": \"enrol\".*\"result\": \"ok\"' '$T/server.log' && grep -q '\"auth\": \"login\".*\"result\": \"ok\"' '$T/server.log'"
ok "the new tab's sign-in reached the server (three logins ok), and every auth record is the call's own origin" \
  "[ \"\$(grep -c '\"auth\": \"login\".*\"result\": \"ok\"' '$T/server.log')\" = 3 ] && ! grep '\"auth\"' '$T/server.log' | grep -v '\"origin\": \"$B\"' | grep -vq '\"origin\": \"$QB\"'"
ok "the QR's fragment URL enrolled a passkey on the second address (PHONE-136)" \
  "grep -q '\"auth\": \"enrol\", \"origin\": \"$QB\", \"result\": \"ok\"' '$T/server.log'"
ok "no request line, and no log row, carries the fragment (#code) or a code= (PHONE-136)" \
  "! grep -q 'code=' '$T/server.log'"
stop
ok "no Unix socket anywhere under the scratch root (echo mode serves no legs)" "[ -z \"\$(find '$T' -name '*.sock')\" ]"
echo "exit $RC"; exit "$RC"
