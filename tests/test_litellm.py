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

asyncio.run(main())
