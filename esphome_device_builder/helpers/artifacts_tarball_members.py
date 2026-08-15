"""Member names of the remote-build artifacts tarball."""

from __future__ import annotations

# Tarball member names that ride alongside the build tree. The
# receiver-side packer (``controllers.remote_build.artifacts_tarball``)
# writes them; the offloader-side materialiser
# (``helpers.remote_artifacts_materialise``) pulls them out and stages
# them at the offloader's canonical cache locations; the WS-adapter
# (``unpack_artifacts_response``) ignores ``storage.json`` /
# ``platformio.ini`` (they're not flash images) and reads
# ``idedata.json`` to recover the upstream-canonical flash-image
# manifest.
STORAGE_MEMBER_NAME = "storage.json"
IDEDATA_MEMBER_NAME = "idedata.json"
PLATFORMIO_INI_MEMBER_NAME = "platformio.ini"
# Read by the offloader's ``read_build_info_hash`` to populate
# ``expected_config_hash`` post-build (see #654).
BUILD_INFO_MEMBER_NAME = "build_info.json"
