// Translation dictionaries.  Keys are short identifiers; values are the
// display text for each language.  `en` is the canonical source — any key
// missing from `th` falls back to its `en` value (see `t()` in i18n.ts).
//
// Add new entries in pairs to keep both dicts in sync.
export type LangCode = "en" | "th";

export const translations: Record<LangCode, Record<string, string>> = {
  en: {
    // Login
    "login.title": "Sign in",
    "login.subtitle": "Staff login required",
    "login.email": "Email",
    "login.password": "Password",
    "login.submit": "Sign in",
    "login.select_branch": "Select branch",
    "login.network_error": "Network error. Please retry.",
    // Top bar
    "top.order_hub": "Order Hub",
    "top.drawer": "Drawer",
    "top.discount": "Discount",
    "top.save": "Save",
    "top.customer": "Customer",
    // Admin sidebar
    "side.shop": "Shop",
    "side.transactions": "Transactions",
    "side.reports": "Reports",
    "side.inventory": "Inventory",
    "side.customers": "Customers",
    "side.products": "Products",
    "side.drawer": "Drawer",
    "side.branches": "Branches",
    "side.settings": "Settings",
    "side.logout": "Log out",
    // Common
    "common.save": "Save",
    "common.cancel": "Cancel",
    "common.delete": "Delete",
    "common.edit": "Edit",
  },
  th: {
    // ภาษาไทย — Thai translations.  Filled in as Language settings work
    // graduates from "Coming soon" to a real picker.  Missing keys fall back
    // to English automatically.
    "login.title": "เข้าสู่ระบบ",
    "login.subtitle": "ต้องเข้าสู่ระบบโดยพนักงาน",
    "login.email": "อีเมล",
    "login.password": "รหัสผ่าน",
    "login.submit": "เข้าสู่ระบบ",
    "login.select_branch": "เลือกสาขา",
    "login.network_error": "ข้อผิดพลาดเครือข่าย โปรดลองอีกครั้ง",
    "top.order_hub": "ออเดอร์ทั้งหมด",
    "top.drawer": "ลิ้นชัก",
    "top.discount": "ส่วนลด",
    "top.save": "บันทึก",
    "top.customer": "ลูกค้า",
    "side.shop": "ร้าน",
    "side.transactions": "ธุรกรรม",
    "side.reports": "รายงาน",
    "side.inventory": "สินค้าคงคลัง",
    "side.customers": "ลูกค้า",
    "side.products": "สินค้า",
    "side.drawer": "ลิ้นชัก",
    "side.branches": "สาขา",
    "side.settings": "การตั้งค่า",
    "side.logout": "ออกจากระบบ",
    "common.save": "บันทึก",
    "common.cancel": "ยกเลิก",
    "common.delete": "ลบ",
    "common.edit": "แก้ไข",
  },
};
