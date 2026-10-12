# unprofane

The Python client for [Unprofane](https://unprofane.com), a profanity and toxicity API that is hard to fool.

```bash
pip install unprofane
```

```python
from unprofane import Unprofane

client = Unprofane("up_live_YOUR_KEY")
r = client.check("xXsh1tXx", context="username", policy="teen")
if not r["allowed"]:
    print(r["action"], r["evasion"], r["censored"])
```

Async:

```python
from unprofane import AsyncUnprofane

async with AsyncUnprofane("up_live_YOUR_KEY") as client:
    r = await client.check("hello", context="comment")
```

`check`, `check_batch` (up to 100), `check_many` (any number, chunked), `feedback`, `usage`, `get_list`, `put_list`, `delete_list`. Retries 429 and 5xx with jitter and honours `Retry-After`; other failures raise `UnprofaneError` with the problem details and the request id. Docs: https://unprofane.com/docs/.
