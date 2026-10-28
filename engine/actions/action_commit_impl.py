"""Implementation approval action handler."""

from .base import SyncActionHandler
from .common import require_approval


class action_commit_impl(SyncActionHandler):
    name = "commit_impl"

    def run(self, config, execute, output_path):
        approval = require_approval(config, ("impl", "implement"), oracle=True)
        return {**approval, "approved": True}
