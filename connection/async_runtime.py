import asyncio

import nest_asyncio


global_loop = asyncio.new_event_loop()
nest_asyncio.apply()
asyncio.set_event_loop(global_loop)


def run_async(coro):
    """Run async coroutine safely using the persistent global loop."""
    global global_loop
    if global_loop.is_closed():
        global_loop = asyncio.new_event_loop()
        nest_asyncio.apply()
        asyncio.set_event_loop(global_loop)
    return global_loop.run_until_complete(coro)
