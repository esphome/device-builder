/* Fixture source for esp-stacktrace-decoder tests. See test/fixtures/README.md. */
static int helper(int v) { return v * 3; }
int crash_here(int v) { return helper(v) + 1; }
int main(void) { return crash_here(7); }
