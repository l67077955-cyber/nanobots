import sys, asyncio
sys.path.append('/root/nanobot-src')
from nanobot.tools.memory_palace import MemoryPalaceTool

async def test():
    t = MemoryPalaceTool()
    content = "a" * 90000
    r = await t.execute("store", content=content, wing="wing_test", room="test-room")
    print("len:", len(r), repr(r))

asyncio.run(test())
