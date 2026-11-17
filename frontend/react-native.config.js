// Native library linking overrides.
//
// react-native-star-io10 (Star's StarXpand SDK) drives the Star TSP143IIIU on
// iPad. Android tills print to the Epson TM-T82X through our own
// `bravepos-printer` module instead, so the Star SDK is kept out of the
// Android build entirely: the live Android app must not change because the
// iOS app gained a printer.
module.exports = {
  dependencies: {
    "react-native-star-io10": {
      platforms: { android: null },
    },
  },
};
