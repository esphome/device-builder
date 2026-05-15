# Real-compile tests

Tests in this directory invoke a full ``esphome compile``
subprocess end-to-end. The ESP8266 build alone takes ~4 min cold
on a CI runner (PlatformIO toolchain download + first compile)
and ~5 s incremental, which is two orders of magnitude past the
default suite's per-test budget. They run in their own CI job
(``real-compile-tests`` in
``.github/workflows/real-compile-tests.yml``) and are excluded
from the main ``pytest`` invocation via
``--ignore=tests/real_compile``.

The CI workflow is gated on a ``paths:`` filter — it only runs
when the diff touches the materialiser, the receiver-side
packer, the storage-path resolver, or this directory. New tests
that pin behavior of additional surfaces should extend that
filter so the gate stays meaningful.

These tests pin behavior that needs the real PlatformIO/SCons
incremental-build decider in the loop (e.g. mtime interactions
between the materialiser and ``.pioenvs/<name>/*.o``). A unit
test against ``StorageJSON.load`` / ``storage_should_clean``
can pin the storage-side gate but can't catch SCons-side
invalidations driven by ``platformio.ini`` mtime drift.
