
import os

# Ensure API KEY is set (simulating loaded from .env or env var)
# We won't set "ENABLED" explicitly
if "DOC_INGEST__MODEL__ENABLED" in os.environ:
    del os.environ["DOC_INGEST__MODEL__ENABLED"]

from app.config import get_settings
from app.services.model_service import ModelService

def verify_implicit_model():
    print("--- Starting Implicit Config Verification ---")
    settings = get_settings().model

    # Check if 'enabled' field is gone and we rely on api_key
    print(f"API Key present: {bool(settings.api_key)}")
    if hasattr(settings, 'enabled'):
        print("WARNING: 'enabled' field still exists in settings!")
    else:
        print("SUCCESS: 'enabled' field correctly removed from settings.")

    service = ModelService()
    if service._client:
        print("SUCCESS: ModelService initialized client (implicitly enabled).")
    else:
        print("FAILURE: ModelService did NOT initialize client.")

    print("--- Verification Done ---")

if __name__ == "__main__":
    verify_implicit_model()
