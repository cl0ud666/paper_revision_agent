"""Prompt templates grouped by role; shared policies have a single source."""

from .audit import (
    AUDIT_HEADER as AUDIT_HEADER,
    AUDIT_SYSTEM as AUDIT_SYSTEM,
    NAME_CHECK_HEADER as NAME_CHECK_HEADER,
    NAME_CHECK_SYSTEM as NAME_CHECK_SYSTEM,
    render_audit_prompt as render_audit_prompt,
    render_name_check_prompt as render_name_check_prompt,
)
from .consistency import (
    CROSS_CHECK_HEADER as CROSS_CHECK_HEADER,
    CROSS_CHECK_SYSTEM as CROSS_CHECK_SYSTEM,
    TARGETED_CHECK_HEADER as TARGETED_CHECK_HEADER,
    TARGETED_CHECK_SYSTEM as TARGETED_CHECK_SYSTEM,
    WHOLE_PAPER_HEADER as WHOLE_PAPER_HEADER,
    WHOLE_PAPER_SYSTEM as WHOLE_PAPER_SYSTEM,
    render_cross_check_prompt as render_cross_check_prompt,
    render_targeted_check_prompt as render_targeted_check_prompt,
    render_whole_paper_prompt as render_whole_paper_prompt,
)
from .planning import (
    ORCHESTRATOR_HEADER as ORCHESTRATOR_HEADER,
    ORCHESTRATOR_SYSTEM as ORCHESTRATOR_SYSTEM,
    render_orchestrator_prompt as render_orchestrator_prompt,
)
from .policies import (
    DECISION_POLICY as DECISION_POLICY,
    TERM_POLICY as TERM_POLICY,
)
from .review import (
    REVIEW_HEADER as REVIEW_HEADER,
    REVIEW_SYSTEM as REVIEW_SYSTEM,
    render_review_prompt as render_review_prompt,
)
from .revision import (
    REVISION_HEADER as REVISION_HEADER,
    REVISION_SYSTEM as REVISION_SYSTEM,
    render_patch_prompt as render_patch_prompt,
)
