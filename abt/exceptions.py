class AbtError(Exception):
    """Base exception for all abt errors."""


class ProjectNotFoundError(AbtError):
    """Raised when abt_project.yml is not found."""


class ProjectValidationError(AbtError):
    """Raised when project configuration is invalid."""


class SchemaNotFoundError(AbtError):
    """Raised when a referenced schema model is not found."""


class DuplicateSourceError(AbtError):
    """Raised when a source name is defined in multiple files."""


class SourceNotFoundError(AbtError):
    """Raised when a referenced source is not found."""


class PromptCompileError(AbtError):
    """Raised when a .prompt file fails to compile."""


class GraphBuildError(AbtError):
    """Raised when the graph cannot be assembled."""


class NodeExecutionError(AbtError):
    """Raised when a node fails during execution."""


class ToolExecutionError(AbtError):
    """Raised when a tool call fails."""


class AllNodesFailedError(AbtError):
    """Raised when all nodes in a require_any folder fail."""
