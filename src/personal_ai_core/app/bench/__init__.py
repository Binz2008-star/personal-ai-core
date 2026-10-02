"""The capability benchmark (ADR-022): does the system do the task?

`tasks` loads and validates a task file, `checks` judges a finished run
mechanically, and `policy` answers the agent's approval questions without a
person, under the rules ADR-022 D3 sets. The runner is a separate unit.
"""
