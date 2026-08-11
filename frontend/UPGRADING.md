# Upgrading the Expo SDK

This app is a **managed / CNG** Expo project (no committed `android/`/`ios/` dirs — they're
generated on demand by `expo prebuild`). It has one custom native module, the Android USB
printer at [plugins/native/bravepos-printer](plugins/native/bravepos-printer), wired in by the
config plugin [plugins/withBravePosPrinter.js](plugins/withBravePosPrinter.js).

Package manager is **yarn** (`yarn.lock`). Do **not** commit a `package-lock.json`.

## Node

Node is pinned via [.nvmrc](.nvmrc). Always start with:

```bash
nvm use            # picks up .nvmrc (install once: nvm install 22)
```

When an SDK requires a newer Node, bump `.nvmrc` and run `nvm install <ver> && nvm use`.

## Routine SDK bump (the repeatable flow)

Run everything from `frontend/` with the right Node active:

```bash
nvm use
npx expo install expo@latest          # or expo@^57.0.0 to pin a specific SDK
npx expo install --fix                # aligns react, react-native, all expo-* + RN community deps
npx expo-doctor@latest                # MUST end "No issues detected" — fix everything it reports
```

`expo install --fix` is the source of truth for version-coupled packages
(`react-native-reanimated`, `react-native-worklets`, `react-native-gesture-handler`,
`react-native-screens`, `react-native-safe-area-context`, etc.) — never pin these by hand.

Then bump deps Expo does **not** manage (only if doctor/peer warnings ask, or to stay current):
`react-native-dotenv`, `qrcode-generator`. Keep `eslint` in step with what
`eslint-config-expo` expects (it currently expects eslint 9, not 10).

## Verify before any build

```bash
npx expo-doctor@latest                                         # 21/21 checks
npx tsc --noEmit                                               # type check
npx expo prebuild --platform android --clean --no-install     # exercises the printer plugin
npx expo export --platform android --output-dir /tmp/x        # full Metro bundle compile
```

After `prebuild`, confirm the printer plugin's three patches landed, then delete the generated
`android/` (it's not committed):
- `android/settings.gradle` includes `:bravepos-printer`
- `android/app/build.gradle` has `implementation project(':bravepos-printer')`
- `android/app/src/main/java/com/bravebrand/pos/MainApplication.kt` has the
  `BravePosPrinterPackage` import + `add(BravePosPrinterPackage())`

If RN's generated `MainApplication.kt` / `app/build.gradle` layout changes in a future SDK and a
patch goes missing, update the regexes in [plugins/withBravePosPrinter.js](plugins/withBravePosPrinter.js).

The on-device printer can only be validated with a real build — run `eas build -p android
--profile preview` yourself (not auto-triggered) and test printing.

## Things that bit us on the 56 → 57 jump (watch for analogues)

- **`expo install --fix` downgrades Sentry.** Expo's `bundledNativeModules.json` pins
  `@sentry/react-native` to `~7.11.0` (the version current when SDK 57 shipped), so `--fix`
  happily rolls back the major we're on. Sentry ships on its own train, independent of Expo
  SDKs. We keep `^8.x` and hold `--fix` off it with `expo.install.exclude` in
  [package.json](package.json). Re-check this each SDK bump: if Expo's pin ever catches up
  past ours, drop the exclude.
- **`--fix` also appends a duplicate Sentry config plugin** — a bare `"@sentry/react-native"`
  next to our configured `["@sentry/react-native/expo", {...}]` in [app.json](app.json). They
  are the *same* plugin (`app.plugin.js` is just `module.exports = require('./expo')`), so the
  bare entry re-runs it with no `url`/`project`/`organization`. Delete the added line after
  every `--fix`.
- **A stale eslint cache from the previous SDK segfaults `yarn lint`** (exit 139, "Segmentation
  fault (core dumped)" — not a real crash, and not a code problem). Clear it after any SDK bump:
  `rm -rf .expo/cache/eslint`.
- **Sentry 8 moved to `sentry.gradle.kts`.** `expo prebuild` now emits an `apply from:` pointing
  at `sentry.gradle.kts` instead of `sentry.gradle`. Fine on 8.22 (both files ship), but if a
  future Sentry drops one, the Android build breaks at configure time.
- **`expo prebuild` clears native directories by default now**, so the `--clean` flag above is
  redundant (harmless — keep it for older CLIs).
- **Known upstream issue, not yet acted on**: importing `react-native-reanimated` under Hermes V1
  raises memory use 25–30%. Workaround is worklets *bundle mode* (a `bundleMode` option on the
  worklets babel plugin, which needs a `babel.config.js` — this project currently has none). It
  changes how worklets are bundled, so it needs a real on-device soak test before adopting. If
  the POS tablets start showing memory pressure, start here.
- **Node floor moved to `^20.19.4 || ^22.13.0 || ^24.3.0 || >=25`** (RN 0.86). `.nvmrc` already
  pins 22, so no change was needed — but the `Array.prototype.toReversed` polyfill at the top of
  [metro.config.js](metro.config.js) exists only to support Node 18 and is now dead code.

## Things that bit us on the 54 → 56 jump (watch for analogues)

- **Config schema drops**: `newArchEnabled` and `android.edgeToEdgeEnabled` were removed (both are
  always-on now). `android.usesCleartextTraffic` moved into the `expo-build-properties` plugin.
  expo-doctor's schema check catches these.
- **expo-router dropped `@react-navigation/*` compatibility** (SDK 56). We removed the four direct
  `@react-navigation/*` deps — the app imports none of them directly. If you ever add direct
  react-navigation imports, migrate them to expo-router equivalents instead.
- **`StyleSheet.absoluteFillObject` was removed** from React Native — use `StyleSheet.absoluteFill`
  (now a plain object, spreadable).
- **OTA updates**: `eas update` now requires `--environment`, e.g.
  `eas update --environment production --channel production`.
- **Lint**: `eslint-config-expo` / `react-hooks` v7 add stricter rules
  (`set-state-in-effect`, etc.). Pre-existing code may newly fail `yarn lint`; that's a separate
  cleanup, not an upgrade blocker.
