# printer-cam

Brings back a live web view of the **Creality K2 / K2 Plus** built-in camera, and
makes it safe to expose through a tunnel.

Creality removed the viewer page that used to be served on port 8000, but the
firmware still runs the `webrtc_local` signalling service behind it. This stack
points [go2rtc](https://github.com/AlexxIT/go2rtc) at that service and serves
the H.264 stream to browsers **without ever re-encoding it**.

```
K2:8000  --WebRTC-->  go2rtc  --.
webrtc_local        (re-mux only)|
                                 >--  web  -->  browser
K2:7125  --HTTP--->  print     --'  (one port)
Moonraker            status
```

`web` is the only service published: it proxies the viewer and the stream
straight through to go2rtc, and adds one endpoint, `/print`, carrying four
numbers for the progress overlay. go2rtc's HTTP port is not published at all
any more, and Moonraker is never reachable from the browser.

It publishes two ports: the viewer on `HTTP_PORT` (point your tunnel here) and
the on/off switch on `ADMIN_PORT` (do not).

The viewer asks for WebRTC and MSE at the same time and keeps whichever
connects, so a LAN browser gets real-time video and a browser coming through an
HTTP tunnel transparently falls back to a ~0.6s feed.

## What it costs

Container CPU as a share of **one** core (AMD Ryzen 7 7735HS; stream is
1280x720 H.264, ~15 fps, ~2 Mbps):

| State | CPU | RAM |
|---|---|---|
| Nobody watching | **0.00%** | 7 MiB |
| 1 viewer | ~2.7% | 16 MiB |
| 6 concurrent viewers | ~4.5% | 24 MiB |

Two things make this cheap:

- **No transcoding, ever.** The printer's H.264 is passed through as-is. No
  ffmpeg, no decode, no encode.
- **On demand.** go2rtc only dials the printer while somebody is watching. Close
  the tab and the connection to the printer drops, back to 0%.

One go2rtc connection to the printer is fanned out to all viewers, so the
printer's own load does not grow with the number of people watching.

## Latency

| Transport | Measured latency | Rendered frame rate |
|---|---|---|
| WebRTC | real-time (drift -0.06s over 15s) | ~15 fps |
| MSE | ~0.6s behind live | 15.0 fps |

Both transports are now smooth. Getting MSE there took a fix worth
understanding, because out of the box it plays this camera at about 1.5 fps:

**The printer's timestamps are wrong.** It stamps its RTP timestamps as though
it were encoding at ~1.5 fps while actually sending ~14.6 fps. go2rtc copies
those timestamps faithfully into the fMP4 it muxes, so the container claims
1.5 fps and its media clock runs about 9.7x faster than wall time. A browser
renders MSE frames according to those timestamps, so it plays a slideshow.

WebRTC is immune -- it renders frames as they arrive and uses timestamps only
for jitter buffering -- which is why the same feed looks fine there and awful
on MSE.

`video-rtc.js`'s own live controller cannot cope with it either: it keeps a
5-media-second window, which here is only ~0.5 real seconds of video, and
drives `playbackRate` toward 1.0, i.e. toward 1.5 fps. Its forward seeks into a
window that thin stall the decoder outright, which is the ~3 fps stuttering
state.

So `www/index.html` overrides `onmse()`: it measures how fast media time is
actually advancing against the wall clock and plays at that rate (converges to
~10x here), with a small nudge to settle at ~0.6s of latency. The measurement
is adaptive rather than a hardcoded factor, so it keeps working if the printer
or go2rtc is ever fixed -- the ratio simply converges to 1. There is no audio
track, so a high playback rate has no pitch side effect.

**WebRTC is still the default** and is worth having: it is real-time and it
reports the stream's true 1280x720. It needs UDP/TCP 8555 reachable *and* a
candidate address the browser can route to, which on a LAN means setting
`WEBRTC_CANDIDATE` (or using host networking). Without it go2rtc can only
advertise the container's internal `172.x` address and viewers fall back to
MSE -- which is now a perfectly good fallback rather than a broken one.

For low latency *through the tunnel*, forward TCP 8555 as a raw TCP port and
load the viewer with `?mode=webrtc/tcp`, which pins ICE to TCP candidates.
Otherwise the MSE fallback over plain HTTP is fine.

## The on/off switch

For printing something you would rather not have on the internet: a page with a
single switch that refuses the viewer, the stream and the print data outright.

```
http://<docker-host>:1985/
```

Three things make it worth trusting:

**It is on a port of its own, and your tunnel does not forward it.** That, not
an IP allowlist, is the separation. An allowlist alone would not work: a tunnel
running as a container on the same Docker network has a private source address
too, indistinguishable from a machine on your LAN. `ADMIN_ALLOW` (private
ranges by default) and an optional `ADMIN_PASS` sit on top as defence in depth.

**The position survives a restart.** It is written to a file on the
`camera-switch` volume and restored before nginx starts, so a reboot or a
`docker compose up -d` does not quietly put the camera back on the air. A fresh
volume starts live.

**Anyone already watching gets dropped, not grandfathered.** This took two
mechanisms, because the two transports leave by different doors:

- *Through the front door* (MSE, `stream.mp4`, the viewer itself): nginx
  normally keeps old workers alive through a reload until their connections
  close, and a video stream never closes, so a viewer mid-stream would have
  carried on indefinitely. `worker_shutdown_timeout 3s` in `web/main.conf`
  terminates them instead. Measured: an in-flight stream is cut about three
  seconds after the switch moves.
- *Not through the front door at all* (WebRTC): once a peer connection is up,
  media flows browser <-> `go2rtc:8555` directly, and the player has already
  closed the signalling WebSocket, so nothing upstream has a connection left to
  drop. Gating HTTP alone left an established WebRTC session streaming
  happily -- verified at 14.8 fps with the switch off. So the switch also POSTs
  to go2rtc's `/api/restart`, which re-execs the process and tears down every
  peer connection at once. Verified after the fix: 0 fps, playhead frozen,
  `pcState` closed.

`/api/restart` is in go2rtc's `allow_paths` for that one purpose, and the front
door denies the path on its public listener (`location = /api/restart { deny
all; }`) -- without that, `location /` would proxy it straight through to the
internet. go2rtc's own port is not published either, so the only thing that can
reach it is the front-door container. Do not "tidy up" either half.

As a side effect of the port separation, the viewer page cannot drive the
switch even in the browser: it is a different origin with no CORS headers, so a
`fetch` from the viewer fails outright.

While it is off, every path on the viewer port returns 503 with a plain "the
camera is switched off" page -- the viewer, the assets, `/api/ws`,
`/api/stream.mp4` and `/print` alike. The switch's own port stays up, or you
could never turn it back on. And because go2rtc only dials the printer while
somebody is watching, switching off also stops the camera being read at all.

| Variable | Default | Notes |
|---|---|---|
| `ADMIN_PORT` | `1985` | The switch. Do not tunnel this |
| `ADMIN_BIND` | `0.0.0.0` | Set to one interface to narrow it further |
| `ADMIN_ALLOW` | private ranges | Space-separated CIDRs |
| `ADMIN_USER` / `ADMIN_PASS` | `admin` / *(empty)* | Optional password on the switch |

## Print progress overlay

While a print is running, a bar appears over the bottom of the video with
percentage, filament type and colour, elapsed time and filament used. When
nothing is printing there is no bar and no placeholder.

It reads one endpoint, `/print`, which returns only:

```json
{"printing": true, "paused": false, "progress": 0.386,
 "elapsed_s": 1621, "filament_m": 5.03,
 "filament_type": "PLA", "filament_name": "Soleyin Ultra PLA",
 "filament_color": "#ffffff", "filament_g": 15.0}
```

...or `{"printing": false}`. Nothing else about the printer leaves the network
-- not the filename, not the file path, not positions or temperatures.

**Moonraker is deliberately never exposed to the browser.** It has no CORS
headers, so a page could not read it directly anyway, but the real reason is
that its API can *control* the printer: cancel a print, run arbitrary gcode,
trigger an emergency stop. Putting that behind a tunnel would be reckless. Only
the front-door container talks to it, over a single fixed read-only query, and
`/print` refuses anything but `GET`.

**There is deliberately no time-remaining figure.** This printer's Moonraker
reports `slicer: Unknown` and leaves `estimated_time` null -- it does not parse
slicer metadata at all -- so the only way to produce one is to extrapolate from
elapsed time and progress. That assumes every remaining layer takes as long as
the average layer so far, which on a real print is wrong often enough to be
worse than showing nothing.

- **Filament is reported in metres**, straight from the extruded length that
  Klipper tracks. That is a unit conversion and nothing more, so it is exact.
- **Grams are derived** from the filament type, which the printer does report --
  see below. No configuration needed, and no guessing either: if the type is
  unknown or unrecognised, grams are omitted rather than invented.

The overlay also shows the filament type and a swatch of its actual colour.

### Why the numbers move between polls

`/print` is polled every three seconds. Left alone that makes the fascia read
as a stale snapshot -- the clock jumping in three-second steps, the bar
hopping -- so the viewer fills in the gaps, with different treatment for
different kinds of figure:

- **Elapsed time** is extrapolated exactly: the polled value plus how long ago
  it was polled. It is wall-clock time, so there is nothing to guess. It holds
  still while a print is paused, because Klipper's own `print_duration` stops
  then too. Seconds are shown past the hour (`1h12m07s`) precisely so it keeps
  ticking on the long prints where that matters.
- **Progress and filament used** are interpolated *towards* the last reading
  and never past it. Extrapolating those would mean inventing an extrusion
  rate, and a running total that overshoots and then walks backwards reads as a
  bug rather than as precision. This costs up to one poll of lag and never
  shows a figure that was not genuinely true. Grams follow from the server's
  own grams-per-metre, so the two never disagree.

The interpolation is linear rather than eased: these quantities really do
advance at a steady rate, so with regular polls the display reaches reading N
just as N+1 arrives and the velocity stays continuous. It runs on
`requestAnimationFrame`, which stops by itself in a background tab, and the
loop is torn down entirely when no print is running.

### Where the filament type comes from

Moonraker will not tell you, which is a dead end worth documenting:

| Moonraker source | Reports type? | In practice |
|---|---|---|
| `box` (the CFS) | yes -- `material_type`, `color_value`, `remain_len` per slot | only while the CFS is connected; disconnected, all 16 slots read `-1` |
| `filament_rack` | yes -- but as an opaque code (`001601`) | no published mapping, no lookup table in the printer's own config |
| file metadata | `filament_type` | `null`, like the rest of the slicer metadata |

**Creality's own WebSocket on port 9999 does tell you**, already decoded, which
is how Creality Print and OrcaSlicer know. One read request:

```json
{"method": "get", "params": {"boxsInfo": 1}}
```

comes back with, for the spool holder (box `id: 0`; `1`-`4` are CFS boxes):

```json
{"vendor": "Creality", "type": "PLA", "name": "Soleyin Ultra PLA",
 "color": "#0ffffff", "percent": 100, "rfid": "01601", "state": 1}
```

Note `rfid: "01601"` against Moonraker's `material_type: "001601"` -- the
Moonraker field is that same material id, and this WebSocket is what resolves
it to a name. `state: 1` means the details were entered by hand rather than
read off an RFID tag, and colours arrive with a stray leading zero
(`#0ffffff`), which is stripped.

The type is looked up in a small density table (PLA 1.24, PETG 1.27, ABS 1.04,
TPU 1.21, and so on) to produce grams. `FILAMENT_DENSITY` still overrides it for
anything the table doesn't cover. If port 9999 is unreachable or reports
nothing usable, the overlay simply shows metres.

The protocol is not documented by Creality and may change with firmware, so
every part of this degrades to "no filament info" rather than failing. Credit
to [DaviBe92/k2-websocket-re](https://github.com/DaviBe92/k2-websocket-re) for
reverse-engineering it.

If you connect the CFS, the same call starts reporting `percent` per slot from
RFID spools -- filament left on the spool, a better figure than grams used, and
not something this reads today.

The endpoint is cached for two seconds, so a room full of viewers still means
one request to the printer every two seconds. If the printer is asleep or
unreachable, it reports "not printing" and the overlay simply stays away.

## Frame rate

The camera delivers **~14.6-14.9 fps** at 1280x720, measured off the wire
against a wall clock. That is the ceiling, and it is set by the printer.

Nothing in this stack can raise it -- no amount of pipeline work invents frames
the printer never sent. Moonraker reports `target_fps: 30` for this webcam, but
that is a UI hint, not what the encoder produces. The signalling protocol
offers no knob either: a client POSTs nothing but a base64'd
`{"type":"offer","sdp":...}` to `/call/webrtc_local`, with no resolution or
frame-rate parameter. Raising it would mean reconfiguring the camera daemon on
the printer over SSH, which this project deliberately does not do -- and the
printer's SoC is a 2-core ARMv7, so 720p60 is unlikely to be reachable there
in any case.

Re-measured since: **893 frames in 60.0s = 14.9 fps**, with 57 IDRs, so a
keyframe interval of ~1.05 real seconds.

Note this is separate from the ~1.5 fps *rendering* bug described under
Latency, which was a timestamp problem in playback and is fixed.

## Artifacts, and why no error correction can fix them

The picture smears and blocks every few seconds. That is **packet loss on the
printer's wifi**, and it is unrecoverable from this end. The measurements, all
taken on the LAN with nothing else in the path:

| | |
|---|---|
| Printer's only network interface | `wlan0` -- there is no wired link up |
| Media transport | UDP, ~465 packets/s, ~2.0 Mbit/s |
| RTP packets lost in 61.2s | **200 of 28,619 = 0.699%** |
| Loss events | 17 bursts, the largest 32 consecutive packets |
| Retransmissions received | **0** |
| Resulting decode faults | ~7 per minute (`Invalid NAL unit size`, `deblocking_filter_idc out of range`, `corrupt decoded frame`) |

Bursts of thirty packets are the signature of interference or a contended
channel, not of anything software is doing.

**There is no ECC to add.** Every mechanism that could repair this is either
absent or refused:

- **FEC** (ULPFEC / FlexFEC / RED) is never negotiated. go2rtc strips those
  codecs from its capability list outright (`pkg/webrtc/helpers.go`), and the
  printer does not offer them in its answer either. Neither end would use it.
- **NACK** *is* negotiated -- the printer's SDP answer carries
  `a=rtcp-fb:98 nack` and `a=rtcp-fb:98 nack pli`, and go2rtc duly asks. In 45
  seconds it sent **78 NACK requests and received not one retransmission**; the
  printer sends no RTCP at all, not even a sender report. It advertises the
  feature and does not implement it. Requesting harder cannot help.
- **ICE-TCP** would give retransmission for free, and the printer does offer a
  passive TCP candidate. go2rtc cannot take it: its WebRTC *client* is built
  with no TCP mux (`clientAPI, _ = webrtc.NewAPI()` in
  `internal/webrtc/webrtc.go`), so it gathers UDP candidates only. No config
  option changes this -- it would need a patched go2rtc.

### What the loss actually looks like

Measured in the browser over 120 seconds on each transport, same camera, same
minute-to-minute conditions:

| | WebRTC | MSE |
|---|---|---|
| Packets lost on *this* leg | 0 | n/a (TCP) |
| Frames received | 1783 | -- |
| Frames decoded | 1647 | -- |
| **Frames the decoder rejected** | **128 (7.2%)** | none rejected -- decoder dies instead |
| Keyframe requests sent | 39 | cannot send any |
| Freezes | 13, totalling **11.2s (9.3%)** | continuous reconnect loop |
| Decoded frame rate | 13.7 fps | unusable |

So the 0.699% packet loss upstream costs **7.2% of frames**, because a burst
destroys the frame it hits and then every P-frame that references it until the
next IDR. On WebRTC that reads as roughly one hitch every nine seconds, each
about as long as the keyframe interval. The browser asks for an early keyframe
39 times; the printer ignores all of them, so each freeze runs its full ~1s.

MSE was worse than "artifacts": Chrome raises `MEDIA_ERR_DECODE` on the damaged
bitstream and the decoder stops. video-rtc.js closes the WebSocket, and its
`onclose()` waits `RECONNECT_TIMEOUT - (now - connectTS)` -- with a fault every
~8 seconds the stream is never up for the full 15, so nearly the whole 15s
penalty was charged for each one and the viewer never held a picture. The
viewer now recovers immediately from a decode fault instead (see
`onDecodeFault()` in `www/index.html`), rate-limited to eight in ten seconds so
a genuinely broken stream still backs off, and it suppresses the reconnect
overlay for 1.5s so a sub-second recovery does not flash the whole UI. The
measured media/wall ratio the MSE controller depends on is also kept on the
instance now, so a recovery resumes at speed instead of re-deriving it from
scratch -- which used to take longer than the gap between faults.

That makes MSE degraded rather than broken, and no more than that: at seven
faults a minute it still cannot hold a steady rate. **MSE is a fallback, not a
second option.** If the picture matters, make WebRTC work.

What actually helps, in order:

1. **Put the printer on ethernet.** This is the fix. Loss goes to roughly zero
   and everything below becomes moot.
2. **Improve the wifi** -- 5 GHz, a clearer channel, or an AP closer to the
   printer. 0.699% is not a marginal link; it is a bad one.
3. **Stay on WebRTC.** It does not reduce loss and it is not immune to it (see
   below), but MSE on this camera is far worse: the same damaged bitstream
   makes Chrome raise `MEDIA_ERR_DECODE` and stop, and MSE also has to honour
   the printer's RTP timestamps, which are junk, so it stutters as well.

Because of that the viewer **names its transport**. If it falls back to MSE a
notice says so, top left, with the reason -- almost always that 8555 is not
reachable and `WEBRTC_CANDIDATE` is unset. In the healthy WebRTC case nothing
is shown.

### Why WebRTC is not immune either

It would be reasonable to expect the browser to hide this: WebRTC normally
detects an incomplete frame and discards it rather than rendering rubbish. That
does not apply here, because **go2rtc launders the loss before the browser ever
sees it.**

In `pkg/h264/rtp.go`, `RTPDepay()` accumulates NAL units into an access unit
until it sees a packet with the marker bit, and it never once looks at
`packet.SequenceNumber`. So when a burst goes missing mid-frame, whatever
arrived is assembled and emitted as though it were a whole frame. `RTPPay()`
then re-packetizes for each consumer with `sequencer.NextSequenceNumber()` --
a fresh, contiguous sequence.

The consumer therefore receives an unbroken RTP stream that happens to contain
structurally damaged frames. There is no gap to detect and nothing to NACK.
What happens next is split:

- frames damaged badly enough to be undecodable are rejected by the decoder --
  the 128 above, which is what the freezes are;
- frames damaged mildly enough to decode are decoded, and render as smeared or
  stale blocks that propagate until the next keyframe.

So WebRTC gets both the freezes *and* some smearing, from the same cause. This
is most visible on a moving scene: a stale block during a travel move stands
out, while the same block on an idle bed is invisible.

The fix, if it is ever worth the cost, is upstream of the browser: track the
incoming sequence number in the depacketizer, and on a gap discard the access
unit under construction and keep discarding until the next IDR. That trades the
smearing for a clean freeze of up to one keyframe interval. It is about twenty
lines, but it means building go2rtc from a patched source instead of using the
published image, so it is deliberately **not** done here -- fixing the
printer's network removes the need for it entirely.

And note that raising the frame rate, if it were possible, would make this
*worse*: twice the packets across the same lossy channel, at half the bits per
frame.

## Deploying in Portainer

**Stacks -> Add stack -> Repository**, pointing at this repo. Portainer will
build the image (it is a two-line `Dockerfile` over the upstream go2rtc image)
and start it.

Set these under **Environment variables** (locally, copy `.env.example` to
`.env` instead -- Compose reads it automatically):

| Variable | Default | Notes |
|---|---|---|
| `PRINTER_IP` | `192.168.1.17` | LAN address of the printer |
| `HTTP_PORT` | `1984` | Host port the viewer is published on |
| `WEBRTC_CANDIDATE` | *(empty)* | Set to `<docker-host-lan-ip>:8555` for real-time WebRTC. Empty = MSE only |
| `AUTH_USER` | *(empty)* | Leave empty to disable HTTP basic auth |
| `AUTH_PASS` | *(empty)* | |
| `LOG_LEVEL` | `info` | `debug` or `trace` when troubleshooting |
| `MOONRAKER_PORT` | `7125` | Moonraker's port on the printer |
| `FILAMENT_DENSITY` | *(empty)* | Overrides the density looked up from the reported filament type |
| `FILAMENT_DIAMETER` | `1.75` | Used when converting length to grams |
| `CREALITY_WS_PORT` | `9999` | Creality's WebSocket, read for the filament type |
| `ADMIN_PORT` | `1985` | The on/off switch. Do not tunnel this port |
| `ADMIN_ALLOW` | private ranges | Who may reach the switch |
| `ADMIN_PASS` | *(empty)* | Optional password on the switch |

Then open `http://<docker-host>:1984/`.

The compose file pulls a prebuilt image from `ghcr.io`, so **Web editor** works
just as well as **Repository** -- paste `docker-compose.yml` in and set the
environment variables. Nothing is built at deploy time.

That is deliberate. Portainer cannot build a stack when its environment is
connected through the **Portainer Agent**: the agent does not proxy BuildKit's
gRPC session, so the build fails with

```
listing workers for Build: failed to list workers: Unavailable:
error reading server preface: http2: frame too large
```

which is a Portainer/agent limitation rather than anything wrong with the
stack. See [portainer#12530](https://github.com/orgs/portainer/discussions/12530).
Publishing the image from CI removes the build step from the deploy path
entirely.

The image is built for `linux/amd64` and `linux/arm64` by
`.github/workflows/publish-image.yml` on every push to `master`.

The package is public -- verified with an anonymous pull -- so Portainer needs
no registry credentials. If a pull ever does fail with `denied` or
`unauthorized`, check the package's visibility at
`github.com/users/pakkid/packages/container/printer-cam/settings`, or add a
credential in Portainer under **Registries**.

To build locally instead:

```bash
docker build -t printer-cam:local . && sed -i 's|image: ghcr.io/pakkid/printer-cam:latest|image: printer-cam:local|' docker-compose.yml
```

### Pointing your tunnel at it

For plain remote viewing the tunnel needs **only port 1984 over HTTP**, and it
must pass WebSocket upgrades through (Cloudflare Tunnel, `nginx` with
`proxy_set_header Upgrade`, Tailscale Funnel all do). Viewers arriving this way
get the MSE fallback: ~0.6s behind live at the full ~15 fps.

If you want real-time remotely, additionally forward **TCP 8555** as a raw TCP
port and use `?mode=webrtc/tcp`.

If the tunnel container runs on the same Docker host, the tidiest wiring is to
put it on the same Docker network and skip the published HTTP port entirely --
uncomment the `networks:` lines in `docker-compose.yml` and point the tunnel at
`http://printer-cam:1984`.

If your tunnel serves this under a subpath rather than its own hostname, add
`base_path: /cam` to the `api:` section of `config/go2rtc.yaml`.

## Security

This is built to be internet-facing, so the attack surface is cut down in the
image itself rather than left to the tunnel:

- **`allow_paths` allowlist.** Only `/`, `/api/ws`, `/api/stream.mp4` and
  `/api/frame.jpeg` are registered as HTTP handlers. Everything else returns
  404 whether or not the caller is authenticated. That deliberately removes
  `/api/streams`, `/api/config`, `/api/restart`, `/api/exit` and `/api/log`.
  This matters: `/api/streams` lets a caller define new streams by URL, and
  `/api/config` rewrites the config file. An unprotected go2rtc API is the
  whole ballgame, so it is simply not present here.
- **No bundled web UI.** `static_dir` replaces go2rtc's own pages with the
  viewer in `www/`, so the config and stream editors do not exist either.
- **`local_auth: true`.** go2rtc's default exempts `127.0.0.1` from basic auth.
  That default is dangerous here: a tunnel client on the same host pointed at
  `http://127.0.0.1:1984` would authenticate as loopback and hand the whole
  internet an unauthenticated feed. With `local_auth: true`, browsers coming
  through the tunnel still authenticate normally, because tunnels forward the
  `Authorization` header.

Set `AUTH_USER` / `AUTH_PASS` unless you are putting real authentication in
front of it (Cloudflare Access, Authelia, Tailscale ACLs). Basic auth over a
plain-HTTP tunnel sends the password in cleartext -- terminate TLS at the
tunnel.

Note that with auth enabled, anything on your LAN that scrapes
`/api/frame.jpeg` (Fluidd, Moonraker, Home Assistant) has to send credentials
too.

## Using it

- Viewer: `http://host:1984/`
- On/off switch: `http://host:1985/` (LAN only -- never tunnel it)
- Snapshot / VLC / ffmpeg: `http://host:1984/api/stream.mp4?src=printer`
- JPEG still: `http://host:1984/api/frame.jpeg?src=printer`

Viewer URL options:

| Param | Default | Meaning |
|---|---|---|
| `src` | `printer` | Stream name |
| `mode` | `webrtc,mse,mp4` | Transport preference. `webrtc/tcp` forces TCP ICE; `mse` forces the fallback |
| `aspect` | `16/9` | Display aspect ratio -- see the caveat below |
| `buffer` | `6` | Seconds of MSE buffer to retain (real seconds, not the stream's fictional ones) |

The snapshot button in the viewer grabs the frame the browser has already
decoded, via a canvas, so it costs the server nothing.

`/api/frame.jpeg` is the one endpoint here that *does* spend real CPU: go2rtc
decodes the H.264 and encodes a JPEG in-process (no ffmpeg is spawned, but it
is still a full decode + encode per call). Polled at roughly 1.5 requests per
second it measured ~10% of a core, against ~2.7% for streaming the same
feed. Fine for the occasional still, not something to poll hard. It does come
out at the correct 1280x720, so it is unaffected by the aspect-ratio caveat
below.

## Things worth knowing

**`mjpeg` is intentionally not in the transport list.** It would re-encode
every single frame to JPEG rather than passing H.264 through, which is the one
thing here that would actually burn CPU continuously.

**`mp4` mode is not really video.** It is in the list purely as a last resort
for browsers with no MediaSource at all (older iOS Safari). `video-rtc.js`
implements it by decoding into a canvas and assigning the result to the video
element's `poster`, i.e. a still-frame slideshow. Don't reach for it as a
fallback for smooth playback -- it isn't one.

**The aspect ratio is corrected client-side, on purpose.** The printer's WebRTC
signalling carries no `sprop-parameter-sets`, so go2rtc has no SPS when it
builds the fMP4 init segment and writes a dummy 128x96 one instead. The frames
really are 1280x720, but a browser on MSE takes its intrinsic size from that
dummy track header and reports 1280x960 -- which stretches the picture
vertically by 4:3. Nothing in go2rtc repairs codec metadata from the in-band
SPS, and routing the stream through ffmpeg to rewrite the SDP would defeat the
point of the exercise. So `www/index.html` sizes the video box to the real 16:9
and lets the frame fill it. WebRTC reports the true 1280x720 and the same
correction is a no-op for it, so both transports render correctly. Override
with `?aspect=` if your camera is genuinely not 16:9.

**The status overlay dims the video rather than sitting beside it.** State
lives in one card over a scrim (`body.busy` drives both), so there is no
persistent indicator cluttering the picture once the stream is live. Transport
goes to the console instead. All the motion is CSS -- spinner, scrim fade, card
lift, the error border pulse -- and it is all disabled under
`prefers-reduced-motion`.

**WebRTC handover looks like a disconnect if you don't expect it.** When WebRTC
wins the race, `video-rtc.js` sets `pcState = OPEN` and then deliberately closes
the WebSocket. The viewer's `onclose` checks `pcState` before reporting a
disconnect, otherwise the UI flashes "reconnecting" over a perfectly live
stream.

**Config is env-driven, with one deliberate exception.**
`config/go2rtc.yaml` reads `PRINTER_IP`, `AUTH_USER`, `AUTH_PASS` and
`WEBRTC_CANDIDATE` through go2rtc's own `${VAR:default}` substitution, so
changing the printer's address means editing `.env` (or the Portainer stack's
environment variables) and redeploying. Nothing inside the image hardcodes it.

`LOG_LEVEL` is the exception and is applied by `docker-compose.yml` as a
`-config` override instead. go2rtc registers every value it substitutes as a
"secret" and redacts it from log output, and its redacting writer returns a
short byte count, so **any log line containing a substituted value is dropped**
with `zerolog: could not write event`. For the log level that string appears in
every line, which loses the whole log. Compose substitutes it before the
container starts, which avoids the mechanism entirely.

The same quirk has a small cost for `PRINTER_IP`: at `LOG_LEVEL=debug` the two
`start producer` / `stop producer` lines contain the address and are therefore
dropped. Everything else logs normally, and at `info` nothing is affected. For
`AUTH_PASS` the redaction is the point -- the password never reaches the log.

**`www/video-rtc.js` is vendored** from go2rtc v1.9.14 and is MIT licensed;
its licence is included as `www/LICENSE-go2rtc`. It is the upstream player
component -- WebSocket signalling, reconnect, WebRTC negotiation -- and
`www/index.html` subclasses it, overriding `onmse()` as described above.
Refresh it from upstream if you bump the base image.

## Requirements

- go2rtc **>= 1.9.10** for `#format=creality`. The printer sends RTP with a
  payload type it never declares in its SDP answer; without that handling a
  generic WebRTC client reports "connected" and renders nothing. Pinned to
  1.9.14 here.
- The printer reachable on TCP 8000 from the Docker host.

## Troubleshooting

```bash
docker logs printer-cam
```

- **Viewer stuck on "Connecting"** -- check the printer is up:
  `curl -i http://<printer-ip>:8000/`. A `200` with `Content-Length: 0` is
  correct and expected; that empty page is the UI Creality removed.
- **Which transport am I on?** The viewer logs it to the browser console
  (`[printer-cam] transport: WebRTC`). If it says MSE on the LAN, then
  `WEBRTC_CANDIDATE` is unset or wrong, or 8555 is not reachable. That costs
  you ~0.6s of latency rather than breaking anything.
- **Works on the LAN, not through the tunnel** -- the tunnel is almost
  certainly not forwarding WebSocket upgrades. As a quick check, open
  `?mode=mp4`, which uses a plain HTTP response instead.
- **401 loops** -- `local_auth: true` means loopback needs credentials too.
- **Everything returns 503 and the off page** -- the switch is off. Open
  `http://host:1985/`. It also comes back off after a restart, by design.
- **The switch page is unreachable** -- check `ADMIN_ALLOW` covers the address
  nginx actually sees, which depends on how Docker publishes the port. The
  startup log prints the allowlist it built.
- **Nothing at all after a firmware update** -- Creality may have moved or
  removed `/call/webrtc_local`. Verify with
  `curl -i http://<printer-ip>:8000/call/webrtc_local`.
