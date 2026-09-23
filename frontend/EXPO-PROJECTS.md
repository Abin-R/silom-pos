# Expo projects: which one, and how to switch back

There are **two** EAS projects for this app. They are not interchangeable, and
which one `app.json` points at decides where builds go and — more importantly —
which installed apps can still receive an OTA update.

## The one we build from now

| | |
|---|---|
| Account | `therollingpinn` |
| Project slug | `brave-pos` |
| Project ID | `2a3cd2b5-54ec-49b1-9476-af6497db4529` |
| Update URL | `https://u.expo.dev/2a3cd2b5-54ec-49b1-9476-af6497db4529` |
| Dashboard | https://expo.dev/accounts/therollingpinn/projects/brave-pos |
| Signed in as | tech@therollingpinn.com |

This is the company-owned project and the one the Google Play release is built
from. Its Android keystore is the upload key Play will register on the first
AAB, so from that point it cannot be swapped without Google's help.

## The old one, still live

| | |
|---|---|
| Account | `shopsterabin1234s-organization` |
| Project slug | `bravepos` |
| Project ID | `92b2c7d7-717a-4f60-9d80-c8c81b64eef6` |
| Update URL | `https://u.expo.dev/92b2c7d7-717a-4f60-9d80-c8c81b64eef6` |
| Channels | `test`, `preview`, `production` |
| Signed in as | the `shopsterabin1234` login |

**Do not delete it.** Every till in the field today runs a `preview`-channel APK
built here, with that update URL compiled in. They poll this project and nothing
else. Delete it and their update checks start failing.

## Switching back to push to the tills

Until a till has been reinstalled from the new project, an OTA fix only reaches
it through the old one. To ship such a fix, put these four values back in
`app.json` — all four, together, or the build fails on a project mismatch:

```jsonc
"slug": "bravepos",
"owner": "shopsterabin1234s-organization",
"extra": { "eas": { "projectId": "92b2c7d7-717a-4f60-9d80-c8c81b64eef6" } },
"updates": { "url": "https://u.expo.dev/92b2c7d7-717a-4f60-9d80-c8c81b64eef6" }
```

Then `npx eas-cli login` as the old account before `eas update`. Switch the
file back afterwards, so the next Play build does not accidentally go out from
the old project with the wrong signing key.

The authoritative copy of those values is this repo's git history: the commit
that first pointed `app.json` at `therollingpinn/brave-pos` has the old ones in
its diff.

## The gap this leaves

A till is reachable by exactly one project at a time. Between now and the day a
till is reinstalled from Play, new features shipped from `brave-pos` do not
reach it, and nothing warns you about that — `eas update` reports success
either way, because the update was published; it just was not published
anywhere that till is listening.
