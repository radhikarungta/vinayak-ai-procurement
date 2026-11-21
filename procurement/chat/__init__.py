"""Ask: a chat assistant that answers questions from the app's own data.

- tools.py  Read-only tools the model can call. They call the feature packages in-process
            (cases, itemcodes, vendors, shortlist, quotes) and return compact JSON.
- api.py    POST /api/chat (stateless: the browser keeps the history), GET /api/chat/status.
"""
