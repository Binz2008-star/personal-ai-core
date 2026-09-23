"""Commands whose behaviour against unified-llm-local @ 21a36b0 is pinned.

Shared by the characterization tests (which assert what the SOURCE does) and
tests/unit/test_agent_commands.py (which holds the Core to it, or to a
numbered fix). One list, so the two cannot drift apart.
"""

# The source refuses these, and so must the Core: the parity half of ADAPT.
SOURCE_REFUSES = [
    "rm -rf build",
    "sudo ls",
    "curl http://example.com",
    "bash -c ls",
    "ls; rm x",
    "cat $(whoami)",
    "git push origin main",
    "git reset --hard",
    "",
]

# The source ALLOWS these. Each is closed in agent/commands.py by the fix
# with that number.
SOURCE_LETS_THROUGH = [
    ("python script.py", 1),
    ("npx some-package", 1),
    ("find . -delete", 2),
    ("git commit -m x", 3),
    ("git add .", 3),
    ("git stash", 4),
    ("git -c core.pager=less log", 5),
    ("git diff --output=report.txt", 5),
    ("./ls", 6),
    ("cat /etc/passwd", 7),
    ("grep -r key ~", 7),
    ("git remote add origin url", 8),
]
