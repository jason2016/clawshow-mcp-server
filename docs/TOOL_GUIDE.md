# Tool Development Guide

## File Naming

```
tools/{function_name}.py
```

One file per tool. Name matches the primary action (e.g. `stripe_payment.py`, `notification.py`).

## File Structure

Follow `tools/stripe_payment.py` as the reference template:

```python
"""
Tool: {tool_name}
------------------
One-line description of what it does.

Env required:
  ENV_VAR_NAME — description
"""

from __future__ import annotations

import os
import json
from datetime import datetime, timezone
from typing import Callable


def register(mcp, record_call: Callable) -> None:

    @mcp.tool()
    def tool_name(
        required_param: str,
        optional_param: str = "default",
    ) -> str:
        """
        English description for AI discoverability.
        
        Call this tool when [trigger conditions].
        
        Examples of natural language that should trigger this tool:
        - 'English example'
        - 'French example'
        
        Args:
            required_param: Description
            optional_param: Description
        
        Returns:
            JSON string with result fields.
        """
        record_call("tool_name")
        
        # Implementation
        result = {...}
        return json.dumps(result, ensure_ascii=False)
```

## Rules

### Descriptions
- **MUST be in English** — AI agents worldwide need to understand
- Include 3-5 natural language trigger examples (English + French)
- Start with what the tool does, then when to call it

### Input/Output
- All parameters typed with defaults where possible
- Return type is always `str` (JSON-encoded)
- Error responses: `{"status": "error", "message": "clear explanation"}`
- Success responses: include relevant IDs, timestamps, URLs

### Environment Variables
- Named: `SERVICE_SECRET_KEY` (e.g. `STRIPE_SECRET_KEY`, `RESEND_API_KEY`)
- Read via `os.environ.get("KEY", "")` inside the tool function
- Missing key → return JSON error, don't raise exception
- Import heavy libraries (`stripe`, `resend`) inside the function, not at module top

### Error Handling
- Wrap external API calls in try/except
- Return structured JSON errors, never let exceptions bubble up
- Single item failure must not block batch operations (see `notification.py`)

### Data Storage
- Path: `data/{engine}/{namespace}/`
- ID format: `PREFIX-YYYYMMDD-NNN` (e.g. `ORD-20260402-001`)
- Files: one JSON per record, filename = ID
- Auto-create directories with `mkdir(parents=True, exist_ok=True)`

### Registration
After creating the tool file, register in `server.py`:

```python
from tools.my_tool import register as _register_my_tool
_register_my_tool(mcp, _record_call)
```

### Testing
```bash
# Verify import succeeds
python -c "import sys; sys.path.insert(0,'.'); from tools.my_tool import register; print('OK')"

# Verify all tools register
python -c "
import sys; sys.path.insert(0, '.')
from dotenv import load_dotenv; load_dotenv()
from server import mcp
for t in mcp._tool_manager.list_tools():
    print(t.name)
"
```
