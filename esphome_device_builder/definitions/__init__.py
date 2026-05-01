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
from pathlib import Path

import yaml

from ..models import (
    BoardCatalogEntry,
    BoardCatalogResponse,
    BoardEsphomeConfig,
    BoardHardware,
    BoardPin,
    BoardTag,
    Connectivity,
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

_IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".svg", ".webp")
_GENERIC_DIR = _BOARDS_DIR / "_generic"


# ---------------------------------------------------------------------------
# Boards
# ---------------------------------------------------------------------------


def _local_to_url(local_path: Path) -> str:
    """Convert a local image path to a relative URL served by /boards/images."""
    rel = local_path.relative_to(_BOARDS_DIR)
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


def _load_featured_component(data: dict) -> FeaturedComponent:
    """Load a FeaturedComponent from its YAML dict form."""
    raw_fields = data.get("fields") or {}
    fields = {key: _coerce_field_preset(val) for key, val in raw_fields.items()}
    return FeaturedComponent(
        id=data["id"],
        component_id=data["component_id"],
        name=data.get("name"),
        description=data.get("description"),
        fields=fields,
    )


def _load_featured_bundle(data: dict) -> FeaturedBundle:
    """Load a FeaturedBundle from its YAML dict form."""
    return FeaturedBundle(
        id=data["id"],
        name=data["name"],
        description=data.get("description", ""),
        component_ids=list(data.get("component_ids", [])),
    )


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


def load_board_catalog() -> BoardCatalogResponse:
    """Load all board definitions from subdirectories."""
    boards: list[BoardCatalogEntry] = []

    for manifest in sorted(_BOARDS_DIR.glob("*/manifest.yaml")):
        try:
            data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
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
                        _load_featured_component(fc) for fc in data.get("featured_components", [])
                    ],
                    featured_bundles=[
                        _load_featured_bundle(fb) for fb in data.get("featured_bundles", [])
                    ],
                )
            )
        except Exception:
            _LOGGER.exception("Failed to load board definition from %s", manifest.parent.name)

    return BoardCatalogResponse(boards=boards)
