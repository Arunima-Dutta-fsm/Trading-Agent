"""AABA multi-portfolio trading agent.

Plain summary: one CrewAI agent (one LLM) runs three student portfolios through a
local MCP server. Code, not the LLM, works out signals, checks risk and places
paper orders. BUYs go through automatically after every check passes. SELLs wait
for the right student's approval. HOLDs place no order.
"""

__version__ = "1.0.0"
