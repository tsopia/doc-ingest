import os
import time
from app.config import get_settings
from langfuse import Langfuse
from loguru import logger

# Force debug logging
import sys
logger.remove()
logger.add(sys.stderr, level="DEBUG")

def verify():
    print("--- 1. Check Config Loading ---")
    settings = get_settings()
    lf_settings = settings.langfuse

    print(f"Public Key loaded: {'YES' if lf_settings.public_key else 'NO'} ({lf_settings.public_key[:4]}...)")
    print(f"Secret Key loaded: {'YES' if lf_settings.secret_key else 'NO'} ({lf_settings.secret_key[:4]}...)")
    print(f"Host: {lf_settings.host}")

    if not (lf_settings.public_key and lf_settings.secret_key):
        print("ERROR: credentials missing.")
        return

    print("\n--- 2. Initialize Langfuse Client ---")
    try:
        langfuse = Langfuse(
            public_key=lf_settings.public_key,
            secret_key=lf_settings.secret_key,
            host=lf_settings.host,
            debug=True
        )
        print("Client initialized.")
    except Exception as e:
        print(f"ERROR initializing client: {e}")
        return

    print("\n--- 3. Send Test Trace ---")
    try:
        trace = langfuse.trace(name="debug-verification-trace")
        span = trace.span(name="debug-span")
        time.sleep(0.5)
        span.end()
        trace.update(output={"status": "verified"})

        print("Trace created. Id:", trace.id)

        print("Flushing...")
        langfuse.flush()
        print("Flush complete.")
    except Exception as e:
        print(f"ERROR sending trace: {e}")

if __name__ == "__main__":
    verify()
