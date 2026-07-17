// Types for the wasm-bindgen glue shipped in the pinned esp-stacktrace-decoder
// release (see build.mjs). The file itself only exists in dist/ after a build,
// so it is declared rather than checked in; keep this in sync with the release
// the build pins.

/** One address the decoder resolved. Fields are wasm-backed getters. */
export class DecodedAddress {
  /** A BigInt, not a number: Rust types it u64 and the glue returns
   *  `BigInt.asUintN(64, ...)`. Narrowed at the postMessage boundary. */
  readonly address: bigint;
  readonly function_name: string;
  readonly location: string;
  free(): void;
}

/**
 * Resolve every address found in *dump* against the *bin* ELF.
 *
 * Pure computation: the decoder greps the addresses out of the dump itself and
 * answers from the ELF's DWARF. It makes no network calls (the page's CSP
 * forbids them regardless).
 */
export function decode(bin: Uint8Array, dump: string): DecodedAddress[];

/** Compile and instantiate the wasm. Resolves once it is ready to `decode`. */
export default function init(): Promise<unknown>;
