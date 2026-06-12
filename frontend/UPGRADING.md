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
