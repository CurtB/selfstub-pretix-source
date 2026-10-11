"""
Pretix organizer payment-settings management channel.

One write path, two transports:

- ``--stdin``: reads a single JSON request on stdin, writes a single JSON
  response on stdout. Used by ``apps/cli`` via
  ``docker exec -i <pretix> python3 /opt/selfstub/admin_server.py --stdin``
  (no auth — docker exec is host-level access already).
- default (HTTP serve): a small internal HTTP service on the private Docker
  network. Used by the Next.js dashboard server actions, which cannot
  ``docker exec``. Requires ``Authorization: Bearer $PRETIX_ADMIN_TOKEN``.

Why this exists: ``payment_stripe_*`` fields are NOT in Pretix's REST settings
serializers (whitelisted ``default_fields``) — a settings PATCH returns 200
but silently ignores them (verified on 2026.8.0). Organizer-level
``organizer.settings.set`` is the supported write path; it cascades to every
event via hierarkey. Every write is verified by re-reading state — never
trust a write that wasn't re-read.

Request JSON::

    {"action": "status",          "organizer": "<slug>"}
    {"action": "set",             "organizer": "<slug>",
     "set": {"payment_stripe_*": <value>}, "unset": ["payment_stripe_*"]}
    {"action": "clear",           "organizer": "<slug>"}
    {"action": "migrate_events",  "organizer": "<slug>",
     "dry_run": false, "enable_stripe_plugin": true}
    {"action": "set_source_link", "label": "<text>", "url": "https://..."}

``set_source_link`` is global, not organizer-scoped: it sets the instance-wide
``footer_text``/``footer_link`` settings so every shop page carries the
AGPLv3 §13 source-code link required by our CE license posture (ADR 0008).

Response JSON: ``{"ok": true, "result": {...}}`` or ``{"ok": false, "error":
"<code>"}``. Secret values are never echoed — status reports masked
fingerprints (last4) only.
"""

import hmac
import json
import os
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Organizer-level keys we manage. Values must be JSON scalars (str/bool/num).
ALLOWED_KEY = re.compile(r"^payment_stripe_[a-z0-9_]+$")

# Event-level credential keys moved to organizer level by migrate_events.
EVENT_CREDENTIAL_KEYS = ("payment_stripe_secret_key", "payment_stripe_publishable_key")

STRIPE_PLUGIN = "pretix.plugins.stripe"

# Validation for the global shop-footer source link (AGPLv3 §13 offer).
FOOTER_LABEL_MAX = 200
FOOTER_URL_RE = re.compile(r"^https://[^\s]{1,500}$")

ADMIN_PORT = int(os.environ.get("PRETIX_ADMIN_PORT", "8390"))
ADMIN_TOKEN = os.environ.get("PRETIX_ADMIN_TOKEN")


def _mask(key, value):
    """Return a safe report for a stored setting — never the raw value."""
    v = str(value)
    if key.endswith("_enabled") or key.startswith("payment_stripe_method_"):
        return {"set": True, "value": v.lower() in ("true", "1", "yes")}
    return {"set": True, "last4": v[-4:] if len(v) >= 4 else "****"}


def _org_state(org):
    """Masked fingerprint of every payment_stripe_* row on the organizer."""
    from pretix.base.models.organizer import Organizer_SettingsStore

    state = {}
    for row in Organizer_SettingsStore.objects.filter(
        object=org, key__startswith="payment_stripe_"
    ):
        # Read the deserialized value through the settings proxy, not the raw
        # (serialized) store column.
        state[row.key] = _mask(row.key, org.settings.get(row.key))
    return state


def _get_organizer(slug):
    from pretix.base.models import Organizer

    return Organizer.objects.filter(slug=slug).first()


def _status(org):
    return {"organizer": org.slug, "settings": _org_state(org)}


def _set(org, set_map, unset_keys):
    applied = []
    unset = []
    if set_map:
        for key, value in set_map.items():
            if not ALLOWED_KEY.match(key):
                return {"ok": False, "error": f"key_not_allowed:{key}"}
            if not isinstance(value, (str, bool, int, float)):
                return {"ok": False, "error": f"bad_value_type:{key}"}
        for key, value in set_map.items():
            org.settings.set(key, value)
            applied.append(key)
    for key in unset_keys or []:
        if not ALLOWED_KEY.match(key):
            return {"ok": False, "error": f"key_not_allowed:{key}"}
        # Delete only clears the organizer-local row; with no parent above the
        # organizer the key becomes unset (hierarkey __delitem__).
        try:
            del org.settings[key]
            unset.append(key)
        except KeyError:
            pass  # already unset — idempotent

    # Verify after writing — never trust a write that wasn't re-read.
    mismatched = []
    if set_map:
        org.settings.flush()
        for key, value in set_map.items():
            if org.settings.get(key) != value:
                mismatched.append(key)

    result = {
        "organizer": org.slug,
        "applied": sorted(applied),
        "unset": sorted(unset),
        "verified": not mismatched,
        "state": _org_state(org),
    }
    if mismatched:
        result["mismatched"] = sorted(mismatched)
    return result


def _clear(org):
    from pretix.base.models.organizer import Organizer_SettingsStore

    removed = []
    for row in Organizer_SettingsStore.objects.filter(
        object=org, key__startswith="payment_stripe_"
    ):
        try:
            del org.settings[row.key]
            removed.append(row.key)
        except KeyError:
            pass
    return {
        "organizer": org.slug,
        "removed": sorted(removed),
        "state": _org_state(org),
    }


def _migrate_events(org, dry_run, enable_stripe_plugin):
    """
    S1.5 retrofit: move event-level Stripe credential keys up to organizer
    level (they then cascade to every event) and delete the event-level
    leftovers. Optionally ensure the Stripe plugin is enabled on each event.

    Only the first event's credentials win if several events carry different
    keys — reported as conflicts for a human to resolve.
    """
    from pretix.base.models import Event
    from pretix.base.models.event import Event_SettingsStore

    events = []
    conflicts = []
    for ev in Event.objects.filter(organizer=org).order_by("pk"):
        entry = {"slug": ev.slug, "moved": [], "cleared": [], "plugin_enabled": False}

        # Event-level rows only (raw store — settings.get would also return
        # organizer-cascaded values, which we must NOT treat as leftovers).
        rows = {
            r.key: r.value
            for r in Event_SettingsStore.objects.filter(
                object=ev, key__in=EVENT_CREDENTIAL_KEYS
            )
        }

        for key in EVENT_CREDENTIAL_KEYS:
            if key not in rows:
                continue
            # Deserialize through the proxy — the raw store column is
            # hierarkey-serialized.
            value = ev.settings.get(key)
            org_value = org.settings.get(key)
            if org_value in (None, ""):
                if not dry_run:
                    org.settings.set(key, value)
                entry["moved"].append(key)
                clear = True
            elif org_value == value:
                # Redundant duplicate of the organizer value — safe to clear.
                clear = True
            else:
                # Event-level value DIFFERS from the organizer's — deleting it
                # would silently reroute that event's payments. Keep the row
                # and surface the conflict for a human to resolve.
                conflicts.append(f"{ev.slug}:{key}")
                clear = False
            if clear:
                if not dry_run:
                    try:
                        del ev.settings[key]
                    except KeyError:
                        pass
                entry["cleared"].append(key)

        if enable_stripe_plugin:
            current = set(filter(None, (ev.plugins or "").split(",")))
            if STRIPE_PLUGIN not in current:
                if not dry_run:
                    ev.plugins = ",".join(sorted(current | {STRIPE_PLUGIN}))
                    ev.save(update_fields=["plugins"])
                entry["plugin_enabled"] = True

        if entry["moved"] or entry["cleared"] or entry["plugin_enabled"]:
            events.append(entry)

    return {
        "organizer": org.slug,
        "dry_run": dry_run,
        "events": events,
        "conflicts": conflicts,
        "state": _org_state(org) if not dry_run else None,
    }


def _set_source_link(request):
    """
    Set the instance-wide presale footer link (GlobalSettingsObject
    ``footer_text`` + ``footer_link``) — our AGPLv3 §13 source-code offer,
    required because we run CE for third-party organizers (ADR 0008).
    Instance-wide, not organizer-scoped. Idempotent.
    """
    label = request.get("label")
    url = request.get("url")
    if not isinstance(label, str) or not 1 <= len(label) <= FOOTER_LABEL_MAX:
        return {"ok": False, "error": "bad_label"}
    if not isinstance(url, str) or not FOOTER_URL_RE.match(url):
        return {"ok": False, "error": "bad_url"}

    from pretix.base.settings import GlobalSettingsObject

    gs = GlobalSettingsObject()
    gs.settings.set("footer_text", label)
    gs.settings.set("footer_link", url)
    gs.settings.flush()

    # Re-read-verify, same discipline as the payment writes.
    state = {
        "footer_text": str(gs.settings.get("footer_text") or ""),
        "footer_link": str(gs.settings.get("footer_link") or ""),
    }
    return {
        "state": state,
        "verified": state["footer_text"] == label and state["footer_link"] == url,
    }


def handle(request):
    """Dispatch a request dict → response dict. Never raises secrets."""
    from django_scopes import scopes_disabled

    if not isinstance(request, dict):
        return {"ok": False, "error": "bad_request"}
    action = request.get("action")

    if action == "set_source_link":
        with scopes_disabled():
            result = _set_source_link(request)
        return result if result.get("ok") is False else {"ok": True, "result": result}

    slug = request.get("organizer")
    if not slug or not isinstance(slug, str):
        return {"ok": False, "error": "missing_organizer"}

    with scopes_disabled():
        org = _get_organizer(slug)
        if org is None:
            return {"ok": False, "error": "unknown_organizer"}

        try:
            if action == "status":
                result = _status(org)
            elif action == "set":
                result = _set(org, request.get("set") or {}, request.get("unset"))
                if result.get("ok") is False:
                    return result
            elif action == "clear":
                result = _clear(org)
            elif action == "migrate_events":
                result = _migrate_events(
                    org,
                    dry_run=bool(request.get("dry_run")),
                    enable_stripe_plugin=bool(request.get("enable_stripe_plugin")),
                )
            else:
                return {"ok": False, "error": "unknown_action"}
        except Exception as exc:  # surface a safe code, never internals
            return {"ok": False, "error": f"internal:{type(exc).__name__}"}

    return {"ok": True, "result": result}


def _log(line):
    print(f"[admin_server] {line}", file=sys.stderr, flush=True)


class _Handler(BaseHTTPRequestHandler):
    def _send(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # silence default access log (may echo paths)
        pass

    def _authorized(self):
        expected = f"Bearer {ADMIN_TOKEN}"
        return hmac.compare_digest(self.headers.get("Authorization") or "", expected)

    def do_GET(self):
        if self.path == "/healthz":
            return self._send(200, {"ok": True})
        self._send(404, {"ok": False, "error": "not_found"})

    def do_POST(self):
        if self.path != "/v1/organizer-payment-settings":
            return self._send(404, {"ok": False, "error": "not_found"})
        if not self._authorized():
            return self._send(401, {"ok": False, "error": "unauthorized"})
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > 64 * 1024:
                return self._send(400, {"ok": False, "error": "bad_request"})
            request = json.loads(self.rfile.read(length))
        except Exception:
            return self._send(400, {"ok": False, "error": "bad_request"})

        # Masked audit log: action + organizer + key names only — never values.
        if isinstance(request, dict):
            keys = sorted((request.get("set") or {}).keys()) + sorted(request.get("unset") or [])
            _log(
                f"action={request.get('action')} organizer={request.get('organizer')} keys={keys}"
            )
        response = handle(request)
        self._send(200 if response.get("ok") else 400, response)


def main():
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "production_settings")
    import django

    django.setup()

    if "--stdin" in sys.argv:
        try:
            request = json.load(sys.stdin)
        except Exception:
            json.dump({"ok": False, "error": "bad_request"}, sys.stdout)
            return
        json.dump(handle(request), sys.stdout)
        return

    if not ADMIN_TOKEN:
        _log("PRETIX_ADMIN_TOKEN is not set — refusing to serve")
        sys.exit(2)

    server = ThreadingHTTPServer(("0.0.0.0", ADMIN_PORT), _Handler)
    _log(f"serving organizer-payment-settings on :{ADMIN_PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
