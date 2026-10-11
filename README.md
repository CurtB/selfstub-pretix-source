# selfstub-pretix-source

Source-code offer under **GNU AGPLv3 §13** for the pretix-based ticket shop at
<https://tickets.selfstub.com>, operated by Selfstub.

## What runs

The ticketing backend is [pretix](https://pretix.eu) **Community Edition**,
pinned to upstream release **2026.8.0** — the stock
`pretix/standalone:2026.8.0` container image with **no modifications to pretix
itself**.

- Upstream source: <https://github.com/pretix/pretix> (tag `2026.8.0`)
- Upstream license: AGPLv3 with additional terms — see
  <https://github.com/pretix/pretix/blob/master/LICENSE>

## What this repository contains

The Selfstub-specific additions mounted into that stock image, published so
that anyone interacting with the shop over the network can obtain the source
of the running software:

- `admin_server.py` — internal organizer-settings write channel (mounted at
  `/opt/selfstub/admin_server.py`). Used by the platform CLI via
  `docker exec --stdin` and by the web dashboard over a private-network HTTP
  shim; manages organizer-level `payment_stripe_*` settings that pretix's
  REST API does not expose, and the instance-wide shop-footer source link.
- `management/commands/create_organizer_token.py` — Django management command
  for organizer + API-token provisioning (mounted into
  `pretix/base/management/commands/`).
- `pretix.cfg`, `pretix.prod.cfg`, `pretix.env.example` — configuration
  mounted at `/etc/pretix/pretix.cfg`. Secrets are environment-only and are
  never committed.

The rest of the platform (Next.js frontend, Strapi CMS, media worker)
communicates with pretix over its public REST API and is not part of the
pretix covered work.

## License

These files are licensed under **GNU AGPLv3** (see `LICENSE`), matching
pretix's licensing. "pretix" is a trademark of ramirotech / pretix GmbH; this
repository is not an authorized or official pretix distribution.
