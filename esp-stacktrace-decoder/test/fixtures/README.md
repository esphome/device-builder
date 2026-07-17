# Test fixtures

## `tiny-riscv32.elf`

A 6 KB RISC-V ELF with DWARF, just big enough for a real decode. It exists
because a junk ELF decodes to zero frames, and a test that cannot produce a
frame cannot catch a bug in one: the `address` field is a Rust `u64`, so the
wasm glue returns a **BigInt**, which structured clone carries happily and
`JSON.stringify` rejects. That shipped once and only a real decode caught it.

Built from `tiny.c` with the toolchain ESPHome installs for esp32c3:

```sh
riscv32-esp-elf-gcc -g -O0 -nostdlib -nostartfiles -Ttext=0x42000000 \
  -fdebug-prefix-map="$PWD"=. -o tiny-riscv32.elf tiny.c
```

`-Ttext=0x42000000` puts the code where an esp32c3 maps flash, so the addresses
look like the ones a real backtrace carries. `-fdebug-prefix-map` keeps the
build path out of the DWARF, so the fixture is reproducible and the expected
locations do not depend on who built it.

Known addresses, verified with `riscv32-esp-elf-addr2line -pfiaC`:

| address | function | location |
|---|---|---|
| `0x42000000` | `helper` | `././tiny.c:2` |
| `0x42000020` | `crash_here` | `././tiny.c:3` |
| `0x42000040` | `main` | `././tiny.c:4` |

Decode correctness in general belongs to upstream esp-stacktrace-decoder, which
is verified against real addr2line over full ESPHome firmware ELFs. This fixture
only has to prove the page's own wiring: that the wasm loads, decodes, and hands
back frames the embedder can actually consume.
