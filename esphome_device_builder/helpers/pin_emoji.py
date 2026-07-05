"""Matrix-SAS emoji rendering of a peer-link pin fingerprint."""

from __future__ import annotations

from functools import lru_cache

# Exact port of the frontend's ``src/util/pin-emoji.ts``: the
# 64-emoji vocabulary and indexing from v1.x of the Matrix
# Short-Authentication-String spec
# (https://spec.matrix.org/v1.8/client-server-api/#sas-method-emoji),
# as popularised by Element X. We are NOT speaking the Matrix
# protocol — only borrowing the UX vocabulary so the CLI banner and
# the frontend's ``esphome-pin-emoji-grid`` render the identical
# sequence for a given pin. Entries are escape-coded (not literal
# glyphs) so a diff against the TS table compares codepoints, VS16
# selectors included. The order is frozen by the spec; any edit here
# desyncs the two sides mid-upgrade.
SAS_EMOJI: tuple[tuple[str, str], ...] = (
    ("\U0001f436", "Dog"),
    ("\U0001f431", "Cat"),
    ("\U0001f981", "Lion"),
    ("\U0001f40e", "Horse"),
    ("\U0001f984", "Unicorn"),
    ("\U0001f437", "Pig"),
    ("\U0001f418", "Elephant"),
    ("\U0001f430", "Rabbit"),
    ("\U0001f43c", "Panda"),
    ("\U0001f413", "Rooster"),
    ("\U0001f427", "Penguin"),
    ("\U0001f422", "Turtle"),
    ("\U0001f41f", "Fish"),
    ("\U0001f419", "Octopus"),
    ("\U0001f98b", "Butterfly"),
    ("\U0001f337", "Flower"),
    ("\U0001f333", "Tree"),
    ("\U0001f335", "Cactus"),
    ("\U0001f344", "Mushroom"),
    ("\U0001f30f", "Globe"),
    ("\U0001f319", "Moon"),
    ("\u2601\ufe0f", "Cloud"),
    ("\U0001f525", "Fire"),
    ("\U0001f34c", "Banana"),
    ("\U0001f34e", "Apple"),
    ("\U0001f353", "Strawberry"),
    ("\U0001f33d", "Corn"),
    ("\U0001f355", "Pizza"),
    ("\U0001f382", "Cake"),
    ("\u2764\ufe0f", "Heart"),
    ("\U0001f600", "Smiley"),
    ("\U0001f916", "Robot"),
    ("\U0001f3a9", "Hat"),
    ("\U0001f453", "Glasses"),
    ("\U0001f527", "Wrench"),
    ("\U0001f385", "Santa"),
    ("\U0001f44d", "Thumbs up"),
    ("\u2602\ufe0f", "Umbrella"),
    ("\u231b\ufe0f", "Hourglass"),
    ("\u23f0", "Clock"),
    ("\U0001f381", "Gift"),
    ("\U0001f4a1", "Light bulb"),
    ("\U0001f4d5", "Book"),
    ("\u270f\ufe0f", "Pencil"),
    ("\U0001f4ce", "Paperclip"),
    ("\u2702\ufe0f", "Scissors"),
    ("\U0001f512", "Lock"),
    ("\U0001f511", "Key"),
    ("\U0001f528", "Hammer"),
    ("\u260e\ufe0f", "Telephone"),
    ("\U0001f3c1", "Flag"),
    ("\U0001f682", "Train"),
    ("\U0001f6b2", "Bicycle"),
    ("\u2708\ufe0f", "Aeroplane"),
    ("\U0001f680", "Rocket"),
    ("\U0001f3c6", "Trophy"),
    ("\u26bd\ufe0f", "Ball"),
    ("\U0001f3b8", "Guitar"),
    ("\U0001f3ba", "Trumpet"),
    ("\U0001f514", "Bell"),
    ("\u2693\ufe0f", "Anchor"),
    ("\U0001f3a7", "Headphones"),
    ("\U0001f4c1", "Folder"),
    ("\U0001f4cc", "Pin"),
)

PIN_EMOJI_COUNT = 7

# 7 chunks x 6 bits = 42 leading bits; 11 hex nibbles carry 44.
_NIBBLES_NEEDED = 11


@lru_cache(maxsize=8)
def pin_emoji_slots(pin_sha256: str) -> tuple[tuple[str, str], ...]:
    """
    Map a hex pin's leading 42 bits to 7 ``(emoji, name)`` SAS slots.

    Mirrors the frontend's ``pinSha256ToEmojis``: consume the hex
    left-to-right in 6-bit chunks, index each into the SAS table.
    Cached — the banner renders emoji and names off one computation,
    and a process only ever displays a handful of distinct pins.
    """
    bits = int(pin_sha256[:_NIBBLES_NEEDED], 16)
    total_bits = _NIBBLES_NEEDED * 4
    return tuple(
        SAS_EMOJI[(bits >> (total_bits - 6 * (i + 1))) & 0x3F] for i in range(PIN_EMOJI_COUNT)
    )


def pin_emoji(pin_sha256: str) -> str:
    """Render the 7-emoji SAS sequence, space-separated for console display."""
    return _render(pin_sha256, part=0, sep=" ")


def pin_emoji_names(pin_sha256: str) -> str:
    """Render the matching SAS emoji names, for terminals with poor emoji fonts."""
    return _render(pin_sha256, part=1, sep=", ")


def _render(pin_sha256: str, *, part: int, sep: str) -> str:
    """Join one field of each cached slot: ``part`` 0 = emoji, 1 = name."""
    return sep.join(slot[part] for slot in pin_emoji_slots(pin_sha256))
