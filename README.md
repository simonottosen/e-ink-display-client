# E-ink display client

Raspberry Pi client for the Pimoroni Inky Impression E673 / Spectra 6 7.3-inch
display.

Requires 2.4 GHz only

## Set up a Pi

```bash
sudo apt update && sudo apt install -y git
git clone https://github.com/simonottosen/e-ink-display-client.git
cd ~/e-ink-display-client
sudo ./pi-agent/scripts/install-pi-agent.sh "$PWD"

sudoedit /etc/inky-agent/config.env
// set server url
sudoedit /boot/firmware/config.txt
// comment out dtparam=spi=on 

reboot...

sudo systemctl enable --now inky-agent
systemctl status inky-agent
```

## Edit the agent configuration

```bash
sudoedit /etc/inky-agent/config.env
```

## Add WiFi SSID
`````js
sudo nmcli connection add type wifi \
  ifname wlan0 \
  con-name "a WiFi" \
  ssid "SSID" \
  wifi-sec.key-mgmt wpa-psk \
  wifi-sec.psk "PASSWORD"

sudo nmcli connection modify "a WiFi" \
  connection.autoconnect yes \
  connection.autoconnect-priority 20
`````

## Update an installed Pi

```bash
cd ~/e-ink-display-client
git pull --ff-only
sudo ./pi-agent/scripts/install-pi-agent.sh "$PWD"
sudo systemctl restart inky-agent
```
