"""Template hygiene checks.

Run:
    python manage.py test bravepos.tests.test_templates \
        --settings=bravepos_api.settings_test
"""
from __future__ import annotations

import re
from pathlib import Path

from django.test import TestCase

TEMPLATE_ROOTS = [
    Path(__file__).resolve().parent.parent.parent / "backoffice" / "templates",
    Path(__file__).resolve().parent.parent / "templates",
]


class TemplateCommentTests(TestCase):
    def test_no_multiline_hash_comments(self):
        """`{# … #}` is single-line ONLY.

        Django's lexer matches it with a non-DOTALL regex, so a `{#` whose `#}`
        sits on a later line is not a comment at all — the opener is dropped and
        every following line renders as visible text on the page. This shipped
        once: the sidebar and the login form both printed their own source
        comments to users. Multi-line commentary must use
        `{% comment %} … {% endcomment %}`.
        """
        offenders = []
        for root in TEMPLATE_ROOTS:
            if not root.exists():
                continue
            for path in sorted(root.rglob("*.html")):
                text = path.read_text(encoding="utf-8")
                for match in re.finditer(r"\{#", text):
                    close = text.find("#}", match.end())
                    line = text.count("\n", 0, match.start()) + 1
                    if close == -1 or "\n" in text[match.end():close]:
                        offenders.append(f"{path.name}:{line}")

        self.assertEqual(
            offenders, [],
            "Multi-line {# #} comments leak into the rendered page. "
            "Use {% comment %}…{% endcomment %} instead: " + ", ".join(offenders),
        )
