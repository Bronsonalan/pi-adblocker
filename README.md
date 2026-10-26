# Pi Adblock

Pi Adblock is a small DNS server for a Raspberry Pi. It answers names in `blocklist.txt` with `0.0.0.0` (and `::` for IPv6) and forwards every other name to `1.1.1.1`, then `1.0.0.1`, then `9.9.9.9`. Clients that use the Pi for DNS stop reaching those ad domains, without any software on the phone or laptop.

`blocklist.txt` is the block list. A listed name also blocks every subdomain under it, so `doubleclick.net` and `ads.doubleclick.net` are both blocked. A built-in safety list keeps Google, Apple, GitHub, Cloudflare, Microsoft, and the services those names depend on reachable even when a broad parent is listed. `mask.icloud.com` and `mask-h2.icloud.com` (iCloud Private Relay) stay blocked. A bare TLD or other public suffix in the file is ignored. Names under `.local`, `.lan`, and `.home.arpa`, plus reverse lookups for private addresses, are forwarded.

Nothing is downloaded. The Pi checks `blocklist.txt` out and pulls it once a minute. To stop blocking a name, delete its line. The allowlist stays a separate file on the Pi.

The program is plain Python 3 from the standard library. Raspberry Pi OS already has `python3` and `git`. The installer uses apt only when `git` is missing, and it says so. Do not install anything with pip.

## One-command install

On the Pi:

```bash
curl -fsSL https://raw.githubusercontent.com/Bronsonalan/pi-adblocker/main/bootstrap.sh | sudo bash
```

That downloads this repository, unpacks it, and runs the installer as root. The installer copies the program to `/opt/piadblock`, writes `/etc/piadblock`, installs `piadblock.service`, and starts it so it comes back on boot.

You should see `piadblock: downloading ...`, then `piadblock: running installer`, then the installer transcript in the next section. It finishes with `done`.

The same transcript is what you get from the step-by-step install below. Use that path if you want to run each command yourself.

## Update an existing Pi

Updating is the same installer again. It keeps a custom `upstream` list in `/etc/piadblock/config.json`, makes `/opt/piadblock` a git checkout if it is not one yet, and starts three timers: pull every minute, heartbeat every hour, and a stats push every five minutes. The DNS service still starts on boot.

The old default of only `1.1.1.1` with a 2 second timeout is rewritten on that run to `1.1.1.1`, `1.0.0.1`, and `9.9.9.9`, with a 1 second timeout per try. The transcript then contains:

```text
config: upstream is 1.1.1.1 1.0.0.1 9.9.9.9, timeout 1.0s per try
```

A list you chose yourself, such as `8.8.8.8`, stays as written. `systemctl is-active piadblock` prints `active` after the installer finishes.

### 1. Allow the existing deploy key to push

This step is in the browser, not on the Pi. The Pi (user `techexplain`) already has an ed25519 key at `/home/techexplain/.ssh/pi_adblocker_deploy` and a `Host github.com` entry in `~/.ssh/config` that points at it. Write access on that GitHub deploy key has to be turned on once.

1. Open [the repository deploy keys](https://github.com/Bronsonalan/pi-adblocker/settings/keys).
2. Open the key that matches `/home/techexplain/.ssh/pi_adblocker_deploy`.
3. Check **Allow write access** and save.

The installer reuses that key and the SSH config. It generates a new key only when both are missing. If the installer prints `deploy key: wrote ...` followed by a public key, add that key on the same page with **Allow write access** checked, then run the installer once more.

### 2. Run the installer

```bash
curl -fsSL https://raw.githubusercontent.com/Bronsonalan/pi-adblocker/main/bootstrap.sh | sudo bash
```

This downloads the current `main` branch and runs the installer as root. The DNS server stays root so it can bind port 53 and reload itself. Every git command runs as `techexplain` through `runuser`, because OpenSSH refuses a private key owned by another user. When `~/.ssh/config` already has `Host github.com`, that entry chooses the key, and the installer does not set `GIT_SSH_COMMAND`.

On a Pi that is already a checkout, expected output includes:

```text
git: already installed (/usr/bin/git)
checkout: keeping the git repo at /opt/piadblock
blocklist: /opt/piadblock/blocklist.txt
git user: techexplain (git pull and push run as this user; the DNS service stays root)
deploy key: reusing /home/techexplain/.ssh/pi_adblocker_deploy (from ~/.ssh/config Host github.com)
git remote: git@github.com:Bronsonalan/pi-adblocker.git
git: checkout /opt/piadblock tracking git@github.com:Bronsonalan/pi-adblocker.git
systemd: installed piadblock-pull.timer
systemd: installed piadblock-heartbeat.timer
systemd: installed piadblock-stats.timer
systemd: started piadblock-pull.timer
systemd: started piadblock-heartbeat.timer
systemd: started piadblock-stats.timer
systemd: started piadblock.service
self-check: doubleclick.net A -> 0.0.0.0
self-check: doubleclick.net AAAA -> ::
systemd: started piadblock-pull.service
systemd: started piadblock-heartbeat.service
systemd: started piadblock-stats.service
done
```

The first update from an older install has no `.git` yet, so that run prints `installing files to /opt/piadblock` instead of `checkout: keeping the git repo`. If `git` was not installed, the line before the git user line is:

```text
git: not found, installing it with apt (Raspberry Pi OS normally includes git)
```

A failed fetch or push does not fail the install and does not stop DNS. You then see one of:

```text
warning: git pull failed (...). Blocking is unchanged.
warning: git push failed (...). Blocking is unchanged.
```

### 3. Confirm the timers are waiting, not dead

```bash
systemctl status piadblock-pull.timer piadblock-heartbeat.timer piadblock-stats.timer --no-pager
```

This prints the three timers and exits. Each one should be `enabled` and `Active: active (waiting)`, with a `Trigger:` time. `inactive (dead)` and `Trigger: n/a` means the unit was enabled but not started. Rerunning the installer above starts them in this session and leaves them enabled for the next boot.

Then read the last pull:

```bash
sudo journalctl -u piadblock-pull -n 20 --no-pager
```

This prints the last pull and exits.

Expected output includes either:

```text
blocklist unchanged
```

or, after `blocklist.txt` changed on GitHub:

```text
blocklist changed; reloaded
```

If the key cannot push yet, the same journal shows:

```text
warning: git push failed (...). Blocking is unchanged.
```

Queries keep being answered. The next pull retries the push after write access is on.

## Step by step

Run these from the Pi, one at a time.

### 1. Check that Python is already installed

```bash
python3 --version
```

This prints the interpreter Raspberry Pi OS shipped. Pi Adblock needs 3.9 or newer (Bullseye and Bookworm both qualify).

Expected output is one line, for example:

```text
Python 3.11.2
```

The minor version can differ. If this command is not found, you are not on a normal Raspberry Pi OS image.

### 2. Download the source archive

```bash
curl -fsSL -o /tmp/pi-adblocker.tar.gz https://github.com/Bronsonalan/pi-adblocker/archive/refs/heads/main.tar.gz
```

`curl` saves the git archive to `/tmp`. `-f` makes a failed download a non-zero exit instead of an HTML error page. There is no progress meter.

Expected output: the command returns to the shell with no text.

Confirm the file is there:

```bash
ls -lh /tmp/pi-adblocker.tar.gz
```

Expected output is one line, similar to:

```text
-rw-r--r-- 1 pi pi 32K Oct 14 21:00 /tmp/pi-adblocker.tar.gz
```

The size only needs to be non-zero.

### 3. Unpack the archive

```bash
sudo tar -xzf /tmp/pi-adblocker.tar.gz -C /tmp
```

This creates `/tmp/pi-adblocker-main/`. Expected output: no text.

```bash
ls /tmp/pi-adblocker-main/install.py
```

Expected output:

```text
/tmp/pi-adblocker-main/install.py
```

### 4. Install and start the service

```bash
sudo python3 /tmp/pi-adblocker-main/install.py
```

This needs root so it can bind port 53 and install a systemd unit. It:

- copies the code to `/opt/piadblock`
- writes `/etc/piadblock/config.json` (upstreams `1.1.1.1`, `1.0.0.1`, `9.9.9.9`) when that file is not already there
- writes the allowlist, and uses `/opt/piadblock/blocklist.txt` from the git checkout as the block list
- does **not** download a blocklist
- reuses the existing deploy key (or writes one if none exists) and tracks `git@github.com:Bronsonalan/pi-adblocker.git`
- frees port 53 if `systemd-resolved` or `dnsmasq` has it (see below)
- points the Pi's own resolver at those upstreams when `/etc/resolv.conf` currently points at localhost or the router, and refreshes a file this installer already wrote. The file uses `options timeout:1 attempts:1 rotate edns0`, so a nameserver that does not answer costs about a second. glibc's default is 5 seconds, tried twice, which is about 10 seconds. A public resolver you wrote yourself is left as-is, so the Pi does not loop once the LAN uses the Pi for DNS
- enables and starts `piadblock.service`, plus a minutely pull timer, an hourly heartbeat timer, and a stats timer every five minutes (no blocklist-download timer)

On a typical Raspberry Pi OS image, where `systemd-resolved` is holding port 53 and `/etc/resolv.conf` points at `127.0.0.53` or the router, expected output looks like this (`192.168.1.50` will be your Pi's address, and the `example.com` address will be whatever `1.1.1.1` returns that day):

```text
piadblock installer 0.1.0
python3: Python 3.11.2
installing files to /opt/piadblock
config: wrote /etc/piadblock/config.json
allowlist.txt: wrote /etc/piadblock/allowlist.txt
blocklist: /opt/piadblock/blocklist.txt
blocklists: none configured, download skipped
port 53: systemd-resolved is using port 53; disabling DNSStubListener
port 53: free
resolver: backed up /etc/resolv.conf
resolver: pointed this Pi at 1.1.1.1 1.0.0.1 9.9.9.9 (timeout 1s, 1 attempt)
git: already installed (/usr/bin/git)
git user: techexplain (git pull and push run as this user; the DNS service stays root)
deploy key: reusing /home/techexplain/.ssh/pi_adblocker_deploy (from ~/.ssh/config Host github.com)
git remote: git@github.com:Bronsonalan/pi-adblocker.git
git: checkout /opt/piadblock tracking git@github.com:Bronsonalan/pi-adblocker.git
systemd: installed piadblock.service
systemd: installed piadblock-pull.timer
systemd: installed piadblock-heartbeat.timer
systemd: installed piadblock-stats.timer
systemd: started piadblock-pull.timer
systemd: started piadblock-heartbeat.timer
systemd: started piadblock-stats.timer
systemd: started piadblock.service
self-check: querying 127.0.0.1
self-check: doubleclick.net A -> 0.0.0.0
self-check: doubleclick.net AAAA -> ::
self-check: example.com A -> 172.66.147.243
systemd: started piadblock-pull.service
systemd: started piadblock-heartbeat.service
systemd: started piadblock-stats.service

Pi Adblock is installed and will start on boot.
DNS is listening on port 53.
Point your router DHCP DNS at 192.168.1.50.
Status page: http://192.168.1.50:8080/
Check a block: nslookup doubleclick.net 192.168.1.50
done
```

Lines you might see instead, depending on what was already installed:

- `port 53: free` as the only port line, when nothing was listening.
- `port 53: stopping and disabling dnsmasq` when `dnsmasq` had port 53. Uninstall does not start `dnsmasq` again.
- `resolver: /etc/resolv.conf already uses a public DNS server; leaving it` when the Pi already uses something like `1.1.1.1` or `8.8.8.8` that this installer did not write.
- `resolver: /etc/resolv.conf already points at the upstream list` when a previous install wrote that file and it already matches.
- `resolver: pinned upstream DNS in /etc/dhcpcd.conf` when `dhcpcd` is installed.
- `resolver: told NetworkManager not to overwrite /etc/resolv.conf` when NetworkManager is running.
- `warning: example.com forward failed ...` when the Pi cannot reach `1.1.1.1`. Blocking still works; fix the upstream later in the config.

A non-zero exit prints `error:` and, if the service crashed, the last journal lines. The usual cause is some other program still bound to port 53. That case is covered below.

### 5. Confirm the service is running

```bash
systemctl status piadblock --no-pager
```

This asks systemd for the unit state. It does not change anything.

Expected output includes `enabled` and `Active: active (running)`. The PID and timestamp will differ:

```text
● piadblock.service - Pi Adblock DNS server
     Loaded: loaded (/etc/systemd/system/piadblock.service; enabled; preset: enabled)
     Active: active (running) since Wed 2026-10-14 21:00:00 UTC; 15s ago
   Main PID: 1234 (python3)
      Tasks: 10
     CGroup: /system.slice/piadblock.service
             └─1234 /usr/bin/python3 -m piadblock serve
```

`enabled` means it starts on boot.

### 6. Read the Pi's LAN address

```bash
hostname -I
```

This prints the addresses configured on the Pi.

Expected output is one line of addresses, for example:

```text
192.168.1.50
```

Use the IPv4 address (`192.168...`, `10...`, or `172.16...` through `172.31...`). Give the Pi a DHCP reservation on the router so this address does not change.

## Point the network at the Pi

DNS has to be changed where devices get their resolver. The Pi does not intercept traffic by itself.

### The router (preferred)

1. Open the router's LAN or DHCP settings (the name varies: DHCP, LAN, DNS server).
2. Set the DNS server handed out to clients to the Pi's IPv4 address, for example `192.168.1.50`.
3. If there is a second DNS server field, leave it empty. A second server such as `8.8.8.8` lets devices skip the Pi.
4. Save, then reconnect a phone or laptop to the Wi-Fi, or reboot it, so it picks up a new DHCP lease.

The Pi itself keeps using `1.1.1.1` directly. That is deliberate. If the Pi asked the router, and the router asked the Pi, lookups on the Pi would loop and blocklist edits would fail.

### One device, without changing the router

Set that device's DNS server to the Pi's address and leave the alternate DNS blank.

- Windows: Settings, Network, the adapter, DNS server assignment, Manual.
- macOS: System Settings, Network, the interface, Details, DNS.
- Android and iOS: the Wi-Fi network's DNS setting.

Turn off encrypted DNS or the device never asks the Pi:

- Android: Settings, Network, Private DNS, Off.
- Chrome: Settings, Privacy and security, Use secure DNS, Off (or the OS default).
- iCloud Private Relay and Firefox `about:config` `network.trr.mode` bypass the Pi the same way.

## Check that blocking works

Run these on a device that is using the Pi for DNS. Replace `192.168.1.50` with the address from `hostname -I`. Putting the address on the command makes the test unambiguous: the `Server:` line must be the Pi, not the router.

### A blocked name

```bash
nslookup doubleclick.net 192.168.1.50
```

This asks the Pi for `doubleclick.net`. `nslookup` sends both an A query and an AAAA query.

Expected output:

```text
Server:		192.168.1.50
Address:	192.168.1.50#53

Non-authoritative answer:
Name:	doubleclick.net
Address: 0.0.0.0
Name:	doubleclick.net
Address: ::
```

`0.0.0.0` is the IPv4 sink. `::` is the IPv6 sink. The name is not "missing"; it is answered on purpose so clients stop searching.

A subdomain is blocked by the same denylist entry:

```bash
nslookup ads.doubleclick.net 192.168.1.50
```

Expected output is the same shape: `Address: 0.0.0.0` and `Address: ::`.

IPv6 only:

```bash
nslookup -type=AAAA doubleclick.net 192.168.1.50
```

Expected output includes:

```text
Name:	doubleclick.net
Address: ::
```

### A name that is not on the list

```bash
nslookup example.com 192.168.1.50
```

This is forwarded to `1.1.1.1`. Expected output shows the Pi as `Server:` and one or more public addresses, not `0.0.0.0` or `::`. The addresses change over time. A current answer looks like:

```text
Server:		192.168.1.50
Address:	192.168.1.50#53

Non-authoritative answer:
Name:	example.com
Address: 172.66.147.243
Name:	example.com
Address: 104.20.23.154
Name:	example.com
Address: 2606:4700:10::6814:179a
Name:	example.com
Address: 2606:4700:10::ac42:93f3
```

If `Server:` is your router or `127.0.0.53`, the device is not using the Pi. Check DHCP, then reconnect the device.

If a blocked name still shows a public address, the device cached the old answer. Flush it and query again:

```bash
# Windows
ipconfig /flushdns
```

```bash
# macOS
sudo dscacheutil -flushcache
sudo killall -HUP mDNSResponder
```

On the Pi itself, if it uses systemd-resolved as a client:

```bash
resolvectl flush-caches
```

### Watch the lookup log

Every query is printed to stdout. systemd sends that to the journal. On the Pi:

```bash
journalctl -u piadblock -n 20 --no-pager
```

This prints the last 20 log lines and exits.

After the `nslookup` commands above, expected lines include:

```text
BLOCKED 192.168.1.20 doubleclick.net A
BLOCKED 192.168.1.20 doubleclick.net AAAA
OK 192.168.1.20 example.com A
```

`BLOCKED` is a denylist hit. `OK` was forwarded to an upstream or served from the cache. `FAIL` means every upstream failed. The first field is the client's address, then the domain, then the query type.

Each failed attempt is also a Python log line, one reason per try: `timeout`, `servfail`, `refused`, `socket`, or `truncation` (a UDP response with the TC bit, retried over TCP). A later upstream can still answer, so a timeout on `1.1.1.1` is logged and counted even when `1.0.0.1` returns the address.

```bash
journalctl -u piadblock -n 200 --no-pager | grep 'upstream '
```

Expected lines look like:

```text
upstream timeout 1.1.1.1 for example.com A: timed out
upstream servfail 1.0.0.1 for ads.example A: SERVFAIL
upstream truncation 1.1.1.1 for example.com A: TC bit set; retrying over TCP
upstream failed for example.com A: 1.1.1.1 timeout: timed out; 1.0.0.1 socket: ...
```

`upstream failed` is the line where the client was given SERVFAIL or REFUSED. The per-reason counters are also on `/api/stats` under `upstream_errors` (since the process started) and under `today.upstream_errors` (the UTC day). The same totals are in the minute summary:

```text
stats queries=12 blocked=4 cached=2 forwarded=6 errors=1 timeout=2 servfail=1 refused=0 socket=0 truncation=0
```

Follow new lines as you browse:

```bash
journalctl -u piadblock -f
```

Ctrl-C stops following. Expected output is a stream of `BLOCKED` and `OK` lines, one per lookup.

Startup also logs the list sizes. With `blocklist.txt` and no downloaded list the blocklist count is the number of names actually loaded (public-suffix lines are left out). A downloaded list, when one is configured, is included in that same count, and a name that appears in both is counted once:

```text
lists: 91526 blocklist, 0 allowlist
```

That blocklist count follows the file. After a bad reload the journal says `keeping the lists already loaded` and the previous count stays in use.

## Port 53 is already in use

Only one program can bind UDP port 53 on `0.0.0.0`. The installer tries to clear the two that show up on Raspberry Pi OS. You can see the current owner yourself.

```bash
sudo ss -ulpn 'sport = :53'
```

`ss` lists UDP sockets whose local port is 53, including the process name. `iproute2` (which provides `ss`) is part of Raspberry Pi OS.

### systemd-resolved

Expected output when the stub resolver has the port:

```text
State  Recv-Q Send-Q Local Address:Port Peer Address:PortProcess
UNCONN 0      0      127.0.0.53%lo:53   0.0.0.0:*    users:(("systemd-resolve",pid=412,fd=14))
```

The installer writes `/etc/systemd/resolved.conf.d/piadblock.conf` with `DNSStubListener=no` and `DNS=1.1.1.1`, then restarts `systemd-resolved`. Uninstall removes that file and restarts resolved.

To do the same thing by hand, then re-run the installer:

```bash
sudo mkdir -p /etc/systemd/resolved.conf.d
```

Expected output: no text. The directory exists.

```bash
printf '[Resolve]\nDNSStubListener=no\nDNS=1.1.1.1\n' | sudo tee /etc/systemd/resolved.conf.d/piadblock.conf
```

Expected output is the file contents, printed by `tee`:

```text
[Resolve]
DNSStubListener=no
DNS=1.1.1.1
```

```bash
sudo systemctl restart systemd-resolved
```

Expected output: no text, exit code 0. Run `sudo ss -ulpn 'sport = :53'` again. The `systemd-resolve` line should be gone.

### dnsmasq

Expected `ss` output:

```text
State  Recv-Q Send-Q Local Address:Port Peer Address:PortProcess
UNCONN 0      0            0.0.0.0:53   0.0.0.0:*    users:(("dnsmasq",pid=880,fd=4))
```

The installer runs `systemctl stop dnsmasq` and `systemctl disable dnsmasq`. To do it by hand:

```bash
sudo systemctl disable --now dnsmasq
```

Expected output says the unit was disabled, for example:

```text
Removed "/etc/systemd/system/multi-user.target.wants/dnsmasq.service".
```

Then:

```bash
systemctl is-active dnsmasq
```

Expected output:

```text
inactive
```

Uninstall will not start `dnsmasq` again. Turn it back on only if you are removing Pi Adblock:

```bash
sudo systemctl enable --now dnsmasq
```

### Anything else

If `ss` shows `named`, `unbound`, or another program, the installer stops and prints the process. It will not kill an unknown daemon. Stop that service yourself (`sudo systemctl stop named`, or the equivalent), then run `sudo python3 /tmp/pi-adblocker-main/install.py` again.

After a successful install, `ss` should show Python on port 53:

```text
State  Recv-Q Send-Q Local Address:Port Peer Address:PortProcess
UNCONN 0      0            0.0.0.0:53   0.0.0.0:*    users:(("python3",pid=1234,fd=5))
UNCONN 0      0               [::]:53      [::]:*    users:(("python3",pid=1234,fd=7))
```

The IPv6 line is the same server. The PID will differ.

## Status page

On a device on the LAN:

```bash
curl -s http://192.168.1.50:8080/health
```

Expected output:

```text
ok
```

Counters, including blocked queries:

```bash
curl -s http://192.168.1.50:8080/api/stats
```

Expected output is JSON. `blocked` increases when a denylist name is queried. `blocklist_domains` is the number of names on `blocklist.txt`, plus any downloaded list, with each name counted once. `deny_count` is the file alone and `allow_count` is the allowlist. Each `recent_blocked` entry keeps a raw unix `ts`.

Open `http://192.168.1.50:8080/` for theme D, the aura overview. Open `http://192.168.1.50:8080/c` for theme C, the denser HUD. Each page links to the other (`THEME_C` on `/`, `THEME_D` on `/c`). Both poll `/api/stats` and `/api/crt` every 3 seconds. The three totals are today's UTC queries, blocked count, and block percent. A delta pill uses the previous UTC day already stored in `days`; when that day is absent the pill is `—`. The table is the `/api/crt` lookup feed, newest first, with `BLOCKED` rows highlighted. Theme C also lists `today.top_blocked` when that list is not empty. The footer shows the blocklist size, the allowlist count, and `PROC`, which is `queries`, `blocked`, and `blocked_percent` since this process started. Lookup times are the browser's local 24-hour clock. `/api/stats` and `/api/crt` are unchanged, so the CRT console keeps reading `/api/crt`.

The television layout is `http://192.168.1.50:8080/crt` (theme D). `http://192.168.1.50:8080/crt/c` is theme C, with the top-blocked strip. The THEME D and THEME C links switch between them. `http://192.168.1.50:8080/?crt=1` and `http://192.168.1.50:8080/c?crt=1` are the theme D television page. Each television page is a 4:3 frame with large type, the three totals, six lookups, and a FULLSCREEN button. The first click or keypress also enters fullscreen. `/` and `/c` stay the dashboard pages.

The page has no password. It is meant for the LAN. To listen only on the Pi, set `"status_listen": "127.0.0.1"` in the config and restart.

### Local demo (Mac, no Pi)

For UI work on a Mac, run the same pages with fake live data. No DNS, no sudo, no `/var/lib/piadblock`. One paste in Terminal downloads the latest code into `~/pi-adblocker-demo` and serves `http://localhost:8080/`:

```bash
curl -fsSL https://raw.githubusercontent.com/Bronsonalan/pi-adblocker/main/demo.sh | bash
```

Ctrl-C, up-arrow, Enter refreshes the checkout after a push. To try a branch before it merges:

```bash
curl -fsSL https://raw.githubusercontent.com/Bronsonalan/pi-adblocker/BRANCH/demo.sh | bash -s -- BRANCH
```

Or from a checkout: `python3 -m piadblock demo` (optional `--port 8081` if 8080 is busy).

## Current network setup

The main Pi 4 is on Ethernet at `172.20.1.217`. It is the DNS server. The router has a DHCP reservation so that address stays on this Pi. Kevin's DNS is set to Manual `172.20.1.217`.

TODO: second Wi-Fi SSID name.

TODO: DHCP reservation router.

TODO: DHCP reservation MAC address.

## CRT display

### Browser on the television

The Mac mirrors onto the 4:3 CRT. Open this on the Mac:

http://172.20.1.217:8080/crt

Theme C on the television is `http://172.20.1.217:8080/crt/c`. The THEME D and THEME C links switch between them. `http://172.20.1.217:8080/?crt=1` and `http://172.20.1.217:8080/c?crt=1` are the theme D layout. Press the FULLSCREEN button, or click or press a key anywhere on the page, to enter fullscreen.

Chrome kiosk on macOS:

```bash
open -a "Google Chrome" --args --kiosk http://172.20.1.217:8080/crt
```

In Safari, choose View > Enter Full Screen. Hide the toolbar in full screen by unchecking View > Always Show Toolbar in Full Screen.

### Console on the Pi

A fullscreen feed for the monitor on the Pi's console (`tty1`). It is laid out for a 640×480 CRT: a 16×32 console font gives 40 columns and 15 rows of large text. The newest lookup is the top row. A blocked name is drawn in white on red and marked `BLOCKED`, with the client address and the domain. The header shows today's query count and blocked count, the local time (for example `2:31:05 PM`), and the blocklist size with thousands separators when the status page reports one.

The DNS server only appends each lookup to a short in-memory list. Drawing happens in a separate process, so a slow console does not stall queries. When a Terminus 32×16 console font is installed, the display loads it on `tty1`, which is 40 by 15 characters on a 640×480 screen. The installer writes `piadblock-crt.service` and leaves it stopped, so the login prompt stays on `tty1` until you turn the display on. The display reads `http://127.0.0.1:8080/api/crt` from the status page, which is on by default.

Turn it on:

```bash
sudo systemctl enable --now piadblock-crt.service
```

Expected output: no text. The console leaves the login prompt and shows the lookup feed. `systemctl is-active piadblock-crt` prints `active`.

Restart it:

```bash
sudo systemctl restart piadblock-crt
```

Expected output: no text. The console stays on the feed. `systemctl is-active piadblock-crt` prints `active`.

Turn it off:

```bash
sudo systemctl disable --now piadblock-crt.service
```

Expected output: no text. The console returns to the login prompt. `systemctl is-active piadblock-crt` prints `inactive`.

## Configuration

The file is `/etc/piadblock/config.json`. Defaults:

- listen on `0.0.0.0` port `53` (UDP and TCP)
- forward to `1.1.1.1`, then `1.0.0.1`, then `9.9.9.9`, each with a 1 second timeout
- answer blocked A queries with `0.0.0.0` and AAAA queries with `::`
- cache up to 2048 replies
- `"blocklists": []` and `"auto_update": false` so nothing is downloaded and no timer runs
- status page on port `8080`

The server tries the upstream list in order. A timeout, socket error, SERVFAIL, or REFUSED moves to the next address. A truncated UDP answer is retried over TCP on that same address before the next one is tried. One slow server costs a single `upstream_timeout` (default 1 second) and the client still gets the next server's answer. The block list is loaded before the port opens, so the first query after listen already has it. A blocked name is answered on the Pi. A forwarded name, such as `google.com`, tries the next upstream when the first one does not answer.

```bash
dig @127.0.0.1 doubleclick.net
dig @127.0.0.1 google.com
```

`doubleclick.net` is `0.0.0.0`. `google.com` is an address from upstream. Both return within a couple of seconds of the service becoming active.

Change the list, then restart:

```bash
sudo systemctl restart piadblock
```

Expected output: no text. `systemctl is-active piadblock` prints `active`.

```json
"upstream": ["1.1.1.1", "1.0.0.1", "9.9.9.9"],
"upstream_timeout": 1.0
```

### Block list and allowlist

`blocklist.txt` at the root of this repo is the block list. On the Pi that file is `/opt/piadblock/blocklist.txt`. One domain per line. `#` starts a comment. That name and every subdomain are blocked. A bare suffix such as `com`, or any other public suffix, is skipped and counted in the log (`skipped N malformed blocklist lines`). The rest of the file still loads.

The server also keeps a built-in safety allowlist. These names, and everything under them, are answered from upstream even if `blocklist.txt` lists them or a more specific child:

`google.com`, `dns.google`, `gstatic.com`, `googleapis.com`, `googleusercontent.com`, `ggpht.com`, `gvt1.com`, `gvt2.com`, `youtube.com`, `ytimg.com`, `gmail.com`, `1e100.net`, `apple.com`, `icloud.com`, `cdn-apple.com`, `mzstatic.com`, `apple-cloudkit.com`, `github.com`, `githubusercontent.com`, `cloudflare.com`, `cloudflare-dns.com`, `microsoft.com`, `windows.com`, `windowsupdate.com`, `microsoftonline.com`, `office.com`, `office365.com`, `live.com`.

`mask.icloud.com` and `mask-h2.icloud.com` are the exception: they stay blocked, and so do names under them. A line in `/etc/piadblock/allowlist.txt` can still override that. Ad domains that are not under the safety list, such as `doubleclick.net` and `googlesyndication.com`, stay blocked.

`.local`, `.lan`, `.home.arpa`, `localhost`, and reverse lookups for private addresses (including `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`, link-local, and loopback) are forwarded. A public reverse name can still be blocked when it is listed.

Edit the list on GitHub (or in a clone you push to `main`). Within a minute, `piadblock-pull` runs `git pull --rebase` and, only when `blocklist.txt` changed, reloads the running server. The new set is built on a side thread and swapped in when it is ready, so queries keep being answered during the load. The reload does not restart the process and does not drop the listening sockets. If the new file is missing, empty, unreadable, still being written, or parses to nothing, the server logs `keeping the lists already loaded` and continues with the previous list.

To stop blocking a name, delete its line and let that pull land. The next query for that name is forwarded. Putting the line back blocks it again, even if a previous answer was cached.

`/etc/piadblock/allowlist.txt` is a separate override, same one-domain-per-line format. It is not in the git checkout. An allow entry beats a blocklist entry of the same name. A more specific block still wins: allow `good.com` but list `ads.good.com` in `blocklist.txt`, and `ads.good.com` stays blocked.

Allow one name that is still on the block list:

```bash
echo track.example.net | sudo tee -a /etc/piadblock/allowlist.txt
```

Expected output:

```text
track.example.net
```

The running server re-reads the allowlist within a few seconds. To reload immediately:

```bash
sudo systemctl reload piadblock
```

Expected output: no text, exit code 0. The journal then contains a `lists reloaded:` line. `track.example.net` is answered from upstream while it remains on `blocklist.txt`. A different name that is still listed, such as `doubleclick.net`, stays `0.0.0.0`.

### Remote lists later

Download stays off until you put `http://` or `https://` URLs in `"blocklists"` and run an update yourself. No timer is installed.

```bash
sudo python3 -m piadblock update
```

Run that from any directory after install; the unit's `PYTHONPATH` is not required for this command because the code lives in `/opt/piadblock` only if that directory is on `sys.path`. The reliable form is:

```bash
sudo PYTHONPATH=/opt/piadblock python3 -m piadblock update
```

With the default empty list, expected output is:

```text
blocklist is fresh (0 domains)
```

That means "there was nothing to download." After you add URLs, the same command merges them into `/var/lib/piadblock/blocklists/merged.txt`. The server picks up the new file on its own, or you can `sudo systemctl reload piadblock`.

Hosts files (`0.0.0.0 name`), plain domain lists, and Adblock `||name^` lines are accepted. `@@` exceptions and URL paths are ignored.

## Logs and stats

Query lines are on stdout (the journal). A rotating copy of the Python log, not the per-query lines, is also written to `/var/log/piadblock/piadblock.log`.

Saved counters:

```bash
sudo PYTHONPATH=/opt/piadblock python3 -m piadblock stats
```

Expected output, once the server has flushed counters (about once a second, and on shutdown):

```text
queries:   12
blocked:   4 (33.3%)
cached:    2
forwarded: 6
errors:    0
uptime:    120s
file:      /var/lib/piadblock/stats.json
```

That file is the server's state. The top-level `queries` and `blocked` fields are the counts since this process started. `today` is the current UTC day, and it is the document `piadblock-stats` writes. The timer runs every five minutes (`OnCalendar=*:0/5`), reads this file, and pushes `stats/YYYY-MM-DD.json` (on the Pi, `/opt/piadblock/stats/YYYY-MM-DD.json`). The write and the push take the same lock as `git pull`, so a pull cannot revert the file before it is committed. `blocked_domains` is every blocked name that day and its count, reloaded from the state file when the process starts, so a restart does not zero the day. `top_blocked` is that same map sorted by count, then by name. `upstream_errors` counts timeout, servfail, refused, socket, and truncation for that UTC day. The 40-line recent sample on the status page is not written here.

```json
{
  "date": "YYYY-MM-DD",
  "queries": 0,
  "blocked": 0,
  "blocked_domains": {"doubleclick.net": 2},
  "top_blocked": [{"domain": "doubleclick.net", "count": 2}],
  "upstream_errors": {"timeout": 0, "servfail": 0, "refused": 0, "socket": 0, "truncation": 0}
}
```

Compare the live UTC day with the file that was just pushed:

```bash
curl -s http://127.0.0.1:8080/api/stats
sudo systemctl start piadblock-stats.service
python3 -c 'import json; print(json.load(open("/var/lib/piadblock/stats.json"))["today"])'
cat /opt/piadblock/stats/$(date -u +%F).json
```

`today` in the API, the `today` object in `/var/lib/piadblock/stats.json`, and `stats/YYYY-MM-DD.json` are the same counts. The cards labeled "since start" keep growing across UTC midnight and are a different total.

`heartbeat.json` at the repo root is overwritten every hour and pushed:

```json
{"timestamp": "YYYY-MM-DDTHH:MM:SSZ", "uptime_seconds": 0, "version": "0.1.0"}
```

The Pi commits only `heartbeat.json` and `stats/`. `blocklist.txt` is edited on GitHub. Pull and push share one lock and retry a failed fetch or push (a dropped SSH connection, a leftover `.git/index.lock`, or a lookup of `github.com` that got no answer) with a short backoff. The journal line includes git's exit code and the full stderr.

A pull that starts on the minute and fails about 10 seconds later, while a successful pull finishes in 2–3 seconds, is the C library resolver waiting out its default. That default is a 5 second timeout and 2 attempts. SSH's `ConnectTimeout` does not cover the name lookup, and OpenSSH leaves it unlimited until a command sets it. `github.com` is resolved with `/etc/resolv.conf`. When that file points at this Pi, or at one upstream that is not answering, the lookup sits for those 10 seconds and git reports that it could not read the remote. The git process sets `RES_OPTIONS=timeout:1 attempts:1`. SSH is started with `ConnectTimeout=5` and `ConnectionAttempts=1`. When `~/.ssh/config` already has a `Host github.com` key, that file still chooses the key.

```bash
cat /etc/resolv.conf
```

A file written by the installer lists the upstream nameservers and `options timeout:1 attempts:1 rotate edns0`.

```text
warning: git pull failed (exit 128):
fatal: Could not read from remote repository.

Please make sure you have the correct access rights
and the repository exists.
Blocking is unchanged.
```

```bash
journalctl -u piadblock-pull -n 50 --no-pager
```

Check one name against the files on disk, without sending a query:

```bash
sudo PYTHONPATH=/opt/piadblock python3 -m piadblock check doubleclick.net
```

Expected output:

```text
doubleclick.net
blocked
reason: denylist (doubleclick.net)
lists: 91526 blocklist, 0 allowlist
```

The count is however many names `blocklist.txt` loaded, so it moves when that file does.

```bash
sudo PYTHONPATH=/opt/piadblock python3 -m piadblock check example.com
```

Expected output:

```text
example.com
allowed
reason: not listed
lists: 91526 blocklist, 0 allowlist
```

## Uninstall

```bash
sudo python3 /opt/piadblock/uninstall.py
```

This stops and disables the DNS service and the pull, heartbeat, and stats timers, removes those unit files, and restores `/etc/resolv.conf` and the systemd-resolved stub listener if the installer changed them. Config, the allowlist, and logs are kept. The git checkout under `/opt/piadblock` is removed.

Expected output when the installer had changed resolved and resolv.conf:

```text
piadblock uninstall
stopped piadblock.service
removed /etc/systemd/system/piadblock.service
restored /etc/resolv.conf
restored systemd-resolved DNSStubListener
removed /opt/piadblock
kept /etc/piadblock (run uninstall.py --purge to remove config, lists, and logs)
done
```

If `dnsmasq` was disabled at install time, you also get:

```text
dnsmasq was disabled by piadblock and was not started again.
To turn it back on: sudo systemctl enable --now dnsmasq
```

Delete config, lists, and logs too:

```bash
sudo python3 /opt/piadblock/uninstall.py --purge
```

Run that **before** a normal uninstall if you still need the script under `/opt/piadblock`. `--purge` also removes `/opt/piadblock` when you are not running the script from inside it. If the script is already gone, the copies under `/etc/piadblock` can be removed with:

```bash
sudo rm -rf /etc/piadblock /var/lib/piadblock /var/log/piadblock
```

Expected output: no text.

## Capacity benchmark

The benchmark builds synthetic lists of 100000, 250000, and 500000 names, starts the real server on `127.0.0.1` port `15353`, and measures cold load time, reload time, memory, and DNS latency while one client streams queries during the reload. It does not bind port 53, does not read the installed block list, and deletes its temporary files when it finishes. sudo is not required.

On the Pi, after this version is installed:

```bash
python3 /opt/piadblock/tools/benchmark.py
```

Expected output has this shape. The three `measuring` lines are progress. The table follows. `cold_s` is how long it takes to build the matcher before the server listens. `peak_mb` is `VmHWM` and `rss_mb` is `VmRSS` after the reload. `p50_ms` is a blocked name queried while that reload is parsing. `fwd_p50` is forwarded queries against the local stub while the server is idle, taken before the reload, and should sit near that stub's round trip (well under 10 ms). The recommendation is the largest size whose reload p95 stayed under 50 ms, with no timeouts, and whose peak RSS stayed under 25% of RAM (MemTotal on a Pi, or 512 MB of an assumed 2 GB Pi 4 on any other machine).

```text
measuring 100000 domains...
measuring 250000 domains...
measuring 500000 domains...
piadblock capacity benchmark
host: <this machine>
MemTotal: <MB> MB
rss budget: <MB> MB (...)
p95 limit during reload: 50 ms
query timeout: 1000 ms, one client streaming queries during the reload
upstream: local stub on 127.0.0.1 (latency is the blocker, not 1.1.1.1)
port 53 is not used; the live service is not modified
cold_s is the time to build the matcher before the server listens
peak_mb is VmHWM; rss_mb is VmRSS after reload
p50_ms is blocked-name latency while that reload is parsing
fwd_p50 is idle forwarded latency against the stub, before the reload

    size    cold_s  reload_s    rss_mb   peak_mb    p50_ms    p95_ms    max_ms   fwd_p50  timeouts   queries
  100000   <cold>   <reload>   <rss>    <peak>    <p50>    <p95>    <max>    <fwd>         0    <count>
  250000   ...
  500000   ...

recommended max list size: <domains> domains
reason: largest measured size with reload p95 <p95> ms under 50 ms, zero timeouts, and peak RSS <peak> MB under <budget> MB
```

From a checkout, the same script is `python3 tools/benchmark.py` in the repo root. Same output.

### Reference measured on a cloud VM (not a Raspberry Pi)

Python 3.11, x86_64, MemTotal 16003 MB. These numbers are not from a Pi. Latency on a Pi 4 will be different. The script still judged RSS against a 512 MB budget, which is 25% of an assumed 2 GB Pi 4.

```text
measuring 100000 domains...
measuring 250000 domains...
measuring 500000 domains...
piadblock capacity benchmark
host: not a Raspberry Pi (x86_64, Linux)
MemTotal: 16003 MB
rss budget: 512 MB (25% of 2048 MB, assumed for a Pi 4 because this host is not a Pi)
p95 limit during reload: 50 ms
query timeout: 1000 ms, one client streaming queries during the reload
upstream: local stub on 127.0.0.1 (latency is the blocker, not 1.1.1.1)
port 53 is not used; the live service is not modified
cold_s is the time to build the matcher before the server listens
peak_mb is VmHWM; rss_mb is VmRSS after reload
p50_ms is blocked-name latency while that reload is parsing
fwd_p50 is idle forwarded latency against the stub, before the reload

    size    cold_s  reload_s    rss_mb   peak_mb    p50_ms    p95_ms    max_ms   fwd_p50  timeouts   queries
  100000     0.404     0.438      42.4      58.8      3.19      4.51      5.80      0.20         0       154
  250000     1.056     1.112      58.9      99.3      3.28      4.41     12.22      0.14         0       412
  500000     2.069     2.245      85.3     170.0      3.26      4.46     23.62      0.18         0       836

recommended max list size: 500000 domains
reason: largest measured size with reload p95 4.46 ms under 50 ms, zero timeouts, and peak RSS 170.0 MB under 512 MB
note: these numbers are from this machine, not from a Raspberry Pi. Latency will be different on a Pi. Run the same command there.
```

On that VM, idle forwarded p50 against the stub was 0.14–0.20 ms. Reload p50 stayed near 3.3 ms at each size. Peak RSS at 500000 names was 170 MB. Run the command above on the Pi before treating 500000 as the list size to ship.

## Tests

From a checkout of this repository:

```bash
python3 -m unittest discover -s tests
```

This runs the standard-library test suite: DNS packet parsing, block decisions, the cache, a local server that blocks, forwards, and prints lookup lines, the CRT frame (highlighting, truncation, and order), a bootstrap install into a temporary directory, and a tiny run of the capacity benchmark. It does not need root and it does not bind port 53.

Expected output ends with:

```text
----------------------------------------------------------------------
Ran 129 tests in 59.354s

OK (skipped=1)
```

The elapsed time varies. `OK` is the result that matters. One test reads `/tmp/stevenblack.hosts` when that file is on the machine; without the file, that test is skipped.

## What the server does

1. A client sends a DNS query to the Pi on UDP port 53 (TCP is accepted too, for large answers).
2. Local names and private-address reverse lookups are forwarded. A built-in safety name (Google, Apple, GitHub, Cloudflare, Microsoft, and the related parents above) is forwarded too, except `mask.icloud.com` and `mask-h2.icloud.com`.
3. If the name, or a parent of the name, is on `blocklist.txt` and not covered by a more specific allow entry, the Pi answers immediately: A is `0.0.0.0`, AAAA is `::`, other types are an empty NOERROR. The line in the journal starts with `BLOCKED`.
4. Otherwise the Pi returns a cached answer or forwards the query to `1.1.1.1`, then `1.0.0.1`, then `9.9.9.9`. The journal line starts with `OK` when one of them answers. A reload builds the next list off to the side and yields while it parses, so this step keeps running while a large file is loading. A failed reload keeps the list that was already answering.

Other query types for a blocked name are not forwarded, so a client cannot skip the block by asking for a TXT or HTTPS record.
