# Nginx reverse proxy (optional)

Gives each service on a machine a clean subdomain URL instead of a port number, and an
auto-generated index page on the bare domain that links to all of them.

```
Browser -> LAN DNS -> nginx (port 80) -> backend service (port N)
```

## Services file

One `.services` file per machine; `example.services` is the template:

```
# domain: example.internal
dashboard 8080
```

`sudo sh install.sh <file>` reads it and generates the nginx config and index page.

## Adding or changing a service

1. Add a line to your `.services` file: `myapp 9000`
2. Re-run: `sudo sh install.sh myhost.services`

No DNS change needed: the wildcard record below already covers every subdomain.

## Installation

### One-time manual step: DNS records

In your LAN resolver (AdGuard Home: **Filters > DNS Rewrites**; Pi-hole: Local DNS) point
both the bare domain and its wildcard at the machine:

| Domain               | Answer          |
| -------------------- | --------------- |
| `example.internal`   | the host's LAN IP |
| `*.example.internal` | the host's LAN IP |

### Linux (Debian/Ubuntu)

```sh
sudo sh install.sh myhost.services              # install or update
sudo sh install.sh myhost.services --uninstall   # remove
```

### OpenWrt

```sh
sh install.sh myrouter.services              # install or update
sh install.sh myrouter.services --uninstall   # remove, restores uhttpd to port 80
```

On OpenWrt the script moves uhttpd (LuCI) from port 80 to 8081 so nginx can take port 80.
Automatic uninstall supports a dedicated nginx installation with the stock UCI
`_lan`/`_redirect2ssl` servers and nginx-util 1.2 template, with this services file
as its only custom site. It stops and disables nginx (including its packaged
80/443 listeners), restores uhttpd, and verifies a temporary file through uhttpd
before removing the generated configuration and `index.html`. Other files in
the index directory are kept. A failed transition restores the previous setup;
incomplete recovery leaves backups at the path printed by the installer.

Manual configurations, extra sites or location files, and changed or older
unrecognized templates are refused before any changes. For those setups, first
arrange which server will own ports 80/443: preserve nginx for its other sites,
or stop/disable it and verify uhttpd after restoring its listeners. Then remove
only this services file's generated configuration and index. Do not restart
nginx with remaining 80/443 listeners while assigning those ports to uhttpd.

`sh install.sh --help` prints the full usage.
