// Wires our custom in-repo `bravepos-printer` Kotlin module into the Expo
// Android build.  Three steps run during prebuild:
//
//   1. android/settings.gradle      → include the module from plugins/native/
//   2. android/app/build.gradle     → implementation project(':bravepos-printer')
//   3. android/app/.../MainApplication.kt → import + add(BravePosPrinterPackage())
//
// Lets us bypass third-party RN USB printer libraries entirely while keeping
// the native code version-controlled inside our own repo.
const {
  withSettingsGradle,
  withAppBuildGradle,
  withMainApplication,
  withAndroidManifest,
  withDangerousMod,
} = require('@expo/config-plugins');
const fs = require('fs');
const path = require('path');

const MODULE_NAME = ':bravepos-printer';
const MODULE_DIR = '../plugins/native/bravepos-printer';
const PACKAGE_IMPORT = 'import com.bravebrand.posprinter.BravePosPrinterPackage';
const PACKAGE_ADD = 'add(BravePosPrinterPackage())';

const withGradleInclude = (config) =>
  withSettingsGradle(config, (mod) => {
    if (mod.modResults.contents.includes(`'${MODULE_NAME}'`)) return mod;
    mod.modResults.contents +=
      `\ninclude '${MODULE_NAME}'\n` +
      `project('${MODULE_NAME}').projectDir = new File(rootProject.projectDir, '${MODULE_DIR}')\n`;
    return mod;
  });

const withAppDependency = (config) =>
  withAppBuildGradle(config, (mod) => {
    const needle = `implementation project('${MODULE_NAME}')`;
    if (mod.modResults.contents.includes(needle)) return mod;
    mod.modResults.contents = mod.modResults.contents.replace(
      /(dependencies\s*\{[\s\S]*?)(^\})/m,
      (_, body, close) => `${body}    ${needle}\n${close}`,
    );
    return mod;
  });

const withPackageRegistration = (config) =>
  withMainApplication(config, (mod) => {
    let contents = mod.modResults.contents;
    if (!contents.includes(PACKAGE_IMPORT)) {
      contents = contents.replace(
        /(\nimport [^\n]+\n)(?!\s*import )/m,
        (m) => `${m}${PACKAGE_IMPORT}\n`,
      );
    }
    if (!contents.includes(PACKAGE_ADD)) {
      contents = contents.replace(
        /(PackageList\(this\)\.packages\.apply\s*\{\s*\n)/,
        `$1              ${PACKAGE_ADD}\n`,
      );
    }
    mod.modResults.contents = contents;
    return mod;
  });

// AndroidManifest: usb.host feature + USB_DEVICE_ATTACHED intent-filter
// + a device_filter.xml listing Seiko Epson vendor id (0x04B8 = 1208).
// Filtering by vendor only — covers TM-T82X variants M352 + M352A and
// every other Epson POS printer.  Epson's own SDK README recommends
// vendor-id-only filtering, since the product-ID enumeration is internal.
const EPSON_VENDOR_ID = '1208';
const DEVICE_FILTER_XML = `<?xml version="1.0" encoding="utf-8"?>
<resources>
  <usb-device vendor-id="${EPSON_VENDOR_ID}" />
</resources>
`;

const withUsbManifest = (config) =>
  withAndroidManifest(config, (mod) => {
    const manifest = mod.modResults.manifest;

    const features = (manifest['uses-feature'] = manifest['uses-feature'] || []);
    if (!features.some((f) => f.$ && f.$['android:name'] === 'android.hardware.usb.host')) {
      features.push({
        $: { 'android:name': 'android.hardware.usb.host', 'android:required': 'false' },
      });
    }

    // Epson SDK's TCP discovery uses UDP multicast — requires WiFi multicast
    // permission on Android.  INTERNET and ACCESS_NETWORK_STATE are added by
    // Expo's defaults so we don't repeat them here.
    const perms = (manifest['uses-permission'] = manifest['uses-permission'] || []);
    const required = [
      'android.permission.CHANGE_WIFI_MULTICAST_STATE',
      'android.permission.ACCESS_WIFI_STATE',
    ];
    for (const name of required) {
      if (!perms.some((p) => p.$ && p.$['android:name'] === name)) {
        perms.push({ $: { 'android:name': name } });
      }
    }

    const app = manifest.application && manifest.application[0];
    const main = app && (app.activity || []).find(
      (a) => a.$ && a.$['android:name'] === '.MainActivity',
    );
    if (main) {
      main['intent-filter'] = main['intent-filter'] || [];
      const has = main['intent-filter'].some((f) =>
        (f.action || []).some(
          (a) => a.$ && a.$['android:name'] === 'android.hardware.usb.action.USB_DEVICE_ATTACHED',
        ),
      );
      if (!has) {
        main['intent-filter'].push({
          action: [{ $: { 'android:name': 'android.hardware.usb.action.USB_DEVICE_ATTACHED' } }],
        });
      }
      main['meta-data'] = main['meta-data'] || [];
      if (!main['meta-data'].some(
        (m) => m.$ && m.$['android:name'] === 'android.hardware.usb.action.USB_DEVICE_ATTACHED',
      )) {
        main['meta-data'].push({
          $: {
            'android:name': 'android.hardware.usb.action.USB_DEVICE_ATTACHED',
            'android:resource': '@xml/device_filter',
          },
        });
      }
    }

    return mod;
  });

const withDeviceFilterXml = (config) =>
  withDangerousMod(config, [
    'android',
    async (mod) => {
      const xmlDir = path.join(
        mod.modRequest.platformProjectRoot,
        'app', 'src', 'main', 'res', 'xml',
      );
      fs.mkdirSync(xmlDir, { recursive: true });
      fs.writeFileSync(path.join(xmlDir, 'device_filter.xml'), DEVICE_FILTER_XML);
      return mod;
    },
  ]);

module.exports = (config) =>
  withDeviceFilterXml(
    withUsbManifest(
      withPackageRegistration(withAppDependency(withGradleInclude(config))),
    ),
  );
