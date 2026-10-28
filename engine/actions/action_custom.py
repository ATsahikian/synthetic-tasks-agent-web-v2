"""Custom action handler."""

from .base import AgentActionHandler


class action_custom(AgentActionHandler):
    name = "custom"
    parameters = frozenset({"agent", "prompt"})
    required_parameters = frozenset({"prompt"})

    def run(self, config, execute, output_path):
        context = self.context(config)
        prompt = context + (
            "Apply the original user command to this task only. Follow the Cyber Bench guide's model-role rules. "
            "Never author instruction.md or writeup.md yourself; required edits must be delegated to a fresh first-party Muse session from the original approved sources. "
            "Do not relax protected-authoring boundaries or claim validation that was not run."
        )
        self.execute_agent(config, execute, prompt)
        return {"agent": config["execution"]["agent"]}
