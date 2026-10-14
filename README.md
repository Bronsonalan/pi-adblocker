# Pi Adblock

Pi Adblock is a small DNS server for a Raspberry Pi. It answers a fixed denylist with `0.0.0.0` (and `::` for IPv6) and forwards every other name to `1.1.1.1`. Clients that use the Pi for DNS stop reaching those ad domains, without any software on the phone or laptop.

v1 blocks these 10 names, and every subdomain under them:

- `doubleclick.net`
- `googlesyndication.com`
- `googleadservices.com`
- `adnxs.com`
- `adsrvr.org`
- `criteo.com`
- `taboola.com`
- `outbrain.com`
- `scorecardresearch.com`
- `amazon-adsystem.com`

So `doubleclick.net` and `ads.doubleclick.net` are both blocked. `example.com` and `google.com` are not. Nothing is downloaded. There is no daily blocklist update. You grow the list later by editing the denylist.

The program is plain Python 3 from the standard library. Raspberry Pi OS already has `python3`. Do not install packages with pip or apt for this.

## One-command install

On the Pi:

```bash
curl -fsSL https://raw.githubusercontent.com/Bronsonalan/pi-adblocker/main/bootstrap.sh | sudo bash
```

That downloads this repository, unpacks it, and runs the installer as root. The installer copies the program to `/opt/piadblock`, writes `/etc/piadblock`, installs `piadblock.service`, and starts it so it comes back on boot.

You should see `piadblock: downloading ...`, then `piadblock: running installer`, then the installer transcript in the next section. It finishes with `done`.

The same transcript is what you get from the step-by-step install below. Use that path if you want to run each command yourself.

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
- writes `/etc/piadblock/config.json` (upstream `1.1.1.1`)
- writes the allowlist and the 10-domain denylist
- does **not** download a blocklist
- frees port 53 if `systemd-resolved` or `dnsmasq` has it (see below)
- points the Pi's own resolver at `1.1.1.1` when `/etc/resolv.conf` currently points at localhost or the router, so the Pi does not loop once the LAN uses the Pi for DNS
- enables and starts `piadblock.service` (no update timer)

On a typical Raspberry Pi OS image, where `systemd-resolved` is holding port 53 and `/etc/resolv.conf` points at `127.0.0.53` or the router, expected output looks like this (`192.168.1.50` will be your Pi's address, and the `example.com` address will be whatever `1.1.1.1` returns that day):

```text
piadblock installer 0.1.0
python3: Python 3.11.2
installing files to /opt/piadblock
config: wrote /etc/piadblock/config.json
allowlist.txt: wrote /etc/piadblock/allowlist.txt
denylist.txt: wrote /etc/piadblock/denylist.txt
blocklists: none configured, download skipped
port 53: systemd-resolved is using port 53; disabling DNSStubListener
port 53: free
resolver: backed up /etc/resolv.conf
resolver: pointed this Pi at 1.1.1.1
systemd: installed piadblock.service
systemd: started piadblock.service
self-check: querying 127.0.0.1
self-check: doubleclick.net A -> 0.0.0.0
self-check: doubleclick.net AAAA -> ::
self-check: example.com A -> 172.66.147.243

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
- `resolver: /etc/resolv.conf already uses a public DNS server; leaving it` when the Pi already uses something like `1.1.1.1` or `8.8.8.8`.
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

`BLOCKED` is a denylist hit. `OK` was forwarded to `1.1.1.1` or served from the cache. `FAIL` means the upstream did not answer. The first field is the client's address, then the domain, then the query type.

Follow new lines as you browse:

```bash
journalctl -u piadblock -f
```

Ctrl-C stops following. Expected output is a stream of `BLOCKED` and `OK` lines, one per lookup.

Startup also logs the list sizes. With the default denylist and no downloaded list:

```text
lists: 0 blocklist, 0 allow, 10 deny
```

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

Expected output is JSON. `blocked` increases when a denylist name is queried. `blocklist_domains` stays `0` because no remote list is downloaded. Open `http://192.168.1.50:8080/` in a browser for the same numbers as a page.

The page has no password. It is meant for the LAN. To listen only on the Pi, set `"status_listen": "127.0.0.1"` in the config and restart.

## Configuration

The file is `/etc/piadblock/config.json`. Defaults:

- listen on `0.0.0.0` port `53` (UDP and TCP)
- forward to `1.1.1.1` port `53`
- answer blocked A queries with `0.0.0.0` and AAAA queries with `::`
- cache up to 2048 replies
- `"blocklists": []` and `"auto_update": false` so nothing is downloaded and no timer runs
- status page on port `8080`

Change the upstream, then restart:

```bash
sudo systemctl restart piadblock
```

Expected output: no text. `systemctl is-active piadblock` prints `active`.

A second upstream is used only when the first one fails. Example:

```json
"upstream": ["1.1.1.1", "1.0.0.1"]
```

### Denylist and allowlist

`/etc/piadblock/denylist.txt` is the block list. One domain per line. That name and every subdomain are blocked. Do not add a bare suffix such as `com`.

`/etc/piadblock/allowlist.txt` is the exception list, same format. An allow entry beats a deny entry of the same name. A more specific deny still wins: allow `good.com` but deny `ads.good.com`, and `ads.good.com` stays blocked.

Add a name:

```bash
echo track.example.net | sudo tee -a /etc/piadblock/denylist.txt
```

Expected output:

```text
track.example.net
```

The running server re-reads the file within a few seconds. To reload immediately:

```bash
sudo systemctl reload piadblock
```

Expected output: no text, exit code 0. The journal then contains:

```text
lists reloaded: 0 blocklist, 0 allow, 11 deny
```

```bash
nslookup track.example.net 192.168.1.50
```

Expected output includes `Address: 0.0.0.0`.

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

Expected output, once the server has flushed counters (about once a minute, and on shutdown):

```text
queries:   12
blocked:   4 (33.3%)
cached:    2
forwarded: 6
errors:    0
uptime:    120s
file:      /var/lib/piadblock/stats.json
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
lists: 0 blocked, 0 allowed, 10 denied
```

```bash
sudo PYTHONPATH=/opt/piadblock python3 -m piadblock check example.com
```

Expected output:

```text
example.com
allowed
reason: not listed
lists: 0 blocked, 0 allowed, 10 denied
```

## Uninstall

```bash
sudo python3 /opt/piadblock/uninstall.py
```

This stops and disables the service, removes the unit file, and restores `/etc/resolv.conf` and the systemd-resolved stub listener if the installer changed them. Config, the denylist, and logs are kept.

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

## Tests

From a checkout of this repository:

```bash
python3 -m unittest discover -s tests
```

This runs the standard-library test suite: DNS packet parsing, block decisions, the cache, and a local server that blocks, forwards, and prints lookup lines. It does not need root and it does not bind port 53.

Expected output ends with:

```text
----------------------------------------------------------------------
Ran 60 tests in 8.052s

OK
```

The elapsed time varies. `OK` is the result that matters.

## What the server does

1. A client sends a DNS query to the Pi on UDP port 53 (TCP is accepted too, for large answers).
2. If the name, or a parent of the name, is on the denylist and not covered by a more specific allow entry, the Pi answers immediately: A is `0.0.0.0`, AAAA is `::`, other types are an empty NOERROR. The line in the journal starts with `BLOCKED`.
3. Otherwise the Pi returns a cached answer or forwards the query to `1.1.1.1`, and the journal line starts with `OK`.

Other query types for a blocked name are not forwarded, so a client cannot skip the block by asking for a TXT or HTTPS record.
