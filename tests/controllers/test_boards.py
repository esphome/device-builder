"""Tests for the board catalog controller.

``BoardCatalog`` is the in-memory cache + filter/search layer over
the on-disk ``definitions/boards/<id>/manifest.yaml`` set. The
loader (``load_board_catalog``) is exercised by
``script/validate_definitions.py``; this file pins the
*controller's* behaviour with a hand-built fixture catalog so the
tests don't drift with the real catalog churn.

The controller has two consumer surfaces:

* WebSocket commands ``boards/get_board`` and ``boards/get_boards``
  (decorated with ``@api_command``).
* In-process lookups ``get_by_id`` / ``find_by_pio_board`` /
  ``find_by_platform_variant``, used by the components controller
  and the device-import flow.

Both surfaces are covered here against the same fixture set so a
filter regression hits both halves.
"""

from __future__ import annotations

import pytest

from esphome_device_builder.controllers.boards import BoardCatalog
from esphome_device_builder.models import (
    BoardCatalogEntry,
    BoardCatalogIndex,
    BoardTag,
    Esp32Variant,
    Platform,
)
from esphome_device_builder.models.boards import BoardEsphomeConfig


def _board(
    *,
    board_id: str,
    name: str | None = None,
    description: str = "",
    manufacturer: str = "Acme",
    platform: Platform = Platform.ESP32,
    variant: Esp32Variant | None = None,
    pio_board: str = "esp32dev",
    tags: list[BoardTag] | None = None,
    featured: bool = False,
    is_generic: bool = False,
) -> BoardCatalogIndex:
    """Compact factory for slim catalog entries — defaults to a plausible ESP32 board."""
    return BoardCatalogIndex(
        id=board_id,
        name=name or board_id,
        description=description,
        manufacturer=manufacturer,
        esphome=BoardEsphomeConfig(platform=platform, board=pio_board, variant=variant),
        tags=tags or [],
        featured=featured,
        is_generic=is_generic,
    )


def _seed_catalog(cat: BoardCatalog, boards: list[BoardCatalogIndex]) -> None:
    """Wire *boards* in as the slim index + seed the body cache.

    Bodies are derived from the slim entries (empty hardware / pins /
    featured_components / featured_bundles / default_components) and
    pre-populated into the LazyBodyStore's cache so ``await
    get_board`` round-trips without disk access.
    """
    cat._boards = boards
    cat._known_ids = frozenset(b.id for b in boards)
    for slim in boards:
        body = BoardCatalogEntry(
            id=slim.id,
            name=slim.name,
            description=slim.description,
            manufacturer=slim.manufacturer,
            esphome=slim.esphome,
            tags=list(slim.tags),
            images=list(slim.images),
            docs_url=slim.docs_url,
            product_url=slim.product_url,
            featured=slim.featured,
            is_generic=slim.is_generic,
        )
        cat._body_store.cache_put(slim.id, body)


@pytest.fixture
def catalog() -> BoardCatalog:
    """Build a controller pre-loaded with a deterministic mini-catalog.

    Avoids ``BoardCatalog.load()`` and the real on-disk YAML so the
    tests are stable across catalog updates. Mix: two ESP32 variants
    (S3 + C3), one ESP8266, plus generic fallbacks; one entry per
    platform is featured.
    """
    cat = BoardCatalog()
    _seed_catalog(
        cat,
        [
            _board(
                board_id="seeed-xiao-esp32c3",
                name="Seeed XIAO ESP32-C3",
                description="Compact dev board",
                manufacturer="Seeed",
                platform=Platform.ESP32,
                variant=Esp32Variant.ESP32C3,
                pio_board="esp32-c3-devkitm-1",
                tags=[BoardTag.COMPACT, BoardTag.USB_C],
                featured=True,
            ),
            _board(
                board_id="m5stack-cores3",
                name="M5Stack CoreS3",
                description="Display-equipped ESP32-S3",
                manufacturer="M5Stack",
                platform=Platform.ESP32,
                variant=Esp32Variant.ESP32S3,
                pio_board="m5stack-cores3",
                tags=[BoardTag.DISPLAY],
            ),
            _board(
                board_id="generic-esp32c3",
                name="Generic ESP32-C3",
                manufacturer="Generic",
                platform=Platform.ESP32,
                variant=Esp32Variant.ESP32C3,
                pio_board="esp32-c3-devkitm-1",
                is_generic=True,
            ),
            _board(
                board_id="generic-esp32s3",
                name="Generic ESP32-S3",
                manufacturer="Generic",
                platform=Platform.ESP32,
                variant=Esp32Variant.ESP32S3,
                pio_board="esp32-s3-devkitc-1",
                is_generic=True,
            ),
            _board(
                board_id="d1-mini",
                name="Wemos D1 Mini",
                description="Classic ESP8266 dev board",
                manufacturer="Wemos",
                platform=Platform.ESP8266,
                pio_board="d1_mini",
                tags=[BoardTag.COMPACT],
                featured=True,
            ),
            _board(
                board_id="generic-esp8266",
                name="Generic ESP8266",
                manufacturer="Generic",
                platform=Platform.ESP8266,
                pio_board="nodemcuv2",
                is_generic=True,
            ),
        ],
    )
    return cat


# ---------------------------------------------------------------------------
# get_board / get_by_id
# ---------------------------------------------------------------------------


async def test_get_board_returns_match_by_id(catalog: BoardCatalog) -> None:
    """``boards/get_board`` returns the entry whose ``id`` matches."""
    board = await catalog.get_board(board_id="m5stack-cores3")
    assert board is not None
    assert board.id == "m5stack-cores3"
    assert board.esphome.variant == Esp32Variant.ESP32S3


async def test_get_board_returns_none_for_unknown_id(catalog: BoardCatalog) -> None:
    """Unknown board id → ``None`` (not an exception).

    The frontend treats ``None`` as "board no longer in catalog";
    raising would surface as a generic 500 instead of letting the
    UI render the device with a stale label.
    """
    assert await catalog.get_board(board_id="not-a-real-board") is None


def test_get_by_id_is_synchronous_alias_for_get_board(catalog: BoardCatalog) -> None:
    """``get_by_id`` is the in-process counterpart used by other controllers.

    Pinned separately from ``get_board`` because the components
    controller and import flow depend on the synchronous shape —
    a refactor that turned ``get_by_id`` into an async method would
    surface here.
    """
    board = catalog.get_by_id("d1-mini")
    assert board is not None
    assert board.esphome.platform == Platform.ESP8266
    assert catalog.get_by_id("ghost") is None


# ---------------------------------------------------------------------------
# get_boards — filtering
# ---------------------------------------------------------------------------


async def test_get_boards_unfiltered_returns_everything_with_total(
    catalog: BoardCatalog,
) -> None:
    """No filters → every board, paged response carries the full ``total``."""
    resp = await catalog.get_boards()
    assert resp.total == 6
    assert len(resp.boards) == 6
    assert resp.offset == 0
    assert resp.limit == 50


async def test_get_boards_filters_by_platform(catalog: BoardCatalog) -> None:
    """``platform=esp8266`` drops every entry on a different platform."""
    resp = await catalog.get_boards(platform=Platform.ESP8266)

    assert resp.total == 2
    assert {b.id for b in resp.boards} == {"d1-mini", "generic-esp8266"}


async def test_get_boards_filters_by_variant_case_insensitive(
    catalog: BoardCatalog,
) -> None:
    """``variant`` filter is case-insensitive — ``ESP32C3`` matches ``esp32c3``.

    Frontend may send the upper-cased enum name (``ESP32C3``)
    while the catalog stores the lowercase value (``esp32c3``).
    The controller lowercases both sides so the dropdown's
    selected value round-trips.
    """
    resp = await catalog.get_boards(variant="ESP32C3")

    assert resp.total == 2
    assert {b.id for b in resp.boards} == {"seeed-xiao-esp32c3", "generic-esp32c3"}


async def test_get_boards_filters_by_tag(catalog: BoardCatalog) -> None:
    """``tag=display`` returns only the entry tagged for it."""
    resp = await catalog.get_boards(tag=BoardTag.DISPLAY)

    assert resp.total == 1
    assert resp.boards[0].id == "m5stack-cores3"


async def test_get_boards_query_searches_name_description_manufacturer_id_tags(
    catalog: BoardCatalog,
) -> None:
    """The free-text ``query`` matches across multiple fields, case-insensitive."""
    # Name match.
    by_name = await catalog.get_boards(query="xiao")
    assert {b.id for b in by_name.boards} == {"seeed-xiao-esp32c3"}

    # Manufacturer match.
    by_mfr = await catalog.get_boards(query="WEMOS")
    assert {b.id for b in by_mfr.boards} == {"d1-mini"}

    # Description match.
    by_desc = await catalog.get_boards(query="display-equipped")
    assert {b.id for b in by_desc.boards} == {"m5stack-cores3"}

    # Tag match.
    by_tag_query = await catalog.get_boards(query="usb-c")
    assert {b.id for b in by_tag_query.boards} == {"seeed-xiao-esp32c3"}

    # ID match.
    by_id = await catalog.get_boards(query="generic-esp8266")
    assert {b.id for b in by_id.boards} == {"generic-esp8266"}


async def test_get_boards_filters_compose(catalog: BoardCatalog) -> None:
    """Multiple filters AND together — platform + variant + tag.

    Pin the composition: a refactor that swapped any filter for
    OR semantics would silently widen results.
    """
    resp = await catalog.get_boards(
        platform=Platform.ESP32,
        variant=Esp32Variant.ESP32C3,
        tag=BoardTag.COMPACT,
    )

    assert resp.total == 1
    assert resp.boards[0].id == "seeed-xiao-esp32c3"


# ---------------------------------------------------------------------------
# get_boards — sorting + pagination
# ---------------------------------------------------------------------------


async def test_get_boards_sorts_featured_first_generic_after_featured(
    catalog: BoardCatalog,
) -> None:
    """Featured first, generic fallbacks next, the rest alphabetical.

    Drives the dashboard's "browse all" listing — generics are the
    safe catch-all most users want, so they sit at the top of each
    list (below the separately-rendered featured boards). A refactor
    that flipped the sort key tuple would surface here.
    """
    resp = await catalog.get_boards()
    ids = [b.id for b in resp.boards]

    # Featured pair, tie-broken alphabetically by name —
    # "Seeed ..." < "Wemos D1 Mini" so Seeed comes first.
    assert ids[0:2] == ["seeed-xiao-esp32c3", "d1-mini"]
    # Generic fallbacks right after the featured pair.
    assert ids[2:5] == ["generic-esp32c3", "generic-esp32s3", "generic-esp8266"]
    # Non-featured non-generic falls to the end.
    assert ids[-1] == "m5stack-cores3"


async def test_get_boards_sorts_wifi_first_within_generic_and_nongeneric_tiers() -> None:
    """Pin tier order: featured, WiFi generic, generic, WiFi non-generic, non-generic."""
    cat = BoardCatalog()
    _seed_catalog(
        cat,
        [
            _board(board_id="featured", name="Featured Board", featured=True),
            # WiFi generic named after the plain generic; WiFi wins the
            # tie-break despite the later name.
            _board(
                board_id="generic-wifi",
                name="Generic Zzz",
                tags=[BoardTag.WIFI],
                is_generic=True,
            ),
            _board(board_id="generic-plain", name="Generic Aaa", is_generic=True),
            # Alphabetically-first WiFi non-generic; still sorts below both
            # generics (generics-first outranks both name and WiFi).
            _board(board_id="nongeneric-wifi", name="Aaa Vendor", tags=[BoardTag.WIFI]),
            _board(board_id="nongeneric-plain", name="Zzz Vendor"),
        ],
    )

    resp = await cat.get_boards()
    ids = [b.id for b in resp.boards]

    assert ids == [
        "featured",
        "generic-wifi",
        "generic-plain",
        "nongeneric-wifi",
        "nongeneric-plain",
    ]


async def test_get_boards_paginates_via_offset_and_limit(
    catalog: BoardCatalog,
) -> None:
    """``offset`` + ``limit`` slice the sorted list; ``total`` is the unsliced count.

    Page-2 view: skip the first two and take two. ``total`` stays
    at the full count so the frontend can render "showing 3-4 of
    6" without a second request.
    """
    resp = await catalog.get_boards(offset=2, limit=2)

    assert resp.total == 6
    assert resp.offset == 2
    assert resp.limit == 2
    assert len(resp.boards) == 2
    # After-featured slice: the first two generics alphabetically.
    assert [b.id for b in resp.boards] == ["generic-esp32c3", "generic-esp32s3"]


async def test_get_boards_offset_past_end_returns_empty_page(
    catalog: BoardCatalog,
) -> None:
    """Offset past the result count → empty page, ``total`` still accurate.

    Frontend handles "no more results" by checking
    ``len(boards) < limit``; ``total`` lets it short-circuit
    further requests.
    """
    resp = await catalog.get_boards(offset=100, limit=10)

    assert resp.total == 6
    assert resp.boards == []


# ---------------------------------------------------------------------------
# find_by_pio_board
# ---------------------------------------------------------------------------


def test_find_by_pio_board_prefers_generic_when_multiple_match(
    catalog: BoardCatalog,
) -> None:
    """Multiple matches for the same pio_board → prefer the generic.

    Real-world trigger: a user imports a vanilla ``esp32-c3-devkitm-1``
    YAML; the catalog contains both that generic plus several vendor
    products built on the same reference design (Seeed XIAO,
    "Athom Smart Plug v3", etc.). Without the generic preference the
    dashboard would mislabel a plain dev-kit as the first vendor entry
    by alphabetical id, which is exactly the regression that motivated
    this branch.
    """
    board = catalog.find_by_pio_board("esp32-c3-devkitm-1")

    assert board is not None
    assert board.id == "generic-esp32c3"
    assert board.is_generic is True


def test_find_by_pio_board_prefer_exact_id_beats_generic(catalog: BoardCatalog) -> None:
    """``prefer_exact_id`` lands on the entry named after the pio_board.

    Resolving a device's own ``board: esp32-c3-devkitm-1`` must use that
    exact entry and its pinout, not the broader ``generic-esp32c3`` that
    shares the PlatformIO board; the default order still returns the generic.
    """
    catalog._boards.append(
        _board(
            board_id="esp32-c3-devkitm-1",
            name="Espressif ESP32-C3-DevKitM-1",
            platform=Platform.ESP32,
            variant=Esp32Variant.ESP32C3,
            pio_board="esp32-c3-devkitm-1",
        )
    )

    default = catalog.find_by_pio_board("esp32-c3-devkitm-1")
    exact = catalog.find_by_pio_board("esp32-c3-devkitm-1", prefer_exact_id=True)

    assert default is not None and default.id == "generic-esp32c3"
    assert exact is not None and exact.id == "esp32-c3-devkitm-1"


def test_find_by_pio_board_returns_first_when_no_generic(
    catalog: BoardCatalog,
) -> None:
    """Without a generic among the matches, fall back to the first match.

    Pin the iteration-order fallback by giving the catalog *two*
    non-generic entries with the same ``pio_board``: a regression
    that swapped the fallback to "any match" rather than "first
    match" would surface here. The fixture-as-shipped only has one
    non-generic for ``esp32-c3-devkitm-1`` once the generics are
    dropped, so we add a second to make the order check meaningful.
    """
    catalog._boards = [b for b in catalog._boards if not b.is_generic]
    # Insert a second vendor entry with the same pio_board *after*
    # the existing Seeed XIAO so iteration order is observable.
    catalog._boards.append(
        _board(
            board_id="zzz-second-vendor-c3",
            name="ZZZ Second Vendor C3",
            platform=Platform.ESP32,
            variant=Esp32Variant.ESP32C3,
            pio_board="esp32-c3-devkitm-1",
        )
    )

    board = catalog.find_by_pio_board("esp32-c3-devkitm-1")

    assert board is not None
    assert board.is_generic is False
    # First in iteration order wins — Seeed XIAO was added before the
    # ZZZ stand-in.
    assert board.id == "seeed-xiao-esp32c3"


def test_find_by_pio_board_id_match_tiebreaks_when_no_generic(
    catalog: BoardCatalog,
) -> None:
    """Without a generic match, prefer the entry whose id equals the pio_board.

    Regression for issue #395: a YAML referencing PlatformIO
    ``d1_mini`` was resolving to ``aquaping`` (a vendor product
    built on the d1_mini reference design) because both entries
    matched and the alphabetical-first vendor won the fallback.
    The catalog also contains the canonical ``d1-mini`` entry,
    which the tiebreaker now picks even before ``is_generic`` is
    set on either side.

    Use synthetic boards so the test is robust against the real
    ``d1-mini`` manifest later being marked ``is_generic`` (which
    would short-circuit before the tiebreaker fires) — the
    tiebreaker itself is what we're pinning here. Strip the
    fixture's pre-existing ``d1_mini`` matches before inserting
    the synthetic pair so the assertion can only be satisfied by
    the synthetic canonical entry — leaving the fixture's own
    ``d1-mini`` in place would let the assertion pass against
    that one and the synthetic insert would be redundant.
    """
    catalog._boards = [
        b for b in catalog._boards if not b.is_generic and b.esphome.board != "d1_mini"
    ]
    # Vendor entry sharing the same pio_board, alphabetically
    # earlier than the canonical entry — the same shape as the
    # aquaping/d1-mini collision in production.
    catalog._boards.insert(
        0,
        _board(
            board_id="aaa-vendor-product",
            platform=Platform.ESP8266,
            pio_board="d1_mini",
        ),
    )
    # Canonical entry — id matches the pio_board after _ <-> -
    # normalization. No is_generic flag, so the tiebreaker is the
    # only thing that picks it.
    catalog._boards.append(
        _board(
            board_id="d1-mini",
            platform=Platform.ESP8266,
            pio_board="d1_mini",
        ),
    )

    board = catalog.find_by_pio_board("d1_mini")

    assert board is not None
    assert board.id == "d1-mini"
    # Confirm the tiebreaker wins without is_generic (i.e. it
    # didn't accidentally short-circuit on the existing
    # generic-preference branch).
    assert board.is_generic is False


def test_find_by_pio_board_prefers_matching_variant(catalog: BoardCatalog) -> None:
    """When ``pio_variant`` is provided, prefer entries whose variant matches.

    Variant filter narrows the candidate pool *before* the generic
    preference applies. Add two same-pio different-variant entries
    (no generic among them) so the variant filter is the only thing
    that picks a winner.
    """
    catalog._boards.append(
        _board(
            board_id="alt-c3-board",
            platform=Platform.ESP32,
            variant=Esp32Variant.ESP32C3,
            pio_board="some-shared-pio",
        )
    )
    catalog._boards.append(
        _board(
            board_id="alt-s3-board",
            platform=Platform.ESP32,
            variant=Esp32Variant.ESP32S3,
            pio_board="some-shared-pio",
        )
    )

    board = catalog.find_by_pio_board("some-shared-pio", pio_variant="esp32s3")

    assert board is not None
    assert board.id == "alt-s3-board"


def test_find_by_pio_board_falls_back_to_first_when_variant_unmatched(
    catalog: BoardCatalog,
) -> None:
    """``pio_variant`` not matching any candidate → still return the first match.

    "Best effort" semantics — a YAML referencing a known PlatformIO
    board with a stale variant should still resolve to *something*
    rather than dropping the device from the dashboard.
    """
    board = catalog.find_by_pio_board("esp32-c3-devkitm-1", pio_variant="esp32c6-not-in-fixture")

    assert board is not None
    assert board.esphome.board == "esp32-c3-devkitm-1"


def test_find_by_pio_board_returns_none_for_unknown(catalog: BoardCatalog) -> None:
    """No matching ``esphome.board`` value → ``None``."""
    assert catalog.find_by_pio_board("nonexistent-board") is None


def test_find_by_pio_board_scopes_to_platform(catalog: BoardCatalog) -> None:
    """``platform`` disambiguates a pio_board shared across platforms.

    nRF52 and rp2040 both ship ``adafruit_itsybitsy``; a scoped lookup must
    return the matching platform, not whichever entry iterates first.
    """
    catalog._boards = [
        _board(
            board_id="adafruit_itsybitsy",
            platform=Platform.RP2040,
            pio_board="adafruit_itsybitsy",
        ),
        _board(
            board_id="adafruit_itsybitsy_nrf52",
            platform=Platform.NRF52,
            pio_board="adafruit_itsybitsy",
        ),
    ]
    rp = catalog.find_by_pio_board("adafruit_itsybitsy", platform=Platform.RP2040)
    nrf = catalog.find_by_pio_board("adafruit_itsybitsy", platform="nrf52")
    assert rp is not None and rp.esphome.platform is Platform.RP2040
    assert nrf is not None and nrf.esphome.platform is Platform.NRF52


def test_find_by_pio_board_scoped_miss_returns_none(catalog: BoardCatalog) -> None:
    """A pio_board that exists only on another platform → ``None`` when scoped.

    The caller then falls back (free-text pin field) rather than being handed a
    wrong-platform entry whose pins ESPHome would reject.
    """
    catalog._boards = [
        _board(
            board_id="adafruit_itsybitsy",
            platform=Platform.RP2040,
            pio_board="adafruit_itsybitsy",
        ),
    ]
    assert catalog.find_by_pio_board("adafruit_itsybitsy", platform=Platform.NRF52) is None
    # Unscoped still resolves (back-compat for callers without a platform).
    assert catalog.find_by_pio_board("adafruit_itsybitsy") is not None


# ---------------------------------------------------------------------------
# find_all_by_pio_board / get_compatible_boards
# ---------------------------------------------------------------------------


def test_find_all_by_pio_board_returns_every_sibling(catalog: BoardCatalog) -> None:
    """Every entry on a shared pio_board is returned, generics last."""
    boards = catalog.find_all_by_pio_board("esp32-c3-devkitm-1")

    assert [b.id for b in boards] == ["seeed-xiao-esp32c3", "generic-esp32c3"]


def test_find_all_by_pio_board_scopes_to_platform(catalog: BoardCatalog) -> None:
    """``platform`` (enum or string) drops siblings on other platforms."""
    catalog._boards = [
        _board(
            board_id="adafruit_itsybitsy",
            platform=Platform.RP2040,
            pio_board="adafruit_itsybitsy",
        ),
        _board(
            board_id="adafruit_itsybitsy_nrf52",
            platform=Platform.NRF52,
            pio_board="adafruit_itsybitsy",
        ),
    ]

    rp = catalog.find_all_by_pio_board("adafruit_itsybitsy", platform=Platform.RP2040)
    nrf = catalog.find_all_by_pio_board("adafruit_itsybitsy", platform="nrf52")

    assert [b.id for b in rp] == ["adafruit_itsybitsy"]
    assert [b.id for b in nrf] == ["adafruit_itsybitsy_nrf52"]


def test_find_all_by_pio_board_unknown_returns_empty(catalog: BoardCatalog) -> None:
    """No catalog entry on that pio_board → empty list."""
    assert catalog.find_all_by_pio_board("nonexistent-board") == []


async def test_get_compatible_boards_returns_siblings_including_self(
    catalog: BoardCatalog,
) -> None:
    """Returns every sibling on the board's pio target, the board included."""
    response = await catalog.get_compatible_boards(board_id="generic-esp32c3")

    assert [b.id for b in response.boards] == ["seeed-xiao-esp32c3", "generic-esp32c3"]
    assert response.total == 2


async def test_get_compatible_boards_lone_board_returns_only_itself(
    catalog: BoardCatalog,
) -> None:
    """A board with a unique pio target comes back as just itself."""
    response = await catalog.get_compatible_boards(board_id="m5stack-cores3")

    assert [b.id for b in response.boards] == ["m5stack-cores3"]
    assert response.total == 1


async def test_get_compatible_boards_unknown_id_returns_empty(
    catalog: BoardCatalog,
) -> None:
    """Unknown board id → empty page (not an exception)."""
    response = await catalog.get_compatible_boards(board_id="not-a-real-board")

    assert response.boards == []
    assert response.total == 0


# ---------------------------------------------------------------------------
# find_by_platform_variant
# ---------------------------------------------------------------------------


def test_find_by_platform_variant_prefers_generic_fallback(
    catalog: BoardCatalog,
) -> None:
    """When matches include a generic, prefer the generic.

    Documented in the function's docstring: a YAML naming only
    the platform should resolve to "Generic ESP32-C3" rather than
    a vendor-specific board that happens to share the variant.
    """
    board = catalog.find_by_platform_variant("esp32", variant="esp32c3")

    assert board is not None
    assert board.id == "generic-esp32c3"
    assert board.is_generic is True


def test_find_by_platform_variant_no_generic_returns_first(
    catalog: BoardCatalog,
) -> None:
    """When no generic exists for the variant, fall back to the first match.

    Removes the two generics so the helper has to land on the
    non-generic ESP32-S3 (M5Stack).
    """
    catalog._boards = [b for b in catalog._boards if not b.is_generic]

    board = catalog.find_by_platform_variant("esp32", variant="esp32s3")

    assert board is not None
    assert board.id == "m5stack-cores3"


def test_find_by_platform_variant_without_variant_falls_through(
    catalog: BoardCatalog,
) -> None:
    """No variant supplied → first matching platform entry (may be generic)."""
    board = catalog.find_by_platform_variant("esp8266")

    assert board is not None
    assert board.esphome.platform == Platform.ESP8266
    # The generic preference still kicks in when present.
    assert board.id == "generic-esp8266"


def test_find_by_platform_variant_unknown_platform_returns_none(
    catalog: BoardCatalog,
) -> None:
    """A platform not represented in the catalog → ``None``."""
    assert catalog.find_by_platform_variant("rp2040") is None


def test_find_by_platform_variant_empty_platform_returns_none(
    catalog: BoardCatalog,
) -> None:
    """Empty string short-circuits — guard against accidentally matching everything.

    Without the guard, an empty ``platform.value`` comparison
    would still succeed against entries whose ``platform`` is
    ``None`` / empty (none in our enum, but the early-return is
    cheap defense).
    """
    assert catalog.find_by_platform_variant("") is None


# ---------------------------------------------------------------------------
# load
# ---------------------------------------------------------------------------


def test_load_replaces_internal_list_from_index_loader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``load()`` swaps the internal list for what ``load_board_index`` returns.

    Patches the loader so the test doesn't depend on the on-disk
    artefacts. Pins the controller-loader contract: ``_boards``
    becomes the slim list and ``_known_ids`` is rebuilt to match
    so the lazy body store's ``is_known`` gate stays accurate.
    """
    fake_boards = [_board(board_id="from-loader", platform=Platform.ESP32)]

    monkeypatch.setattr(
        "esphome_device_builder.controllers.boards.load_board_index",
        lambda: fake_boards,
    )

    cat = BoardCatalog()
    assert cat._boards == []
    cat.load()
    assert cat._boards == fake_boards
    assert cat._known_ids == frozenset({"from-loader"})
