"""Context：项目规则发现与后续上下文投影能力。"""

from mini_pi.context.compaction import CutBoundary, CutPoint, find_cut_point
from mini_pi.context.project import ProjectInstruction, load_project_instructions
from mini_pi.context.projection import (
    SUMMARY_TAG,
    CompactionProjection,
    MessageProjection,
    project_compaction,
    project_entry_path,
    project_messages,
)
from mini_pi.context.sections import (
    SystemPromptState,
    apply_section_patch,
    diff_sections,
    replay_system_messages,
)
from mini_pi.context.tokens import TokenEstimate, TokenSource, estimate_tokens

__all__ = [
    "SUMMARY_TAG",
    "CompactionProjection",
    "CutBoundary",
    "CutPoint",
    "MessageProjection",
    "ProjectInstruction",
    "SystemPromptState",
    "TokenEstimate",
    "TokenSource",
    "apply_section_patch",
    "diff_sections",
    "estimate_tokens",
    "find_cut_point",
    "load_project_instructions",
    "project_compaction",
    "project_entry_path",
    "project_messages",
    "replay_system_messages",
]
