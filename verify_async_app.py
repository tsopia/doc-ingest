import asyncio
import time
import os
import sys
from loguru import logger
from langfuse import Langfuse, observe
from starlette.concurrency import iterate_in_threadpool
from dotenv import load_dotenv

load_dotenv()

# Force debug logging
logger.remove()
logger.add(sys.stderr, level="DEBUG")

# Config from Env
PUBLIC_KEY = os.getenv("DOC_INGEST__LANGFUSE__PUBLIC_KEY")
SECRET_KEY = os.getenv("DOC_INGEST__LANGFUSE__SECRET_KEY")
HOST = os.getenv("DOC_INGEST__LANGFUSE__HOST")

print(f"DEBUG: PK={PUBLIC_KEY}, SK={SECRET_KEY}, HOST={HOST}")

os.environ["LANGFUSE_PUBLIC_KEY"] = PUBLIC_KEY
os.environ["LANGFUSE_SECRET_KEY"] = SECRET_KEY
os.environ["LANGFUSE_HOST"] = HOST

# Init - relies on Env vars



class ModelService:
    @observe() # Child span
    def process_stream_sync(self):
        print("DEBUG: Inside ModelService.process_stream_sync (Generator)")
        yield "chunk1"
        time.sleep(0.1)
        yield "chunk2"
        time.sleep(0.1)
        yield "chunk3"

class ParserService:
    def __init__(self):
        self.model = ModelService()

    @observe() # Root trace
    async def process_workflow(self):
        print("DEBUG: Inside ParserService.process_workflow (Async Generator)")
        yield "start"

        # Simulate app logic: iterating sync generator in threadpool
        print("DEBUG: Starting threadpool iteration")
        async for chunk in iterate_in_threadpool(self.model.process_stream_sync()):
            yield f"model:{chunk}"

        yield "end"

async def main():
    print("DEBUG: Starting Main")
    service = ParserService()

    async for event in service.process_workflow():
        print(f"Received: {event}")

    print("DEBUG: Main Finished. ")
    # langfuse_context.flush() # Not available
    # Wait for background flush?
    time.sleep(2)
    print("DEBUG: Flush complete")

if __name__ == "__main__":
    if not PUBLIC_KEY:
        print("ERROR: Env vars not set. Run with env vars.")
        sys.exit(1)
    asyncio.run(main())
