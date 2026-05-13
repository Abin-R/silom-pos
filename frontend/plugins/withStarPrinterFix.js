// Patches node_modules/react-native-star-io10/android/build.gradle so it
// works under Gradle 8.14+ / EAS Build.
//
// The library ships a local AAR (stario10.aar) and historically wired it up
// via a `flatDir` repository:
//     repositories { flatDir { dirs ".../src/lib" } }
//     implementation (name: 'stario10', ext: 'aar')
//
// Modern Gradle setups (and EAS) set `RepositoriesMode.FAIL_ON_PROJECT_REPOS`
// in settings.gradle, which silently ignores project-level repo declarations.
// The flatDir is gone, so resolution fails with "Could not find :stario10:".
//
// Fix: swap the named-artifact dependency for a direct `files(...)` reference.
// `files()` doesn't go through Maven resolution at all, so the project-repo
// ban doesn't apply.
//
// This is a one-line patch but has to run after `npm install` (which is what
// EAS does on every build).  An Expo config plugin with `withDangerousMod`
// fires during prebuild, which always runs before Gradle — perfect timing.
const { withDangerousMod } = require('@expo/config-plugins');
const fs = require('fs');
const path = require('path');

const ORIGINAL = "implementation (name: 'stario10', ext: 'aar')";
const REPLACEMENT =
  'implementation files("$rootDir/../node_modules/react-native-star-io10/android/src/lib/stario10.aar")';

const withStarPrinterFix = (config) => {
  return withDangerousMod(config, [
    'android',
    async (config) => {
      const buildGradle = path.join(
        config.modRequest.projectRoot,
        'node_modules',
        'react-native-star-io10',
        'android',
        'build.gradle',
      );
      if (!fs.existsSync(buildGradle)) {
        console.warn(
          '[withStarPrinterFix] build.gradle not found; skipping patch:',
          buildGradle,
        );
        return config;
      }
      const content = fs.readFileSync(buildGradle, 'utf8');
      if (content.includes(REPLACEMENT)) {
        // Already patched (idempotent — important because Expo plugins can run
        // multiple times in a single build).
        return config;
      }
      if (!content.includes(ORIGINAL)) {
        console.warn(
          '[withStarPrinterFix] expected line not found; bailing out:',
          ORIGINAL,
        );
        return config;
      }
      fs.writeFileSync(buildGradle, content.replace(ORIGINAL, REPLACEMENT));
      console.log('[withStarPrinterFix] patched react-native-star-io10 build.gradle');
      return config;
    },
  ]);
};

module.exports = withStarPrinterFix;
