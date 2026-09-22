"""Context：项目规则发现与后续上下文投影能力。"""

from mini_pi.context.project import ProjectInstruction, load_project_instructions
from mini_pi.context.projection import project_entry_path
from mini_pi.context.sections import (
    SystemPromptState,
    apply_section_patch,
    diff_sections,
    replay_system_messages,
)

__all__ = [
    "ProjectInstruction",
    "SystemPromptState",
    "apply_section_patch",
    "diff_sections",
    "load_project_instructions",
    "project_entry_path",
    "replay_system_messages",
]
