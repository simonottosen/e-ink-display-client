# E-ink display client

The Raspberry Pi half of a self-hosted e-ink photo frame. It drives a Pimoroni
Inky Impression E673 / Spectra 6 7.3-inch panel (800×480) and does exactly one
thing: ask the host what should be on the display, fetch it, draw it.

The other half is the
[host](https://github.com/simonottosen/e-ink-display-server) — a Docker service
that owns the photo library, crops and renders images, runs the playlists, and
syncs shared iCloud albums.

> This is a fork of [Pimoroni's `inky`](https://github.com/pimoroni/inky)
> library. The `inky/` directory is theirs, unmodified. Everything in
> `pi-agent/` is new.

## Why a dumb client

A photo frame hung on a wall is the worst possible place to debug software. So
the Pi holds no library, makes no decisions, and renders nothing. The host sends
it a finished 800×480 RGB PNG; the Pi's only job is to put that on the panel and
say whether it worked.

That buys three things:

- **No inbound ports.** The agent only makes outbound requests, so the Pi needs
  no port forwarding, no reverse proxy, and no certificate.
- **It survives the host being down.** The panel keeps showing the last image,
  the agent backs off and retries, and it reconnects on its own.
- **Nothing to migrate.** All the state worth keeping lives on the host. If the
  SD card dies, reflash it, run the installer, and the panel repaints itself.

## How it works

The host keeps a **revision** number. The agent remembers which revision is
physically on the panel and polls every 10 seconds to compare:

```
GET /api/v1/displays/inky-main/desired?current_revision=6
  → 204 No Content          nothing to do, sleep
  → 200 { revision: 7, … }  new image waiting

GET  /api/v1/artifacts/{sha256}            fetch it, verify the hash
POST /api/v1/displays/…/jobs/{id}/started  tell the host we're refreshing
     …refresh the panel (this blocks for ~30 seconds)…
POST /api/v1/displays/…/jobs/{id}/completed
```

A heartbeat goes out every 60 seconds carrying the current revision, which is
what makes the host's "last seen" useful when the panel looks wrong.

Two details do the real work of making this reliable:

**Completion is persisted before it is reported.** The state file is written the
moment the panel refresh returns, with the acknowledgement marked pending. If
the network drops between drawing the image and telling the host, the
acknowledgement is retried on the next poll instead of being lost.

**The agent's revision describes the panel, not the host.** If the host's
database is ever reset it starts counting from zero again and offers a revision
*lower* than the one the agent holds. The agent accepts it. Treating its own
number as a high-water mark would strand the display on a stale image until the
host counted back up, which could be never.

## Set up a Pi

```bash
sudo apt update && sudo apt install -y git
git clone https://github.com/simonottosen/e-ink-display-client.git
cd ~/e-ink-display-client
sudo ./pi-agent/scripts/install-pi-agent.sh "$PWD"
```

The installer enables SPI and I²C, installs a virtualenv, creates a locked-down
`inky-agent` service account, and installs the systemd unit.

Then point it at your host:

```bash
sudoedit /etc/inky-agent/config.env     # set INKY_AGENT_SERVER_URL
```

**One manual step the installer cannot do for you.** It appends
`dtoverlay=spi0-0cs` to `/boot/firmware/config.txt`, but `raspi-config` has
already written `dtparam=spi=on` there. The two conflict, and the panel will not
refresh until you comment the second one out:

```bash
sudoedit /boot/firmware/config.txt      # comment out dtparam=spi=on
sudo reboot
```

After the reboot:

```bash
sudo systemctl enable --now inky-agent
systemctl status inky-agent
```

### Wi-Fi

The Pi Zero 2 W and friends are **2.4 GHz only** — a 5 GHz-only SSID will not
appear.

```bash
sudo nmcli connection add type wifi \
  ifname wlan0 \
  con-name "a WiFi" \
  ssid "SSID" \
  wifi-sec.key-mgmt wpa-psk \
  wifi-sec.psk "PASSWORD"

sudo nmcli connection modify "a WiFi" \
  connection.autoconnect yes \
  connection.autoconnect-priority 20
```

### Updating

```bash
cd ~/e-ink-display-client
git pull --ff-only
sudo ./pi-agent/scripts/install-pi-agent.sh "$PWD"
sudo systemctl restart inky-agent
```

The installer is idempotent and will not overwrite `/etc/inky-agent/config.env`.

## Configuration

`/etc/inky-agent/config.env`, read by systemd at start. Shell-style
`KEY=value`; every key can also be set as an environment variable.

| Setting | Default | Notes |
|---|---|---|
| `INKY_AGENT_SERVER_URL` | `http://192.168.0.101:8000` | **Change this.** The host app URL |
| `INKY_AGENT_DISPLAY_ID` | `inky-main` | Must match the host |
| `INKY_AGENT_DATA_DIR` | `/var/lib/inky-agent` | State and cached artifacts |
| `INKY_AGENT_DEVICE_TOKEN` | *(empty)* | Sent as a bearer token when set |
| `INKY_AGENT_POLL_INTERVAL_SECONDS` | `10` | Minimum 5 |
| `INKY_AGENT_HEARTBEAT_INTERVAL_SECONDS` | `60` | Minimum 5 |
| `INKY_AGENT_REQUEST_TIMEOUT_SECONDS` | `30` | |
| `INKY_AGENT_RETRY_INITIAL_SECONDS` | `2` | Backoff doubles per failure… |
| `INKY_AGENT_RETRY_MAX_SECONDS` | `60` | …up to this |
| `INKY_AGENT_HARDWARE_ENABLED` | `true` | `false` runs everything but the panel |

`INKY_AGENT_SERVER_URL` is the **application** URL. It is never a Docker socket
or a Docker API endpoint.

## What lives where on the Pi

```
/etc/inky-agent/config.env           settings (root:inky-agent, 0640)
/etc/systemd/system/inky-agent.service
/var/lib/inky-agent/state.json       current revision, pending acknowledgement
/var/lib/inky-agent/artifacts/       cached PNGs, by sha256
~/e-ink-display-client/pi-agent/.venv
```

`state.json` is written atomically — temp file, rename, fsync the directory — so
a power cut during a write cannot leave it truncated. **Deleting it is the
reset button**: the agent forgets what is on the panel and repaints from
whatever the host currently wants.

The service runs as a system user with `NoNewPrivileges`, `ProtectSystem=strict`
and `ProtectHome`, and sees the source tree read-only at `/opt/inky`. It is
`Type=notify` with a 120-second watchdog, so if the poll loop wedges, systemd
restarts it.

## Running without a panel

Everything except the hardware works on an ordinary machine, which is how the
agent is developed:

```bash
python3 -m venv pi-agent/.venv
pi-agent/.venv/bin/python -m pip install -e .
pi-agent/.venv/bin/python -m pip install -e pi-agent

INKY_AGENT_HARDWARE_ENABLED=false \
INKY_AGENT_DATA_DIR=/tmp/inky-agent \
INKY_AGENT_SERVER_URL=http://127.0.0.1:8000 \
pi-agent/.venv/bin/inky-agent
```

With hardware disabled the real driver is swapped for a no-op, so polling,
downloading, checksum verification, state persistence, and acknowledgements all
run for real — only the GPIO/SPI call is skipped.

## Tests

The toolchain is the `inky-tools:dev` image built from the
[host repo](https://github.com/simonottosen/e-ink-display-server):

```bash
docker run --rm -v "$PWD":/w -w /w/pi-agent inky-tools:dev run --all-extras pytest -q
```

Coverage is deliberately narrow and aimed at the things that fail at 3am: the
poll/refresh/acknowledge cycle and revision handling (`test_agent.py`), the
exact endpoints and headers the host expects (`test_transport.py`), checksum
verification and atomic artifact installation (`test_spool.py`), and the shared
wire models (`test_contract.py`).

## Troubleshooting

```bash
systemctl status inky-agent
journalctl -u inky-agent -n 50 --no-pager
```

**The panel never updates.** Check `dtparam=spi=on` is commented out in
`/boot/firmware/config.txt` and that the Pi has been rebooted since.

**The agent runs but nothing appears.** Ask the host what it thinks:
`curl http://<host>:8000/api/v1/displays/inky-main`. `last_seen_at` tells you
whether heartbeats are arriving; `last_error` usually says the rest.

**Refreshes look slow.** They are. A full six-colour refresh takes tens of
seconds — the driver waits up to 40 for the panel to report it has finished —
and the display worker serialises them, so one refresh always completes before
the next begins.

## Hardware

Pimoroni Inky Impression 7.3" (E673 / Spectra 6), 800×480, six colours plus
black and white. The driver is Pimoroni's; this fork vendors it so the agent and
the panel library install as one unit.

## Licence

MIT, inherited from Pimoroni's `inky`. See [LICENSE](LICENSE).
