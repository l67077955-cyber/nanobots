"""Manual litellm smoke-test (429 handling).

Run directly:  python scripts/test_litellm.py

NOTE: guarded by __main__ so importing this module has no side effects
(no live API call, no env mutation).
"""

import asyncio
import litellm
import os

async def main():
    litellm.api_base = "https://openrouter.ai/api/v1"
    os.environ["OPENROUTER_API_KEY"] = os.environ.get("OPENROUTER_API_KEY", "dummy")
    try:
        response = await litellm.acompletion(
            model="openrouter/deepseek-v4-flash", # Intentional bad model to test 429
            messages=[{"role": "user", "content": "hello"}],
        )
        print(response)
    except Exception as e:
        print(f"Exception: {type(e).__name__}: {e}")

if __name__ == "__main__":
    asyncio.run(main())
