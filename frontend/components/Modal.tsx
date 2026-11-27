// Drop-in replacement for react-native's Modal.
//
// On iOS, RN's Modal defaults `supportedOrientations` to ['portrait'], so on
// an iPad held in landscape every dialog (Checkout, confirms, pickers…)
// forces the screen to rotate to portrait while it is open. Allowing every
// orientation lets the modal follow the device like the rest of the app.
//
// Android ignores this prop (ReactModalHostManager.setSupportedOrientations
// is a no-op), so this is iOS-only in effect.
import React from "react";
import { Modal as RNModal, type ModalProps } from "react-native";

const ALL_ORIENTATIONS: ModalProps["supportedOrientations"] = [
  "portrait",
  "portrait-upside-down",
  "landscape",
  "landscape-left",
  "landscape-right",
];

export default function Modal(props: ModalProps) {
  return <RNModal supportedOrientations={ALL_ORIENTATIONS} {...props} />;
}
