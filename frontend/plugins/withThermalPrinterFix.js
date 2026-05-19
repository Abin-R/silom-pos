// Patches `react-native-thermal-receipt-printer-image-qr` so it builds and
// runs cleanly with only the bits we actually use (USB on Android).
//
// What gets fixed at prebuild time:
//
//   1. node_modules/.../dist/utils/net-connect.js
//      The library's top-level `import Ping from 'react-native-ping'`
//      forces a hard dependency on the rarely-maintained `react-native-ping`
//      native module, which we don't need (USB-only).  Replace the file with
//      a no-op stub.  connectToHost becomes a throwing function — never
//      called from our app.
//
//   2. AndroidManifest.xml
//      Declare `<uses-feature android:hardware.usb.host/>` so Android 12+
//      permits USB enumeration, and an intent-filter on MainActivity for
//      `USB_DEVICE_ATTACHED` so Brave POS appears in the "Choose an app for
//      USB device" picker when the Star printer is plugged in (Star vendor
//      ID 0x0519 / 1305).  Without this filter, only B-POS / Shopify POS
//      show up — exactly the symptom the user reported earlier.
const {
  withDangerousMod,
  withAndroidManifest,
} = require('@expo/config-plugins');
const fs = require('fs');
const path = require('path');

const STAR_VENDOR_ID = '1305'; // 0x0519
const DEVICE_FILTER_XML = `<?xml version="1.0" encoding="utf-8"?>
<resources>
  <!-- Star Micronics — covers all current TSP100 / mC-Print / mPOP models. -->
  <usb-device vendor-id="${STAR_VENDOR_ID}" />
</resources>
`;

const NET_CONNECT_STUB = `// Stubbed out by withThermalPrinterFix — Brave POS uses USB only and we
// don't want a hard dep on react-native-ping.  If someone later imports
// connectToHost it throws, which is loud and obvious.
export var connectToHost = function (ipAddress, timeout) {
  return Promise.reject(new Error(
    'NetPrinter is disabled in this build (USB only).  Add react-native-ping ' +
    'and remove this stub if you need it.'
  ));
};
`;

const withThermalPrinterFix = (config) => {
  // 1. Stub net-connect.js
  config = withDangerousMod(config, [
    'android',
    async (mod) => {
      const file = path.join(
        mod.modRequest.projectRoot,
        'node_modules',
        'react-native-thermal-receipt-printer-image-qr',
        'dist',
        'utils',
        'net-connect.js',
      );
      if (fs.existsSync(file)) {
        const current = fs.readFileSync(file, 'utf8');
        if (current !== NET_CONNECT_STUB) {
          fs.writeFileSync(file, NET_CONNECT_STUB);
          console.log('[withThermalPrinterFix] stubbed net-connect.js (no react-native-ping)');
        }
      }

      // 1b. Patch USBPrinterAdapter.java for Android 13+ registerReceiver.
      // The library uses the 2-arg form which throws SecurityException on
      // Android 13+ — that's the crash we see when tapping Scan USB.  Wrap
      // it in an SDK-version check; use raw literal `2` (= Context.RECEIVER_EXPORTED)
      // and `33` (= Build.VERSION_CODES.TIRAMISU) so the source still compiles
      // against the library's pinned compileSdk 32 (which doesn't know those
      // names).  Runtime check still gates the 3-arg call to Android 13+.
      //
      // We additionally bump the library's own compileSdk to 36 below so that
      // the 3-arg registerReceiver overload is in the available method set.
      const adapterPath = path.join(
        mod.modRequest.projectRoot,
        'node_modules',
        'react-native-thermal-receipt-printer-image-qr',
        'android', 'src', 'main', 'java', 'com', 'pinmi', 'react', 'printer',
        'adapter', 'USBPrinterAdapter.java',
      );
      if (fs.existsSync(adapterPath)) {
        let src = fs.readFileSync(adapterPath, 'utf8');
        const OLD = 'mContext.registerReceiver(mUsbDeviceReceiver, filter);';
        const NEW =
          '// 33 = Build.VERSION_CODES.TIRAMISU, 2 = Context.RECEIVER_EXPORTED\n' +
          '        if (android.os.Build.VERSION.SDK_INT >= 33) {\n' +
          '            mContext.registerReceiver(mUsbDeviceReceiver, filter, 2);\n' +
          '        } else {\n' +
          '            mContext.registerReceiver(mUsbDeviceReceiver, filter);\n' +
          '        }';
        if (src.includes(OLD) && !src.includes('SDK_INT >= 33')) {
          src = src.replace(OLD, NEW);
          fs.writeFileSync(adapterPath, src);
          console.log('[withThermalPrinterFix] patched USBPrinterAdapter for Android 13+ registerReceiver');
        }
      }

      // 1c. Bump the library's compileSdkVersion from 32 → host project's
      // (default 36) so the 3-arg `registerReceiver(BroadcastReceiver, IntentFilter, int)`
      // overload exists in the API surface during compilation.  Also align
      // buildToolsVersion / targetSdk.
      const libGradle = path.join(
        mod.modRequest.projectRoot,
        'node_modules',
        'react-native-thermal-receipt-printer-image-qr',
        'android', 'build.gradle',
      );
      if (fs.existsSync(libGradle)) {
        let g = fs.readFileSync(libGradle, 'utf8');
        const beforeG = g;
        g = g
          .replace(/compileSdkVersion\s*=\s*32/, 'compileSdkVersion = 36')
          .replace(/buildToolsVersion\s*=\s*"32\.0\.0"/, 'buildToolsVersion = "36.0.0"')
          .replace(/targetSdkVersion\s+32/, 'targetSdkVersion 36');
        if (g !== beforeG) {
          fs.writeFileSync(libGradle, g);
          console.log('[withThermalPrinterFix] bumped library compileSdkVersion 32 → 36');
        }
      }

      return mod;
    },
  ]);

  // 2. Manifest: usb.host feature + USB_DEVICE_ATTACHED intent-filter
  //    on MainActivity, plus a device_filter.xml in res/xml.
  config = withAndroidManifest(config, (mod) => {
    const manifest = mod.modResults.manifest;

    // <uses-feature> at the manifest root
    const features = (manifest['uses-feature'] = manifest['uses-feature'] || []);
    if (!features.some((f) => f.$ && f.$['android:name'] === 'android.hardware.usb.host')) {
      features.push({
        $: {
          'android:name': 'android.hardware.usb.host',
          'android:required': 'false',
        },
      });
    }

    // intent-filter + meta-data on MainActivity
    const application = manifest.application && manifest.application[0];
    if (application) {
      const mainActivity = (application.activity || []).find(
        (a) => a.$ && a.$['android:name'] === '.MainActivity',
      );
      if (mainActivity) {
        mainActivity['intent-filter'] = mainActivity['intent-filter'] || [];
        const hasFilter = mainActivity['intent-filter'].some((f) =>
          (f.action || []).some(
            (a) => a.$ && a.$['android:name'] === 'android.hardware.usb.action.USB_DEVICE_ATTACHED',
          ),
        );
        if (!hasFilter) {
          mainActivity['intent-filter'].push({
            action: [{ $: { 'android:name': 'android.hardware.usb.action.USB_DEVICE_ATTACHED' } }],
          });
        }
        mainActivity['meta-data'] = mainActivity['meta-data'] || [];
        const hasMeta = mainActivity['meta-data'].some(
          (m) => m.$ && m.$['android:name'] === 'android.hardware.usb.action.USB_DEVICE_ATTACHED',
        );
        if (!hasMeta) {
          mainActivity['meta-data'].push({
            $: {
              'android:name': 'android.hardware.usb.action.USB_DEVICE_ATTACHED',
              'android:resource': '@xml/device_filter',
            },
          });
        }
      }
    }

    return mod;
  });

  // 3. Drop device_filter.xml into res/xml
  config = withDangerousMod(config, [
    'android',
    async (mod) => {
      const xmlDir = path.join(
        mod.modRequest.platformProjectRoot,
        'app',
        'src',
        'main',
        'res',
        'xml',
      );
      fs.mkdirSync(xmlDir, { recursive: true });
      fs.writeFileSync(path.join(xmlDir, 'device_filter.xml'), DEVICE_FILTER_XML);
      return mod;
    },
  ]);

  return config;
};

module.exports = withThermalPrinterFix;
