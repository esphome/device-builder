// ESP32/ESP8266 firmware (and the bootloader a factory image starts with) carry
// the ESP image magic byte at the bootloader offset.
export const ESP_IMAGE_MAGIC = 0xe9;

export interface FlashPart {
  data: Uint8Array;
  address: number;
}

// Reject anything that isn't an ESP image before the chip is erased. The magic
// sits at byte 0 for ESP8266 and native-USB ESP32 parts (S3/C3/C6, bootloader
// at 0x0); the original ESP32 / ESP32-S2 merged factory image pads 0x0-0xFFF
// with 0xFF and places the bootloader (magic) at 0x1000. Returns an error
// string, or null when it looks valid.
export function validateEspImage(files: FlashPart[]): string | null {
  const boot = files.find((f) => f.address === 0);
  if (!boot) {
    return "No image at offset 0x0; expected an ESP32/ESP8266 factory image.";
  }
  const hasMagic =
    boot.data[0] === ESP_IMAGE_MAGIC ||
    (boot.data.length > 0x1000 && boot.data[0x1000] === ESP_IMAGE_MAGIC);
  if (!hasMagic) {
    return "This does not look like ESP32/ESP8266 firmware (no 0xE9 image magic at 0x0 or 0x1000).";
  }
  return null;
}
