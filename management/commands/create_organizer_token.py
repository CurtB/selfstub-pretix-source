"""
Custom Django management command: create_organizer_token

Creates a Pretix organizer with an "API Access" team and an API token.
This bypasses the REST API (which has no organizer creation endpoint) and
uses the Django ORM directly.

Usage:
    python -m django create_organizer_token --slug tenant-a --name "Tenant A Events"
    python -m django create_organizer_token --slug tenant-a --name "..." --check-only

Output (JSON on stdout, logs on stderr):
    {"organizer_slug": "tenant-a", "token": "abc123...", "created": true, "token_exists": true}

Idempotent:
- If the organizer doesn't exist: creates organizer + team + token.
- If the organizer exists but the token is missing: creates the token.
- If both exist: returns the existing token value.
- With --check-only: reports state without creating anything (token is null
  if it can't be recovered — Pretix only shows tokens once at creation).
"""

import json

from django.core.management.base import BaseCommand
from pretix.base.models import Organizer, Team
from pretix.base.models.organizer import TeamAPIToken


class Command(BaseCommand):
    help = "Create a Pretix organizer with an API Access team and token"

    def add_arguments(self, parser):
        parser.add_argument("--slug", required=True, help="Organizer slug")
        parser.add_argument("--name", required=True, help="Organizer display name")
        parser.add_argument(
            "--token-name", default="onboarding-cli", help="Name for the API token"
        )
        parser.add_argument(
            "--check-only",
            action="store_true",
            default=False,
            help="Report state without creating anything",
        )

    def handle(self, *args, **options):
        slug = options["slug"]
        name = options["name"]
        token_name = options["token_name"]
        check_only = options["check_only"]

        # Check if organizer already exists
        organizer = Organizer.objects.filter(slug=slug).first()

        if check_only:
            # Read-only mode: report state without creating
            token = None
            token_exists = False
            if organizer:
                existing = TeamAPIToken.objects.filter(
                    team__organizer=organizer, name=token_name, active=True
                ).first()
                token = existing.token if existing else None
                token_exists = existing is not None
            result = {
                "organizer_slug": slug,
                "organizer_exists": organizer is not None,
                "token": token,
                "token_exists": token_exists,
            }
            self.stdout.write(json.dumps(result))
            return

        if organizer:
            # Organizer exists — check if an active token exists, create if missing
            existing_token = TeamAPIToken.objects.filter(
                team__organizer=organizer, name=token_name, active=True
            ).first()

            if existing_token:
                result = {
                    "organizer_slug": organizer.slug,
                    "token": existing_token.token,
                    "created": False,
                    "token_exists": True,
                }
            else:
                # Token was deleted — create a new one on the existing
                # "API Access" team (or create the team if it's missing too)
                team = Team.objects.filter(
                    organizer=organizer, name="API Access"
                ).first()
                if not team:
                    team = Team.objects.create(
                        organizer=organizer,
                        name="API Access",
                        all_events=True,
                        all_event_permissions=True,
                        all_organizer_permissions=True,
                    )
                token = TeamAPIToken.objects.create(
                    team=team, name=token_name, active=True
                )
                result = {
                    "organizer_slug": organizer.slug,
                    "token": token.token,
                    "created": False,
                    "token_exists": True,
                    "token_recreated": True,
                }
            self.stdout.write(json.dumps(result))
            return

        # Create organizer
        organizer = Organizer.objects.create(slug=slug, name=name)

        # Create "API Access" team with full permissions.
        # CONSTRAINT: all_organizer_permissions=True covers
        # can_change_organizer_settings, which the dashboard's Layer 3 Pretix
        # health card needs to GET /organizers/{org}/webhooks/. Any future
        # least-privilege refactor that replaces this flag with an explicit
        # allow-list MUST retain can_change_organizer_settings, otherwise the
        # health card will 403 and falsely report webhookConfigured: false.
        team = Team.objects.create(
            organizer=organizer,
            name="API Access",
            all_events=True,
            all_event_permissions=True,
            all_organizer_permissions=True,
        )

        # Create API token
        token = TeamAPIToken.objects.create(team=team, name=token_name, active=True)

        result = {
            "organizer_slug": organizer.slug,
            "token": token.token,
            "created": True,
            "token_exists": True,
        }
        self.stdout.write(json.dumps(result))
