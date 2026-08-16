"""Host compiler: project law into a skill. Hooks are optional, not isolation."""

from .compile import (
    collect_compiler_input,
    compile_hosts,
    hosts_to_compile,
    projection_root,
    render_deny_hook,
    render_skill,
)
from .doctor import (
    assert_projections_allow_mutation,
    doctor_payload,
    inspect_projections,
    render_doctor_text,
)
from .models import (
    AUTHORITY_SKILL_AND_HOOK,
    AUTHORITY_SKILL_ONLY,
    HostDoctorReport,
    HostFinding,
    HostManifest,
    HostProjection,
)

__all__ = (
    "AUTHORITY_SKILL_AND_HOOK",
    "AUTHORITY_SKILL_ONLY",
    "HostDoctorReport",
    "HostFinding",
    "HostManifest",
    "HostProjection",
    "assert_projections_allow_mutation",
    "collect_compiler_input",
    "compile_hosts",
    "doctor_payload",
    "hosts_to_compile",
    "inspect_projections",
    "projection_root",
    "render_deny_hook",
    "render_doctor_text",
    "render_skill",
)
