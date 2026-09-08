"""Optional browser front end for the chatbot.

This is an alternative *presentation* layer, not a second host: it drives the
same :class:`~adoptforme_chatbot.app.ChatApp`, the same
:class:`~adoptforme_chatbot.mcp_host.manager.MCPManager` and the same
:class:`~adoptforme_chatbot.llm.base.LLMProvider` the terminal uses. Only
:mod:`~adoptforme_chatbot.web.presenter` and :mod:`~adoptforme_chatbot.web.server`
are new; nothing about the conversation loop, MCP routing or logging changes
based on which presentation layer is attached.
"""
