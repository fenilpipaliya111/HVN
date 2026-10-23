"""Request PSU off and both pumps stopped using the modular controller."""
import asyncio
from bo_loop import stop_equipment

if __name__ == "__main__":
    asyncio.run(stop_equipment())
