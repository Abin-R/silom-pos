// metro.config.js

// metro-config@0.83+ uses Array.prototype.toReversed (requires Node 20).
// Polyfill so this repo keeps building on Node 18 without a system upgrade.
if (!Array.prototype.toReversed) {
  Object.defineProperty(Array.prototype, 'toReversed', {
    value: function () { return Array.from(this).reverse(); },
    configurable: true, writable: true,
  });
}

// Sentry's wrapper around Expo's metro config — required so source maps are
// generated/uploaded for readable stack traces. Drop-in for getDefaultConfig.
const { getSentryExpoConfig } = require("@sentry/react-native/metro");
const path = require('path');
const { FileStore } = require('metro-cache');

const config = getSentryExpoConfig(__dirname);

// Use a stable on-disk store (shared across web/android)
const root = process.env.METRO_CACHE_ROOT || path.join(__dirname, '.metro-cache');
config.cacheStores = [
  new FileStore({ root: path.join(root, 'cache') }),
];


// // Exclude unnecessary directories from file watching
// config.watchFolders = [__dirname];
// config.resolver.blacklistRE = /(.*)\/(__tests__|android|ios|build|dist|.git|node_modules\/.*\/android|node_modules\/.*\/ios|node_modules\/.*\/windows|node_modules\/.*\/macos)(\/.*)?$/;

// // Alternative: use a more aggressive exclusion pattern
// config.resolver.blacklistRE = /node_modules\/.*\/(android|ios|windows|macos|__tests__|\.git|.*\.android\.js|.*\.ios\.js)$/;

// Reduce the number of workers to decrease resource usage
config.maxWorkers = 2;

module.exports = config;
