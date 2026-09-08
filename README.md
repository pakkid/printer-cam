# printer-cam

Brings back a live web view of the **Creality K2 / K2 Plus** built-in camera, and
makes it safe to expose through a tunnel.

Creality removed the viewer page that used to be served on port 8000, but the
firmware still runs the `webrtc_local` signalling service behind it. This stack
points [go2rtc](https://github.com/AlexxIT/go2rtc) at that service and serves
the H.264 stream to browsers **without ever re-encoding it**.

```
                                      ,--WebRTC------> browser   (LAN, real-time)
K2 (192.168.1.17:8000) --WebRTC--> go2rtc
      webrtc_local              (re-mux only)
                                      `--MSE/WebSocket-> browser (via your tunnel)
```

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

Note this is separate from the ~1.5 fps *rendering* bug described under
Latency, which was a timestamp problem in playback and is fixed. The keyframe
interval, for the record, is ~1.04 real seconds.

## Deploying in Portainer

**Stacks -> Add stack -> Repository**, pointing at this repo. Portainer will
build the image (it is a two-line `Dockerfile` over the upstream go2rtc image)
and start it.

Set these under **Environment variables**:

| Variable | Default | Notes |
|---|---|---|
| `PRINTER_IP` | `192.168.1.17` | LAN address of the printer |
| `HTTP_PORT` | `1984` | Host port the viewer is published on |
| `WEBRTC_CANDIDATE` | *(empty)* | Set to `<docker-host-lan-ip>:8555` for real-time WebRTC. Empty = MSE only |
| `AUTH_USER` | *(empty)* | Leave empty to disable HTTP basic auth |
| `AUTH_PASS` | *(empty)* | |
| `LOG_LEVEL` | `info` | `debug` or `trace` when troubleshooting |

Then open `http://<docker-host>:1984/`.

A repository stack is the path of least resistance because this compose file
uses `build: .`, so the deploy needs `Dockerfile`, `www/` and `config/` next to
it. Portainer clones the repo onto the host, so that resolves. The repo can be
private -- Portainer takes credentials for it.

Portainer's **Web editor** and **Upload** options accept a compose file on its
own with no build context, so `build:` has nothing to work from there. If you
want to avoid git entirely, either:

- copy this directory to the Docker host and run `docker compose up -d` over
  SSH (Portainer will then show it as an external stack, with limited editing);
  or
- build and push the image to a registry once, then replace `build: .` with
  `image: ghcr.io/<you>/printer-cam:1` and paste the compose into the web
  editor -- no build context needed.

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

**Config layering.** `config/go2rtc.yaml` is static and baked into the image;
`docker-compose.yml` layers your environment on top by passing extra `-config`
arguments containing inline JSON. That is deliberate. go2rtc does support
`${VAR}` substitution inside the YAML, but it registers every substituted value
as a "secret" and its log redaction writer then returns a short byte count,
which corrupts its own log output (`zerolog: could not write event`). Compose
does the substitution instead, before the container starts.

**`www/video-rtc.js` is vendored** from go2rtc v1.9.14 (MIT). It is the
upstream player component -- WebSocket signalling, MSE buffering, reconnect --
and `www/index.html` subclasses it for the UI. Refresh it from upstream if you
bump the base image.

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
- **Nothing at all after a firmware update** -- Creality may have moved or
  removed `/call/webrtc_local`. Verify with
  `curl -i http://<printer-ip>:8000/call/webrtc_local`.
