"""Board and component definition loaders.

Definitions are stored in subdirectories:

    definitions/
    ├── boards/
    │   ├── esp32-devkit-v1/
    │   │   ├── manifest.yaml
    │   │   └── images/
    │   │       └── board-top.png
    │   └── ...
    └── components/
        ├── binary_sensor/
        │   └── manifest.yaml
        └── ...

To add a new board or component, create a subfolder with a manifest.yaml file.
See any existing manifest for the schema.
"""

from __future__ import annotations

import logging
from functools import cache
from pathlib import Path
from typing import NamedTuple

import orjson
import yaml

from ..helpers.lazy_catalog import is_unsafe_catalog_id
from ..helpers.yaml import FastestSafeLoader
from ..models import (
    BoardCatalogEntry,
    BoardCatalogIndex,
    BoardCatalogResponse,
    BoardEsphomeConfig,
    BoardHardware,
    BoardPin,
    BoardTag,
    Connectivity,
    DefaultComponent,
    Esp32Variant,
    FeaturedBundle,
    FeaturedComponent,
    FieldPreset,
    PinFeature,
    Platform,
)

_LOGGER = logging.getLogger(__name__)

_DEFINITIONS_DIR = Path(__file__).parent
_BOARDS_DIR = _DEFINITIONS_DIR / "boards"
_BOARDS_INDEX_JSON = _DEFINITIONS_DIR / "boards.index.json"
_BOARDS_BODIES_DIR = _DEFINITIONS_DIR / "board_bodies"
_FEATURED_COMPONENTS_INDEX_JSON = _DEFINITIONS_DIR / "featured_components.index.json"
_PIN_REGISTRY_MODES_INDEX_JSON = _DEFINITIONS_DIR / "pin_registry_modes.index.json"
_PLATFORM_CAPABILITIES_INDEX_JSON = _DEFINITIONS_DIR / "platform_capabilities.index.json"

_IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".svg", ".webp")
_GENERIC_DIR = _BOARDS_DIR / "_generic"


# ---------------------------------------------------------------------------
# Boards
# ---------------------------------------------------------------------------


def _local_to_url(local_path: Path) -> str:
    """Convert a local image path to a relative URL served by /boards/images."""
    # ``as_posix`` keeps the URL forward-slash separated on Windows,
    # where ``Path.relative_to`` would otherwise produce backslashes.
    rel = local_path.relative_to(_BOARDS_DIR).as_posix()
    return f"/boards/images/{rel}"


def _generic_image_url(platform: str, variant: str | None) -> str:
    """Return the URL for a generic chip image based on platform/variant."""
    # For ESP32, prefer variant-specific image (esp32s3.svg) over generic esp32.svg
    if variant:
        variant_svg = _GENERIC_DIR / f"{variant}.svg"
        if variant_svg.exists():
            return _local_to_url(variant_svg)
    platform_svg = _GENERIC_DIR / f"{platform}.svg"
    if platform_svg.exists():
        return _local_to_url(platform_svg)
    return ""


def _resolve_images(board_dir: Path, manifest_images: list[str] | None) -> list[str]:
    """Build the images list from manifest entries and local files.

    Local images are converted to relative URLs served by the
    /boards/images static route (e.g. /boards/images/esp32-devkit-v1/images/photo.png).
    External URLs are kept as-is.
    """
    images: list[str] = []

    # First: explicit entries from manifest (URLs or relative paths)
    for entry in manifest_images or []:
        if entry.startswith(("http://", "https://")):
            images.append(entry)
        else:
            # Resolve relative path against board directory
            local = board_dir / entry
            if local.exists():
                images.append(_local_to_url(local))

    # Then: auto-discover images in an images/ subfolder (not already listed)
    images_dir = board_dir / "images"
    if images_dir.is_dir():
        known = {p.rsplit("/", 1)[-1] for p in images}
        images.extend(
            _local_to_url(img)
            for img in sorted(images_dir.iterdir())
            if img.suffix.lower() in _IMAGE_EXTENSIONS and img.name not in known
        )

    return images


def _parse_pin_features(raw: list[str], board_id: str, gpio: int) -> list[PinFeature]:
    """Parse pin feature strings into PinFeature enums, logging unknowns."""
    features: list[PinFeature] = []
    for f in raw:
        try:
            features.append(PinFeature(f))
        except ValueError:
            _LOGGER.warning(
                "Board %s GPIO %d: unknown pin feature '%s' — skipping", board_id, gpio, f
            )
    return features


def _parse_tags(raw: list[str], board_id: str) -> list[BoardTag]:
    """Parse tag strings into BoardTag enums, logging unknowns."""
    tags: list[BoardTag] = []
    for t in raw:
        try:
            tags.append(BoardTag(t))
        except ValueError:
            _LOGGER.warning("Board %s: unknown tag '%s' — skipping", board_id, t)
    return tags


def _parse_connectivity(raw: list[str], board_id: str) -> list[Connectivity]:
    """Parse connectivity strings into Connectivity enums, logging unknowns."""
    result: list[Connectivity] = []
    for c in raw:
        try:
            result.append(Connectivity(c))
        except ValueError:
            _LOGGER.warning("Board %s: unknown connectivity '%s' — skipping", board_id, c)
    return result


def _load_pin(data: dict, board_id: str) -> BoardPin:
    """Load a BoardPin from a dict."""
    gpio = data["gpio"]
    return BoardPin(
        gpio=gpio,
        label=data.get("label", f"GPIO{gpio}"),
        features=_parse_pin_features(data.get("features", []), board_id, gpio),
        available=data.get("available"),
        occupied_by=data.get("occupied_by"),
        notes=data.get("notes"),
    )


def _coerce_field_preset(raw: object) -> FieldPreset:
    """
    Normalise the YAML representation of a field preset.

    Three accepted shapes:

    - primitive (string/number/bool/null) → ``FieldPreset(value=raw)``
    - list → ``FieldPreset(value=raw)`` (used for fields that take a list)
    - dict → parsed as the explicit ``{value, locked, suggestions}`` form

    Unknown keys in the dict form are silently dropped — schema validation
    in ``script/validate_definitions.py`` is the strict gate.
    """
    if isinstance(raw, dict):
        return FieldPreset(
            value=raw.get("value"),
            locked=bool(raw.get("locked", False)),
            suggestions=list(raw["suggestions"]) if "suggestions" in raw else None,
        )
    return FieldPreset(value=raw)  # type: ignore[arg-type]


def _resolve_featured_image(raw: object, board_dir: Path) -> str:
    """
    Resolve a featured entry's ``image_url`` the same way board images resolve.

    An ``http(s)://`` value passes through untouched; a relative path inside the
    board dir becomes its ``/boards/images/...`` URL when the file exists. An
    absolute path, a parent-dir escape, or a missing file is dropped (logged) so
    bad manifest data degrades to the component's generic image rather than
    raising in ``_local_to_url`` or emitting a traversal URL.
    """
    if not isinstance(raw, str) or not raw:
        return ""
    if raw.startswith(("http://", "https://")):
        return raw
    if Path(raw).is_absolute() or ".." in Path(raw).parts:
        _LOGGER.warning(
            "Board %s: featured image %r must be a path inside the board dir; skipping",
            board_dir.name,
            raw,
        )
        return ""
    local = board_dir / raw
    if local.is_file():
        return _local_to_url(local)
    _LOGGER.warning("Board %s: featured image %r not found; skipping", board_dir.name, raw)
    return ""


def _load_featured_component(data: dict, board_dir: Path) -> FeaturedComponent:
    """Load a FeaturedComponent from its YAML dict form."""
    raw_fields = data.get("fields") or {}
    fields = {key: _coerce_field_preset(val) for key, val in raw_fields.items()}
    return FeaturedComponent(
        id=data["id"],
        component_id=data["component_id"],
        name=data.get("name"),
        description=data.get("description"),
        fields=fields,
        image_url=_resolve_featured_image(data.get("image_url"), board_dir),
    )


def _load_featured_bundle(data: dict, board_dir: Path) -> FeaturedBundle:
    """Load a FeaturedBundle from its YAML dict form."""
    return FeaturedBundle(
        id=data["id"],
        name=data["name"],
        description=data.get("description", ""),
        component_ids=list(data.get("component_ids", [])),
        image_url=_resolve_featured_image(data.get("image_url"), board_dir),
    )


def _load_default_component(entry: object) -> DefaultComponent:
    """Normalize a default_components entry (string or object) into a DefaultComponent."""
    if isinstance(entry, str):
        return DefaultComponent(id=entry)
    if isinstance(entry, dict):
        return DefaultComponent(id=entry["id"], fields=dict(entry.get("fields") or {}))
    msg = f"default_components entry must be a string or object, got {type(entry).__name__}"
    raise TypeError(msg)


def _load_esphome_config(data: dict, board_id: str) -> BoardEsphomeConfig:
    """Load a BoardEsphomeConfig from a dict."""
    platform = Platform(data["platform"])
    variant_raw = data.get("variant")
    variant = Esp32Variant(variant_raw) if variant_raw else None
    return BoardEsphomeConfig(
        platform=platform,
        board=data["board"],
        variant=variant,
        framework=data.get("framework"),
    )


def _load_hardware(data: dict | None, board_id: str) -> BoardHardware:
    """Load a BoardHardware from a dict."""
    if not data:
        return BoardHardware()
    return BoardHardware(
        flash_size=data.get("flash_size"),
        ram_size=data.get("ram_size"),
        cpu_frequency=data.get("cpu_frequency"),
        connectivity=_parse_connectivity(data.get("connectivity", []), board_id),
    )


def build_board_catalog_from_manifests(*, strict: bool = False) -> BoardCatalogResponse:
    """
    Build the board catalog by parsing every ``manifest.yaml`` on disk.

    With ``strict=True`` a single broken manifest aborts the walk by
    re-raising; otherwise the offending board is skipped and the
    failure is logged.
    """
    boards: list[BoardCatalogEntry] = []

    for manifest in sorted(_BOARDS_DIR.glob("*/manifest.yaml")):
        try:
            # CSafeLoader (via FastestSafeLoader) is the libyaml-backed
            # equivalent of SafeLoader — same safe-only construction
            # surface, so the S506 ban on non-safe loaders is a false
            # positive.
            data = yaml.load(
                manifest.read_text(encoding="utf-8"),
                Loader=FastestSafeLoader,  # noqa: S506
            )
            board_dir = manifest.parent
            board_id = board_dir.name

            esphome_cfg = _load_esphome_config(data["esphome"], board_id)
            images = _resolve_images(board_dir, data.get("images"))

            # Fall back to generic chip image when no specific image exists
            if not images:
                generic = _generic_image_url(
                    esphome_cfg.platform.value,
                    esphome_cfg.variant.value if esphome_cfg.variant else None,
                )
                if generic:
                    images = [generic]

            boards.append(
                BoardCatalogEntry(
                    id=data["id"],
                    name=data["name"],
                    description=data["description"],
                    manufacturer=data.get("manufacturer", ""),
                    esphome=esphome_cfg,
                    hardware=_load_hardware(data.get("hardware"), board_id),
                    images=images,
                    tags=_parse_tags(data.get("tags", []), board_id),
                    pins=[_load_pin(p, board_id) for p in data.get("pins", [])],
                    docs_url=data.get("docs_url", ""),
                    product_url=data.get("product_url", ""),
                    featured=data.get("featured", False),
                    is_generic=data.get("is_generic", False),
                    featured_components=[
                        _load_featured_component(fc, board_dir)
                        for fc in data.get("featured_components", [])
                    ],
                    featured_bundles=[
                        _load_featured_bundle(fb, board_dir)
                        for fb in data.get("featured_bundles", [])
                    ],
                    default_components=[
                        _load_default_component(d) for d in data.get("default_components", [])
                    ],
                )
            )
        except Exception:
            if strict:
                raise
            _LOGGER.exception("Failed to load board definition from %s", manifest.parent.name)

    return BoardCatalogResponse(boards=boards)


def load_board_index() -> list[BoardCatalogIndex]:
    """Load the slim board index from ``definitions/boards.index.json``.

    Returns an empty list (with a logged warning) when the file is
    missing or fails to decode — never raises, so a malformed
    artefact can't take dashboard startup down with it.
    """
    if not _BOARDS_INDEX_JSON.exists():
        _LOGGER.warning(
            "boards.index.json missing — board catalog will be empty. "
            "Run script/sync_boards.py to generate the artefact.",
        )
        return []
    try:
        payload = orjson.loads(_BOARDS_INDEX_JSON.read_bytes())
        return [BoardCatalogIndex.from_dict(entry) for entry in payload["boards"]]
    except Exception:
        _LOGGER.exception(
            "Failed to load boards.index.json — board catalog will be empty. "
            "Run script/sync_boards.py to regenerate the artefact.",
        )
        return []


def load_board_body_from_disk(board_id: str) -> BoardCatalogEntry | None:
    """Load one ``board_bodies/<id>.json`` body file by id, or ``None``.

    Returns ``None`` for traversal-shaped ids, missing files, and
    decode failures — the LazyBodyStore caller already short-circuits
    via the slim index's ``is_known`` gate, so this is the rare
    half-installed-wheel fallback.
    """
    if is_unsafe_catalog_id(board_id):
        _LOGGER.warning("Refusing board body for traversal-shaped id: %r", board_id)
        return None
    path = _BOARDS_BODIES_DIR / f"{board_id}.json"
    if not path.exists():
        return None
    try:
        return BoardCatalogEntry.from_dict(orjson.loads(path.read_bytes()))
    except Exception:
        _LOGGER.exception("Failed to load board body %s", path)
        return None


def load_featured_components_index() -> dict[str, list[FeaturedComponent]]:
    """Load the aggregated ``{board_id: list[FeaturedComponent]}`` index.

    Read once at startup by the components controller to build its
    cross-catalog featured-component registry without ever touching
    per-board body files. Missing / malformed artefact yields an
    empty map; the registry just has no featured components for any
    board in that degenerate case.
    """
    if not _FEATURED_COMPONENTS_INDEX_JSON.exists():
        _LOGGER.warning(
            "featured_components.index.json missing — featured components will be empty. "
            "Run script/sync_boards.py to generate the artefact.",
        )
        return {}
    try:
        payload = orjson.loads(_FEATURED_COMPONENTS_INDEX_JSON.read_bytes())
    except Exception:
        _LOGGER.exception(
            "Failed to load featured_components.index.json — featured components will be empty."
        )
        return {}
    return {
        board_id: [FeaturedComponent.from_dict(fc) for fc in entries]
        for board_id, entries in payload.items()
    }


def load_pin_registry_modes_index() -> dict[str, list[str]]:
    """Load the aggregated ``{registry_key: [allowed_modes]}`` map.

    Read once at startup by the components controller so the frontend can
    scope the long-form pin Mode checkboxes per registry. Missing / malformed
    artefact yields an empty map; the frontend then shows every mode flag (the
    pre-scoping behaviour).
    """
    if not _PIN_REGISTRY_MODES_INDEX_JSON.exists():
        _LOGGER.warning(
            "pin_registry_modes.index.json missing — pin Mode flags won't be "
            "scoped per registry. Run script/sync_components.py to generate it.",
        )
        return {}
    try:
        payload = orjson.loads(_PIN_REGISTRY_MODES_INDEX_JSON.read_bytes())
    except Exception:
        _LOGGER.exception(
            "Failed to load pin_registry_modes.index.json — pin Mode flags won't be scoped."
        )
        return {}
    if not isinstance(payload, dict):
        _LOGGER.warning(
            "pin_registry_modes.index.json is not a mapping — ignoring; pin Mode "
            "flags won't be scoped."
        )
        return {}
    # Tolerate a malformed artefact: drop any entry whose value isn't a list,
    # keep only string flags, so a partial / hand-mangled file degrades to
    # "show every flag" rather than crashing startup.
    return {
        str(key): [str(m) for m in modes if isinstance(m, str)]
        for key, modes in payload.items()
        if isinstance(modes, list)
    }


class PlatformCapabilities(NamedTuple):
    """Static esphome platform metadata snapshotted by script/sync_components.py."""

    esp32_variants: list[str]
    esp32_no_wifi_variants: list[str]
    libretiny_families: list[str]
    rp2040_no_wifi_boards: list[str]
    # ``{component: [{title, description, file}]}`` for the platforms whose
    # download types are static (esp32 / esp8266 / rp2040). Build-dir-dependent
    # platforms (libretiny / nrf52) are absent and resolved via subprocess.
    download_types: dict[str, list[dict[str, str]]]


@cache
def load_platform_capabilities_index() -> PlatformCapabilities:
    """Load the static platform metadata the main process uses instead of esphome.

    Read off a cheap JSON instead of importing ``esphome.components.esp32`` / ``.wifi``
    (which pull espidf / requests / esphome.config). Cached so the several import-time
    callers share one parse. Missing / malformed artefact yields empty lists,
    degrading download routing to "return the raw platform" and wifi inference to
    "assume wifi" (fail-open). Regenerate with script/sync_components.py.
    """
    return _load_platform_capabilities(_PLATFORM_CAPABILITIES_INDEX_JSON)


def _load_platform_capabilities(path: Path) -> PlatformCapabilities:
    """Parse a platform-capabilities index at *path*; empty on missing / malformed."""
    empty = PlatformCapabilities([], [], [], [], {})
    if not path.exists():
        _LOGGER.warning(
            "platform_capabilities.index.json missing — download routing + wifi "
            "inference degraded. Run script/sync_components.py to generate it.",
        )
        return empty
    try:
        payload = orjson.loads(path.read_bytes())
    except Exception:
        _LOGGER.exception("Failed to load platform_capabilities.index.json — degraded.")
        return empty
    if not isinstance(payload, dict):
        _LOGGER.warning("platform_capabilities.index.json is not a mapping — ignoring.")
        return empty

    def _str_list(key: str) -> list[str]:
        value = payload.get(key)
        if not isinstance(value, list):
            return []
        return [str(item) for item in value if isinstance(item, str)]

    return PlatformCapabilities(
        esp32_variants=_str_list("esp32_variants"),
        esp32_no_wifi_variants=_str_list("esp32_no_wifi_variants"),
        libretiny_families=_str_list("libretiny_families"),
        rp2040_no_wifi_boards=_str_list("rp2040_no_wifi_boards"),
        download_types=_parse_download_types(payload.get("download_types")),
    )


def coerce_download_entries(value: object) -> list[dict[str, str]]:
    """Coerce a download-types list to ``[{title, description, file}]``.

    Drops anything that isn't a dict with a string ``file``; returns ``[]`` for a
    non-list. The validation boundary for both the generated index and the
    device-builder-helper subprocess reply, so a malformed payload can't reach a
    downstream ``entry["file"]``.
    """
    if not isinstance(value, list):
        _LOGGER.warning("download-types payload is not a list: %s", type(value).__name__)
        return []
    clean = [
        {
            "title": str(entry.get("title", "")),
            "description": str(entry.get("description", "")),
            "file": entry["file"],
        }
        for entry in value
        if isinstance(entry, dict) and isinstance(entry.get("file"), str)
    ]
    if len(clean) != len(value):
        _LOGGER.warning(
            "Dropped %d malformed download-type entry(ies) of %d",
            len(value) - len(clean),
            len(value),
        )
    return clean


def _parse_download_types(value: object) -> dict[str, list[dict[str, str]]]:
    """Coerce the index ``download_types`` block, dropping any malformed entry."""
    if not isinstance(value, dict):
        return {}
    return {
        str(component): coerce_download_entries(entries)
        for component, entries in value.items()
        if isinstance(entries, list)
    }


def load_board_catalog() -> BoardCatalogResponse:
    """Reassemble the full board catalog from the split artefacts.

    Convenience for tests + the manifest-drift check; eager-loads
    every body file. Runtime callers should prefer
    :func:`load_board_index` + per-id :func:`load_board_body_from_disk`
    (or the controller's ``LazyBodyStore``) so bodies don't all sit
    resident at once.
    """
    boards: list[BoardCatalogEntry] = []
    for slim in load_board_index():
        body = load_board_body_from_disk(slim.id)
        if body is None:
            _LOGGER.warning("Board body missing for id %r — skipping", slim.id)
            continue
        boards.append(body)
    return BoardCatalogResponse(boards=boards)
