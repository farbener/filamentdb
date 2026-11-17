# FilamentDB

Small self-hosted filament inventory with barcode scanning and exports for
HueForge and Bambu Studio. Python standard library + SQLite, no build step.

## How updates work

```
git push  ->  GitHub Actions builds ghcr.io/farbener/filamentdb:latest
          ->  Watchtower on TrueNAS sees the new image (every 5 min)
          ->  filamentdb restarts with the new version, data stays in /data
```

## One-time setup

1. Push this folder to `github.com/farbener/filamentdb` (branch `main`).
   The first push triggers the build (Actions tab).
2. Make the package public so TrueNAS can pull it without login:
   GitHub -> your profile -> Packages -> filamentdb -> Package settings ->
   Change visibility -> Public.
3. On TrueNAS create the dataset `POOL/apps/filamentdb/data` and run:
   ```sh
   chown -R 568:568 /mnt/POOL/apps/filamentdb/data
   ```
4. Apps -> Discover Apps -> ⋮ -> **Install via YAML**, paste `compose.yaml`,
   replace `POOL` and set `APP_PASSWORD`.
5. Open `http://truenas:8099`. On first start the database is filled from
   `seed.json`.

## HTTPS (required for the phone camera)

Browsers only allow camera access over HTTPS. Pick one:

- **Reverse proxy** you already run (Nginx Proxy Manager, Caddy, Traefik):
  proxy `filament.yourdomain` -> `truenas:8099` with a valid certificate.
- **Tailscale**: `tailscale serve --bg 8099` on a host that can reach it gives
  `https://<host>.<tailnet>.ts.net`. Your father needs to be in the tailnet.
- **Self-signed (LAN only)**: set `TLS_SELF_SIGNED: "1"` and
  `TLS_HOSTS: "192.168.1.161,truenas"` (your server's IP and/or name). The app
  creates a certificate in `data/tls` on first start. Then open
  `https://<ip>:8099` (plain http stops working on that port). Each phone shows
  a certificate warning once: choose Advanced -> Proceed. Change `TLS_HOSTS`
  and the certificate is recreated.
- **Direct TLS**: mount a cert and set `TLS_CERT=/certs/fullchain.pem` and
  `TLS_KEY=/certs/privkey.pem`.

Without HTTPS everything works except the camera; barcodes can be typed in.

## Usage

- **Scan**: known barcode -> opens the entry (spool count +1 prefilled).
  Unknown barcode -> new entry. Brand/material/temps are guessed from other
  barcodes of the same manufacturer (same 7-digit prefix). You can also attach
  the barcode to an existing entry without one (e.g. the seeded spools).
- **Spools 0** keeps the entry but hides it and leaves it out of exports.
- **TD**: enter the HueForge Transmission Distance. Only entries with TD are
  exported to HueForge.

## Automatic lookup for new barcodes (optional)

When you scan an unknown barcode, the app looks for product data and fills
the **empty** form fields with a suggestion (brand, material, colour, hex,
weight, temperatures). You always confirm before saving, and **TD is never
filled in** because it has to be measured.

1. A free barcode database is asked first (only filament-like titles count).
2. If `ANTHROPIC_API_KEY` is set, Claude searches the web for the product.
   Needs an API key from console.anthropic.com (billed separately from claude.ai
   plans) with web search enabled. Set it in the TrueNAS YAML, or put it alone in `data/anthropic.key` (read on every lookup). Never put it in
   this repo. `LOOKUP_LIMIT_PER_HOUR` (default 30) caps the cost.

Without a key only step 1 runs. Only the barcode number is sent out.

## Exports

| Export | Import |
|---|---|
| `HueForge library` (JSON) | HueForge: Filaments -> Import |
| `Bambu Studio presets` (ZIP) | Unzip, Bambu Studio: File -> Import -> Import Configs |
| `CSV backup` | Spreadsheet / backup |

Bambu Studio gets one user preset per brand + material (colours are set per
AMS slot). Presets inherit from `Generic <material> @base` and set vendor,
type and nozzle/bed temperatures (midpoint of the label range).

## Backup

Everything is in `/mnt/POOL/apps/filamentdb/data/filaments.db`. Include the
dataset in your snapshot task.
