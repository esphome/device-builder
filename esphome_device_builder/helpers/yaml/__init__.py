"""Utilities for generating and modifying ESPHome YAML config files."""

from __future__ import annotations

import yaml

# Prefer the libyaml-backed C loader when PyYAML was built against
# libyaml. On the M5 MacBook Pro, parsing the full board catalog
# (492 manifests) drops from 1.6s to 210ms — a ~7-8x speedup that
# directly cuts dashboard startup wall-time. Mirrors ESPHome's own
# ``yaml_util.FastestAvailableSafeLoader`` so a future audit
# against upstream lands on the same name. PyYAML wheels ship the
# C extension on every platform we target; the SafeLoader fallback
# is for the rare source install against a libyaml-less build.
#
# We deliberately do NOT replicate the upstream ``parse_yaml``
# C-then-pure-Python retry-on-YAMLError pattern. ESPHome surfaces
# the parse error to the user's terminal and uses the pure-Python
# loader's readable error message; every device-builder load site
# either swallows ``yaml.YAMLError`` (mqtt block, secrets file)
# or catches it inside the outer ``except Exception`` of the
# board-catalog walk where the manifest is our own internal data
# linted by ``script/validate_definitions.py``. A double parse
# would cost us per-error wall-time with no user-visible benefit.
try:
    FastestSafeLoader: type = yaml.CSafeLoader
except AttributeError:  # pragma: no cover
    # PyYAML wheels on every platform we ship to bundle libyaml,
    # so the fallback is never exercised in CI; ``# pragma: no
    # cover`` keeps Codecov honest about the patch-coverage number.
    FastestSafeLoader = yaml.SafeLoader


# Re-exports at the bottom. Redundant-alias form marks these as
# intentional re-exports (PEP 484) so external callers'
# ``from .helpers.yaml import X`` keeps working unchanged across
# the split arc.
from .api_encryption import generate_api_encryption_key as generate_api_encryption_key
from .api_encryption import rewrite_api_encryption_key as rewrite_api_encryption_key
from .component import _splice_into_domain_block as _splice_into_domain_block
from .component import generate_component_yaml as generate_component_yaml
from .component import merge_component_yaml as merge_component_yaml
from .inline import _indent_block as _indent_block
from .inline import remove_inline_handler as remove_inline_handler
from .inline import upsert_inline_handler as upsert_inline_handler
from .scalar import ESPHOME_YAML_INDENT as ESPHOME_YAML_INDENT
from .scalar import YamlUpsertNotSupportedError as YamlUpsertNotSupportedError
from .scalar import _quote as _quote
from .scalar import _safe_yaml_scalar as _safe_yaml_scalar
from .scalar import _strip_yaml_quotes as _strip_yaml_quotes
from .scalar import read_yaml_scalar as read_yaml_scalar
from .scalar import rewrite_yaml_scalar as rewrite_yaml_scalar
from .substitution import parse_substitution_ref as parse_substitution_ref
from .substitution import rewrite_name_or_substitution as rewrite_name_or_substitution
from .top_block import (
    upsert_yaml_leaf_under_top_block as upsert_yaml_leaf_under_top_block,
)
