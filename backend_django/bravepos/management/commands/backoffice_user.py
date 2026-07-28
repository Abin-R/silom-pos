"""Create, list, and reset backoffice web logins from the shell.

The Users page in the backoffice is the normal way to do this. This command
exists for the cases that page can't cover: bootstrapping the first admin on a
fresh deploy, and getting back in when nobody can sign in.

    python manage.py backoffice_user --list
    python manage.py backoffice_user --create data --email data@therollingpinn.com --name "Data Team"
    python manage.py backoffice_user --create admin --name "Owner" --role admin
    python manage.py backoffice_user --reset data
    python manage.py backoffice_user --revoke olduser

Passwords are generated (14 chars) unless ``--password`` is given, and printed
once. Nothing stores the plaintext.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q

from bravepos.models import Staff

ALPHABET = "abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def generate_password(length: int = 14) -> str:
    import secrets

    return "".join(secrets.choice(ALPHABET) for _ in range(length))


class Command(BaseCommand):
    help = "Manage backoffice web logins (username or email + password)."

    def add_arguments(self, parser):
        parser.add_argument("--list", action="store_true",
                            help="Show every account with backoffice access.")
        parser.add_argument("--create", metavar="USERNAME",
                            help="Create an account with this username.")
        parser.add_argument("--reset", metavar="USERNAME_OR_EMAIL",
                            help="Generate a new password for an account.")
        parser.add_argument("--revoke", metavar="USERNAME_OR_EMAIL",
                            help="Remove backoffice access (keeps the POS PIN login).")
        parser.add_argument("--email", default="", help="Email sign-in identifier.")
        parser.add_argument("--name", default="", help="Display name.")
        parser.add_argument("--role", default="cashier", choices=["admin", "cashier"],
                            help="admin unlocks Users + Audit Log. Default: cashier.")
        parser.add_argument("--password", default="",
                            help="Set an explicit password instead of generating one.")

    def handle(self, *args, **options):
        if options["list"]:
            return self._list()
        if options["create"]:
            return self._create(options)
        if options["reset"]:
            return self._reset(options)
        if options["revoke"]:
            return self._revoke(options)
        raise CommandError("Pick one of --list / --create / --reset / --revoke.")

    # ── helpers ──
    def _find(self, identifier: str) -> Staff:
        staff = Staff.objects.filter(
            Q(username__iexact=identifier) | Q(email__iexact=identifier)
        ).first()
        if staff is None:
            raise CommandError(f"No account matches “{identifier}”.")
        return staff

    def _print_credentials(self, staff: Staff, password: str, heading: str):
        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS(heading))
        self.stdout.write(f"  Name:     {staff.name}")
        if staff.username:
            self.stdout.write(f"  Username: {staff.username}")
        self.stdout.write(f"  Email:    {staff.email}")
        self.stdout.write(f"  Password: {password}")
        self.stdout.write(f"  Role:     {staff.role}")
        self.stdout.write("")
        self.stdout.write(self.style.WARNING(
            "  Shown once — nothing stores the plaintext. Send it privately and "
            "have the user change it after first sign-in."
        ))
        self.stdout.write("")

    # ── actions ──
    def _list(self):
        users = Staff.objects.filter(backoffice_access=True).order_by("-role", "name")
        if not users:
            self.stdout.write("No accounts have backoffice access.")
            return
        self.stdout.write(f"{'USERNAME':<20}{'EMAIL':<40}{'ROLE':<10}{'ACTIVE':<8}LAST SIGN-IN")
        for staff in users:
            last = staff.last_login_at.strftime("%Y-%m-%d %H:%M") if staff.last_login_at else "never"
            self.stdout.write(
                f"{(staff.username or '—'):<20}{staff.email:<40}"
                f"{staff.role:<10}{('yes' if staff.active else 'no'):<8}{last}"
            )

    def _create(self, options):
        username = options["create"].strip()
        email = options["email"].strip()
        name = options["name"].strip() or username

        if Staff.objects.filter(Q(username__iexact=username) | Q(email__iexact=username)).exists():
            raise CommandError(f"“{username}” is already taken.")
        if email and Staff.objects.filter(Q(email__iexact=email) | Q(username__iexact=email)).exists():
            raise CommandError(f"“{email}” is already taken.")

        password = options["password"] or generate_password()
        if len(password) < 10:
            raise CommandError("Password must be at least 10 characters.")

        staff = Staff(
            name=name,
            username=username,
            # Email is a required unique column; synthesise a non-routable
            # placeholder when the account signs in by username only.
            email=email or f"{username}@users.noreply.rollingpinn.com",
            role=options["role"],
            active=True,
            backoffice_access=True,
        )
        staff.set_password(password)
        staff.save()
        self._print_credentials(staff, password, "Created backoffice user")

    def _reset(self, options):
        staff = self._find(options["reset"].strip())
        password = options["password"] or generate_password()
        if len(password) < 10:
            raise CommandError("Password must be at least 10 characters.")
        staff.set_password(password)
        if not staff.backoffice_access:
            staff.backoffice_access = True
            self.stdout.write(self.style.WARNING(
                "  (backoffice access was off for this account — turned it back on)"
            ))
        staff.save()
        self._print_credentials(staff, password, "Password reset")

    def _revoke(self, options):
        staff = self._find(options["revoke"].strip())
        staff.backoffice_access = False
        staff.username = None
        staff.set_password(generate_password(32))  # unusable, not blank
        staff.save()
        self.stdout.write(self.style.SUCCESS(
            f"Revoked backoffice access for {staff.name} <{staff.email}>. "
            "Their POS PIN login is unaffected."
        ))
