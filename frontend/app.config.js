// App variants.
//
// `app.json` remains the single source of truth for the real app. This file is
// handed that config and, unless `APP_VARIANT` names a variant, returns it
// completely untouched — so every existing build profile produces exactly what
// it produced before this file existed.
//
// The `test` variant is a genuinely *separate* Android app: its own
// application id, so it installs alongside the production POS on a till rather
// than upgrading over the top of it. That is the whole point. A tablet can
// carry both, staff can tell them apart on the home screen, and nobody can
// lose a working till by installing a trial build.
//
// Three things have to differ for that to hold:
//   * `package` / `bundleIdentifier` — what makes it a different app at all.
//   * `name` — what a cashier reads under the icon. Same icon, different name;
//     if these two ever look identical on a home screen, someone will ring up
//     a real sale on the trial build.
//   * `scheme` — deep links. Two apps claiming `bravepos://` means whichever
//     was installed last answers for both.
//
// The EAS project id is deliberately shared, so both variants live in one
// project and one build list. They are kept apart by their update channel, set
// per build profile in eas.json — a `test` build never receives a `preview`
// update, and the tills never receive a test one.

const VARIANTS = {
  test: {
    name: "Brave POS Test",
    androidPackage: "com.bravebrand.pos.test",
    iosBundleIdentifier: "com.bravebrand.pos.test",
    scheme: "bravepostest",
  },
};

module.exports = ({ config }) => {
  const variant = VARIANTS[process.env.APP_VARIANT];
  // No variant, or one we don't recognise: hand back app.json verbatim rather
  // than a "close enough" copy. A typo in APP_VARIANT must not silently ship a
  // half-renamed build.
  if (!variant) return config;

  return {
    ...config,
    name: variant.name,
    scheme: variant.scheme,
    ios: { ...config.ios, bundleIdentifier: variant.iosBundleIdentifier },
    android: { ...config.android, package: variant.androidPackage },
  };
};
