# Quadlet deployment units (Podman)

Copy-ready Quadlet definitions for the three production components.
Install them into `~/.config/containers/systemd/` on the host and run
`systemctl --user daemon-reload`.

| File | What it runs | Network | Exposed on host |
| --- | --- | --- | --- |
| `scraper-api.container` | The scraper API (existing standalone unit, `Network=host`) | host | API port (e.g. 8080) |
| `scraper-api-full.container` | The scraper API with external SearXNG + Camofox enabled | `scraper-network` | API port only |
| `searxng.container` | [SearXNG](https://docs.searxng.org) metasearch | `scraper-network` | **none** (private) |
| `camofox.container` | Camofox browser service | `scraper-network` | **none** (private) |
| `scraper-api-network.network` | Shared private bridge network | — | — |
| `config/searxng-settings.yml` | SearXNG `settings.yml` (JSON output enabled) | — | — |

## Networking

All three containers join the private `scraper-network`. The scraper
resolves SearXNG and Camofox by **container name** DNS
(`http://searxng:8080`, `http://camofox:9377`).  SearXNG and Camofox
publish **no** host ports — the scraper API is the only externally
consumed facade.  Do not add `Port=` lines to the two backend units
unless you explicitly require public exposure.

The two scraper units are alternatives:

- `scraper-api.container` (host networking) — standalone deployment;
  with host networking the base URLs must be `http://127.0.0.1:8080` /
  `http://127.0.0.1:9377` and the two backends must be reachable on the
  host (e.g. separate `Port=` publications or their own host listeners).
- `scraper-api-full.container` (shared bridge network) — the normal
  three-service deployment; only the API port is published.

Host-side layout:

```text
~/.config/containers/systemd/
  scraper-api-full.container     (or scraper-api.container)
  searxng.container
  camofox.container
  scraper-api-network.network

~/scraper-api/
  configs/config.yaml            # copied from configs/config.example.yaml
  data/                          # cache volume
  debug/
  logs/
  .env                           # secrets (0600)
  searxng-settings.yml           # copied from remote/config/
```

## Secrets

Create `~/scraper-api/.env` (mode `0600`) — never commit real values:

```dotenv
SCRAPER_API_KEY=$SCRAPER_API_KEY
CAMOFOX_ACCESS_KEY=$CAMOFOX_ACCESS_KEY
CAMOFOX_API_KEY=$CAMOFOX_API_KEY
```

Generate values with `openssl rand -hex 32`.  The config file references
them via `${CAMOFOX_ACCESS_KEY}` expansion (unreferenced/unset variables
are treated as *unset* and produce a warning at startup — the placeholder
is never sent on the wire).  The `camofox` container reads the same file
for its own listener credentials.

## Full deployment (three services)

```bash
mkdir -p ~/scraper-api/{configs,data,debug,logs}
cp remote/scraper-api-full.container remote/searxng.container \
   remote/camofox.container remote/scraper-api-network.network \
   ~/.config/containers/systemd/
cp remote/config/searxng-settings.yml ~/scraper-api/searxng-settings.yml
cp configs/config.example.yaml ~/scraper-api/configs/config.yaml
chmod 600 ~/scraper-api/.env   # create first — see Secrets above

# Build the scraper image locally (or set Image= to the GHCR tag and
# let deploy.sh pull it).
podman build -t localhost/scraper-api:latest .

systemctl --user daemon-reload
systemctl --user start scraper-api-network
systemctl --user start searxng
systemctl --user start camofox
systemctl --user start scraper-api
```

Enable on boot with `systemctl --user enable <unit>` for each unit.

## Standalone deployment (no external backends)

Keep `scraper-api.container` as today and leave `search.providers.*.enabled`
and `camofox.enabled` false in the config — the service runs with plain
HTTP/Botasaurus scraping and no search.  This path is unchanged and
remains the minimal deployment.
