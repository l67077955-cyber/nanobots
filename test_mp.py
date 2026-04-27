import sys, asyncio
sys.path.append('/root/nanobot-src')
from nanobot.tools.memory_palace import MemoryPalaceTool

async def test():
    t = MemoryPalaceTool()
    
    # Test 1: missing content
    r = await t.execute("store")
    print("missing content len:", len(r), repr(r))
    
    # Test 2: invalid wing
    r = await t.execute("store", content="x", wing="invalid wing!!", room="r")
    print("invalid wing len:", len(r), repr(r))

    # Test 3: too long content
    r = await t.execute("store", content="x"*200000, wing="w", room="r")
    print("too long len:", len(r), repr(r))

asyncio.run(test())
