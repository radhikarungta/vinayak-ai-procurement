"""Email allow/block rules, applied to inbox mail before any Claude call. See rules.py."""

from .rules import HOW_RULES_COMBINE, EmailFacts, Rule, RuleError, active_rules, env_rules, evaluate

__all__ = ["HOW_RULES_COMBINE", "EmailFacts", "Rule", "RuleError", "active_rules", "env_rules", "evaluate"]
