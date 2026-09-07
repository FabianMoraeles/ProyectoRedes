"""Optional browser front end for the chatbot.

This is an alternative *presentation* layer, not a second host: it drives the
same :class:`~adoptamatch_chatbot.app.ChatApp`, the same
:class:`~adoptamatch_chatbot.mcp_host.manager.MCPManager` and the same
:class:`~adoptamatch_chatbot.llm.base.LLMProvider` the terminal uses. Only
:mod:`~adoptamatch_chatbot.web.presenter` and :mod:`~adoptamatch_chatbot.web.server`
are new; nothing about the conversation loop, MCP routing or logging changes
based on which presentation layer is attached.
"""
