import { ESPLoader, Transport, UsbJtagSerialReset } from "esptool-js";

const ESPRESSIF_USB_VID = 0x303a;

// RTC watchdog registers per chip (base + offsets), from esptool python.
const WDT_RESET_CHIPS: Record<
  string,
  { wdtConfig0: number; wdtConfig1: number; wdtWProtect: number }
> = {
  "ESP32-S2": { wdtConfig0: 0x3f408094, wdtConfig1: 0x3f408098, wdtWProtect: 0x3f4080ac },
  "ESP32-S3": { wdtConfig0: 0x60008098, wdtConfig1: 0x6000809c, wdtWProtect: 0x600080b0 },
  "ESP32-C2": { wdtConfig0: 0x60008084, wdtConfig1: 0x60008088, wdtWProtect: 0x6000809c },
  "ESP32-C3": { wdtConfig0: 0x60008090, wdtConfig1: 0x60008094, wdtWProtect: 0x600080a8 },
};

// Magic key that unlocks the RTC WDT write-protect register.
const RTC_CNTL_WDT_WKEY = 0x50d83aa1;

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

// Full chip reset via the RTC watchdog: the stub processes the writeReg
// commands, the WDT fires, and the chip resets through ROM to the new firmware.
// Works where DTR/RTS reset doesn't reach the chip (native USB-Serial-JTAG,
// CH9102F bridges). Returns false for chips without a safe WDT (C6 freezes;
// classic ESP32 / ESP8266 have none).
async function watchdogReset(loader: ESPLoader, transport: Transport): Promise<boolean> {
  const regs = loader.chip?.CHIP_NAME ? WDT_RESET_CHIPS[loader.chip.CHIP_NAME] : undefined;
  if (!regs) return false;
  // Release the boot-strap pin before the WDT fires so the chip boots from
  // flash, not download mode.
  try {
    await transport.setDTR(false);
    await transport.setRTS(false);
  } catch {
    // setSignals may fail; the WDT can still reset the chip.
  }
  // Sequence + magic value from esptool python's watchdog_reset(): unlock, set
  // timeout, enable+arm, re-lock.
  try {
    await loader.writeReg(regs.wdtWProtect, RTC_CNTL_WDT_WKEY);
    await loader.writeReg(regs.wdtConfig1, 2000);
    await loader.writeReg(regs.wdtConfig0, (1 << 31) | (5 << 28) | (1 << 8) | 2);
    await loader.writeReg(regs.wdtWProtect, 0);
  } catch {
    // A writeReg can race the reset firing; the WDT has already done its job.
  }
  await sleep(200);
  return true;
}

// Boot a classic ESP32 / ESP8266 behind a UART bridge: pulse EN (RTS) low to
// high with GPIO0 (DTR) released so the chip runs the app, not the bootloader.
async function classicHardReset(transport: Transport): Promise<void> {
  await transport.setDTR(false); // GPIO0 high -> boot from flash, not download
  await transport.setRTS(true); // EN low (hold in reset)
  await sleep(100);
  await transport.setRTS(false); // EN high -> boot the app
}

// Reset the just-flashed chip into the app. esptool-js's after("hard_reset")
// only calls setRTS(false), which leaves the chip in the stub bootloader so the
// firmware never boots; pick a real strategy instead. Mirrors the proven
// device-builder-frontend web-serial reset.
export async function hardResetChip(
  loader: ESPLoader,
  transport: Transport,
  port: SerialPort,
): Promise<void> {
  if (await watchdogReset(loader, transport)) return;
  if (port.getInfo().usbVendorId === ESPRESSIF_USB_VID) {
    await new UsbJtagSerialReset(transport).reset();
  } else {
    await classicHardReset(transport);
  }
}
