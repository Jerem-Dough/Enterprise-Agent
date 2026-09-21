"""Harmony Harness: an extendable agent harness for enterprise work.

The kernel is deliberately thin. It owns the loop (detect, gather, plan, gate,
execute, follow up) and nothing about purchasing, quality, or any other domain.
Everything domain-shaped is registered from a folder: providers, tools,
detectors, workflows. See CLAUDE.md for routing and each package's CONTEXT.md
for its contract.
"""

__version__ = "0.1.0"
