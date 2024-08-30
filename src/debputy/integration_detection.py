from typing import Container, Optional, Mapping

from debputy.plugin.api.spec import (
    DebputyIntegrationMode,
    INTEGRATION_MODE_DH_DEBPUTY_RRR,
    INTEGRATION_MODE_DH_DEBPUTY,
    INTEGRATION_MODE_FULL,
)


def determine_debputy_integration_mode(
    source_fields: Mapping[str, str],
    all_sequences: Container[str],
) -> Optional[DebputyIntegrationMode]:

    if source_fields.get("Build-Driver", "").lower() == "debputy":
        return INTEGRATION_MODE_FULL

    has_zz_debputy = "zz-debputy" in all_sequences or "debputy" in all_sequences
    has_zz_debputy_rrr = "zz-debputy-rrr" in all_sequences
    has_any_existing = has_zz_debputy or has_zz_debputy_rrr
    if has_zz_debputy_rrr:
        return INTEGRATION_MODE_DH_DEBPUTY_RRR
    if has_any_existing:
        return INTEGRATION_MODE_DH_DEBPUTY
    if source_fields.get("Source", "") == "debputy":
        # Self-hosting. We cannot set the Build-Driver field since that creates a self-circular dependency loop
        return INTEGRATION_MODE_FULL
    return None
